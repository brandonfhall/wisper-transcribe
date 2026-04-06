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

DEFAULTS = {
    "model": "medium",
    "language": "en",
    "device": "auto",
    "compute_type": "auto",
    "vad_filter": True,
    "timestamps": True,
    "similarity_threshold": 0.65,
    "min_speakers": 2,
    "max_speakers": 8,
    "hf_token": "",
}

COMPUTE_TYPES = ("auto", "float16", "int8_float16", "int8", "float32")


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


def get_config_path() -> Path:
    return get_data_dir() / "config.toml"


def load_config() -> dict:
    config_path = get_config_path()
    config = dict(DEFAULTS)
    if config_path.exists():
        with open(config_path, "rb") as f:
            stored = tomllib.load(f)
        config.update(stored)
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

    Raises RuntimeError if no token is found and stdin is not a tty.
    """
    import os

    token = os.environ.get("HUGGINGFACE_TOKEN", "")
    if token:
        return token

    if config is None:
        config = load_config()
    token = config.get("hf_token", "")
    if token:
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
        "  https://huggingface.co/pyannote/speaker-diarization-3.1"
    )
    token = click.prompt("HuggingFace token").strip()
    if token:
        cfg = load_config()
        cfg["hf_token"] = token
        save_config(cfg)
        click.echo("Token saved to config.")
    return token
