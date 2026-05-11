"""Tests for web/audio_writer.py — Phase 1 storage layer."""
from __future__ import annotations

import struct

import pytest

from wisper_transcribe.web.audio_writer import RealtimePCMMixer, SegmentedOggWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_opus_packet(size: int = 80) -> bytes:
    """Minimal fake Opus bytes accepted by SegmentedOggWriter (no decoding done)."""
    return b"\x7f\xfe" + b"\x00" * (size - 2)


def _feed_seconds(writer: SegmentedOggWriter, seconds: float, pkt_size: int = 80) -> int:
    """Feed `seconds` worth of 20 ms packets. Returns count of packets written."""
    n = int(seconds / 0.020)
    for _ in range(n):
        writer.write(_fake_opus_packet(pkt_size))
    return n


# ---------------------------------------------------------------------------
# SegmentedOggWriter — rotation
# ---------------------------------------------------------------------------

def test_segmented_ogg_writer_rotates_at_60s(tmp_path):
    stream_dir = tmp_path / "combined"
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)

    _feed_seconds(writer, 61.0)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 2, f"expected 2 segments, got {segments}"
    for seg in segments:
        assert seg.stat().st_size > 0


def test_segmented_ogg_writer_three_segments(tmp_path):
    stream_dir = tmp_path / "combined"
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)

    _feed_seconds(writer, 181.0)   # just over 3 × 60 s
    writer.finalize()

    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 4, f"expected 4 segments (3 complete + 1 tail), got {segments}"


def test_segmented_ogg_writer_single_segment_short_session(tmp_path):
    stream_dir = tmp_path / "per-user" / "U1"
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)

    _feed_seconds(writer, 5.0)
    final_path = writer.finalize()

    assert final_path.exists()
    assert final_path.stat().st_size > 0
    segments = list(stream_dir.glob("*.opus"))
    assert len(segments) == 1


def test_segment_eos_page_written_on_finalize(tmp_path):
    """The last 4 bytes of a valid Ogg EOS page header contain the EOS flag."""
    stream_dir = tmp_path / "stream"
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer, 1.0)
    path = writer.finalize()

    data = path.read_bytes()
    # Find the last OggS page
    last_oggs = data.rfind(b"OggS")
    assert last_oggs != -1, "no OggS capture pattern found"
    # Header type byte is at offset 5 from capture pattern; bit 2 = EOS
    header_type = data[last_oggs + 5]
    assert header_type & 0x04, f"EOS bit not set in header_type={header_type:#04x}"


def test_writer_recovers_from_crash_mid_segment(tmp_path):
    """A new writer opened on the same dir after simulated crash starts a new segment."""
    stream_dir = tmp_path / "stream"
    writer1 = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer1, 5.0)
    # Simulate crash: close file handle without writing EOS
    writer1._fh.close()
    writer1._fh = None

    # New writer picks up where it left off (new segment index)
    writer2 = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer2, 5.0)
    writer2.finalize()

    # Both segments exist; second has valid EOS
    segments = sorted(stream_dir.glob("*.opus"))
    assert len(segments) == 2


def test_writer_write_returns_none_within_segment(tmp_path):
    stream_dir = tmp_path / "s"
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    result = writer.write(_fake_opus_packet())
    assert result is None
    writer.finalize()


def test_writer_write_returns_path_on_rotation(tmp_path):
    stream_dir = tmp_path / "s"
    # segment_duration_s=0.02 → packets_per_seg=1; second packet triggers rotation
    writer = SegmentedOggWriter(stream_dir=stream_dir, segment_duration_s=0.02)
    writer.write(_fake_opus_packet())   # fills segment 0 (1 packet = threshold)
    result = writer.write(_fake_opus_packet())  # triggers rotation, writes to seg 1
    assert result is not None
    assert result.exists()
    writer.finalize()


# ---------------------------------------------------------------------------
# RealtimePCMMixer
# ---------------------------------------------------------------------------

def _stereo_48k_frame(value: int = 1000) -> bytes:
    """960 stereo 16-bit samples (one 20 ms frame at 48 kHz)."""
    sample = struct.pack("<hh", value, value)  # L + R
    return sample * 960


def test_realtime_mixer_single_user(tmp_path):
    mixer = RealtimePCMMixer()
    mixer.add_frame("U1", _stereo_48k_frame(1000))
    out = mixer.mix()
    # 320 samples × 2 bytes = 640 bytes
    assert len(out) == 640
    samples = struct.unpack("<320h", out)
    assert all(s > 0 for s in samples)


def test_realtime_mixer_clears_after_mix(tmp_path):
    mixer = RealtimePCMMixer()
    mixer.add_frame("U1", _stereo_48k_frame(1000))
    mixer.mix()
    out2 = mixer.mix()
    samples = struct.unpack("<320h", out2)
    assert all(s == 0 for s in samples)


def test_realtime_mixer_clips_correctly():
    mixer = RealtimePCMMixer()
    # Two users at max positive value — should clip to 32767, not wrap around
    mixer.add_frame("U1", _stereo_48k_frame(32767))
    mixer.add_frame("U2", _stereo_48k_frame(32767))
    out = mixer.mix()
    samples = struct.unpack("<320h", out)
    assert max(samples) == 32767


def test_realtime_mixer_empty_produces_silence():
    mixer = RealtimePCMMixer()
    out = mixer.mix()
    samples = struct.unpack("<320h", out)
    assert all(s == 0 for s in samples)
