import pytest

from wisper_transcribe.formatter import to_markdown, update_speaker_names, parse_transcript_blocks, rewrite_transcript_blocks
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


# ---------------------------------------------------------------------------
# parse_transcript_blocks
# ---------------------------------------------------------------------------

_SAMPLE_BODY = """\
# Session 01

**Alice** *(00:00)*: Welcome everyone
**Bob** *(00:12)*: Thanks for having me
**Alice** *(00:18)*: Let's get started

---
*Transcribed by wisper-transcribe v1.0*
"""


def test_parse_blocks_returns_correct_count():
    blocks = parse_transcript_blocks(_SAMPLE_BODY)
    assert len(blocks) == 3


def test_parse_blocks_speaker_and_timestamp():
    blocks = parse_transcript_blocks(_SAMPLE_BODY)
    assert blocks[0]["speaker"] == "Alice"
    assert blocks[0]["timestamp"] == "00:00"
    assert blocks[0]["text"] == "Welcome everyone"
    assert blocks[0]["has_speaker"] is True


def test_parse_blocks_indices_sequential():
    blocks = parse_transcript_blocks(_SAMPLE_BODY)
    assert [b["index"] for b in blocks] == [0, 1, 2]


def test_parse_blocks_no_speaker_lines():
    body = "*(00:05)* Narrator text\n**Alice** *(00:10)*: Hello"
    blocks = parse_transcript_blocks(body)
    assert len(blocks) == 2
    assert blocks[0]["has_speaker"] is False
    assert blocks[0]["speaker"] == ""
    assert blocks[0]["timestamp"] == "00:05"
    assert blocks[1]["has_speaker"] is True


def test_parse_blocks_skips_heading_and_rule():
    body = "# Title\n---\n**Alice** *(00:01)*: Hi"
    blocks = parse_transcript_blocks(body)
    assert len(blocks) == 1
    assert blocks[0]["speaker"] == "Alice"


# ---------------------------------------------------------------------------
# rewrite_transcript_blocks
# ---------------------------------------------------------------------------

_SAMPLE_MD = """\
---
title: Session 01
---

# Session 01

**Alice** *(00:00)*: Welcome everyone
**Bob** *(00:12)*: Thanks for having me
**Alice** *(00:18)*: Let's get started

---
*Transcribed by wisper-transcribe v1.0*
"""


def test_rewrite_blocks_changes_target_speaker():
    result = rewrite_transcript_blocks(_SAMPLE_MD, {1: "Charlie"})
    assert "**Charlie**" in result
    assert "**Bob**" not in result
    # Alice blocks untouched
    assert result.count("**Alice**") == 2


def test_rewrite_blocks_leaves_others_unchanged():
    result = rewrite_transcript_blocks(_SAMPLE_MD, {0: "Diana"})
    assert "**Diana**" in result
    assert "**Bob**" in result
    assert result.count("**Alice**") == 1  # only the second Alice block remains


def test_rewrite_blocks_no_changes_when_empty():
    result = rewrite_transcript_blocks(_SAMPLE_MD, {})
    assert "**Alice**" in result
    assert "**Bob**" in result


def test_rewrite_blocks_strips_newlines_from_speaker():
    result = rewrite_transcript_blocks(_SAMPLE_MD, {0: "Di\nana"})
    assert "**Diana**" in result
    assert "\n" not in result.split("**Diana**")[0].split("\n")[-1]


def test_rewrite_blocks_preserves_frontmatter():
    result = rewrite_transcript_blocks(_SAMPLE_MD, {0: "NewName"})
    assert result.startswith("---")
    assert "title: Session 01" in result
