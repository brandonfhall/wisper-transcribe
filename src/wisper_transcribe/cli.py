from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Optional

import click

# Suppress harmless multiline torchcodec/FFmpeg warnings from pyannote on Windows.
# Set WISPER_DEBUG=1 to disable all warning suppression for debugging.
if not os.environ.get("WISPER_DEBUG"):
    warnings.filterwarnings("ignore", module="pyannote.audio.core.io")

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="wisper")
def main():
    """wisper-transcribe: Podcast transcription with speaker diarization."""


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_dir", type=click.Path(path_type=Path), default=None, help="Output directory (default: same as input)")
@click.option("-m", "--model", "model_size", default="large-v3-turbo", show_default=True, type=click.Choice(["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]), help="Whisper model size")
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
@click.option("--vocab-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
              help="Text file of custom words/names (one per line) to boost transcription accuracy")
@click.option("--initial-prompt", default=None,
              help="Text prepended as context to guide transcription style and vocabulary")
@click.option("--workers", default=1, type=click.IntRange(min=1),
              help="Parallel workers for folder processing (CPU-only; clamped to 1 on GPU)")
@click.option("--verbose", is_flag=True, default=False, help="Show detailed progress")
@click.option("--debug", is_flag=True, default=False,
              help="Write full debug log to ./logs/wisper_<timestamp>.log")
@click.option("--campaign", default=None,
              help="Scope speaker matching to this campaign's roster (slug, e.g. dnd-mondays)")
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
    vocab_file: Optional[Path],
    initial_prompt: Optional[str],
    workers: int,
    verbose: bool,
    debug: bool,
    campaign: Optional[str],
):
    """Transcribe an audio file (or folder of files) to markdown."""
    if debug or verbose:
        from .debug_log import setup_logging
        log_path = setup_logging(debug=debug, verbose=verbose)
        if log_path:
            click.echo(f"  Debug log: {log_path}")

    from .pipeline import process_file, process_folder

    lang = None if language == "auto" else language

    hotwords: Optional[list[str]] = None
    if vocab_file is not None:
        lines = Path(vocab_file).read_text(encoding="utf-8").splitlines()
        hotwords = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

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
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        campaign=campaign,
    )

    if path.is_dir():
        click.echo(f"Processing folder: {path}")
        successes, errors = process_folder(path, verbose=verbose, workers=workers, **kwargs)
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
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host")
@click.option("--port", default=8080, show_default=True, type=int, help="Bind port")
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code change (dev mode)")
@click.option("--debug", is_flag=True, default=False,
              help="Write full debug log to ./logs/wisper_<timestamp>.log")
def server(host: str, port: int, reload: bool, debug: bool) -> None:
    """Start the wisper web UI server.

    Opens a browser-based interface for transcription, speaker management,
    and configuration.  Visit http://localhost:8080 after starting.

    All web assets are served locally — no internet connection required at
    runtime once the package is installed.
    """
    if debug:
        from .debug_log import setup_logging
        log_path = setup_logging(debug=True)
        if log_path:
            click.echo(f"  Debug log: {log_path}")

    try:
        import uvicorn
    except ImportError:
        raise click.ClickException(
            "uvicorn is required to run the web server.  "
            "Install with: pip install 'wisper-transcribe[web]' or pip install uvicorn"
        )
    click.echo(f"Starting wisper web UI on http://{host}:{port}")
    click.echo("Press Ctrl+C to stop.")
    uvicorn.run(
        "wisper_transcribe.web.app:app",
        host=host,
        port=port,
        reload=reload,
    )


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

    # ── LLM post-processing (opt-in) ──────────────────────────────────────────
    click.echo("\n>> LLM post-processing (wisper refine / wisper summarize)")
    click.echo("   These commands clean up transcripts and generate campaign notes.")
    click.echo("   The default provider is Ollama (local — no API key required).")
    click.echo("   Cloud providers (Anthropic, OpenAI, Google) need an API key.")
    want_llm = click.confirm("   Configure an LLM provider now?", default=False)
    if want_llm:
        from .config import LLM_PROVIDERS

        provider = click.prompt(
            f"   Provider [{'/'.join(LLM_PROVIDERS)}]",
            default=config.get("llm_provider", "ollama"),
            show_default=False,
        ).strip().lower()
        if provider not in LLM_PROVIDERS:
            click.echo(f"   WARN: unknown provider {provider!r} — skipping LLM setup", err=True)
        else:
            from .config import _LLM_DEFAULT_ENDPOINTS
            provider_defaults = {
                "ollama": "llama3.1:8b",
                "lmstudio": "",
                "anthropic": "claude-sonnet-4-6",
                "openai": "gpt-4o-mini",
                "google": "gemini-1.5-flash",
            }
            suggested_model = config.get("llm_model", "") or provider_defaults.get(provider, "")

            if provider in ("ollama", "lmstudio"):
                default_ep = _LLM_DEFAULT_ENDPOINTS.get(provider, "http://localhost:11434")
                endpoint = config.get("llm_endpoint") or default_ep
                endpoint = click.prompt(
                    f"   Endpoint [{endpoint}]", default=endpoint, show_default=False
                ).strip()
                config["llm_endpoint"] = endpoint

                local_models = _get_ollama_models() if provider == "ollama" else _get_lmstudio_models(endpoint)
                if local_models:
                    click.echo("")
                    label = "Ollama" if provider == "ollama" else "LM Studio"
                    click.echo(f"   Installed {label} models:")
                    for i, (name, size) in enumerate(local_models, 1):
                        suffix = f"  ({size})" if size else ""
                        click.echo(f"   {i}. {name}{suffix}")
                    raw = click.prompt(
                        f"   Model — number or name [{suggested_model}]",
                        default=suggested_model, show_default=False,
                    ).strip()
                    model_choice = local_models[int(raw) - 1][0] if (raw.isdigit() and 1 <= int(raw) <= len(local_models)) else raw
                else:
                    model_choice = click.prompt(
                        f"   Model [{suggested_model}]", default=suggested_model, show_default=False
                    ).strip()
            else:
                model_choice = click.prompt(
                    f"   Model [{suggested_model}]", default=suggested_model, show_default=False
                ).strip()

            config["llm_provider"] = provider
            config["llm_model"] = model_choice

            if provider not in ("ollama", "lmstudio"):
                env_map = {
                    "anthropic": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
                    "openai": ("OPENAI_API_KEY", "openai_api_key"),
                    "google": ("GOOGLE_API_KEY", "google_api_key"),
                }
                env_name, config_key = env_map[provider]
                click.echo(f"\n   Tip: the env var {env_name} always takes precedence if set.")
                click.echo("   Leave blank to set it later via the env var.")
                entered = click.prompt("   API key", default="", show_default=False,
                                       hide_input=True).strip()
                if entered:
                    config[config_key] = entered

            save_config(config)
            click.echo(f"   OK  : LLM config saved ({provider} / {model_choice})")
    else:
        click.echo("   Skipped — run 'wisper config llm' any time to configure this.")

    # ── Done ──────────────────────────────────────────────────────────────────
    click.echo("")
    click.echo("=" * 42)
    click.echo("Setup complete!")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  wisper transcribe <file.mp3> --enroll-speakers")
    click.echo("  wisper refine session.md --dry-run       # optional: LLM vocabulary cleanup")
    click.echo("  wisper summarize session.md              # optional: generate campaign notes")
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
    from .config import LLM_SECRET_KEYS
    secret_keys = {"hf_token"} | set(LLM_SECRET_KEYS)
    for k, v in cfg.items():
        display = "***" if k in secret_keys and v else repr(v)
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
    elif key in cfg and isinstance(cfg[key], list):
        # Accept comma-separated input: "Kyra, Golarion, Zeldris" → ["Kyra", "Golarion", "Zeldris"]
        value = [w.strip() for w in value.split(",") if w.strip()]
    cfg[key] = value
    save_config(cfg)
    click.echo(f"Set {key} = {value!r}")


@config.command("path")
def config_path():
    """Show path to config file."""
    from .config import get_config_path

    click.echo(get_config_path())


def _get_ollama_models() -> list[tuple[str, str]]:
    """Return (name, size) pairs for models installed in the local Ollama instance.

    Calls ``ollama list`` via subprocess. Returns an empty list if ollama is
    not on PATH, not running, or exits non-zero — callers fall back to a plain
    text prompt in that case.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    models: list[tuple[str, str]] = []
    for line in result.stdout.strip().splitlines()[1:]:  # skip header row
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        size = f"{parts[2]} {parts[3]}" if len(parts) >= 4 else ""
        models.append((name, size))
    return models


def _get_lmstudio_models(endpoint: str = "http://localhost:1234") -> list[tuple[str, str]]:
    """Return (id, size) pairs for models loaded in the local LM Studio instance.

    Queries ``GET /v1/models`` via httpx.  Returns an empty list if LM Studio
    is not running or the request fails — callers fall back to a plain text prompt.
    """
    try:
        import httpx
        r = httpx.get(f"{endpoint.rstrip('/')}/v1/models", timeout=3.0)
        r.raise_for_status()
        data = r.json()
        return [(m["id"], "") for m in data.get("data", []) if m.get("id")]
    except Exception:
        return []


@config.command("llm")
def config_llm():
    """Interactive wizard for LLM provider, model, and API key / endpoint.

    Applies to both local (Ollama) and cloud (Anthropic / OpenAI / Google).
    Mirrors the HF-token setup flow. API keys can alternatively be set via
    environment variables (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY),
    which always take precedence over stored config values.
    """
    from .config import LLM_PROVIDERS, load_config, save_config

    cfg = load_config()
    current_provider = cfg.get("llm_provider", "ollama")

    click.echo("")
    click.echo("LLM provider configuration")
    click.echo("─" * 40)
    provider = click.prompt(
        f"Provider [{'/'.join(LLM_PROVIDERS)}]",
        default=current_provider,
        show_default=False,
    ).strip().lower()
    if provider not in LLM_PROVIDERS:
        raise click.ClickException(f"Unknown provider: {provider!r}")

    from .config import _LLM_DEFAULT_ENDPOINTS
    provider_defaults = {
        "ollama": "llama3.1:8b",
        "lmstudio": "",
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o-mini",
        "google": "gemini-1.5-flash",
    }
    suggested_model = cfg.get("llm_model", "") or provider_defaults.get(provider, "")

    if provider in ("ollama", "lmstudio"):
        default_ep = _LLM_DEFAULT_ENDPOINTS.get(provider, "http://localhost:11434")
        endpoint = cfg.get("llm_endpoint") or default_ep
        endpoint = click.prompt(f"Endpoint [{endpoint}]", default=endpoint,
                                show_default=False).strip()
        cfg["llm_endpoint"] = endpoint

        if provider == "ollama":
            local_models = _get_ollama_models()
        else:
            local_models = _get_lmstudio_models(endpoint)

        if local_models:
            click.echo("")
            label = "Ollama" if provider == "ollama" else "LM Studio"
            click.echo(f"Installed {label} models:")
            for i, (name, size) in enumerate(local_models, 1):
                suffix = f"  ({size})" if size else ""
                click.echo(f"  {i}. {name}{suffix}")
            raw = click.prompt(
                f"Model — number or name [{suggested_model}]",
                default=suggested_model, show_default=False,
            ).strip()
            model = local_models[int(raw) - 1][0] if (raw.isdigit() and 1 <= int(raw) <= len(local_models)) else raw
        else:
            model = click.prompt(f"Model [{suggested_model}]", default=suggested_model,
                                 show_default=False).strip()
    else:
        model = click.prompt(f"Model [{suggested_model}]", default=suggested_model,
                             show_default=False).strip()

    cfg["llm_provider"] = provider
    cfg["llm_model"] = model

    if provider not in ("ollama", "lmstudio"):
        env_name_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
        }
        config_key_map = {
            "anthropic": "anthropic_api_key",
            "openai": "openai_api_key",
            "google": "google_api_key",
        }
        env_name = env_name_map[provider]
        config_key = config_key_map[provider]
        click.echo(
            f"\nAPI key: the env var {env_name} always takes precedence if set.\n"
            "Leave blank to keep the current stored value (or rely on the env var)."
        )
        entered = click.prompt("API key", default="", show_default=False, hide_input=True).strip()
        if entered:
            cfg[config_key] = entered

    save_config(cfg)
    click.echo("")
    click.echo(f"Saved. Test with: wisper summarize <file.md> --provider {provider}")


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
@click.option("--campaign", default=None, help="Scope matching to this campaign's roster")
def speakers_test(audio: Path, num_speakers: Optional[int], campaign: Optional[str]):
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

    profile_filter = None
    if campaign:
        from .campaign_manager import _validate_campaign_slug, get_campaign_profile_keys
        safe = _validate_campaign_slug(campaign)
        if safe is None:
            raise click.ClickException(f"Invalid campaign slug: {campaign!r}")
        profile_filter = get_campaign_profile_keys(safe)
        click.echo(f"  Campaign filter: {safe} ({len(profile_filter)} member(s))")

    matches = match_speakers(wav_path, diarization, device=device,
                             threshold=config.get("similarity_threshold", 0.65),
                             profile_filter=profile_filter)

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
# wisper campaigns
# ---------------------------------------------------------------------------

@main.group()
def campaigns():
    """Manage campaigns (per-show speaker rosters)."""


@campaigns.command("list")
def campaigns_list():
    """List all campaigns."""
    from .campaign_manager import load_campaigns

    data = load_campaigns()
    if not data:
        click.echo("No campaigns. Run: wisper campaigns create \"My Campaign\"")
        return

    click.echo(f"{'Slug':<25} {'Name':<30} {'Members':<8} {'Created'}")
    click.echo("-" * 72)
    for slug, c in sorted(data.items()):
        click.echo(f"{slug:<25} {c.display_name:<30} {len(c.members):<8} {c.created}")


@campaigns.command("create")
@click.argument("display_name")
def campaigns_create(display_name: str):
    """Create a new campaign. The slug is auto-derived from the name."""
    from .campaign_manager import create_campaign

    try:
        campaign = create_campaign(display_name)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Created campaign {campaign.display_name!r} (slug: {campaign.slug})")


@campaigns.command("delete")
@click.argument("slug")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt")
def campaigns_delete(slug: str, yes: bool):
    """Delete a campaign. Does not affect enrolled speaker profiles."""
    from .campaign_manager import _validate_campaign_slug, delete_campaign

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")

    if not yes:
        click.confirm(f"Delete campaign {safe!r}?", abort=True)

    try:
        delete_campaign(safe)
    except KeyError:
        raise click.ClickException(f"Campaign {safe!r} not found.")

    click.echo(f"Deleted campaign {safe!r}.")


@campaigns.command("show")
@click.argument("slug")
def campaigns_show(slug: str):
    """Show the roster for a campaign."""
    from .campaign_manager import _validate_campaign_slug, load_campaigns
    from .speaker_manager import load_profiles

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")

    data = load_campaigns()
    if safe not in data:
        raise click.ClickException(f"Campaign {safe!r} not found.")

    campaign = data[safe]
    profiles = load_profiles()

    click.echo(f"Campaign: {campaign.display_name} (slug: {campaign.slug})")
    click.echo(f"Created:  {campaign.created}")
    click.echo("")

    if not campaign.members:
        click.echo("  No members yet. Run: wisper campaigns add-member <slug> <profile_key>")
        return

    click.echo(f"  {'Profile Key':<20} {'Display Name':<20} {'Role':<12} {'Character'}")
    click.echo("  " + "-" * 64)
    for key, m in sorted(campaign.members.items()):
        display = profiles[key].display_name if key in profiles else f"(removed: {key})"
        click.echo(f"  {key:<20} {display:<20} {m.role:<12} {m.character}")


@campaigns.command("add-member")
@click.argument("slug")
@click.argument("profile_key")
@click.option("--role", default="", help="Per-campaign role (e.g. DM, Player)")
@click.option("--character", default="", help="Character name for this campaign")
def campaigns_add_member(slug: str, profile_key: str, role: str, character: str):
    """Add a speaker to a campaign roster."""
    from .campaign_manager import _validate_campaign_slug, add_member
    from .speaker_manager import load_profiles

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")

    profiles = load_profiles()
    if profile_key not in profiles:
        raise click.ClickException(
            f"Speaker {profile_key!r} is not enrolled. Run: wisper speakers list"
        )

    try:
        add_member(safe, profile_key, role=role, character=character)
    except KeyError:
        raise click.ClickException(f"Campaign {safe!r} not found.")

    display = profiles[profile_key].display_name
    click.echo(f"Added {display!r} to campaign {safe!r}.")


@campaigns.command("remove-member")
@click.argument("slug")
@click.argument("profile_key")
def campaigns_remove_member(slug: str, profile_key: str):
    """Remove a speaker from a campaign roster."""
    from .campaign_manager import _validate_campaign_slug, remove_member

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")

    try:
        remove_member(safe, profile_key)
    except KeyError:
        raise click.ClickException(f"Campaign {safe!r} not found.")

    click.echo(f"Removed {profile_key!r} from campaign {safe!r}.")


# ---------------------------------------------------------------------------
# wisper transcripts
# ---------------------------------------------------------------------------


@main.group()
def transcripts():
    """Manage transcripts — list, move to campaign, or unlink from a campaign."""


@transcripts.command("list")
@click.option("--campaign", default=None, help="Show only transcripts for this campaign slug")
def transcripts_list(campaign: Optional[str]):
    """List transcripts, grouped by campaign."""
    from wisper_transcribe.campaign_manager import load_campaigns, _validate_campaign_slug
    from wisper_transcribe.config import get_data_dir
    from pathlib import Path as _Path

    if campaign:
        safe = _validate_campaign_slug(campaign)
        if safe is None:
            raise click.ClickException("Invalid campaign slug")

    out_dir = _Path("output")
    if not out_dir.exists():
        out_dir = _Path(get_data_dir()) / "output"

    all_stems = sorted(p.stem for p in out_dir.glob("*.md") if not p.stem.endswith(".summary"))

    campaigns = load_campaigns()

    # Build stem → campaign slug mapping
    stem_to_campaign: dict[str, str] = {}
    for slug, c in campaigns.items():
        for stem in c.transcripts:
            stem_to_campaign[stem] = slug

    if campaign:
        stems = [s for s in all_stems if stem_to_campaign.get(s) == campaign]
        if not stems:
            click.echo(f"No transcripts found for campaign {campaign!r}.")
            return
        for stem in stems:
            click.echo(stem)
        return

    # Grouped output
    printed_any = False
    for slug, c in campaigns.items():
        campaign_stems = [s for s in all_stems if stem_to_campaign.get(s) == slug]
        if not campaign_stems:
            continue
        click.echo(f"\n📁 {c.display_name} [{slug}]")
        for stem in campaign_stems:
            click.echo(f"   {stem}")
        printed_any = True

    uncampaigned = [s for s in all_stems if s not in stem_to_campaign]
    if uncampaigned:
        if printed_any:
            click.echo("\n(no campaign)")
        for stem in uncampaigned:
            click.echo(f"   {stem}")
    elif not printed_any:
        click.echo("No transcripts found.")


@transcripts.command("move")
@click.argument("stem")
@click.option("--campaign", default=None, help="Campaign slug to assign (omit to unlink)")
@click.option("--no-campaign", "unlink", is_flag=True, default=False, help="Remove campaign association")
def transcripts_move(stem: str, campaign: Optional[str], unlink: bool):
    """Assign a transcript to a campaign, or remove its campaign association."""
    from wisper_transcribe.campaign_manager import (
        move_transcript_to_campaign,
        remove_transcript_from_campaign,
        _validate_campaign_slug,
    )

    if unlink:
        remove_transcript_from_campaign(stem)
        click.echo(f"Unlinked {stem!r} from its campaign.")
        return

    if not campaign:
        raise click.ClickException("Provide --campaign <slug> or --no-campaign")

    safe = _validate_campaign_slug(campaign)
    if safe is None:
        raise click.ClickException("Invalid campaign slug")

    try:
        move_transcript_to_campaign(stem, safe)
    except KeyError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Moved {stem!r} → campaign {safe!r}.")


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


# ---------------------------------------------------------------------------
# wisper refine  /  wisper summarize
# ---------------------------------------------------------------------------

_LLM_PROVIDER_CHOICE = click.Choice(["ollama", "anthropic", "openai", "google"])


def _get_llm_client(provider: Optional[str], model: Optional[str],
                    endpoint: Optional[str]):
    """Resolve provider/model/endpoint from CLI flags + config and return a
    client. Wraps LLMUnavailableError into a click.ClickException so the CLI
    exits cleanly with a user-friendly message.
    """
    from .config import load_config
    from .llm import get_client
    from .llm.errors import LLMUnavailableError

    cfg = load_config()
    effective_provider = (provider or cfg.get("llm_provider", "ollama")).strip().lower()
    if model:
        cfg = dict(cfg)
        cfg["llm_model"] = model
    if endpoint and effective_provider == "ollama":
        cfg = dict(cfg)
        cfg["llm_endpoint"] = endpoint

    try:
        return get_client(effective_provider, config=cfg)
    except LLMUnavailableError as exc:
        raise click.ClickException(str(exc))
    except ValueError as exc:
        raise click.ClickException(str(exc))


def _parse_tasks(raw: str, allowed: tuple[str, ...]) -> list[str]:
    tasks = [t.strip().lower() for t in raw.split(",") if t.strip()]
    bad = [t for t in tasks if t not in allowed]
    if bad:
        raise click.ClickException(
            f"Unknown task(s): {', '.join(bad)}. Allowed: {', '.join(allowed)}"
        )
    return tasks


@main.command()
@click.argument("transcript", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--tasks", "tasks_raw", default="vocabulary", show_default=True,
              help="Comma-separated subset of: vocabulary, unknown")
@click.option("--provider", default=None, type=_LLM_PROVIDER_CHOICE,
              help="LLM provider (default: llm_provider from config)")
@click.option("--model", default=None, help="Model override (default: llm_model from config)")
@click.option("--endpoint", default=None, help="Ollama endpoint override")
@click.option("--dry-run/--apply", "dry_run", default=True, show_default=True,
              help="--dry-run prints a colored diff without writing; --apply writes .md.bak and overwrites the transcript")
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI colors in diff output")
def refine(transcript: Path, tasks_raw: str, provider: Optional[str],
           model: Optional[str], endpoint: Optional[str], dry_run: bool,
           no_color: bool):
    """Refine a transcript with an LLM pass.

    Two surgical passes are available:
    - vocabulary: fixes phonetic misspellings of proper nouns using the
      configured hotwords and enrolled character names as ground truth.
      Edits are validated by edit-distance; freeform rewrites are rejected.
    - unknown: surfaces suggestions to resolve "Unknown Speaker N" labels.
      NEVER auto-applied regardless of confidence; suggestions are printed
      alongside the diff for manual review via `wisper fix`.

    YAML frontmatter is never sent to the LLM and is never modified.
    Dry-run is on by default; pass --apply to write changes (a .md.bak is
    created first).
    """
    from .config import load_config
    from .refine import refine_transcript
    from .llm.errors import LLMUnavailableError, LLMResponseError
    from .speaker_manager import load_profiles

    tasks = _parse_tasks(tasks_raw, ("vocabulary", "unknown"))

    cfg = load_config()
    hotwords: list[str] = list(cfg.get("hotwords", []) or [])
    profiles = load_profiles()
    # Character names are conventionally stored in profile.notes (CLAUDE.md).
    character_names: list[str] = []
    for p in profiles.values():
        if p.notes:
            for token in p.notes.replace(";", ",").split(","):
                t = token.strip()
                if t and not t.lower().startswith("voice_of:"):
                    character_names.append(t)

    client = _get_llm_client(provider, model, endpoint)
    original = transcript.read_text(encoding="utf-8")

    try:
        refined_md, applied_edits, suggestions = refine_transcript(
            original,
            client=client,
            hotwords=hotwords,
            character_names=character_names,
            profiles=profiles,
            tasks=tasks,
        )
    except (LLMUnavailableError, LLMResponseError) as exc:
        raise click.ClickException(str(exc))

    # Summary counts
    click.echo(f"Provider: {client.provider} / model: {client.model}")
    click.echo(f"Vocabulary edits: {len(applied_edits)}")
    click.echo(f"Unknown-speaker suggestions: {len(suggestions)} "
               f"(never auto-applied)")

    if suggestions:
        click.echo("\nUnresolved speakers:")
        for s in suggestions:
            reason = f" — {s.reason}" if s.reason else ""
            click.echo(f"  line {s.line_idx + 1}: {s.current_label} → "
                       f"{s.suggested_name} ({s.confidence:.0%}){reason}")

    if not applied_edits:
        click.echo("\nNo vocabulary changes to apply.")
        return

    from .refine import render_diff
    diff = render_diff(original, refined_md, colour=not no_color)
    if diff.strip():
        click.echo("\n" + diff)

    if dry_run:
        click.echo("\n(dry-run) — pass --apply to write changes. "
                   f"A backup {transcript.name}.bak will be created first.")
        return

    backup = transcript.with_suffix(transcript.suffix + ".bak")
    backup.write_text(original, encoding="utf-8")
    transcript.write_text(refined_md, encoding="utf-8")
    click.echo(f"\nWrote {transcript}. Backup at {backup}.")


@main.command()
@click.argument("transcript", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--provider", default=None, type=_LLM_PROVIDER_CHOICE,
              help="LLM provider (default: llm_provider from config)")
@click.option("--model", default=None, help="Model override (default: llm_model from config)")
@click.option("--endpoint", default=None, help="Ollama endpoint override")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="Output path (default: <transcript>.summary.md alongside input)")
@click.option("--sections", "sections_raw",
              default="summary,loot,npcs,followups", show_default=True,
              help="Comma-separated subset of: summary, loot, npcs, followups")
@click.option("--overwrite", is_flag=True, default=False,
              help="Overwrite existing summary file")
@click.option("--refine", "do_refine", is_flag=True, default=False,
              help="Run vocabulary refine on the transcript first (writes .md.bak, updates transcript)")
@click.option("--refine-tasks", "refine_tasks_raw", default="vocabulary", show_default=True,
              help="Which refine tasks to run when --refine is set. Subset of: vocabulary, unknown")
def summarize(transcript: Path, provider: Optional[str], model: Optional[str],
              endpoint: Optional[str], output_path: Optional[Path],
              sections_raw: str, overwrite: bool, do_refine: bool,
              refine_tasks_raw: str):
    """Generate a campaign-notes summary file from a transcript.

    Produces an Obsidian-friendly `<stem>.summary.md` with sections for the
    session recap, loot/inventory changes, notable NPCs, and plot follow-ups.
    Character names matching enrolled speakers or their `notes` are wrapped
    in `[[wiki-links]]`; unknown names are rendered plain.

    Pass `--refine` to run vocabulary refine on the transcript first (same
    behaviour as `wisper refine --apply`). Any unknown-speaker suggestions
    from `--refine-tasks unknown` are written to an `## Unresolved Speakers`
    section of the summary file — they are never auto-applied.
    """
    from .config import load_config
    from .refine import refine_transcript
    from .summarize import default_summary_path, render_markdown, summarize_transcript
    from .llm.errors import LLMUnavailableError, LLMResponseError
    from .speaker_manager import load_profiles

    sections = _parse_tasks(sections_raw, ("summary", "loot", "npcs", "followups"))

    out_path = output_path or default_summary_path(transcript)
    if out_path.exists() and not overwrite:
        raise click.ClickException(
            f"Summary file exists: {out_path}. Pass --overwrite to replace it."
        )

    client = _get_llm_client(provider, model, endpoint)
    click.echo(
        f"Summarizing with {client.provider} / {client.model} ...", err=True
    )
    profiles = load_profiles()
    original = transcript.read_text(encoding="utf-8")

    # Optional refine-then-summarize flow.
    current_md = original
    unresolved: list = []
    refined_flag = False
    if do_refine:
        refine_tasks = _parse_tasks(refine_tasks_raw, ("vocabulary", "unknown"))
        click.echo("Running refine step first...", err=True)
        cfg = load_config()
        hotwords = list(cfg.get("hotwords", []) or [])
        character_names: list[str] = []
        for p in profiles.values():
            if p.notes:
                for token in p.notes.replace(";", ",").split(","):
                    t = token.strip()
                    if t and not t.lower().startswith("voice_of:"):
                        character_names.append(t)
        try:
            refined_md, applied_edits, unresolved = refine_transcript(
                current_md,
                client=client,
                hotwords=hotwords,
                character_names=character_names,
                profiles=profiles,
                tasks=refine_tasks,
            )
        except (LLMUnavailableError, LLMResponseError) as exc:
            # Fall through to summarize-only with a warning; never abort the
            # combined flow just because refine failed.
            click.echo(f"WARN: refine step failed ({exc}); summarizing original.", err=True)
            refined_md, applied_edits = current_md, []

        if applied_edits and refined_md != current_md:
            backup = transcript.with_suffix(transcript.suffix + ".bak")
            backup.write_text(current_md, encoding="utf-8")
            transcript.write_text(refined_md, encoding="utf-8")
            click.echo(f"Refine applied {len(applied_edits)} edit(s). "
                       f"Backup: {backup}")
            current_md = refined_md
            refined_flag = True
        elif applied_edits:
            # Edits returned but after apply they produced identical text —
            # treat as no-op but still mark refined=True for provenance.
            refined_flag = True

    try:
        note = summarize_transcript(
            current_md, profiles, client,
            sections=sections,
            source_transcript=transcript.name,
            unresolved_speakers=unresolved,
            refined=refined_flag,
        )
    except (LLMUnavailableError, LLMResponseError) as exc:
        raise click.ClickException(str(exc))

    body = render_markdown(note, profiles=profiles, sections=sections)
    out_path.write_text(body, encoding="utf-8")
    click.echo(f"Wrote {out_path}")
    click.echo(
        f"  sections: {', '.join(sections)} | "
        f"loot: {len(note.loot)} | npcs: {len(note.npcs)} | "
        f"follow-ups: {len(note.followups)} | "
        f"unresolved speakers: {len(note.unresolved_speakers)}"
    )
