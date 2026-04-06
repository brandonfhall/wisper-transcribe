from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from .audio_utils import SUPPORTED_EXTENSIONS, convert_to_wav, get_duration, validate_audio
from .config import check_ffmpeg, get_device, get_hf_token, load_config
from .formatter import to_markdown
from .models import TranscriptionSegment
from .transcriber import transcribe


def _seconds_to_hhmmss(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"


_MAX_PLAYBACK_SECONDS = 10.0


def _play_excerpt(wav_path: Path, start: float, end: float) -> None:
    """Play up to _MAX_PLAYBACK_SECONDS of a WAV excerpt. Silently no-ops on failure."""
    try:
        from pydub import AudioSegment
        from pydub.playback import play

        clip_start_ms = int(start * 1000)
        clip_end_ms = int(min(end, start + _MAX_PLAYBACK_SECONDS) * 1000)
        clip = AudioSegment.from_wav(str(wav_path))[clip_start_ms:clip_end_ms]
        import click
        click.echo("  [playing audio excerpt...]")
        play(clip)
    except Exception:
        pass  # No audio device or backend available — skip silently


def process_file(
    path: Path,
    output_dir: Optional[Path] = None,
    model_size: str = "medium",
    device: str = "auto",
    language: Optional[str] = "en",
    include_timestamps: bool = True,
    overwrite: bool = False,
    no_diarize: bool = False,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    enroll_speakers: bool = False,
    play_audio: bool = False,
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
        tqdm.write(f"  Skipping {path.name} (output already exists, use --overwrite to force)")
        return out_path

    tqdm.write("")
    tqdm.write("─" * 60)
    tqdm.write(f"  Input  : {path}")
    tqdm.write(f"  Output : {out_path}")
    tqdm.write(f"  Model  : {model_size} ({device})")
    tqdm.write("─" * 60)

    wav_path = convert_to_wav(path)

    segments: list[TranscriptionSegment] = transcribe(
        wav_path,
        model_size=model_size,
        device=device,
        language=language,
    )

    duration = get_duration(wav_path)
    aligned_segments = segments
    speaker_map: Optional[dict[str, str]] = None
    speaker_metadata: list[dict] = []

    if not no_diarize:
        from .aligner import align
        from .diarizer import diarize

        hf_token = get_hf_token(config)
        if hf_token:
            diarization = diarize(
                wav_path,
                hf_token=hf_token,
                device=device,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
            aligned_segments = align(segments, diarization)

            unique_speakers = sorted(
                {seg.speaker for seg in aligned_segments if seg.speaker != "UNKNOWN"},
                key=lambda label: min(s.start for s in aligned_segments if s.speaker == label),
            )

            if enroll_speakers:
                from .speaker_manager import enroll_speaker
                import click
                speaker_map = {}
                click.echo(f"\n  Found {len(unique_speakers)} speaker(s). Let's name them.")
                for i, label in enumerate(unique_speakers, 1):
                    # Show a sample line for this speaker
                    sample = next(
                        (s for s in aligned_segments if s.speaker == label and s.text.strip()),
                        None,
                    )
                    sample_ts = f"{int(sample.start // 60):02d}:{int(sample.start % 60):02d}" if sample else "??"
                    sample_text = sample.text.strip() if sample else "(no sample)"
                    click.echo(f"\n  Speaker {i} of {len(unique_speakers)} (heard at {sample_ts}):")
                    click.echo(f'    "{sample_text[:80]}"')
                    if play_audio and sample:
                        _play_excerpt(wav_path, sample.start, sample.end)
                    name = click.prompt("  Who is this?").strip()
                    role = click.prompt("  Role (DM/Player/Guest, optional)", default="").strip()
                    notes = click.prompt("  Notes (optional)", default="").strip()
                    speaker_map[label] = name
                    enroll_speaker(
                        name=name.lower().replace(" ", "_"),
                        display_name=name,
                        role=role,
                        audio_path=wav_path,
                        segments=diarization,
                        speaker_label=label,
                        device=device,
                        data_dir=None,
                        notes=notes,
                    )
                    speaker_metadata.append({"name": name, "role": role})
                click.echo(f"\n  Enrolled {len(unique_speakers)} speakers.")
            else:
                from .speaker_manager import match_speakers
                matches = match_speakers(
                    audio_path=wav_path,
                    diarization_segments=diarization,
                    data_dir=None,
                    device=device,
                    threshold=config.get("similarity_threshold", 0.65),
                )
                if matches:
                    speaker_map = matches
                    tqdm.write("  Speaker matches:")
                    for label, name in sorted(matches.items()):
                        tqdm.write(f"    {label} → {name}")
                    # Build speaker metadata from matched names (deduplicated, preserving order)
                    seen: set[str] = set()
                    for name in matches.values():
                        if name not in seen:
                            seen.add(name)
                            speaker_metadata.append({"name": name, "role": ""})
                else:
                    # No profiles yet — use raw labels
                    speaker_map = {s: s for s in unique_speakers}
                    for label in unique_speakers:
                        speaker_metadata.append({"name": label, "role": ""})

    metadata = {
        "title": path.stem.replace("_", " ").replace("-", " ").title(),
        "source_file": path.name,
        "date_processed": datetime.date.today().isoformat(),
        "duration": _seconds_to_hhmmss(duration),
        "speakers": speaker_metadata,
    }

    content = to_markdown(
        aligned_segments,
        speaker_map=speaker_map,
        metadata=metadata,
        include_timestamps=include_timestamps,
    )

    out_path.write_text(content, encoding="utf-8")
    tqdm.write(f"  Wrote {out_path.name}")
    return out_path


def process_folder(
    folder: Path,
    output_dir: Optional[Path] = None,
    verbose: bool = False,
    **kwargs,
) -> tuple[list[Path], list[str]]:
    """Process all audio files in a folder.

    Returns (successful_paths, error_messages).
    Skips files whose .md output already exists unless overwrite=True is in kwargs.
    """
    folder = Path(folder)
    audio_files = sorted(
        f for f in folder.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not audio_files:
        return [], []

    successes: list[Path] = []
    errors: list[str] = []
    overwrite = kwargs.get("overwrite", False)
    out_base = Path(output_dir) if output_dir else folder

    progress = tqdm(
        audio_files, 
        desc="Folder Progress", 
        unit="file", 
        position=0, 
        leave=True,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
        dynamic_ncols=True,
    )

    for f in progress:
        progress.set_description(f"Processing {f.name}")
        out_path = out_base / (f.stem + ".md")
        if out_path.exists() and not overwrite:
            if verbose:
                tqdm.write(f"  Skipping {f.name} (already exists)")
            continue
        try:
            result = process_file(f, output_dir=output_dir, **kwargs)
            successes.append(result)
        except Exception as exc:
            errors.append(f"{f.name}: {exc}")
            tqdm.write(f"  ERROR {f.name}: {exc}")

    return successes, errors
