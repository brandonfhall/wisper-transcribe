from __future__ import annotations

from .models import AlignedSegment, DiarizationSegment, TranscriptionSegment, Word


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


def _assign_word_speakers(
    words: list[Word], diarization: list[DiarizationSegment]
) -> list[str]:
    """Assign a speaker label to each word.

    Each word is assigned the diarization turn with max time overlap. A word
    overlapping no turn inherits the nearest turn's speaker by word-midpoint
    distance. If there is no diarization at all, a word falls back to the
    previous word's assigned speaker, or "UNKNOWN" for the first word.
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
    turn by midpoint distance when no turn overlaps), and consecutive
    same-speaker words are grouped into one AlignedSegment per run — so a
    segment spanning multiple speaker turns splits at the word boundary
    instead of being attributed wholesale to the majority speaker.

    Segments without word data (None or empty `words` — e.g. older callers
    or mocked tests) fall back to the original whole-segment max-overlap
    behavior: the diarization turn with the most time overlap over the
    segment's [start, end], or "UNKNOWN" if none overlaps.
    """
    aligned: list[AlignedSegment] = []

    for t_seg in transcription:
        if t_seg.words:
            speakers = _assign_word_speakers(t_seg.words, diarization)
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
