from pathlib import Path

from wisper_transcribe.models import (
    AlignedSegment,
    DiarizationSegment,
    SpeakerProfile,
    TranscriptionSegment,
)


def test_transcription_segment():
    seg = TranscriptionSegment(start=0.0, end=5.0, text="Hello world")
    assert seg.start == 0.0
    assert seg.end == 5.0
    assert seg.text == "Hello world"


def test_diarization_segment():
    seg = DiarizationSegment(start=1.0, end=3.0, speaker="SPEAKER_00")
    assert seg.speaker == "SPEAKER_00"


def test_aligned_segment():
    seg = AlignedSegment(start=0.5, end=2.5, speaker="SPEAKER_01", text="Hi there")
    assert seg.speaker == "SPEAKER_01"
    assert seg.text == "Hi there"


def test_speaker_profile():
    profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=Path("embeddings/alice.npy"),
        enrolled_date="2026-04-05",
        enrollment_source="session01.mp3",
        notes="Game Master",
    )
    assert profile.display_name == "Alice"
    assert profile.notes == "Game Master"


def test_speaker_profile_default_notes():
    profile = SpeakerProfile(
        name="bob",
        display_name="Bob",
        role="Player",
        embedding_path=Path("embeddings/bob.npy"),
        enrolled_date="2026-04-05",
        enrollment_source="session01.mp3",
    )
    assert profile.notes == ""
