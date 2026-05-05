"""Tests for web/audio_writer.py — SegmentedOggWriter and RealtimeMixer.

All tests use FAKE_OPUS packets (raw bytes, no libopus required).
The slow ffprobe test is gated behind WISPER_SLOW_TESTS=1 and @pytest.mark.slow.
"""
from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

from wisper_transcribe.web.audio_writer import (
    RealtimeMixer,
    SegmentedOggWriter,
    _ogg_crc32,
)

# ---------------------------------------------------------------------------
# Fake Opus packet factory
# ---------------------------------------------------------------------------

_FAKE_PACKET_SIZE = 80  # plausible 20ms Opus frame size in bytes


def _fake_opus_packet(size: int = _FAKE_PACKET_SIZE) -> bytes:
    """Return *size* bytes that stand in for a real Opus packet in unit tests.

    The Ogg writer treats these as opaque bytes — it never decodes them,
    so any byte sequence of the right size works.
    """
    return bytes(size)


def _feed_packets(writer: SegmentedOggWriter, n: int) -> None:
    for _ in range(n):
        writer.write(_fake_opus_packet())


# ---------------------------------------------------------------------------
# Ogg CRC helper unit test
# ---------------------------------------------------------------------------


def test_ogg_crc32_known_value() -> None:
    """Verify the Ogg CRC table against a known-good checksum.

    The empty-byte CRC is 0 (identity of the algorithm with initial value 0).
    A single zero byte should produce table[0] = 0 for the Ogg polynomial.
    """
    assert _ogg_crc32(b"") == 0
    # The CRC of b"\x00" with the Ogg polynomial starting from crc=0:
    # idx = ((0 >> 24) ^ 0) & 0xFF = 0; table[0] = 0; result = (0 << 8) ^ 0 = 0
    assert _ogg_crc32(b"\x00") == 0


def test_ogg_crc32_nonzero_for_nonzero_input() -> None:
    """Non-zero input must produce a non-zero CRC (sanity check)."""
    assert _ogg_crc32(b"OggS") != 0


# ---------------------------------------------------------------------------
# SegmentedOggWriter — segment rotation
# ---------------------------------------------------------------------------


def test_segmented_ogg_writer_single_segment(tmp_path: Path) -> None:
    """Writing fewer packets than one segment should produce exactly one file."""
    stream_dir = tmp_path / "combined"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 10)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 1
    assert segments[0].stat().st_size > 0


def test_segmented_ogg_writer_rotates_at_60s(tmp_path: Path) -> None:
    """Writing 3050 packets (61 s at 20 ms each) must produce exactly 2 segments."""
    stream_dir = tmp_path / "combined"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    # 60 s / 0.02 s = 3000 packets per segment; 3050 → segment 0 + segment 1
    _feed_packets(writer, 3050)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 2, f"Expected 2 segments, got {len(segments)}: {segments}"
    for seg in segments:
        assert seg.stat().st_size > 0, f"Segment {seg.name} is empty"


def test_segmented_ogg_writer_segment_names_are_zero_padded(tmp_path: Path) -> None:
    """Segment files must be named 0000.opus, 0001.opus, etc."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    # 3 s segments, 6 s of audio → 2 segments
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=3.0)
    _feed_packets(writer, 300)  # 6 s
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 2
    assert segments[0].name == "0000.opus"
    assert segments[1].name == "0001.opus"


def test_segmented_ogg_writer_three_segments(tmp_path: Path) -> None:
    """3 × 60 s segments for a 3-minute simulated recording."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 9000)  # 3 × 3000 packets = 3 minutes exactly
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 3


# ---------------------------------------------------------------------------
# SegmentedOggWriter — EOS page
# ---------------------------------------------------------------------------


def _find_last_page_header_type(path: Path) -> int:
    """Walk the Ogg pages in *path* and return the header_type of the last page."""
    data = path.read_bytes()
    pos = 0
    last_header_type = -1
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            break
        header_type = data[pos + 5]
        n_segs = data[pos + 26]
        if pos + 27 + n_segs > len(data):
            break
        seg_table = data[pos + 27 : pos + 27 + n_segs]
        body_size = sum(seg_table)
        last_header_type = header_type
        pos += 27 + n_segs + body_size
    return last_header_type


def test_segment_eos_page_written_on_finalize(tmp_path: Path) -> None:
    """finalize() must set the EOS flag (bit 2) on the last Ogg page."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 5)
    writer.finalize()

    seg = sorted(stream_dir.glob("*.opus"))[0]
    last_ht = _find_last_page_header_type(seg)
    assert last_ht & 0x04, (
        f"Last Ogg page header_type {last_ht:#04x} does not have EOS bit set"
    )


def test_segment_eos_on_each_rotated_segment(tmp_path: Path) -> None:
    """Each completed segment (including rotated ones) must end with EOS."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    # 3 s segments, 10 s → 4 segments (3+3+3+1)
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=3.0)
    _feed_packets(writer, 500)  # 10 s
    writer.finalize()

    for seg_path in sorted(stream_dir.glob("*.opus")):
        ht = _find_last_page_header_type(seg_path)
        assert ht & 0x04, f"{seg_path.name}: missing EOS flag (header_type={ht:#04x})"


@pytest.mark.slow
def test_segment_eos_page_written_on_finalize_ffprobe(tmp_path: Path) -> None:
    """ffprobe must recognise the Ogg container structure (slow/CI-optional)."""
    if not os.environ.get("WISPER_SLOW_TESTS"):
        pytest.skip("Set WISPER_SLOW_TESTS=1 to run ffprobe validation")

    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 5)
    writer.finalize()

    seg = sorted(stream_dir.glob("*.opus"))[0]
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_format", str(seg)],
        capture_output=True,
        timeout=10,
    )
    assert seg.stat().st_size > 0
    # ffprobe exit code 0 means it could at least read the format
    assert result.returncode == 0, (
        f"ffprobe failed (rc={result.returncode}): {result.stderr.decode()}"
    )


# ---------------------------------------------------------------------------
# SegmentedOggWriter — crash recovery (resume from next index)
# ---------------------------------------------------------------------------


def test_writer_recovers_from_crash_mid_segment(tmp_path: Path) -> None:
    """A new writer for a dir with existing .opus files starts from the next index."""
    stream_dir = tmp_path / "combined"
    stream_dir.mkdir()

    # Simulate a previous crashed session that left segment 0000.opus on disk
    (stream_dir / "0000.opus").write_bytes(b"partial-ogg-data-no-eos")

    # A new writer should detect the existing file and start at 0001
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 3)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 2
    assert (stream_dir / "0000.opus").read_bytes() == b"partial-ogg-data-no-eos"
    assert (stream_dir / "0001.opus").stat().st_size > 0


def test_writer_empty_directory_starts_at_zero(tmp_path: Path) -> None:
    """Fresh directory with no existing segments → first segment is 0000.opus."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 1)
    writer.finalize()
    assert (stream_dir / "0000.opus").exists()


# ---------------------------------------------------------------------------
# SegmentedOggWriter — finalize without writing any packets
# ---------------------------------------------------------------------------


def test_writer_finalize_with_no_packets(tmp_path: Path) -> None:
    """finalize() on a writer with zero packets must produce a valid (non-empty) file."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 1
    # File must have at least the two Opus header pages
    assert segments[0].stat().st_size > 0


def test_writer_finalize_twice_is_safe(tmp_path: Path) -> None:
    """Calling finalize() twice must not raise or corrupt the file."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 5)
    writer.finalize()
    size_after_first = (stream_dir / "0000.opus").stat().st_size

    writer.finalize()  # should be a no-op

    assert (stream_dir / "0000.opus").stat().st_size == size_after_first


# ---------------------------------------------------------------------------
# SegmentedOggWriter — Ogg page structure validation
# ---------------------------------------------------------------------------


def test_segment_starts_with_ogg_capture_pattern(tmp_path: Path) -> None:
    """Every segment file must start with the Ogg capture pattern 'OggS'."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 3)
    writer.finalize()

    seg = (stream_dir / "0000.opus").read_bytes()
    assert seg[:4] == b"OggS", "Segment does not start with OggS capture pattern"


def test_segment_first_page_has_bos_flag(tmp_path: Path) -> None:
    """The first page of each segment must have the BOS flag (bit 1) set."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 3)
    writer.finalize()

    seg = (stream_dir / "0000.opus").read_bytes()
    first_header_type = seg[5]  # offset 5 = header_type in the first page
    assert first_header_type & 0x02, (
        f"First page header_type {first_header_type:#04x} does not have BOS bit set"
    )


def test_segment_opus_head_magic(tmp_path: Path) -> None:
    """The first page payload must start with 'OpusHead'."""
    stream_dir = tmp_path / "s"
    stream_dir.mkdir()
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_packets(writer, 1)
    writer.finalize()

    data = (stream_dir / "0000.opus").read_bytes()
    # First page: 27 bytes header + n_segs bytes segment table + body
    n_segs = data[26]
    body_start = 27 + n_segs
    assert data[body_start : body_start + 8] == b"OpusHead"


# ---------------------------------------------------------------------------
# RealtimeMixer
# ---------------------------------------------------------------------------


def test_realtime_mixer_empty_streams_returns_empty() -> None:
    mixer = RealtimeMixer()
    assert mixer.mix({}) == b""


def test_realtime_mixer_single_stream_passthrough() -> None:
    """One stream → output equals the input (no mixing needed)."""
    mixer = RealtimeMixer()
    pcm = struct.pack("<4h", 100, 200, -100, -200)
    result = mixer.mix({"user": pcm})
    assert result == pcm


def test_realtime_mixer_two_streams_summed() -> None:
    mixer = RealtimeMixer()
    a = struct.pack("<4h", 100, 0, -100, 0)
    b = struct.pack("<4h", 50, 50, -50, -50)
    result = mixer.mix({"a": a, "b": b})
    samples = struct.unpack("<4h", result)
    assert samples == (150, 50, -150, -50)


def test_realtime_mixer_clips_correctly() -> None:
    """Samples exceeding int16 range must be clamped, not wrapped."""
    mixer = RealtimeMixer()
    max_val = 32767
    # Two streams both at max → sum = 65534, must clip to 32767
    a = struct.pack("<4h", max_val, max_val, max_val, max_val)
    b = struct.pack("<4h", max_val, max_val, max_val, max_val)
    result = mixer.mix({"a": a, "b": b})
    samples = struct.unpack("<4h", result)
    assert all(v == max_val for v in samples), (
        f"Expected all {max_val}, got {samples}"
    )


def test_realtime_mixer_clips_negative_correctly() -> None:
    """Negative overflow must also clamp to -32768."""
    mixer = RealtimeMixer()
    min_val = -32768
    a = struct.pack("<2h", min_val, min_val)
    b = struct.pack("<2h", min_val, min_val)
    result = mixer.mix({"a": a, "b": b})
    samples = struct.unpack("<2h", result)
    assert all(v == min_val for v in samples)


def test_realtime_mixer_truncates_to_shortest_stream() -> None:
    """Mixer must handle streams of differing lengths by truncating to shortest."""
    mixer = RealtimeMixer()
    long_stream = struct.pack("<4h", 10, 20, 30, 40)
    short_stream = struct.pack("<2h", 1, 2)
    result = mixer.mix({"long": long_stream, "short": short_stream})
    # Result length should match shortest stream
    assert len(result) == len(short_stream)
    samples = struct.unpack("<2h", result)
    assert samples == (11, 22)


def test_realtime_mixer_zero_length_streams_returns_empty() -> None:
    mixer = RealtimeMixer()
    result = mixer.mix({"a": b"", "b": b""})
    assert result == b""


def test_realtime_mixer_multiple_users() -> None:
    """Six simultaneous users all at moderate levels should not clip."""
    mixer = RealtimeMixer()
    # 6 users at 5000 each → 30000, within int16 range
    streams = {
        f"user_{i}": struct.pack("<2h", 5000, -5000)
        for i in range(6)
    }
    result = mixer.mix(streams)
    samples = struct.unpack("<2h", result)
    assert samples == (30000, -30000)
