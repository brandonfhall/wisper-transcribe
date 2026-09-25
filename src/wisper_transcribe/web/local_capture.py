"""LocalCaptureManager — local mic + system-audio capture, mirroring BotManager.

Simpler than BotManager: no reconnect/backoff/close-code machinery, no token
resolution. Thread-based, not asyncio, because soundcard recorders are
blocking pulls (`recorder.record(numframes)` blocks until audio exists).

Threading/data-flow model (pinned in plan.md's Phase 1 implementation
notes): capture threads only fill FIFOs; the tick thread does ALL writing.
Two capture threads (mic, system) resample each block to 16 kHz mono int16
and push it onto per-track byte FIFOs. The tick thread wakes once per
`ticker()` tick (~20 ms of wall clock in production), drains one 20 ms
chunk from each FIFO -- substituting silence for a starved track -- writes
each track's chunk to that track's own `SegmentedWavWriter`, sums the two
(int32 accumulate, clip to int16) and writes the mix to the combined
writer. Silence substitution keeps all three tracks wall-clock continuous
and mutually aligned even when a track (e.g. WASAPI loopback with nothing
playing) delivers no frames for a while -- which Phase 2's timestamp-based
energy attribution depends on.

Injection seams for tests (the BotManager `audio_source_factory` pattern,
adapted):
  - `capture_factory(device_id, samplerate) -> iterator of (np.ndarray shape
    (n, ch) float32, samplerate)`, blocking. Default wraps `soundcard`
    (imported lazily -- never at module level, so this module stays
    importable and testable without the optional `[live]` extra installed).
  - `ticker() -> iterator`, yielding once per tick. Default is a real
    20 ms wall-clock ticker; tests inject a finite instant generator so the
    manager can be exercised without any real-time delay ("media time not
    wall time" -- same lesson as the R12 combined-track mixer fix in
    discord_bot.py).

One capture *session* at a time, enforced the same way BotManager does:
`start_session` raises `RuntimeError` if the current `active_recording` is
still `"recording"`/`"degraded"`. Cross-manager (Discord vs local)
exclusion is enforced at the route layer (web/routes/record.py), not here.

`_active_recording` is deliberately never reset to `None` after a session
finishes -- it stays set to the now-`"completed"` Recording, exactly like
BotManager. Callers that need "is a session currently active" must check
`.status in {"recording", "degraded"}`, not `is not None`.
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
    save_recording_merged,
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
# Public: web/routes/record.py imports this for the cross-manager (Discord vs
# local) mutual-exclusion check -- "is a session actually active" is
# `.status in ACTIVE_STATUSES`, never `active_recording is not None` (see the
# module docstring above).
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
    """Default `capture_factory`: opens a soundcard device and blocks on `record()`.

    Never imported at module level -- `soundcard` is an optional `[live]`
    extra and this module must stay importable (and, for tests, fully
    exercisable via an injected `capture_factory`) without it installed.
    Applies the documented Windows COM-init threading caveat: this runs
    inside the calling capture thread (it's a generator, so its body -
    including CoInitialize - executes on whatever thread iterates it), and
    uninitializes on exit.
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
    """Return `{"microphones": [...], "loopbacks": [...], "available": bool,
    "default_microphone_id": str, "default_loopback_id": str}`.

    Each device entry is `{"id": str, "name": str}`. `available: False`
    (never an exception) whenever `soundcard` is not importable or
    enumeration otherwise fails -- callers (`GET /api/record/devices`, the
    Record page) use this to hide the Local section entirely rather than
    surfacing a 500. The two `default_*_id` fields echo the OS's current
    default recording / playback device (empty string if unavailable or the
    lookup itself fails) so the picker can pre-select them instead of
    defaulting to the first device in an arbitrary enumeration order.
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
    file path, so this is safe -- see CLAUDE.md's web route security rules)."""
    for d in devices:
        if d.get("id") == device_id:
            return d.get("name", device_id)
    return device_id


# ---------------------------------------------------------------------------
# LocalCaptureManager
# ---------------------------------------------------------------------------

class LocalCaptureManager:
    """Manages one local mic+system-audio capture session at a time.

    Mirrors `BotManager`'s start()/stop()/start_session()/stop_session()
    shape for `app.py` lifespan wiring, but every method here is
    synchronous -- capture is thread-based, not asyncio. Callers on an
    asyncio event loop (routes, lifespan) must wrap the blocking calls
    (`stop_session()`, `stop()`) in `asyncio.to_thread()`.
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
        # Phase 2: optional live-transcription tap, called from the tick
        # thread with (mic_bytes, system_bytes, mixed_bytes) once per tick,
        # in addition to (never instead of) the disk writes above. Must be
        # fast/non-blocking -- it runs on the hot tick-thread path.
        self._live_sink: Optional[Callable[[bytes, bytes, bytes], None]] = None
        # Live level meter (Record page noise-floor gauge): peak per-track
        # RMS observed since the last `get_and_reset_levels()` read, not an
        # instantaneous snapshot -- the reader (an ~1s SSE poll) would
        # otherwise miss short transients between polls. Plain dict +
        # lock, read from the asyncio event loop thread while written from
        # the tick thread; no producer/consumer ordering requirement beyond
        # "don't tear the dict read", so a simple lock is enough (mirrors
        # `_live_sink`'s read-without-lock tolerance for a single float
        # would be fine too, but two related values need to stay in sync).
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
        """Register (or clear, with `None`) the Phase 2 live-transcription
        tap. Safe to call at any time -- reads of `self._live_sink` in the
        tick thread just see whatever was last set."""
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
        """Create a Recording and start the two capture threads + tick thread.

        `mic_name`/`system_name` are the *resolved* device display names
        (looked up by the caller via `enumerate_devices()` +
        `resolve_device_name()`), stored in `Recording.devices` for the
        detail page -- never a client-supplied free-text string. Falls back
        to the raw device id if no name is given. `name` is the opposite:
        a client-supplied free-text session title, display-only (never used
        in a file path -- `Recording.id`, a server-generated uuid4, is what
        backs the on-disk directory), so no server-side resolution needed.
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

        self._active_recording = recording
        log.info("Local capture session started: %s", recording.id)
        return recording

    def stop_session(self) -> None:
        """Signal capture/tick threads to stop, join them, and finalise. Blocking.

        The tick thread is the sole owner of writing; this method is the
        sole owner of finalising (closing writers + concatenating the
        combined track) -- the tick thread itself only ever exits its loop,
        it never finalises, so there is exactly one finalise per session
        regardless of whether the ticker is finite (tests) or infinite
        (production, where only `stop_event` ends it).
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
        """Flag the active recording as degraded when a capture thread dies
        unexpectedly (e.g. a USB mic/loopback device unplugged mid-session).

        Without this, ACTIVE_STATUSES's inclusion of "degraded" is
        misleading -- nothing ever set it for local capture -- and a dead
        track silently keeps recording silence-substituted audio with no
        visible signal until the user reviews the resulting file. Never
        raises: a bookkeeping failure here must not take down the tick
        thread, which keeps writing (silence for the dead track) regardless.
        """
        recording = self._active_recording
        if recording is None or recording.status != "recording":
            return
        try:
            recording.status = "degraded"
            save_recording_merged(recording, self._data_dir)
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

        save_recording_merged(recording, self._data_dir)
        if became_completed:
            log.info("Local recording %s finalised as completed", recording.id)
