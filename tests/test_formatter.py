import pytest

from wisper_transcribe.formatter import to_markdown, update_speaker_names
from wisper_transcribe.time_utils import format_timestamp as _format_timestamp
from wisper_transcribe.models import AlignedSegment, TranscriptionSegment


METADATA = {
    "title": "Session 01",
    "source_file": "session01.mp3",
    "date_processed": "2026-04-05",
    "duration": "1:23:45",
    "speakers": [{"name": "Alice", "role": "DM"}, {"name": "Bob", "role": "Player"}],
}


def test_format_timestamp_seconds_only():
    assert _format_timestamp(45.0) == "00:45"


def test_format_timestamp_minutes():
    assert _format_timestamp(90.0) == "01:30"


def test_format_timestamp_hours():
    assert _format_timestamp(3661.0) == "01:01:01"


def test_to_markdown_no_speakers():
    segments = [
        TranscriptionSegment(start=0.0, end=5.0, text="Hello world"),
        TranscriptionSegment(start=5.0, end=10.0, text="This is a test"),
    ]
    result = to_markdown(segments, speaker_map=None, metadata=METADATA)
    assert "# Session 01" in result
    assert "Hello world" in result
    assert "This is a test" in result
    assert "**" not in result  # no speaker names


def test_to_markdown_with_speakers():
    segments = [
        AlignedSegment(start=0.0, end=5.0, speaker="SPEAKER_00", text="Welcome everyone"),
        AlignedSegment(start=5.0, end=10.0, speaker="SPEAKER_01", text="Thanks for having me"),
    ]
    speaker_map = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}
    result = to_markdown(segments, speaker_map=speaker_map, metadata=METADATA)
    assert "**Alice**" in result
    assert "**Bob**" in result
    assert "Welcome everyone" in result
    assert "Thanks for having me" in result


def test_to_markdown_merges_consecutive_same_speaker():
    segments = [
        AlignedSegment(start=0.0, end=3.0, speaker="SPEAKER_00", text="First line"),
        AlignedSegment(start=3.0, end=6.0, speaker="SPEAKER_00", text="Second line"),
        AlignedSegment(start=6.0, end=9.0, speaker="SPEAKER_01", text="Different speaker"),
    ]
    speaker_map = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}
    result = to_markdown(segments, speaker_map=speaker_map, metadata=METADATA)
    # Alice should appear only once (merged)
    assert result.count("**Alice**") == 1
    assert "First line Second line" in result
    assert "**Bob**" in result


def test_to_markdown_timestamps_included():
    segments = [TranscriptionSegment(start=12.0, end=17.0, text="Hello")]
    result = to_markdown(segments, speaker_map=None, metadata=METADATA, include_timestamps=True)
    assert "(00:12)" in result


def test_to_markdown_timestamps_excluded():
    segments = [TranscriptionSegment(start=12.0, end=17.0, text="Hello")]
    result = to_markdown(segments, speaker_map=None, metadata=METADATA, include_timestamps=False)
    assert "00:12" not in result


def test_to_markdown_frontmatter():
    segments = [TranscriptionSegment(start=0.0, end=1.0, text="Hi")]
    result = to_markdown(segments, speaker_map=None, metadata=METADATA)
    assert result.startswith("---")
    assert "source_file: session01.mp3" in result
    assert "date_processed: '2026-04-05'" in result


def test_to_markdown_skips_empty_segments():
    segments = [
        TranscriptionSegment(start=0.0, end=1.0, text="  "),
        TranscriptionSegment(start=1.0, end=2.0, text="Real text"),
    ]
    result = to_markdown(segments, speaker_map=None, metadata=METADATA)
    assert "Real text" in result
    # Only one content block (the empty one is skipped)
    assert result.count("Real text") == 1


def test_update_speaker_names_in_body():
    content = "**Alice** *(00:12)*: Hello everyone\n**Bob** *(00:18)*: Hi Alice"
    result = update_speaker_names(content, "Alice", "Diana")
    assert "**Diana**" in result
    assert "**Alice**" not in result
    assert "**Bob**" in result  # unchanged


def test_update_speaker_names_in_frontmatter():
    content = "---\nspeakers:\n- name: Alice\n  role: DM\n---\n**Alice** *(00:12)*: Hi"
    result = update_speaker_names(content, "Alice", "Diana")
    assert "- name: Diana" in result
    assert "- name: Alice" not in result
