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
from wisper_transcribe.web.live_transcribe import FORCE_CUT_S, MIN_COMMIT_S, SILENCE_GAP_S

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


def _utterances(segments: list[DiarizationSegment], max_s: float,
                boundaries: str = "pauses") -> list[tuple[float, float]]:
    """The utterances a live session would have committed, in time order.

    ``pauses`` (default) mirrors web/live_transcribe.py: speech from any
    speaker runs on until a SILENCE_GAP_S pause, so back-to-back speakers land
    in one utterance — the case live labelling has to survive. ``turns`` cuts
    at every ground-truth speaker change instead: a best case for comparison.
    Either way a stretch is force-cut every ``max_s`` (FORCE_CUT_S live) and
    anything shorter than MIN_COMMIT_S is dropped, as the live loop never
    commits it.
    """
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    spans: list[list] = []
    for seg in ordered:
        if spans:
            last = spans[-1]
            joins = (seg.start <= last[1] + SILENCE_GAP_S if boundaries == "pauses"
                     else last[2] == seg.speaker and seg.start <= last[1] + 1e-3)
            if joins:
                last[1] = max(last[1], seg.end)
                continue
        spans.append([seg.start, seg.end, seg.speaker])

    windows: list[tuple[float, float]] = []
    for start, end, _speaker in spans:
        cut = start
        while end - cut > max_s:
            windows.append((cut, cut + max_s))
            cut += max_s
        windows.append((cut, end))
    return [(a, b) for a, b in windows if b - a >= MIN_COMMIT_S]


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
    actually sit under that true speaker. Solved as an assignment problem
    (scipy's Hungarian solver): the pool can grow to hundreds of voices when
    utterance embeddings disagree, which an exhaustive search can't handle.
    Returns ``voice_id -> speaker``; voices left unpaired are absent.
    """
    from scipy.optimize import linear_sum_assignment

    voices = sorted(pair_seconds)
    speakers = sorted({sp for row in pair_seconds.values() for sp in row})
    if not voices or not speakers:
        return {}
    gain = np.array([[pair_seconds[v].get(sp, 0.0) for sp in speakers] for v in voices])
    rows, cols = linear_sum_assignment(gain, maximize=True)
    return {voices[r]: speakers[c] for r, c in zip(rows, cols) if gain[r, c] > 0}


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


class TooShort(Exception):
    """The window gave no usable embedding (too short: the model returns NaN)."""


def _evaluate(windows: list[tuple[float, float]],
              segments: list[DiarizationSegment],
              embed, threshold: float) -> tuple[dict, VoicePool, dict[int, str], list[int], list[tuple[float, float]]]:
    """Replay the windows: embed each, assign it online, and score the labels.

    ``embed(start, end)`` returns one unit embedding for that window (patched in
    tests). Returns ``(report, pool, mapping, labels, windows)`` where mapping is
    the best one-to-one voice -> true-speaker assignment, labels is each kept
    utterance's pool id, and windows are the utterances that embedded (failed
    ones are skipped and counted).
    """
    coverage = _coverage_by_speaker(segments)
    pool = VoicePool(threshold)
    latencies: list[float] = []
    labels: list[int] = []
    kept: list[tuple[float, float]] = []
    failures = 0
    too_short: list[tuple[float, float]] = []
    for start, end in windows:
        t0 = time.monotonic()
        try:
            embedding = embed(start, end)
        except InterruptedError:
            raise
        except TooShort:
            # Expected live: a one-word interjection can't be labelled.
            too_short.append((start, end))
            continue
        except Exception:
            # Skip the utterance (a very short excerpt can fail extraction),
            # but count it: if many fail, the numbers would describe the
            # failures, not the voices, so stop.
            failures += 1
            if failures > max(3, 0.05 * len(windows)):
                raise
            continue
        latencies.append(time.monotonic() - t0)
        labels.append(pool.assign(embedding))
        kept.append((start, end))
    windows = kept

    accuracy, mapping = _report_accuracy(windows, coverage, pool, labels)
    arr = np.asarray(latencies)
    report = {
        "meta": {
            "utterances": len(windows),
            "failed_utterances": failures,
            "too_short_utterances": len(too_short),
            "too_short_seconds_fraction": (
                sum(e - s for s, e in too_short)
                / max(1e-9, sum(e - s for s, e in kept) + sum(e - s for s, e in too_short))),
            "longest_too_short_s": max((e - s for s, e in too_short), default=0.0),
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
    return report, pool, mapping, labels, windows


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
    """Fraction of utterance-seconds that show the right final name.

    Scored against who actually speaks in each stretch of the utterance (the
    ground-truth coverage), not against the speaker the voice maps to: an
    utterance holding two speakers is only partly right whatever it shows.
    ``mapping`` is unused here; kept for the caller's signature.
    """
    correct = 0.0
    total = 0.0
    for (start, end), voice in zip(windows, labels):
        total += end - start
        shown = names.get(voice, "")
        for speaker, seconds in _window_coverage(start, end, coverage).items():
            if shown and shown == speaker_map.get(speaker, speaker):
                correct += seconds
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
    # extract_embedding reads WAV only (stored audio is FLAC): convert once.
    from wisper_transcribe.audio_utils import convert_to_wav
    source = audio
    audio = convert_to_wav(source)
    max_s = args.max_utterance_s or DEFAULT_MAX_UTTERANCE_S
    windows = _utterances(segments, max_s, args.boundaries)
    out = Path(args.out) if args.out else Path("live-diarization-eval") / loc.stem
    out.mkdir(parents=True, exist_ok=True)

    print(f"Replaying {len(windows)} utterances from {audio.name} on {device} ...",
          file=sys.stderr)

    # The same model and crop extract_embedding uses, but the audio is loaded
    # once: extract_embedding reloads the whole session per call, which a live
    # loop (embedding an in-memory chunk) would not, so timing it would
    # measure file loading.
    from pyannote.core import Segment as PyannoteSegment
    from wisper_transcribe.audio_utils import load_wav_as_tensor
    inference = speaker_manager._load_embedding_model(device)
    audio_dict = load_wav_as_tensor(audio)

    def embed(start: float, end: float) -> np.ndarray:
        emb = np.asarray(inference.crop(audio_dict, PyannoteSegment(start, end)),
                         dtype=np.float32).reshape(-1)
        if not np.isfinite(emb).all():
            raise TooShort
        return speaker_manager._unit(emb)

    try:
        report, pool, mapping, labels, windows = _evaluate(windows, segments, embed, args.threshold)
    finally:
        if audio != source:
            audio.unlink(missing_ok=True)
    report["meta"].update({
        "transcript": loc.stem, "transcript_id": loc.id, "audio": str(source),
        "device": device, "max_utterance_s": max_s, "boundaries": args.boundaries,
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
    run.add_argument("--boundaries", choices=("pauses", "turns"), default="pauses",
                     help="cut utterances at pauses like the live loop (default), "
                          "or at speaker turns (best case)")
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
