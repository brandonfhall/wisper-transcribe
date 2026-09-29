"""Near-real-time transcription for local capture sessions.

Accumulates the tick thread's PCM in a ``LiveRingBuffer`` until a VAD silence
gap (>= SILENCE_GAP_S) or FORCE_CUT_S, transcribes the chunk with the shared
faster-whisper model cache (safe: JOB_LIVE holds the only worker slot), labels
each line "You" (mic) or "Other" (system) by per-track RMS, and passes the
``LiveLine``s to a callback.

No diarization here; it is too heavy per chunk. The post-session Transcribe
pass is the authoritative transcript.
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

# int16 RMS below which a track counts as silent. Silero VAD can flag room tone
# as speech and Whisper hallucinates text over it. Real speech measures well
# above this on a USB condenser mic; tune per mic via the Record page slider.
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

    ``push()`` runs on the capture tick thread and must stay cheap;
    ``snapshot()``/``drop_prefix()`` run on the live loop's thread. The three
    tracks stay byte-aligned because the tick thread always pushes
    equal-length chunks.
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

# Tail length scanned by find_commit_boundary (see its docstring for why
# this bound is safe).
_VAD_WINDOW_S = SILENCE_GAP_S + 2 * POLL_INTERVAL_S + 0.5


def find_commit_boundary(mixed_i16: np.ndarray) -> Optional[int]:
    """Return the sample index to cut the buffer at, or None if not ready.

    Cuts at the end of the last speech region once SILENCE_GAP_S of silence
    follows it (so a cut never lands mid-word), or force-cuts at FORCE_CUT_S to
    bound latency during continuous speech. A chunk with no speech is cut but
    not transcribed.

    Only the last ``_VAD_WINDOW_S`` is scanned, avoiding O(n²) rescans. This
    is safe: the loop polls every POLL_INTERVAL_S, so trailing silence never
    exceeds SILENCE_GAP_S + POLL_INTERVAL_S before a commit, and the end of
    the last speech region is always inside the window.
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
    """Return ``mic_label`` or ``other_label`` by RMS energy, or None if
    neither track clears ``noise_floor`` (the caller drops the segment).

    ``mic_label`` defaults to "You" and becomes the profile name with
    "This is me". Ties go to ``mic_label``: showing your own words as the
    other party would be more misleading.
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

    Uses ``transcriber._model`` directly (no temp file, no progress bar);
    safe because JOB_LIVE holds the only worker slot. ``vad_filter`` is off
    since the chunk was already VAD-gated.
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
    """Commit and transcribe chunks from the ring buffer until ``stop_event`` is set.

    Backpressure is bounded: if transcription falls behind, the buffer grows
    and the next ``find_commit_boundary`` force-cuts at FORCE_CUT_S, so the
    backlog never exceeds one chunk.

    A failing chunk is logged, reported via ``on_warning``, and skipped; it
    never ends the session.

    ``initial_prompt`` is never chained from earlier output; chaining sends
    Whisper into repetition loops on real speech.

    ``get_noise_floor`` is read every iteration so the Record page slider
    applies to the next chunk.
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
                # Generic message only; the traceback is in the server log.
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
