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
- **Windows launcher dependency refresh.** `start.bat` reinstalls dependencies when `pyproject.toml` is newer than `.venv\.wisper-deps` (untested on Windows): update an existing install with `git pull`, double-click `start.bat`, confirm the one-time "Dependencies changed" reinstall runs, and that a second launch skips it.
- **Docker image with forced alignment.** The "Docker Build" workflow is disabled on GitHub, so nothing builds the image automatically. Confirm the CPU and GPU images build with `transformers`, and that a diarized job on the GPU image downloads the alignment model into `./cache/` and logs "Aligned words".
- **macOS loopback (PR #59).** Record page on a Mac with BlackHole installed: BlackHole appears under System Audio and captures audio.

---

## Forced word alignment — follow-ups

Shipped in #64 (design in `architecture.md`, "Forced word alignment"), default `forced_alignment = auto`. It shipped on spot-check evidence rather than the full labelled gate: on a 2 h episode, aligned and unaligned runs disagreed on 155 of 20,675 words; one-word "islands" inside another speaker's run were 10 (unaligned) vs 3 (aligned); 4 of 4 hand-checked disputed words were right with alignment. The labelling sheets were deleted after merge.

- **If attribution at speaker changes regresses, or before changing the aligner, smoothing thresholds, or the default,** run `scripts/alignment_eval.py` (see `docs/scenarios.md`) on a live-table excerpt and label its sheet. Arms already exist for the open questions: `aligned-guard-1s` (on edited podcast audio, words moved >1 s favoured Whisper 8 vs 1, the opposite of the Hanataz spike's 28 vs 1), `aligned-smooth-1w` / `aligned-nosmooth`, and `aligned-exclusive`.
- **Fallback engine:** `torchaudio` MMS_FA + star token (no new dependency, but CC-BY-NC weights and weaker on crosstalk). Only if the `transformers` dependency becomes a problem; recipe in git history (`6286e21`).
- **Watch:** `Qwen3ASR*` is new in transformers 5.x and may be renamed; the calls are covered by tests.

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

## Storage — SQLite migration (plan, awaiting review)

Branch `feat/sqlite-storage`. Plan only. The schema below is a **DRAFT**. Decisions made so far are under "Decisions" at the end; questions 4, 9, and 10 are still open.

### Why, and what SQLite does and doesn't fix

Transcripts, campaigns, recordings, and journals are linked by transcript stem across separate files, so every delete path has to remember every link. #64 fixed three that didn't. Other links nothing cleans up today:
- `Recording.transcript_path` (absolute path) and status `transcribed` survive a delete from `/transcripts`.
- `journaled_sessions` in `campaigns/<slug>/journal.md` frontmatter keeps deleted stems.
- `recordings.json` `discord_speakers` values aren't rekeyed by profile rename.
- `pipeline.py` associates a campaign with a stem written to *any* `-o` directory, so the web UI, which only sees `get_output_dir()`, shows it as missing.

Other gains:
- **Cross-process safety.** `_campaigns_lock`, `_profiles_lock`, and the per-recording mutex are `threading.Lock`s, so a CLI process and the server (or `--workers N`) can already lose each other's updates. `campaigns.json` and `speakers.json` are also written in place, not atomically. SQLite transactions fix both.
- **Job history across restarts,** which also keeps the evidence the open "missing transcript file" bug lost on restart.
- **Relational queries** without loading and scanning every JSON file.

**Foreign keys alone don't close the bug class:**
1. SQLite ships with `PRAGMA foreign_keys` **off**, and it is set per connection. Every connection must go through one `db.connect()` that turns it on, and a test must enforce that.
2. Files are deleted out of band: Finder, Obsidian, a different launch CWD, an unmounted Docker volume. A foreign key can't see that, so a **reconcile pass** is still needed (see "Transcript identity").
3. A write that touches both a row and a file can't be atomic. One ordering rule applies everywhere: **the `.md` is the existence marker, the row follows it, and companion files follow the row.** Deleting a transcript goes in four steps:
   1. Read the companion paths: the audio copy's path comes from the row or sidecar, because a collision suffix (`_1`, `_2`) means it can't be derived from the stem.
   2. Unlink the `.md`.
   3. Delete the row in one transaction. The cascade removes campaign, journal, and speaker rows and nulls recording and job links.
   4. Unlink the companions, best-effort.

   A crash between steps 3 and 4 leaves companions with no `.md` and no row. That is the only case reconcile sweeps (see "Transcript identity").

Costs:
- A one-time import on every existing install.
- Ongoing schema migrations.
- Losing "just open the JSON" inspectability (partly restored by `wisper db dump`).
- Hybrid storage between phases, contained on this branch (see "Phases").

### What moves and what stays

| Data | Decision | Why |
|------|----------|-----|
| `speakers.json` | **DB** (`profiles`) | Relational: rosters, Discord bindings, and recordings reference it. |
| `.npy` embeddings (256 float32, 1 KB) | **DB BLOB** (decided) | Keeps vector, `embedding_space` tag, and EMA update atomic with the row. Rename stops moving files. |
| `<key>.mp3` reference clips | File | Media, served by a route. Stays key-named, and rename moves it as today (after the DB commit, per the ordering rule), so the migration renames no files. |
| `campaigns.json` | **DB** (`campaigns`, `campaign_members`, `campaign_transcripts`) | The core relational data. |
| `journal.md` body | File | Obsidian-ready product the user reads. |
| `journaled_sessions` | **DB** (`journal_entries`) | A stem list, so it becomes a foreign key. The frontmatter copy is open question 10. |
| `<stem>.md` transcripts | File, plus a DB **registry row** | The file is the product, edited in Obsidian and synced. The row is the identity that links point at. Listing still reads frontmatter from disk, so there is no cached title to go stale. |
| `.summary.md`, excerpt `.mp3`/`.txt`, source audio copy | File | Product or media. Existence is checked on disk. |
| `_diar.json` | Split recommended (open question 4) | `speaker_map`, `speaker_map_source`, and per-label embeddings are relational and go to `transcript_speakers`. `diarization_segments` (hundreds of KB, never queried) goes to a JSON column or stays in a slimmed sidecar. |
| `recordings.json` + `metadata.json` | **DB** (`recordings`, `recording_segments`, `recording_markers`) | Kills the `save_recording_merged()` re-read dance, since appends become INSERTs. |
| WAV segments, `combined.wav`, `live_transcript.md` | File | Media. |
| Job queue | In-memory runtime, plus a DB **history projection** | Events, closures, and cancel flags can't persist. Same DB file, not a jobs-only store. |
| `config.toml`, `server.json` | File | Hand-edited, holds secrets, or is a runtime pointer. Out of scope. |

### Schema (DRAFT, for discussion)

One file, `<data dir>/wisper.db`. Surrogate integer ids for everything user-renamable, so a rename is one `UPDATE`. Every stored path is **relative, POSIX-separated, and resolved against a named root**, never absolute, so the same DB works on host and Docker and on Windows.

```
meta(key PK, value)                     -- output_root_hint, imported_at, …
profiles(id PK, key UNIQUE, display_name, role, notes, enrolled_date,
         enrollment_source, embedding BLOB NULL, embedding_space)
campaigns(id PK, slug UNIQUE, display_name, created)
campaign_members(campaign_id FK→campaigns CASCADE, profile_id FK→profiles CASCADE,
                 role, character, discord_user_id, PK(campaign_id, profile_id))
transcripts(id PK, stem UNIQUE, created_at, missing_since NULL)   -- stem is relative to the output root
campaign_transcripts(campaign_id FK CASCADE, transcript_id FK CASCADE UNIQUE, position)
                                        -- UNIQUE(transcript_id) = one campaign per transcript
journal_entries(campaign_id FK CASCADE, transcript_id FK CASCADE, position, folded_at)
transcript_speakers(transcript_id FK CASCADE, label, display_name, source,
                    embedding BLOB NULL, embedding_space, PK(transcript_id, label))
transcript_diarization(transcript_id PK FK CASCADE, segments_json, audio_rel_path NULL)
recordings(id TEXT PK uuid, campaign_id FK SET NULL, transcript_id FK SET NULL, status,
           source, name, started_at, ended_at, voice_channel_id, guild_id, devices_json,
           combined_rel_path, notes)
recording_speakers(recording_id FK CASCADE, discord_user_id, profile_id FK SET NULL NULL)
                                        -- NULL profile = unbound speaker
recording_segments(recording_id FK CASCADE, idx, stream, started_at, duration_s, rel_path, finalized)
recording_markers(recording_id FK CASCADE, timestamp, elapsed_s)
recording_rejoins(recording_id FK CASCADE, timestamp, close_code, attempt_number)
jobs(id TEXT PK uuid, type, status, created_at, started_at, finished_at, error_code,
     transcript_id FK SET NULL, campaign_id FK SET NULL, recording_id FK SET NULL,
     params_json, log_tail)
```

Alternatives held open: keep embeddings as `.npy` (drop the BLOB columns); keep `diarization_segments` in the sidecar (drop `transcript_diarization`); key `recording_speakers` by profile key text (no FK). When a transcript is deleted, `recordings.transcript_id` becomes NULL, and app code reverts the status `transcribed` → `completed` in the same transaction, so the Transcribe button comes back (open question 8).

### Transcript identity (settle before Phase 2)

Today "a transcript" is `get_output_dir()/<stem>.md`. That resolves to `./output` relative to the **CWD** when that directory exists, and to `data_dir/output` otherwise. In Docker it is `/app/output`, a different bind mount from `/data`. So the same DB can see different transcript roots depending on where it was launched.

Proposed rules:
1. The registry holds only transcripts under the resolved output root, keyed by stem. That is the web UI's scope today, and stems are unique there.
2. `meta.output_root_hint` records the resolved root. When a later launch resolves a different root, the app logs a warning and shows a banner. It **never** marks rows missing because of the change, so launching from another directory can't wipe campaign associations.
3. **Reconcile** runs at startup and on list pages, which already glob:
   - An unregistered `.md` gets a new row.
   - A row whose `.md` is gone gets `missing_since` set but keeps its campaign position (the campaign page already renders missing entries) **and all of its companions**, because the file may come back (a sync or a Finder rename). A reappearing file clears the flag.
   - A companion is an orphan only when it has **no `.md` and no row**. The sweep deletes only pattern-identifiable orphans (`.summary.md`, `_diar.json`, `_excerpt_*`) and never deletes a generic audio file.
   - Rows are never deleted automatically (open question 5).
   - **While the resolved root differs from `meta.output_root_hint`, reconcile does nothing:** no inserts, no missing flags, no sweep, until the user confirms the new root. Otherwise every old-root row would be flagged missing, and with `stem UNIQUE` a same-named `.md` in the new root would silently take over an old row's campaign association.
4. `wisper transcribe -o elsewhere --campaign X`: register and associate only when the output lands under the output root. Otherwise warn that the web UI won't see it (decided).

### Migrating existing installs

- **Mechanism.** `db.py` holds an ordered list of Python migration functions, versioned by `PRAGMA user_version`. The stdlib `sqlite3` module is enough: no ORM, no Alembic, no new dependency. Connections use Python 3.12+ `autocommit=True` with an explicit `BEGIN IMMEDIATE`, avoiding the legacy implicit-transaction mode.
- **Each phase is one migration:** create its tables, then import its legacy files if present, in **the same `BEGIN IMMEDIATE` transaction that bumps `user_version`**. The result is all or nothing.
- **Idempotency.** Re-running is a no-op because the version already moved. If the server and a CLI command start at the same moment, the second blocks on the write lock, then sees the new version and skips.
- **Downgrade guard.** A DB whose `user_version` is newer than the code knows makes the app refuse to start, with a clear message. An old build must not write to a newer schema.
- **Backup.** Before a migration that imports legacy data, copy those legacy files into `<data dir>/backups/pre-sqlite-v<N>-<timestamp>/`. Before any later schema migration, snapshot `wisper.db` with the sqlite3 backup API. Audio is never copied.
- **Dirty data** (existing installs contain exactly what foreign keys forbid), handled without aborting:
  - A campaign stem with no `.md` is imported as a registry row with `missing_since` set, so order is kept.
  - A stem listed in two campaigns stays in the first; the rest are reported.
  - A member whose profile key isn't in `speakers.json` is dropped and reported.
  - `journaled_sessions` entries with no transcript are dropped and reported.
  - A sidecar or metadata file that fails to parse is skipped and reported.
- **Unparseable top-level JSON** (`speakers.json`, `campaigns.json`) rolls the transaction back, leaves `user_version` unchanged, and stops startup with a message naming the file and the backup. Importing an empty store instead would silently lose data.
- **Import report** goes to the log and to `import-report.txt` in the backup dir.
- **Legacy files after import are deleted** once the import transaction has committed (decided). This covers `speakers.json`, `.npy` files, `campaigns.json`, `recordings.json`, `metadata.json`, and `_diar.json` (or its non-segment keys, per question 4). A crash between commit and delete is harmless: the version has already moved, so nothing re-imports, and the next startup deletes files a committed migration already imported.
- **No downgrade support** (decided): no `export-legacy`, no rollback path. The downgrade guard stays because it's a few lines and stops an old build from writing to a newer schema.

### Concurrency

- **Connection per unit of work.** `with db.transaction(data_dir) as conn:` opens, sets pragmas (`foreign_keys=ON`, `busy_timeout=5000`), runs, commits, closes. There is no module-level cached connection, so `WISPER_DATA_DIR` and the per-test data dir keep working, and threads never share a connection (event loop, `to_thread` worker, capture threads, tick thread).
- **Every load-modify-save uses `BEGIN IMMEDIATE`.** A deferred read-then-write gets `SQLITE_BUSY` on lock upgrade without waiting on `busy_timeout`. The `threading.Lock`s go away; the DB lock also covers other processes.
- **Never hold a write transaction across ML or LLM work.** This is today's "not around embedding extraction" rule, generalized. For example, `relabel_campaign()` computes first, then writes renames in one short transaction.
- **Capture hot path.** `record_completed_wav_segment()` and marker appends are single-row autocommit INSERTs, once per 60 s rotation. They keep the "never raises" contract: if busy past a short timeout, log and continue, and startup reconcile rebuilds the manifest from the `NNNN.wav` files on disk.
- **The one-job-at-a-time invariant is unchanged.** It protects the model globals, not storage, and the DB neither needs nor relaxes it. The worker writes job transitions (pending → running → terminal) as short transactions.
- **Async routes** call sync `sqlite3` inline. That costs about the same as today's inline JSON reads (sub-millisecond at this scale). Revisit only if a query gets slow.
- **Journal mode.** Rollback journal (`DELETE`) everywhere (decided). Write volume is tiny, it works on any filesystem, and WAL needs shared memory that can misbehave on network or virtualized filesystems. WAL is the upgrade if read/write contention is ever observed (open question 7).
- **Docker.** The DB lives in `/data`, next to `config.toml`. Transcripts stay on the `/app/output` mount and recordings on the nested `/data/recordings` mount, which is why stored paths are relative to a named root. Needs verification, not assumption: a `docker compose run wisper …` CLI container and `wisper-web` sharing `./data`, on Linux and on Docker Desktop for Mac. A host process and a container writing the same bind-mounted DB at once is unsupported; the docs will say so.
- **Windows.** An open DB holds a file lock, so docs note "stop the server before moving or restoring the data dir". Stored relative paths use POSIX separators and are converted with `pathlib` on read. `%APPDATA%` is local, not a synced folder, but a data dir redirected into OneDrive via `WISPER_DATA_DIR` gets a docs warning.

### Phases (each merges separately and ships its migration, tests, and docs)

Manager **public APIs stay stable** through Phases 1–4 (`load_profiles()` → `dict[str, SpeakerProfile]`, `load_campaigns()`, `get_transcripts_for_campaign()`, `load_recordings()`, …). Routes, the CLI, and the CLAUDE.md mock targets therefore barely change in the early phases, and most of the ~200 test references keep working. Internals switch from JSON to SQL. A later cleanup phase can narrow the APIs (e.g. drop whole-store `save_*`).

**Phase 0 — Foundation (no data moves).**
- New `db.py`: `connect()`, `transaction()`, migrations runner, downgrade guard, backup helper.
- `wisper db status | backup | dump`.
- Migrations run on first `connect()`; startup in `web/app.py` calls it early so a failure is reported before serving.
- Tests: `test_db.py`, plus a guard test that no module outside `db.py` calls `sqlite3.connect`, in the spirit of `test_tailwind.py`.
- Docs: architecture.md (module map; "Database" replaces "File-store locking"), `docs/cli-reference.md`, `docs/configuration.md` (data layout, backup).

**Phase 1 — Profiles + campaigns** (coupled through `rename_profile()` → `rekey_member()`).
- Import `speakers.json` and `campaigns.json`. `campaign_transcripts` temporarily holds stem text plus position.
- Embeddings to BLOB: import `.npy`, and `load_profile_embedding()`/`update_embedding()` read and write the column.
- Rename becomes one transaction across profile and memberships.
- Changes: `speaker_manager.py`, `campaign_manager.py`. `web/routes/speakers.py` and `campaigns.py` change only if an API narrows.
- Tests: rewrite `test_speaker_manager.py`/`test_campaign_manager.py` internals, plus importer tests (including the dirty-data cases above).

**Phase 2 — Transcript registry and links (fixes the #64 class).**
- New `transcript_store.py`: `register()`, `reconcile()`, and `delete_transcript()` as the only delete path, following the ordering rule. Everything that unlinks a `.md` calls it.
- Migration rebuilds `campaign_transcripts` onto `transcript_id`, imports `journaled_sessions` into `journal_entries`, and links recordings' `transcript_path` if Phase 4 has landed (otherwise Phase 4 does it).
- Changes:
  - `web/routes/transcripts.py`: single delete, bulk delete, list, and campaign assign; `_delete_transcript_companions()` moves into the store.
  - `web/routes/record.py`: `_purge_recording_files()`.
  - `web/routes/campaigns.py`: the missing-entry rendering reads `missing_since`.
  - `pipeline.py`: CLI registration and the `--campaign` rule.
  - `web/jobs.py`: register on completion.
  - `journal.py`: `unjournalled_sessions()`, `update_journal()`, `rebuild_campaign()`.
  - `cli.py`: `transcripts list/move`, `campaigns journal/reorder`.
- Tests: cascade tests for every delete path; reconcile (external delete keeps order; reappearing file clears the flag; an output-root change doesn't mark rows missing); a guard test that no module outside `transcript_store.py` unlinks `*.md` in the output dir.

**Phase 3 — Diarization sidecar data** (shape pending question 4).
- Import `_diar.json` into `transcript_speakers`/`transcript_diarization`; the sidecar is slimmed or removed.
- Changes: `jobs._write_enrollment_sidecar()`, `web/enroll_shared.py` (`resolve_current_names()`, `apply_renames()`), `speaker_registry.py` (`_load_sidecar`/`_write_sidecar`, `embeddings_to/from_sidecar`), and `web/routes/transcripts.py` and `transcribe.py` (enroll wizard).
- `apply_renames()` rewrites the `.md` and then updates `speaker_map` rows. Under the ordering rule the file comes first. A crash in between leaves the rows stale, so the existing interval-matching fallback stays as the repair path.
- CLAUDE.md's "`_diar.json` carries the authoritative `speaker_map`" gotcha is rewritten to name the table.
- Tests: the largest fixture churn (36 `_diar.json` references across 6 test files), moved to a helper that seeds the DB.

**Phase 4 — Recordings.**
- Import `recordings.json`, each `metadata.json`, and `discord_speakers` → `recording_speakers.profile_id`, which fixes rename-not-rekeying for free.
- Delete `save_recording_merged()` and the per-recording mutex; `reconcile_on_startup()` becomes one `UPDATE`.
- Changes: `recording_manager.py`, `web/discord_bot.py`, `web/local_capture.py` (hot-path contract above), `web/routes/record.py`, and the `wisper record` CLI.
- Tests: `test_recording_manager.py`, record routes, and a hot-path test with the DB held busy.

**Phase 5 — Job history.**
- Write-through from `JobQueue` at submit and at each status transition. On terminal status, store `error_code` (the same generic codes, never exception text) and the last ~200 log lines.
- `params_json` holds an allowlisted subset of kwargs: no secrets, no temp paths.
- At startup, pending and running rows become `failed` / "Interrupted by restart". They are never auto-resumed: the uploads are gone and the jobs are multi-hour GPU work.
- The in-memory 50-job cap stays. The DB keeps every job (decided); list views page through it (question 9).
- UI: a job-history view on the dashboard or jobs page, and "jobs for this transcript/campaign" links.
- Changes: `web/jobs.py`, `web/routes/dashboard.py` and `transcribe.py`, `docs/web-ui.md`.

**Phase 6 — Cleanup.**
- Remove the remaining JSON code paths (the importers stay, frozen, for old installs).
- Narrow APIs where the stability shims are no longer needed.
- Final pass over architecture.md, `docs/`, and CLAUDE.md.

### Test strategy

- The autouse `_isolated_data_dir` fixture already gives each test its own `WISPER_DATA_DIR`, so each test gets its own `wisper.db` for free, provided `db.py` caches nothing across calls. The cost is migrations on first connect in each test (a few ms). If the suite slows noticeably, add a session-scoped template DB that is copied per test.
- **`test_db.py`:**
  - Fresh install migrates to the latest version.
  - A re-run is a no-op.
  - The downgrade guard refuses a newer DB.
  - `foreign_keys` is on for every connection `db.connect()` returns.
  - `BEGIN IMMEDIATE` under two threads: the second waits instead of erroring.
  - One cross-process import race under spawn (the macOS default): exactly one import happens.
- **Importer tests** use a synthetic legacy data dir built by a frozen copy of today's serializers (`tests/_legacy_store.py`), so the tests don't change when the managers do. Cases: clean import, each dirty-data case, a malformed top-level file (rolls back, version unchanged, backup present), and a re-run.
- **Invariant guards:** no raw `sqlite3.connect` outside `db.py`, and no `.md` unlink outside `transcript_store.py`.
- **Cross-platform paths:** relative-path round-trips through `PureWindowsPath`/`PurePosixPath`, so the Windows logic is tested on macOS and Linux CI.
- No CI change: `sqlite3` is stdlib, and the bundled SQLite on 3.13/3.14 (macOS, Windows, and Debian slim) supports `RETURNING`, `DROP COLUMN`, and `STRICT`.
- Existing test rules still hold: no GPU, network, or real audio, and synthetic data only.

**Docs touched across the phases:**
- architecture.md: Module Map; Data Storage tree and "Output directory"; "File-store locking" → "Database"; Job Queue ("Nothing persists across restarts" changes in Phase 5); Test Strategy; Known Constraints (host-plus-container DB writes, WAL).
- `docs/configuration.md`: data layout, backups, the `WISPER_DATA_DIR` + synced-folder warning.
- `docs/cli-reference.md`: `wisper db`, and the `transcribe --campaign` rule.
- `docs/docker.md`: DB location, backup, CLI and web containers sharing `./data`.
- `docs/scenarios.md`: restore from backup, moved output dir, externally deleted transcripts.
- `docs/web-ui.md`: missing transcripts, job history.
- CLAUDE.md: new gotchas (connect only through `db.py`; `BEGIN IMMEDIATE`; no transaction across ML work; delete transcripts only via `transcript_store`; `speaker_map` location).
- README: unchanged.

### Decisions (2026-09-30)

1. **Output root:** keep the current `./output` vs `data_dir/output` resolution, with the drift warning and reconcile freeze.
2. **CLI `--campaign` outside the output root:** warn and skip the association.
3. **Embeddings:** DB BLOBs. Size check: 1 KB per profile and 1 KB per label per transcript. 500 transcripts × 8 labels ≈ 4 MB, which is negligible for SQLite.
5. **Missing transcripts:** keep flagged rows until the user removes them.
6. **Legacy files:** delete them after a committed import. No downgrade support. Pending: whether to keep the `backups/pre-sqlite-*` copy.
7. **Journal mode:** rollback journal everywhere.
8. **Deleting a recording's transcript:** revert the recording to `completed`. This applies to every recording, both Discord and local capture.
11. **Merge cadence:** stack phase PRs on `feat/sqlite-storage`; one PR to `main` when the feature is complete.

### Open questions

4. **`_diar.json` shape:** (a) move everything, (b) move the relational parts and keep a segments-only sidecar, or (c) registry row only.
9. **Job history UI:** the dashboard keeps showing the 20 most recent jobs; a new paginated history page is proposed.
10. **Journal frontmatter:** stop writing `journaled_sessions`, or keep it as a read-only mirror?

---

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal shipped first and set the pattern: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
