from __future__ import annotations

from typing import Optional

from .models import AlignedSegment, DiarizationSegment, TranscriptionSegment, Word

# Micro-run smoothing thresholds (see _smooth_word_speakers). Higher values
# swallow real short interjections ("yeah"); lower values let boundary jitter
# split sentences. 2 words / 1.0 s read as "part of the sentence" in review.
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
    """Brute-force O(words * turns) implementation -- the correctness reference.

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

    Each word takes the turn with the greatest overlap, else the nearest turn
    by midpoint, else the previous word's speaker ("UNKNOWN" for the first).

    Sweeps words against turns sorted by start, keeping only turns that can
    still overlap, instead of scanning every turn per word (~60M checks for a
    3-hour session). The sweep is only valid for time-ordered words, so
    unordered input goes to ``_assign_word_speakers_bruteforce()``.

    Ties for max overlap go to the turn earliest in the caller's
    ``diarization`` list, matching the brute-force result exactly.
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


def _smooth_word_speakers(
    words: list[Word],
    speakers: list[str],
    max_words: int = _MICRO_RUN_MAX_WORDS,
    max_seconds: float = _MICRO_RUN_MAX_SECONDS,
) -> list[str]:
    """Absorb sandwiched micro-runs into the surrounding speaker.

    Diarization boundaries jitter by a word or two, e.g. A("The quick brown")
    B("fox") A("jumps over"). A run is absorbed when:
    - it has runs on both sides (edge runs always survive),
    - both neighbours are the same speaker, different from this run's
      (an interjection between two different speakers is kept), and
    - it is at most ``max_words`` words OR shorter than ``max_seconds``.

    Repeats to a fixpoint, since one absorption can expose another
    (``A B A B A`` collapses to one A run).
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
            if word_count <= max_words or span < max_seconds:
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
    """Assign speakers to transcription segments.

    With word timestamps, each word gets a speaker (``_assign_word_speakers``),
    micro-runs are smoothed, and consecutive same-speaker words become one
    AlignedSegment, so a segment spanning a speaker change splits at the word
    boundary.

    Segments without word data use the turn with the most overlap over the
    whole segment, or "UNKNOWN".
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
