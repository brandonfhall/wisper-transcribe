from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

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
def transcribe(
    path: Path,
    output_dir: Optional[Path],
    model_size: str,
    language: str,
    device: str,
    overwrite: bool,
    timestamps: bool,
):
    """Transcribe an audio file (or folder of files) to markdown."""
    from .pipeline import process_file

    lang = None if language == "auto" else language

    if path.is_dir():
        click.echo(f"Processing folder: {path}")
        _transcribe_folder(path, output_dir, model_size, device, lang, timestamps, overwrite)
    else:
        try:
            out = process_file(
                path,
                output_dir=output_dir,
                model_size=model_size,
                device=device,
                language=lang,
                include_timestamps=timestamps,
                overwrite=overwrite,
            )
            click.echo(f"Done: {out}")
        except Exception as e:
            raise click.ClickException(str(e))


def _transcribe_folder(path, output_dir, model_size, device, language, timestamps, overwrite):
    from .audio_utils import SUPPORTED_EXTENSIONS
    from .pipeline import process_file

    audio_files = [
        f for f in sorted(path.iterdir())
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not audio_files:
        click.echo("No supported audio files found.")
        return

    click.echo(f"Processing {len(audio_files)} files...")
    transcribed = skipped = errors = 0

    for i, f in enumerate(audio_files, 1):
        out_path = (output_dir or f.parent) / (f.stem + ".md")
        if out_path.exists() and not overwrite:
            click.echo(f"  [{i}/{len(audio_files)}] {f.name} → skipped (already exists)")
            skipped += 1
            continue
        try:
            process_file(f, output_dir=output_dir, model_size=model_size, device=device,
                         language=language, include_timestamps=timestamps, overwrite=overwrite)
            click.echo(f"  [{i}/{len(audio_files)}] {f.name} → {f.stem}.md ✓")
            transcribed += 1
        except Exception as e:
            click.echo(f"  [{i}/{len(audio_files)}] {f.name} → ERROR: {e}", err=True)
            errors += 1

    click.echo(f"\nDone. {transcribed} transcribed, {skipped} skipped, {errors} errors.")


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
