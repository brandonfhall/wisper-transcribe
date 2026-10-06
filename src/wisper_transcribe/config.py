import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
import platformdirs
import tomli_w

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

APP_NAME = "wisper-transcribe"

# Cosine similarity a label needs to match a profile. Calibrated on real
# sessions in the WeSpeaker space: same-person pairs scored 0.65+, different
# people at most 0.50.
DEFAULT_SIMILARITY_THRESHOLD = 0.55
# The old pyannote/embedding default. Saved configs hold it verbatim, so
# load_config() migrates that exact value.
_LEGACY_SIMILARITY_THRESHOLD = 0.65

DEFAULTS = {
    "model": "large-v3-turbo",
    "language": "en",
    "device": "auto",
    "compute_type": "auto",
    "vad_filter": True,
    "timestamps": True,
    "similarity_threshold": DEFAULT_SIMILARITY_THRESHOLD,
    "min_speakers": 2,
    "max_speakers": 8,
    "hf_token": "",
    "hotwords": [],
    # Apple Silicon: "auto" uses MLX Whisper on MPS when mlx-whisper is installed,
    # "true" requires MLX (errors if not installed), "false" always uses faster-whisper CPU.
    "use_mlx": "auto",
    # Run transcription and diarization concurrently via ProcessPoolExecutor(max_workers=2).
    # Each subprocess gets its own copy of the module-level model globals.
    # Disabled by default — enable after benchmarking on your hardware.
    "parallel_stages": False,
    # Proxy the four GPU functions through one warm spawned child so Stop
    # terminates the process and frees the GPU immediately. Off runs
    # everything in-process. Ignored by the CLI.
    "ml_worker": True,
    # Re-time Whisper's words against the audio before speaker assignment
    # (word_alignment.py). "auto" aligns when diarization runs on a GPU
    # (CUDA or MPS); CPU adds ~9-20 min per 2.5 h session.
    "forced_alignment": "auto",
    # LLM post-processing (refine, summarize, campaign journal). Opt-in.
    # Default provider is local Ollama; cloud providers require explicit config + key.
    "llm_provider": "ollama",                 # ollama | anthropic | openai | google
    "llm_model": "",                          # blank → per-provider default via resolve_llm_model()
    "llm_endpoint": "http://localhost:11434", # ollama default; lmstudio default is :1234
    "llm_temperature": 0.2,
    "anthropic_api_key": "",                  # env ANTHROPIC_API_KEY takes precedence
    "openai_api_key": "",                     # env OPENAI_API_KEY takes precedence
    "google_api_key": "",                     # env GOOGLE_API_KEY takes precedence
    "ollama_cloud_api_key": "",               # env OLLAMA_API_KEY takes precedence — used by `ollama-cloud` provider for direct ollama.com calls
    # Discord recording bot
    "discord_bot_token": "",                  # env DISCORD_BOT_TOKEN takes precedence
    "discord_default_guild": "",
    "discord_default_channel": "",
    "discord_presets": [],                    # [{"name": "...", "guild_id": "...", "channel_id": "..."}]
    # Where transcripts live (the web UI's scope). Blank → <data dir>/output.
    # env WISPER_OUTPUT_DIR takes precedence.
    "output_dir": "",
}

LLM_PROVIDERS = ("ollama", "ollama-cloud", "lmstudio", "anthropic", "openai", "google")

# Per-provider default model names. Override via config (llm_model) or CLI (--model).
_LLM_DEFAULT_MODELS = {
    "ollama": "llama3.1:8b",
    "ollama-cloud": "gpt-oss:120b",
    "lmstudio": "",          # blank → use whatever model is loaded in LM Studio
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
    "google": "gemini-1.5-flash",
}

# Per-provider default endpoint URLs.
# ollama-cloud is hardcoded — ollama.com is THE endpoint for this provider, no override exposed.
_LLM_DEFAULT_ENDPOINTS = {
    "ollama":       "http://localhost:11434",
    "ollama-cloud": "https://ollama.com",
    "lmstudio":     "http://localhost:1234",
}

# env var → config key mapping for LLM API keys. Keys are never logged.
_LLM_API_KEY_ENV = {
    "anthropic":    ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    "openai":       ("OPENAI_API_KEY", "openai_api_key"),
    "google":       ("GOOGLE_API_KEY", "google_api_key"),
    "ollama-cloud": ("OLLAMA_API_KEY", "ollama_cloud_api_key"),
}

# config-key set used by config_show to mask secrets when printing settings.
LLM_SECRET_KEYS = frozenset({
    "anthropic_api_key", "openai_api_key", "google_api_key", "ollama_cloud_api_key",
})

COMPUTE_TYPES = ("auto", "float16", "int8_float16", "int8", "float32")

# Gated Hugging Face model; users accept its terms once.
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
# Profile embeddings load from the diarization repo's WeSpeaker subfolder, so
# one license covers both and profiles share the clustering's embedding space.
EMBEDDING_SUBFOLDER = "embedding"
# Stored on each profile. Profiles tagged otherwise (or untagged, from the old
# pyannote/embedding model) are incomparable and never matched.
EMBEDDING_SPACE = "wespeaker-resnet34"
# Re-times Whisper's words against the audio before speaker assignment.
# Apache-2.0 and ungated, so no token is needed.
FORCED_ALIGNMENT_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B-hf"

# Allowed values, shared by CLI click.Choice lists and web-form validation.
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
DEVICES = ("auto", "cpu", "cuda", "mps")
FORCED_ALIGNMENT_MODES = ("auto", "true", "false")

# Choice-valued keys, validated by `wisper config set` and the web Config page.
CONFIG_CHOICES = {
    "model": MODEL_SIZES,
    "device": DEVICES,
    "compute_type": COMPUTE_TYPES,
    "forced_alignment": FORCED_ALIGNMENT_MODES,
}


def resolve_compute_type(compute_type: str, device: str) -> str:
    """Resolve 'auto' to a concrete CTranslate2 compute type based on device."""
    if compute_type != "auto":
        return compute_type
    return "float16" if device == "cuda" else "int8"


def get_data_dir() -> Path:
    import os
    override = os.environ.get("WISPER_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(APP_NAME))


def resolve_output_root(cfg: dict, data_dir: Path, env: Optional[str]) -> Path:
    """The transcript output root for a given config, data dir, and env value.

    ``env`` (``WISPER_OUTPUT_DIR``), then ``cfg["output_dir"]``, then
    ``<data_dir>/output``. The working directory is never consulted: the
    database records which transcripts exist, so the root must not change
    with where wisper was launched. A relative setting is relative to the
    data dir; ``env`` is used as given, never joined to it.
    """
    if env:
        return Path(env).expanduser()
    configured = str(cfg.get("output_dir") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else data_dir / path
    return data_dir / "output"


def get_output_root(config: Optional[dict] = None) -> Path:
    """Resolve the transcript output root without creating it.

    ``WISPER_OUTPUT_DIR`` wins without reading ``config.toml``.
    """
    import os
    env = os.environ.get("WISPER_OUTPUT_DIR")
    if env:
        return resolve_output_root({}, get_data_dir(), env)
    return resolve_output_root(
        config if config is not None else load_config(), get_data_dir(), None
    )


def get_config_path() -> Path:
    return get_data_dir() / "config.toml"


def load_config() -> dict:
    config_path = get_config_path()
    config = dict(DEFAULTS)
    if config_path.exists():
        with open(config_path, "rb") as f:
            stored = tomllib.load(f)
        config.update(stored)
    if config.get("similarity_threshold") == _LEGACY_SIMILARITY_THRESHOLD:
        config["similarity_threshold"] = DEFAULT_SIMILARITY_THRESHOLD
    return config


def save_config(config: dict) -> None:
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "wb") as f:
        tomli_w.dump(config, f)


def check_ffmpeg() -> None:
    """Raise RuntimeError with install instructions if ffmpeg is not found."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        platform = sys.platform
        if platform == "win32":
            install_hint = "  winget install Gyan.FFmpeg\n  or download from https://ffmpeg.org/download.html"
        elif platform == "darwin":
            install_hint = "  brew install ffmpeg"
        else:
            install_hint = "  sudo apt install ffmpeg  (or your distro's equivalent)"

        raise RuntimeError(
            "ffmpeg not found. Please install it:\n" + install_hint
        )


def forced_alignment_enabled(setting, device: str) -> bool:
    """Resolve the ``forced_alignment`` setting for a resolved ``device``.

    ``auto`` means on for GPU devices only. A hand-edited TOML boolean is
    accepted as well as the strings.
    """
    if isinstance(setting, bool):
        return setting
    setting = str(setting).strip().lower()
    if setting in ("true", "1", "yes", "on"):
        return True
    if setting in ("false", "0", "no", "off"):
        return False
    return device in ("cuda", "mps")


def get_device() -> str:
    """Return 'cuda', 'mps', or 'cpu' based on available hardware."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except ImportError:
        return "cpu"


def get_hf_token(config: Optional[dict] = None) -> str:
    """Return HuggingFace token from env var, config, or interactive prompt.

    Accepts HUGGINGFACE_TOKEN or HF_TOKEN (huggingface_hub's canonical name).
    Whichever is found is propagated to both env vars so third-party libraries
    (e.g. mlx-whisper) that only look for HF_TOKEN also see it.

    Raises RuntimeError if no token is found and stdin is not a tty.
    """
    import os

    token = os.environ.get("HUGGINGFACE_TOKEN", "") or os.environ.get("HF_TOKEN", "")
    if token:
        os.environ.setdefault("HUGGINGFACE_TOKEN", token)
        os.environ.setdefault("HF_TOKEN", token)
        return token

    if config is None:
        config = load_config()
    token = config.get("hf_token", "")
    if token:
        os.environ.setdefault("HUGGINGFACE_TOKEN", token)
        os.environ.setdefault("HF_TOKEN", token)
        return token

    # Interactive prompt as last resort
    import sys
    if not sys.stdin.isatty():
        raise RuntimeError(
            "HuggingFace token required for speaker diarization.\n"
            "Set it with: wisper config set hf_token <your_token>\n"
            "Or export HUGGINGFACE_TOKEN=<your_token>"
        )

    click.echo(
        "\nA HuggingFace token is required for speaker diarization.\n"
        "Get a free token at https://huggingface.co/settings/tokens\n"
        "You must also accept the pyannote model terms at:\n"
        f"  https://huggingface.co/{DIARIZATION_MODEL}"
    )
    token = click.prompt("HuggingFace token").strip()
    if token:
        cfg = load_config()
        cfg["hf_token"] = token
        save_config(cfg)
        click.echo("Token saved to config.")
    return token


def get_llm_api_key(provider: str, config: Optional[dict] = None) -> Optional[str]:
    """Return the API key for a cloud LLM provider.

    Resolution order (mirrors get_hf_token):
    1. Environment variable (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY)
    2. Config file key (anthropic_api_key / openai_api_key / google_api_key)
    3. None

    `ollama` has no key and always returns None. Unknown providers raise ValueError.
    The returned key is never logged anywhere; callers must pass it directly to the
    provider SDK.
    """
    import os

    if provider == "ollama":
        return None
    if provider not in _LLM_API_KEY_ENV:
        raise ValueError(f"Unknown LLM provider: {provider!r}")

    env_name, config_key = _LLM_API_KEY_ENV[provider]
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value

    if config is None:
        config = load_config()
    stored = config.get(config_key, "")
    return stored.strip() if stored else None


def resolve_llm_model(provider: str, override: Optional[str] = None,
                      config: Optional[dict] = None) -> str:
    """Return the model name to use for a given provider.

    Resolution order:
    1. `override` argument (from CLI --model)
    2. `llm_model` in config (if non-empty)
    3. Per-provider default (_LLM_DEFAULT_MODELS)

    Unknown providers raise ValueError.
    """
    if provider not in _LLM_DEFAULT_MODELS:
        raise ValueError(f"Unknown LLM provider: {provider!r}")

    if override:
        return override

    if config is None:
        config = load_config()
    stored = config.get("llm_model", "") or ""
    if stored.strip():
        return stored.strip()

    return _LLM_DEFAULT_MODELS[provider]
