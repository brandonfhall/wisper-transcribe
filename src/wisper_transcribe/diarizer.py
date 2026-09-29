from __future__ import annotations

from pathlib import Path
from typing import Optional

# Must be the very first ML-adjacent action in this module.  The speechbrain
# shim below imports speechbrain (which pulls in torch), so suppress must be
# in place before that import fires or the torch flop_counter warning leaks.
from ._noise_suppress import suppress_third_party_noise as _suppress
_suppress()

# speechbrain 1.0's guard against lazy-loading optional integrations (k2,
# transformers, spacy, numba, ...) checks for a forward-slash path, which never
# matches on Windows, so each missing integration raises. Patch
# LazyModule.ensure_module before pyannote imports speechbrain so a failed
# import returns an empty stub instead.
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

from tqdm import tqdm

from pyannote.audio import Pipeline

from .models import DiarizationSegment

# Pipeline cache. _pipeline_device is the device it was moved to; a different
# device reloads it.
_pipeline = None
_pipeline_device: Optional[str] = None


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
    """Load pyannote speaker-diarization-3.1 and cache it.

    Built into a local and published only after the device checks and
    ``.to(device)`` succeed, so a failure leaves the previous cache intact.
    """
    global _pipeline, _pipeline_device

    try:
        pipeline = Pipeline.from_pretrained(
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
    pipeline.to(torch.device(device))

    # Free the old pipeline reference before publishing the new one so peak
    # memory doesn't hold two pipelines.
    _pipeline = None
    _pipeline_device = None
    _pipeline = pipeline
    _pipeline_device = device
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

    # Reload when a job asks for a different device than the cached one.
    if _pipeline is None or _pipeline_device != device:
        load_pipeline(hf_token, device)

    kwargs: dict = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    else:
        if min_speakers is not None:
            kwargs["min_speakers"] = min_speakers
        if max_speakers is not None:
            kwargs["max_speakers"] = max_speakers

    # Pass a preloaded waveform so pyannote skips torchcodec, which needs
    # FFmpeg's shared build on Windows.
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
