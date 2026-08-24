"""Tests for web/local_capture.py — Phase 1 (capture layer) + Phase 2 (live-sink tap).

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

from wisper_transcribe.recording_manager import append_marker, load_recordings
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


def test_start_session_passes_through_name(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0),
    )
    rec = mgr.start_session("campaign-1", "mic-dev", "sys-dev", name="Session 14 — the ambush")
    assert rec.name == "Session 14 — the ambush"
    mgr.stop_session()


def test_start_session_defaults_name_to_none(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0),
    )
    rec = mgr.start_session("campaign-1", "mic-dev", "sys-dev")
    assert rec.name is None
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
    # Regression: SegmentedWavWriter.finalize() still closes and returns a
    # path for the empty (0-frame) segment even with no ticks ever run --
    # record_completed_wav_segment() must skip it, or the manifest would
    # show a phantom "Segments: 1" contradicting combined_path being None.
    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.segment_manifest == []


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
# Degraded status on capture-thread failure
# ---------------------------------------------------------------------------

def _raising_capture_factory(device_id, samplerate):
    raise RuntimeError("device unplugged")


def test_capture_thread_failure_marks_recording_degraded(tmp_path):
    from wisper_transcribe.recording_manager import load_recordings

    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=_raising_capture_factory, ticker=instant_ticker(0),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    for t in mgr._capture_threads:
        t.join(timeout=5.0)

    assert rec.status == "degraded"
    assert load_recordings(tmp_path)[rec.id].status == "degraded"

    mgr.stop_session()  # cleanup


def test_mark_degraded_noop_when_no_active_recording(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0),
    )
    mgr._mark_degraded()  # must not raise


def test_mark_degraded_does_not_override_terminal_status(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    mgr.stop_session()
    assert rec.status == "completed"

    mgr._mark_degraded()

    assert rec.status == "completed"  # not overwritten by a late/spurious call


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
# Phase 2: live-transcription sink tap
# ---------------------------------------------------------------------------

def test_do_tick_calls_live_sink_with_three_equal_length_tracks(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)
    mgr._fifos["mic"].push(np.full(320, 1000, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 2000, dtype="<i2").tobytes())

    calls = []
    mgr.set_live_sink(lambda mic, system, mixed: calls.append((mic, system, mixed)))
    mgr._do_tick()
    _finalize_manual_writers(mgr)

    assert len(calls) == 1
    mic, system, mixed = calls[0]
    assert len(mic) == len(system) == len(mixed) == 320 * 2
    assert np.all(np.frombuffer(mic, dtype="<i2") == 1000)
    assert np.all(np.frombuffer(system, dtype="<i2") == 2000)
    assert np.all(np.frombuffer(mixed, dtype="<i2") == 3000)


def test_do_tick_no_sink_registered_is_noop(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)
    mgr._fifos["mic"].push(np.full(320, 1, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 1, dtype="<i2").tobytes())
    mgr._do_tick()  # must not raise with no sink set
    _finalize_manual_writers(mgr)


def test_live_sink_exception_disables_sink_without_crashing_tick(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)
    mgr._fifos["mic"].push(np.full(320, 1, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 1, dtype="<i2").tobytes())

    calls = {"n": 0}

    def bad_sink(mic, system, mixed):
        calls["n"] += 1
        raise RuntimeError("boom")

    mgr.set_live_sink(bad_sink)
    mgr._do_tick()  # must not raise
    assert calls["n"] == 1
    assert mgr._live_sink is None  # disabled after the exception

    mgr._fifos["mic"].push(np.full(320, 1, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 1, dtype="<i2").tobytes())
    mgr._do_tick()  # second tick proceeds fine with the sink cleared
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Live level gauge (Record page noise-floor meter)
# ---------------------------------------------------------------------------

def test_get_and_reset_levels_defaults_to_zero(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    assert mgr.get_and_reset_levels() == {"mic": 0.0, "system": 0.0}


def test_do_tick_updates_level_peaks(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)
    mgr._fifos["mic"].push(np.full(320, 1000, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 2000, dtype="<i2").tobytes())

    mgr._do_tick()
    _finalize_manual_writers(mgr)

    levels = mgr.get_and_reset_levels()
    assert levels == {"mic": 1000.0, "system": 2000.0}


def test_get_and_reset_levels_resets_after_read(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)
    mgr._fifos["mic"].push(np.full(320, 1000, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 1000, dtype="<i2").tobytes())
    mgr._do_tick()
    _finalize_manual_writers(mgr)

    mgr.get_and_reset_levels()
    assert mgr.get_and_reset_levels() == {"mic": 0.0, "system": 0.0}


def test_get_and_reset_levels_keeps_peak_across_multiple_ticks(tmp_path):
    """A quiet tick after a loud one must not erase the loud peak -- the
    gauge should reflect the loudest moment since the last poll, not the
    most recent tick."""
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    _wire_manual_writers(mgr, tmp_path)

    mgr._fifos["mic"].push(np.full(320, 3000, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 3000, dtype="<i2").tobytes())
    mgr._do_tick()

    mgr._fifos["mic"].push(np.full(320, 10, dtype="<i2").tobytes())
    mgr._fifos["system"].push(np.full(320, 10, dtype="<i2").tobytes())
    mgr._do_tick()
    _finalize_manual_writers(mgr)

    assert mgr.get_and_reset_levels() == {"mic": 3000.0, "system": 3000.0}


def test_start_session_resets_stale_level_peaks(tmp_path):
    mgr = LocalCaptureManager(
        data_dir=tmp_path, capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    mgr._level_peaks = {"mic": 500.0, "system": 500.0}
    mgr.start_session(None, "mic-1", "sys-1")
    try:
        assert mgr.get_and_reset_levels() == {"mic": 0.0, "system": 0.0}
    finally:
        mgr.stop_session()


def test_set_live_sink_replaces_and_clears():
    mgr = LocalCaptureManager(
        data_dir=".", capture_factory=scripted_capture_factory({}), ticker=instant_ticker(0)
    )
    sink = lambda mic, system, mixed: None
    mgr.set_live_sink(sink)
    assert mgr._live_sink is sink
    mgr.set_live_sink(None)
    assert mgr._live_sink is None


def test_finalise_clears_live_sink(tmp_path):
    n_ticks = 2
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    mgr.set_live_sink(lambda mic, system, mixed: None)
    mgr.start_session(None, "mic-dev", "sys-dev")
    _run_session_to_completion(mgr)
    assert mgr._live_sink is None


# ---------------------------------------------------------------------------
# Segment manifest + concurrent-append preservation
# ---------------------------------------------------------------------------

def test_combined_track_rotation_and_finalize_populate_segment_manifest(tmp_path, monkeypatch):
    """segment_manifest was previously always empty -- append_segment()
    existed but nothing ever called it for local sessions either. Each
    combined-track rotation during _do_tick, plus the final segment closed
    by _finalise, should land in Recording.segment_manifest as
    stream == "mixed" entries."""
    import functools

    import wisper_transcribe.web.local_capture as local_capture_module

    monkeypatch.setattr(
        local_capture_module,
        "SegmentedWavWriter",
        functools.partial(SegmentedWavWriter, segment_duration_s=0.1),
    )

    n_ticks = 13
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    _run_session_to_completion(mgr)

    combined_dir = tmp_path / "recordings" / rec.id / "combined"
    segments_on_disk = sorted(combined_dir.glob("*.wav"))

    manifest = load_recordings(tmp_path)[rec.id].segment_manifest
    assert len(manifest) == len(segments_on_disk)
    assert all(seg.stream == "mixed" for seg in manifest)
    assert [seg.index for seg in manifest] == list(range(len(segments_on_disk)))


def test_marker_added_mid_session_survives_finalise(tmp_path):
    """Regression: BotManager/LocalCaptureManager hold one long-lived
    Recording object for the whole session and periodically call
    save_recording() with it directly (per-track writer setup, disconnect
    handling, finalise). append_marker() mutates Recording.markers through
    an independent load-fresh + mutex-protected save from a different call
    site (the marker route) -- a plain save_recording(recording, ...) using
    the stale long-lived object would silently overwrite that marker.
    Confirmed reproducible before the save_recording_merged() fix: this
    test failed (0 markers) against the unfixed code."""
    n_ticks = 5
    blocks = {"mic-dev": [_block() for _ in range(n_ticks)]}
    mgr = LocalCaptureManager(
        data_dir=tmp_path,
        capture_factory=scripted_capture_factory(blocks),
        ticker=instant_ticker(n_ticks),
    )
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    append_marker(rec.id, data_dir=tmp_path)
    _run_session_to_completion(mgr)

    loaded = load_recordings(tmp_path)[rec.id]
    assert len(loaded.markers) == 1


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
    assert result == {
        "microphones": [],
        "loopbacks": [],
        "available": False,
        "default_microphone_id": "",
        "default_loopback_id": "",
    }


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
    fake_sc.default_microphone = MagicMock(return_value=_fake_device("mic1", "Built-in Microphone"))
    fake_sc.default_speaker = MagicMock(return_value=_fake_device("loop1", "Speakers (loopback)"))
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    result = enumerate_devices()
    assert result["available"] is True
    assert result["microphones"] == [{"id": "mic1", "name": "Built-in Microphone"}]
    assert result["loopbacks"] == [{"id": "loop1", "name": "Speakers (loopback)"}]
    assert result["default_microphone_id"] == "mic1"
    assert result["default_loopback_id"] == "loop1"


def test_enumerate_devices_default_ids_blank_when_lookup_fails(monkeypatch):
    fake_sc = types.ModuleType("soundcard")
    fake_sc.all_microphones = MagicMock(return_value=[
        _fake_device("mic1", "Built-in Microphone", isloopback=False),
    ])
    fake_sc.default_microphone = MagicMock(side_effect=RuntimeError("no default"))
    fake_sc.default_speaker = MagicMock(side_effect=RuntimeError("no default"))
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    result = enumerate_devices()
    assert result["available"] is True
    assert result["default_microphone_id"] == ""
    assert result["default_loopback_id"] == ""


def test_enumerate_devices_degrades_to_unavailable_on_exception(monkeypatch):
    fake_sc = types.ModuleType("soundcard")
    fake_sc.all_microphones = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    result = enumerate_devices()
    assert result == {
        "microphones": [],
        "loopbacks": [],
        "available": False,
        "default_microphone_id": "",
        "default_loopback_id": "",
    }


def test_resolve_device_name_found_and_fallback():
    devices = [{"id": "a", "name": "Device A"}]
    assert resolve_device_name(devices, "a") == "Device A"
    assert resolve_device_name(devices, "unknown-id") == "unknown-id"
