import pytest

from wisper_transcribe.aligner import align
from wisper_transcribe.models import AlignedSegment, DiarizationSegment, TranscriptionSegment, Word


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


# ---------------------------------------------------------------------------
# Word-level alignment (F8)
# ---------------------------------------------------------------------------


def test_align_word_level_splits_segment_at_turn_boundary():
    """A whisper segment spanning two diarization turns splits into two
    AlignedSegments at the word boundary, each with the correct text."""
    words = [
        Word(start=0.0, end=1.0, text="Hello"),
        Word(start=1.0, end=2.0, text="there"),
        Word(start=2.0, end=3.0, text="friend"),
        Word(start=3.0, end=4.0, text="now"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=4.0, text="Hello there friend now", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=2.0, end=4.0, speaker="SPEAKER_01"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 2
    assert result[0].speaker == "SPEAKER_00"
    assert result[0].text == "Hello there"
    assert result[0].start == 0.0
    assert result[0].end == 2.0
    assert result[1].speaker == "SPEAKER_01"
    assert result[1].text == "friend now"
    assert result[1].start == 2.0
    assert result[1].end == 4.0


def test_align_word_level_single_turn_yields_one_segment():
    """A segment fully inside one diarization turn yields exactly one
    AlignedSegment with words joined into the original text."""
    words = [
        Word(start=1.0, end=1.5, text="Hi"),
        Word(start=1.5, end=2.0, text="there"),
    ]
    transcription = [TranscriptionSegment(start=1.0, end=2.0, text="Hi there", words=words)]
    diarization = [DiarizationSegment(start=0.0, end=10.0, speaker="SPEAKER_00")]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "SPEAKER_00"
    assert result[0].text == "Hi there"
    assert result[0].start == 1.0
    assert result[0].end == 2.0


def test_align_word_no_overlap_inherits_nearest_turn():
    """A word overlapping no diarization turn inherits the nearest turn's
    speaker by word-midpoint distance."""
    words = [Word(start=2.2, end=2.8, text="gap")]
    transcription = [TranscriptionSegment(start=2.2, end=2.8, text="gap", words=words)]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=5.0, end=7.0, speaker="SPEAKER_01"),
    ]

    result = align(transcription, diarization)

    # midpoint 2.5: distance to SPEAKER_00's turn (ends at 2.0) is 0.5;
    # distance to SPEAKER_01's turn (starts at 5.0) is 2.5 — nearer turn wins.
    assert len(result) == 1
    assert result[0].speaker == "SPEAKER_00"


def test_align_word_level_no_diarization_is_unknown():
    """No diarization at all: every word inherits the previous word's
    speaker, starting from UNKNOWN — the whole segment stays UNKNOWN."""
    words = [Word(start=0.0, end=1.0, text="Hi"), Word(start=1.0, end=2.0, text="there")]
    transcription = [TranscriptionSegment(start=0.0, end=2.0, text="Hi there", words=words)]

    result = align(transcription, [])

    assert len(result) == 1
    assert result[0].speaker == "UNKNOWN"
    assert result[0].text == "Hi there"


def test_align_fallback_no_diarization_is_unknown():
    """Fallback (no-words) path with no diarization at all also yields UNKNOWN."""
    transcription = [TranscriptionSegment(start=0.0, end=2.0, text="No diarization")]

    result = align(transcription, [])

    assert result[0].speaker == "UNKNOWN"


def test_align_words_none_uses_fallback():
    """words=None takes the whole-segment fallback path (no splitting)."""
    transcription = [
        TranscriptionSegment(
            start=0.0, end=4.0, text="Hello there friend now", words=None
        )
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=2.0, end=4.0, speaker="SPEAKER_01"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "SPEAKER_00"  # first max-overlap turn wins ties
    assert result[0].text == "Hello there friend now"


def test_align_words_empty_list_uses_fallback():
    """words=[] (empty list) also takes the whole-segment fallback path."""
    transcription = [
        TranscriptionSegment(
            start=0.0, end=4.0, text="Hello there friend now", words=[]
        )
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=2.0, end=4.0, speaker="SPEAKER_01"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "SPEAKER_00"


def test_align_word_level_three_turn_sandwich():
    """A B A within one whisper segment yields three AlignedSegments,
    since only consecutive same-speaker words are grouped."""
    words = [
        Word(start=0.0, end=1.0, text="one"),
        Word(start=1.0, end=2.0, text="two"),
        Word(start=2.0, end=3.0, text="three"),
        Word(start=3.0, end=4.0, text="four"),
        Word(start=4.0, end=5.0, text="five"),
        Word(start=5.0, end=6.0, text="six"),
    ]
    transcription = [
        TranscriptionSegment(
            start=0.0, end=6.0, text="one two three four five six", words=words
        )
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="A"),
        DiarizationSegment(start=2.0, end=4.0, speaker="B"),
        DiarizationSegment(start=4.0, end=6.0, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 3
    assert result[0].speaker == "A"
    assert result[0].text == "one two"
    assert result[1].speaker == "B"
    assert result[1].text == "three four"
    assert result[2].speaker == "A"
    assert result[2].text == "five six"
