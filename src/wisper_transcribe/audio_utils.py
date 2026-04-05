import tempfile
from pathlib import Path

from pydub import AudioSegment

SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4"}


def validate_audio(path: Path) -> None:
    """Raise ValueError if the file doesn't exist or has an unsupported format."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"Audio file not found: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported audio format '{path.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )


def convert_to_wav(path: Path) -> Path:
    """Convert audio to 16kHz mono WAV using pydub. Returns a temp file path.

    If the file is already a 16kHz mono WAV, returns the original path unchanged.
    """
    path = Path(path)
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
