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
    """A B A within one whisper segment yields three AlignedSegments when B
    is a genuine interjection (F13: 3+ words AND >= 1.0s survives the
    micro-run smoothing pass -- only consecutive same-speaker words are
    grouped, but a real interjection is not absorbed into its neighbors)."""
    words = [
        Word(start=0.0, end=1.0, text="one"),
        Word(start=1.0, end=2.0, text="two"),
        Word(start=2.0, end=3.0, text="three"),
        Word(start=3.0, end=4.0, text="four"),
        Word(start=4.0, end=5.0, text="five"),
        Word(start=5.0, end=6.0, text="six"),
        Word(start=6.0, end=7.0, text="seven"),
    ]
    transcription = [
        TranscriptionSegment(
            start=0.0, end=7.0, text="one two three four five six seven", words=words
        )
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="A"),
        DiarizationSegment(start=2.0, end=5.0, speaker="B"),
        DiarizationSegment(start=5.0, end=7.0, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 3
    assert result[0].speaker == "A"
    assert result[0].text == "one two"
    assert result[1].speaker == "B"
    assert result[1].text == "three four five"
    assert result[2].speaker == "A"
    assert result[2].text == "six seven"


# ---------------------------------------------------------------------------
# Micro-run smoothing (F13)
# ---------------------------------------------------------------------------


def test_align_smooths_one_word_sandwich_into_single_segment():
    """A B A where B is a single word is absorbed into A -- a single segment
    with the full sentence text reassembled, not a three-way split."""
    words = [
        Word(start=0.0, end=1.0, text="The"),
        Word(start=1.0, end=2.0, text="quick"),
        Word(start=2.0, end=3.0, text="fox"),
        Word(start=3.0, end=4.0, text="brown"),
        Word(start=4.0, end=5.0, text="dog"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=5.0, text="The quick fox brown dog", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="A"),
        DiarizationSegment(start=2.0, end=3.0, speaker="B"),
        DiarizationSegment(start=3.0, end=5.0, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "A"
    assert result[0].text == "The quick fox brown dog"


def test_align_two_word_run_over_one_second_absorbed_by_word_count_rule():
    """A B A where B is exactly 2 words but spans >= 1.0s is still absorbed --
    the word-count branch of the OR is sufficient on its own."""
    words = [
        Word(start=0.0, end=1.0, text="hello"),
        Word(start=1.0, end=2.0, text="there"),
        Word(start=2.0, end=3.0, text="yes"),
        Word(start=3.0, end=4.0, text="indeed"),
        Word(start=4.0, end=5.0, text="okay"),
        Word(start=5.0, end=6.0, text="then"),
    ]
    transcription = [
        TranscriptionSegment(
            start=0.0, end=6.0, text="hello there yes indeed okay then", words=words
        )
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="A"),
        DiarizationSegment(start=2.0, end=4.0, speaker="B"),
        DiarizationSegment(start=4.0, end=6.0, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "A"
    assert result[0].text == "hello there yes indeed okay then"


def test_align_three_word_run_under_one_second_absorbed_by_duration_rule():
    """A B A where B has 3 words (over the word-count cap) but spans < 1.0s
    is still absorbed -- the duration branch of the OR is sufficient on its
    own even when the word-count branch alone would not trigger."""
    words = [
        Word(start=0.0, end=0.5, text="hi"),
        Word(start=0.5, end=0.6, text="um"),
        Word(start=0.6, end=0.7, text="er"),
        Word(start=0.7, end=0.8, text="uh"),
        Word(start=0.8, end=1.3, text="there"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=1.3, text="hi um er uh there", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=0.5, speaker="A"),
        DiarizationSegment(start=0.5, end=0.8, speaker="B"),
        DiarizationSegment(start=0.8, end=1.3, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "A"
    assert result[0].text == "hi um er uh there"


def test_align_different_speaker_sandwich_not_absorbed():
    """A B C -- a short middle run flanked by two DIFFERENT speakers is a
    genuine interjection and survives, regardless of its length."""
    words = [
        Word(start=0.0, end=1.0, text="one"),
        Word(start=1.0, end=2.0, text="two"),
        Word(start=2.0, end=3.0, text="three"),
        Word(start=3.0, end=4.0, text="four"),
        Word(start=4.0, end=5.0, text="five"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=5.0, text="one two three four five", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=2.0, speaker="A"),
        DiarizationSegment(start=2.0, end=3.0, speaker="B"),
        DiarizationSegment(start=3.0, end=5.0, speaker="C"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 3
    assert [s.speaker for s in result] == ["A", "B", "C"]
    assert result[1].text == "three"


def test_align_micro_run_at_start_of_segment_not_absorbed():
    """A one-word run with no left neighbor (segment start) is never absorbed
    -- there is no sandwich to speak of."""
    words = [
        Word(start=0.0, end=1.0, text="hm"),
        Word(start=1.0, end=2.0, text="okay"),
        Word(start=2.0, end=3.0, text="sure"),
        Word(start=3.0, end=4.0, text="thing"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=4.0, text="hm okay sure thing", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=1.0, speaker="B"),
        DiarizationSegment(start=1.0, end=4.0, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 2
    assert result[0].speaker == "B"
    assert result[0].text == "hm"
    assert result[1].speaker == "A"
    assert result[1].text == "okay sure thing"


def test_align_micro_run_at_end_of_segment_not_absorbed():
    """A one-word run with no right neighbor (segment end) is never absorbed."""
    words = [
        Word(start=0.0, end=1.0, text="sure"),
        Word(start=1.0, end=2.0, text="thing"),
        Word(start=2.0, end=3.0, text="okay"),
        Word(start=3.0, end=4.0, text="bye"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=4.0, text="sure thing okay bye", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=3.0, speaker="A"),
        DiarizationSegment(start=3.0, end=4.0, speaker="B"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 2
    assert result[0].speaker == "A"
    assert result[0].text == "sure thing okay"
    assert result[1].speaker == "B"
    assert result[1].text == "bye"


def test_align_cascading_absorption_collapses_to_single_run():
    """A B A B A with tiny (1-word) B runs collapses to a single A run --
    absorbing the first B run makes its neighboring A runs adjacent to the
    next B run, exposing it for absorption too (fixpoint loop)."""
    words = [
        Word(start=0.0, end=1.0, text="one"),
        Word(start=1.0, end=2.0, text="two"),
        Word(start=2.0, end=3.0, text="three"),
        Word(start=3.0, end=4.0, text="four"),
        Word(start=4.0, end=5.0, text="five"),
    ]
    transcription = [
        TranscriptionSegment(start=0.0, end=5.0, text="one two three four five", words=words)
    ]
    diarization = [
        DiarizationSegment(start=0.0, end=1.0, speaker="A"),
        DiarizationSegment(start=1.0, end=2.0, speaker="B"),
        DiarizationSegment(start=2.0, end=3.0, speaker="A"),
        DiarizationSegment(start=3.0, end=4.0, speaker="B"),
        DiarizationSegment(start=4.0, end=5.0, speaker="A"),
    ]

    result = align(transcription, diarization)

    assert len(result) == 1
    assert result[0].speaker == "A"
    assert result[0].text == "one two three four five"
