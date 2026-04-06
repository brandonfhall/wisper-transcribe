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
@click.option("--device", default="auto", show_default=True, type=click.Choice(["auto", "cpu", "cuda", "mps"]), help="Compute device")
@click.option("--overwrite", is_flag=True, default=False, help="Overwrite existing output files")
@click.option("--timestamps/--no-timestamps", default=True, show_default=True, help="Include timestamps in output")
@click.option("-n", "--num-speakers", default=None, type=int, help="Expected number of speakers (improves diarization)")
@click.option("--min-speakers", default=None, type=int, help="Minimum number of speakers")
@click.option("--max-speakers", default=None, type=int, help="Maximum number of speakers")
@click.option("--no-diarize", is_flag=True, default=False, help="Skip speaker diarization")
@click.option("--enroll-speakers", is_flag=True, default=False, help="Interactively name and enroll detected speakers")
@click.option("--play-audio", is_flag=True, default=False, help="Play each speaker's audio excerpt during enrollment")
@click.option("--compute-type", default="auto", show_default=True,
              type=click.Choice(["auto", "float16", "int8_float16", "int8", "float32"]),
              help="CTranslate2 quantization (auto=float16 on CUDA, int8 on CPU)")
@click.option("--vad/--no-vad", default=None,
              help="Voice activity detection to skip silence (default: on, from config)")
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
    play_audio: bool,
    compute_type: str,
    vad: Optional[bool],
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
        play_audio=play_audio,
        compute_type=compute_type,
        vad_filter=vad,
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


@main.command()
def setup():
    """Guided first-run setup: ffmpeg, HF token, and model pre-download."""
    import os
    import sys

    from .config import check_ffmpeg, get_device, load_config, save_config

    click.echo("")
    click.echo("wisper-transcribe setup")
    click.echo("=" * 42)

    # ── ffmpeg ────────────────────────────────────────────────────────────────
    click.echo("\n>> Checking ffmpeg...")
    try:
        check_ffmpeg()
        click.echo("   OK  : ffmpeg found")
    except RuntimeError as e:
        click.echo(f"   FAIL: {e}", err=True)
        click.echo("   Run setup.sh (Mac/Linux) or setup.ps1 (Windows) to install it automatically.")
        sys.exit(1)

    # ── Device ────────────────────────────────────────────────────────────────
    click.echo("\n>> Detecting compute device...")
    device = get_device()
    labels = {"cuda": "NVIDIA GPU (CUDA)", "mps": "Apple Silicon GPU (MPS)", "cpu": "CPU"}
    click.echo(f"   OK  : {labels.get(device, device)}")
    if device == "mps":
        click.echo("   Note: transcription uses CPU (CTranslate2 limitation); diarization uses MPS")

    # ── HuggingFace token ─────────────────────────────────────────────────────
    click.echo("\n>> Checking HuggingFace token...")
    config = load_config()
    token = os.environ.get("HUGGINGFACE_TOKEN", "") or config.get("hf_token", "")
    if token:
        click.echo("   OK  : token already configured")
    else:
        click.echo("   A free HuggingFace token is required for speaker diarization.")
        click.echo("   Get one at: https://huggingface.co/settings/tokens")
        click.echo("")
        click.echo("   You must also accept the model licenses (free, one-time):")
        click.echo("     https://huggingface.co/pyannote/speaker-diarization-3.1")
        click.echo("     https://huggingface.co/pyannote/embedding")
        click.echo("")
        token = click.prompt("   HuggingFace token", hide_input=True).strip()
        if token:
            config["hf_token"] = token
            save_config(config)
            click.echo("   OK  : token saved")
        else:
            click.echo("   WARN: no token provided — diarization will be skipped on first run")

    # ── Model pre-download ────────────────────────────────────────────────────
    if token:
        click.echo("\n>> Pre-downloading pyannote models (first run only — may take a few minutes)...")
        try:
            from pyannote.audio import Inference, Model, Pipeline

            click.echo("   Downloading pyannote/speaker-diarization-3.1 ...")
            pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
            del pipeline
            click.echo("   Downloading pyannote/embedding ...")
            model = Model.from_pretrained("pyannote/embedding", token=token)
            del model
            click.echo("   OK  : all models cached — subsequent runs start immediately")
        except Exception as e:
            click.echo(f"   WARN: model download failed: {e}", err=True)
            click.echo("   Models will download automatically on first transcription run.")

    # ── Done ──────────────────────────────────────────────────────────────────
    click.echo("")
    click.echo("=" * 42)
    click.echo("Setup complete!")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  wisper transcribe <file.mp3> --enroll-speakers")
    click.echo("")


@main.group()
def config():
    """Manage wisper configuration."""


@config.command("show")
def config_show():
    """Show current configuration and data paths."""
    import os
    from .config import COMPUTE_TYPES, get_config_path, get_data_dir, get_device, load_config, resolve_compute_type

    cfg = load_config()
    data_dir = get_data_dir()
    profiles_dir = data_dir / "profiles"
    hf_cache = os.environ.get(
        "HF_HOME",
        os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub"),
    )

    click.echo("─" * 50)
    click.echo("Paths")
    click.echo("─" * 50)
    click.echo(f"  Config file    : {get_config_path()}")
    click.echo(f"  Data directory : {data_dir}")
    click.echo(f"  Speaker profiles: {profiles_dir}")
    click.echo(f"  HF model cache : {hf_cache}")
    click.echo("")
    click.echo("─" * 50)
    click.echo("Models")
    click.echo("─" * 50)
    device = get_device()
    model = cfg.get("model", "medium")
    ct_setting = cfg.get("compute_type", "auto")
    ct_resolved = resolve_compute_type(ct_setting, device)
    ct_display = f"{ct_setting} → {ct_resolved}" if ct_setting == "auto" else ct_setting
    click.echo(f"  Device         : {device}")
    click.echo(f"  Whisper model  : {model}")
    click.echo(f"  Compute type   : {ct_display}")
    click.echo(f"  Diarization    : pyannote/speaker-diarization-3.1")
    click.echo(f"  Embedding      : pyannote/embedding")
    click.echo("")
    click.echo("─" * 50)
    click.echo("Settings")
    click.echo("─" * 50)
    for k, v in cfg.items():
        display = "***" if k == "hf_token" and v else repr(v)
        click.echo(f"  {k:<22} = {display}")


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


@speakers.command("reset")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt")
def speakers_reset(yes: bool):
    """Delete all enrolled speaker profiles and embeddings."""
    from .speaker_manager import load_profiles, reset_profiles

    profiles = load_profiles()
    count = len(profiles)
    if count == 0:
        click.echo("No speakers enrolled — nothing to reset.")
        return

    names = ", ".join(p.display_name for p in profiles.values())
    click.echo(f"This will permanently delete {count} speaker(s): {names}")

    if not yes:
        click.confirm("Reset speaker database?", abort=True)

    reset_profiles()
    click.echo(f"Removed {count} speaker(s). Database is now empty.")


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
