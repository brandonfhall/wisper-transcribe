from __future__ import annotations

import datetime
import subprocess
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
    """Play up to _MAX_PLAYBACK_SECONDS of a WAV excerpt using ffplay.

    ffplay ships with ffmpeg, which is already a hard dependency, making this
    reliable on Windows, macOS, and Linux without extra Python audio packages.
    Warns (but doesn't abort) if playback fails.
    """
    import click

    duration = min(end - start, _MAX_PLAYBACK_SECONDS)
    click.echo("  [playing audio excerpt...]")
    try:
        subprocess.run(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel", "quiet",
                "-ss", str(start),
                "-t", str(duration),
                str(wav_path),
            ],
            check=True,
        )
    except FileNotFoundError:
        click.echo("  [ffplay not found — skipping audio playback]")
    except subprocess.CalledProcessError:
        click.echo("  [audio playback failed — skipping]")


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
    compute_type: str = "auto",
    vad_filter: Optional[bool] = None,
    initial_prompt: Optional[str] = None,
    hotwords: Optional[list[str]] = None,
) -> Path:
    """Run the full pipeline on a single audio file. Returns path to output .md."""
    from .config import resolve_compute_type

    path = Path(path)
    config = load_config()

    if device == "auto":
        device = get_device()
    if model_size == "medium":
        model_size = config.get("model", "medium")
    if language == "en":
        language = config.get("language", "en")
    if compute_type == "auto":
        compute_type = config.get("compute_type", "auto")
    if vad_filter is None:
        vad_filter = config.get("vad_filter", True)
    if hotwords is None:
        config_hotwords = config.get("hotwords", [])
        hotwords = config_hotwords if config_hotwords else None

    check_ffmpeg()
    validate_audio(path)

    out_dir = Path(output_dir) if output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (path.stem + ".md")

    if out_path.exists() and not overwrite:
        tqdm.write(f"  Skipping {path.name} — already processed (use --overwrite to re-run)")
        return out_path

    resolved_ct = resolve_compute_type(compute_type, device)
    tqdm.write("")
    tqdm.write("─" * 60)
    tqdm.write(f"  Input  : {path}")
    tqdm.write(f"  Output : {out_path}")
    tqdm.write(f"  Model  : {model_size} ({device}, {resolved_ct})")
    tqdm.write("─" * 60)

    wav_path = convert_to_wav(path)

    segments: list[TranscriptionSegment] = transcribe(
        wav_path,
        model_size=model_size,
        device=device,
        language=language,
        compute_type=compute_type,
        vad_filter=vad_filter,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
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
                from .speaker_manager import enroll_speaker, load_profiles
                import click
                speaker_map = {}
                existing_profiles = load_profiles()
                existing_names = sorted(existing_profiles.keys())  # stable order
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

                    # Offer replay loop if --play-audio is set
                    if play_audio and sample:
                        _play_excerpt(wav_path, sample.start, sample.end)

                    # Show existing speakers the user can pick by number
                    if existing_names:
                        click.echo("  Existing speakers:")
                        for idx, pname in enumerate(existing_names, 1):
                            p = existing_profiles[pname]
                            role_str = f" ({p.role})" if p.role else ""
                            click.echo(f"    {idx}. {p.display_name}{role_str}")
                        click.echo("  Enter a number to select, or type a new name.")

                    # Name prompt — loop to support 'r' replay and numeric selection
                    name = ""
                    while True:
                        prompt_hint = " (or 'r' to replay)" if play_audio and sample else ""
                        raw = click.prompt(f"  Who is this?{prompt_hint}").strip()
                        if play_audio and sample and raw.lower() == "r":
                            _play_excerpt(wav_path, sample.start, sample.end)
                            continue
                        if existing_names and raw.isdigit():
                            idx = int(raw) - 1
                            if 0 <= idx < len(existing_names):
                                name = existing_profiles[existing_names[idx]].display_name
                                break
                            else:
                                click.echo(f"  Please enter a number between 1 and {len(existing_names)}, or a name.")
                                continue
                        if raw:
                            name = raw
                            break

                    # If user picked an existing profile, skip re-enrollment
                    is_existing = name in {p.display_name for p in existing_profiles.values()}
                    if is_existing:
                        profile_key = next(k for k, p in existing_profiles.items() if p.display_name == name)
                        role = existing_profiles[profile_key].role
                        notes = ""
                        click.echo(f"  Using existing profile for {name}.")
                        if click.confirm(
                            f"  Add this episode's audio to improve future recognition of {name}?",
                            default=False,
                        ):
                            from .speaker_manager import extract_embedding, update_embedding
                            try:
                                new_emb = extract_embedding(wav_path, diarization, label, device)
                                update_embedding(profile_key, new_emb)
                                click.echo(f"  Updated voice profile for {name}.")
                            except Exception as exc:
                                click.echo(f"  Could not update profile: {exc}")
                    else:
                        role = click.prompt("  Role (DM/Player/Guest, optional)", default="").strip()
                        notes = click.prompt("  Notes (optional)", default="").strip()
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

                    speaker_map[label] = name
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
            tqdm.write(f"  Skipping {f.name} — already processed (use --overwrite to re-run)")
            continue
        try:
            result = process_file(f, output_dir=output_dir, **kwargs)
            successes.append(result)
        except Exception as exc:
            errors.append(f"{f.name}: {exc}")
            tqdm.write(f"  ERROR {f.name}: {exc}")

    return successes, errors
