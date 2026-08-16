"""Tests for web/local_capture.py — Phase 1 (capture layer, no live transcription).

Mirrors tests/test_discord_bot.py's structure but for LocalCaptureManager:
a scripted capture_factory + instant ticker take the place of asyncio fake
audio sources, since LocalCaptureManager is thread-based rather than
asyncio-based (soundcard recorders are blocking pulls). No real soundcard
package, audio devices, or Win32 calls anywhere in this file.
"""
from __future__ import annotations

import sys
import types
import wave
from unittest.mock import MagicMock

import numpy as np
import pytest

from wisper_transcribe.web.audio_writer import SegmentedWavWriter
from wisper_transcribe.web.local_capture import (
    FIFO_CAP_BYTES,
    LocalCaptureManager,
    _ByteFifo,
    enumerate_devices,
    resolve_device_name,
)


# ---------------------------------------------------------------------------
# Fakes: scripted capture_factory + instant ticker
# ---------------------------------------------------------------------------

def _block(n_frames: int = 960, channels: int = 2, value: float = 0.5, sr: int = 48000):
    """One 20 ms block at 48 kHz, matching the shape soundcard would hand back."""
    return np.full((n_frames, channels), value, dtype=np.float32), sr


def scripted_capture_factory(blocks_by_device: dict):
    """capture_factory that yields pre-scripted blocks keyed by device_id.

    A device_id with no entry (or an empty list) yields nothing at all --
    that track's FIFO is simply never populated, exercising the
    starved-track silence-substitution path.
    """
    def _factory(device_id, samplerate):
        for block, sr in blocks_by_device.get(device_id, []):
            yield block, sr
    return _factory


def instant_ticker(n: int):
    """ticker() factory yielding exactly n ticks with no real-time delay."""
    def _ticker():
        for _ in range(n):
            yield
    return _ticker


def _read_wav(path) -> tuple[int, int, int, int, bytes]:
    with wave.open(str(path), "rb") as wf:
        return (
            wf.getframerate(),
            wf.getnchannels(),
            wf.getsampwidth(),
            wf.getnframes(),
            wf.readframes(wf.getnframes()),
        )


def _run_session_to_completion(mgr: LocalCaptureManager) -> None:
    """Wait for the (finite, scripted) capture + tick threads to run to
    natural completion before calling stop_session().

    A finite test ticker exhausts and returns on its own; `stop_session()`
    additionally sets `stop_event`, which the tick loop also checks on
    every iteration. Calling `stop_session()` immediately after
    `start_session()` races that check against the tick thread still
    working through its scripted ticks and can truncate the session early
    -- exactly the interruption `stop_event` is *supposed* to cause for the
    real, infinite production ticker. Joining first (safe here because the
    ticker is finite and always terminates) removes that race for
    duration-sensitive assertions; tests that only check existence/status
    don't need it.
    """
    for t in mgr._capture_threads:
        t.join(timeout=5.0)
    if mgr._tick_thread is not None:
        mgr._tick_thread.join(timeout=5.0)
    mgr.stop_session()


# ---------------------------------------------------------------------------
# start_session / stop_session lifecycle
# ---------------------------------------------------------------------------

def test_start_session_creates_local_recording(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory({}),
        ticker=instant_ticker(0),
    )
    rec = mgr.start_session("campaign-1", "mic-dev", "sys-dev", mic_name="Mic", system_name="Speakers")
    assert rec.source == "local"
    assert rec.voice_channel_id == ""
    assert rec.guild_id == ""
    assert rec.devices == {"mic": "Mic", "system": "Speakers"}
    assert rec.status == "recording"
    mgr.stop_session()


def test_start_session_raises_if_already_active(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory({}),
        ticker=instant_ticker(0),
    )
    mgr.start_session(None, "mic-dev", "sys-dev")
    with pytest.raises(RuntimeError):
        mgr.start_session(None, "mic-dev", "sys-dev")
    mgr.stop_session()


def test_device_names_fall_back_to_id_when_not_given(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory({}),
        ticker=instant_ticker(0),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    assert rec.devices == {"mic": "mic-dev", "system": "sys-dev"}
    mgr.stop_session()


# ---------------------------------------------------------------------------
# Per-track WAV output + duration
# ---------------------------------------------------------------------------

def test_per_track_wavs_written_with_correct_params_and_duration(tmp_path):
    n_ticks = 10
    blocks = {
        "mic-dev": [_block() for _ in range(n_ticks)],
        "sys-dev": [_block() for _ in range(n_ticks)],
    }
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    _run_session_to_completion(mgr)

    for track in ("mic", "system"):
        seg_dir = tmp_path / "recordings" / rec.id / "per-user" / track
        segments = sorted(seg_dir.glob("*.wav"))
        assert segments, f"no segments written for {track}"
        rate, channels, sampwidth, _, _ = _read_wav(segments[-1])
        assert rate == 16000 and channels == 1 and sampwidth == 2
        total_frames = sum(_read_wav(s)[3] for s in segments)
        assert total_frames == n_ticks * 320


def test_combined_duration_equals_tick_count_times_20ms(tmp_path):
    n_ticks = 25
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    _run_session_to_completion(mgr)

    assert rec.combined_path is not None and rec.combined_path.exists()
    rate, channels, sampwidth, nframes, _ = _read_wav(rec.combined_path)
    assert rate == 16000 and channels == 1 and sampwidth == 2
    assert nframes == n_ticks * 320


def test_silence_substitution_for_starved_track(tmp_path):
    """system never provides frames -- its track must be entirely silent,
    but still wall-clock continuous (same duration as mic)."""
    n_ticks = 15
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}  # system: no entry at all
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    _run_session_to_completion(mgr)

    seg_dir = tmp_path / "recordings" / rec.id / "per-user" / "system"
    segments = sorted(seg_dir.glob("*.wav"))
    assert segments
    _, _, _, nframes, frames = _read_wav(segments[-1])
    assert nframes == n_ticks * 320
    samples = np.frombuffer(frames, dtype="<i2")
    assert np.all(samples == 0)


def test_stop_session_sets_completed_and_combined_path(tmp_path):
    n_ticks = 5
    blocks = {
        "mic-dev": [_block() for _ in range(n_ticks)],
        "sys-dev": [_block() for _ in range(n_ticks)],
    }
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    _run_session_to_completion(mgr)

    assert rec.status == "completed"
    assert rec.ended_at is not None
    assert rec.combined_path is not None
    assert rec.combined_path.exists()


def test_no_frames_session_leaves_combined_path_none(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory({}),
        ticker=instant_ticker(0),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    mgr.stop_session()

    assert rec.status == "completed"
    assert rec.combined_path is None


def test_stop_session_persists_recording_to_disk(tmp_path):
    from wisper_transcribe.recording_manager import load_recordings

    n_ticks = 3
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    mgr.stop_session()

    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].status == "completed"
    assert loaded[rec.id].source == "local"


def test_stop_session_noop_when_no_active_session(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory({}),
        ticker=instant_ticker(0),
    )
    mgr.stop_session()  # must not raise
    assert mgr.active_recording is None


def test_stop_lifecycle_method_stops_active_session(tmp_path):
    n_ticks = 4
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    mgr.start()
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    mgr.stop()
    assert rec.status == "completed"


def test_stop_lifecycle_method_noop_when_nothing_active(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory({}),
        ticker=instant_ticker(0),
    )
    mgr.start()
    mgr.stop()  # must not raise


# ---------------------------------------------------------------------------
# _do_tick() direct unit tests -- deterministic, no threads
# ---------------------------------------------------------------------------

def _wire_manual_writers(mgr: LocalCaptureManager, tmp_path):
    mgr._fifos = {"mic": _ByteFifo(), "system": _ByteFifo()}
    mgr._writers = {
        "mic": SegmentedWavWriter(stream_dir=tmp_path / "mic"),
        "system": SegmentedWavWriter(stream_dir=tmp_path / "system"),
    }
    mgr._combined_writer = SegmentedWavWriter(stream_dir=tmp_path / "combined")


def _finalize_manual_writers(mgr: LocalCaptureManager):
    mgr._writers["mic"].finalize()
    mgr._writers["system"].finalize()
    mgr._combined_writer.finalize()


def test_do_tick_mixes_two_tracks_into_combined(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)

    mgr._fifos["mic"].push(np.full(320, 1000, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 2000, dtype="<i2").tobytes())

    mgr._do_tick()
    _finalize_manual_writers(mgr)

    _, _, _, _, combined_frames = _read_wav(sorted((tmp_path / "combined").glob("*.wav"))[0])
    assert np.all(np.frombuffer(combined_frames, dtype="<i2") == 3000)


def test_do_tick_pads_starved_track_with_silence(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)

    mgr._fifos["mic"].push(np.full(320, 5000, dtype="<i2").tobytes())
    # system FIFO stays empty -- starved

    mgr._do_tick()
    _finalize_manual_writers(mgr)

    _, _, _, sys_nframes, sys_frames = _read_wav(sorted((tmp_path / "system").glob("*.wav"))[0])
    assert sys_nframes == 320
    assert np.all(np.frombuffer(sys_frames, dtype="<i2") == 0)

    _, _, _, _, combined_frames = _read_wav(sorted((tmp_path / "combined").glob("*.wav"))[0])
    assert np.all(np.frombuffer(combined_frames, dtype="<i2") == 5000)  # mic(5000) + silence(0)


def test_do_tick_overflow_drains_surplus_and_pads_other_track(tmp_path):
    """A FIFO past FIFO_CAP_BYTES drains its full surplus this tick; the
    other track's chunk is padded with silence to the same (larger) length
    so the mix stays a straight per-sample sum of equal-length arrays."""
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)

    overflow_samples = FIFO_CAP_BYTES // 2 + 500  # samples, past the cap
    mgr._fifos["mic"].push(np.full(overflow_samples, 100, dtype="<i2").tobytes())
    # system FIFO stays empty this tick

    mgr._do_tick()
    _finalize_manual_writers(mgr)

    _, _, _, mic_nframes, _ = _read_wav(sorted((tmp_path / "mic").glob("*.wav"))[0])
    _, _, _, sys_nframes, _ = _read_wav(sorted((tmp_path / "system").glob("*.wav"))[0])
    assert mic_nframes == sys_nframes == overflow_samples
    assert mic_nframes > 320  # confirms this tick drained more than the nominal 20ms


# ---------------------------------------------------------------------------
# _ByteFifo
# ---------------------------------------------------------------------------

def test_byte_fifo_drain_returns_less_than_requested_when_starved():
    fifo = _ByteFifo()
    fifo.push(b"\x01\x02")
    assert fifo.drain(10) == b"\x01\x02"
    assert fifo.available() == 0


def test_byte_fifo_push_empty_is_noop():
    fifo = _ByteFifo()
    fifo.push(b"")
    assert fifo.available() == 0


# ---------------------------------------------------------------------------
# enumerate_devices()
# ---------------------------------------------------------------------------

def test_enumerate_devices_unavailable_when_soundcard_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "soundcard", None)
    result = enumerate_devices()
    assert result == {"microphones": [], "loopbacks": [], "available": False}


def _fake_device(id_: str, name: str, isloopback: bool = False):
    dev = MagicMock()
    dev.id = id_
    dev.name = name
    dev.isloopback = isloopback
    return dev


def test_enumerate_devices_splits_mics_and_loopbacks(monkeypatch):
    fake_sc = types.ModuleType("soundcard")
    fake_sc.all_microphones = MagicMock(return_value=[
        _fake_device("mic1", "Built-in Microphone", isloopback=False),
        _fake_device("loop1", "Speakers (loopback)", isloopback=True),
    ])
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    result = enumerate_devices()
    assert result["available"] is True
    assert result["microphones"] == [{"id": "mic1", "name": "Built-in Microphone"}]
    assert result["loopbacks"] == [{"id": "loop1", "name": "Speakers (loopback)"}]


def test_enumerate_devices_degrades_to_unavailable_on_exception(monkeypatch):
    fake_sc = types.ModuleType("soundcard")
    fake_sc.all_microphones = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    result = enumerate_devices()
    assert result == {"microphones": [], "loopbacks": [], "available": False}


def test_resolve_device_name_found_and_fallback():
    devices = [{"id": "a", "name": "Device A"}]
    assert resolve_device_name(devices, "a") == "Device A"
    assert resolve_device_name(devices, "unknown-id") == "unknown-id"
