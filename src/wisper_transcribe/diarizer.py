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
import time
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

from .config import DIARIZATION_MODEL

from .models import DiarizationSegment

# Pipeline cache. _pipeline_device is the device it was moved to; a different
# device reloads it.
_pipeline = None
_pipeline_device: Optional[str] = None


# pyannote step name -> (stage number, label, unit). pyannote 4.x reports
# progress (a total) only for these two steps. Embeddings progress counts
# batches of `embedding_batch_size` (chunk, local speaker) pairs, not chunks.
# The job page matches "segmentation" / "embedding" in these labels.
_STAGES = {
    "segmentation": (1, "segmentation", "chunk"),
    "embeddings": (2, "speaker embeddings", "batch"),
}


class _DiarizationProgressHook:
    """Translates pyannote pipeline hook calls into tqdm progress bars.

    pyannote calls hook(step_name, artifact, file, total, completed) at each
    chunk of the segmentation and embedding steps.  Each step gets one bar,
    labelled as a numbered stage of diarization so a new bar doesn't read as
    the step restarting.  A bar closes only when the step changes (or on
    close()): segmentation's ``completed`` can overshoot ``total`` and then
    report once more for the orphan last chunk, which must not open a second
    bar.  Each stage logs its duration when it first reaches its total.
    """

    def __init__(self) -> None:
        self._bar: Optional[tqdm] = None  # type: ignore[type-arg]
        self._step: Optional[str] = None
        self._started = 0.0
        self._logged = False

    def __call__(self, step_name, *args, total=None, completed=None, **kwargs):  # noqa: ARG002
        if total is None:
            return
        stage = _STAGES.get(step_name)
        if step_name != self._step:
            self.close()
            self._step = step_name
            self._started = time.monotonic()
            self._logged = False
            if stage:
                desc = f"  Diarizing {stage[0]}/{len(_STAGES)} · {stage[1]}"
            else:
                desc = f"  {step_name.capitalize()}"
            self._bar = tqdm(
                total=total,
                desc=desc,
                position=1,
                leave=False,
                unit=stage[2] if stage else "chunk",
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                dynamic_ncols=True,
            )
        if completed is not None and self._bar is not None:
            self._bar.n = min(completed, total)
            self._bar.refresh()
            if completed >= total and not self._logged:
                self._logged = True
                label, unit = (stage[1], stage[2]) if stage else (step_name, "chunk")
                plural = "batches" if unit == "batch" else f"{unit}s"
                tqdm.write(
                    f"  Diarize {label}: {total} {plural} "
                    f"({time.monotonic() - self._started:.1f}s)"
                )

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        self._step = None


def load_pipeline(hf_token: str, device: str):
    """Load the pyannote diarization pipeline and cache it.

    Built into a local and published only after the device checks and
    ``.to(device)`` succeed, so a failure leaves the previous cache intact.
    """
    global _pipeline, _pipeline_device

    try:
        pipeline = Pipeline.from_pretrained(
            DIARIZATION_MODEL,
            token=hf_token,
        )
    except Exception as e:
        from huggingface_hub.errors import GatedRepoError

        if isinstance(e, GatedRepoError):
            raise RuntimeError(
                "Your Hugging Face token can't access the diarization model. "
                f"Accept its terms at https://huggingface.co/{DIARIZATION_MODEL} "
                "(free, one-time), then retry."
            ) from e
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

    # Delegate to the warm worker when this thread is a web job (see ml_worker).
    from .ml_worker import delegated_call
    _delegated, _result = delegated_call(
        "diarize", (audio_path,),
        dict(hf_token=hf_token, device=device, num_speakers=num_speakers,
             min_speakers=min_speakers, max_speakers=max_speakers),
    )
    if _delegated:
        return _result

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
