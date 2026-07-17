"""Segmented WAV audio writer + PCM downsampling for Discord recordings.

Writes 16 kHz mono 16-bit PCM as a sequence of self-contained WAV files,
each capped at `segment_duration_s` seconds (default 60), using the stdlib
`wave` module.

Crash safety: `wave.Wave_write.writeframes()` patches the RIFF/`data` chunk
sizes in the file's header on every call after the first (see cpython's
`wave.py` — `_patchheader()`), so a segment's header always reflects
whatever has actually been written through Python's file buffer. We also
`flush()` after every `write()` call to push that header patch + PCM data
out of the Python-level buffer, and `fsync()` on segment close/finalize.
A hard process crash mid-segment can therefore leave the *current* segment
mildly short of the last few frames, but its header and data length always
agree, so `wave`/anything else can open and play it — no corruption, only
a possibly-truncated tail. All previously-rotated segments are already
closed and fully valid. This replaces the old hand-rolled Ogg/Opus muxer
(`SegmentedOggWriter`), which wrote raw 48 kHz stereo PCM frames into Ogg
pages as though they were pre-encoded Opus packets — producing files that
no Opus decoder could read (see senior-review finding R12).
"""
from __future__ import annotations

import logging
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

    JDA delivers raw 48 kHz stereo 16-bit PCM (both per-user and the
    pre-mixed `__mixed__` track); everything downstream (embedding
    extraction, `convert_to_wav`) wants 16 kHz mono, so we downsample at
    write time rather than storing ~6x more data than needed.

    Pure NumPy/stdlib (Python 3.13 removed `audioop`): average L+R to mono,
    apply a cheap 3-tap moving-average low-pass as anti-aliasing, then
    decimate by 3 (48000 / 16000 = 3). This is not broadcast-quality
    resampling, but it's more than adequate for speech destined for
    Whisper/pyannote, both of which resample to 16 kHz internally anyway.

    A full 20 ms frame at 48 kHz (960 stereo samples) downsamples to
    exactly 320 mono samples (20 ms at 16 kHz) — one incoming frame maps to
    one 20 ms chunk of output, preserving wall-clock duration exactly.
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
        """Write one chunk of 16 kHz mono 16-bit PCM. Rotates if the current
        segment has reached its media-time duration cap.

        Rotation is based on sample count (media time), not wall-clock
        time, so tests that feed frames faster than real-time work
        correctly. Returns the path of the completed segment if rotation
        occurred, else None.
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
# Segment concatenation (R2: combined-track finalisation)
# ---------------------------------------------------------------------------

def concat_wav_segments(segments_dir: Path, out_path: Path) -> Optional[Path]:
    """Concatenate rotated WAV segments in `segments_dir` into one WAV file.

    All segments share the same params (written by the same
    `SegmentedWavWriter`), so this is a plain frame-concatenation via the
    stdlib `wave` module — no re-encoding needed.

    Unreadable/corrupt segments (e.g. a zero-byte segment left behind by a
    crash before any frame was written) are skipped with a warning rather
    than aborting the whole merge, so a crash loses at most its own
    segment's tail.

    Returns `out_path` on success, or `None` if there were no segments (or
    none were readable) — callers should leave `Recording.combined_path`
    unset in that case so the existing "no audio" state stays reportable.
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
