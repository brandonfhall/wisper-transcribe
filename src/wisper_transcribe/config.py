import subprocess
import sys
from pathlib import Path

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
    "timestamps": True,
    "similarity_threshold": 0.65,
    "min_speakers": 2,
    "max_speakers": 8,
    "hf_token": "",
}


def get_data_dir() -> Path:
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
    """Return 'cuda' if a CUDA GPU is available, else 'cpu'."""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"
