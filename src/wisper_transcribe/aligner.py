from __future__ import annotations

from .models import AlignedSegment, DiarizationSegment, TranscriptionSegment


def align(
    transcription: list[TranscriptionSegment],
    diarization: list[DiarizationSegment],
) -> list[AlignedSegment]:
    """Assign a speaker label to each transcription segment.

    For each transcription segment, finds the diarization segment with the
    maximum time overlap and uses that speaker label. Falls back to "UNKNOWN"
    if no diarization segment overlaps.
    """
    aligned: list[AlignedSegment] = []

    for t_seg in transcription:
        best_speaker = "UNKNOWN"
        best_overlap = 0.0

        for d_seg in diarization:
            overlap_start = max(t_seg.start, d_seg.start)
            overlap_end = min(t_seg.end, d_seg.end)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = d_seg.speaker

        aligned.append(
            AlignedSegment(
                start=t_seg.start,
                end=t_seg.end,
                speaker=best_speaker,
                text=t_seg.text,
            )
        )

    return aligned
