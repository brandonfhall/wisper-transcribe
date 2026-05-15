import re as _re
import subprocess
import tempfile
import threading
from pathlib import Path

from pydub import AudioSegment

# Audio-only formats handled by pydub.
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".m4b", ".flac", ".ogg"}

# Video container formats — audio is extracted via ffmpeg with explicit
# stream mapping so only the first audio track is used.
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm",
                    ".flv", ".ts", ".mts", ".m2ts"}

SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

_OUT_TIME_RE = _re.compile(r'^out_time=(\d+):(\d+):(\d+\.\d+)$')


def validate_audio(path: Path) -> None:
    """Raise ValueError if the file doesn't exist or has an unsupported format."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Audio file not found: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported format '{path.suffix}'. "
            f"Supported audio: {', '.join(sorted(AUDIO_EXTENSIONS))}. "
            f"Supported video: {', '.join(sorted(VIDEO_EXTENSIONS))}."
        )


def _probe_duration(video_path: Path) -> float | None:
    """Return video duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _extract_first_audio_track(video_path: Path) -> Path:
    """Extract the first audio stream from a video file as a 16kHz mono WAV.

    Streams ffmpeg's ``-progress pipe:1`` output to drive a tqdm progress bar
    so both the CLI terminal and the web job log show extraction progress and
    ETA.  Uses ``-map 0:a:0`` so only the primary audio track is extracted
    regardless of how many tracks the container holds.
    """
    from tqdm import tqdm as _tqdm

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    out_path = Path(tmp.name)

    total_seconds = _probe_duration(video_path)

    _tqdm.write(f"  Extracting audio from {video_path.name!r}…")

    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-map", "0:a:0",
                "-ac", "1",
                "-ar", "16000",
                "-vn",
                "-progress", "pipe:1",  # structured progress → stdout
                "-nostats",             # suppress stderr progress lines
                str(out_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg not found. Install it to process video files: "
            "https://ffmpeg.org/download.html"
        ) from exc

    # Drain stderr in the background so the pipe never blocks.
    stderr_lines: list[str] = []

    def _drain() -> None:
        for line in proc.stderr:
            stderr_lines.append(line.decode(errors="replace").rstrip())

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    # Drive a tqdm bar from ffmpeg's structured progress output.
    bar_kw: dict = dict(
        desc="Extracting audio",
        unit="%",
        bar_format="{desc}: {percentage:3.0f}%|{bar}| [{elapsed}<{remaining}]",
    )
    if total_seconds:
        pbar = _tqdm(total=100, **bar_kw)
    else:
        # Duration unknown — show time elapsed without a percentage.
        pbar = _tqdm(total=None, desc="Extracting audio",
                     bar_format="{desc}: {elapsed} elapsed")

    last_pct = 0
    try:
        for raw in proc.stdout:
            m = _OUT_TIME_RE.match(raw.decode(errors="replace").strip())
            if m and total_seconds:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                elapsed_s = h * 3600 + mn * 60 + s
                pct = min(int(elapsed_s / total_seconds * 100), 99)
                if pct > last_pct:
                    pbar.update(pct - last_pct)
                    last_pct = pct
        if total_seconds:
            pbar.update(100 - last_pct)
    finally:
        pbar.close()

    proc.wait()
    drain_thread.join(timeout=5)

    if proc.returncode != 0:
        stderr_tail = "\n".join(stderr_lines[-10:])
        raise ValueError(
            f"ffmpeg could not extract audio from {video_path.name!r}. "
            f"Does the file have an audio track?\nffmpeg: {stderr_tail}"
        )

    _tqdm.write("  Audio extraction complete.")
    return out_path


def convert_to_wav(path: Path) -> Path:
    """Convert an audio or video file to a 16kHz mono WAV.

    Video files: ffmpeg extracts only the first audio track (``-map 0:a:0``).
    Audio files: pydub handles conversion; already-correct WAVs are returned
    unchanged.
    """
    path = Path(path)

    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return _extract_first_audio_track(path)

    audio = AudioSegment.from_file(str(path))

    if (
        path.suffix.lower() == ".wav"
        and audio.frame_rate == 16000
        and audio.channels == 1
    ):
        return path

    audio = audio.set_frame_rate(16000).set_channels(1)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    out_path = Path(tmp.name)
    audio.export(str(out_path), format="wav")
    return out_path


def get_duration(path: Path) -> float:
    """Return audio duration in seconds."""
    audio = AudioSegment.from_file(str(path))
    return len(audio) / 1000.0


def load_wav_as_tensor(path: Path) -> dict:
    """Read a WAV file and return a ``{waveform, sample_rate}`` dict for pyannote.

    Handles mono/stereo layout and int→float32 normalisation so that callers
    (diarizer, speaker_manager) don't each have to reimplement the same logic.
    The returned waveform is a ``torch.Tensor`` with shape ``(channels, time)``.
    """
    import numpy as np
    import scipy.io.wavfile as _wavfile
    import torch

    sample_rate, data = _wavfile.read(str(path))
    if data.ndim == 1:
        data = data[np.newaxis, :]          # (time,) → (1, time)
    else:
        data = data.T                        # (time, ch) → (ch, time)
    if np.issubdtype(data.dtype, np.integer):
        data = data.astype(np.float32) / np.iinfo(data.dtype).max
    waveform = torch.from_numpy(data.copy())
    return {"waveform": waveform, "sample_rate": sample_rate}
