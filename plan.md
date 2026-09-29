# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Missing transcript file after a successful Transcribe job

A local-recording `JOB_TRANSCRIPTION` job reported COMPLETED and logged "Wrote `<id>.md`", and the recording's metadata points at that path, but the file never existed on disk. Ruled out: a CWD-relative write, a `WISPER_DATA_DIR` mismatch, and every delete path in `jobs.py` (the upload-cleanup helpers are gated on `job.is_web_upload`, which recordings never set). The job queue has no persistence, so the evidence was lost on restart.

**Next step:** run the server with `WISPER_DEBUG=1` during local-recording transcribes so a recurrence is captured. See `LIVE_AUDIO_TEST_PLAN.md` §4a.

### Ollama: empty responses from reasoning models

`ollama.py`'s `_post_chat` reads only `message.content` from the stream. A reasoning model can spend its whole budget on the `thinking` field and return no content, and a streamed `"error"` field is never checked. Both cases surface as `Ollama JSON response did not parse: ... Raw: ''`, which blames the parse layer. Seen intermittently when summarizing a ~150k-char transcript with `ollama-cloud`; a plain retry succeeded. A non-thinking local model instead ignored the `format` schema and returned prose.

**Decision needed:** add retry-on-empty-content and/or surface a streamed error distinctly. Either change touches the shared client code used by every provider.

---

## Manual verification owed

- **Live recording + campaign journal:** `LIVE_AUDIO_TEST_PLAN.md` — real-device capture, live transcript, journal browser flows, bulk delete, busy-queue notice.
- **Live Discord acceptance test.** The recording pipeline (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is covered by synthesized-PCM tests, but the JDA → socket → Python path needs one real session: record a few minutes with 2+ speakers, play the per-user WAVs, and run Transcribe.
- **macOS loopback (PR #59).** Record page on a Mac with BlackHole installed: BlackHole appears under System Audio and captures audio.

---

## Forced word alignment (planned — next)

Speaker identity is now consistent (#63); the remaining attribution error is **timing**. `aligner.py` gives each word to the diarization turn it overlaps most, using faster-whisper's word timestamps. Those are a by-product of decoding, not a measurement, and at speaker changes they put words in the neighbouring turn. `_smooth_word_speakers` (fold runs of ≤2 words or <1 s into the neighbours) papers over this and also swallows genuine one-word interjections.

Forced alignment re-times each already-transcribed word against the audio with a CTC acoustic model. faster-whisper stays the decoder; only `Word.start`/`Word.end` change.

### Spike results (2026-09-29, real session audio)

10-min excerpt of Hanataz 2026-09-19 (30:00–40:00), production transcribe + community-1 diarize. Spike scripts lived in the session scratchpad and are not committed.

- **Model:** `torchaudio.pipelines.MMS_FA`, already importable from `torchaudio` (spike ran on torchaudio 2.11 + torch 2.13; `pyproject.toml` has `>=2.8.0` with no upper bound), so no new pip dependency. 315M params, 1.26 GB one-time download to the torch hub cache (`~/.cache/torch/hub/checkpoints/model.pt`).
- **Speed:** 10 min aligned in 5–6 s on an RTX 3090 (~80 s for a 2.5 h session); ~7× realtime on 8 CPU threads (~21 min for 2.5 h).
- **Memory:** fp16 model, 1.0 GB peak VRAM, same proxy, probes, inversion and edge counts as fp32 (1.9 GB).
- **Effect:** words whose midpoint lies inside their assigned speaker's diarization turn: **90.0% → 97.4%**. The final speaker changes on 32 of 1369 words (2.3%) across 80 speaker changes.
- **Whisper is sometimes seconds off, not ~100 ms.** Median |Δstart| 87 ms, p90 523 ms, 49 words shifted >1 s. Spot checks by re-transcribing short windows: "he's" whisper 39.15 s → aligned 41.57 s (aligned correct); "i'm" 47.03 → 50.20 (aligned correct).
- **Large shifts are mostly right.** Of the >1 s shifts where re-transcription heard the word at exactly one of the two candidate times, the aligned time won 31, Whisper's 3. Function words ("the", "you", "and") turn up almost anywhere in dense crosstalk, which may favour the aligned candidate; on content words only (≥4 letters, no stopwords) it's 8 vs 1. Directional, small sample.
- **Plain alignment drifts under crosstalk; the star token mostly fixes it.** Without it, "blend in" moved 114.85 → 119.83 s, which was wrong (Whisper was right): untranscribed speech from other players forced the aligner to stretch. With `get_model(with_star=True)` and a `*` token between every word to absorb audio not in the transcript, "blend" stays at 114.90 while the two real fixes above still happen. Star mode is required, not optional. It isn't a complete fix: the next word, "in", still moves 115.13 → 119.31 (wrong), and "grease" 162.76 → 164.48 (wrong). These are among the 3 Whisper wins above.
- **Alignment scores are not a usable quality gate.** The correct "blend" alignment scored 0.007, and 604 of 1369 words scored <0.3. Don't threshold on them.
- **Crop edges are not a quality gate either.** 32 words land within 0.1 s of their crop edge, but none of them is a >1 s shift, and of the known-bad shifts only one is edge-pinned. Reverting edge-pinned words to Whisper times lowers the proxy (97.4% → 97.2%).
- **Per-segment ±0.25 s crops beat wider context.** Padding 1–2 s causes 33–49 order inversions (words jump into the neighbouring segment's audio) with no proxy gain. Aligning 30–120 s windows of consecutive segments has no crop-edge pinning and 0–5 inversions, but loses the re-transcription audit on the words where it disagrees with per-segment crops (5 heard vs 13): without the segment boundary as a prior, words drift.

### Alternatives checked (2026-09-29)

- **Qwen3-ForcedAligner-0.6B** (Apache-2.0, 11 languages). Run on the same clip with the same ±0.25 s per-segment crops: proxy 97.1%, all three probes correct, 16 s per 10 min (~3× slower), 1.8 GB VRAM (bf16). Constraints:
  - 80 ms timestamp bins.
  - 180 s input cap.
  - It often puts a segment's first word at the crop start.
  - `qwen-asr` pins `transformers==4.57.6`, which needs `huggingface_hub<1.0`; the venv has 1.23. That's resolvable, since pyannote and faster-whisper only need ≥0.28, but it adds transformers plus gradio, flask, librosa, nagisa and soynlp.
  - The native `-hf` model needs an unreleased transformers.

  No accuracy edge over MMS_FA here. It's the licence fallback.
- **Benchmarks** (FA-Bench Buckeye, via summarised fetch, approximate): MMS-FA ~35 ms MAE, Qwen3-FA ~35 ms, WhisperX ~40 ms. These measure tens-of-ms precision on clean reference transcripts, not multi-second misplacement under crosstalk, which is this project's failure mode.
- **nyra-forced-aligner:** best on Buckeye (~20 ms MAE), but non-commercial licence, English-only, needs espeak-ng, and absorbs untranscribed speech into neighbouring words (the drift star prevents). **FuseAlign:** built for in-the-wild audio, but no released weights. **WhisperX / `ctc-forced-aligner`:** same MMS/wav2vec2 CTC approach with heavier or extra dependencies.

### Decisions

- **Engine:** `torchaudio.functional.forced_align` + `MMS_FA` with star. `forced_align` was slated for removal in torchaudio 2.9 but kept after user feedback ([pytorch/audio#3902](https://github.com/pytorch/audio/issues/3902), 2026-01-22 update); `Wav2Vec2FABundle` is the documented path. No deprecation warning on 2.11.
- **Recipe:** model in fp16 on CUDA, emissions cast to float32 before `forced_align`. The spike also applied `log_softmax` on top of the `with_star` output. That's a per-frame constant shift, so it changes only the scores, never the path: 0 of the frames in 195 segments differ with and without it. Optional; keep it only if scores get used.
- **License, needs sign-off:** the MMS_FA weights are **CC-BY-NC 4.0**. The code downloads them at runtime (not redistributed) and this is a personal, non-commercial tool, so it's fine for current use; state it in the docs. If that changes, Qwen3-ForcedAligner (Apache-2.0, above) is the fallback.
- **Languages:** MMS_FA takes romanized lowercase text (`a–z` and `'`). Latin-script languages work after accent stripping (`unicodedata` NFKD). Non-Latin scripts need `uroman`; phase 1 keeps Whisper times for them rather than adding the dependency. No `language` parameter: `transcribe()` discards `info.language` on every path (faster-whisper, MLX, parallel worker), so with `--language auto` the requested language says nothing about the audio. The per-word rule covers it instead: a non-Latin word normalizes to empty and keeps its Whisper times.
- **Default:** config key `forced_alignment` = `auto` | `true` | `false`. `auto` aligns when diarization runs on a GPU, since CPU adds ~20 min per 2.5 h session. Never with `--no-diarize`; timing only matters for speaker attribution.
- **Apple Silicon:** `forced_align` has CPU and CUDA kernels only (its docstring: `devices:: CPU CUDA`). Emissions run on MPS, the alignment step on CPU.
- **Docker:** the weights must not be downloaded at image build time; baking CC-BY-NC weights into an image is redistribution. Runtime download only.

### Open questions

- **Does `auto` count MPS as a GPU?** Decide once MPS speed is known.
- **Measurement harness:** the spike scripts weren't committed. Decide where the proxy/audit scripts live (e.g. `scripts/`, gitignored output) before phase 2.
- **torch/torchaudio pairing:** torchaudio 2.11 declares no torch requirement and is the latest release, while torch is at 2.14. The spike ran with torch 2.13; a fresh install gets 2.14 with a compiled `libtorchaudio` that hasn't been tested. Add a torch upper bound, a torchaudio upper bound, both, or neither?

### Phases

1. **`word_alignment.py` module + tests.**
   - `align_words(wav_path, segments, device) -> list[TranscriptionSegment]`: per Whisper segment, crop `[start − 0.25 s, end + 0.25 s]`, emissions per the recipe above, targets `* w1 * w2 * … *`, `forced_align` + `merge_tokens`, map token spans back to words by target-index ownership, frames → seconds by `crop_samples / frames / 16000`.
   - Words with no alignable characters after normalization (e.g. "20", "&") keep Whisper times, clamped between their re-timed neighbours so word order stays monotonic.
   - Cross-segment order: the padded crops overlap, so a segment's first word can land before the previous segment's last word (7 cases in the spike). Clamp its start to the previous word's end (and its end to at least its start).
   - Any per-segment failure (empty emission, more targets than frames, exception) keeps that segment's Whisper times. Alignment never fails the job.
   - Lazy model cache as a module global (`_fa_model`, `_fa_device`) like `diarizer._pipeline`; covered by the one-job-at-a-time invariant; tests reset it.
   - Audio via `audio_utils.load_wav_as_tensor()`, never `torchaudio.load` (the torchcodec/Windows constraint).
   - The per-segment loop runs under a tqdm bar ("Aligning"), like "Transcribing". Web job cancellation and the SSE log only see tqdm writes, so a single summary line at the end would leave ~80 s (GPU) to ~21 min (CPU) that can't be cancelled.
   - Tests with a mocked model: span → word mapping, star ownership, unalignable-word clamping (incl. non-Latin words), failure fallback, monotonic output within a segment and across neighbouring segments.
2. **Pipeline + config wiring.**
   - `pipeline.process_file()`: after transcription, before `align()`, when diarization ran and `forced_alignment` resolves on. In `parallel_stages` mode it runs in the main process after both futures return (it needs only the WAV and the segments).
   - After the progress bar, log one summary line: words re-timed, words kept, time taken.
   - The MLX path (Apple Silicon) yields the same `Word` objects; emissions on MPS, alignment on CPU.
   - Config key, web Config page field, `wisper transcribe --forced-align/--no-forced-align`. `wisper setup` pre-downloads the model when enabled.
   - Docker: compose mounts only `./cache:/root/.cache/huggingface`, but torch hub writes to `/root/.cache/torch`. Set `TORCH_HOME` to a path under the existing cache mount (or add a mount), or the 1.26 GB model re-downloads per container.
3. **Re-tune smoothing.** With aligned words, reduce `_MICRO_RUN_MAX_WORDS` / `_MICRO_RUN_MAX_SECONDS`, or skip smoothing for aligned segments, so real interjections survive. Decide from the measurement below.
4. **Optional: `exclusive_speaker_diarization` for word assignment.** community-1's exclusive view is built for reconciling with transcripts. Try it as a measurement arm: aligned words × {regular, exclusive} turns. Embedding and excerpt selection keep the regular, overlap-aware view either way.

### Measurement (gate for phases 2–4)

- **Automatic proxy:** % of words whose midpoint is inside their assigned speaker's turn, plus the raw micro-run count before smoothing. Spike baseline: 90.0% (Whisper) vs 97.4% (star-aligned). Sanity check only: aligned words shrink onto speech, which is where turns are, so the proxy partly measures two acoustic segmentations agreeing, not correctness.
- **Manual ground truth:** ~3 excerpts of 3 min with heavy crosstalk from different sessions. At every speaker change, mark the correct speaker of the 2 words on either side (~100–150 judgements). Compare Whisper-timed vs aligned, and the smoothing variants. Ship if boundary-word accuracy improves and no excerpt gets worse.
- **Large-shift audit:** in one session, check every word shifted >1 s by re-transcribing a window `[t − 0.5, t + 1.0]` around each candidate time. Count only words heard at exactly one candidate; the window is wide enough that candidates <1.5 s apart are often both heard.

### Risks

- **Whisper text errors** (misheard or hallucinated words) still get force-placed somewhere; star absorbs some of it. Watch the >1 s shifts in the audit.
- **Residual crosstalk drift:** star doesn't stop every word from being pulled into untranscribed speech ("in", "grease" above). ~1 in 10 large shifts in the spike was wrong. No cheap detector found yet (scores and crop edges both fail).
- **Overlapped speech:** one word timeline can't represent two people talking at once. Alignment only helps the words Whisper transcribed.
- **CPU-only installs:** ~20 min per 2.5 h session, which is why `auto` is GPU-only.
- **torchaudio maintenance mode:** `forced_align` is kept today, and torchaudio has no upper bound, so a future release could drop it on a fresh install. If that happens, pin torchaudio or vendor the CTC alignment (a small dynamic program).
- **VRAM:** in the sequential path the MMS model stays resident next to Whisper and pyannote as module globals. ~1 GB in fp16, so tight only on small cards.

---

## Speaker consistency — remaining

Shipped in #63; the design is in `architecture.md`. What's left:

- **Profile cleanup (user action).** Re-enroll every profile after the embedding-model change; delete the `speaker_*` / `SPEAKER_NN` junk and duplicate profiles; enroll Mike and Ben from sessions where they're clearly separated. Consider a `wisper speakers doctor` check that flags identical or near-identical (>0.95) profile embeddings.
- **Diarization measurement set.** 8–12 hand-corrected excerpts of 2–3 min, stratified by speaker count (2–3 / 4–5 / 6–8), in-room vs remote, low vs high overlap. Report DER split into missed / false alarm / confusion (`pyannote.metrics`) plus JER, and compare configs with a paired bootstrap over recordings (B ≥ 1000). The 0.55 threshold and the community-1 choice rest on one session pair until this exists.
- **Ruled out for now:** Sortformer (4-speaker cap), DiariZen (CC-BY-NC), NVIDIA Nemotron diarization (no independent validation). Revisit only if community-1 plateaus on the measurement set.

---

## Live recording — feature requests

- **Change input devices mid-session.** `LocalCaptureManager.start_session()` binds both capture threads to fixed device IDs; switching today means Stop + Start (a new `Recording` and a gap in the transcript). Needs capture threads that can restart against a new device while the tick thread, segment writers, and `Recording` keep running. Needs a design pass first.
- **Per-speaker diarization on the live system track.** The system track is a single RMS-attributed "Other", because OS loopback is already a mixed-down stream. pyannote only gives consistent labels across a whole-file pass; live use would need an incremental layer (embed each turn, match against a running per-session speaker pool) plus added latency in the live loop. Revisit if the You/Other split becomes limiting.
- **Channel picker on the Record page.** `GET /api/record/channels` already lists the guilds and voice channels the bot can see; the Record page still takes raw IDs or a preset.
- **Replay markers into the ticker on reload.** Markers persist on `Recording.markers` and show on the detail page, but a page reload doesn't re-insert them into the live ticker (only transcript lines come back through SSE).

---

## Intel Arc GPU support (planned — not started)

Design only; no code written. File:line references date from the original design spike — re-locate them before implementing.

### Context

wisper-transcribe today accelerates on **NVIDIA (CUDA)** and **Apple Silicon (MPS)**. We want **Intel discrete Arc GPUs** (Alchemist + Battlemage, validated on an **A310**) to be a first-class target — "as flawless as CUDA."

The hard constraint that shapes everything: **faster-whisper / CTranslate2 has no Intel backend** — it runs CPU or CUDA only. So transcription on an Arc card requires a *second inference engine*, not a device-string tweak. Diarization/embedding (pyannote on PyTorch) **can** run on Intel via the PyTorch `xpu` device, so those are mostly plumbing.

**The codebase already has the pattern we need.** The MLX path for Apple Silicon (transcriber.py:171-186) dispatches to an alternate backend (`_transcribe_mlx`) based on device, returning the same `list[TranscriptionSegment]`. The OpenVINO backend mirrors this almost 1:1.

### Locked decisions (from PM)

| Decision | Choice | Implication |
|---|---|---|
| User-facing device token | **`intel`** | `--device intel`, `device = "intel"`. Translated internally: torch `xpu`, OpenVINO `GPU`. |
| `device=auto` behavior | **Auto-select** | Resolve order: CUDA → **intel** → MPS → CPU. No flags needed when an Arc card is present. |
| XPU diarization op-gap | **Warn loudly, continue on CPU** | Transcription stays on the Arc GPU; diarization/embedding retry on CPU with a prominent per-run warning. |

### Architect-level calls (documented for the record)

- **Transcription engine = `optimum-intel` (`OVModelForSpeechSeq2Seq`).** Auto-converts HF Whisper models to OpenVINO IR on first run and caches them (mirrors MLX's auto-download UX). `openvino-genai`'s `WhisperPipeline` is faster but lower-level — a **future perf lever**, not v1.
- **OpenVINO uses HF-format Whisper models** (e.g. `openai/whisper-large-v3-turbo`), a *separate download* from faster-whisper's CTranslate2 models. Documented; not a blocker.
- **`compute_type` does not apply to the OpenVINO path.** CT2 quant types are CT2-only. On `intel`, OpenVINO defaults to FP16 on GPU; INT8 (NNCF) is future. The intel path **skips `resolve_compute_type()`**.
- **Optional dependency, mirroring `[macos]`/mlx.** Core install unchanged; Intel is `pip install "wisper-transcribe[intel]"` + a torch XPU-index install (handled by Docker/setup scripts, like cu126).

### Device translation model (the heart of the change)

One user token (`intel`) fans out to two frameworks. Centralize the mapping in one place.

```
user "intel"
   ├─ transcription  → OpenVINO  device="GPU"   (optimum-intel)
   └─ diarization /  → PyTorch   torch.device("xpu")
      embedding
```

**Add to `config.py`:**

```python
def torch_device_string(device: str) -> str:
    """Map the user-facing device token to a torch device string.
    'intel' is exposed to users but PyTorch/IPEX call Intel GPUs 'xpu'.
    Everything else passes through unchanged.
    """
    return "xpu" if device == "intel" else device
```

**Extend `get_device()` (config.py:148) — the order encodes the auto-select decision:**

```python
def get_device() -> str:
    """Return 'cuda', 'intel', 'mps', or 'cpu' based on available hardware."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            return "intel"          # ← Intel Arc, auto-selected after CUDA
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except ImportError:
        return "cpu"
```

> **Builder note:** `torch.xpu` is provided natively by the **PyTorch XPU wheel** (`https://download.pytorch.org/whl/xpu`, the Intel analog of cu126). IPEX adds extra op coverage/perf but `torch.xpu.is_available()` works without it on the XPU build. The `hasattr(torch, "xpu")` guard keeps CPU/CUDA wheels (no `torch.xpu`) safe.

### Implementation phases

Each phase is committed separately and **pauses for PM review**. **Docs are updated in the same commit as the code** (CLAUDE.md rule). Hardware-validation milestones run on the Proxmox A310 (native Linux build or the Docker `intel` target); Windows-native is validated-by-proxy.

#### Phase 1 — Device plumbing & detection (no hardware; 100% unit-testable)

**Goal:** `intel` is a recognized device everywhere CUDA/MPS are, `auto` detects it, and selecting it without the backend gives a clean "not installed" error.

| File | Change |
|---|---|
| config.py | Add `torch_device_string()`; extend `get_device()`. `resolve_compute_type()` unchanged (CT2-only). |
| cli.py:29 | Add `"intel"` to `--device` `click.Choice`. |
| cli.py:202-208 | `setup` command: add `"intel": "Intel Arc GPU (XPU/OpenVINO)"` label + note (transcription=OpenVINO, diarization=XPU). |
| web/routes/config.py:28 | Add `"intel"` to the device choices array. |
| docs/configuration.md, docs/cli-reference.md | Document the `intel` device value. |

**Tests** (test_config.py, test_cli.py): `get_device()` → `"intel"` when `torch.xpu.is_available()` mocked True (cuda False); `torch_device_string("intel") == "xpu"` + passthrough; CLI/web accept `--device intel`.

#### Phase 2 — OpenVINO transcription backend ⭐ core feature

**Goal:** `--device intel` transcribes Whisper on the Arc GPU. **Mirror the MLX structure exactly.**

`transcriber.py` additions (model after `_MLX_MODEL_MAP` / `_is_mlx_available` / `_transcribe_mlx`):

```python
_OPENVINO_MODEL_MAP = {
    "tiny":            "openai/whisper-tiny",
    "base":            "openai/whisper-base",
    "small":           "openai/whisper-small",
    "medium":          "openai/whisper-medium",
    "large-v3":        "openai/whisper-large-v3",
    "large-v3-turbo":  "openai/whisper-large-v3-turbo",
}

def _is_openvino_available() -> bool:
    """True if optimum-intel + openvino are importable. Cheap find_spec check
    (mirrors _is_mlx_available) so it's safe to call from the uvicorn process;
    the heavy import happens inside _transcribe_openvino (worker/subprocess)."""
    import importlib.util
    return (importlib.util.find_spec("optimum") is not None
            and importlib.util.find_spec("openvino") is not None)

def _transcribe_openvino(audio_path, model_size="medium", language="en",
                         initial_prompt=None, hotwords=None):
    """Transcribe on an Intel GPU via OpenVINO (optimum-intel).
    Returns list[TranscriptionSegment] — same contract as faster-whisper/MLX."""
    from optimum.intel import OVModelForSpeechSeq2Seq
    from transformers import AutoProcessor, pipeline as hf_pipeline
    from tqdm import tqdm

    repo = _OPENVINO_MODEL_MAP.get(model_size, f"openai/whisper-{model_size}")
    tqdm.write(f"  Using OpenVINO backend ({repo}) on Intel GPU")

    # export=True converts HF → OpenVINO IR on first run and caches it.
    # device="GPU" targets the Intel Arc.
    model = OVModelForSpeechSeq2Seq.from_pretrained(repo, export=True, device="GPU")
    processor = AutoProcessor.from_pretrained(repo)
    asr = hf_pipeline(
        "automatic-speech-recognition",
        model=model, tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        chunk_length_s=30, return_timestamps=True,
    )
    # hotwords → initial_prompt prefix, same trick as MLX (no native hotwords param).
    prompt = initial_prompt or ""
    if hotwords:
        hw = ", ".join(hotwords)
        prompt = f"{hw}. {prompt}".strip() if prompt else hw
    gen = {"language": language} if language else {}
    if prompt:
        gen["initial_prompt"] = prompt   # passed via generate_kwargs where supported
    result = asr(str(audio_path), generate_kwargs=gen)

    segs = []
    for ch in result.get("chunks", []):
        start, end = ch.get("timestamp", (None, None))
        text = (ch.get("text") or "").strip()
        if text and start is not None and end is not None:
            segs.append(TranscriptionSegment(start=float(start), end=float(end), text=text))
    return segs
```

Dispatch in `transcribe()` — add a branch beside the MLX one (transcriber.py:167-187):

```python
    if device == "auto":
        device = get_device()

    if device == "intel":
        if not _is_openvino_available():
            raise RuntimeError(
                "Intel GPU transcription needs the OpenVINO backend.\n"
                "Install it with: pip install 'wisper-transcribe[intel]'\n"
                "Or use --device cpu."
            )
        return _transcribe_openvino(audio_path, model_size=model_size,
                                    language=language, initial_prompt=initial_prompt,
                                    hotwords=hotwords)
    # ... existing MLX (mps) branch and faster-whisper path unchanged ...
```

> **Builder notes:**
> - `load_model()` / CTranslate2 are **never touched** on the intel path — `_transcribe_openvino` returns first. Leave `load_model` as-is.
> - `compute_type` is intentionally ignored on intel. If user set non-`auto`, `tqdm.write` a one-line note it doesn't apply to OpenVINO.
> - `vad_filter` has no OpenVINO equivalent here (like MLX) — silently skipped.
> - Confirm the exact `generate_kwargs` prompt key against the installed transformers version during hardware bring-up; `initial_prompt` support varies. **Flag for hardware milestone.**

| File | Change |
|---|---|
| pyproject.toml:44 | New extra: `intel = ["optimum-intel[openvino]>=1.20", "openvino>=2024.4", "transformers>=4.45"]`. (torch XPU build is index-url — Phase 4.) |
| docs/setup.md, architecture.md | Add OpenVINO backend to the component table + a design-decision section. |

**Tests** (test_transcriber.py): patch `_is_openvino_available`→True and mock the optimum/HF pipeline factory; assert `transcribe(..., device="intel")` returns mapped `TranscriptionSegment`s; assert chunk→segment mapping (drop empty/None); assert clean RuntimeError when unavailable. **No real model load** — mock the pipeline as `test_transcriber.py` mocks `WhisperModel`.

**🔌 Hardware milestone #1 (A310):** real transcription on GPU; verify IR conversion + cache; sanity-check WER vs CPU.

#### Phase 3 — XPU diarization & embedding (warn-and-fallback)

**Goal:** pyannote diarization + speaker embeddings run on the Arc GPU via `xpu`, with a loud CPU fallback on op gaps.

`diarizer.py` — `load_pipeline()` (diarizer.py:91-121):

```python
    import torch
    from .config import torch_device_string
    if device == "intel":
        if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            raise RuntimeError("Intel XPU not available. Install the PyTorch XPU "
                               "build, or use --device cpu.")
    # ... existing cuda / mps validation ...
    _pipeline.to(torch.device(torch_device_string(device)))   # 'intel' → 'xpu'
```

`diarize()` — wrap execution with warn-and-fallback (realizes the PM decision):

```python
    try:
        diarization = _pipeline(audio_dict, hook=hook, **kwargs)
    except Exception as exc:
        if device == "intel":
            from tqdm import tqdm
            tqdm.write("⚠  INTEL GPU DIARIZATION FAILED — falling back to CPU for "
                       f"diarization (transcription stays on GPU). Reason: {exc}")
            load_pipeline(hf_token, "cpu")          # reload pipeline on CPU
            diarization = _pipeline(audio_dict, hook=hook, **kwargs)
        else:
            raise
```

`speaker_manager.py` — `_load_embedding_model()` (speaker_manager.py:101-121): mirror the translation (`device in ("cuda","mps","intel")` → `.to(torch.device(torch_device_string(device)))`) and the same try/warn/CPU-fallback around `inference.crop(...)` in `extract_embedding()`.

> **Builder notes:**
> - The fallback **reloads** the module-level `_pipeline` on CPU (can't reliably move a partially-failed pipeline). After a fallback the global stays CPU for the rest of the run — acceptable; resets next process.
> - Keep the warning prominent and **every run** (PM decision), not once-per-session.

**Tests** (test_diarizer.py, test_speaker_manager.py): mock `torch.xpu.is_available()`→True, assert `_pipeline.to` called with `torch.device("xpu")` for `device="intel"`; simulate the pipeline call raising once, assert warning + CPU reload+retry returns `DiarizationSegment`s.

**🔌 Hardware milestone #2 (A310):** diarization on GPU; deliberately exercise the fallback and confirm warning + CPU completion.

#### Phase 4 — Packaging: Docker, Linux, Windows

**Docker** — new `intel` stage in `Dockerfile` mirroring `gpu`, plus the **Intel GPU runtime** the OpenVINO GPU plugin needs (the extra step CUDA doesn't have):

```dockerfile
# ── intel target ──────────────────────────────────────────────────────────
FROM base AS intel
# Intel GPU runtime for the OpenVINO GPU plugin + level-zero (apt, from Intel repo):
#   intel-opencl-icd, libze1, libze-intel-gpu1   (package names per Intel's docs)
RUN pip install --no-cache-dir -e ".[intel]" \
 && pip install --no-cache-dir --upgrade "torch>=2.8.0" "torchaudio>=2.8.0" \
        --index-url https://download.pytorch.org/whl/xpu \
 && curl -sL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" \
         -o /app/src/wisper_transcribe/static/htmx.min.js \
 && python -m wisper_transcribe.tailwind
ENTRYPOINT ["wisper"]
CMD ["--help"]
```

**docker-compose.yml** — `wisper-intel` + `wisper-intel-web` services with `/dev/dri` passthrough (Intel analog of the nvidia `deploy.resources` block):

```yaml
  wisper-intel-web:
    build: { context: ., target: intel }
    image: wisper-transcribe:intel
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "render"        # host 'render' gid; may need a numeric gid on some hosts
    # ... shared volumes/env/ports as the other services ...
```

**Makefile:** `start-intel` (`docker compose up wisper-intel-web`), `build-intel`, `shell-intel`.

**setup scripts:**
- `setup.sh` (Linux): detect Arc (`lspci | grep -i 'VGA.*Intel.*Arc'` or `/dev/dri/renderD*` + `clinfo`); install torch from xpu index; `pip install -e ".[intel]"`; verify `python -c "import torch; print(torch.xpu.is_available())"`.
- `setup.ps1` (Windows): detect via `Get-CimInstance Win32_VideoController` name match `Arc|Intel`; install xpu-index torch; `[intel]` extra. **Windows = code-complete, validated-by-proxy.**

| File | Doc |
|---|---|
| docs/docker.md | Intel section: `/dev/dri` passthrough, `render` group, runtime packages, `make start-intel`, `wisper --device intel` verify. |
| docs/setup.md | Intel install paths (Docker / Linux / Windows), model-storage note (separate HF-format models), A310/4 GB model-size guidance. |

> **Builder notes / risks:**
> - Intel **compute-runtime apt package names drift** across base-image versions — confirm against Intel's current install guide during the Docker build; most failure-prone step. Pin versions once known good.
> - On Proxmox: the A310 must be **passed through to the VM/LXC** running Docker, and `/dev/dri/renderD128` visible inside the container; the container user needs the host `render` gid.
> - torch XPU wheels bundle the SYCL runtime, but the **OpenVINO GPU plugin** still needs the system `intel-opencl-icd`/level-zero — don't assume torch wheels cover OpenVINO.

**🔌 Hardware milestone #3 (A310):** build + run the Docker `intel` target on the Proxmox host end-to-end; repeat with a native Linux `setup.sh` build.

#### Phase 5 — Final docs consolidation

`architecture.md`: module-map entries for the OpenVINO backend + `torch_device_string`; a "Design Decisions" entry (engine choice, device-token mapping, warn-and-fallback); **Known Constraints** rows (Intel transcription requires OpenVINO not CT2; XPU diarization op gaps → CPU fallback; Windows validated-by-proxy). `README.md`: add Intel to the accelerator list if the quickstart mentions GPUs. Remove these Intel entries from `plan.md` as phases complete.

### Complete device-touchpoint inventory (so nothing is missed)

Representative branch points (most handled via the `auto`-resolve + `torch_device_string` helper, so few need bespoke logic):

- **Choice lists:** cli.py:29, web/routes/config.py:28 — add `"intel"`.
- **Detection/resolve:** config.py:148 (`get_device`), pipeline.py:375 & pipeline.py:594 (`auto`→`get_device`, already generic), transcriber.py:167.
- **Backend dispatch:** transcriber.py:171 (add intel branch beside MLX).
- **torch `.to(device)`:** diarizer.py:120, speaker_manager.py:118-120 — route through `torch_device_string`.
- **Validation blocks:** diarizer.py:108-119, transcriber.py:120-128 — add intel/xpu checks.
- **Parallel-workers guard** pipeline.py:596 (`workers>1 and device!="cpu"` → clamp to 1): **no change** — `intel` is non-cpu so correctly clamped to a single worker, like cuda.
- **MLX gates** pipeline.py:410, transcriber.py:171: stay `mps`-only — **no change**.

### Testing strategy (CI stays GPU-free)

All ML mocked, per CLAUDE.md. New seams are patchable like the existing ones:

- `_is_openvino_available()` and the optimum/HF pipeline factory are the mock points for transcription (parallel to `WhisperModel`).
- `torch.xpu.is_available()` is patched for detection + diarization device tests.
- Fallback paths tested by making the mocked pipeline call raise once, then asserting the warning + CPU retry.
- New/extended files: test_transcriber.py, test_diarizer.py, test_speaker_manager.py, test_config.py, test_cli.py, plus test_web_routes.py for the device choice.

**Hardware validation runbook** (A310, outside CI — Linux native or Docker `intel`):
1. `wisper setup` reports `Intel Arc GPU (XPU/OpenVINO)`.
2. `wisper transcribe sample.mp3 --device intel` → transcript; first run converts/caches IR; GPU utilized (`intel_gpu_top`).
3. `--device auto` selects intel automatically.
4. Force an XPU diarization op gap → loud warning + CPU completion (transcription still on GPU).
5. Docker `make start-intel` → web UI transcribes a file end-to-end with `/dev/dri` passthrough.

### Risks & open items

- **R1 — Intel compute-runtime packaging** (Docker apt names / Proxmox passthrough). Highest-risk; resolved empirically at Phase 4 hardware bring-up.
- **R2 — `generate_kwargs` prompt key** for OpenVINO transcription varies by transformers version (Phase 2 hardware check).
- **R3 — IPEX vs native `torch.xpu`** op coverage for pyannote; warn-and-fallback (Phase 3) is the safety net by design.
- **R4 — Windows-native is validated-by-proxy** (no A310 on the Windows box) — documented, not claimed as proven.

---

## Job cancellation — best-effort GPU stop

Stopping an in-flight transcribe job marks it Failed, but the GPU keeps running until the current CTranslate2 batch finishes.

**Why cancellation is cooperative-only:**
- `cancel_event.is_set()` is checked only inside `capturing_write()` and `ProgressCatcher.write()`, which fire when tqdm emits output.
- Between tqdm ticks the worker is inside CTranslate2's C++ code, which has no Python yield points or cancel hook.
- `pipeline.py` has no awareness of the job's cancel event.

**Options:**
1. **Run transcription in a subprocess and terminate it on cancel.** `parallel_stages = true` already does this for concurrent transcribe + diarize. Generalizing it costs ~1–2 s of startup per job but releases the GPU cleanly.
2. **Check the cancel event between segments in `pipeline.process_file()`.** Cheaper, but doesn't help mid-batch.
3. **Document cancel as best-effort** and add a force-quit button that terminates at the OS level.

**Recommendation:** option 1, reusing the parallel-stages subprocess plumbing. Deferred until cancellation is used often enough to justify it.

---

## DAVE sidecar → Python migration (parked)

The Java sidecar (JDA 6.3.0 + JDAVE 0.1.8) receives and decrypts DAVE-encrypted audio end-to-end. DAVE is mandatory for non-stage voice, so the only question is where it's implemented. DAVE is MLS over OpenMLS and every path depends on a native (Rust/JNI) binding; the choice is which language wraps it.

**Python readiness (as of 2026-06):**
- **pycord PR #3159** — DAVE receive for pycord, which has native voice receive. Approved but still a draft, milestoned for 2.9.0rc1. The right target once released.
- **discord.py PR #10300** — shipped in 2.7.x but flagged tentative. discord.py has no first-class voice receive, so it doesn't fit a recording bot.
- **`davey`** — the OpenMLS binding both use; beta (v0.1.5) with no usage docs.

**Verdict:** keep the sidecar. Revisit when pycord 2.9 ships #3159 as stable.

**Migration path:**
1. Delete `discord-bot/` (the Gradle/Java project).
2. Write a ~100-line Python replacement that emits the same wire format over the existing Unix socket: length-prefixed user_id + 48 kHz stereo PCM, including the pre-mixed `__mixed__` stream.
3. Point `BotManager` at the Python script instead of the JAR.
4. Remove the Java builder stages from `Dockerfile` and the Java 25 requirement from launchers and docs.

Nothing else changes; the wire protocol is the stable interface.

**Fallback if the native-binding ecosystem stalls:** skip DAVE entirely — run a real Discord client in the channel and capture its decrypted output through a loopback device. Heavier to operate and loses per-speaker separation; kept only as an escape hatch.

---

## Storage — SQLite migration (future consideration)

Today files are the database: `speakers.json` + `.npy` embeddings, `campaigns.json`, `recordings.json`, `.md` transcripts with `.summary.md` / `_diar.json` / excerpt sidecars, and an in-memory job queue.

**Why it might be worth it later:**
- Transactional writes across related data (`campaigns.json` and `speakers.json` can drift on a mid-write crash).
- Persistent job history across restarts.
- Relational queries ("all transcripts for a speaker", "jobs by campaign").
- One source of truth instead of a growing set of sidecars.

**Why not now:**
- Needs a one-time migration for existing installs.
- `.npy` embeddings stay on disk regardless.
- Loses "just open the file" inspectability.
- Schema migrations become ongoing maintenance.
- A jobs-only SQLite hybrid was rejected: two storage patterns is worse than one.

**Revisit when:** multi-user or multi-process writes are needed, job history across restarts becomes a user need, or another cross-cutting JSON file appears.

---

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal shipped first and set the pattern: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
