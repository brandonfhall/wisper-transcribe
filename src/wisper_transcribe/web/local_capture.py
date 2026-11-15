"""LocalCaptureManager — local mic + system-audio capture, mirroring BotManager.

Thread-based rather than asyncio because soundcard recorders are blocking
pulls. No reconnect or token handling.

Data flow: two capture threads (mic, system) resample each block to 16 kHz
mono int16 and push it onto per-track byte FIFOs. A single tick thread wakes
every ~20 ms, drains one chunk from each FIFO (silence for a starved track),
writes each track to its own SegmentedWavWriter, and writes their int32 sum
(soft-limited to int16) to the combined writer. Silence substitution keeps all
three tracks wall-clock aligned, which live speaker attribution relies on.

Each FIFO is also a jitter buffer: a track is drained only once it holds
``JITTER_S`` of audio, and re-primes after it runs dry. A process stall (the
live job loading a model, say) delays the capture threads and the tick thread
alike; the wall-clock ticker then catches up with back-to-back ticks, which
would otherwise drain empty FIFOs and pad zeros into the middle of speech.
On Windows the WASAPI buffer is opened at ``WASAPI_BUFFER_S`` (soundcard's
default is one ~10 ms device period), so a stall doesn't drop samples either.
soundcard's "data discontinuity" warnings are counted, not printed, and logged
as a per-session summary.

Test seams:
  - ``capture_factory(device_id, samplerate)`` -> blocking iterator of
    ``(float32 array (n, ch), samplerate)``. The default wraps ``soundcard``,
    imported lazily so this module still imports where it is missing or broken.
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
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np

from wisper_transcribe.models import Recording
from wisper_transcribe.recording_manager import (
    combined_path_for,
    combined_wav_path_for,
    create_recording,
    encode_combined_flac,
    record_completed_wav_segment,
    register_capture_files,
    start_listen_copy,
    trim_recording_audio,
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
# Jitter-buffer depth per track (see the module docstring). Read when a
# manager is built, so tests can pin it.
JITTER_S = 0.25
# WASAPI shared-mode buffer on Windows. Bigger only adds capacity: data still
# arrives every device period, so it adds no latency.
WASAPI_BUFFER_S = 0.5
# Combined-mix soft limiter: linear up to this fraction of full scale, then a
# tanh knee that approaches (never reaches past) full scale.
LIMIT_KNEE = 0.8
# How often the tick thread logs capture health at debug level.
_STATS_EVERY_TICKS = int(60 / TICK_S)

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
# soundcard "data discontinuity" warnings -> per-track counters
# ---------------------------------------------------------------------------

_discontinuity_lock = threading.Lock()
_discontinuities: dict[str, int] = {}
_counter_installed = False


def _track_of_thread(name: str) -> Optional[str]:
    """``"mic"``/``"system"`` for a capture thread name, else None."""
    for track in _TRACKS:
        if name.startswith(f"local-capture-{track}-"):
            return track
    return None


def _install_discontinuity_counter() -> None:
    """Count soundcard's ``SoundcardRuntimeWarning`` per track instead of
    printing it. Installed once per process.

    ``warnings.catch_warnings()`` isn't thread-safe, so this wraps
    ``warnings.showwarning`` once (counting by the warning thread's name) and
    sets the category to ``always`` so each one is counted, not just the first.
    Every other warning goes to the previous handler unchanged.
    """
    global _counter_installed
    with _discontinuity_lock:
        if _counter_installed:
            return
        _counter_installed = True
    try:
        from soundcard import SoundcardRuntimeWarning
    except Exception:
        return
    warnings.filterwarnings("always", category=SoundcardRuntimeWarning)
    previous = warnings.showwarning

    def _show(message, category, filename, lineno, file=None, line=None):
        if issubclass(category, SoundcardRuntimeWarning):
            track = _track_of_thread(threading.current_thread().name) or "other"
            with _discontinuity_lock:
                _discontinuities[track] = _discontinuities.get(track, 0) + 1
            return
        return previous(message, category, filename, lineno, file, line)

    warnings.showwarning = _show


def take_discontinuities() -> dict[str, int]:
    """Discontinuity counts per track since the last call, then reset."""
    with _discontinuity_lock:
        counts = dict(_discontinuities)
        _discontinuities.clear()
    return counts


def soft_limit(mixed: np.ndarray) -> np.ndarray:
    """Soft-limit an int32 sum of int16 tracks into int16.

    Samples within ``LIMIT_KNEE`` of full scale pass unchanged; louder ones
    bend along a tanh knee toward full scale instead of clipping flat, so a
    loud moment on both tracks doesn't square off the mix.
    """
    full = 32767.0
    knee = LIMIT_KNEE * full
    x = mixed.astype(np.float64)
    mag = np.abs(x)
    over = mag > knee
    if np.any(over):
        span = full - knee
        x[over] = np.sign(x[over]) * (knee + span * np.tanh((mag[over] - knee) / span))
    return np.clip(np.rint(x), -32768, 32767).astype("<i2")


# ---------------------------------------------------------------------------
# Default (production) capture factory -- soundcard, lazily imported
# ---------------------------------------------------------------------------

def _soundcard_capture_factory(device_id: str, samplerate: int) -> Iterator:
    """Default ``capture_factory``: open a soundcard device and block on ``record()``.

    Imports ``soundcard`` lazily. Being a generator, its body —
    including Windows COM initialisation — runs on the capture thread that
    iterates it, and uninitialises on exit. On Windows the recorder gets a
    ``WASAPI_BUFFER_S`` buffer; elsewhere ``blocksize`` sets the fragment size
    (latency), so the backend default stays.
    """
    import soundcard as sc

    _install_discontinuity_counter()
    _com_init()
    try:
        mic = sc.get_microphone(id=device_id, include_loopback=True)
        options = {"blocksize": int(samplerate * WASAPI_BUFFER_S)} if sys.platform == "win32" else {}
        with mic.recorder(samplerate=samplerate, **options) as recorder:
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
        jitter_s: Optional[float] = None,
    ):
        """
        capture_factory(device_id, samplerate) -> iterator of (np.ndarray, samplerate)
            Defaults to `_soundcard_capture_factory`. Tests inject a
            scripted generator factory and never import soundcard.
        ticker() -> iterator, yielding once per tick.
            Defaults to `_wall_clock_ticker`. Tests inject a finite instant
            generator.
        jitter_s
            Jitter-buffer depth per track; defaults to the module's
            ``JITTER_S``. 0 drains whatever is there every tick.
        """
        self._data_dir = Path(data_dir)
        self._capture_factory = capture_factory or _soundcard_capture_factory
        self._ticker = ticker or _wall_clock_ticker
        depth = JITTER_S if jitter_s is None else jitter_s
        self._jitter_bytes = int(depth * RATE) * BYTES_PER_SAMPLE
        # Per-track jitter-buffer state and capture-health counters, reset
        # each session. Touched only by the tick thread (and switch_device,
        # which only clears a flag).
        self._primed: dict[str, bool] = {t: False for t in _TRACKS}
        self._underruns: dict[str, int] = {t: 0 for t in _TRACKS}
        self._max_tick_gap_s = 0.0
        self._ticks = 0
        self._discontinuity_totals: dict[str, int] = {}

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
        # Per-track capture state, so switch_device() can retire one track's
        # thread without touching the other. `_capture_events[track]` is the
        # generation token: a capture loop checks its own event before every
        # push, so a retired thread can never push into the FIFO after a
        # switch (even if it was mid-block in record() when the switch ran).
        self._track_threads: dict[str, threading.Thread] = {}
        self._capture_events: dict[str, threading.Event] = {}
        self._track_device_ids: dict[str, str] = {}
        # Tracks whose capture thread died on a device error; a new thread on
        # that track clears it. A degraded session recovers only when empty.
        self._failed_tracks: set[str] = set()
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

    def current_device_ids(self) -> dict[str, str]:
        """The device id each track's capture thread is on now (for pre-selecting
        the switch dropdown). Empty before the first session."""
        return dict(self._track_device_ids)

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
        name: Optional[str] = None,
    ) -> Recording:
        """Create a Recording and start both capture threads and the tick thread.

        ``name`` is a free-text, display-only session title; it never becomes a
        path. The device ids are held in memory (``_track_device_ids``), not
        stored.
        """
        if self._active_recording is not None and self._active_recording.status in ACTIVE_STATUSES:
            raise RuntimeError(f"Session {self._active_recording.id} is already active")

        recording = create_recording(
            voice_channel_id="",
            guild_id="",
            campaign_slug=campaign_slug,
            data_dir=self._data_dir,
            source="local",
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
        self._primed = {t: False for t in _TRACKS}
        self._underruns = {t: 0 for t in _TRACKS}
        self._max_tick_gap_s = 0.0
        self._ticks = 0
        self._discontinuity_totals = {}
        take_discontinuities()  # drop counts from before this session

        self._stop_event = threading.Event()
        # Published before any thread starts: a device that fails at once
        # calls _mark_degraded(), which needs the active recording.
        self._active_recording = recording
        self._track_threads = {}
        self._failed_tracks = set()
        self._capture_events = {}
        self._capture_threads = []
        self._track_device_ids = {"mic": mic_device_id, "system": system_device_id}
        for track, device_id in (("mic", mic_device_id), ("system", system_device_id)):
            self._start_capture_thread(track, device_id)

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
        # Play out what the jitter buffer still holds (with no buffer there
        # is no intentional backlog, and a racing tail isn't worth a tick).
        if self._jitter_bytes and self._writers and self._combined_writer is not None:
            try:
                self._do_tick(flush=True)
            except Exception:
                log.warning("Could not drain the capture buffers at stop", exc_info=True)
        self._log_capture_stats(final=True)
        self._finalise(recording)
        log.info("Local capture session stopped: %s", recording.id)

    # ------------------------------------------------------------------
    # Capture threads -- fill FIFOs only, never write
    # ------------------------------------------------------------------

    def _start_capture_thread(
        self, track: str, device_id: str, event: Optional[threading.Event] = None
    ) -> threading.Thread:
        """Start one track's capture thread; returns it and records the state.

        The thread checks its own `event` (never the shared `_stop_event`)
        before every push, so a `switch_device()` retires the old thread
        without stopping the session.
        """
        if event is None:
            event = threading.Event()
        self._capture_events[track] = event
        self._failed_tracks.discard(track)
        thread = threading.Thread(
            target=self._capture_loop,
            args=(track, device_id, self._fifos[track], event),
            name=f"local-capture-{track}-{self._active_recording.id[:8]}",
            daemon=True,
        )
        self._track_threads[track] = thread
        self._track_device_ids[track] = device_id
        thread.start()
        # Keep the legacy list in sync (tests and stop_session() join it).
        self._capture_threads = list(self._track_threads.values())
        return thread

    def switch_device(self, track: str, device_id: str) -> None:
        """Restart one track's capture thread on ``device_id``; blocking.

        Only that track is disturbed -- segments, the combined track, the
        live transcript, and the ``Recording`` continue. The old thread is
        retired with its own stop event (checked before every FIFO push), so
        it can never push after the switch even if ``record()`` is mid-block;
        it is joined with a short timeout. A session that went ``degraded``
        because this track's device died returns to ``recording``. Call off
        the request thread (see routes/record.py).
        """
        if track not in _TRACKS:
            raise ValueError(f"unknown track {track!r}")
        recording = self._active_recording
        if recording is None or recording.status not in ACTIVE_STATUSES:
            raise RuntimeError("no active session")

        old_event = self._capture_events.get(track)
        old_thread = self._track_threads.get(track)
        if old_event is not None:
            old_event.set()
        if old_thread is not None:
            old_thread.join(timeout=2.0)

        # The new device starts from an empty buffer; re-prime it.
        self._primed[track] = False
        self._start_capture_thread(track, device_id)

        # Recover a track-killed session, but only once no track is still
        # failed: switching the healthy track leaves the dead one dead. Via the targeted writer (a full-object save is off-limits
        # during capture).
        if recording.status == "degraded" and not self._failed_tracks:
            recording.status = "recording"
            try:
                update_recording_status(recording.id, "recording", self._data_dir)
            except Exception:
                log.warning("Failed to return recording %s to recording",
                            recording.id, exc_info=True)
        log.info("Switched %s capture to device %s on recording %s",
                 track, device_id, recording.id)

    def _capture_loop(
        self, track: str, device_id: str, fifo: "_ByteFifo", event: threading.Event
    ) -> None:
        try:
            for block, samplerate in self._capture_factory(device_id, _DEFAULT_CAPTURE_SAMPLERATE):
                if self._stop_event.is_set() or event.is_set():
                    return
                pcm = resample_to_16k_mono(np.asarray(block), samplerate)
                if pcm:
                    # Re-check right before the push: a switch that lands
                    # between the check above and here must still keep the
                    # old thread out of the FIFO.
                    if event.is_set() or self._stop_event.is_set():
                        return
                    fifo.push(pcm)
        except Exception:
            log.exception("Local capture thread %r failed", track)
            # Don't degrade a track that a switch already retired.
            if not event.is_set() and not self._stop_event.is_set():
                self._failed_tracks.add(track)
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
        last = time.monotonic()
        for _ in self._ticker():
            if self._stop_event.is_set():
                return
            now = time.monotonic()
            self._max_tick_gap_s = max(self._max_tick_gap_s, now - last)
            last = now
            self._do_tick()
            self._ticks += 1
            if self._ticks % _STATS_EVERY_TICKS == 0:
                self._log_capture_stats(final=False)

    def _log_capture_stats(self, final: bool) -> None:
        """Capture health: discontinuities (dropped device samples), jitter
        underruns (zeros inserted), and the longest gap between ticks.

        Debug once a minute; one INFO summary when the session ends.
        """
        for track, n in take_discontinuities().items():
            self._discontinuity_totals[track] = self._discontinuity_totals.get(track, 0) + n
        recording = self._active_recording
        rid = recording.id if recording is not None else "?"
        msg = ("Local capture %s: %d ticks, discontinuities %s, underruns %s, "
               "longest tick gap %.0f ms")
        args = (rid, self._ticks, dict(self._discontinuity_totals), dict(self._underruns),
                self._max_tick_gap_s * 1000)
        if final:
            log.info(msg, *args)
        else:
            log.debug(msg, *args)

    def _do_tick(self, flush: bool = False) -> None:
        """One tick: drain each FIFO, write per-track + mixed combined chunks.

        Normally drains exactly `TICK_BYTES` (20 ms) per primed track. A track
        is primed once its FIFO holds the jitter depth; until then it writes
        silence. A primed track that comes up short (underrun) pads the rest
        with silence and re-primes. Either way every track stays wall-clock
        continuous.

        If a FIFO has grown past `FIFO_CAP_BYTES` (device clock running
        faster than the tick clock), this tick drains the full surplus
        instead of dropping audio -- and every track's chunk for this tick
        is padded with silence up to that larger length, so the combined
        mix stays a straight per-sample sum of two equal-length arrays.

        ``flush`` (used once at stop) drains everything left, primed or not,
        so the jitter buffer's last fraction of a second isn't lost.
        """
        tick_bytes = TICK_BYTES
        for fifo in self._fifos.values():
            avail = fifo.available()
            if avail > FIFO_CAP_BYTES or (flush and avail > tick_bytes):
                tick_bytes = max(tick_bytes, avail - avail % BYTES_PER_SAMPLE)

        wanted = tick_bytes // BYTES_PER_SAMPLE
        track_samples: dict[str, np.ndarray] = {}
        for name, fifo in self._fifos.items():
            if not self._primed.get(name, False) and (flush or fifo.available() >= self._jitter_bytes):
                self._primed[name] = True
            chunk = fifo.drain(tick_bytes) if self._primed.get(name, False) else b""
            if self._primed.get(name, False) and len(chunk) < TICK_BYTES and not flush:
                self._primed[name] = False
                self._underruns[name] = self._underruns.get(name, 0) + 1
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
        mixed_bytes = soft_limit(mixed).tobytes()
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

            combined_out = combined_wav_path_for(recording.id, self._data_dir)
            combined_out.parent.mkdir(parents=True, exist_ok=True)
            try:
                merged = concat_wav_segments(combined_dir, combined_out)
            except Exception:
                log.warning("Failed to concatenate local capture combined segments", exc_info=True)
                merged = None
            if merged is not None:
                # Store FLAC and verify it before deleting the WAV; on failure
                # keep and register the WAV, so the capture still finishes.
                if encode_combined_flac(recording.id, merged, self._data_dir) is not None:
                    merged.unlink(missing_ok=True)
                    merged = combined_path_for(recording.id, self._data_dir)
                recording.combined_path = merged
                log.info("Local recording %s combined track written to %s", recording.id, merged)

        register_capture_files(recording.id, self._data_dir)
        try:
            trim_recording_audio(recording.id, self._data_dir)
        except Exception:
            log.warning("Could not trim local recording %s", recording.id, exc_info=True)

        # Degraded is a live state (a track died mid-session); once stopped the
        # session is over like any other, or it would block every new session.
        became_completed = recording.status in ACTIVE_STATUSES
        if became_completed:
            recording.status = "completed"
            recording.ended_at = datetime.now(timezone.utc)

        # combined.flac's path is derived from the layout; only the status
        # change needs writing, and a terminal status set earlier is kept.
        if became_completed:
            update_recording_status(recording.id, "completed", self._data_dir,
                                    ended_at=recording.ended_at)
            log.info("Local recording %s finalised as completed", recording.id)
        # The denoised playback copy is made in the background.
        start_listen_copy(recording.id, self._data_dir)
