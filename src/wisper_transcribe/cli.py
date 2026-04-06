from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional

import click

# Suppress harmless multiline torchcodec/FFmpeg warnings from pyannote on Windows
warnings.filterwarnings("ignore", module="pyannote.audio.core.io")

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="wisper")
def main():
    """wisper-transcribe: Podcast transcription with speaker diarization."""


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_dir", type=click.Path(path_type=Path), default=None, help="Output directory (default: same as input)")
@click.option("-m", "--model", "model_size", default="medium", show_default=True, type=click.Choice(["tiny", "base", "small", "medium", "large-v3"]), help="Whisper model size")
@click.option("-l", "--language", default="en", show_default=True, help="Language code (e.g. en, fr) or 'auto'")
@click.option("--device", default="auto", show_default=True, type=click.Choice(["auto", "cpu", "cuda"]), help="Compute device")
@click.option("--overwrite", is_flag=True, default=False, help="Overwrite existing output files")
@click.option("--timestamps/--no-timestamps", default=True, show_default=True, help="Include timestamps in output")
@click.option("-n", "--num-speakers", default=None, type=int, help="Expected number of speakers (improves diarization)")
@click.option("--min-speakers", default=None, type=int, help="Minimum number of speakers")
@click.option("--max-speakers", default=None, type=int, help="Maximum number of speakers")
@click.option("--no-diarize", is_flag=True, default=False, help="Skip speaker diarization")
@click.option("--enroll-speakers", is_flag=True, default=False, help="Interactively name and enroll detected speakers")
@click.option("--verbose", is_flag=True, default=False, help="Show detailed progress")
def transcribe(
    path: Path,
    output_dir: Optional[Path],
    model_size: str,
    language: str,
    device: str,
    overwrite: bool,
    timestamps: bool,
    num_speakers: Optional[int],
    min_speakers: Optional[int],
    max_speakers: Optional[int],
    no_diarize: bool,
    enroll_speakers: bool,
    verbose: bool,
):
    """Transcribe an audio file (or folder of files) to markdown."""
    from .pipeline import process_file, process_folder

    lang = None if language == "auto" else language
    kwargs = dict(
        output_dir=output_dir,
        model_size=model_size,
        device=device,
        language=lang,
        include_timestamps=timestamps,
        overwrite=overwrite,
        no_diarize=no_diarize,
        num_speakers=num_speakers,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        enroll_speakers=enroll_speakers,
    )

    if path.is_dir():
        click.echo(f"Processing folder: {path}")
        successes, errors = process_folder(path, verbose=verbose, **kwargs)
        skipped = sum(
            1 for f in path.iterdir()
            if f.suffix.lower() in _audio_extensions()
            and (kwargs.get("output_dir") or path) / (f.stem + ".md") not in successes
            and not any(e.startswith(f.name) for e in errors)
        )
        click.echo(f"\nDone. {len(successes)} transcribed, {skipped} skipped, {len(errors)} errors.")
        for err in errors:
            click.echo(f"  ERROR: {err}", err=True)
    else:
        try:
            out = process_file(path, **kwargs)
            click.echo(f"Done: {out}")
        except Exception as e:
            raise click.ClickException(str(e))


def _audio_extensions():
    from .audio_utils import SUPPORTED_EXTENSIONS
    return SUPPORTED_EXTENSIONS


@main.group()
def config():
    """Manage wisper configuration."""


@config.command("show")
def config_show():
    """Show current configuration."""
    from .config import get_config_path, load_config

    cfg = load_config()
    click.echo(f"Config file: {get_config_path()}")
    for k, v in cfg.items():
        click.echo(f"  {k} = {v!r}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a configuration value."""
    from .config import load_config, save_config

    cfg = load_config()
    # Attempt to coerce to existing type
    if key in cfg and isinstance(cfg[key], bool):
        value = value.lower() in ("true", "1", "yes")
    elif key in cfg and isinstance(cfg[key], float):
        value = float(value)
    cfg[key] = value
    save_config(cfg)
    click.echo(f"Set {key} = {value!r}")


@config.command("path")
def config_path():
    """Show path to config file."""
    from .config import get_config_path

    click.echo(get_config_path())


# ---------------------------------------------------------------------------
# wisper enroll
# ---------------------------------------------------------------------------

@main.command()
@click.argument("name")
@click.option("--audio", required=True, type=click.Path(exists=True, path_type=Path), help="Audio file to extract voice from")
@click.option("--segment", default=None, help="Time range to use, e.g. '0:30-1:15'")
@click.option("--notes", default="", help="Free-text notes about this speaker")
@click.option("--update", is_flag=True, default=False, help="Average with existing embedding instead of replacing")
def enroll(name: str, audio: Path, segment: Optional[str], notes: str, update: bool):
    """Enroll a speaker from a reference audio clip."""
    from .audio_utils import convert_to_wav
    from .config import get_device, load_config
    from .speaker_manager import enroll_speaker, extract_embedding, update_embedding

    config = load_config()
    device = get_device() if config.get("device", "auto") == "auto" else config["device"]

    wav_path = convert_to_wav(audio)

    # Build a fake single-segment diarization covering the whole file (or requested segment)
    if segment:
        start_str, end_str = segment.split("-")
        def _parse_time(t: str) -> float:
            parts = t.strip().split(":")
            return sum(float(p) * (60 ** (len(parts) - 1 - i)) for i, p in enumerate(parts))
        start = _parse_time(start_str)
        end = _parse_time(end_str)
    else:
        from .audio_utils import get_duration
        start, end = 0.0, get_duration(wav_path)

    from .models import DiarizationSegment
    fake_segs = [DiarizationSegment(start=start, end=end, speaker="SPEAKER_00")]

    key = name.lower().replace(" ", "_")

    if update:
        new_emb = extract_embedding(wav_path, fake_segs, "SPEAKER_00", device)
        update_embedding(key, new_emb)
        click.echo(f"Updated embedding for {name!r} (EMA blend).")
    else:
        profile = enroll_speaker(
            name=key,
            display_name=name,
            role="",
            audio_path=wav_path,
            segments=fake_segs,
            speaker_label="SPEAKER_00",
            device=device,
            notes=notes,
        )
        click.echo(f"Enrolled {profile.display_name!r}.")


# ---------------------------------------------------------------------------
# wisper speakers
# ---------------------------------------------------------------------------

@main.group()
def speakers():
    """Manage enrolled speaker profiles."""


@speakers.command("list")
def speakers_list():
    """List all enrolled speakers."""
    from .speaker_manager import load_profiles

    profiles = load_profiles()
    if not profiles:
        click.echo("No speakers enrolled. Run: wisper transcribe --enroll-speakers")
        return

    click.echo(f"{'Name':<20} {'Role':<12} {'Enrolled':<12} {'Source'}")
    click.echo("-" * 60)
    for name, p in sorted(profiles.items()):
        click.echo(f"{p.display_name:<20} {p.role:<12} {p.enrolled_date:<12} {p.enrollment_source}")


@speakers.command("remove")
@click.argument("name")
def speakers_remove(name: str):
    """Remove an enrolled speaker profile."""
    from .speaker_manager import _get_embeddings_dir, load_profiles, save_profiles

    profiles = load_profiles()
    key = name.lower().replace(" ", "_")
    if key not in profiles:
        raise click.ClickException(f"Speaker {name!r} not found.")

    emb_path = _get_embeddings_dir() / f"{key}.npy"
    if emb_path.exists():
        emb_path.unlink()

    del profiles[key]
    save_profiles(profiles)
    click.echo(f"Removed speaker {name!r}.")


@speakers.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def speakers_rename(old_name: str, new_name: str):
    """Rename an enrolled speaker."""
    from .speaker_manager import _get_embeddings_dir, load_profiles, save_profiles

    profiles = load_profiles()
    old_key = old_name.lower().replace(" ", "_")
    new_key = new_name.lower().replace(" ", "_")

    if old_key not in profiles:
        raise click.ClickException(f"Speaker {old_name!r} not found.")

    profile = profiles.pop(old_key)
    profile.name = new_key
    profile.display_name = new_name

    emb_dir = _get_embeddings_dir()
    old_emb = emb_dir / f"{old_key}.npy"
    new_emb = emb_dir / f"{new_key}.npy"
    if old_emb.exists():
        old_emb.rename(new_emb)
    profile.embedding_path = new_emb

    profiles[new_key] = profile
    save_profiles(profiles)
    click.echo(f"Renamed {old_name!r} → {new_name!r}.")


@speakers.command("test")
@click.argument("audio", type=click.Path(exists=True, path_type=Path))
@click.option("-n", "--num-speakers", default=None, type=int)
def speakers_test(audio: Path, num_speakers: Optional[int]):
    """Show speaker matching results for an audio file without writing output."""
    from .audio_utils import convert_to_wav
    from .config import get_device, get_hf_token, load_config
    from .diarizer import diarize
    from .speaker_manager import match_speakers

    config = load_config()
    device = get_device() if config.get("device", "auto") == "auto" else config["device"]
    hf_token = get_hf_token(config)

    wav_path = convert_to_wav(audio)
    diarization = diarize(wav_path, hf_token=hf_token, device=device, num_speakers=num_speakers)
    matches = match_speakers(wav_path, diarization, device=device,
                             threshold=config.get("similarity_threshold", 0.65))

    if not matches:
        click.echo("No enrolled profiles to match against.")
        return

    for label, name in sorted(matches.items()):
        click.echo(f"  {label} → {name}")


# ---------------------------------------------------------------------------
# wisper fix
# ---------------------------------------------------------------------------

@main.command()
@click.argument("transcript", type=click.Path(exists=True, path_type=Path))
@click.option("--speaker", required=True, help="Current speaker name to replace")
@click.option("--name", "new_name", required=True, help="Correct name")
@click.option("--re-enroll", is_flag=True, default=False, help="Also update voice embedding from original audio")
def fix(transcript: Path, speaker: str, new_name: str, re_enroll: bool):
    """Fix a speaker name in an existing transcript."""
    from .formatter import update_speaker_names

    content = transcript.read_text(encoding="utf-8")
    updated = update_speaker_names(content, speaker, new_name)
    transcript.write_text(updated, encoding="utf-8")
    click.echo(f"Updated {transcript.name}: {speaker!r} → {new_name!r}")

    if re_enroll:
        click.echo("Re-enrollment from fix is not yet automated. "
                   "Run: wisper enroll <name> --audio <original_file> --update")
