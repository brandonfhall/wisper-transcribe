from __future__ import annotations

from pathlib import Path
from typing import Optional

# torchaudio 2.x removed AudioMetaData from its public API in favour of
# torchcodec.  pyannote-audio 3.x still references it at import time as a
# type annotation.  Patch the name back in before pyannote loads so the
# import succeeds; the actual audio loading is done via scipy (see diarize()).
import torchaudio as _torchaudio

if not hasattr(_torchaudio, "AudioMetaData"):
    import collections
    _torchaudio.AudioMetaData = collections.namedtuple(  # type: ignore[attr-defined]
        "AudioMetaData",
        ["sample_rate", "num_frames", "num_channels", "bits_per_sample", "encoding"],
    )

if not hasattr(_torchaudio, "list_audio_backends"):
    _torchaudio.list_audio_backends = lambda: ["soundfile"]  # type: ignore[attr-defined]

if not hasattr(_torchaudio, "info"):
    import scipy.io.wavfile as _sf_info
    def _torchaudio_info_compat(filepath, *args, **kwargs):  # type: ignore[misc]
        sr, data = _sf_info.read(str(filepath))
        frames = data.shape[0]
        channels = 1 if data.ndim == 1 else data.shape[1]
        bits = data.dtype.itemsize * 8
        return _torchaudio.AudioMetaData(  # type: ignore[attr-defined]
            sample_rate=sr, num_frames=frames, num_channels=channels,
            bits_per_sample=bits, encoding="PCM_S",
        )
    _torchaudio.info = _torchaudio_info_compat  # type: ignore[attr-defined]

# huggingface_hub >=0.25 removed use_auth_token from hf_hub_download().
# pyannote-audio 3.x still passes it at call-time (pipeline.py, model.py).
# Patch at the huggingface_hub module level BEFORE pyannote imports the symbol,
# so every subsequent `from huggingface_hub import hf_hub_download` in pyannote
# binds to this wrapper instead of the raw function.
import huggingface_hub as _hf_hub
_orig_hf_hub_download = _hf_hub.hf_hub_download

def _compat_hf_hub_download(*args, use_auth_token=None, **kwargs):
    if use_auth_token is not None and "token" not in kwargs:
        kwargs["token"] = use_auth_token
    return _orig_hf_hub_download(*args, **kwargs)

_hf_hub.hf_hub_download = _compat_hf_hub_download

# speechbrain 1.0 lazy-loads optional integrations (k2, transformers, spacy,
# numba, …) whenever something calls inspect.getmembers() on the speechbrain
# package.  Any integration whose optional dependency is not installed raises
# instead of silently no-oping.  Patch LazyModule.__load (before speechbrain
# is imported by pyannote) so that a failed import returns an empty stub module
# rather than crashing.  This covers all missing optional deps at once.
import sys as _sys
import types as _types
import speechbrain.utils.importutils as _sb_import_utils

_sb_LazyModule = _sb_import_utils.LazyModule
_orig_ensure_module = _sb_LazyModule.ensure_module

def _tolerant_ensure_module(self, stacklevel=1):  # type: ignore[misc]
    # speechbrain checks `.endswith("/inspect.py")` to suppress lazy loads
    # triggered by inspect.getmembers(), but the forward-slash check never
    # matches on Windows (paths use backslash).  As a result every optional
    # integration (k2, transformers, spacy, numba…) is actually loaded and
    # crashes when its optional dependency isn't installed.  Catch the
    # ImportError here and return an empty stub so missing integrations are
    # silently ignored.
    try:
        return _orig_ensure_module(self, stacklevel + 1)
    except (ImportError, ModuleNotFoundError):
        stub = _types.ModuleType(self.target)
        _sys.modules.setdefault(self.target, stub)
        self.lazy_module = stub  # type: ignore[attr-defined]
        return stub

_sb_LazyModule.ensure_module = _tolerant_ensure_module  # type: ignore[method-assign]

# PyTorch 2.6 changed torch.load's default weights_only from False → True.
# pyannote-audio 3.x calls torch.load without specifying weights_only, and its
# checkpoints contain custom globals (TorchVersion, etc.) not in the safe list.
# All loads here are from trusted HuggingFace checkpoints, so restore the
# pre-2.6 default by making weights_only=False the default when not specified.
import torch as _torch

if not hasattr(_torch, "_compat_load_patched"):
    _orig_torch_load = _torch.load

    def _compat_torch_load(f, *args, weights_only=None, **kwargs):  # type: ignore[misc]
        if weights_only is None:
            weights_only = False
        return _orig_torch_load(f, *args, weights_only=weights_only, **kwargs)

    _torch.load = _compat_torch_load  # type: ignore[assignment]
    _torch._compat_load_patched = True  # type: ignore[attr-defined]

from tqdm import tqdm

from pyannote.audio import Pipeline

from .models import DiarizationSegment

_pipeline = None


class _DiarizationProgressHook:
    """Translates pyannote pipeline hook calls into tqdm progress bars.

    pyannote calls hook(step_name, artifact, file, total, completed) at each
    chunk of the segmentation and embedding steps.  We open a new tqdm bar
    whenever the step name changes and update it on each callback.
    """

    def __init__(self) -> None:
        self._bar: Optional[tqdm] = None  # type: ignore[type-arg]
        self._step: Optional[str] = None

    def __call__(self, step_name, *args, total=None, completed=None, **kwargs):  # noqa: ARG002
        if total is None:
            return
        if step_name != self._step:
            if self._bar is not None:
                self._bar.close()
            self._step = step_name
            self._bar = tqdm(
                total=total,
                desc=f"  {step_name.capitalize()}",
                position=1,
                leave=False,
                unit="chunk",
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
                dynamic_ncols=True,
            )
        if completed is not None and self._bar is not None:
            self._bar.n = completed
            self._bar.refresh()
            if completed >= total:
                self._bar.close()
                self._bar = None
                self._step = None

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def load_pipeline(hf_token: str, device: str):
    """Load pyannote speaker-diarization-3.1, cache module-level."""
    global _pipeline

    try:
        _pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )
    except Exception as e:
        if "locate the file on the Hub" in str(e) or "connection" in str(e).lower():
            raise RuntimeError(
                "Failed to download the diarization model from Hugging Face. "
                "Please ensure you have an active internet connection for the first run."
            ) from e
        raise
        
    import torch
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is not available. Your PyTorch installation may not include CUDA support.\n"
            "Reinstall with CUDA support:\n"
            "  pip install 'torch>=2.8.0' torchaudio --index-url https://download.pytorch.org/whl/cu126\n"
            "Or use --device cpu"
        )
    if device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError(
            "MPS is not available on this system. Use --device cpu instead."
        )
    _pipeline.to(torch.device(device))
    return _pipeline


def diarize(
    audio_path: Path,
    hf_token: str,
    device: str,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
) -> list[DiarizationSegment]:
    """Run speaker diarization and return labeled time segments."""
    global _pipeline

    if _pipeline is None:
        load_pipeline(hf_token, device)

    kwargs: dict = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    else:
        if min_speakers is not None:
            kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            kwargs["max_speakers"] = max_speakers

    # Load audio via scipy and pass as a tensor dict to bypass torchcodec,
    # which is pyannote 4.x's default decoder but fails on Windows unless
    # the FFmpeg "full-shared" build is installed. The input is always a
    # WAV file (guaranteed by convert_to_wav() in the pipeline).
    import numpy as np
    import scipy.io.wavfile as _wavfile
    import torch

    sample_rate, data = _wavfile.read(str(audio_path))
    if data.ndim == 1:
        data = data[np.newaxis, :]          # (time,) → (1, time)
    else:
        data = data.T                        # (time, ch) → (ch, time)
    if np.issubdtype(data.dtype, np.integer):
        data = data.astype(np.float32) / np.iinfo(data.dtype).max
    waveform = torch.from_numpy(data.copy())
    hook = _DiarizationProgressHook()
    try:
        diarization = _pipeline(
            {"waveform": waveform, "sample_rate": sample_rate},
            hook=hook,
            **kwargs,
        )
    finally:
        hook.close()

    segments: list[DiarizationSegment] = []
    for turn, _track, speaker in diarization.itertracks(yield_label=True):
        segments.append(
            DiarizationSegment(
                start=turn.start,
                end=turn.end,
                speaker=speaker,
            )
        )
    return segments
