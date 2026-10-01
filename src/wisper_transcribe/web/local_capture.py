"""LocalCaptureManager — local mic + system-audio capture, mirroring BotManager.

Thread-based rather than asyncio because soundcard recorders are blocking
pulls. No reconnect or token handling.

Data flow: two capture threads (mic, system) resample each block to 16 kHz
mono int16 and push it onto per-track byte FIFOs. A single tick thread wakes
every ~20 ms, drains one chunk from each FIFO (silence for a starved track),
writes each track to its own SegmentedWavWriter, and writes their int32 sum
(clipped to int16) to the combined writer. Silence substitution keeps all
three tracks wall-clock aligned, which live speaker attribution relies on.

Test seams:
  - ``capture_factory(device_id, samplerate)`` -> blocking iterator of
    ``(float32 array (n, ch), samplerate)``. The default wraps ``soundcard``,
    imported lazily so this module works without the ``[live]`` extra.
  - ``ticker()`` -> iterator yielding once per tick. Tests pass a finite,
    instant generator.

``start_session`` raises if a session is still active; Discord-vs-local
exclusion is enforced in routes/record.py. ``_active_recording`` is never reset
to None after a session, so check ``.status``, not ``is not None``.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

from wisper_transcribe.models import Recording
from wisper_transcribe.recording_manager import (
    create_recording,
    record_completed_wav_segment,
    update_recording_status,
)
from wisper_transcribe.web.audio_writer import (
    SegmentedWavWriter,
    concat_wav_segments,
    resample_to_16k_mono,
)
from wisper_transcribe.web.live_transcribe import _rms

log = logging.getLogger(__name__)

RATE = 16000
TICK_S = 0.020
SAMPLES_PER_TICK = int(RATE * TICK_S)              # 320
BYTES_PER_SAMPLE = 2
TICK_BYTES = SAMPLES_PER_TICK * BYTES_PER_SAMPLE   # 640
FIFO_CAP_S = 2.0
FIFO_CAP_BYTES = int(FIFO_CAP_S * RATE) * BYTES_PER_SAMPLE
_DEFAULT_CAPTURE_SAMPLERATE = 48000

_TRACKS = ("mic", "system")
# Used by routes/record.py for cross-manager exclusion. "Active" means
# `.status in ACTIVE_STATUSES`, never `active_recording is not None`.
ACTIVE_STATUSES = frozenset({"recording", "degraded"})


# ---------------------------------------------------------------------------
# Byte FIFO
# ---------------------------------------------------------------------------

class _ByteFifo:
    """Thread-safe byte buffer: a capture thread pushes, the tick thread drains.

    `drain(n)` never blocks -- it returns whatever is available up to `n`
    bytes (possibly less, possibly zero). Starved-track silence substitution
    and FIFO-overflow draining both fall out of that same non-blocking
    contract in `LocalCaptureManager._do_tick()`.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()

    def push(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._buf.extend(data)

    def available(self) -> int:
        with self._lock:
            return len(self._buf)

    def drain(self, n_bytes: int) -> bytes:
        with self._lock:
            n = min(n_bytes, len(self._buf))
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk


# ---------------------------------------------------------------------------
# COM init (Windows soundcard threading caveat)
# ---------------------------------------------------------------------------

def _com_init() -> None:
    """CoInitialize the calling thread. No-op on non-Windows."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        log.debug("CoInitialize failed (non-fatal)", exc_info=True)


def _com_uninit() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Default (production) capture factory -- soundcard, lazily imported
# ---------------------------------------------------------------------------

def _soundcard_capture_factory(device_id: str, samplerate: int) -> Iterator:
    """Default ``capture_factory``: open a soundcard device and block on ``record()``.

    Imports ``soundcard`` lazily (optional extra). Being a generator, its body —
    including Windows COM initialisation — runs on the capture thread that
    iterates it, and uninitialises on exit.
    """
    import soundcard as sc

    _com_init()
    try:
        mic = sc.get_microphone(id=device_id, include_loopback=True)
        with mic.recorder(samplerate=samplerate) as recorder:
            while True:
                block = recorder.record(numframes=None)
                yield block, samplerate
    finally:
        _com_uninit()


def _wall_clock_ticker() -> Iterator:
    """Default `ticker`: yields once per real ~20 ms, forever (until the
    consumer stops calling next() -- there is no other way to stop a
    generator-based ticker)."""
    next_t = time.monotonic()
    while True:
        yield
        next_t += TICK_S
        remaining = next_t - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

_UNAVAILABLE_DEVICES = {
    "microphones": [],
    "loopbacks": [],
    "available": False,
    "default_microphone_id": "",
    "default_loopback_id": "",
}


def enumerate_devices() -> dict:
    """Return ``{"microphones", "loopbacks", "available", "default_microphone_id",
    "default_loopback_id"}``.

    Devices are ``{"id", "name"}``. ``available`` is False (never an exception)
    when ``soundcard`` is missing or enumeration fails, so the Record page can
    hide the Local card. The default ids are the OS defaults ("" if unknown) so
    the picker can preselect them.
    """
    try:
        import soundcard as sc
    except Exception:
        return dict(_UNAVAILABLE_DEVICES)

    try:
        all_mics = sc.all_microphones(include_loopback=True)
    except Exception:
        log.warning("soundcard device enumeration failed", exc_info=True)
        return dict(_UNAVAILABLE_DEVICES)

    microphones: list = []
    loopbacks: list = []
    for m in all_mics:
        try:
            entry = {"id": str(getattr(m, "id", m)), "name": str(getattr(m, "name", m))}
        except Exception:
            continue
        (loopbacks if getattr(m, "isloopback", False) else microphones).append(entry)

    default_microphone_id = ""
    try:
        default_microphone_id = str(sc.default_microphone().id)
    except Exception:
        pass

    default_loopback_id = ""
    try:
        # soundcard's Windows loopback mic for a render endpoint shares that
        # endpoint's speaker id -- this is the same id space as `loopbacks`.
        default_loopback_id = str(sc.default_speaker().id)
    except Exception:
        pass

    return {
        "microphones": microphones,
        "loopbacks": loopbacks,
        "available": True,
        "default_microphone_id": default_microphone_id,
        "default_loopback_id": default_loopback_id,
    }


def resolve_device_name(devices: list, device_id: str) -> str:
    """Look up `device_id` in an `enumerate_devices()` list; falls back to
    echoing the id itself when not found (device ids are never used in a
    file path, so this is safe -- see .claude/rules/web-security.md)."""
    for d in devices:
        if d.get("id") == device_id:
            return d.get("name", device_id)
    return device_id


# ---------------------------------------------------------------------------
# LocalCaptureManager
# ---------------------------------------------------------------------------

class LocalCaptureManager:
    """Manages one local mic + system-audio capture session at a time.

    Same lifecycle shape as ``BotManager`` but synchronous; async callers
    must wrap ``stop_session()`` and ``stop()`` in ``asyncio.to_thread()``.
    """

    def __init__(
        self,
        data_dir: Path,
        capture_factory: Optional[Callable] = None,
        ticker: Optional[Callable[[], Iterator]] = None,
    ):
        """
        capture_factory(device_id, samplerate) -> iterator of (np.ndarray, samplerate)
            Defaults to `_soundcard_capture_factory`. Tests inject a
            scripted generator factory and never import soundcard.
        ticker() -> iterator, yielding once per tick.
            Defaults to `_wall_clock_ticker`. Tests inject a finite instant
            generator.
        """
        self._data_dir = Path(data_dir)
        self._capture_factory = capture_factory or _soundcard_capture_factory
        self._ticker = ticker or _wall_clock_ticker

        self._active_recording: Optional[Recording] = None
        self._stop_event = threading.Event()
        self._fifos: dict[str, _ByteFifo] = {}
        self._writers: dict[str, SegmentedWavWriter] = {}
        self._combined_writer: Optional[SegmentedWavWriter] = None
        self._combined_dir: Optional[Path] = None
        # Wall-clock start of the combined writer's *current* segment, used
        # by record_completed_wav_segment() to approximate segment
        # duration -- see that function's docstring.
        self._combined_segment_started_at: Optional[datetime] = None
        self._capture_threads: list[threading.Thread] = []
        self._tick_thread: Optional[threading.Thread] = None
        # Optional live-transcription tap, called from the tick thread with
        # (mic, system, mixed) bytes each tick. Must not block.
        self._live_sink: Optional[Callable[[bytes, bytes, bytes], None]] = None
        # Per-track peak RMS since the last get_and_reset_levels(), so ~1 s
        # polls don't miss transients. Locked so both values read together.
        self._level_lock = threading.Lock()
        self._level_peaks: dict[str, float] = {"mic": 0.0, "system": 0.0}

    # ------------------------------------------------------------------
    # Lifecycle (mirrors BotManager/JobQueue)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Called from FastAPI lifespan -- initialises state."""
        log.info("LocalCaptureManager started")

    def stop(self) -> None:
        """Called from FastAPI lifespan -- stops any active session. Blocking."""
        if self._active_recording is not None and self._active_recording.status in ACTIVE_STATUSES:
            self.stop_session()
        log.info("LocalCaptureManager stopped")

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    @property
    def active_recording(self) -> Optional[Recording]:
        return self._active_recording

    def set_live_sink(self, sink: Optional[Callable[[bytes, bytes, bytes], None]]) -> None:
        """Register (or clear with ``None``) the live-transcription tap. Safe at any time."""
        self._live_sink = sink

    def get_and_reset_levels(self) -> dict[str, float]:
        """Return `{"mic": rms, "system": rms}` peak levels observed since
        the last call, then reset the peaks to 0.0. Used by `/record/sse`
        to drive the Record page's live level gauge; harmless to call when
        no session is active (returns whatever -- unread -- zeros are there)."""
        with self._level_lock:
            levels = dict(self._level_peaks)
            self._level_peaks = {"mic": 0.0, "system": 0.0}
        return levels

    def start_session(
        self,
        campaign_slug: Optional[str],
        mic_device_id: str,
        system_device_id: str,
        mic_name: str = "",
        system_name: str = "",
        name: Optional[str] = None,
    ) -> Recording:
        """Create a Recording and start both capture threads and the tick thread.

        ``mic_name``/``system_name`` are server-resolved device names (falling
        back to the raw id). ``name`` is a free-text, display-only session
        title; it never becomes a path.
        """
        if self._active_recording is not None and self._active_recording.status in ACTIVE_STATUSES:
            raise RuntimeError(f"Session {self._active_recording.id} is already active")

        recording = create_recording(
            voice_channel_id="",
            guild_id="",
            campaign_slug=campaign_slug,
            data_dir=self._data_dir,
            source="local",
            devices={
                "mic": mic_name or mic_device_id,
                "system": system_name or system_device_id,
            },
            name=name,
        )

        rec_dir = self._data_dir / "recordings" / recording.id
        self._fifos = {"mic": _ByteFifo(), "system": _ByteFifo()}
        self._writers = {
            "mic": SegmentedWavWriter(stream_dir=rec_dir / "per-user" / "mic"),
            "system": SegmentedWavWriter(stream_dir=rec_dir / "per-user" / "system"),
        }
        self._combined_dir = rec_dir / "combined"
        self._combined_writer = SegmentedWavWriter(stream_dir=self._combined_dir)
        self._combined_segment_started_at = datetime.now(timezone.utc)

        with self._level_lock:
            self._level_peaks = {"mic": 0.0, "system": 0.0}

        self._stop_event = threading.Event()
        # Published before any thread starts: a device that fails at once
        # calls _mark_degraded(), which needs the active recording.
        self._active_recording = recording
        self._capture_threads = [
            threading.Thread(
                target=self._capture_loop,
                args=("mic", mic_device_id, self._fifos["mic"]),
                name=f"local-capture-mic-{recording.id[:8]}",
                daemon=True,
            ),
            threading.Thread(
                target=self._capture_loop,
                args=("system", system_device_id, self._fifos["system"]),
                name=f"local-capture-system-{recording.id[:8]}",
                daemon=True,
            ),
        ]
        for t in self._capture_threads:
            t.start()

        self._tick_thread = threading.Thread(
            target=self._tick_loop,
            name=f"local-tick-{recording.id[:8]}",
            daemon=True,
        )
        self._tick_thread.start()
        log.info("Local capture session started: %s", recording.id)
        return recording

    def stop_session(self) -> None:
        """Stop and join the threads, then finalise. Blocking.

        The only finaliser: the tick thread never finalises, so there is
        exactly one finalise per session whether the ticker ends on its own
        (tests) or via ``stop_event``.
        """
        if self._active_recording is None:
            return
        recording = self._active_recording
        self._stop_event.set()
        for t in self._capture_threads:
            t.join(timeout=5.0)
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=5.0)
        self._finalise(recording)
        log.info("Local capture session stopped: %s", recording.id)

    # ------------------------------------------------------------------
    # Capture threads -- fill FIFOs only, never write
    # ------------------------------------------------------------------

    def _capture_loop(self, name: str, device_id: str, fifo: "_ByteFifo") -> None:
        try:
            for block, samplerate in self._capture_factory(device_id, _DEFAULT_CAPTURE_SAMPLERATE):
                if self._stop_event.is_set():
                    return
                pcm = resample_to_16k_mono(np.asarray(block), samplerate)
                if pcm:
                    fifo.push(pcm)
        except Exception:
            log.exception("Local capture thread %r failed", name)
            self._mark_degraded()

    def _mark_degraded(self) -> None:
        """Mark the session degraded when a capture thread dies (e.g. device unplugged).

        Otherwise the dead track silently records silence. Never raises: the
        tick thread keeps running regardless.
        """
        recording = self._active_recording
        if recording is None or recording.status != "recording":
            return
        try:
            recording.status = "degraded"
            update_recording_status(recording.id, "degraded", self._data_dir)
        except Exception:
            log.warning("Failed to mark recording %s degraded", recording.id, exc_info=True)

    # ------------------------------------------------------------------
    # Tick thread -- does ALL writing
    # ------------------------------------------------------------------

    def _tick_loop(self) -> None:
        for _ in self._ticker():
            if self._stop_event.is_set():
                return
            self._do_tick()

    def _do_tick(self) -> None:
        """One tick: drain each FIFO, write per-track + mixed combined chunks.

        Normally drains exactly `TICK_BYTES` (20 ms) per track -- draining
        less than that (because a track's FIFO is starved, e.g. WASAPI
        loopback with nothing playing) is silently padded with silence so
        every track stays wall-clock continuous.

        If a FIFO has grown past `FIFO_CAP_BYTES` (device clock running
        faster than the tick clock), this tick drains the full surplus
        instead of dropping audio -- and every track's chunk for this tick
        is padded with silence up to that larger length, so the combined
        mix stays a straight per-sample sum of two equal-length arrays.
        """
        tick_bytes = TICK_BYTES
        for fifo in self._fifos.values():
            avail = fifo.available()
            if avail > FIFO_CAP_BYTES:
                tick_bytes = max(tick_bytes, avail)

        wanted = tick_bytes // BYTES_PER_SAMPLE
        track_samples: dict[str, np.ndarray] = {}
        for name, fifo in self._fifos.items():
            chunk = fifo.drain(tick_bytes)
            samples = np.frombuffer(chunk, dtype="<i2") if chunk else np.zeros(0, dtype="<i2")
            if len(samples) < wanted:
                samples = np.concatenate([samples, np.zeros(wanted - len(samples), dtype="<i2")])
            track_samples[name] = samples
            self._writers[name].write(samples.tobytes())

        with self._level_lock:
            for name in _TRACKS:
                r = _rms(track_samples[name])
                if r > self._level_peaks[name]:
                    self._level_peaks[name] = r

        mixed = np.zeros(wanted, dtype=np.int32)
        for name in _TRACKS:
            mixed += track_samples[name].astype(np.int32)
        mixed_clipped = np.clip(mixed, -32768, 32767).astype("<i2")
        mixed_bytes = mixed_clipped.tobytes()
        completed_path = self._combined_writer.write(mixed_bytes)
        if completed_path is not None:
            self._combined_segment_started_at = record_completed_wav_segment(
                self._active_recording.id,
                completed_path,
                self._combined_segment_started_at,
                finalized=True,
                data_dir=self._data_dir,
            )

        sink = self._live_sink
        if sink is not None:
            try:
                sink(track_samples["mic"].tobytes(), track_samples["system"].tobytes(), mixed_bytes)
            except Exception:
                log.warning("Live-transcription sink raised; disabling it", exc_info=True)
                self._live_sink = None

    # ------------------------------------------------------------------
    # Finalise (BotManager._finalise, minus Discord specifics)
    # ------------------------------------------------------------------

    def _finalise(self, recording: Recording) -> None:
        # Tick thread has already stopped by the time _finalise runs
        # (stop_session() joins it first) -- clearing here is defensive
        # tidiness, not a race guard.
        self._live_sink = None

        for writer in self._writers.values():
            try:
                writer.finalize()
            except Exception:
                log.warning("Failed to finalise local capture writer", exc_info=True)
        self._writers = {}

        combined_dir = self._combined_dir
        if self._combined_writer is not None:
            final_path: Optional[Path] = None
            try:
                final_path = self._combined_writer.finalize()
            except Exception:
                log.warning("Failed to finalise local capture combined writer", exc_info=True)
            if final_path is not None:
                record_completed_wav_segment(
                    recording.id,
                    final_path,
                    self._combined_segment_started_at,
                    finalized=True,
                    data_dir=self._data_dir,
                )
            self._combined_writer = None
            self._combined_segment_started_at = None

            combined_out = self._data_dir / "recordings" / recording.id / "combined.wav"
            try:
                merged = concat_wav_segments(combined_dir, combined_out)
            except Exception:
                log.warning("Failed to concatenate local capture combined segments", exc_info=True)
                merged = None
            if merged is not None:
                recording.combined_path = merged
                log.info("Local recording %s combined track written to %s", recording.id, merged)

        became_completed = recording.status == "recording"
        if became_completed:
            recording.status = "completed"
            recording.ended_at = datetime.now(timezone.utc)

        # combined.wav's path is derived from the layout; only the status
        # change needs writing, and a terminal status set earlier is kept.
        if became_completed:
            update_recording_status(recording.id, "completed", self._data_dir,
                                    ended_at=recording.ended_at)
            log.info("Local recording %s finalised as completed", recording.id)
