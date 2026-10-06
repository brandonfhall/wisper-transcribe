"""Offline harness for per-speaker labels on the live system track.

Replays a recorded session in the same "live utterance" sizes the live loop
commits (see ``web/live_transcribe.py``), computes one WeSpeaker embedding per
utterance, and assigns each utterance to a per-session voice pool online
(cosine against running-mean centroids, opening a new voice below threshold).
The transcript's final pyannote diarization is the ground truth:

  run    Replay one transcript's audio and diarization, write <out>/report.json,
         and print per-utterance latency, label accuracy, pool size, and overlap.
  score  Compare several runs (e.g. thresholds) in one table.

Example:

  python scripts/live_diarization_eval.py run session-30m --threshold 0.55
  python scripts/live_diarization_eval.py run 812 --threshold 0.60 --out live-diarization-eval/s812-60
  python scripts/live_diarization_eval.py score live-diarization-eval/*/

Output goes to ``live-diarization-eval/<stem>/`` (gitignored: it holds real
audio). Nothing here changes the product; the post-session pyannote pass stays
the authoritative transcript.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

from wisper_transcribe import speaker_manager
from wisper_transcribe.config import DEFAULT_SIMILARITY_THRESHOLD
from wisper_transcribe.models import DiarizationSegment
from wisper_transcribe.speaker_manager import _cosine_similarity
from wisper_transcribe.web.live_transcribe import FORCE_CUT_S

# The live loop force-cuts a chunk once it reaches FORCE_CUT_S seconds of
# continuous speech (web/live_transcribe.py:FORCE_CUT_S); shorter chunks end at
# a SILENCE_GAP_S pause. So FORCE_CUT_S is the longest a committed system-track
# utterance can be, and the harness default.
DEFAULT_MAX_UTTERANCE_S = FORCE_CUT_S
OVERLAP_FRACTION = 0.2


# ---------------------------------------------------------------------------
# Loading a transcript and building live-sized utterances
# ---------------------------------------------------------------------------


def _resolve_transcript(ref: str):
    """The Located for a transcript id (all digits) or a stem, or exit."""
    from wisper_transcribe import transcript_store

    if ref.isdigit():
        loc = transcript_store.locate(int(ref))
    else:
        found = transcript_store.find_by_stem(ref)
        loc = found[0] if found else None
    if loc is None:
        sys.exit(f"No transcript matches {ref!r} (run inside the data dir, or pass an id)")
    return loc


def _utterances(segments: list[DiarizationSegment],
                max_s: float) -> list[tuple[float, float]]:
    """Ground-truth turns in time order, each split to at most ``max_s``.

    A turn is a stretch one ground-truth speaker holds; pyannote emits
    consecutive same-speaker turns, so merge them first. The split mirrors the
    live loop's force-cut of long continuous speech.
    """
    turns: list[tuple[float, float, str]] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        if turns and turns[-1][2] == seg.speaker and seg.start <= turns[-1][1] + 1e-3:
            turns[-1] = (turns[-1][0], max(turns[-1][1], seg.end), seg.speaker)
        else:
            turns.append((seg.start, seg.end, seg.speaker))

    windows: list[tuple[float, float]] = []
    for start, end, _speaker in turns:
        n = max(1, int(np.ceil((end - start) / max_s)))
        step = (end - start) / n
        for i in range(n):
            windows.append((start + i * step, end if i == n - 1 else start + (i + 1) * step))
    return windows


def _coverage_by_speaker(segments: list[DiarizationSegment]) -> dict[str, list[tuple[float, float]]]:
    """Per speaker, its turns merged into non-overlapping intervals."""
    by_speaker: dict[str, list[tuple[float, float]]] = {}
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        turns = by_speaker.setdefault(seg.speaker, [])
        if turns and seg.start <= turns[-1][1]:
            turns[-1] = (turns[-1][0], max(turns[-1][1], seg.end))
        else:
            turns.append((seg.start, seg.end))
    return by_speaker


def _window_coverage(start: float, end: float,
                     coverage: dict[str, list[tuple[float, float]]]) -> dict[str, float]:
    """Seconds each ground-truth speaker sounds inside ``[start, end)``."""
    out: dict[str, float] = {}
    for speaker, turns in coverage.items():
        seconds = 0.0
        for a, b in turns:
            over = min(end, b) - max(start, a)
            if over > 0:
                seconds += over
        if seconds > 0:
            out[speaker] = seconds
    return out


def _dominant_speaker(start: float, end: float,
                      coverage: dict[str, list[tuple[float, float]]]) -> str | None:
    """The speaker holding the most of an utterance window, or None."""
    cover = _window_coverage(start, end, coverage)
    return max(cover, key=cover.get) if cover else None


def _overlap_fraction(windows: list[tuple[float, float]],
                      coverage: dict[str, list[tuple[float, float]]]) -> float:
    """Fraction of utterances where a second true speaker covers > OVERLAP_FRACTION."""
    if not windows:
        return 0.0
    overlapping = 0
    for start, end in windows:
        duration = end - start
        cover = sorted(_window_coverage(start, end, coverage).values(), reverse=True)
        if len(cover) > 1 and cover[1] > OVERLAP_FRACTION * duration:
            overlapping += 1
    return overlapping / len(windows)


# ---------------------------------------------------------------------------
# Online voice pool
# ---------------------------------------------------------------------------


class VoicePool:
    """Per-session voices grown online from unit embeddings.

    An utterance whose cosine against the closest running-mean centroid is at
    least ``threshold`` joins that voice (its centroid is updated); otherwise it
    opens a new voice. This is exactly the live candidate design, replayed.
    """

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.centroids: list[np.ndarray] = []
        self.counts: list[int] = []

    def assign(self, embedding: np.ndarray) -> int:
        best_id, best_sim = -1, -1.0
        for i, centroid in enumerate(self.centroids):
            sim = _cosine_similarity(embedding, centroid)
            if sim > best_sim:
                best_id, best_sim = i, sim
        if best_id >= 0 and best_sim >= self.threshold:
            total = self.centroids[best_id] * self.counts[best_id] + embedding
            norm = np.linalg.norm(total)
            self.centroids[best_id] = total / norm if norm > 0 else total
            self.counts[best_id] += 1
            return best_id
        self.centroids.append(np.asarray(embedding, dtype=np.float32))
        self.counts.append(1)
        return len(self.centroids) - 1


def _best_mapping(pair_seconds: dict[int, dict[str, float]]) -> dict[int, str]:
    """One-to-one voice->speaker map maximizing the utterance-seconds it explains.

    ``pair_seconds[voice][speaker]`` is how many seconds that voice's utterances
    actually sit under that true speaker. Any voice may be paired with any
    speaker or left unassigned; every assignment is tried exactly (pool and
    speaker counts are small). Returns ``voice_id -> speaker``; unmapped voices
    are absent from the result.
    """
    ids = sorted(pair_seconds, key=lambda i: -sum(pair_seconds[i].values()))
    best: tuple[float, dict[int, str]] = (-1.0, {})

    def walk(i: int, used: frozenset, chosen: dict[int, str], score: float) -> None:
        nonlocal best
        if i == len(ids):
            if score > best[0]:
                best = (score, dict(chosen))
            return
        if score + sum(sum(pair_seconds[j].values()) for j in ids[i:]) <= best[0]:
            return  # cannot beat the incumbent
        vid = ids[i]
        for speaker, seconds in pair_seconds[vid].items():
            if speaker in used:
                continue
            chosen[vid] = speaker
            walk(i + 1, used | {speaker}, chosen, score + seconds)
        chosen.pop(vid, None)
        walk(i + 1, used, chosen, score)  # leave this voice unassigned

    walk(0, frozenset(), {}, 0.0)
    return best[1]


def _report_accuracy(windows: list[tuple[float, float]],
                     coverage: dict[str, list[tuple[float, float]]],
                     pool: VoicePool, labels: list[int]) -> tuple[float, dict[int, str]]:
    """Best one-to-one label accuracy over utterance-seconds.

    For each pool voice, sum the seconds its utterances sit under each true
    speaker, then pick the assignment maximizing the total. Returns
    ``(accuracy, voice_id -> true speaker)``.
    """
    pair_seconds: dict[int, dict[str, float]] = {i: {} for i in range(len(pool.centroids))}
    for (start, end), voice in zip(windows, labels):
        for speaker, seconds in _window_coverage(start, end, coverage).items():
            pair_seconds[voice][speaker] = pair_seconds[voice].get(speaker, 0.0) + seconds
    mapping = _best_mapping(pair_seconds)
    correct = sum(pair_seconds[vid][speaker] for vid, speaker in mapping.items())
    total = sum(end - start for start, end in windows)
    return (correct / total if total else 0.0), mapping


def _evaluate(windows: list[tuple[float, float]],
              segments: list[DiarizationSegment],
              embed, threshold: float) -> tuple[dict, VoicePool, dict[int, str], list[int]]:
    """Replay the windows: embed each, assign it online, and score the labels.

    ``embed(start, end)`` returns one unit embedding for that window (patched in
    tests). Returns ``(report, pool, mapping, labels)`` where mapping is the best
    one-to-one voice -> true-speaker assignment and labels is each utterance's
    pool id.
    """
    coverage = _coverage_by_speaker(segments)
    pool = VoicePool(threshold)
    latencies: list[float] = []
    labels: list[int] = []
    for start, end in windows:
        t0 = time.monotonic()
        try:
            embedding = embed(start, end)
        except InterruptedError:
            raise  # Stop: not one failed utterance
        except Exception:
            # A sub-100 ms excerpt can fail extraction; treat it as no voice
            # (a zero vector opens a new pool entry rather than stealing one).
            embedding = np.zeros(pool.centroids[0].size if pool.centroids else 256, dtype=np.float32)
        latencies.append(time.monotonic() - t0)
        labels.append(pool.assign(embedding))

    accuracy, mapping = _report_accuracy(windows, coverage, pool, labels)
    arr = np.asarray(latencies)
    report = {
        "meta": {
            "utterances": len(windows),
            "ground_truth_speakers": len(coverage),
            "threshold": threshold,
            "embedding_dim": int(pool.centroids[0].size) if pool.centroids else 0,
        },
        "latency_s": {
            "mean": float(arr.mean()) if len(arr) else 0.0,
            "p50": float(np.percentile(arr, 50)) if len(arr) else 0.0,
            "p95": float(np.percentile(arr, 95)) if len(arr) else 0.0,
            "max": float(arr.max()) if len(arr) else 0.0,
        },
        "pool_voices": len(pool.centroids),
        "true_speakers": len(coverage),
        "label_accuracy": accuracy,
        "overlap_fraction": _overlap_fraction(windows, coverage),
        "voices": [
            {"id": i, "utterances": pool.counts[i], "speaker": mapping.get(i, "")}
            for i in range(len(pool.centroids))
        ],
    }
    return report, pool, mapping, labels


def _profile_match(pool: VoicePool, threshold: float) -> tuple[dict[int, str], bool]:
    """Match each pool voice to an enrolled profile the way match_speakers does.

    The pool's centroids stand in for the labels ``match_speakers`` would
    extract, and ``assign_labels`` applies the same score-then-assign rules.
    Returns ``(voice_id -> display name, any profiles enrolled)``.
    """
    profiles = speaker_manager.load_profiles()
    if not profiles:
        return {}, False
    labels = [f"VOICE_{i:02d}" for i in range(len(pool.centroids))]
    queries = {label: pool.centroids[i] for i, label in enumerate(labels)}
    assigned = speaker_manager.assign_labels(labels, queries, profiles, threshold=threshold)
    return {i: assigned[label] for i, label in enumerate(labels)}, True


def _name_accuracy(windows: list[tuple[float, float]],
                   coverage: dict[str, list[tuple[float, float]]],
                   mapping: dict[int, str], labels: list[int],
                   names: dict[int, str], speaker_map: dict[str, str]) -> float:
    """Fraction of utterance-seconds whose pool voice shows the right final name."""
    correct = 0.0
    total = 0.0
    for (start, end), voice in zip(windows, labels):
        total += end - start
        truth = mapping.get(voice)
        if truth is not None and names.get(voice, "") == speaker_map.get(truth, truth):
            correct += end - start
    return correct / total if total else 0.0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args) -> None:
    from wisper_transcribe import transcript_store
    from wisper_transcribe.config import get_device

    loc = _resolve_transcript(args.transcript)
    sidecar = transcript_store.read_sidecar(loc.md)
    if not sidecar or not sidecar.get("diarization_segments"):
        sys.exit(f"{loc.stem}: no diarization sidecar (nothing to replay)")
    segments = [
        DiarizationSegment(s["start"], s["end"], s["speaker"])
        for s in sidecar["diarization_segments"]
    ]
    audio = transcript_store.audio_path(loc.md)
    if audio is None:
        sys.exit(f"{loc.stem}: no audio found next to the transcript")

    device = get_device() if args.device == "auto" else args.device
    max_s = args.max_utterance_s or DEFAULT_MAX_UTTERANCE_S
    windows = _utterances(segments, max_s)
    out = Path(args.out) if args.out else Path("live-diarization-eval") / loc.stem
    out.mkdir(parents=True, exist_ok=True)

    print(f"Replaying {len(windows)} utterances from {audio.name} on {device} ...",
          file=sys.stderr)

    def embed(start: float, end: float) -> np.ndarray:
        one = [DiarizationSegment(start, end, "UTT")]
        return speaker_manager.extract_embedding(audio, one, "UTT", device)

    report, pool, mapping, labels = _evaluate(windows, segments, embed, args.threshold)
    report["meta"].update({
        "transcript": loc.stem, "transcript_id": loc.id, "audio": str(audio),
        "device": device, "max_utterance_s": max_s,
        "default_max_utterance_s": DEFAULT_MAX_UTTERANCE_S,
    })

    names, has_profiles = _profile_match(pool, args.threshold)
    report["profiles_enrolled"] = has_profiles
    if has_profiles:
        speaker_map = sidecar.get("speaker_map") or {}
        report["name_accuracy"] = _name_accuracy(
            windows, _coverage_by_speaker(segments), mapping, labels, names, speaker_map)
        report["voice_names"] = {f"VOICE_{i:02d}": name for i, name in names.items()}
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    _print_report(report, out)


def _print_report(report: dict, out: Path) -> None:
    lat = report["latency_s"]
    print(f"Wrote {out / 'report.json'}")
    print(f"  utterances {report['meta']['utterances']}, "
          f"voices {report['pool_voices']} vs {report['true_speakers']} true speakers")
    print(f"  label accuracy {report['label_accuracy']:.1%}, "
          f"overlap {report['overlap_fraction']:.1%}")
    print(f"  embedding latency mean {lat['mean'] * 1000:.1f} ms, "
          f"p50 {lat['p50'] * 1000:.1f}, p95 {lat['p95'] * 1000:.1f}, "
          f"max {lat['max'] * 1000:.1f}")


def cmd_score(args) -> None:
    header = (f"{'run':32} {'thr':>5} {'acc':>6} {'voices':>6} {'true':>4} "
              f"{'overlap':>7} {'p95 ms':>7} {'name':>6}")
    print(header)
    for out in map(Path, args.outs):
        path = out / "report.json"
        if not path.exists():
            print(f"{str(out):32} (no report.json)")
            continue
        r = json.loads(path.read_text(encoding="utf-8"))
        name = r.get("name_accuracy")
        name_s = f"{name:.1%}" if name is not None else "-"
        print(f"{str(out):32} {r['meta']['threshold']:5.2f} "
              f"{r['label_accuracy']:6.1%} {r['pool_voices']:6d} {r['true_speakers']:4d} "
              f"{r['overlap_fraction']:7.1%} {r['latency_s']['p95'] * 1000:7.1f} {name_s:>6}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="replay one transcript's audio and diarization")
    run.add_argument("transcript", help="transcript id or stem")
    run.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD,
                     help=f"cosine to join a pool voice (default {DEFAULT_SIMILARITY_THRESHOLD})")
    run.add_argument("--max-utterance-s", type=float, default=None,
                     help=f"split longer turns (default {DEFAULT_MAX_UTTERANCE_S:g})")
    run.add_argument("--out", default=None,
                     help="output folder (default live-diarization-eval/<stem>)")
    run.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"])
    run.set_defaults(func=cmd_run)

    score = sub.add_parser("score", help="compare several runs in one table")
    score.add_argument("outs", nargs="+")
    score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
