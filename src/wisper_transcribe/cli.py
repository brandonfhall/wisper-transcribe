from __future__ import annotations

import os
import re
import sys
import warnings
from pathlib import Path
from typing import Optional

import click

# Suppress harmless multiline torchcodec/FFmpeg warnings from pyannote on Windows.
# Set WISPER_DEBUG=1 to disable all warning suppression for debugging.
if not os.environ.get("WISPER_DEBUG"):
    warnings.filterwarnings("ignore", module="pyannote.audio.core.io")


def _ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 for every command.

    Legacy console code pages (cp1252, cp437) can't encode the box-drawing and
    arrow characters written via tqdm.write(), which would crash a job that
    otherwise succeeded. Streams that can't be reconfigured (e.g. test
    capture objects) are left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_ensure_utf8_stdio()

from . import __version__
from . import config as _config
from . import file_registry
from .transcript_store import atomic_write_text, save_summary, save_transcript


class _WisperGroup(click.Group):
    """Report database refusals (too-new schema, dev data dir, runtime
    conflict, old SQLite) as a clean error instead of a traceback."""

    def invoke(self, ctx):
        from .db import DatabaseError
        try:
            return super().invoke(ctx)
        except DatabaseError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=_WisperGroup)
@click.version_option(__version__, prog_name="wisper")
def main():
    """wisper-transcribe: Podcast transcription with speaker diarization."""


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_dir", type=click.Path(path_type=Path), default=None, help="Output directory (default: same as input)")
@click.option("-m", "--model", "model_size", default=None, type=click.Choice(_config.MODEL_SIZES), help="Whisper model size (default: from config)")
@click.option("-l", "--language", default=None, help="Language code (e.g. en, fr) or 'auto' for auto-detect (default: from config)")
@click.option("--device", default="auto", show_default=True, type=click.Choice(_config.DEVICES), help="Compute device")
@click.option("--overwrite", is_flag=True, default=False, help="Overwrite existing output files")
@click.option("--timestamps/--no-timestamps", default=None, help="Include timestamps in output (default: from config)")
@click.option("-n", "--num-speakers", default=None, type=int, help="Expected number of speakers (improves diarization)")
@click.option("--min-speakers", default=None, type=int, help="Minimum number of speakers")
@click.option("--max-speakers", default=None, type=int, help="Maximum number of speakers")
@click.option("--no-diarize", is_flag=True, default=False, help="Skip speaker diarization")
@click.option("--enroll-speakers", is_flag=True, default=False, help="Interactively name and enroll detected speakers")
@click.option("--play-audio", is_flag=True, default=False, help="Play each speaker's audio excerpt during enrollment")
@click.option("--compute-type", default="auto", show_default=True,
              type=click.Choice(_config.COMPUTE_TYPES),
              help="CTranslate2 quantization (auto=float16 on CUDA, int8 on CPU)")
@click.option("--vad/--no-vad", default=None,
              help="Voice activity detection to skip silence (default: on, from config)")
@click.option("--forced-align/--no-forced-align", "forced_align", default=None,
              help="Re-time words against the audio before speaker assignment "
                   "(default: from config; auto = on when diarizing on a GPU)")
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
              help="Campaign slug: write into its folder and use its roster")
@click.option("--keep-both", "keep_both", is_flag=True, default=False,
              help="A name already in the campaign: save this run as a new copy")
def transcribe(
    path: Path,
    output_dir: Optional[Path],
    model_size: Optional[str],
    language: Optional[str],
    device: str,
    overwrite: bool,
    timestamps: Optional[bool],
    num_speakers: Optional[int],
    min_speakers: Optional[int],
    max_speakers: Optional[int],
    no_diarize: bool,
    enroll_speakers: bool,
    play_audio: bool,
    compute_type: str,
    vad: Optional[bool],
    forced_align: Optional[bool],
    vocab_file: Optional[Path],
    initial_prompt: Optional[str],
    workers: int,
    verbose: bool,
    debug: bool,
    campaign: Optional[str],
    keep_both: bool,
):
    """Transcribe an audio file (or folder of files) to markdown.

    ``--model``/``--language``/``--timestamps`` default to None ("use config").
    ``--language auto`` is forwarded as the literal ``"auto"`` to request
    auto-detection.
    """
    if debug or verbose:
        from .debug_log import setup_logging
        log_path = setup_logging(debug=debug, verbose=verbose)
        if log_path:
            click.echo(f"  Debug log: {log_path}")

    from .pipeline import process_file, process_folder

    hotwords: Optional[list[str]] = None
    if vocab_file is not None:
        lines = Path(vocab_file).read_text(encoding="utf-8").splitlines()
        hotwords = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    extra: dict = {}
    if campaign:
        extra.update(_campaign_output_args(campaign, output_dir, path, keep_both, overwrite))
    out_dir = extra.pop("output_dir", output_dir)
    out_overwrite = extra.pop("overwrite", overwrite)

    kwargs = dict(
        output_dir=out_dir,
        model_size=model_size,
        device=device,
        language=language,
        include_timestamps=timestamps,
        overwrite=out_overwrite,
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
        forced_alignment=None if forced_align is None else str(forced_align).lower(),
        **extra,
    )

    if path.is_dir():
        click.echo(f"Processing folder: {path}")
        successes, skipped, errors = process_folder(path, verbose=verbose, workers=workers, **kwargs)
        click.echo(f"\nDone. {len(successes)} transcribed, {len(skipped)} skipped, {len(errors)} errors.")
        for err in errors:
            click.echo(f"  ERROR: {err}", err=True)
    else:
        try:
            out = process_file(path, **kwargs)
            click.echo(f"Done: {out}")
        except Exception as e:
            raise click.ClickException(str(e))


def _keep_both_suffix() -> str:
    """The timestamp a CLI ``--keep-both`` copy is named after.

    The run's local start time, e.g. ``2026-10-05 0142``. No colon, which
    Windows forbids; colons separate a time on Windows drives.
    """
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H%M")


def _campaign_output_args(campaign: str, output_dir: Optional[Path], path: Path,
                          keep_both: bool, overwrite: bool) -> dict:
    """Resolve a CLI run's output into a campaign folder, and name clashes.

    ``--campaign`` writes into the campaign's folder when no ``-o`` names
    somewhere else; a name already in the campaign is refused unless
    ``--keep-both`` or ``--overwrite`` is given. Returns the extra kwargs
    (``output_dir``, ``output_stem``, ``overwrite``); an unknown slug or a
    taken folder is a ``ClickException``.
    """
    from . import campaign_folders, db
    from .config import get_output_root
    from .transcript_store import _stem_row, nfc, next_free_stem

    root = get_output_root()
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (campaign,)).fetchone()
    if row is None:
        raise click.ClickException(f"No campaign with slug {campaign!r}. Run `wisper campaigns list`.")
    campaign_id = row[0]

    if output_dir is not None:
        try:
            same = Path(os.path.realpath(output_dir)) == Path(os.path.realpath(root))
        except OSError:
            same = False
        if not same:
            return {}  # -o elsewhere: roster-only, unchanged

    try:
        out = campaign_folders.ensure_folder(campaign_id)
    except campaign_folders.FolderTakenError as exc:
        raise click.ClickException(
            f"The campaign's folder name is taken by another folder: {exc}."
        ) from None
    except FileNotFoundError:
        raise click.ClickException("The transcripts folder isn't available.")

    source = path if path.is_file() else None
    stem = nfc(source.stem) if source is not None else None
    result: dict = {"output_dir": out}
    if stem is None:
        # A folder run names each member after its input file; process_file
        # skips (or overwrites) an existing output there.
        return result

    with db.connection() as conn:
        same = _stem_row(conn, campaign_id, stem)
    taken = same is not None
    md = out / f"{stem}.md"
    flac = out / f"{stem}.flac"
    if not (taken or md.is_file() or flac.is_file()):
        return result
    if keep_both:
        suffix = _keep_both_suffix()
        room = max(1, 100 - len(suffix) - 3)
        named = f"{stem[:room].rstrip()} ({suffix})"
        result["output_stem"] = next_free_stem(out, named, campaign_id)
        return result
    if overwrite:
        from .transcript_store import locate
        loc = locate(same[0]) if taken else None
        if loc is not None:
            # Reuse the row: write where its .md is (a misplaced session's is
            # still in the root), under its current name.
            result["output_dir"] = loc.dir
            result["output_stem"] = loc.stem
        else:
            result["output_stem"] = stem  # an unregistered file of that name
        return result
    raise click.ClickException(
        f"{stem!r} is already in the campaign. Pass --keep-both to save a new "
        "copy, or --overwrite to replace it."
    )


def _audio_extensions():
    from .audio_utils import SUPPORTED_EXTENSIONS
    return SUPPORTED_EXTENSIONS


@main.command()
# Default to loopback: the web UI has no auth or CSRF protection. Use
# --host 0.0.0.0 only on trusted networks (Docker passes it explicitly).
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind host (use 0.0.0.0 to expose on the network — no auth, trusted networks only)")
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
    # The lock is taken here, in the parent, before the first connect(): a
    # --reload worker can release a dead process's lock late on Windows, and
    # a trim in progress must stop the server before it migrates anything.
    from . import db
    lock = db.ServerLock()
    try:
        lock.acquire()
    except db.ServerLockHeld:
        raise click.ClickException(
            "wisper storage trim is running; try again when it finishes"
        )
    try:
        # Fail before serving if the database can't be used (the lifespan checks
        # again for `uvicorn` launched directly and --reload subprocesses).
        # This connect migrates, so the lifespan sees no upgrade: report it here.
        before = db.schema_version()
        db.connect().close()
        if 0 < before < db.LATEST_VERSION:
            from .web.app import _report_upgrade
            _report_upgrade(before)

        # Publish bind address so app.py can write server.json for CLI discovery.
        os.environ["WISPER_BIND"] = f"{host}:{port}"

        click.echo(f"Starting wisper web UI on http://{host}:{port}")
        click.echo("Press Ctrl+C to stop.")
        uvicorn.run(
            "wisper_transcribe.web.app:app",
            host=host,
            port=port,
            reload=reload,
            access_log=False,
        )
    finally:
        lock.release()


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
        from .transcriber import _is_mlx_available
        if _is_mlx_available():
            click.echo("   Note: transcription uses MLX; diarization and word alignment use MPS")
        else:
            click.echo("   Note: transcription uses CPU (install the [macos] extra for MLX); "
                       "diarization and word alignment use MPS")

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
        click.echo("   You must also accept the model license (free, one-time):")
        click.echo(f"     https://huggingface.co/{_config.DIARIZATION_MODEL}")
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
            from pyannote.audio import Pipeline

            click.echo(f"   Downloading {_config.DIARIZATION_MODEL} ...")
            pipeline = Pipeline.from_pretrained(_config.DIARIZATION_MODEL, token=token)
            del pipeline
            click.echo("   OK  : all models cached — subsequent runs start immediately")
        except Exception as e:
            click.echo(f"   WARN: model download failed: {e}", err=True)
            click.echo("   Models will download automatically on first transcription run.")

    cfg = _config.load_config()
    if _config.forced_alignment_enabled(cfg.get("forced_alignment", "auto"), _config.get_device()):
        click.echo(f"\n>> Pre-downloading the word alignment model ({_config.FORCED_ALIGNMENT_MODEL}, ~1.7 GB)...")
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(_config.FORCED_ALIGNMENT_MODEL)
            click.echo("   OK  : alignment model cached")
        except Exception as e:
            click.echo(f"   WARN: alignment model download failed: {e}", err=True)
            click.echo("   It will download automatically on first transcription run.")

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
            from .config import _LLM_DEFAULT_ENDPOINTS, _LLM_DEFAULT_MODELS
            suggested_model = config.get("llm_model", "") or _LLM_DEFAULT_MODELS.get(provider, "")

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
                from .config import _LLM_API_KEY_ENV
                env_name, config_key = _LLM_API_KEY_ENV[provider]
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
    click.echo(f"  Diarization    : {_config.DIARIZATION_MODEL}")
    click.echo(f"  Embedding      : {_config.DIARIZATION_MODEL} ({_config.EMBEDDING_SUBFOLDER}/)")
    click.echo("")
    click.echo("─" * 50)
    click.echo("Settings")
    click.echo("─" * 50)
    from .config import LLM_SECRET_KEYS
    secret_keys = {"hf_token", "discord_bot_token"} | set(LLM_SECRET_KEYS)
    for k, v in cfg.items():
        display = "***" if k in secret_keys and v else repr(v)
        click.echo(f"  {k:<22} = {display}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a configuration value."""
    from .config import CONFIG_CHOICES, DEFAULTS, load_config, save_config

    if key not in DEFAULTS:
        raise click.ClickException(
            f"Unknown config key {key!r}; run `wisper config show` to list keys."
        )
    if key in CONFIG_CHOICES and value not in CONFIG_CHOICES[key]:
        raise click.ClickException(
            f"Invalid value {value!r} for {key}; choose from: {', '.join(CONFIG_CHOICES[key])}."
        )

    cfg = load_config()
    # Coerce to DEFAULTS[key]'s type (not the stored value's, which may be
    # wrong). bool first: bool is a subclass of int.
    schema_value = DEFAULTS[key]
    coerced: object
    if isinstance(schema_value, bool):
        coerced = value.lower() in ("true", "1", "yes")
    elif isinstance(schema_value, int):
        coerced = int(value)
    elif isinstance(schema_value, float):
        coerced = float(value)
    elif isinstance(schema_value, list):
        # Accept comma-separated input: "Kyra, Golarion, Zeldris" → ["Kyra", "Golarion", "Zeldris"]
        coerced = [w.strip() for w in value.split(",") if w.strip()]
    elif key == "output_dir" and value.strip():
        # Store an absolute path so the root doesn't depend on the CWD.
        coerced = str(Path(value.strip()).expanduser().resolve())
    else:
        coerced = value
    cfg[key] = coerced
    save_config(cfg)
    click.echo(f"Set {key} = {coerced!r}")


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

    from .config import _LLM_DEFAULT_ENDPOINTS, _LLM_DEFAULT_MODELS
    suggested_model = cfg.get("llm_model", "") or _LLM_DEFAULT_MODELS.get(provider, "")

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
        from .config import _LLM_API_KEY_ENV
        env_name, config_key = _LLM_API_KEY_ENV[provider]
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
    try:
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
    finally:
        # Delete the converted temp WAV (no-op when `audio` was already one).
        if wav_path != audio:
            wav_path.unlink(missing_ok=True)


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


@speakers.command("doctor")
def speakers_doctor():
    """Report likely problems with enrolled profiles. Changes nothing.

    Flags pairs of profiles that sound like the same person, profiles from an
    older speaker model, and profiles named like a placeholder.
    """
    from .speaker_manager import (
        DUPLICATE_SIMILARITY, find_duplicate_profiles, load_profiles,
        placeholder_name_profiles, stale_profile_keys,
    )

    profiles = load_profiles()
    if not profiles:
        click.echo("No speakers enrolled.")
        return

    problems = 0
    duplicates = find_duplicate_profiles(profiles)
    if duplicates:
        problems += len(duplicates)
        click.echo(f"Likely duplicates (voice similarity above {DUPLICATE_SIMILARITY:.2f}):")
        for a, b, sim in duplicates:
            click.echo(f"  {profiles[a].display_name} ({a}) ≈ {profiles[b].display_name} ({b})  {sim:.2f}")
        click.echo("  Keep one: wisper speakers remove <name>, then rename the other if needed.")

    stale = stale_profile_keys(profiles)
    if stale:
        problems += len(stale)
        click.echo("Enrolled with an older speaker model, or no voice sample (never matched):")
        for key in stale:
            click.echo(f"  {profiles[key].display_name} ({key})")
        click.echo("  Re-enroll them: wisper enroll <name> --audio <file>")

    placeholders = placeholder_name_profiles(profiles)
    if placeholders:
        problems += len(placeholders)
        click.echo("Named like a placeholder (probably enrolled by accident):")
        for key in placeholders:
            click.echo(f"  {profiles[key].display_name} ({key})")
        click.echo("  Remove or rename: wisper speakers remove <name> / wisper speakers rename <old> <new>")

    if problems == 0:
        click.echo(f"No problems found in {len(profiles)} profile(s).")


@speakers.command("remove")
@click.argument("name")
def speakers_remove(name: str):
    """Remove an enrolled speaker profile."""
    # Shared with the web remove route (one transaction, then the .mp3 clip).
    from .speaker_manager import remove_profile

    key = name.lower().replace(" ", "_")
    try:
        remove_profile(key)
    except KeyError:
        raise click.ClickException(f"Speaker {name!r} not found.")
    click.echo(f"Removed speaker {name!r}.")


@speakers.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def speakers_rename(old_name: str, new_name: str):
    """Rename an enrolled speaker."""
    # Shared with the web rename route (memberships follow the profile id).
    from .speaker_manager import rename_profile

    old_key = old_name.lower().replace(" ", "_")
    try:
        rename_profile(old_key, new_name)
    except KeyError:
        raise click.ClickException(f"Speaker {old_name!r} not found.")
    except ValueError as e:
        if "exists" in str(e):
            raise click.ClickException(f"Speaker {new_name!r} already exists.")
        raise click.ClickException(f"Invalid speaker name: {new_name!r}")
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
    try:
        diarization = diarize(wav_path, hf_token=hf_token, device=device, num_speakers=num_speakers)

        profile_filter = None
        if campaign:
            from .campaign_manager import _validate_campaign_slug, get_campaign_profile_keys
            safe = _validate_campaign_slug(campaign)
            if safe is None:
                raise click.ClickException(f"Invalid campaign slug: {campaign!r}")
            profile_filter = get_campaign_profile_keys(safe)
            click.echo(f"  Campaign filter: {safe} ({len(profile_filter)} member(s))")

        allow_many_to_one = num_speakers is None
        scores: dict[str, tuple[str, float]] = {}
        matches = match_speakers(wav_path, diarization, device=device,
                                 threshold=config.get("similarity_threshold", _config.DEFAULT_SIMILARITY_THRESHOLD),
                                 profile_filter=profile_filter,
                                 allow_many_to_one=allow_many_to_one,
                                 scores=scores)

        if not matches:
            click.echo("No enrolled profiles to match against.")
            return

        if allow_many_to_one:
            click.echo("  (num_speakers not pinned — many-to-one matching enabled)")

        from .pipeline import _score_note
        for label, name in sorted(matches.items()):
            click.echo(f"  {label} → {name}{_score_note(name, scores.get(label))}")
    finally:
        # Delete the converted temp WAV, as in `enroll`.
        if wav_path != audio:
            wav_path.unlink(missing_ok=True)


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

# --provider choices for LLM commands, derived from config.LLM_PROVIDERS.
# Must be defined above its first use: decorators run at import time.
def _llm_provider_choice() -> click.Choice:
    from .config import LLM_PROVIDERS
    return click.Choice(LLM_PROVIDERS)


_LLM_PROVIDER_CHOICE = _llm_provider_choice()

# One table for campaign name/delete refusals, shared with the web's ?error= codes.
_CAMPAIGN_CODE_MESSAGES = {
    "invalid": "Enter a campaign name.",
    "slug_taken": "A campaign with that name already exists.",
    "taken": "A campaign with that name already exists.",
    "folder_exists": "A folder with that name already exists in your transcripts folder.",
    "folder_taken": "A folder with that name already exists in your transcripts folder.",
    "busy": "A job is running for this campaign; try again when it finishes.",
    "pending": "The last rename of this campaign's folder hasn't finished; "
               "Retry it under Needs attention first.",
    "folder_missing": "The campaign's folder is missing from your transcripts folder; "
                      "see Needs attention.",
    "reserved": "A session in this campaign is named like the renamed journal; "
                "rename that session first.",
    "legacy_journal": "This campaign's old journal is waiting to be moved; see Needs attention.",
    "delete_incomplete": "A new session appeared in the campaign while it was being deleted; "
                         "nothing else was removed.",
}


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
    from .campaign_manager import CampaignError, create_campaign

    try:
        campaign = create_campaign(display_name)
    except CampaignError as exc:
        raise click.ClickException(_CAMPAIGN_CODE_MESSAGES.get(exc.code, str(exc)))
    except ValueError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"Created campaign {campaign.display_name!r} (slug: {campaign.slug}) "
               f"(folder: {campaign.folder})")


@campaigns.command("delete")
@click.argument("slug")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt")
@click.option("--delete-transcripts", is_flag=True, default=False,
              help="Also delete the campaign's transcripts, their files, and its journal")
def campaigns_delete(slug: str, yes: bool, delete_transcripts: bool):
    """Delete a campaign. Does not affect enrolled speaker profiles.

    By default the campaign's transcripts and journal file stay.
    """
    from .campaign_manager import _validate_campaign_slug, delete_campaign

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")

    if not yes:
        what = ("and delete its transcripts, their files, and its journal"
                if delete_transcripts else "and move its transcripts to the output root")
        click.confirm(f"Delete campaign {safe!r} {what}?", abort=True)

    try:
        outcome = delete_campaign(safe, delete_transcripts=delete_transcripts)
    except KeyError:
        raise click.ClickException(f"Campaign {safe!r} not found.")

    if outcome.status == "deleted":
        click.echo(f"Deleted campaign {safe!r}.")
        return
    if outcome.status == "kept":
        names = ", ".join(outcome.kept)
        raise click.ClickException(
            f"Campaign {safe!r} was kept: {len(outcome.kept)} session(s) didn't "
            f"{'delete' if delete_transcripts else 'move'} ({names}). "
            "Close the file in another program and retry."
        )
    if outcome.status == "busy":
        raise click.ClickException(_CAMPAIGN_CODE_MESSAGES["busy"])
    raise click.ClickException(_CAMPAIGN_CODE_MESSAGES["delete_incomplete"])


@campaigns.command("rename")
@click.argument("slug")
@click.argument("new_name")
def campaigns_rename(slug: str, new_name: str):
    """Rename a campaign: its display name, slug, folder, and journal file."""
    from .campaign_manager import _validate_campaign_slug, load_campaigns
    from .campaign_folders import rename_campaign

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")

    try:
        outcome = rename_campaign(safe, new_name)
    except KeyError:
        raise click.ClickException(f"Campaign {safe!r} not found.")

    if outcome.status == "renamed":
        campaign = load_campaigns().get(outcome.new_slug)
        if campaign is not None:
            click.echo(f"Renamed campaign {safe!r} to {campaign.display_name!r} "
                       f"(slug: {campaign.slug}, folder: {campaign.folder}).")
            return
    if outcome.status == "pending":
        click.echo(_CAMPAIGN_CODE_MESSAGES["pending"])
        return
    raise click.ClickException(_CAMPAIGN_CODE_MESSAGES.get(outcome.status, outcome.status))


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
    from . import db
    with db.connection() as conn:
        pending = conn.execute(
            "SELECT folder_pending FROM campaigns WHERE id = ?", (campaign.id,)
        ).fetchone()
    click.echo(f"Folder:   {campaign.folder}")
    if pending is not None and pending[0] is not None:
        click.echo(f"Rename pending → {pending[0]}")
    from .journal import journal_path, journal_stale_since, sync_journal
    sync_journal(safe)
    jpath = journal_path(safe)
    if jpath is not None and jpath.exists():
        stale = journal_stale_since(safe)
        if stale:
            click.echo(f"Journal:  STALE since {stale} — it mentions sessions that were moved, "
                       "removed, or re-transcribed.")
            click.echo(f"          Rebuild it: wisper campaigns journal {safe} --rebuild")
        else:
            click.echo("Journal:  up to date")
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


@campaigns.command("reorder")
@click.argument("slug")
@click.argument("stem", required=False, default=None)
@click.option("--up", "move_up", is_flag=True, default=False,
              help="Move STEM one position earlier")
@click.option("--down", "move_down", is_flag=True, default=False,
              help="Move STEM one position later")
@click.option("--set", "set_order_raw", default=None,
              help="Replace the whole order in one shot: comma-separated list "
                   "of every transcript stem in the campaign, in the desired order")
def campaigns_reorder(slug: str, stem: Optional[str], move_up: bool, move_down: bool,
                      set_order_raw: Optional[str]):
    """Reorder a campaign's transcripts.

    This is the order sessions get folded into the rolling journal in
    (`wisper campaigns journal`) — not necessarily the order they were
    recorded, since it tracks when a transcript was associated with the
    campaign, not any date parsed from its filename.

    \b
    wisper campaigns reorder d-d-mondays s02 --up
    wisper campaigns reorder d-d-mondays --set "s01,s02,s03"
    """
    from .campaign_manager import (
        _validate_campaign_slug, get_transcripts_for_campaign, load_campaigns,
        reorder_campaign_transcript, set_campaign_transcript_order,
    )

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")
    if safe not in load_campaigns():
        raise click.ClickException(f"Campaign {safe!r} not found.")

    if set_order_raw is not None:
        if stem is not None or move_up or move_down:
            raise click.ClickException("--set cannot be combined with STEM/--up/--down.")
        order = [s.strip() for s in set_order_raw.split(",") if s.strip()]
        try:
            set_campaign_transcript_order(safe, order)
        except ValueError as exc:
            raise click.ClickException(str(exc))
    else:
        if move_up == move_down:
            raise click.ClickException("Pass exactly one of --up or --down (or use --set).")
        if not stem:
            raise click.ClickException("STEM is required with --up/--down.")
        try:
            reorder_campaign_transcript(safe, stem, "up" if move_up else "down")
        except ValueError as exc:
            raise click.ClickException(str(exc))

    click.echo(f"Transcript order for {safe!r}:")
    for i, s in enumerate(get_transcripts_for_campaign(safe), 1):
        marker = " <-" if s == stem else ""
        click.echo(f"  {i}. {s}{marker}")


@campaigns.command("relabel")
@click.argument("slug")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would change without writing anything")
@click.option("--no-backfill", is_flag=True, default=False,
              help="Only use voice data already stored with each transcript; "
                   "don't re-read source audio for older ones")
@click.option("--device", default="auto", type=click.Choice(_config.DEVICES),
              help="Device for extracting voice data from source audio")
def campaigns_relabel(slug: str, dry_run: bool, no_backfill: bool, device: str):
    """Re-match automatically named speakers across a campaign's sessions.

    Every web-transcribed session in the campaign is matched against the
    roster again, and unknown voices heard in two or more sessions get one
    shared "Recurring Speaker N" name. Names you set by hand are never changed.
    """
    from .campaign_manager import _validate_campaign_slug, load_campaigns
    from .config import get_device
    from .speaker_registry import relabel_campaign

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")
    if safe not in load_campaigns():
        raise click.ClickException(f"Campaign {safe!r} not found.")
    if device == "auto":
        device = get_device()

    report = relabel_campaign(safe, device=device, backfill=not no_backfill,
                              dry_run=dry_run, progress=click.echo)
    changed = sum(len(t.renamed) for t in report.transcripts)
    verb = "Would rename" if dry_run else "Renamed"
    click.echo(f"{verb} {changed} speaker label(s) across {len(report.transcripts)} session(s).")
    if report.recurring:
        click.echo(f"Unknown voices heard in more than one session: {report.recurring}")
    for t in report.transcripts:
        if t.skipped:
            click.echo(f"  Skipped {t.stem}: {t.skipped}")


@campaigns.command("journal")
@click.argument("slug")
@click.option("--session", default=None,
              help="Specific session stem to fold (default: next unjournalled)")
@click.option("--all", "fold_all", is_flag=True, default=False,
              help="Fold every pending session in one run (oldest first)")
@click.option("--rebuild", is_flag=True, default=False,
              help="Start the journal over from each session's existing "
                   "summary (one LLM call per session; summaries and your "
                   "edits to them are kept). Asks for confirmation unless "
                   "--yes is also passed.")
@click.option("--resummarize", is_flag=True, default=False,
              help="With --rebuild: re-summarize every session transcript "
                   "first, overwriting the summaries (two LLM calls per session).")
@click.option("--export", "export", is_flag=True, default=False,
              help="Print the journal with its folded-session list in the "
                   "frontmatter (no LLM call). Use -o to write a file.")
@click.option("-o", "--output", "output", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="With --export: write to this file")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the --rebuild confirmation prompt")
@click.option("--provider", default=None, type=_LLM_PROVIDER_CHOICE,
              help="LLM provider (default: llm_provider from config)")
@click.option("--model", default=None, help="Model override (default: llm_model from config)")
@click.option("--endpoint", default=None, help="Ollama endpoint override")
def campaigns_journal(slug: str, session: Optional[str], fold_all: bool,
                      rebuild: bool, resummarize: bool, export: bool,
                      output: Optional[Path], yes: bool,
                      provider: Optional[str], model: Optional[str],
                      endpoint: Optional[str]):
    """Fold session summaries into a rolling campaign journal.

    The journal is a single living document at
    ``campaigns/<slug>/journal.md`` that the LLM rewrites as each new session
    is folded in. With no flags it folds the next unjournalled session (one
    that has a ``.summary.md`` from `wisper summarize`). Pass ``--all`` to fold
    every pending session, ``--session <stem>`` to fold a specific one,
    ``--rebuild`` to start the journal over from the existing summaries
    (add ``--resummarize`` to re-summarize the transcripts first), or
    ``--export`` to print it with its folded-session list.
    """
    from .campaign_manager import _validate_campaign_slug, get_transcripts_for_campaign, load_campaigns
    from .journal import (
        export_journal, rebuild_campaign, refold_campaign, unjournalled_sessions, update_journal,
    )
    from .llm.errors import LLMResponseError, LLMUnavailableError
    from .speaker_manager import load_profiles

    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise click.ClickException(f"Invalid campaign slug: {slug!r}")
    if safe not in load_campaigns():
        raise click.ClickException(f"Campaign {safe!r} not found.")

    exclusive = [session is not None, fold_all, rebuild, export]
    if sum(exclusive) > 1:
        raise click.ClickException("--session, --all, --rebuild, and --export are mutually exclusive.")
    if resummarize and not rebuild:
        raise click.ClickException("--resummarize only applies with --rebuild.")
    if output is not None and not export:
        raise click.ClickException("-o/--output only applies with --export.")

    if export:
        text = export_journal(safe)
        if text is None:
            raise click.ClickException(f"Campaign {safe!r} has no journal yet.")
        if output is None:
            click.echo(text, nl=False)
        else:
            output.write_bytes(text.encode("utf-8"))
            click.echo(f"Wrote {output}")
        return

    if rebuild:
        stems = get_transcripts_for_campaign(safe)
        transcript_count = len(stems)
        if transcript_count == 0:
            click.echo(f"Campaign {safe!r} has no transcripts to rebuild from.")
            return
        if resummarize:
            calls = transcript_count * 2
            question = (f"Rebuild {safe!r} from transcripts: re-summarize all {transcript_count} "
                        f"session(s), overwriting their summaries, and regenerate the journal "
                        f"from scratch? This is {calls} LLM calls.")
        else:
            from .transcript_store import find_by_stem
            campaign_id = load_campaigns()[safe].id
            unsummarized = sum(
                1 for st in stems
                if not any(loc.companion(".summary.md").exists()
                           for loc in find_by_stem(st, campaign_id=campaign_id))
            )
            calls = transcript_count + unsummarized
            extra = f" ({unsummarized} need a summary first)" if unsummarized else ""
            question = (f"Rebuild {safe!r}: start the journal over from the {transcript_count} "
                        f"sessions' existing summaries{extra}? About {calls} LLM calls.")
        if not yes:
            click.confirm(question, abort=True)
        client = _get_llm_client(provider, model, endpoint)
        click.echo(f"Rebuilding {safe!r} with {client.provider} / {client.model} "
                   f"({transcript_count} session(s)) ...", err=True)
        rebuild_fn = rebuild_campaign if resummarize else refold_campaign
        result = rebuild_fn(
            safe, client, load_profiles(), data_dir=None,
            on_progress=lambda msg: click.echo(f"  {msg}", err=True),
        )
        click.echo(f"Summarized: {len(result.resummarized)}")
        if result.skipped:
            click.echo(f"Skipped: {len(result.skipped)}")
            for stem, reason in result.skipped:
                click.echo(f"  {stem}: {reason}")
        if result.journal is not None:
            click.echo(f"Wrote {result.journal.path}")
            click.echo(f"  journaled sessions: {len(result.journal.journaled_sessions)}")
        return

    # Decide the work list up front so we can report 'nothing to do' cleanly.
    if session:
        targets = [session]
    else:
        targets = unjournalled_sessions(safe)
        if not targets:
            click.echo("Journal is already up to date — no sessions to fold.")
            click.echo("  (Sessions need a .summary.md first: run `wisper summarize`.)")
            return
        if not fold_all:
            targets = targets[:1]

    client = _get_llm_client(provider, model, endpoint)
    click.echo(f"Folding {len(targets)} session(s) with "
               f"{client.provider} / {client.model} ...", err=True)

    result = None
    for stem in targets:
        click.echo(f"  Folding in: {stem}", err=True)
        try:
            result = update_journal(safe, client, load_profiles(), session_stem=stem)
        except FileNotFoundError as exc:
            raise click.ClickException(str(exc))
        except (LLMUnavailableError, LLMResponseError) as exc:
            raise click.ClickException(str(exc))

    if result is not None:
        click.echo(f"Wrote {result.path}")
        click.echo(f"  journaled sessions: {len(result.journaled_sessions)}")


# ---------------------------------------------------------------------------
# wisper transcripts
# ---------------------------------------------------------------------------


@main.group()
def transcripts():
    """Manage transcripts — list, move between campaigns, or rename."""


@transcripts.command("list")
@click.option("--campaign", default=None, help="Show only transcripts for this campaign slug")
def transcripts_list(campaign: Optional[str]):
    """List transcripts, grouped by campaign."""
    from wisper_transcribe.campaign_manager import load_campaigns, _validate_campaign_slug
    from wisper_transcribe.path_utils import get_output_dir

    if campaign:
        safe = _validate_campaign_slug(campaign)
        if safe is None:
            raise click.ClickException("Invalid campaign slug")

    out_dir = get_output_dir()
    from . import campaign_folders
    from .transcript_store import list_transcripts, reconcile
    campaign_folders.finish_pending_renames()  # before scanning the folders
    reconcile(out_dir)  # register new files, flag ones deleted outside wisper

    # Present sessions from the database, newest first (same order as the page).
    present_locs = list_transcripts(output_dir=out_dir)
    all_stems = list(dict.fromkeys(loc.stem for loc in present_locs))
    present = set(all_stems)

    campaigns = load_campaigns()

    # Build stem → campaign slug mapping
    stem_to_campaign: dict[str, str] = {}
    for slug, c in campaigns.items():
        for stem in c.transcripts:
            stem_to_campaign[stem] = slug

    def _label(stem: str) -> str:
        return stem if stem in present else f"{stem}  (missing — file not found)"

    def _attention_note() -> None:
        from .transcript_store import needs_attention
        total = needs_attention(out_dir).total
        if total:
            noun = "item needs" if total == 1 else "items need"
            click.echo(f"\n{total} {noun} attention; see the Transcripts page or `wisper storage trim`")

    if campaign:
        # Campaign order, including entries whose file is missing.
        stems = campaigns[campaign].transcripts if campaign in campaigns else []
        if not stems:
            click.echo(f"No transcripts found for campaign {campaign!r}.")
        for stem in stems:
            click.echo(_label(stem))
        _attention_note()
        return

    # Grouped output, each campaign in its fold order
    printed_any = False
    for slug, c in campaigns.items():
        if not c.transcripts:
            continue
        click.echo(f"\n📁 {c.display_name} [{slug}]")
        for stem in c.transcripts:
            click.echo(f"   {_label(stem)}")
        printed_any = True

    uncampaigned = [s for s in all_stems if s not in stem_to_campaign]
    if uncampaigned:
        if printed_any:
            click.echo("\n(no campaign)")
        for stem in uncampaigned:
            click.echo(f"   {stem}")
    elif not printed_any:
        click.echo("No transcripts found.")
    _attention_note()


def _find_transcript(name: str, from_slug: Optional[str], *, exclude_slug: Optional[str] = None,
                     data_dir=None):
    """One :class:`Located` named ``name``, narrowed by ``from_slug``.

    A name in several campaigns is refused unless ``--from`` (the source
    campaign) picks one; for a move, the target campaign is dropped from the
    candidates first, so a name already in the target identifies the session
    coming in. The error names the campaigns.
    """
    from . import db
    from wisper_transcribe.campaign_manager import _validate_campaign_slug
    from .transcript_store import find_by_stem

    def campaign_id(slug: str) -> int:
        safe = _validate_campaign_slug(slug)
        if safe is None:
            raise click.ClickException("Invalid campaign slug")
        with db.connection(data_dir) as conn:
            row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()
        if row is None:
            raise click.ClickException(f"Campaign {safe!r} not found.")
        return row[0]

    if from_slug:
        found = find_by_stem(name, campaign_id=campaign_id(from_slug), data_dir=data_dir)
    else:
        found = find_by_stem(name, data_dir=data_dir)

    if not found:
        raise click.ClickException(
            f"No transcript named {name!r}. Run `wisper transcripts list`; a file just added "
            "is picked up by the next scan."
        )
    if len(found) > 1 and exclude_slug:
        excluded = campaign_id(exclude_slug)
        remaining = [loc for loc in found if loc.campaign_id != excluded]
        if remaining:
            found = remaining
    if len(found) > 1:
        with db.connection(data_dir) as conn:
            slugs = [conn.execute("SELECT slug FROM campaigns WHERE id = ?",
                                  (loc.campaign_id,)).fetchone() for loc in found]
        places = sorted(r[0] if r else "no campaign" for r in slugs)
        raise click.ClickException(
            f"{name!r} is in several campaigns ({', '.join(places)}); pass --from <slug>."
        )
    return found[0]


def _clash_flag(keep_both: bool, overwrite: bool) -> str:
    if keep_both and overwrite:
        raise click.ClickException("Pass only one of --keep-both or --overwrite.")
    return "keep_both" if keep_both else "overwrite" if overwrite else "ask"


def _report_move(outcome, stem: str, target_slug: Optional[str]) -> None:
    """Print a :class:`MoveOutcome` and exit 1 for the statuses that failed."""
    where = f"campaign {target_slug!r}" if target_slug else "the transcripts folder"
    if outcome.status == "moved":
        click.echo(f"Moved {stem!r} to {where}.")
    elif outcome.status == "unchanged":
        click.echo(f"{stem!r} is already there.")
    elif outcome.status == "partial":
        names = ", ".join(p.name for p in outcome.kept)
        click.echo(f"Moved {stem!r} to {where}; kept in place: {names}.")
    elif outcome.status == "invalid":
        raise click.ClickException(f"{stem!r} is not a valid session name.")
    elif outcome.status == "busy":
        ids = ", ".join(outcome.busy)
        raise click.ClickException(
            f"A job is running for this session ({ids}). If no wisper server is running, these "
            "are left over from a crash: start the server once to clear them."
        )
    elif outcome.status == "folder_taken":
        raise click.ClickException(
            f"The campaign's folder isn't available ({outcome.detail or 'folder taken'})."
        )
    elif outcome.status == "unavailable":
        raise click.ClickException("The transcripts folder isn't available.")
    elif outcome.status == "clash":
        when = f" (last modified {outcome.clash_modified})" if outcome.clash_modified else ""
        raise click.ClickException(
            f"A file with that name is already there{when}. Use --keep-both or --overwrite."
        )
    elif outcome.status == "reserved":
        raise click.ClickException("That name is the campaign journal's.")
    elif outcome.status == "locked":
        raise click.ClickException("A file is open in another program; nothing was moved.")
    else:
        raise click.ClickException(f"Could not complete the move ({outcome.status}).")


@transcripts.command("move")
@click.argument("stem")
@click.option("--campaign", default=None, help="Campaign slug to move the session into")
@click.option("--no-campaign", "unlink", is_flag=True, default=False,
              help="Move the session to the transcripts folder root")
@click.option("--from", "from_slug", default=None,
              help="Disambiguate a name that is in several campaigns")
@click.option("--keep-both", is_flag=True, default=False,
              help="On a name clash, save the moved session as '<name> (2)'")
@click.option("--overwrite", is_flag=True, default=False,
              help="On a name clash, replace the existing session")
def transcripts_move(stem: str, campaign: Optional[str], unlink: bool, from_slug: Optional[str],
                      keep_both: bool, overwrite: bool):
    """Move a transcript to a campaign, or out of all of them, moving its files."""
    from wisper_transcribe.campaign_manager import _validate_campaign_slug
    from .transcript_store import move_transcript

    if unlink and campaign:
        raise click.ClickException("Pass only one of --campaign or --no-campaign.")
    if not unlink and not campaign:
        raise click.ClickException("Provide --campaign <slug> or --no-campaign")
    clash = _clash_flag(keep_both, overwrite)

    target = None
    if campaign:
        target = _validate_campaign_slug(campaign)
        if target is None:
            raise click.ClickException("Invalid campaign slug")
    loc = _find_transcript(stem, from_slug, exclude_slug=target)
    outcome = move_transcript(loc.id, target, clash=clash)
    _report_move(outcome, loc.stem, target)


@transcripts.command("rename")
@click.argument("name")
@click.argument("new_name")
@click.option("--campaign", default=None, help="Disambiguate a name that is in several campaigns")
@click.option("--keep-both", is_flag=True, default=False,
              help="On a name clash, use the next free '<new_name> (2)'")
@click.option("--overwrite", is_flag=True, default=False,
              help="On a name clash, replace the existing session")
def transcripts_rename(name: str, new_name: str, campaign: Optional[str],
                       keep_both: bool, overwrite: bool):
    """Rename a transcript and its companion files."""
    from .transcript_store import rename_transcript

    clash = _clash_flag(keep_both, overwrite)
    loc = _find_transcript(name, campaign)
    outcome = rename_transcript(loc.id, new_name, clash=clash)
    if outcome.status == "moved":
        click.echo(f"Renamed {loc.stem!r} to {outcome.new_stem!r}.")
        return
    if outcome.status == "unchanged":
        click.echo(f"{loc.stem!r} is already named that.")
        return
    if outcome.status == "partial":
        names = ", ".join(p.name for p in outcome.kept)
        click.echo(f"Renamed {loc.stem!r} to {outcome.new_stem!r}; kept in place: {names}.")
        return
    _report_move(outcome, new_name, campaign)


# ---------------------------------------------------------------------------
# wisper fix
# ---------------------------------------------------------------------------

@main.command()
@click.argument("transcript", type=click.Path(exists=True, path_type=Path))
@click.option("--speaker", required=True, help="Current speaker name to replace")
@click.option("--name", "new_name", required=True, help="Correct name")
@click.option("--re-enroll", is_flag=True, default=False, help="Print the command that re-enrolls the voice from the original audio")
def fix(transcript: Path, speaker: str, new_name: str, re_enroll: bool):
    """Fix a speaker name in an existing transcript."""
    from .formatter import update_speaker_names

    content = transcript.read_text(encoding="utf-8")
    updated = update_speaker_names(content, speaker, new_name)
    save_transcript(transcript, updated)
    click.echo(f"Updated {transcript.name}: {speaker!r} → {new_name!r}")

    if re_enroll:
        click.echo("To re-enroll the voice, run: "
                   "wisper enroll <name> --audio <original_file> --update")


# ---------------------------------------------------------------------------
# wisper refine  /  wisper summarize
# ---------------------------------------------------------------------------

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
    if endpoint and effective_provider in ("ollama", "lmstudio"):
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
    # Character names come from profile.notes (comma- or semicolon-separated).
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
    atomic_write_text(backup, original)
    file_registry.add_if_owned(
        backup, kind="backup", owner=file_registry.Owner.for_path(transcript))
    save_transcript(transcript, refined_md)
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
            atomic_write_text(backup, current_md)
            file_registry.add_if_owned(
                backup, kind="backup", owner=file_registry.Owner.for_path(transcript))
            save_transcript(transcript, refined_md)
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
    save_summary(out_path, body)
    click.echo(f"Wrote {out_path}")
    click.echo(
        f"  sections: {', '.join(sections)} | "
        f"loot: {len(note.loot)} | npcs: {len(note.npcs)} | "
        f"follow-ups: {len(note.followups)} | "
        f"unresolved speakers: {len(note.unresolved_speakers)}"
    )


# ---------------------------------------------------------------------------
# wisper record — Discord recording bot CLI client
# ---------------------------------------------------------------------------

def _get_server_url() -> str:
    """Return the URL of the running wisper server.

    Priority: WISPER_SERVER_URL env var → data_dir/server.json.
    Raises click.ClickException with a friendly message if neither is set.
    """
    env_url = os.environ.get("WISPER_SERVER_URL", "").strip()
    if env_url:
        return env_url

    from .config import get_data_dir
    server_json = get_data_dir() / "server.json"
    if not server_json.exists():
        raise click.ClickException(
            "wisper server is not running — start it with `wisper server` and try again.\n"
            "  (Or set WISPER_SERVER_URL to override.)"
        )
    try:
        import json as _json
        data = _json.loads(server_json.read_text(encoding="utf-8"))
        return data["url"]
    except Exception as exc:
        raise click.ClickException(f"Could not read server.json: {exc}")


def _record_request(method: str, path: str, timeout: float = 10, **kwargs) -> dict:
    """Make an HTTP request to the running wisper server. Returns parsed JSON."""
    import httpx
    url = _get_server_url().rstrip("/") + path
    try:
        resp = httpx.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        raise click.ClickException(
            "Could not connect to wisper server. Is it still running?"
        )
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200]
        raise click.ClickException(f"Server returned {exc.response.status_code}: {body}")


@main.group()
def record():
    """Control the Discord recording bot."""


@record.command("start")
@click.option("--campaign", default=None, help="Campaign slug to associate the recording with")
@click.option("--voice-channel", default=None, help="Discord voice channel ID to join")
@click.option("--guild", default=None, help="Discord guild ID (required if bot is in multiple servers)")
@click.option("--preset", default=None, help="Use a saved preset by name (fills guild + channel)")
def record_start(campaign: Optional[str], voice_channel: Optional[str], guild: Optional[str], preset: Optional[str]):
    """Join a Discord voice channel and start recording."""
    from .config import load_config

    if preset:
        cfg = load_config()
        presets = list(cfg.get("discord_presets", []) or [])
        match = None
        for p in presets:
            if p["name"] == preset:
                match = p
                break
        if match is None:
            raise click.ClickException(f"No preset named {preset!r}. Run: wisper config discord-presets list")
        if guild is None:
            guild = match["guild_id"]
        if voice_channel is None:
            voice_channel = match["channel_id"]
        click.echo(f"Using preset {preset!r}: guild={guild}, channel={voice_channel}")

    # Fall back to the `wisper config discord` defaults (the web form uses
    # the same keys).
    if guild is None or voice_channel is None:
        cfg = load_config()
        if guild is None:
            guild = cfg.get("discord_default_guild") or None
        if voice_channel is None:
            voice_channel = cfg.get("discord_default_channel") or None

    if not voice_channel:
        raise click.ClickException("--voice-channel is required. Use --preset <name> or pass --voice-channel directly.")
    if not guild:
        raise click.ClickException("--guild is required. Use --preset <name> or pass --guild directly.")

    payload: dict = {"voice_channel_id": voice_channel}
    if campaign:
        payload["campaign_slug"] = campaign
    if guild:
        payload["guild_id"] = guild
    result = _record_request("POST", "/api/record/start", json=payload)
    click.echo(result)


@record.command("stop")
def record_stop():
    """Stop the active recording session and queue transcription."""
    result = _record_request("POST", "/api/record/stop")
    click.echo(result)


@record.command("list")
@click.option("--campaign", default=None, help="Filter by campaign slug")
def record_list(campaign: Optional[str]):
    """List recordings, grouped by campaign."""
    params = {}
    if campaign:
        params["campaign"] = campaign
    result = _record_request("GET", "/api/recordings", params=params)
    click.echo(result)


@record.command("show")
@click.argument("recording_id")
def record_show(recording_id: str):
    """Show metadata and segment info for a recording."""
    from .recording_manager import _validate_recording_id
    if not _validate_recording_id(recording_id):
        raise click.ClickException(f"Invalid recording ID: {recording_id!r}")
    result = _record_request("GET", f"/api/recordings/{recording_id}")
    click.echo(result)


@record.command("transcribe")
@click.argument("recording_id")
def record_transcribe(recording_id: str):
    """Re-queue transcription for an existing recording."""
    from .recording_manager import _validate_recording_id
    if not _validate_recording_id(recording_id):
        raise click.ClickException(f"Invalid recording ID: {recording_id!r}")
    result = _record_request("POST", f"/api/recordings/{recording_id}/transcribe")
    click.echo(result)


@record.command("delete")
@click.argument("recording_id")
@click.confirmation_option(
    prompt="This permanently deletes the recording's audio (and transcript, if any) from disk. Continue?"
)
def record_delete(recording_id: str):
    """Delete a recording and its files on disk (audio, transcript, campaign notes)."""
    from .recording_manager import _validate_recording_id
    if not _validate_recording_id(recording_id):
        raise click.ClickException(f"Invalid recording ID: {recording_id!r}")
    result = _record_request("POST", f"/api/recordings/{recording_id}/delete?purge=true")
    click.echo(result)


@record.command("recover")
@click.argument("recording_id")
def record_recover(recording_id: str):
    """Rebuild a crashed session's audio from its segments so it can be transcribed."""
    from .recording_manager import _validate_recording_id
    if not _validate_recording_id(recording_id):
        raise click.ClickException(f"Invalid recording ID: {recording_id!r}")
    # Joining hours of segments can take a while.
    result = _record_request("POST", f"/api/recordings/{recording_id}/recover", timeout=600)
    click.echo(result)


# ---------------------------------------------------------------------------
# wisper config discord
# ---------------------------------------------------------------------------

@config.command("discord")
def config_discord():
    """Configure the Discord recording bot token and defaults."""
    from .config import load_config, save_config

    cfg = load_config()
    click.echo("")
    click.echo("wisper Discord bot configuration")
    click.echo("=" * 42)
    click.echo("  Get your bot token from: https://discord.com/developers/applications")
    click.echo("  Required permissions: View Channels, Connect, Speak")
    click.echo("")

    current_token = cfg.get("discord_bot_token", "")
    token_display = "***set***" if current_token else "not set"
    new_token = click.prompt(
        f"  Bot token [{token_display}]",
        default="",
        show_default=False,
        hide_input=True,
    ).strip()
    if new_token:
        cfg["discord_bot_token"] = new_token

    current_guild = cfg.get("discord_default_guild", "")
    new_guild = click.prompt(
        f"  Default guild (server) ID [{current_guild or 'none'}]",
        default=current_guild,
        show_default=False,
    ).strip()
    if new_guild:
        cfg["discord_default_guild"] = new_guild

    current_channel = cfg.get("discord_default_channel", "")
    new_channel = click.prompt(
        f"  Default voice channel ID [{current_channel or 'none'}]",
        default=current_channel,
        show_default=False,
    ).strip()
    if new_channel:
        cfg["discord_default_channel"] = new_channel

    save_config(cfg)
    click.echo("")
    click.echo("  OK  : Discord config saved.")
    click.echo("  Tip : DISCORD_BOT_TOKEN env var always takes precedence over config.")


@config.group("discord-presets")
def config_discord_presets():
    """Manage saved Discord channel presets for quick-select."""


@config_discord_presets.command("add")
@click.option("--name", required=True, help="Label for this preset (e.g. 'Weekly D&D')")
@click.option("--guild", required=True, help="Discord guild (server) ID")
@click.option("--channel", required=True, help="Discord voice channel ID")
def discord_presets_add(name: str, guild: str, channel: str):
    """Add a Discord channel preset."""
    from .config import load_config, save_config
    cfg = load_config()
    presets = list(cfg.get("discord_presets", []) or [])
    presets.append({"name": name.strip(), "guild_id": guild.strip(), "channel_id": channel.strip()})
    cfg["discord_presets"] = presets
    save_config(cfg)
    click.echo(f"Added preset {name!r} (guild={guild}, channel={channel})")


@config_discord_presets.command("list")
def discord_presets_list():
    """List saved Discord channel presets."""
    from .config import load_config
    cfg = load_config()
    presets = list(cfg.get("discord_presets", []) or [])
    if not presets:
        click.echo("No presets saved. Run: wisper config discord-presets add --name <label> --guild <id> --channel <id>")
        return
    click.echo(f"{'Name':<25} {'Guild ID':<22} {'Channel ID'}")
    click.echo("-" * 64)
    for p in presets:
        click.echo(f"{p['name']:<25} {p['guild_id']:<22} {p['channel_id']}")


@config_discord_presets.command("remove")
@click.argument("name")
def discord_presets_remove(name: str):
    """Remove a Discord channel preset by name."""
    from .config import load_config, save_config
    cfg = load_config()
    presets = list(cfg.get("discord_presets", []) or [])
    new_presets = [p for p in presets if p["name"] != name]
    if len(new_presets) == len(presets):
        raise click.ClickException(f"No preset named {name!r}. Run: wisper config discord-presets list")
    cfg["discord_presets"] = new_presets
    save_config(cfg)
    click.echo(f"Removed preset {name!r}.")


# ---------------------------------------------------------------------------
# wisper search
# ---------------------------------------------------------------------------

def _terminal_snippet(snippet: str) -> str:
    """A search snippet (escaped HTML with <mark> tags) as styled terminal text."""
    import html
    parts = re.split(r"<mark>(.*?)</mark>", str(snippet))
    return "".join(
        click.style(html.unescape(part), bold=True, fg="yellow") if i % 2 else html.unescape(part)
        for i, part in enumerate(parts)
    )


@main.command("search")
@click.argument("query")
@click.option("--campaign", default=None, help="Only transcripts in this campaign (slug)")
@click.option("--speaker", default=None, help="Only blocks spoken by this name (exact)")
@click.option("--kind", type=click.Choice(["transcript", "summary"]), default=None,
              help="Only transcripts or only session summaries")
@click.option("--limit", type=click.IntRange(1, 200), default=10, show_default=True,
              help="Maximum number of transcripts to show")
def search(query: str, campaign: Optional[str], speaker: Optional[str], kind: Optional[str],
           limit: int):
    """Search every transcript's title and text, and every session summary, for QUERY.

    Words match their other forms ("fights" finds "fight"); "double quotes"
    match a phrase; a trailing * matches a prefix. Transcripts not yet in the
    search index are indexed first.
    """
    from . import search_index
    from .transcript_store import reconcile

    reconcile()  # registers new files and marks edited ones for reindexing
    indexed, total = search_index.progress()
    if indexed < total:
        click.echo(f"Indexing {total - indexed} transcript(s)...", err=True)
        search_index.run_backfill()

    page = search_index.search(query, campaign=campaign, speaker=speaker, kind=kind,
                               per_page=limit)
    if page.error:
        raise click.ClickException(page.error)
    if not page.groups:
        click.echo("No matches.")
        return
    for group in page.groups:
        where = f"  [{group.campaign_name}]" if group.campaign_name else ""
        click.echo(click.style(group.stem, bold=True) + where
                   + f"  ({group.total_hits} match{'es' if group.total_hits != 1 else ''})")
        if group.stale:
            click.echo("  changed since indexing — run the search again")
            continue
        for hit in group.hits:
            label = hit.kind if hit.kind in ("summary", "title") else " ".join(
                x for x in (hit.timestamp, hit.speaker or "") if x)
            click.echo(f"  {label:<24} {_terminal_snippet(hit.snippet)}")
    if page.has_next:
        click.echo(f"More results: pass --limit {limit * 2}.")


# ---------------------------------------------------------------------------
# wisper db
# ---------------------------------------------------------------------------

@main.group("db")
def db_group():
    """Inspect, back up, dump, or reindex the wisper database."""


@db_group.command("status")
def db_status():
    """Show schema version, integrity checks, and runtime leases.

    Read-only: never migrates, so it also works on a database that startup
    refuses.
    """
    from . import db

    st = db.status()
    click.echo(f"Database       : {st.path}")
    click.echo(f"SQLite         : {st.sqlite_version}")
    if st.capability_error:
        click.echo(f"  ! {st.capability_error}")
    if not st.exists:
        click.echo("Not created yet (it is created on first use).")
        return
    click.echo(f"Size           : {st.size_bytes / 1024:.1f} KB")
    click.echo(f"Schema version : {st.version} (this build: {st.latest})")
    if st.version > st.latest:
        click.echo("  ! Newer than this build; upgrade wisper before using it.")
    elif st.version < st.latest:
        click.echo("  Migrations pending; they run on next start.")
    integrity_ok = st.integrity == ["ok"]
    click.echo(f"Integrity      : {'ok' if integrity_ok else 'FAILED'}")
    for line in ([] if integrity_ok else st.integrity[:20]):
        click.echo(f"  {line}")
    click.echo(f"Foreign keys   : {'ok' if st.fk_violations == 0 else f'{st.fk_violations} violation(s)'}")
    if st.schema_drift:
        click.echo(
            "  ! Schema differs from what this build creates at this version. "
            "The database was probably created by an unmerged development build."
        )
    if not st.frozen:
        click.echo("Build          : unmerged development build (schema not frozen)")
    for m in st.migrations:
        backup = ""
        if m["backup_dir"]:
            backup = f", backup {m['backup_dir']}"
            if not (st.path.parent / m["backup_dir"]).exists():
                backup += " (pruned)"
        click.echo(f"  v{m['version']} applied {m['applied_at']}{backup}")
    if st.leases:
        click.echo("Runtime leases :")
        for lease in st.leases:
            vm = " (Docker Desktop VM)" if lease["crosses_vm"] else ""
            state = "active" if lease["age_s"] < db.LEASE_TTL_S else "expired"
            click.echo(
                f"  {lease['runtime']:<9} {lease['holder']}{vm} "
                f"— heartbeat {lease['heartbeat_at']} ({state})"
            )


@db_group.command("backup")
@click.argument("dest", required=False, type=click.Path(dir_okay=False, path_type=Path))
def db_backup(dest: Optional[Path]):
    """Copy the database to DEST (default: <data dir>/backups/wisper-<time>.db).

    Uses SQLite's backup API, so it is consistent even while the server runs.
    """
    from . import db

    out = db.backup(dest=dest)
    click.echo(f"Backed up to {out}")


@db_group.command("reindex")
def db_reindex():
    """Drop and rebuild the full-text search index from the transcript files.

    The index is derived from the files, so this never loses data. The
    running server's index is rebuilt too (it shares the database).
    """
    from . import search_index
    from .transcript_store import reconcile

    reconcile()

    def report(done: int, todo: int) -> None:
        if done == todo or done % 25 == 0:
            click.echo(f"  {done}/{todo}", err=True)

    n = search_index.rebuild(report=report)
    click.echo(f"Indexed {n} transcript(s).")


@db_group.command("dump")
@click.option("-o", "--output", "output", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Write to a file instead of stdout")
def db_dump(output: Optional[Path]):
    """Print the whole database as SQL text."""
    from . import db

    lines = db.dump()
    if output is None:
        for line in lines:
            click.echo(line)
        return
    with open(output, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    click.echo(f"Wrote {output}")


# ---------------------------------------------------------------------------
# wisper storage
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


@main.group("storage")
def storage_group():
    """Reclaim disk space used by older transcripts and recordings."""


def _echo_attention(attention) -> None:
    # Misplaced sessions, legacy journals, and blocked campaign folders are
    # listed above as organize actions or blocked lines; count only these.
    if attention is None:
        return
    count = (len(attention.missing_transcripts) + len(attention.missing_files)
             + len(attention.unclaimed))
    if not count:
        return
    click.echo("")
    click.echo(f"Needs attention ({count}):")
    for m in attention.missing_transcripts:
        where = f" [{m.campaign}]" if m.campaign else ""
        click.echo(f"  missing transcript: {m.stem}{where}")
    for row in attention.missing_files:
        click.echo(f"  missing file: {row.path}")
    for path in attention.unclaimed:
        click.echo(f"  no transcript: {path}")


@storage_group.command("trim")
@click.option("--apply", "apply_", is_flag=True, default=False,
              help="Do it. Without this flag nothing is changed.")
@click.option("--device", default="auto", show_default=True, type=click.Choice(_config.DEVICES),
              help="Compute device for extracting missing speaker voices")
def storage_trim(apply_: bool, device: str):
    """Move sessions into their campaign folders, then shrink stored audio.

    Moves every misplaced session's files into its campaign's folder and moves
    legacy journals out of the data dir, extracts any speaker voices a
    transcript lacks, converts each transcript's audio to a 16 kHz mono FLAC,
    deletes orphaned recording hand-off copies, and removes the segment and
    per-user audio a recording's combined.wav makes redundant. Only files
    wisper tracks are touched.

    Dry run by default. With --apply the web server must be stopped.
    """
    from . import campaign_folders, db, storage_trim as trim

    campaign_folders.finish_pending_renames()  # before scanning the folders

    if apply_:
        lock = db.ServerLock()
        try:
            lock.acquire()
        except db.ServerLockHeld:
            raise click.ClickException("Stop the wisper server first, then run this again.")
        try:
            trim.check_runtime()
            with db.Heartbeat():
                current = trim.plan()
                _echo_plan(current)
                if not current.actions:
                    return
                report = trim.apply(current, device=device, progress=lambda m: click.echo(f"  {m}"))
        finally:
            lock.release()
        click.echo("")
        click.echo(
            f"Moved {len(report.organized)} into campaign folders; "
            f"converted {len(report.converted)}, deleted {len(report.dropped)} copy(ies) and "
            f"{len(report.orphans)} orphan(s), trimmed {len(report.trimmed)} recording(s); "
            + (f"freed {_fmt_bytes(report.freed_bytes)}." if report.freed_bytes >= 0
               else f"used {_fmt_bytes(-report.freed_bytes)} more."))
        for line in report.errors:
            click.echo(f"  ! {line}")
        _echo_attention(report.attention)
        if report.errors:
            raise SystemExit(1)
        return

    trim.check_runtime()
    current = trim.plan()
    _echo_plan(current)
    if current.actions:
        click.echo("")
        click.echo("Dry run: nothing was changed. Run again with --apply to do this.")


def _echo_plan(current) -> None:
    from .storage_trim import KIND_LABELS, ORGANIZE

    if not current.actions and not current.blocked:
        click.echo("Nothing to trim.")
    for a in current.actions:
        note = f"  {a.note}" if a.note and a.kind == ORGANIZE else ""
        click.echo(f"{KIND_LABELS[a.kind]:<16} {_fmt_bytes(a.size):>10}  {a.path}{note}")
    for name, sessions, why in current.blocked:
        noun = "session" if sessions == 1 else "sessions"
        click.echo(f"{name}: {sessions} {noun} can't be organized ({why}; "
                   "see Needs attention)")
    if current.actions:
        if current.move_bytes:
            n = len(current.moves)
            click.echo(f"Move {n} session{'s' if n != 1 else ''} into their campaign "
                       f"folders ({_fmt_bytes(current.move_bytes)}, nothing deleted)")
        if current.total_bytes:
            click.echo(f"{'Deletions free':<16} {_fmt_bytes(current.total_bytes):>10}")
        if current.convert_bytes:
            click.echo(f"{'Converted':<16} {_fmt_bytes(current.convert_bytes):>10}  "
                       "replaced by 16 kHz mono FLAC, about 90 MB per hour of audio; "
                       "a compressed audio file can grow")
    _echo_attention(current.attention)
