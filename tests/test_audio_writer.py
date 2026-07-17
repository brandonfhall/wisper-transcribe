"""Tests for web/audio_writer.py — R12 remediation: WAV segments + downsampling.

Replaces the old SegmentedOggWriter/RealtimePCMMixer tests (Ogg/Opus muxer
deleted; see senior-review finding R12 — the old writer produced undecodable
Ogg files and the mixer advanced the combined track N x 20ms per real 20ms
with N concurrent speakers).
"""
from __future__ import annotations

import struct
import wave

import numpy as np
import pytest

from wisper_transcribe.web.audio_writer import (
    SegmentedWavWriter,
    concat_wav_segments,
    downsample_48k_stereo_to_16k_mono,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stereo_48k_frame(value: int = 1000, n_samples: int = 960) -> bytes:
    """One 20 ms 48 kHz stereo 16-bit PCM frame (960 samples/channel)."""
    sample = struct.pack("<hh", value, value)  # L + R
    return sample * n_samples


def _mono_16k_frame(n_samples: int = 320) -> bytes:
    """320 samples of 16 kHz mono 16-bit PCM (one 20 ms chunk)."""
    return struct.pack(f"<{n_samples}h", *([1000] * n_samples))


def _feed_seconds(writer: SegmentedWavWriter, seconds: float) -> int:
    """Feed `seconds` worth of 20 ms mono-16k chunks. Returns count fed."""
    n = int(seconds / 0.020)
    for _ in range(n):
        writer.write(_mono_16k_frame())
    return n


def _read_wav(path) -> tuple[int, int, int, int, bytes]:
    with wave.open(str(path), "rb") as wf:
        return (
            wf.getframerate(),
            wf.getnchannels(),
            wf.getsampwidth(),
            wf.getnframes(),
            wf.readframes(wf.getnframes()),
        )


# ---------------------------------------------------------------------------
# SegmentedWavWriter — rotation
# ---------------------------------------------------------------------------

def test_segmented_wav_writer_rotates_at_60s(tmp_path):
    stream_dir = tmp_path / "combined"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)

    _feed_seconds(writer, 61.0)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.wav"))
    assert len(segments) == 2, f"expected 2 segments, got {segments}"
    for seg in segments:
        assert seg.stat().st_size > 0


def test_segmented_wav_writer_three_segments(tmp_path):
    stream_dir = tmp_path / "combined"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)

    _feed_seconds(writer, 181.0)   # just over 3 x 60 s
    writer.finalize()

    segments = sorted(stream_dir.glob("*.wav"))
    assert len(segments) == 4, f"expected 4 segments (3 complete + 1 tail), got {segments}"


def test_segmented_wav_writer_single_segment_short_session(tmp_path):
    stream_dir = tmp_path / "per-user" / "U1"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)

    _feed_seconds(writer, 5.0)
    final_path = writer.finalize()

    assert final_path.exists()
    assert final_path.stat().st_size > 0
    segments = list(stream_dir.glob("*.wav"))
    assert len(segments) == 1


def test_segment_is_valid_wav_with_correct_params(tmp_path):
    stream_dir = tmp_path / "stream"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer, 1.0)
    path = writer.finalize()

    rate, channels, sampwidth, nframes, _ = _read_wav(path)
    assert rate == 16000
    assert channels == 1
    assert sampwidth == 2
    assert nframes == 320 * 50  # 1.0s / 0.02s = 50 chunks x 320 samples


def test_writer_recovers_from_crash_mid_segment(tmp_path):
    """A new writer opened on the same dir after simulated crash starts a new segment."""
    stream_dir = tmp_path / "stream"
    writer1 = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer1, 5.0)
    # Simulate crash: drop references without calling finalize()/close().
    # The header was already patched on every write() (see module docstring),
    # so the segment on disk is still a valid, readable WAV.
    writer1._fh = None
    writer1._wf = None

    # New writer picks up where it left off (new segment index)
    writer2 = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer2, 5.0)
    writer2.finalize()

    segments = sorted(stream_dir.glob("*.wav"))
    assert len(segments) == 2
    # The "crashed" first segment is still readable and has the frames
    # that were written before the simulated crash.
    rate, channels, sampwidth, nframes, _ = _read_wav(segments[0])
    assert rate == 16000 and channels == 1 and sampwidth == 2
    assert nframes == 320 * 250  # 5.0s / 0.02s = 250 chunks x 320 samples


def test_writer_write_returns_none_within_segment(tmp_path):
    stream_dir = tmp_path / "s"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    result = writer.write(_mono_16k_frame())
    assert result is None
    writer.finalize()


def test_writer_write_returns_path_on_rotation(tmp_path):
    stream_dir = tmp_path / "s"
    # segment_duration_s=0.02 -> samples_per_seg=320; second chunk triggers rotation
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=0.02)
    writer.write(_mono_16k_frame())   # fills segment 0 (320 samples = threshold)
    result = writer.write(_mono_16k_frame())  # triggers rotation, writes to seg 1
    assert result is not None
    assert result.exists()
    writer.finalize()


def test_writer_ignores_empty_write(tmp_path):
    stream_dir = tmp_path / "s"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    assert writer.write(b"") is None
    path = writer.finalize()
    # An empty segment still produces a (near-)empty but valid file handle close.
    assert path.exists()


# ---------------------------------------------------------------------------
# downsample_48k_stereo_to_16k_mono
# ---------------------------------------------------------------------------

def test_downsample_produces_correct_sample_count():
    """A full 20 ms frame (960 stereo samples @ 48k) -> 320 mono samples @ 16k."""
    frame = _stereo_48k_frame(1000)
    out = downsample_48k_stereo_to_16k_mono(frame)
    assert len(out) == 320 * 2  # 320 samples x 2 bytes


def test_downsample_empty_input_returns_empty():
    assert downsample_48k_stereo_to_16k_mono(b"") == b""


def test_downsample_silence_stays_silent():
    frame = _stereo_48k_frame(0)
    out = downsample_48k_stereo_to_16k_mono(frame)
    samples = struct.unpack(f"<{len(out)//2}h", out)
    assert all(s == 0 for s in samples)


def test_downsample_averages_left_right():
    """Distinct L/R values average to mono rather than picking one channel."""
    sample = struct.pack("<hh", 1000, -1000)  # L=1000, R=-1000 -> mono ~0
    frame = sample * 960
    out = downsample_48k_stereo_to_16k_mono(frame)
    samples = struct.unpack(f"<{len(out)//2}h", out)
    assert all(abs(s) < 5 for s in samples)


def test_downsample_ramp_stays_monotonic_and_nonsilent():
    """A monotonically increasing stereo ramp survives low-pass + decimation
    as a (non-decreasing, non-silent) mono ramp — content is not destroyed."""
    n = 960
    values = np.linspace(-30000, 30000, n).astype("<i2")
    stereo = np.empty(n * 2, dtype="<i2")
    stereo[0::2] = values
    stereo[1::2] = values
    out = downsample_48k_stereo_to_16k_mono(stereo.tobytes())
    samples = np.frombuffer(out, dtype="<i2")

    assert len(samples) == 320
    assert not np.all(samples == 0)
    # Non-decreasing overall (allow tiny float/round jitter of 1 LSB). The
    # very first sample is affected by the convolution's zero-padded left
    # edge, so it is excluded from the step-to-step monotonicity check.
    diffs = np.diff(samples[1:].astype(np.int32))
    assert (diffs >= -1).all(), f"ramp should stay ~monotonic, got diffs min={diffs.min()}"
    assert samples[-1] > samples[0]


def test_downsample_clips_to_int16_range():
    frame = _stereo_48k_frame(32767)
    out = downsample_48k_stereo_to_16k_mono(frame)
    samples = struct.unpack(f"<{len(out)//2}h", out)
    assert max(samples) <= 32767
    assert min(samples) >= -32768


# ---------------------------------------------------------------------------
# concat_wav_segments (R2)
# ---------------------------------------------------------------------------

def test_concat_no_segments_returns_none(tmp_path):
    segments_dir = tmp_path / "empty"
    segments_dir.mkdir()
    out = concat_wav_segments(segments_dir, tmp_path / "combined.wav")
    assert out is None
    assert not (tmp_path / "combined.wav").exists()


def test_concat_merges_segments_duration_matches_sum(tmp_path):
    stream_dir = tmp_path / "combined"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=1.0)
    _feed_seconds(writer, 2.5)  # rotates at 1.0s -> 3 segments (1.0, 1.0, 0.5)
    writer.finalize()

    segments = sorted(stream_dir.glob("*.wav"))
    assert len(segments) == 3
    total_frames_in = sum(_read_wav(s)[3] for s in segments)

    out_path = concat_wav_segments(stream_dir, tmp_path / "combined.wav")
    assert out_path is not None
    rate, channels, sampwidth, nframes, _ = _read_wav(out_path)
    assert rate == 16000 and channels == 1 and sampwidth == 2
    assert nframes == total_frames_in
    assert nframes == 320 * 125  # 2.5s / 0.02s = 125 chunks x 320 samples


def test_concat_skips_unreadable_segment(tmp_path):
    stream_dir = tmp_path / "combined"
    writer = SegmentedWavWriter(stream_dir=stream_dir, segment_duration_s=60.0)
    _feed_seconds(writer, 1.0)
    writer.finalize()

    # Corrupt/zero-byte segment that sorts after the valid one.
    (stream_dir / "0001.wav").write_bytes(b"")

    out_path = concat_wav_segments(stream_dir, tmp_path / "combined.wav")
    assert out_path is not None
    _, _, _, nframes, _ = _read_wav(out_path)
    assert nframes == 320 * 50  # only the valid segment's frames


def test_concat_all_segments_unreadable_returns_none(tmp_path):
    segments_dir = tmp_path / "combined"
    segments_dir.mkdir()
    (segments_dir / "0000.wav").write_bytes(b"")
    (segments_dir / "0001.wav").write_bytes(b"not a wav file")

    out = concat_wav_segments(segments_dir, tmp_path / "combined.wav")
    assert out is None
