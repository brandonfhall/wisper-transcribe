from __future__ import annotations

from typing import Optional

from .models import AlignedSegment, DiarizationSegment, TranscriptionSegment, Word

# F13: thresholds for the micro-run smoothing pass in `_smooth_word_speakers()`.
# Diarization turn boundaries jitter by a word or two, so a run this short (or
# this brief) sandwiched between two same-speaker runs is far more likely to be
# misattributed boundary noise than a genuine interjection. Set too high and a
# real short interjection ("yeah", "mm-hmm") gets swallowed into the other
# speaker's turn; set too low and sentence-splitting jitter survives. 2 words /
# 1.0s was chosen as the point where a run reads as "part of the sentence"
# rather than "someone else spoke here" in manual review.
_MICRO_RUN_MAX_WORDS = 2
_MICRO_RUN_MAX_SECONDS = 1.0


def _best_overlap_speaker(
    start: float, end: float, diarization: list[DiarizationSegment]
) -> tuple[str, bool]:
    """Return (speaker, found) for the diarization turn with max overlap over [start, end].

    found is False when no turn overlaps at all (best_overlap stayed at 0.0).
    """
    best_speaker = "UNKNOWN"
    best_overlap = 0.0

    for d_seg in diarization:
        overlap_start = max(start, d_seg.start)
        overlap_end = min(end, d_seg.end)
        overlap = max(0.0, overlap_end - overlap_start)

        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = d_seg.speaker

    return best_speaker, best_overlap > 0.0


def _nearest_speaker(midpoint: float, diarization: list[DiarizationSegment]) -> str:
    """Return the speaker of the diarization turn nearest to midpoint (0 if inside)."""
    best_speaker = "UNKNOWN"
    best_distance = None

    for d_seg in diarization:
        if d_seg.start <= midpoint <= d_seg.end:
            distance = 0.0
        else:
            distance = min(abs(midpoint - d_seg.start), abs(midpoint - d_seg.end))

        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_speaker = d_seg.speaker

    return best_speaker


def _assign_word_speakers_bruteforce(
    words: list[Word], diarization: list[DiarizationSegment]
) -> list[str]:
    """Original O(words * turns) implementation -- the correctness reference.

    Kept as the unconditional fallback for `_assign_word_speakers()` below
    when its sweep's sortedness precondition doesn't hold, so identity with
    this function is guaranteed for ANY input, not just time-ordered words.
    """
    speakers: list[str] = []
    prev_speaker = "UNKNOWN"

    for w in words:
        if diarization:
            speaker, found = _best_overlap_speaker(w.start, w.end, diarization)
            if not found:
                midpoint = (w.start + w.end) / 2.0
                speaker = _nearest_speaker(midpoint, diarization)
        else:
            speaker = prev_speaker

        speakers.append(speaker)
        prev_speaker = speaker

    return speakers


def _assign_word_speakers(
    words: list[Word], diarization: list[DiarizationSegment]
) -> list[str]:
    """Assign a speaker label to each word.

    Each word is assigned the diarization turn with max time overlap. A word
    overlapping no turn inherits the nearest turn's speaker by word-midpoint
    distance. If there is no diarization at all, a word falls back to the
    previous word's assigned speaker, or "UNKNOWN" for the first word.

    R27: a 3-hour session can carry ~30k words and ~2k diarization turns; a
    brute-force per-word scan of every turn (`_best_overlap_speaker`) is
    ~60M overlap computations. This assigns turns once by `start` and walks
    words with a sweep (two-pointer) over that ordering, keeping only the
    turns that can still overlap the current word ("active" set) instead of
    rescanning all of them -- amortized O(words + turns) in the common case
    where turns don't all overlap each other, vs. O(words * turns) before.

    The sweep's expiry step drops a turn once its `end` falls behind the
    *current* word's `start`, on the assumption that no later word could
    need it back -- true only when `words` is time-ordered (non-decreasing
    `start`), which always holds for whisper/faster-whisper output but
    isn't a guarantee this function should silently rely on. So identity
    with `_assign_word_speakers_bruteforce()` is made unconditional instead
    of assumed: a single ascending-order check up front routes any
    unsorted input straight to the brute-force reference rather than
    running the sweep on an input it isn't valid for.

    Tie-breaking is bit-identical to the old brute-force scan: for the
    max-overlap turn, `_best_overlap_speaker`'s `if overlap > best_overlap`
    is strict, so among turns tied for the max overlap value, the one
    appearing EARLIEST in the original (caller-supplied) `diarization` list
    order wins -- not the one found first during the sweep. This is
    reproduced here by comparing original list indices among ties rather
    than relying on visit order. The nearest-turn fallback (no word overlaps
    any turn) reuses `_nearest_speaker` unchanged -- it triggers only for
    words diarization doesn't cover at all, which is rare, so it stays
    brute-force without hurting the common-case complexity.
    """
    if not diarization:
        speakers: list[str] = []
        prev_speaker = "UNKNOWN"
        for _ in words:
            speakers.append(prev_speaker)
        return speakers

    for i in range(len(words) - 1):
        if words[i].start > words[i + 1].start:
            return _assign_word_speakers_bruteforce(words, diarization)

    speakers = []
    n = len(diarization)
    order = sorted(range(n), key=lambda i: diarization[i].start)
    sorted_starts = [diarization[i].start for i in order]

    # Active window: (original_index, turn) pairs admitted because their
    # start is before the current word's end, not yet expired because their
    # end is still >= the current word's start.
    active: list[tuple[int, DiarizationSegment]] = []
    next_idx = 0

    for w in words:
        while next_idx < n and sorted_starts[next_idx] <= w.end:
            oi = order[next_idx]
            active.append((oi, diarization[oi]))
            next_idx += 1

        if active:
            active = [(oi, t) for oi, t in active if t.end >= w.start]

        best_oi: Optional[int] = None
        best_overlap = 0.0
        for oi, t in active:
            overlap = min(w.end, t.end) - max(w.start, t.start)
            if overlap <= 0.0:
                continue
            if best_oi is None or overlap > best_overlap or (
                overlap == best_overlap and oi < best_oi
            ):
                best_overlap = overlap
                best_oi = oi

        if best_oi is not None:
            speaker = diarization[best_oi].speaker
        else:
            midpoint = (w.start + w.end) / 2.0
            speaker = _nearest_speaker(midpoint, diarization)

        speakers.append(speaker)

    return speakers


def _find_runs(speakers: list[str]) -> list[tuple[int, int, str]]:
    """Return maximal runs of consecutive equal entries as (start_idx, end_idx, value).

    ``end_idx`` is inclusive. Assumes ``speakers`` is non-empty.
    """
    runs: list[tuple[int, int, str]] = []
    start = 0
    for i in range(1, len(speakers)):
        if speakers[i] != speakers[start]:
            runs.append((start, i - 1, speakers[start]))
            start = i
    runs.append((start, len(speakers) - 1, speakers[start]))
    return runs


def _smooth_word_speakers(words: list[Word], speakers: list[str]) -> list[str]:
    """Absorb sandwiched micro-runs into the surrounding speaker (F13).

    Word-level alignment has no smoothing on its own: a diarization boundary
    that jitters by a word or two produces a short run misattributed to the
    "wrong" speaker mid-sentence, e.g. A("The quick brown") B("fox")
    A("jumps over the lazy dog") -- three rendered blocks where the middle
    one is boundary noise, not a real speaker change.

    A run is absorbed into its neighbors' speaker when ALL of:
    - it has a run on both sides (never the first or last run -- no sandwich
      is possible at the edges, so edge runs always survive untouched)
    - both neighboring runs share the SAME speaker, which differs from this
      run's speaker (a genuine interjection between two *different* speakers
      is never absorbed)
    - the run is short: at most `_MICRO_RUN_MAX_WORDS` words, OR its time
      span is under `_MICRO_RUN_MAX_SECONDS` (either condition is enough --
      the OR catches both "few long words" and "many short words" jitter)

    Runs until no more absorptions happen (fixpoint): absorbing one run can
    make its two neighbors adjacent and therefore mergeable, which can expose
    a further sandwich (e.g. `A B A B A` with tiny B runs collapses to one A
    run). Each absorption strictly reduces the run count, so this terminates.
    """
    speakers = list(speakers)

    changed = True
    while changed:
        changed = False
        runs = _find_runs(speakers)
        if len(runs) < 3:
            break

        for i in range(1, len(runs) - 1):
            start_idx, end_idx, run_speaker = runs[i]
            prev_speaker = runs[i - 1][2]
            next_speaker = runs[i + 1][2]

            if prev_speaker != next_speaker or prev_speaker == run_speaker:
                continue

            word_count = end_idx - start_idx + 1
            span = words[end_idx].end - words[start_idx].start
            if word_count <= _MICRO_RUN_MAX_WORDS or span < _MICRO_RUN_MAX_SECONDS:
                for j in range(start_idx, end_idx + 1):
                    speakers[j] = prev_speaker
                changed = True
                break  # run boundaries shifted -- recompute before continuing

    return speakers


def _group_consecutive_words(words: list[Word], speakers: list[str]) -> list[AlignedSegment]:
    """Group consecutive same-speaker words into AlignedSegments."""
    segments: list[AlignedSegment] = []
    run_words: list[Word] = []
    run_speaker = None

    for w, speaker in zip(words, speakers):
        if run_speaker is not None and speaker != run_speaker:
            segments.append(
                AlignedSegment(
                    start=run_words[0].start,
                    end=run_words[-1].end,
                    speaker=run_speaker,
                    text=" ".join(rw.text for rw in run_words),
                )
            )
            run_words = []
        run_speaker = speaker
        run_words.append(w)

    if run_words:
        segments.append(
            AlignedSegment(
                start=run_words[0].start,
                end=run_words[-1].end,
                speaker=run_speaker,
                text=" ".join(rw.text for rw in run_words),
            )
        )

    return segments


def align(
    transcription: list[TranscriptionSegment],
    diarization: list[DiarizationSegment],
) -> list[AlignedSegment]:
    """Assign speaker label(s) to each transcription segment.

    When a segment carries word-level timestamps, each word is assigned to
    the diarization turn with max time overlap (falling back to the nearest
    turn by midpoint distance when no turn overlaps). The per-word speaker
    list is then smoothed (F13, `_smooth_word_speakers()`) to absorb sandwiched
    micro-runs caused by diarization boundary jitter, before consecutive
    same-speaker words are grouped into one AlignedSegment per run — so a
    segment spanning multiple speaker turns splits at the word boundary
    instead of being attributed wholesale to the majority speaker, without
    fragmenting mid-sentence on a one- or two-word jitter.

    Segments without word data (None or empty `words` — e.g. older callers
    or mocked tests) fall back to the original whole-segment max-overlap
    behavior: the diarization turn with the most time overlap over the
    segment's [start, end], or "UNKNOWN" if none overlaps. The smoothing pass
    only operates on per-word speaker lists, so this fallback is unaffected.
    """
    aligned: list[AlignedSegment] = []

    for t_seg in transcription:
        if t_seg.words:
            speakers = _assign_word_speakers(t_seg.words, diarization)
            speakers = _smooth_word_speakers(t_seg.words, speakers)
            aligned.extend(_group_consecutive_words(t_seg.words, speakers))
            continue

        best_speaker, _ = _best_overlap_speaker(t_seg.start, t_seg.end, diarization)
        aligned.append(
            AlignedSegment(
                start=t_seg.start,
                end=t_seg.end,
                speaker=best_speaker,
                text=t_seg.text,
            )
        )

    return aligned
