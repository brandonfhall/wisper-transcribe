from pathlib import Path
from typing import Optional

from .models import TranscriptionSegment

_model = None


def load_model(model_size: str, device: str):
    """Load faster-whisper model, caching it module-level."""
    global _model
    from faster_whisper import WhisperModel

    _model = WhisperModel(model_size, device=device, compute_type="float16" if device == "cuda" else "int8")
    return _model


def transcribe(
    audio_path: Path,
    model_size: str = "medium",
    device: str = "auto",
    language: Optional[str] = "en",
) -> list[TranscriptionSegment]:
    """Transcribe audio and return a list of timestamped segments."""
    global _model

    from .config import get_device

    if device == "auto":
        device = get_device()

    if _model is None:
        load_model(model_size, device)

    segments, _info = _model.transcribe(
        str(audio_path),
        language=language if language else None,
        beam_size=5,
    )

    return [
        TranscriptionSegment(start=seg.start, end=seg.end, text=seg.text.strip())
        for seg in segments
        if seg.text.strip()
    ]
