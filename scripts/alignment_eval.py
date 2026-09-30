"""Measure forced word alignment on your own audio.

Four steps, each a subcommand. Output goes to ``alignment-eval/`` (gitignored:
it holds a copy of the audio).

  run    Cut an excerpt, transcribe, diarize and align it with the production
         code, save everything to <out>/eval.json, and print the automatic
         proxy for each arm (see ARMS).
  audit  Re-transcribe short windows around every word alignment moved by
         more than 1 s, and count which placement Whisper actually hears.
  sheet  Write <out>/sheet.csv: the words either side of every speaker change
         any arm produces, for a person to label blind (no arm's answer is
         shown), plus <out>/speakers.txt with sample times for each voice.
  score  Read filled-in sheets and report boundary-word accuracy per arm.

Example:

  python scripts/alignment_eval.py run session.mp3 --start 1800 --duration 180 \\
      --out alignment-eval/session-30m
  python scripts/alignment_eval.py audit alignment-eval/session-30m
  python scripts/alignment_eval.py sheet alignment-eval/session-30m
  # listen to clip.wav, fill the correct_speaker column in sheet.csv
  python scripts/alignment_eval.py score alignment-eval/*/

The proxy (share of words whose midpoint lies inside a diarization turn of
their assigned speaker) is a sanity check only: aligned words shrink onto
speech, which is where turns are, so it partly measures two acoustic
segmentations agreeing. The labelled sheet is the real test.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from wisper_transcribe.aligner import (
    _MICRO_RUN_MAX_SECONDS,
    _MICRO_RUN_MAX_WORDS,
    _assign_word_speakers,
    _find_runs,
    _smooth_word_speakers,
)
from wisper_transcribe.models import DiarizationSegment, Word

# name -> (word timing, diarization view, smoothing (max_words, max_seconds) or None).
# "guarded" timing is aligned, except words moved more than _GUARD_SECONDS
# keep Whisper's time.
_DEFAULT_SMOOTHING = (_MICRO_RUN_MAX_WORDS, _MICRO_RUN_MAX_SECONDS)
_GUARD_SECONDS = 1.0
ARMS = {
    "whisper": ("whisper", "regular", _DEFAULT_SMOOTHING),
    "aligned": ("aligned", "regular", _DEFAULT_SMOOTHING),
    "aligned-guard-1s": ("guarded", "regular", _DEFAULT_SMOOTHING),
    "aligned-smooth-1w": ("aligned", "regular", (1, 0.5)),
    "aligned-nosmooth": ("aligned", "regular", None),
    "aligned-exclusive": ("aligned", "exclusive", _DEFAULT_SMOOTHING),
    "aligned-exclusive-nosmooth": ("aligned", "exclusive", None),
}

_STOPWORDS = set(
    "the a an and or but if of to in on at by for with from as is are was were be been "
    "it its this that these those i you he she we they me him her us them my your his our "
    "their not no yes so do does did have has had will would can could should just like".split()
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load(out: Path) -> dict:
    return json.loads((out / "eval.json").read_text(encoding="utf-8"))


def _turns(data: dict, view: str) -> list[DiarizationSegment]:
    return [DiarizationSegment(*t) for t in data[view]]


def _norm(text: str) -> str:
    return re.sub(r"[^\w']", "", text.lower())


def _fmt(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:05.2f}"


def _timing(w: dict, timing: str) -> list[float]:
    if timing == "guarded":
        moved = abs(sum(w["aligned"]) / 2 - sum(w["whisper"]) / 2) > _GUARD_SECONDS
        return w["whisper"] if moved else w["aligned"]
    return w[timing]


def assign(data: dict, arm: str) -> tuple[list[str], int]:
    """Flat per-word speakers for ``arm``, and the micro-run count before smoothing.

    Mirrors aligner.align(): assignment and smoothing run per transcription
    segment.
    """
    timing, view, smoothing = ARMS[arm]
    turns = _turns(data, view)
    speakers: list[str] = []
    micro = 0
    for seg in data["segments"]:
        words = [Word(*_timing(w, timing), w["text"]) for w in seg["words"]]
        if not words:
            continue
        sp = _assign_word_speakers(words, turns)
        runs = _find_runs(sp)
        micro += sum(1 for a, b, _ in runs[1:-1] if b - a + 1 <= 2)
        if smoothing:
            sp = _smooth_word_speakers(words, sp, *smoothing)
        speakers.extend(sp)
    return speakers, micro


def _flat_words(data: dict) -> list[dict]:
    return [w for seg in data["segments"] for w in seg["words"]]


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _annotation_turns(annotation) -> list[list]:
    return [[t.start, t.end, spk] for t, _, spk in annotation.itertracks(yield_label=True)]


def cmd_run(args) -> None:
    import time

    from wisper_transcribe import diarizer
    from wisper_transcribe.audio_utils import load_wav_as_tensor
    from wisper_transcribe.config import get_device, get_hf_token, load_config
    from wisper_transcribe.transcriber import transcribe
    from wisper_transcribe.word_alignment import align_words

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    clip = out / "clip.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(args.start), "-t", str(args.duration),
         "-i", str(args.audio), "-ac", "1", "-ar", "16000", str(clip)],
        check=True,
    )

    cfg = load_config()
    device = get_device() if args.device == "auto" else args.device
    language = args.language or cfg.get("language", "en")
    language = None if language == "auto" else language

    print(f"Transcribing on {device} ...", file=sys.stderr)
    segments = transcribe(
        clip, model_size=cfg.get("model", "large-v3-turbo"), device=device, language=language,
        compute_type=cfg.get("compute_type", "auto"), vad_filter=cfg.get("vad_filter", True),
        use_mlx=cfg.get("use_mlx", "auto"),
    )

    print("Diarizing ...", file=sys.stderr)
    pipeline = diarizer.load_pipeline(get_hf_token(cfg), device)
    kwargs = (
        {"num_speakers": args.num_speakers} if args.num_speakers
        else {"min_speakers": cfg.get("min_speakers"), "max_speakers": cfg.get("max_speakers")}
    )
    raw = pipeline(load_wav_as_tensor(clip), **{k: v for k, v in kwargs.items() if v})

    print("Aligning ...", file=sys.stderr)
    t0 = time.monotonic()
    aligned, _stats = align_words(clip, segments, device, language)
    align_seconds = time.monotonic() - t0

    data = {
        "meta": {
            "audio": str(args.audio), "start": args.start, "duration": args.duration,
            "device": device, "model": cfg.get("model"), "language": language,
            "align_seconds": round(align_seconds, 1),
        },
        "segments": [
            {"words": [
                {"text": w.text, "whisper": [w.start, w.end], "aligned": [a.start, a.end]}
                for w, a in zip(s.words or [], a_seg.words or [])
            ]}
            for s, a_seg in zip(segments, aligned)
        ],
        "regular": _annotation_turns(raw.speaker_diarization),
        "exclusive": _annotation_turns(raw.exclusive_speaker_diarization),
    }
    (out / "eval.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"Wrote {out / 'eval.json'}  (alignment took {align_seconds:.1f}s)")
    _print_proxy(data)


def _print_proxy(data: dict) -> None:
    words = _flat_words(data)
    regular = _turns(data, "regular")
    shifted = sum(
        abs(sum(w["aligned"]) / 2 - sum(w["whisper"]) / 2) > 1.0 for w in words
    )
    print(f"\n{len(words)} words, {len(regular)} turns, {shifted} words shifted >1 s by alignment\n")
    print(f"{'arm':30} {'proxy':>7} {'micro-runs':>11} {'changes':>8}")
    for arm, (timing, _view, _s) in ARMS.items():
        speakers, micro = assign(data, arm)
        ok = 0
        for w, spk in zip(words, speakers):
            mid = sum(_timing(w, timing)) / 2
            ok += any(t.speaker == spk and t.start <= mid <= t.end for t in regular)
        changes = sum(a != b for a, b in zip(speakers, speakers[1:]))
        print(f"{arm:30} {ok / max(len(words), 1):7.1%} {micro:11d} {changes:8d}")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def cmd_audit(args) -> None:
    import numpy as np
    import scipy.io.wavfile as wavfile

    from wisper_transcribe.config import load_config
    from wisper_transcribe.transcriber import transcribe

    out = Path(args.out)
    data = _load(out)
    sr, audio = wavfile.read(str(out / "clip.wav"))
    cfg = load_config()
    device = data["meta"]["device"]

    def heard(word: str, t: float) -> bool:
        a, b = max(0, int((t - 0.5) * sr)), min(len(audio), int((t + 1.0) * sr))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.wav"
            wavfile.write(str(path), sr, audio[a:b].astype(np.int16))
            segs = transcribe(path, model_size=cfg.get("model", "large-v3-turbo"), device=device,
                              language=data["meta"]["language"], vad_filter=False,
                              use_mlx=cfg.get("use_mlx", "auto"))
        return word in {_norm(t) for s in segs for t in s.text.split()}

    tally = {"aligned": 0, "whisper": 0, "both": 0, "neither": 0}
    content = {"aligned": 0, "whisper": 0}
    for w in _flat_words(data):
        if abs(sum(w["aligned"]) / 2 - sum(w["whisper"]) / 2) <= 1.0:
            continue
        word = _norm(w["text"])
        if not word:
            continue
        at_aligned, at_whisper = heard(word, w["aligned"][0]), heard(word, w["whisper"][0])
        key = ("both" if at_aligned and at_whisper else "aligned" if at_aligned
               else "whisper" if at_whisper else "neither")
        tally[key] += 1
        if key in content and len(word) >= 4 and word not in _STOPWORDS:
            content[key] += 1
        print(f"  {w['text']!r:18} whisper {_fmt(w['whisper'][0])}  aligned {_fmt(w['aligned'][0])}  heard: {key}")

    print(f"\nShifted >1 s, heard at exactly one placement: aligned {tally['aligned']} vs "
          f"whisper {tally['whisper']} (both {tally['both']}, neither {tally['neither']})")
    print(f"Content words only (>=4 letters, no stopwords): aligned {content['aligned']} vs "
          f"whisper {content['whisper']}")


# ---------------------------------------------------------------------------
# sheet / score
# ---------------------------------------------------------------------------


def cmd_sheet(args) -> None:
    out = Path(args.out)
    data = _load(out)
    words = _flat_words(data)

    rows: set[int] = set()
    preds = [assign(data, arm)[0] for arm in ARMS]
    for speakers in preds:
        for i in range(len(speakers) - 1):
            if speakers[i] != speakers[i + 1]:
                rows.update(j for j in range(i - 1, i + 3) if 0 <= j < len(words))
    # Only rows where arms disagree can separate them; label those first.
    # Still blind: the sheet never says which arm said what.
    disagree = {i for i in rows if len({p[i] for p in preds}) > 1}

    sheet = out / "sheet.csv"
    if sheet.exists() and not args.force:
        sys.exit(f"{sheet} exists (it may hold your labels); pass --force to overwrite")
    with sheet.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["word_index", "discriminating", "clip_time", "source_time", "word",
                         "context", "correct_speaker"])
        for i in sorted(rows, key=lambda i: (i not in disagree, i)):
            t = words[i]["aligned"][0]
            context = " ".join(
                (f"[{w['text']}]" if j == i else w["text"])
                for j, w in enumerate(words[max(0, i - 4): i + 5], start=max(0, i - 4))
            )
            writer.writerow([i, "yes" if i in disagree else "", _fmt(t), _fmt(t + data["meta"]["start"]),
                             words[i]["text"], context, ""])

    legend = out / "speakers.txt"
    lines = ["Each label's three longest turns (clip time), to learn the voices:"]
    by_speaker: dict[str, list] = {}
    for start, end, spk in data["regular"]:
        by_speaker.setdefault(spk, []).append((end - start, start, end))
    for spk in sorted(by_speaker):
        longest = sorted(by_speaker[spk], reverse=True)[:3]
        lines.append(f"  {spk}: " + ", ".join(f"{_fmt(s)}-{_fmt(e)}" for _, s, e in longest))
    lines += [
        "",
        "Fill correct_speaker with the label who actually says the bracketed word.",
        "Rows marked discriminating (listed first) are the ones that decide the result;",
        "the rest only matter if you want to check that nothing got worse.",
        "Leave it blank or write ? if unsure; write overlap if two people say it at once.",
    ]
    legend.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} rows ({len(disagree)} discriminating) to {sheet} and the voice legend to {legend}")


def cmd_score(args) -> None:
    totals = {arm: [0, 0] for arm in ARMS}  # [correct, labelled]
    wins = {arm: [0, 0] for arm in ARMS}    # vs whisper: [arm only right, whisper only right]
    for out in map(Path, args.outs):
        data = _load(out)
        with (out / "sheet.csv").open(encoding="utf-8") as f:
            labels = {
                int(r["word_index"]): r["correct_speaker"].strip()
                for r in csv.DictReader(f)
                if r["correct_speaker"].strip() not in ("", "?")
            }
        if not labels:
            print(f"{out}: no labels yet")
            continue
        preds = {arm: assign(data, arm)[0] for arm in ARMS}
        line = []
        for arm in ARMS:
            right = sum(preds[arm][i] == lab for i, lab in labels.items())
            totals[arm][0] += right
            totals[arm][1] += len(labels)
            for i, lab in labels.items():
                a_ok, w_ok = preds[arm][i] == lab, preds["whisper"][i] == lab
                wins[arm][0] += a_ok and not w_ok
                wins[arm][1] += w_ok and not a_ok
            line.append(f"{arm} {right}/{len(labels)}")
        print(f"{out}: " + ", ".join(line))

    print(f"\n{'arm':30} {'accuracy':>9} {'labelled':>9} {'vs whisper (won/lost)':>22}")
    for arm, (right, n) in totals.items():
        if n:
            print(f"{arm:30} {right / n:9.1%} {n:9d} {wins[arm][0]:>13d}/{wins[arm][1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="transcribe, diarize and align an excerpt")
    run.add_argument("audio", type=Path)
    run.add_argument("--start", type=float, default=0.0, help="excerpt start, seconds")
    run.add_argument("--duration", type=float, default=180.0, help="excerpt length, seconds")
    run.add_argument("--out", required=True)
    run.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"])
    run.add_argument("--language", default=None, help="default: from config")
    run.add_argument("--num-speakers", type=int, default=None)
    run.set_defaults(func=cmd_run)

    audit = sub.add_parser("audit", help="re-transcription check of >1 s shifts")
    audit.add_argument("out")
    audit.set_defaults(func=cmd_audit)

    sheet = sub.add_parser("sheet", help="write the blind labelling sheet")
    sheet.add_argument("out")
    sheet.add_argument("--force", action="store_true", help="overwrite an existing sheet.csv")
    sheet.set_defaults(func=cmd_sheet)

    score = sub.add_parser("score", help="accuracy per arm from labelled sheets")
    score.add_argument("outs", nargs="+")
    score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
