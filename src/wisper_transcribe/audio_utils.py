import subprocess
import tempfile
from pathlib import Path

from pydub import AudioSegment

# Audio-only formats handled by pydub.
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}

# Video container formats — audio is extracted via ffmpeg with explicit
# stream mapping so only the first audio track is used.
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm",
                    ".flv", ".ts", ".mts", ".m2ts"}

SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


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


def _extract_first_audio_track(video_path: Path) -> Path:
    """Extract the first audio stream from a video file as a 16kHz mono WAV.

    Uses ffmpeg with ``-map 0:a:0`` so only stream 0 is taken regardless of
    how many audio tracks the container holds.  Raises ValueError on ffmpeg
    failure (e.g. no audio track) and RuntimeError if ffmpeg is not on PATH.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    out_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-map", "0:a:0",   # first audio stream only
                "-ac", "1",        # mono
                "-ar", "16000",    # 16 kHz
                "-vn",             # drop video stream
                str(out_path),
            ],
            capture_output=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg not found. Install it to process video files: "
            "https://ffmpeg.org/download.html"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-400:]
        raise ValueError(
            f"ffmpeg could not extract audio from {video_path.name!r}. "
            f"Does the file have an audio track?\nffmpeg: {stderr}"
        )

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
