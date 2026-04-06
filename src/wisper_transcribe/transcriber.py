from pathlib import Path
from typing import Optional

from .models import TranscriptionSegment

_model = None


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
) -> list[TranscriptionSegment]:
    """Transcribe audio and return a list of timestamped segments."""
    global _model

    from .config import get_device

    if device == "auto":
        device = get_device()

    if _model is None:
        load_model(model_size, device, compute_type)

    segments, info = _model.transcribe(
        str(audio_path),
        language=language if language else None,
        beam_size=5,
        vad_filter=vad_filter,
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
