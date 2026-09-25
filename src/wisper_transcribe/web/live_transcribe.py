"""Live (near-real-time) transcription for local capture sessions — Phase 2.

Rolling-window transcription of the mixed 16 kHz mono stream produced by
`LocalCaptureManager`'s tick thread: accumulate PCM in a `LiveRingBuffer`
until a VAD silence gap (>= SILENCE_GAP_S) or a hard duration cap
(FORCE_CUT_S) is reached, transcribe the committed chunk with the existing
faster-whisper model (module-level cache shared with regular transcription
jobs -- the JOB_LIVE job holds the JobQueue's single worker slot for its
duration, so there is never a concurrent transcription job to race with),
attribute each resulting line to "You" (mic) or "Other" (system) by
comparing per-track RMS energy over the line's span, and hand the
resulting `LiveLine`s to a caller-supplied callback.

No pyannote in this path -- diarization is too heavy to run per few-second
chunk. The post-session full pipeline pass (unchanged) still runs real
diarization + speaker ID once the recording is handed off to
`POST /recordings/{id}/transcribe`; this is a crash-safety/near-real-time
preview only.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000
BYTES_PER_SAMPLE = 2
SILENCE_GAP_S = 0.6
FORCE_CUT_S = 15.0
MIN_COMMIT_S = 0.3           # skip transcribing sub-300ms scraps
POLL_INTERVAL_S = 0.25       # how often the live loop checks the ring buffer

# int16 RMS floor below which a track counts as "nothing happening" rather
# than a real (if quiet) voice. Silero VAD's speech/silence split (used to
# decide chunk boundaries) can still flag room tone / mic self-noise as
# "speech", and faster-whisper tends to hallucinate plausible-sounding text
# on that kind of near-silent audio rather than returning nothing -- this
# is a coarse starting point (real speech observed well above this on a
# USB condenser mic; true silence measures exactly 0.0) and may need
# tuning for a given mic's gain/self-noise level. Raised 150 -> 300
# 2026-08-16 after field use: 150 still let some mic self-noise through.
NOISE_FLOOR_RMS = 300.0


@dataclass
class LiveLine:
    speaker: str          # "You" | "Other"
    text: str
    start_s: float         # seconds since session start
    end_s: float

    def to_dict(self) -> dict:
        return {
            "speaker": self.speaker,
            "text": self.text,
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
        }


# ---------------------------------------------------------------------------
# LiveRingBuffer -- fed one tick (20 ms) at a time by the capture tick thread
# ---------------------------------------------------------------------------

class LiveRingBuffer:
    """Thread-safe accumulator of mic/system/mixed int16 PCM.

    `push()` is called from `LocalCaptureManager`'s tick thread (the hot
    path -- keep it O(tick size), never blocking); `snapshot()` /
    `drop_prefix()` are called from the live-transcription loop's own
    thread. All three tracks stay byte-aligned with each other at all
    times -- `push()` always receives equal-length chunks (LocalCaptureManager
    pads a starved track to match, same invariant Phase 1's combined mixer
    relies on).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mic = bytearray()
        self._system = bytearray()
        self._mixed = bytearray()
        self._elapsed_s = 0.0        # media time at the END of the buffered audio

    def push(self, mic: bytes, system: bytes, mixed: bytes) -> None:
        if not mixed:
            return
        with self._lock:
            self._mic.extend(mic)
            self._system.extend(system)
            self._mixed.extend(mixed)
            self._elapsed_s += len(mixed) / BYTES_PER_SAMPLE / RATE

    def snapshot(self) -> tuple[bytes, bytes, bytes, float]:
        """Copy of everything buffered so far, plus elapsed media time at
        the end of that buffer (for stamping committed lines)."""
        with self._lock:
            return bytes(self._mic), bytes(self._system), bytes(self._mixed), self._elapsed_s

    def drop_prefix(self, n_bytes: int) -> None:
        """Discard the first n_bytes of all three tracks after a commit."""
        if n_bytes <= 0:
            return
        with self._lock:
            del self._mic[:n_bytes]
            del self._system[:n_bytes]
            del self._mixed[:n_bytes]

    def __len__(self) -> int:
        with self._lock:
            return len(self._mixed)


# ---------------------------------------------------------------------------
# Chunk-cut decision
# ---------------------------------------------------------------------------

# How much of the buffer's tail actually needs scanning to make the
# trailing-silence decision below -- see find_commit_boundary's docstring
# for why a bounded window is sufficient (not just an optimization that
# happens to usually work).
_VAD_WINDOW_S = SILENCE_GAP_S + 2 * POLL_INTERVAL_S + 0.5


def find_commit_boundary(mixed_i16: np.ndarray) -> Optional[int]:
    """Return the sample index to cut the buffer at, or None if not ready.

    Cuts at the end of the last detected speech region once at least
    SILENCE_GAP_S of trailing silence follows it (so a chunk boundary
    doesn't land mid-word), or force-cuts at FORCE_CUT_S regardless of VAD
    state -- bounds worst-case latency and handles continuous speech that
    never pauses. A chunk with no speech detected at all is force-cut but
    the caller skips transcribing it (nothing to transcribe).

    Only scans the last `_VAD_WINDOW_S` seconds of the buffer, not the
    whole (up to FORCE_CUT_S-long) thing -- rescanning already-analyzed
    early audio on every ~250ms poll would be an O(n^2) cost across a
    session otherwise. This is sound, not just a shortcut that usually
    works: `run_live_loop` calls this every POLL_INTERVAL_S, and trailing
    silence measured against the (growing) buffer end only ever increases
    between polls, so the buffer commits within about one poll interval of
    crossing SILENCE_GAP_S -- it never accumulates more than
    SILENCE_GAP_S + POLL_INTERVAL_S of trailing silence before being
    cleared. The last speech region's END is therefore always within the
    window; only its START (which this function never uses) could fall
    outside it, e.g. for one long region that's still ongoing.
    """
    total_s = len(mixed_i16) / RATE
    if total_s < MIN_COMMIT_S:
        return None
    if total_s >= FORCE_CUT_S:
        return len(mixed_i16)

    from faster_whisper.vad import VadOptions, get_speech_timestamps

    window_samples = int(_VAD_WINDOW_S * RATE)
    offset = max(0, len(mixed_i16) - window_samples)
    tail_f32 = mixed_i16[offset:].astype(np.float32) / 32768.0
    timestamps = get_speech_timestamps(
        tail_f32, vad_options=VadOptions(min_silence_duration_ms=300)
    )
    if not timestamps:
        return None
    last_speech_end = offset + timestamps[-1]["end"]
    trailing_silence_s = (len(mixed_i16) - last_speech_end) / RATE
    if trailing_silence_s >= SILENCE_GAP_S:
        return last_speech_end
    return None


# ---------------------------------------------------------------------------
# Speaker attribution -- per-track RMS energy over a line's span
# ---------------------------------------------------------------------------

def _rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def attribute_speaker(
    mic_span: np.ndarray,
    system_span: np.ndarray,
    mic_label: str = "You",
    other_label: str = "Other",
    noise_floor: float = NOISE_FLOOR_RMS,
) -> Optional[str]:
    """`mic_label` (mic dominant) or `other_label` (system dominant) by RMS
    energy, or `None` when *neither* track clears `noise_floor` -- nothing
    is really happening on either side, so the caller should drop the
    segment rather than mislabel background noise as a real voice. Defaults
    to "You"/"Other"; a session started with a "this is me" enrolled-profile
    selection passes the profile's display_name as `mic_label` instead
    (Phase 3) -- purely cosmetic, doesn't touch the energy-comparison logic
    itself.

    Ties among two tracks that both clear the floor resolve to `mic_label`
    -- a marginal call either way, but false attribution to `other_label`
    would be more misleading (words from your own mic showing up unlabeled
    as the other party).
    """
    mic_rms = _rms(mic_span)
    system_rms = _rms(system_span)
    if mic_rms < noise_floor and system_rms < noise_floor:
        return None
    return mic_label if mic_rms >= system_rms else other_label


# ---------------------------------------------------------------------------
# Transcription -- reuses transcriber.py's module-level model cache
# ---------------------------------------------------------------------------

def transcribe_array(
    audio_f32: np.ndarray,
    model_size: str = "base",
    device: str = "auto",
    compute_type: str = "auto",
    language: Optional[str] = "en",
    initial_prompt: Optional[str] = None,
) -> list:
    """Transcribe an in-memory float32 mono 16 kHz array.

    Reuses `transcriber.py`'s module-level `_model` cache directly (no temp
    file, no tqdm progress-bar UI -- not useful for a sub-15s live chunk).
    Safe to share that cache because JOB_LIVE holds the JobQueue's single
    worker slot for the whole session; no other job can be transcribing
    concurrently. `vad_filter` is off here -- `find_commit_boundary()`
    already VAD-gated the chunk before it was committed.
    """
    from wisper_transcribe import transcriber as _transcriber
    from wisper_transcribe.config import get_device

    if device == "auto":
        device = get_device()

    if _transcriber._model is None or _transcriber._model_key != (model_size, device, compute_type):
        _transcriber.load_model(model_size, device, compute_type)

    segments, _info = _transcriber._model.transcribe(
        audio_f32,
        language=language if language else None,
        beam_size=5,
        vad_filter=False,
        initial_prompt=initial_prompt,
        word_timestamps=False,
    )

    from wisper_transcribe.models import TranscriptionSegment

    result = []
    for seg in segments:
        if seg.text.strip():
            result.append(TranscriptionSegment(start=seg.start, end=seg.end, text=seg.text.strip()))
    return result


def commit_and_transcribe(
    mic_bytes: bytes,
    system_bytes: bytes,
    mixed_bytes: bytes,
    chunk_start_s: float,
    model_size: str = "base",
    device: str = "auto",
    compute_type: str = "auto",
    language: Optional[str] = "en",
    initial_prompt: Optional[str] = None,
    mic_label: str = "You",
    other_label: str = "Other",
    noise_floor: float = NOISE_FLOOR_RMS,
) -> list[LiveLine]:
    """Transcribe one committed chunk, attributing each resulting whisper
    segment to a LiveLine. A chunk can yield zero, one, or several lines
    (whisper may split a chunk that spans an internal pause into more than
    one segment).
    """
    mixed_i16 = np.frombuffer(mixed_bytes, dtype="<i2")
    if len(mixed_i16) == 0:
        return []
    mixed_f32 = mixed_i16.astype(np.float32) / 32768.0

    segments = transcribe_array(
        mixed_f32, model_size=model_size, device=device, compute_type=compute_type,
        language=language, initial_prompt=initial_prompt,
    )
    if not segments:
        return []

    mic_i16 = np.frombuffer(mic_bytes, dtype="<i2")
    system_i16 = np.frombuffer(system_bytes, dtype="<i2")

    lines = []
    for seg in segments:
        start_sample = max(0, int(seg.start * RATE))
        end_sample = min(len(mic_i16), int(seg.end * RATE))
        mic_span = mic_i16[start_sample:end_sample]
        system_span = system_i16[start_sample:end_sample]
        speaker = attribute_speaker(
            mic_span, system_span, mic_label=mic_label, other_label=other_label,
            noise_floor=noise_floor,
        )
        if speaker is None:
            # Neither track cleared the noise floor -- Whisper hallucinated
            # text over what was actually just room tone / mic self-noise.
            # Drop it rather than mislabeling it under mic_label.
            continue
        lines.append(LiveLine(
            speaker=speaker,
            text=seg.text,
            start_s=chunk_start_s + seg.start,
            end_s=chunk_start_s + seg.end,
        ))
    return lines


# ---------------------------------------------------------------------------
# The live loop -- runs in the JOB_LIVE job's worker thread
# ---------------------------------------------------------------------------

def run_live_loop(
    ring_buffer: LiveRingBuffer,
    stop_event: threading.Event,
    on_line: Callable[[LiveLine], None],
    model_size: str = "base",
    device: str = "auto",
    compute_type: str = "auto",
    language: Optional[str] = "en",
    initial_prompt: Optional[str] = None,
    mic_label: str = "You",
    poll_interval_s: float = POLL_INTERVAL_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    get_noise_floor: Callable[[], float] = lambda: NOISE_FLOOR_RMS,
    on_warning: Optional[Callable[[str], None]] = None,
) -> None:
    """Poll the ring buffer, commit + transcribe chunks, call `on_line` for
    each resulting line, until `stop_event` is set.

    Backpressure: if a single call to `commit_and_transcribe` takes longer
    than the audio it covers (falling behind capture -- e.g. a slow CPU
    model choice), the ring buffer simply keeps growing during that call;
    the *next* iteration's `find_commit_boundary` naturally force-cuts at
    FORCE_CUT_S regardless of how much has piled up, so backlog is bounded
    to at most one FORCE_CUT_S-sized chunk of latency rather than growing
    unboundedly -- there is no separate queue of pending windows to merge
    or skip.

    A per-chunk transcription exception is logged and skipped rather than
    ending the loop -- a single flaky chunk must not kill an hours-long
    live session.

    `initial_prompt` is passed through unchanged to every chunk -- it is
    NOT chained from the previous chunk's committed text. That chaining was
    tried (see plan.md's original open decision) and confirmed on real
    speech to send the model into repetition loops ("column column column
    column...") that got worse chunk over chunk as each hallucinated
    repeat re-primed the next prompt, occasionally producing out-of-order
    segment timestamps along with it. Dropped per the decision's own
    documented fallback.

    `get_noise_floor` is called fresh at the top of every iteration (not
    just once at loop start) so a caller can adjust sensitivity live during
    a session -- e.g. the Record page's noise-floor slider mutates
    `job.kwargs["noise_floor"]` and `jobs._run_live_job` wires a getter
    that reads it back out on each call, mirroring how `stop_event` already
    lets the route layer signal a running job rather than baking
    everything into fixed arguments at submit time.
    """
    while not stop_event.is_set():
        mic, system, mixed, elapsed_s = ring_buffer.snapshot()
        mixed_i16 = np.frombuffer(mixed, dtype="<i2")
        cut = find_commit_boundary(mixed_i16)
        if not cut:  # None (not ready) or 0 (degenerate) -- nothing to commit yet
            sleep_fn(poll_interval_s)
            continue

        cut_bytes = cut * BYTES_PER_SAMPLE
        chunk_start_s = elapsed_s - (len(mixed_i16) / RATE)

        try:
            lines = commit_and_transcribe(
                mic[:cut_bytes], system[:cut_bytes], mixed[:cut_bytes],
                chunk_start_s=chunk_start_s,
                model_size=model_size, device=device, compute_type=compute_type,
                language=language, initial_prompt=initial_prompt, mic_label=mic_label,
                noise_floor=get_noise_floor(),
            )
        except Exception:
            log.warning("Live transcription chunk failed; skipping", exc_info=True)
            if on_warning is not None:
                # R13: no raw exception text in a callback that renders into
                # job log / SSE -- the server log (exc_info=True above) has
                # the detail; this just tells the user something was skipped.
                on_warning("Live transcription chunk failed; skipping (see server log)")
            lines = []

        for line in lines:
            try:
                on_line(line)
            except Exception:
                log.warning("Live transcription on_line callback failed", exc_info=True)
                if on_warning is not None:
                    on_warning("Live transcription on_line callback failed (see server log)")
        if not lines:
            # A force-cut with no speech at all consumes the buffer below
            # but produces nothing to hand to on_line -- yield briefly so a
            # long silent stretch doesn't spin the loop.
            sleep_fn(poll_interval_s)

        ring_buffer.drop_prefix(cut_bytes)
