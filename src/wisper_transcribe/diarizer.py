from __future__ import annotations

from pathlib import Path
from typing import Optional

# speechbrain 1.0 lazy-loads optional integrations (k2, transformers, spacy,
# numba, …) whenever something calls inspect.getmembers() on the speechbrain
# package.  Any integration whose optional dependency is not installed raises
# instead of silently no-oping.  This is a bug in speechbrain itself (Windows
# path check uses forward slash so never matches on Windows).  Patch
# LazyModule.ensure_module before speechbrain is imported by pyannote so that
# a failed import returns an empty stub module rather than crashing.
import sys as _sys
import types as _types
try:
    import speechbrain.utils.importutils as _sb_import_utils

    _sb_LazyModule = _sb_import_utils.LazyModule
    _orig_ensure_module = _sb_LazyModule.ensure_module

    def _tolerant_ensure_module(self, stacklevel=1):  # type: ignore[misc]
        try:
            return _orig_ensure_module(self, stacklevel + 1)
        except (ImportError, ModuleNotFoundError):
            stub = _types.ModuleType(self.target)
            _sys.modules.setdefault(self.target, stub)
            self.lazy_module = stub  # type: ignore[attr-defined]
            return stub

    _sb_LazyModule.ensure_module = _tolerant_ensure_module  # type: ignore[method-assign]
except ImportError:
    pass  # speechbrain not installed; patch not needed

from ._noise_suppress import suppress_third_party_noise as _suppress

# Must run before pyannote is imported below — the Lightning compat-shim
# fires redirect warnings the moment pytorch_lightning symbols are resolved.
_suppress()

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
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
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
            token=hf_token,
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

    # Pre-load audio via scipy and pass as a waveform tensor dict.
    # torchcodec (pyannote 4.x's default audio decoder) requires FFmpeg
    # shared DLLs on Windows (Gyan.FFmpeg.Shared).  The scipy bypass works
    # on all platforms and the input is always a WAV file from convert_to_wav().
    from .audio_utils import load_wav_as_tensor

    audio_dict = load_wav_as_tensor(audio_path)
    hook = _DiarizationProgressHook()
    try:
        diarization = _pipeline(
            audio_dict,
            hook=hook,
            **kwargs,
        )
    finally:
        hook.close()

    # pyannote 4.x returns DiarizeOutput(speaker_diarization=Annotation, …)
    # pyannote 3.x / legacy mode returns an Annotation directly.
    annotation = (
        diarization.speaker_diarization
        if hasattr(diarization, "speaker_diarization")
        else diarization
    )

    segments: list[DiarizationSegment] = []
    for turn, _track, speaker in annotation.itertracks(yield_label=True):
        segments.append(
            DiarizationSegment(
                start=turn.start,
                end=turn.end,
                speaker=speaker,
            )
        )
    return segments
