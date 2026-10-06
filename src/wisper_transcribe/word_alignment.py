"""Forced word alignment: re-time Whisper's words against the audio.

Whisper's word timestamps are a by-product of decoding, and at speaker changes
they drift into the neighbouring diarization turn. This module crops each
transcription segment's audio and runs Qwen3-ForcedAligner over it, which
predicts a start and end for every word in one non-autoregressive pass. Only
``Word.start``/``Word.end`` change; the text is Whisper's.

Alignment never fails a job: any segment that can't be aligned (model load
failure, out-of-memory, over-long crop, word-list mismatch) keeps its Whisper
times.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import FORCED_ALIGNMENT_MODEL
from .models import TranscriptionSegment, Word

# The model rejects inputs longer than this.
_MAX_CROP_SECONDS = 180.0
_SAMPLE_RATE = 16000

# Segments per forward pass. On an M5 at batch 8, a 10-min clip aligns in
# ~11 s (MPS) and ~36 s (CPU); batch 16 on an RTX 3090 took 5.6 GB. A batch
# that runs out of memory is split in half and retried.
_BATCH_SIZE = {"cuda": 8, "mps": 8, "cpu": 4}

# Languages the aligner's word splitter supports (ISO 639-1 codes plus
# Cantonese), keyed by the codes and names wisper's language setting may hold.
_SUPPORTED_LANGUAGES = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "pt": "Portuguese", "ru": "Russian", "es": "Spanish",
}

# Model cache, covered by the one-job-at-a-time invariant like diarizer._pipeline.
_fa_model = None
_fa_processor = None
_fa_device: Optional[str] = None


@dataclass
class AlignmentStats:
    words_retimed: int = 0
    words_kept: int = 0
    seconds: float = 0.0


def _resolve_language(language: Optional[str]) -> tuple[bool, Optional[str]]:
    """Map wisper's language setting to (supported, aligner language).

    Empty and "auto" mean auto-detect: the default splitter handles
    space-delimited and CJK text, so pass None.
    """
    if not language or language.lower() == "auto":
        return True, None
    key = language.lower()
    for code, name in _SUPPORTED_LANGUAGES.items():
        if key in (code, name.lower()):
            return True, name
    return False, None


def _dtype_for(device: str):
    import torch

    # fp16 on MPS matched fp32 exactly and ran faster than bf16; bf16 on CPU
    # is slow.
    return {"cuda": torch.bfloat16, "mps": torch.float16}.get(device, torch.float32)


def load_aligner(device: str):
    """Load and cache the aligner processor and model on ``device``.

    ``transformers`` is imported here, not at module level, so importing this
    module stays cheap and tests patch this function instead of the library.
    """
    global _fa_model, _fa_processor, _fa_device

    from transformers import AutoModelForTokenClassification, AutoProcessor

    processor = AutoProcessor.from_pretrained(FORCED_ALIGNMENT_MODEL)
    # .to(device), not device_map=: device_map needs accelerate.
    model = AutoModelForTokenClassification.from_pretrained(
        FORCED_ALIGNMENT_MODEL, dtype=_dtype_for(device)
    ).to(device).eval()

    _fa_model = None
    _fa_processor = None
    _fa_device = None
    _fa_model, _fa_processor, _fa_device = model, processor, device
    return _fa_processor, _fa_model


def _is_oom(exc: BaseException) -> bool:
    import torch

    if isinstance(exc, torch.OutOfMemoryError):
        return True
    return isinstance(exc, RuntimeError) and "out of memory" in str(exc).lower()


def _free_cache(device: str) -> None:
    import torch

    if device == "cuda":
        torch.cuda.empty_cache()
    elif device == "mps":
        torch.mps.empty_cache()


def _run_batch(processor, model, device, crops, texts, language) -> list[list[dict]]:
    """One forward pass. Returns per-crop items, times relative to the crop."""
    import torch

    inputs, word_lists = processor.prepare_forced_aligner_inputs(
        audio=crops, transcript=texts, language=language
    )
    # Only floating tensors take the model dtype; ids and masks stay integer.
    inputs = {
        k: (v.to(device).to(model.dtype) if v.is_floating_point() else v.to(device))
        for k, v in inputs.items()
    }
    with torch.inference_mode():
        logits = model(**inputs).logits
    items = processor.decode_forced_alignment(
        logits, inputs["input_ids"], word_lists, model.config.timestamp_token_id
    )
    return [list(zip(wl, it)) for wl, it in zip(word_lists, items)]


def _align_batch(processor, model, device, jobs, language) -> dict[int, list[dict]]:
    """Align ``jobs`` ([(index, crop, tokens)]), halving the batch on OOM.

    Returns {index: items} for the segments that aligned; failed ones are
    simply absent, so they keep Whisper times.
    """
    try:
        results = _run_batch(
            processor, model, device,
            [crop for _, crop, _ in jobs],
            [" ".join(tokens) for _, _, tokens in jobs],
            language,
        )
    except InterruptedError:
        raise  # web job cancellation; never swallow it
    except Exception as exc:  # noqa: BLE001 - alignment must never fail the job
        if _is_oom(exc) and len(jobs) > 1:
            _free_cache(device)
            mid = len(jobs) // 2
            out = _align_batch(processor, model, device, jobs[:mid], language)
            out.update(_align_batch(processor, model, device, jobs[mid:], language))
            return out
        from tqdm import tqdm

        tqdm.write(f"  Alignment failed for {len(jobs)} segment(s), keeping Whisper times: {type(exc).__name__}")
        return {}

    out = {}
    for (idx, _, tokens), pairs in zip(jobs, results):
        # The processor re-splits the joined tokens; if that doesn't reproduce
        # our per-word split, the items can't be attributed to words.
        if [w for w, _ in pairs] != tokens:
            continue
        out[idx] = [item for _, item in pairs]
    return out


def _retime_segment(
    seg: TranscriptionSegment,
    word_tokens: list[list[str]],
    items: Optional[list[dict]],
    offset: float,
) -> tuple[list[Word], int]:
    """Build the segment's re-timed words. Returns (words, words_retimed).

    Each Whisper word owns ``len(word_tokens[i])`` consecutive items; a word
    with no alignable characters (pure punctuation) owns none and keeps its
    Whisper times, clamped between its re-timed neighbours.
    """
    if items is None:
        return [Word(w.start, w.end, w.text) for w in seg.words], 0

    words: list[Word] = []
    retimed: list[bool] = []
    pos = 0
    for w, tokens in zip(seg.words, word_tokens):
        if tokens:
            first, last = items[pos], items[pos + len(tokens) - 1]
            words.append(Word(first["start_time"] + offset, last["end_time"] + offset, w.text))
            retimed.append(True)
            pos += len(tokens)
        else:
            words.append(Word(w.start, w.end, w.text))
            retimed.append(False)

    # Clamp kept words between their re-timed neighbours.
    prev_end = None
    for w, r in zip(words, retimed):
        if not r and prev_end is not None:
            w.start = max(w.start, prev_end)
            w.end = max(w.end, w.start)
        prev_end = w.end
    next_start = None
    for w, r in zip(reversed(words), reversed(retimed)):
        if not r and next_start is not None:
            w.end = min(w.end, next_start)
            w.start = min(w.start, w.end)
        next_start = w.start

    return words, sum(retimed)


def align_words(
    wav_path: Path,
    segments: list[TranscriptionSegment],
    device: str,
    language: Optional[str] = None,
) -> tuple[list[TranscriptionSegment], AlignmentStats]:
    """Return copies of ``segments`` with forced-aligned word timestamps.

    Each segment is aligned against exactly ``[seg.start, seg.end]``; padding
    lets a segment's words claim its neighbour's audio. ``wav_path`` must be
    the pipeline's 16 kHz mono WAV.
    """
    # Delegate to the warm worker when this thread is a web job (see ml_worker).
    from .ml_worker import delegated_call
    _delegated, _result = delegated_call(
        "align_words", (wav_path, segments, device, language), {},
    )
    if _delegated:
        return _result

    from tqdm import tqdm

    t0 = time.monotonic()
    stats = AlignmentStats()
    total_words = sum(len(s.words or []) for s in segments)

    def unchanged(reason: str):
        tqdm.write(f"  Skipping word alignment ({reason}); keeping Whisper word times")
        stats.words_kept = total_words
        stats.seconds = time.monotonic() - t0
        return [_copy_segment(s, s.words) for s in segments], stats

    supported, lang = _resolve_language(language)
    if not supported:
        return unchanged(f"language {language!r} is not supported by the aligner")
    if not total_words:
        return [_copy_segment(s, s.words) for s in segments], stats

    try:
        if _fa_model is None or _fa_device != device:
            load_aligner(device)
        processor, model = _fa_processor, _fa_model

        from .audio_utils import load_wav_as_tensor

        audio = load_wav_as_tensor(wav_path)
        if audio["sample_rate"] != _SAMPLE_RATE:
            return unchanged(f"expected {_SAMPLE_RATE} Hz audio, got {audio['sample_rate']} Hz")
        wave = audio["waveform"].mean(dim=0).numpy()

        # Per-word tokens from the aligner's own splitter, so items map back
        # to Whisper words exactly (and Japanese/Korean splitter deps are
        # checked before any model work).
        word_tokens = [
            [processor.split_words_for_alignment(w.text, lang) for w in (s.words or [])]
            for s in segments
        ]
    except InterruptedError:
        raise
    except ModuleNotFoundError as exc:
        if exc.name == "transformers":
            # Existing install updated without reinstalling dependencies.
            return unchanged("transformers is not installed; re-run setup.sh / setup.ps1 "
                             "or `pip install -e .`")
        return unchanged(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return unchanged(f"{type(exc).__name__}: {exc}")

    jobs = []
    offsets = {}
    for i, seg in enumerate(segments):
        tokens = [t for wt in word_tokens[i] for t in wt]
        if not tokens or seg.end - seg.start > _MAX_CROP_SECONDS:
            continue
        a = max(0, int(seg.start * _SAMPLE_RATE))
        b = min(len(wave), int(seg.end * _SAMPLE_RATE))
        if b <= a:
            continue
        offsets[i] = a / _SAMPLE_RATE
        jobs.append((i, wave[a:b], tokens))

    # Similar lengths batch together, so less padding.
    jobs.sort(key=lambda j: len(j[1]))
    batch_size = _BATCH_SIZE.get(device, 4)

    aligned: dict[int, list[dict]] = {}
    with tqdm(
        total=len(jobs),
        desc="  Aligning",
        unit="seg",
        position=1,
        leave=False,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        dynamic_ncols=True,
    ) as pbar:
        for k in range(0, len(jobs), batch_size):
            batch = jobs[k:k + batch_size]
            aligned.update(_align_batch(processor, model, device, batch, lang))
            # Outside _align_batch's handler: web cancellation raises from here.
            pbar.update(len(batch))

    result: list[TranscriptionSegment] = []
    prev_end: Optional[float] = None
    for i, seg in enumerate(segments):
        if not seg.words:
            result.append(_copy_segment(seg, seg.words))
            continue
        words, n = _retime_segment(seg, word_tokens[i], aligned.get(i), offsets.get(i, 0.0))
        stats.words_retimed += n
        stats.words_kept += len(words) - n
        # Cross-segment order: a segment can't start before the previous ends.
        if prev_end is not None and words[0].start < prev_end:
            words[0].start = prev_end
            words[0].end = max(words[0].end, words[0].start)
        prev_end = words[-1].end
        result.append(_copy_segment(seg, words))

    stats.seconds = time.monotonic() - t0
    tqdm.write(
        f"  Aligned words: {stats.words_retimed} re-timed, {stats.words_kept} kept "
        f"({stats.seconds:.1f}s)"
    )
    return result, stats


def _copy_segment(seg: TranscriptionSegment, words) -> TranscriptionSegment:
    return TranscriptionSegment(
        start=seg.start,
        end=seg.end,
        text=seg.text,
        words=[Word(w.start, w.end, w.text) for w in words] if words is not None else None,
    )
