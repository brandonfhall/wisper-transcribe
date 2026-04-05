from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from .audio_utils import convert_to_wav, get_duration, validate_audio
from .config import check_ffmpeg, get_device, load_config
from .formatter import to_markdown
from .models import TranscriptionSegment
from .transcriber import transcribe


def _seconds_to_hhmmss(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"


def process_file(
    path: Path,
    output_dir: Optional[Path] = None,
    model_size: str = "medium",
    device: str = "auto",
    language: Optional[str] = "en",
    include_timestamps: bool = True,
    overwrite: bool = False,
) -> Path:
    """Run the full pipeline on a single audio file. Returns path to output .md."""
    path = Path(path)
    config = load_config()

    if device == "auto":
        device = get_device()
    if model_size == "medium":
        model_size = config.get("model", "medium")
    if language == "en":
        language = config.get("language", "en")

    check_ffmpeg()
    validate_audio(path)

    out_dir = Path(output_dir) if output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (path.stem + ".md")

    if out_path.exists() and not overwrite:
        print(f"  Skipping {path.name} (output already exists, use --overwrite to force)")
        return out_path

    print(f"  Transcribing {path.name}...")
    wav_path = convert_to_wav(path)

    segments: list[TranscriptionSegment] = transcribe(
        wav_path,
        model_size=model_size,
        device=device,
        language=language,
    )

    duration = get_duration(wav_path)

    metadata = {
        "title": path.stem.replace("_", " ").replace("-", " ").title(),
        "source_file": path.name,
        "date_processed": datetime.date.today().isoformat(),
        "duration": _seconds_to_hhmmss(duration),
        "speakers": [],
    }

    content = to_markdown(
        segments,
        speaker_map=None,  # Phase 1: no diarization
        metadata=metadata,
        include_timestamps=include_timestamps,
    )

    out_path.write_text(content, encoding="utf-8")
    print(f"  Wrote {out_path.name}")
    return out_path
