from __future__ import annotations

from pathlib import Path
from typing import Optional

from pyannote.audio import Pipeline

from .models import DiarizationSegment

_pipeline = None


def load_pipeline(hf_token: str, device: str):
    """Load pyannote speaker-diarization-3.1, cache module-level."""
    global _pipeline

    _pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    _pipeline.to(device)  # type: ignore[arg-type]
    return _pipeline


def diarize(
    audio_path: Path,
    hf_token: str,
    device: str,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
) -> list[DiarizationSegment]:
    """Run speaker diarization and return labeled time segments."""
    global _pipeline

    if _pipeline is None:
        load_pipeline(hf_token, device)

    kwargs: dict = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    else:
        if min_speakers is not None:
            kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            kwargs["max_speakers"] = max_speakers

    diarization = _pipeline(str(audio_path), **kwargs)

    segments: list[DiarizationSegment] = []
    for turn, _track, speaker in diarization.itertracks(yield_label=True):
        segments.append(
            DiarizationSegment(
                start=turn.start,
                end=turn.end,
                speaker=speaker,
            )
        )
    return segments
