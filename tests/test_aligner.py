import pytest

from wisper_transcribe.aligner import align
from wisper_transcribe.models import AlignedSegment, DiarizationSegment, TranscriptionSegment


def test_align_basic_overlap():
    transcription = [
        TranscriptionSegment(start=0.0, end=5.0, text="Hello world"),
        TranscriptionSegment(start=5.0, end=10.0, text="How are you"),
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=5.5, speaker="SPEAKER_00"),
        DiarizationSegment(start=5.5, end=10.0, speaker="SPEAKER_01"),
    ]
    result = align(transcription, diarization)

    assert len(result) == 2
    assert isinstance(result[0], AlignedSegment)
    assert result[0].speaker == "SPEAKER_00"
    assert result[0].text == "Hello world"
    assert result[1].speaker == "SPEAKER_01"
    assert result[1].text == "How are you"


def test_align_picks_max_overlap():
    # Transcription segment mostly in SPEAKER_01 time
    transcription = [TranscriptionSegment(start=3.0, end=8.0, text="Middle segment")]
    diarization = [
        DiarizationSegment(start=0.0, end=4.0, speaker="SPEAKER_00"),  # 1s overlap
        DiarizationSegment(start=4.0, end=10.0, speaker="SPEAKER_01"),  # 4s overlap
    ]
    result = align(transcription, diarization)

    assert result[0].speaker == "SPEAKER_01"


def test_align_unknown_when_no_overlap():
    transcription = [TranscriptionSegment(start=20.0, end=25.0, text="Orphaned segment")]
    diarization = [
        DiarizationSegment(start=0.0, end=10.0, speaker="SPEAKER_00"),
    ]
    result = align(transcription, diarization)

    assert result[0].speaker == "UNKNOWN"


def test_align_preserves_timestamps():
    transcription = [TranscriptionSegment(start=1.5, end=3.7, text="Test")]
    diarization = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]
    result = align(transcription, diarization)

    assert result[0].start == 1.5
    assert result[0].end == 3.7


def test_align_empty_diarization():
    transcription = [TranscriptionSegment(start=0.0, end=5.0, text="No diarization")]
    result = align(transcription, [])

    assert result[0].speaker == "UNKNOWN"


def test_align_empty_transcription():
    diarization = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]
    result = align([], diarization)

    assert result == []


def test_align_multiple_speakers():
    transcription = [
        TranscriptionSegment(start=0.0, end=3.0, text="First"),
        TranscriptionSegment(start=3.0, end=6.0, text="Second"),
        TranscriptionSegment(start=6.0, end=9.0, text="Third"),
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=3.5, speaker="SPEAKER_00"),
        DiarizationSegment(start=3.5, end=6.5, speaker="SPEAKER_01"),
        DiarizationSegment(start=6.5, end=10.0, speaker="SPEAKER_02"),
    ]
    result = align(transcription, diarization)

    assert result[0].speaker == "SPEAKER_00"
    assert result[1].speaker == "SPEAKER_01"
    assert result[2].speaker == "SPEAKER_02"
