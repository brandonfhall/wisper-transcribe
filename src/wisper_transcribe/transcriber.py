import platform
from pathlib import Path
from typing import Optional

from .models import TranscriptionSegment

_model = None

# Maps standard model-size names to MLX Community HuggingFace repo IDs.
# Only used on Apple Silicon (macOS + MPS) when mlx-whisper is installed.
_MLX_MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def _is_mlx_available() -> bool:
    """Return True if mlx_whisper is installed and importable on Apple Silicon.

    Uses importlib.util.find_spec for the presence check so this is safe to
    call from the main process (e.g. uvicorn) where a full Metal-initialising
    import may conflict with the async event loop.  The actual import happens
    only inside _transcribe_mlx(), which runs in a subprocess.
    """
    if platform.system() != "Darwin":
        return False
    import importlib.util
    return importlib.util.find_spec("mlx_whisper") is not None


def _transcribe_mlx(
    audio_path: Path,
    model_size: str = "medium",
    language: Optional[str] = "en",
    initial_prompt: Optional[str] = None,
    hotwords: Optional[list[str]] = None,
) -> list[TranscriptionSegment]:
    """Transcribe using the MLX Whisper backend (Apple Silicon GPU/ANE).

    vad_filter is not supported by mlx-whisper and is silently skipped.
    hotwords are injected into initial_prompt as a comma-separated prefix,
    which nudges the model toward correct spellings via prior-context priming.
    """
    import os
    import mlx_whisper
    from tqdm import tqdm

    # The model is cached after the first run, but huggingface_hub still shows
    # a "Fetching N files" verification bar on every call. Suppress it.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    repo = _MLX_MODEL_MAP.get(model_size, f"mlx-community/whisper-{model_size}-mlx")
    tqdm.write(f"  Using MLX-Whisper backend ({repo})")

    # Inject hotwords into initial_prompt (mlx-whisper has no native hotwords param).
    effective_prompt = initial_prompt or ""
    if hotwords:
        hw_prefix = ", ".join(hotwords)
        effective_prompt = (
            f"{hw_prefix}. {effective_prompt}".strip() if effective_prompt else hw_prefix
        )

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=repo,
        language=language if language else None,
        word_timestamps=True,
        initial_prompt=effective_prompt or None,
    )

    return [
        TranscriptionSegment(start=seg["start"], end=seg["end"], text=seg["text"].strip())
        for seg in result.get("segments", [])
        if seg.get("text", "").strip()
    ]


def load_model(model_size: str, device: str, compute_type: str = "auto"):
    """Load faster-whisper model, caching it module-level."""
    global _model

    # On Windows, explicitly add PyTorch's bundled CUDA libraries to the PATH
    # so CTranslate2 (faster-whisper's backend) can find cublas64_12.dll
    import sys
    if sys.platform == "win32" and device == "cuda":
        import os
        from pathlib import Path

        search_paths = []
        # 1. Check Python site-packages (newer PyTorch splits DLLs into nvidia-* packages)
        try:
            import torch
            site_packages = Path(torch.__file__).parent.parent
            search_paths.extend([
                site_packages / "torch" / "lib",
                site_packages / "nvidia" / "cublas" / "bin",
                site_packages / "nvidia" / "cublas" / "lib",
            ])
        except ImportError:
            pass

        # 2. Check System CUDA Toolkit paths (default winget locations)
        cuda_base = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
        if cuda_base.exists():
            for version_dir in cuda_base.iterdir():
                if version_dir.is_dir():
                    search_paths.append(version_dir / "bin")

        for p in search_paths:
            if (p / "cublas64_12.dll").exists():
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(p))
                break

    if device == "cuda":
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is not available. Your PyTorch installation may not include CUDA support.\n"
                "Reinstall with CUDA support:\n"
                "  pip install 'torch>=2.8.0' torchaudio --index-url https://download.pytorch.org/whl/cu126\n"
                "Or use --device cpu"
            )

    # CTranslate2 (faster-whisper's backend) does not support MPS.
    # Fall back to CPU so the rest of the pipeline can still use MPS.
    ct2_device = "cpu" if device == "mps" else device
    if device == "mps":
        from tqdm import tqdm
        tqdm.write("  Note: faster-whisper does not support MPS — transcription will use CPU.")

    from faster_whisper import WhisperModel
    from .config import resolve_compute_type

    ct2_compute = resolve_compute_type(compute_type, ct2_device)
    _model = WhisperModel(model_size, device=ct2_device, compute_type=ct2_compute)
    return _model


def transcribe(
    audio_path: Path,
    model_size: str = "medium",
    device: str = "auto",
    language: Optional[str] = "en",
    compute_type: str = "auto",
    vad_filter: bool = True,
    initial_prompt: Optional[str] = None,
    hotwords: Optional[list[str]] = None,
    use_mlx: str = "auto",
) -> list[TranscriptionSegment]:
    """Transcribe audio and return a list of timestamped segments.

    On Apple Silicon (device='mps'), dispatches to the MLX Whisper backend
    when use_mlx is 'auto' (default) or 'true' and mlx-whisper is installed.
    Falls back to faster-whisper on CPU when MLX is unavailable or disabled.
    Set use_mlx='false' to always use the faster-whisper CPU path on Mac.
    """
    global _model

    from .config import get_device

    if device == "auto":
        device = get_device()

    # MLX dispatch: Apple Silicon only, when requested and available.
    if device == "mps" and use_mlx != "false":
        mlx_ok = _is_mlx_available()
        if use_mlx == "true" and not mlx_ok:
            raise RuntimeError(
                "use_mlx=true but mlx-whisper is not installed.\n"
                "Install it with: pip install 'wisper-transcribe[macos]'\n"
                "Or set use_mlx=auto in config to fall back to CPU automatically."
            )
        if mlx_ok:
            return _transcribe_mlx(
                audio_path,
                model_size=model_size,
                language=language,
                initial_prompt=initial_prompt,
                hotwords=hotwords,
            )
        # mlx not available → fall through to CPU path below

    if _model is None:
        load_model(model_size, device, compute_type)

    segments, info = _model.transcribe(
        str(audio_path),
        language=language if language else None,
        beam_size=5,
        vad_filter=vad_filter,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
    )

    from tqdm import tqdm

    result = []
    with tqdm(
        total=round(info.duration, 2),
        desc="  Transcribing",
        unit="s",
        mininterval=5.0,
        position=1,
        leave=False,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        dynamic_ncols=True,
    ) as pbar:
        for seg in segments:
            if seg.text.strip():
                result.append(TranscriptionSegment(start=seg.start, end=seg.end, text=seg.text.strip()))
            # Update progress bar by the difference between the segment's end and our current progress tracker
            pbar.update(seg.end - pbar.n)

    return result
