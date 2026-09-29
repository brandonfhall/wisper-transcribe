"""Segmented WAV writer and PCM resampling for Discord and local recordings.

Writes 16 kHz mono 16-bit PCM as self-contained WAV segments of at most
``segment_duration_s`` (default 60) via the stdlib ``wave`` module.
``downsample_48k_stereo_to_16k_mono()`` handles Discord's fixed 48 kHz stereo;
``resample_to_16k_mono()`` handles local capture's arbitrary-rate float32.

Crash safety: ``writeframes()`` rewrites the header sizes on every call, and
``write()`` flushes after each call (``fsync`` on close). A crash can lose the
last few frames of the current segment, but its header always matches its
data, so it stays playable. Rotated segments are complete.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PCM downsampling: 48 kHz stereo 16-bit -> 16 kHz mono 16-bit
# ---------------------------------------------------------------------------

def downsample_48k_stereo_to_16k_mono(pcm: bytes) -> bytes:
    """Convert 48 kHz stereo 16-bit PCM to 16 kHz mono 16-bit PCM.

    Done at write time so recordings store ~6x less data. Averages L+R, applies
    a 3-tap moving-average low-pass, and decimates by 3 — pure NumPy (``audioop``
    is gone in 3.13) and adequate for speech that Whisper/pyannote resample to
    16 kHz anyway.

    A 20 ms frame (960 stereo samples) becomes exactly 320 mono samples, so
    duration is preserved.
    """
    if not pcm:
        return b""
    n_frames = len(pcm) // 4  # 2 channels * 2 bytes/sample
    if n_frames == 0:
        return b""

    samples = np.frombuffer(pcm[: n_frames * 4], dtype="<i2").reshape(-1, 2)
    mono = samples.astype(np.float64).mean(axis=1)  # average L+R

    if len(mono) >= 3:
        kernel = np.array([1.0, 1.0, 1.0]) / 3.0
        filtered = np.convolve(mono, kernel, mode="same")
    else:
        filtered = mono

    decimated = np.clip(np.round(filtered[::3]), -32768, 32767).astype("<i2")
    return decimated.tobytes()


def resample_to_16k_mono(data: np.ndarray, samplerate: int) -> bytes:
    """Convert float32 PCM at any sample rate to 16 kHz mono 16-bit PCM.

    Local devices deliver [-1.0, 1.0] floats at their native rate (often
    44.1 kHz, not an integer multiple of 16 kHz), so this uses
    ``scipy.signal.resample_poly``. ``data`` may be (n, channels) or (n,);
    channels are averaged. Samples are scaled to int16 range before clipping;
    without that everything rounds to silence.
    """
    if data is None or len(data) == 0:
        return b""

    mono = data.astype(np.float64).mean(axis=1) if data.ndim == 2 else data.astype(np.float64)
    if len(mono) == 0:
        return b""

    if samplerate == 16000:
        resampled = mono
    else:
        import scipy.signal

        g = math.gcd(16000, samplerate)
        up, down = 16000 // g, samplerate // g
        resampled = scipy.signal.resample_poly(mono, up, down)

    scaled = np.clip(np.round(resampled * 32767.0), -32768, 32767).astype("<i2")
    return scaled.tobytes()


# ---------------------------------------------------------------------------
# SegmentedWavWriter
# ---------------------------------------------------------------------------

class SegmentedWavWriter:
    """Writes 16 kHz mono 16-bit PCM into rotating, self-contained WAV files.

    Thread-safe: write() and finalize() may be called from different
    threads. This writer stores whatever 16 kHz mono 16-bit PCM bytes it
    receives — downsampling from Discord's native 48 kHz stereo happens in
    the caller via `downsample_48k_stereo_to_16k_mono()`.
    """

    RATE = 16000
    CHANNELS = 1
    SAMPWIDTH = 2

    def __init__(
        self,
        stream_dir: Path,
        segment_duration_s: float = 60.0,
    ):
        self._dir = Path(stream_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._duration = segment_duration_s
        self._samples_per_seg = int(self._duration * self.RATE)

        self._lock = threading.Lock()
        # Start at the next index after any existing segments (crash recovery).
        existing = sorted(self._dir.glob("*.wav"))
        self._seg_index = (int(existing[-1].stem) + 1) if existing else 0
        self._seg_samples = 0          # samples written to current segment
        self._fh: Optional[object] = None
        self._wf: Optional[wave.Wave_write] = None

        self._open_segment()

    # ------------------------------------------------------------------
    # Segment file paths
    # ------------------------------------------------------------------

    def _segment_path(self, index: int) -> Path:
        return self._dir / f"{index:04d}.wav"

    def _open_segment(self) -> None:
        path = self._segment_path(self._seg_index)
        self._fh = open(path, "wb")
        self._wf = wave.open(self._fh, "wb")
        self._wf.setnchannels(self.CHANNELS)
        self._wf.setsampwidth(self.SAMPWIDTH)
        self._wf.setframerate(self.RATE)
        self._seg_samples = 0

    def _close_segment(self) -> Path:
        """Patch header, flush, fsync, close. Returns the closed segment's path."""
        path = self._segment_path(self._seg_index)
        self._wf.close()  # patches header sizes if needed
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        self._wf = None
        return path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, pcm_16k_mono: bytes) -> Optional[Path]:
        """Write one chunk of 16 kHz mono 16-bit PCM, rotating at the duration cap.

        Rotation counts samples (media time), not wall-clock time. Returns the
        completed segment's path when a rotation happened, else None.
        """
        if not pcm_16k_mono:
            return None
        with self._lock:
            completed_path: Optional[Path] = None

            if self._seg_samples >= self._samples_per_seg:
                completed_path = self._close_segment()
                self._seg_index += 1
                self._open_segment()

            self._wf.writeframes(pcm_16k_mono)
            self._fh.flush()
            self._seg_samples += len(pcm_16k_mono) // (self.CHANNELS * self.SAMPWIDTH)
            return completed_path

    def finalize(self) -> Path:
        """Close the current segment. Returns its path."""
        with self._lock:
            return self._close_segment()

    @property
    def current_segment_index(self) -> int:
        return self._seg_index

    @property
    def current_segment_path(self) -> Path:
        return self._segment_path(self._seg_index)

    @property
    def stream_dir(self) -> Path:
        return self._dir


# ---------------------------------------------------------------------------
# Segment concatenation
# ---------------------------------------------------------------------------

def concat_wav_segments(segments_dir: Path, out_path: Path) -> Optional[Path]:
    """Concatenate the WAV segments in ``segments_dir`` into one file.

    Segments share params, so frames are concatenated without re-encoding.
    Unreadable or empty segments (e.g. left by a crash) are skipped with a
    warning. Returns ``out_path``, or None if nothing was readable — callers
    then leave ``Recording.combined_path`` unset.
    """
    segments_dir = Path(segments_dir)
    segments = sorted(segments_dir.glob("*.wav"))

    out_wf: Optional[wave.Wave_write] = None
    wrote_any = False
    try:
        for seg_path in segments:
            try:
                with wave.open(str(seg_path), "rb") as in_wf:
                    frames = in_wf.readframes(in_wf.getnframes())
                    if not frames:
                        continue
                    if out_wf is None:
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_wf = wave.open(str(out_path), "wb")
                        out_wf.setnchannels(in_wf.getnchannels())
                        out_wf.setsampwidth(in_wf.getsampwidth())
                        out_wf.setframerate(in_wf.getframerate())
                    out_wf.writeframes(frames)
                    wrote_any = True
            except (wave.Error, EOFError, OSError) as exc:
                log.warning("Skipping unreadable combined-track segment %s: %s", seg_path, exc)
    finally:
        if out_wf is not None:
            out_wf.close()

    if not wrote_any:
        if out_path.exists():
            out_path.unlink()
        return None
    return out_path
