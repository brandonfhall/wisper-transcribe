# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Missing transcript file after a successful Transcribe job

A local-recording `JOB_TRANSCRIPTION` job reported COMPLETED and logged "Wrote `<id>.md`", and the recording's metadata points at that path, but the file never existed on disk. Ruled out: a CWD-relative write, a `WISPER_DATA_DIR` mismatch, and every delete path in `jobs.py` (the upload-cleanup helpers are gated on `job.is_web_upload`, which recordings never set). The job queue has no persistence, so the evidence was lost on restart.

**Next step:** run the server with `WISPER_DEBUG=1` during local-recording transcribes so a recurrence is captured. See `LIVE_AUDIO_TEST_PLAN.md` §4a. The SQLite migration adds detection: Phase 2 fails the job if the `.md` is missing at registration and records the output root; Phase 5 keeps the job's log tail across restarts.

### Docker Desktop + native CLI on one data dir corrupts the DB (SQLite migration)

Once storage moves to SQLite: with the web server in Docker Desktop (Mac or Windows) and `./data` bind-mounted, a native `wisper` command pointed at the same `./data` (via `WISPER_DATA_DIR`) and writing at the same time can corrupt `wisper.db`. File locks don't cross the Docker Desktop VM boundary. Reproduced 2026-09-30: one host writer plus one container writer gave `database disk image is malformed` and lost updates. Container + container is fine (15,000/15,000), and native Linux Docker is unaffected.

**Status:** guarded, not fixed. Phase 0's runtime lease makes the second runtime refuse to start when the container is inside Docker Desktop's VM (advisory: two processes starting in the same second could both pass). Documented in `docs/docker.md` ("use Docker, or the native CLI with a local web server, not both at once").

### Ollama: empty responses from reasoning models

`ollama.py`'s `_post_chat` reads only `message.content` from the stream. A reasoning model can spend its whole budget on the `thinking` field and return no content, and a streamed `"error"` field is never checked. Both cases surface as `Ollama JSON response did not parse: ... Raw: ''`, which blames the parse layer. Seen intermittently when summarizing a ~150k-char transcript with `ollama-cloud`; a plain retry succeeded. A non-thinking local model instead ignored the `format` schema and returned prose.

**Decision needed:** add retry-on-empty-content and/or surface a streamed error distinctly. Either change touches the shared client code used by every provider.

---

## Manual verification owed

- **Live recording + campaign journal:** `LIVE_AUDIO_TEST_PLAN.md` — real-device capture, live transcript, journal browser flows, bulk delete, busy-queue notice.
- **Live Discord acceptance test.** The recording pipeline (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is covered by synthesized-PCM tests, but the JDA → socket → Python path needs one real session: record a few minutes with 2+ speakers, play the per-user WAVs, and run Transcribe.
- **Windows launcher dependency refresh.** `start.bat` reinstalls dependencies when `pyproject.toml` is newer than `.venv\.wisper-deps` (untested on Windows): update an existing install with `git pull`, double-click `start.bat`, confirm the one-time "Dependencies changed" reinstall runs, and that a second launch skips it.
- **Docker image with forced alignment.** Do this in the same session as the SQLite migration's Docker check (CLI and web containers sharing `./data`). The "Docker Build" workflow is disabled on GitHub, so nothing builds the image automatically. Confirm the CPU and GPU images build with `transformers`, and that a diarized job on the GPU image downloads the alignment model into `./cache/` and logs "Aligned words".
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

- **Profile cleanup (user action).** Re-enroll every profile after the embedding-model change; delete the `speaker_*` / `SPEAKER_NN` junk and duplicate profiles; enroll Mike and Ben from sessions where they're clearly separated. `wisper speakers doctor` (flags identical or near-identical profiles) is scoped into the SQLite migration's Phase 1.
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

Branch `feat/sqlite-storage`. Plan only. The schema below is a **DRAFT** until signed off. Decisions are listed under "Decisions" at the end.

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
   3. Delete the row in one transaction. The cascade removes campaign, journal, and speaker rows and nulls recording and job links. Until Phase 4, recordings are still JSON, so step 3 also calls `recording_manager.clear_transcript_link(stem)` (clears `transcript_path`, reverts `transcribed` → `completed`). Jobs are in memory until Phase 5 and need nothing.
   4. Unlink the companions, best-effort. The source-audio copy is always under the output root (web uploads are moved there; recordings copy `combined.wav` to `<output>/<recording-id>.wav`, `record.py:930`), so the recording's own `combined.wav` is never touched.

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
| `.npy` embeddings (256 float32, 1 KB) | **DB BLOB** (decided) | Keeps vector, `embedding_space` tag, and EMA update atomic with the row. Rename stops moving files. `SpeakerProfile.embedding_path` becomes `embedding: Optional[np.ndarray]`, loaded with the profile (see Phase 1). |
| `<key>.mp3` reference clips | File | Media, served by a route. Stays key-named, and rename moves it as today (after the DB commit, per the ordering rule), so the migration renames no files. |
| `campaigns.json` | **DB** (`campaigns`, `campaign_members`, `campaign_transcripts`) | The core relational data. |
| `journal.md` body | File | Obsidian-ready product the user reads. |
| `journaled_sessions` | **DB** (`journal_entries`) | A stem list, so it becomes a foreign key. No longer written to `journal.md` frontmatter (decided); a journal export adds it back (see Phase 2). Body and entries now live in two stores, so folds follow the journal write rule in Phase 2. |
| `<stem>.md` transcripts | File, plus a DB **registry row** (full text is **not** stored) | The file is the product, edited in Obsidian and synced. The row is the identity that links point at. Listing still reads frontmatter from disk, so there is no cached title to go stale. Full-text search (Phase 6) is a **derived** FTS5 index built from the files, so the DB never becomes the source of truth for the text. |
| `.summary.md`, excerpt `.mp3`/`.txt`, source audio copy | File | Product or media. Existence is checked on disk. |
| `_diar.json` | **Split** (decided) | `speaker_map`, `speaker_map_source`, per-label embeddings, and the source-audio path go to the DB. The `campaign` key is dropped (the enroll job reads `campaign_transcripts` instead). `diarization_segments` (hundreds of KB, only ever read whole) stays in a slimmed `_diar.json`, treated like the other companion files. |
| `recordings.json` + `metadata.json` | **DB** (`recordings`, `recording_segments`, `recording_markers`) | Kills the `save_recording_merged()` re-read dance, since appends become INSERTs. |
| WAV segments, `combined.wav`, `live_transcript.md` | File | Media. |
| Job queue | In-memory runtime, plus a DB **history projection** | Events, closures, and cancel flags can't persist. Same DB file, not a jobs-only store. |
| `config.toml`, `server.json` | File | Hand-edited, holds secrets, or is a runtime pointer. Out of scope. |

### Schema (DRAFT, for sign-off)

One file, `<data dir>/wisper.db`. The DDL below has been run against SQLite 3.53 with `foreign_keys=ON`; every constraint listed in "Schema principles" was checked by an insert that should fail and did.

**Schema principles** (decided: build it properly, not as a JSON mirror):
- **Normalized to 3NF/BCNF.** Every non-key column depends on the whole key and nothing else. Anything derivable is derived, not stored:
  - A recording's `transcribed` state is `transcript_id IS NOT NULL`; `transcribing` is "an active job exists for this recording" (the in-memory `JobQueue` before Phase 5, the `jobs` table after). Only the capture lifecycle is stored (`capture_status`). Decision 8's revert happens by `ON DELETE SET NULL` with no app code, and the stuck-`transcribing` bug can't happen. `Recording.status` stays a computed property so templates don't change.
  - Segment paths (`recordings/<id>/combined/<idx:04d>.wav`), `combined.wav`, and `per-user/` are fixed layout, so no path columns. The manifest's `stream` is always `"mixed"` (`recording_manager.py:377`), so no stream column.
  - Marker `elapsed_s` is `marked_at − started_at` (`started_at` never changes). This reverses the store-once choice commented at `append_marker()`.
  - `Recording.unbound_speakers` is `recording_speakers` rows with NULL `profile_id`.
- **No multi-valued columns (1NF).** `devices_json` becomes `recording_devices`. One documented exception: `jobs.params_json`, an audit snapshot of kwargs that is never queried by field, constrained to a JSON object.
- **Subtypes enforced by the schema.** Discord-only data (`guild_id`, `voice_channel_id`, speakers, rejoins) hangs off `recording_discord`, and local devices off `recording_devices`. Both use a composite FK to `recordings(id, source)`, so a local recording can't have Discord rows and vice versa.
- **Integrity in the DB, not in app code:** `STRICT` tables, explicit `NOT NULL`, `CHECK` on every enum and on paired-nullable columns (embedding + space, indexed mtime + size), `UNIQUE` for every one-to-one rule the code enforces today (one Discord account per member per campaign — `bind_discord_id`'s loop; one campaign per transcript; one recording per transcript; campaign order positions).
- **Journal entries must belong to the campaign that holds the transcript:** composite FK to `campaign_transcripts(campaign_id, transcript_id)`. Transcripts always move freely: moving, unassigning, or deleting one drops its entry, and a trigger on `journal_entries` deletes (which also fires on cascades) marks the old campaign's journal **stale** (`journal_stale_since`). The journal text is never edited automatically; the user chooses when to rebuild.
- **Every FK child column is indexed** (SQLite doesn't do this; unindexed children make cascades full scans).
- **Surrogate integer ids** for everything user-renamable, so a rename is one `UPDATE`. UUIDs stay the key for recordings and jobs (they're already in URLs).
- **Deliberate non-derivations**, each for a stated reason:
  - `profiles.key`: an alternate key (URL slug and reference-clip filename), written only by `rename_profile()`.
  - `transcript_speakers.display_name`: the name as rendered in that `.md`, which a profile rename doesn't rewrite. It's a fact about the file, not a profile FK.
  - `recording_speakers.profile_id`: who that Discord user was when captured. Needed for recordings with no campaign, where there's no membership to derive it from.
  - `search_blocks` and `search_fts`: a derived, disposable index (Phase 6).
- **Types:** timestamps are ISO-8601 UTC `TEXT`; booleans are `INTEGER CHECK IN (0,1)`; stored paths are relative to the output root and POSIX-separated, with a `CHECK` rejecting absolute paths, backslashes, and `..` components.
- **Checks at runtime:** `PRAGMA foreign_key_check` runs before every migration commits (a non-empty result rolls back); `wisper db status` runs it plus `PRAGMA integrity_check`. Imports use `PRAGMA defer_foreign_keys=ON` so load order doesn't matter.
- **Enum CHECKs mirror Python constants** (`JOB_*`, statuses, `SOURCE_AUTO`/`SOURCE_MANUAL`). A test asserts they match, so adding a job type without a migration fails CI.

```sql
-- Conventions: every table STRICT; timestamps are TEXT, ISO-8601 UTC ('…Z');
-- booleans are INTEGER CHECK IN (0,1); every FK child column is indexed.

CREATE TABLE migrations (                      -- append-only log; PRAGMA user_version is authoritative
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL,
  backup_dir  TEXT                             -- relative to the data dir; NULL = nothing backed up
) STRICT;

CREATE TABLE runtime_leases (                  -- host + Docker Desktop container guard (see Concurrency › Docker)
  runtime      TEXT PRIMARY KEY CHECK (runtime IN ('host', 'container')),
  holder       TEXT NOT NULL,                  -- hostname:pid, for the error message
  crosses_vm   INTEGER NOT NULL CHECK (crosses_vm IN (0, 1)),  -- container inside Docker Desktop's VM
  heartbeat_at TEXT NOT NULL,
  CHECK (runtime = 'container' OR crosses_vm = 0)
) STRICT;

-- Phase 1 ------------------------------------------------------------------
CREATE TABLE profiles (
  id                INTEGER PRIMARY KEY,
  key               TEXT NOT NULL UNIQUE,      -- alternate key: URL slug + clip filename; set only by rename_profile()
  display_name      TEXT NOT NULL CHECK (display_name <> ''),
  role              TEXT NOT NULL DEFAULT '',
  notes             TEXT NOT NULL DEFAULT '',
  enrolled_date     TEXT NOT NULL,
  enrollment_source TEXT NOT NULL,
  embedding         BLOB,
  embedding_space   TEXT,
  CHECK ((embedding IS NULL) = (embedding_space IS NULL)),
  CHECK (embedding IS NULL OR length(embedding) % 4 = 0)   -- float32 vector
) STRICT;

CREATE TABLE campaigns (
  id             INTEGER PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE,
  display_name   TEXT NOT NULL CHECK (display_name <> ''),
  created_at     TEXT NOT NULL,
  journal_sha256 TEXT CHECK (journal_sha256 IS NULL OR length(journal_sha256) = 64),
  journal_stale_since TEXT                   -- journal text mentions a session that left or changed; cleared by rebuild
) STRICT;

CREATE TABLE campaign_members (
  campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  profile_id      INTEGER NOT NULL REFERENCES profiles(id)  ON DELETE CASCADE,
  role            TEXT NOT NULL DEFAULT '',
  character       TEXT NOT NULL DEFAULT '',
  discord_user_id TEXT CHECK (discord_user_id IS NULL OR discord_user_id GLOB '[0-9]*'),
  PRIMARY KEY (campaign_id, profile_id),
  UNIQUE (campaign_id, discord_user_id)       -- one member per Discord account per campaign (NULLs allowed)
) STRICT;
CREATE INDEX campaign_members_profile ON campaign_members(profile_id);

-- Phase 2 ------------------------------------------------------------------
CREATE TABLE transcripts (
  id             INTEGER PRIMARY KEY,
  stem           TEXT NOT NULL UNIQUE CHECK (stem <> '' AND stem NOT GLOB '*[/\]*'),  -- NFC, relative to the output root
  created_at     TEXT NOT NULL,
  missing_since  TEXT,
  audio_rel_path TEXT CHECK (audio_rel_path IS NULL OR (audio_rel_path NOT GLOB '/*'
                                                  AND audio_rel_path NOT GLOB '*\*'
                                                  AND '/' || audio_rel_path || '/' NOT GLOB '*/../*')),
  indexed_mtime_ns INTEGER,                    -- Phase 6
  indexed_size     INTEGER,                    -- Phase 6
  CHECK ((indexed_mtime_ns IS NULL) = (indexed_size IS NULL))
) STRICT;

CREATE TABLE campaign_transcripts (           -- 1:N kept as its own relation so "no campaign" needs no NULLs
  transcript_id INTEGER PRIMARY KEY REFERENCES transcripts(id) ON DELETE CASCADE,   -- one campaign per transcript
  campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL CHECK (position >= 0),
  UNIQUE (campaign_id, position),
  UNIQUE (campaign_id, transcript_id)         -- target of journal_entries' composite FK
) STRICT;

CREATE TABLE journal_entries (
  transcript_id INTEGER PRIMARY KEY,
  campaign_id   INTEGER NOT NULL,
  folded_at     TEXT NOT NULL,
  FOREIGN KEY (campaign_id, transcript_id)
    REFERENCES campaign_transcripts(campaign_id, transcript_id) ON DELETE CASCADE
    -- ON UPDATE NO ACTION: moving a journaled transcript fails unless its entry is deleted first
) STRICT;
CREATE INDEX journal_entries_campaign ON journal_entries(campaign_id, transcript_id);
CREATE TRIGGER journal_entries_ad AFTER DELETE ON journal_entries BEGIN   -- also fires on FK cascades
  UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
   WHERE id = old.campaign_id;
END;

-- Phase 3 ------------------------------------------------------------------
CREATE TABLE transcript_speakers (
  transcript_id   INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  label           TEXT NOT NULL,              -- raw pyannote label
  display_name    TEXT NOT NULL,              -- the name as rendered in the .md (not a profile FK, by design)
  source          TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
  embedding       BLOB,
  embedding_space TEXT,
  PRIMARY KEY (transcript_id, label),
  CHECK ((embedding IS NULL) = (embedding_space IS NULL))
) STRICT;

-- Phase 4 ------------------------------------------------------------------
CREATE TABLE recordings (
  id             TEXT PRIMARY KEY CHECK (length(id) = 36),   -- uuid4
  source         TEXT NOT NULL CHECK (source IN ('discord', 'local')),
  name           TEXT,
  notes          TEXT,
  campaign_id    INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
  transcript_id  INTEGER UNIQUE REFERENCES transcripts(id) ON DELETE SET NULL,
  capture_status TEXT NOT NULL CHECK (capture_status IN ('recording', 'degraded', 'completed', 'failed')),
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  recovered_at   TEXT,
  UNIQUE (id, source),                        -- target of the subtype FKs below
  CHECK (capture_status NOT IN ('recording', 'degraded') OR ended_at IS NULL),
  CHECK (recovered_at IS NULL OR capture_status = 'completed')
) STRICT;
CREATE INDEX recordings_campaign ON recordings(campaign_id);

CREATE TABLE recording_discord (              -- subtype: Discord-only attributes
  recording_id     TEXT PRIMARY KEY,
  source           TEXT NOT NULL DEFAULT 'discord' CHECK (source = 'discord'),
  guild_id         TEXT NOT NULL,
  voice_channel_id TEXT NOT NULL,
  FOREIGN KEY (recording_id, source) REFERENCES recordings(id, source) ON DELETE CASCADE
) STRICT;

CREATE TABLE recording_devices (              -- subtype: local-capture devices (display only)
  recording_id TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'local' CHECK (source = 'local'),
  role         TEXT NOT NULL CHECK (role IN ('mic', 'system')),
  device_name  TEXT NOT NULL,
  PRIMARY KEY (recording_id, role),
  FOREIGN KEY (recording_id, source) REFERENCES recordings(id, source) ON DELETE CASCADE
) STRICT;

CREATE TABLE recording_speakers (             -- Discord users heard; NULL profile = unbound
  recording_id    TEXT NOT NULL REFERENCES recording_discord(recording_id) ON DELETE CASCADE,
  discord_user_id TEXT NOT NULL CHECK (discord_user_id GLOB '[0-9]*'),
  profile_id      INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
  PRIMARY KEY (recording_id, discord_user_id)
) STRICT;
CREATE INDEX recording_speakers_profile ON recording_speakers(profile_id);

CREATE TABLE recording_segments (             -- path derived: recordings/<id>/combined/<idx:04d>.wav
  recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  idx          INTEGER NOT NULL CHECK (idx >= 0),
  started_at   TEXT NOT NULL,
  duration_s   REAL NOT NULL CHECK (duration_s >= 0),
  finalized    INTEGER NOT NULL CHECK (finalized IN (0, 1)),
  PRIMARY KEY (recording_id, idx)
) STRICT;

CREATE TABLE recording_markers (              -- elapsed = marked_at - recordings.started_at (derived)
  recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  marked_at    TEXT NOT NULL,
  PRIMARY KEY (recording_id, marked_at)
) STRICT;

CREATE TABLE recording_rejoins (
  recording_id   TEXT NOT NULL REFERENCES recording_discord(recording_id) ON DELETE CASCADE,
  attempted_at   TEXT NOT NULL,
  close_code     INTEGER NOT NULL,
  attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
  PRIMARY KEY (recording_id, attempted_at)
) STRICT;

-- Phase 5 ------------------------------------------------------------------
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY CHECK (length(id) = 36),
  type          TEXT NOT NULL CHECK (type IN ('transcription', 'refine', 'summarize', 'enroll',
                                              'live', 'campaign_journal', 'speaker_relabel')),
  status        TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  error_code    TEXT,
  transcript_id INTEGER REFERENCES transcripts(id) ON DELETE SET NULL,
  campaign_id   INTEGER REFERENCES campaigns(id)   ON DELETE SET NULL,
  recording_id  TEXT    REFERENCES recordings(id)  ON DELETE SET NULL,
  params_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json) AND json_type(params_json) = 'object'),
  log_tail      TEXT NOT NULL DEFAULT '',
  CHECK (status <> 'pending' OR started_at IS NULL),
  CHECK (status <> 'running' OR started_at IS NOT NULL),   -- a failed job may never have started (cancelled/interrupted while pending)
  CHECK ((status IN ('completed', 'failed')) = (finished_at IS NOT NULL)),
  CHECK ((status = 'failed') = (error_code IS NOT NULL))
) STRICT;
CREATE INDEX jobs_created    ON jobs(created_at);
CREATE INDEX jobs_transcript ON jobs(transcript_id);
CREATE INDEX jobs_campaign   ON jobs(campaign_id);
CREATE INDEX jobs_recording  ON jobs(recording_id);

-- Phase 6 (derived; rebuildable) ------------------------------------------
CREATE TABLE search_blocks (
  id            INTEGER PRIMARY KEY,           -- = search_fts.rowid
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN ('transcript', 'summary')),
  block_idx     INTEGER NOT NULL CHECK (block_idx >= 0),
  speaker       TEXT,                          -- filter + display; NULL for summary sections
  start_s       REAL CHECK (start_s IS NULL OR start_s >= 0),
  UNIQUE (transcript_id, kind, block_idx)
) STRICT;
CREATE VIRTUAL TABLE search_fts USING fts5(
  text, content='', contentless_delete=1,
  tokenize='porter unicode61 remove_diacritics 2', prefix='2 3');
CREATE TRIGGER search_blocks_ad AFTER DELETE ON search_blocks BEGIN
  DELETE FROM search_fts WHERE rowid = old.id;
END;
```

### Transcript identity (settle before Phase 2)

Today "a transcript" is `get_output_dir()/<stem>.md`. That resolves to `./output` relative to the **CWD** when that directory exists, and to `<data dir>/output` otherwise (`~/Library/Application Support/wisper-transcribe/output`, `%LOCALAPPDATA%\wisper-transcribe\output`, `~/.local/share/wisper-transcribe/output`). Docker relies on the CWD check to get `/app/output`. Once the DB (always in the data dir) records which transcripts exist, a CWD-dependent transcript folder means a launch from another directory makes every transcript look deleted and every file look new.

Rules:
1. **The output root is a setting, not a CWD check** (decided). New `output_dir` config key, overridden by `WISPER_OUTPUT_DIR` (same pattern as `WISPER_DATA_DIR`). Blank means `<data dir>/output`. `get_output_dir()` stops looking at the CWD. Docker sets `WISPER_OUTPUT_DIR=/app/output` in the compose env anchor. The Phase 0 migration writes `output_dir` when the old CWD rule would have resolved to a `./output` other than the default, so nothing moves on upgrade. Moving transcripts later is `wisper config set output_dir <path>` (the user moves the files; reconcile re-finds them by stem).
2. The registry holds only transcripts under the output root, keyed by stem. That is the web UI's scope today, and stems are unique there. Stems are **NFC-normalized** on register and reconcile.
3. **Reconcile** runs at startup and on list pages, which already glob:
   - An unregistered `.md` gets a new row.
   - A row whose `.md` is gone gets `missing_since` set but keeps its campaign position (the campaign page already renders missing entries) **and all of its companions**, because the file may come back (a sync, an unmounted drive). A reappearing file clears the flag.
   - **Case-insensitive filesystems** (macOS and Windows defaults; probed once at startup on the output dir): a `.md` whose stem matches a row case-insensitively updates that row's stored stem instead of creating a new row, so a case-only rename keeps its links.
   - A companion is an orphan only when it has **no `.md` and no row**. The sweep deletes only pattern-identifiable orphans (`.summary.md`, `_diar.json`, `_excerpt_*`, atomic-write temp files) and never deletes a generic audio file.
   - Rows are never deleted automatically (decision 5). A missing campaign entry gets a **Relink** action: pick an unregistered `.md` and move the old row's identity onto it, keeping campaign, journal, and speakers.
   - On list pages reconcile does only cheap work (stat, insert, flag). A changed mtime or size marks the row stale for search (Phase 6); the background worker reindexes it, never the request.
4. **`register(stem, *, origin)`** — `origin` is `job` (the app just wrote the file) or `reconcile` (found on disk):
   - Live row + `job` (overwrite or re-transcribe): keep the id, campaign position, and journal entries. Replace `transcript_speakers`, the audio path (deleting the old `output`-root copy if it differs), the sidecar, and search blocks. If the transcript was already journaled, `register()` sets that campaign's `journal_stale_since` in the same transaction — no automatic un-fold.
   - Missing row + either origin: the file is (re)appearing, so clear `missing_since`; a `job` origin also replaces speakers/audio/search as above and logs "reused stem of a previously missing transcript".
5. **Name collisions are blocked, with an explicit overwrite** (decided):
   - Web upload: before submitting, the route checks for `<stem>.md` in the output root. If present, the form returns "A transcript named *X* already exists" with **Overwrite** (resubmit with `overwrite=True`) and **Cancel**.
   - Recording re-transcribe always overwrites, behind a confirmation step.
   - CLI keeps "Skipping — use `--overwrite`", and names the campaign the existing transcript belongs to.
   - The job re-checks immediately before writing. If the file appeared meanwhile and overwrite wasn't chosen, the job fails with "Transcript already exists" instead of today's silent skip that reports success on the old file (`pipeline.py:473`).
6. `wisper transcribe -o elsewhere --campaign X`: register and associate only when the output lands under the output root. Otherwise warn that the web UI won't see it (decided).

### Migrating existing installs

- **Mechanism.** `db.py` holds an ordered list of Python migration functions, versioned by `PRAGMA user_version`. The stdlib `sqlite3` module is enough: no ORM, no Alembic, no new dependency. Connections use Python 3.12+ `autocommit=True` with an explicit `BEGIN IMMEDIATE`, avoiding the legacy implicit-transaction mode.
- **Each phase is one migration:** create its tables, then import its legacy files if present, in **the same `BEGIN IMMEDIATE` transaction that bumps `user_version`**, with `PRAGMA defer_foreign_keys=ON`. `PRAGMA foreign_key_check` must come back empty before commit, or the migration rolls back. The result is all or nothing.
- **Editable until merge** (decision 24): the branch ships as one PR, so no installed DB ever has an intermediate schema. Later phases edit earlier migrations in place instead of stacking `ALTER TABLE` rebuilds; after merge, migrations are frozen and changes go in new ones. Consequence for development: import deletes the legacy JSON and version numbers get reused, so **branch work runs against a copied data dir** (`WISPER_DATA_DIR=~/wisper-dev-data`), never `~/Library/Application Support/wisper-transcribe`. `wisper db status` warns when the DB was created by an unmerged build.
- **Idempotency.** Re-running is a no-op because the version already moved. If the server and a CLI command start at the same moment, the second blocks on the write lock, then sees the new version and skips.
- **Downgrade guard.** A DB whose `user_version` is newer than the code knows makes the app refuse to start, with a clear message. An old build must not write to a newer schema.
- **Backup.** Before a migration that imports legacy data, copy those legacy files into `<data dir>/backups/pre-sqlite-v<N>-<timestamp>/`. Before any later schema migration, snapshot `wisper.db` with the sqlite3 backup API. Audio is never copied.
- **Dirty data** (existing installs contain exactly what foreign keys forbid), handled without aborting:
  - A campaign stem with no `.md` is imported as a registry row with `missing_since` set, so order is kept.
  - A stem listed in two campaigns stays in the first; the rest are reported.
  - A member whose profile key isn't in `speakers.json` is dropped and reported.
  - `journaled_sessions` entries with no transcript are dropped and reported.
  - A sidecar or metadata file that fails to parse is skipped and reported.
  - Two members of one campaign bound to the same Discord id (the new `UNIQUE`): the first keeps it, the rest are cleared and reported.
  - A row that would violate a `CHECK` (an unknown status, an active recording with an `ended_at`, a missing required timestamp) is repaired to the nearest valid value when that's unambiguous (e.g. an active status at import becomes `failed`, as startup reconcile would do), otherwise skipped, and always reported.
  - A sidecar `input_path` outside the output root (old sidecars point at temp dirs or user-chosen files) imports as NULL audio and is reported. The enroll wizard already handles missing source audio.
  - A sidecar `campaign` whose transcript has no `campaign_transcripts` row is associated if that campaign exists, and reported.
- **Unparseable top-level JSON** (`speakers.json`, `campaigns.json`) rolls the transaction back, leaves `user_version` unchanged, and stops startup with a message naming the file and the backup. Importing an empty store instead would silently lose data.
- **Import report** goes to the log and to `import-report.txt` in the backup dir.
- **Legacy files after import are deleted** once the import transaction has committed (decided). This covers `speakers.json`, `.npy` files, `campaigns.json`, `recordings.json`, `metadata.json`, and `_diar.json` (rewritten with only `diarization_segments`). The pre-import copies in `backups/pre-sqlite-*` are kept (decided). A crash between commit and delete is harmless: the version has already moved, so nothing re-imports, and the next startup deletes files a committed migration already imported.
- **No downgrade support** (decided): no `export-legacy`, no rollback path. The downgrade guard stays because it's a few lines and stops an old build from writing to a newer schema.

### Concurrency

- **Connection per unit of work.** `with db.transaction(data_dir) as conn:` opens, sets pragmas (`foreign_keys=ON`, `busy_timeout=5000`), runs, commits, closes. There is no module-level cached connection, so `WISPER_DATA_DIR` and the per-test data dir keep working, and threads never share a connection (event loop, `to_thread` worker, capture threads, tick thread).
- **Every load-modify-save uses `BEGIN IMMEDIATE`.** A deferred read-then-write gets `SQLITE_BUSY` on lock upgrade without waiting on `busy_timeout`. The `threading.Lock`s go away; the DB lock also covers other processes.
- **Never hold a write transaction across ML or LLM work.** This is today's "not around embedding extraction" rule, generalized. For example, `relabel_campaign()` computes first, then writes renames in one short transaction.
- **Capture hot path.** `record_completed_wav_segment()` and marker appends are single-row autocommit INSERTs, once per 60 s rotation. They keep the "never raises" contract: if busy past a short timeout, log and continue, and startup reconcile rebuilds the manifest from the `NNNN.wav` files on disk.
- **The one-job-at-a-time invariant is unchanged.** It protects the model globals, not storage, and the DB neither needs nor relaxes it. The worker writes job transitions (pending → running → terminal) as short transactions.
- **Async routes** call sync `sqlite3` inline. That costs about the same as today's inline JSON reads (sub-millisecond at this scale). Revisit only if a query gets slow.
- **Journal mode.** Rollback journal (`DELETE`) everywhere (decided). Write volume is tiny, it works on any filesystem, and WAL needs shared memory that can misbehave on network or virtualized filesystems. WAL is the upgrade if read/write contention is ever observed (open question 7).
- **Docker.** The DB lives in `/data`, next to `config.toml`. Transcripts stay on the `/app/output` mount and recordings on the nested `/data/recordings` mount, which is why stored paths are relative (to the output root) or derived from fixed layout (recordings), never absolute. **Verified 2026-09-30** on Docker Desktop for Mac (VirtioFS bind mount, rollback journal, `python:3.14-slim` with SQLite 3.46.1), 3 containers × 5,000 `BEGIN IMMEDIATE` read-modify-write transactions, connection per unit of work: 15,000/15,000 increments, no lost updates, no hangs, `integrity_check` ok, with real interleaving between writers. So the CLI container and `wisper-web` can share `./data`. **A host process and a container writing at once corrupts the DB**: the same test with one host writer and one container writer produced `database disk image is malformed`, lost updates, and duplicate values. Locks don't cross the VM boundary, which matches public reports of SQLite on Docker Desktop bind mounts, where WAL is worse still, so decision 7's rollback journal is required there, not just preferred. Native Linux Docker shares the host kernel and isn't affected.
  - **Runtime guard** (decision 27): `db.connect()` records a lease in `runtime_leases`: `container` if `/.dockerenv` exists, else `host`. A container also records `crosses_vm = 1` when `/proc/version` contains `linuxkit` (Docker Desktop's VM kernel). The server refreshes its lease every 60 s; CLI commands refresh theirs while running. Startup refuses, naming the other holder and suggesting `docker compose run wisper …`, only when the *other* runtime's heartbeat is under 2 minutes old **and** the container lease has `crosses_vm = 1`, so native Linux Docker (e.g. a host CLI beside containers on a Linux server) is never blocked. `wisper db status` shows both leases; a crashed holder's lease expires after 2 minutes. It's advisory (its own tiny writes use the same locks, and two processes starting in the same second could both pass), but it stops the sustained concurrent writing that corrupts.
- **Windows.** An open DB holds a file lock, so docs note "stop the server before moving or restoring the data dir". Stored relative paths use POSIX separators and are converted with `pathlib` on read. `%APPDATA%` is local, not a synced folder, but a data dir redirected into OneDrive via `WISPER_DATA_DIR` gets a docs warning. **Researched:** python.org Windows builds compile SQLite with `SQLITE_ENABLE_FTS5` and bundle 3.50.4 for 3.13 and 3.14 (`PCbuild/sqlite3.vcxproj`, `get_externals.bat`), so the ≥ 3.43 + FTS5 requirement holds. `os.replace()` onto a file another process holds open without `FILE_SHARE_DELETE` fails with WinError 5, 32, or 33 (Obsidian, antivirus, the search indexer); that's a known pattern with a standard retry fix (below).

### Phases (each merges separately and ships its migration, tests, and docs)

Manager **public APIs stay stable** through Phases 1–4 (`load_profiles()` → `dict[str, SpeakerProfile]`, `load_campaigns()`, `get_transcripts_for_campaign()`, `load_recordings()`, …). Routes, the CLI, and the CLAUDE.md mock targets therefore barely change in the early phases, and most of the ~200 test references keep working. Internals switch from JSON to SQL. A later cleanup phase can narrow the APIs (e.g. drop whole-store `save_*`).

**Phase 0 — Foundation (no data moves).**
- New `db.py`: `connect()`, `transaction()`, migrations runner, downgrade guard, backup helper.
- `wisper db status | backup | dump`.
- Migrations run on first `connect()`; startup in `web/app.py` calls it early so a failure is reported before serving.
- SQLite capability check at startup: version ≥ 3.43 **and** FTS5 compiled in, tested by creating `temp` `fts5(x, content='', contentless_delete=1)`. Either failure refuses startup with a message naming what's missing, instead of failing mid-migration.
- **Output root setting** (Transcript identity rule 1): `output_dir` config key + `WISPER_OUTPUT_DIR`; `get_output_dir()` drops the CWD check; the migration pins a non-default CWD `./output` into `output_dir`; `docker-compose.yml` sets `WISPER_OUTPUT_DIR=/app/output`.
- `db.to_rel()` / `db.from_rel()`: the only place stored relative paths are converted to and from real paths under the output root.
- **Dev-data guard:** `db.py` has `SCHEMA_FROZEN = False` on the branch. While it's `False`, migrating refuses when the data dir is the platform default (no `WISPER_DATA_DIR`), with "This is an unmerged development build; set WISPER_DATA_DIR to a copy of your data". Flipping it to `True` is part of the merge checklist, and a test fails if `main` ever has it `False`. Tests are unaffected because the autouse fixture always sets `WISPER_DATA_DIR`.
- Runtime guard (`runtime_leases`, see Concurrency › Docker).
- **Windows CI:** add a `windows-latest` job (Python 3.13) that runs `test_db.py`, `test_transcript_store.py`, and the atomic-write tests: real file locks, real `os.replace` refusals (a test holds the target open), a case-insensitive filesystem, and backslash paths. The Linux matrix stays the full suite.
- Tests: `test_db.py`, plus a guard test that no module outside `db.py` calls `sqlite3.connect`, in the spirit of `test_tailwind.py`. Output root: default, config, env override, and the CWD-pinning migration. Dev-data guard: refuses the default dir while unfrozen, allows an override. Runtime guard (with `/.dockerenv` and `/proc/version` mocked): a fresh Docker Desktop container lease refuses a host start and vice versa; a stale lease, or a native-Linux container lease (`crosses_vm = 0`), is allowed.
- Docs: architecture.md (module map; "Database" replaces "File-store locking"; output-root resolution), `docs/cli-reference.md`, `docs/configuration.md` (data layout, backup, `output_dir`/`WISPER_OUTPUT_DIR`), `docs/docker.md`.

**Phase 1 — Profiles + campaigns** (coupled through `rename_profile()` → `rekey_member()`).
- Import `speakers.json` and `campaigns.json`. `campaign_transcripts` temporarily holds stem text plus position.
- Embeddings to BLOB: import `.npy`. **The one API break in Phase 1:** `SpeakerProfile.embedding_path` is replaced by `embedding: Optional[np.ndarray]`, filled by `load_profiles()` in the same query (~1 KB per profile). `load_profile_embedding(profile)` keeps its signature and returns `profile.embedding` after the embedding-space check. `update_embedding()` and `enroll_speaker()` write the column. `pipeline.py:345`'s direct `np.load(embedding_path)` switches to `load_profile_embedding()`. Reference clips stay key-named files, located by a new `reference_clip_path(key, data_dir)` helper.
- Rename becomes one transaction across profile and memberships.
- Changes: `speaker_manager.py`, `campaign_manager.py`, `models.py`, `pipeline.py`. `web/routes/speakers.py` and `campaigns.py` change only if an API narrows. ~13 test files reference `embedding_path` (mechanical churn).
- **`wisper speakers doctor`** (scoped in from "Speaker consistency"): lists profile pairs whose embeddings score above 0.95 cosine (likely duplicates), profiles in an old embedding space, and pipeline-shaped junk names (`AUTO_NAME_RE`: `SPEAKER_NN`, `Unknown Speaker N`). Report only, no automatic fixes; it points at `wisper speakers remove`/`rename`.
- Tests: rewrite `test_speaker_manager.py`/`test_campaign_manager.py` internals, plus importer tests (including the dirty-data cases above), plus `speakers doctor` on synthetic near-duplicate embeddings.

**Phase 2 — Transcript registry and links (fixes the #64 class).**
- New `transcript_store.py`: `register()` (the origin rules in "Transcript identity"), `reconcile()`, `relink()`, and `delete_transcript()` as the only delete path, following the ordering rule. Everything that unlinks a `.md` calls it.
- Migration rebuilds `campaign_transcripts` onto `transcript_id` and imports `journaled_sessions` into `journal_entries` (setting `journal_sha256` from the current `journal.md`). Recordings' `transcript_path` is linked by Phase 4.
- Changes:
  - `web/routes/transcripts.py`: single delete, bulk delete, list, campaign assign, and Relink; `_delete_transcript_companions()` moves into the store.
  - `web/routes/transcribe.py`: the upload name-collision check with Overwrite/Cancel.
  - `web/routes/record.py`: `_purge_recording_files()`; the re-transcribe confirmation.
  - `web/routes/campaigns.py`: the missing-entry rendering reads `missing_since`; Relink; the stale-journal banner.
  - `pipeline.py`: CLI registration, the `--campaign` rule, and the skip message naming the existing transcript's campaign.
  - `web/jobs.py`: register on completion; the pre-write collision re-check (fail with "Transcript already exists").
  - `recording_manager.py`: interim `clear_transcript_link(stem)` for the delete path (removed in Phase 4).
  - `journal.py`: `unjournalled_sessions()`, `update_journal()`, `rebuild_campaign()`, new `refold_campaign()`; stop writing `journaled_sessions` into the frontmatter.
  - `web/jobs.py` and `web/routes/campaigns.py`: a `refold` journal-job mode alongside `rebuild`.
  - `cli.py`: `transcripts list/move`, `campaigns journal/reorder`.
- **Journal write rule.** Body and entries are two stores, and the `.md`-first rule doesn't cover them (body first → a crash folds the session twice; row first → it's silently lost). A fold runs the LLM call outside any transaction, then: (1) writes the new body to a temp file, (2) in one `BEGIN IMMEDIATE` inserts the `journal_entries` row and sets `campaigns.journal_sha256` to the new body's hash, (3) `os.replace`s the temp file into place. A crash before (2) folded nothing and the temp is swept; a crash between (2) and (3) is detected as a hash mismatch with a leftover temp file, which is then moved into place. On read: `journal.md` **missing** → clear that campaign's `journal_entries` and `journal_stale_since` (fresh start) and log it; **present but a different hash** → the user edited it in Obsidian, which is allowed, so keep the entries and adopt the new hash. `refold_campaign()` and `rebuild_campaign()` clear `journal_entries` and then `journal_stale_since` in the transaction that records their reset (the trigger sets the flag as the entries go, so the clear must come after).
- **Campaign order and moves under the constraints.** SQLite checks `UNIQUE(campaign_id, position)` row by row, so `position = position + 1` collides. Reorder is two steps in one transaction: shift the campaign's rows above the current maximum, then write the final positions. Moving a transcript to another campaign never prompts: it deletes the transcript's journal entry in the same transaction (the composite FK refuses the move otherwise), which marks the old campaign's journal stale. Its text is untouched.
- **Stale journal.** `journal_stale_since` is set when a journaled session leaves the campaign (move, unassign, delete) or is overwritten by a re-transcribe. The campaign page shows "This journal mentions sessions that were moved, removed, or re-transcribed since it was written" with a **Rebuild journal** button. `wisper campaigns show` prints the same flag. Nothing is regenerated automatically.
- **Journal rebuild from existing summaries** (decided). Each session's `.summary.md` is written once and persists; only `journal.md` is rewritten by each fold. So the default rebuild is a new `refold_campaign()`: reset the journal and its entries, then fold every session's existing summary in campaign order — **one LLM call per session**, and it keeps any edits made to `.summary.md` files. A session with no summary yet is summarized first (only those). The existing `rebuild_campaign()` stays as **Rebuild from transcripts** (re-summarize everything, then fold; two calls per session; overwrites summaries), for when the summaries themselves are bad. Both run as the existing `JOB_CAMPAIGN_JOURNAL` with a confirmation showing the call count, and both clear `journal_stale_since`. Web: the banner's primary button is Rebuild journal, with Rebuild from transcripts as the secondary. CLI: `wisper campaigns journal <slug> --rebuild` becomes the re-fold, and a new `--rebuild --resummarize` is the full redrive.
- **Export with frontmatter:** a download or CLI export of the journal that adds `journaled_sessions` from the DB to its frontmatter. The same export path can later add DB-held metadata (campaign, speakers) to transcript or summary downloads.
- **Atomic file writes.** New `atomic_write_text(path, text)` helper (temp file in the same dir, then `os.replace()`, the pattern `recording_manager` already uses). Every transcript, summary, sidecar, and journal write goes through it: `pipeline.py` (transcript output), `web/jobs.py` (sidecar, excerpt `.txt`, refine, summarize, live draft), `web/enroll_shared.py` (wizard rewrite), the edit and fix-speaker routes, `speaker_registry._write_sidecar`, `journal.py`. A crash mid-write then leaves the old file or the new one, never a truncated `.md` that reconcile would register and search would index. Temp names get a recognisable prefix so reconcile can sweep leftovers. **Windows:** `os.replace()` raises `PermissionError` (WinError 5, 32, or 33) while Obsidian, antivirus, or the indexer holds the target open, so on Windows only the helper retries those errors with backoff (about 8 attempts, 10 ms doubling, ≤ 1.5 s total), then falls back to an in-place write, logs a warning, and removes the temp file. POSIX calls `os.replace()` once.
- **Missing-transcript detection** (scoped in from the open bug): when a transcription job registers its output, it checks the `.md` exists. If not, the job fails with a distinct error ("Transcript file missing after write") instead of reporting success, and the resolved output root goes into the job log. Covers web jobs and the recording hand-off.
- **Bulk actions UI** on `/transcripts`: row checkboxes plus a toolbar for delete and assign to campaign, wired to the existing `/transcripts/bulk-delete` and `/transcripts/bulk-campaign` routes. Those routes are rewritten in this phase anyway. Delete goes through a confirmation step. Same bulk-select pattern as `/recordings` (a separate hidden form, since rows contain their own forms).
- Tests: cascade tests for every delete path (including the interim recordings-JSON revert); the missing-file job failure; bulk actions through the UI form fields; reconcile (external delete keeps order; reappearing file clears the flag; case-only rename keeps the row on a case-insensitive FS; NFC and NFD names map to one row); `register()` origin cases; upload collision (form offers Overwrite; overwrite keeps campaign and journal entries; the pre-write re-check fails the job); Relink; journal write rule (crash between each step, deleted `journal.md` resets entries, edited `journal.md` keeps them, rebuild clears them); `atomic_write_text` retry and fallback with `os.replace` mocked to raise; a guard test that no module outside `transcript_store.py` unlinks `*.md` in the output dir.

**Phase 3 — Diarization sidecar data.**
- Import `_diar.json`'s speaker map, provenance, embeddings, and `input_path` into `transcript_speakers` and `transcripts.audio_rel_path` (relative to the output root; a path outside it → NULL, reported). The sidecar is rewritten with only `diarization_segments`.
- The enroll job's campaign comes from `get_campaign_for_transcript(stem)` instead of the sidecar's `campaign` key (`jobs.py:1381`), so a reassigned transcript uses its current campaign.
- Changes: `jobs._write_enrollment_sidecar()`, `JobQueue._run_wizard_enroll()` (source audio via `db.from_rel()`, campaign lookup), `web/enroll_shared.py` (`resolve_current_names()`, `apply_renames()`), `speaker_registry.py` (`_load_sidecar`/`_write_sidecar`, `embeddings_to/from_sidecar`), and `web/routes/transcripts.py` and `transcribe.py` (enroll wizard).
- `apply_renames()` rewrites the `.md` and then updates `speaker_map` rows. Under the ordering rule the file comes first. A crash in between leaves the rows stale, so the existing interval-matching fallback stays as the repair path.
- CLAUDE.md's "`_diar.json` carries the authoritative `speaker_map`" gotcha is rewritten to name the table.
- Tests: the largest fixture churn (36 `_diar.json` references across 6 test files), moved to a helper that seeds the DB.

**Phase 4 — Recordings.**
- Import `recordings.json`, each `metadata.json`, `discord_speakers` → `recording_speakers.profile_id`, and `unbound_speakers` → `recording_speakers` rows with NULL profile. This fixes rename-not-rekeying for free. Discord fields go to `recording_discord`, `devices` to `recording_devices`. `status` splits: capture states import into `capture_status`; `transcribed` and `transcribing` import as `completed` (their meaning now comes from `transcript_id` and the job). `transcript_path` links to `transcript_id`. `job_id`, `combined_path`, `per_user_dir`, segment `path`/`stream`, and marker `elapsed_s` are not imported: they're derived (see "Schema principles"). `Recording` keeps these as computed properties, so routes and templates barely change. `Recording.status` is computed in `load_recordings()`'s SQL (`transcript_id IS NOT NULL`, plus `EXISTS` on a `pending`/`running` job for `transcribing`), not by a dataclass property reaching into `web/jobs.py`. Between Phases 4 and 5 an in-memory `JobQueue.find_active_job_for_recording(id)` stands in; it's branch-only and never ships (decision 11).
- **Legacy manifest check (done 2026-09-30):** nothing wrote `segment_manifest` before #58, and since then only `record_completed_wav_segment()` writes it, always `stream="mixed"` under `recordings/<id>/combined/`. So dropping the stream and path columns loses nothing. A non-`mixed` or off-layout entry, if one ever turns up, is skipped and reported. An empty-string `discord_user_id` (in members or `discord_speakers`) imports as NULL / unbound.
- Delete `save_recording_merged()`, the per-recording mutex, and Phase 2's interim `clear_transcript_link()` (`ON DELETE SET NULL` on `recordings.transcript_id` replaces it); `reconcile_on_startup()` becomes one `UPDATE`.
- **Stuck `transcribing` is fixed structurally:** today `reconcile_on_startup()` only resets `recording`/`degraded` (`recording_manager.py:451`), so a restart mid-transcription leaves `transcribing` forever and `record.py:922` refuses to re-transcribe. With `transcribing` derived from a live job, a restart simply has no active job. The restore-on-failure dance at `record.py:936` goes away too.
- **Recover crashed sessions.** Today a session interrupted by a crash is marked `failed` with no `combined.wav`, so it never gets a Transcribe button even though its segments are on disk. Startup keeps marking it `failed` (fast, no file work) and sets `recoverable` when combined segments exist. A **Recover** button on the recording page and `wisper record recover <id>` join the segments with the existing `concat_wav_segments()` (off the request thread), write `combined.wav`, and mark the recording `completed` with `recovered_at` set. It then shows under "Awaiting transcription" like any other, and the detail page notes it was recovered and may be missing the last partial minute. Segments are self-contained WAVs (file-format invariant 1), so recovery needs no repair step. `recoverable` is derived (failed + segments on disk), not stored.
- Changes: `recording_manager.py`, `web/discord_bot.py`, `web/local_capture.py` (hot-path contract above), `web/routes/record.py`, and the `wisper record` CLI.
- Tests: `test_recording_manager.py`, record routes, a hot-path test with the DB held busy, unbound-speaker derivation (including a deleted profile unbinding its speakers), derived status (`transcribed` follows `transcript_id`; `transcribing` only while a job is active; a restart mid-job leaves it transcribable), the subtype constraints, and recovery: a crashed session with synthetic segments becomes `completed` and transcribable; a crashed session with no segments isn't offered recovery; recovery refuses an active session.

**Phase 5 — Job history.**
- Write-through from `JobQueue` at submit and at each status transition. On terminal status, store `error_code` (the same generic codes, never exception text) and the last ~200 log lines.
- `params_json` holds an allowlisted subset of kwargs: no secrets, no temp paths. It includes the resolved output root, so a job that wrote somewhere unexpected is visible afterwards (the missing-transcript bug).
- At startup, pending and running rows become `failed` / "Interrupted by restart". They are never auto-resumed: the uploads are gone and the jobs are multi-hour GPU work. A recording's `transcribing` state and its "view job" link now come from `jobs.recording_id` (latest job), so recordings need no job column and job history survives restarts.
- The in-memory 50-job cap stays. The DB keeps every job (decided).
- UI (decided): the dashboard keeps its 20 most recent (memory plus DB). A new paginated **Job history** page (50 per page, filter by type and status). "Jobs for this" links on transcript and campaign pages.
- Changes: `web/jobs.py`, `web/routes/dashboard.py` and `transcribe.py`, `docs/web-ui.md`.

**Phase 6 — Full-text search** (needs only Phase 2's registry, so it can move earlier).

*What it does:* search every transcript (and its session summary) for words or phrases, e.g. "every session where Strahd comes up". Results show the campaign, session, speaker, timestamp, and a highlighted snippet, and link straight to that moment in the transcript. Filters: campaign, speaker, transcript vs summary.

*Index design:*
- **One row per speaker block,** not per transcript, so a hit points at a moment rather than a 2-hour file. Blocks come from `formatter.parse_transcript_blocks()`, the parser the rename logic already uses. Summaries are indexed per section.
- **Contentless FTS5** (`content=''`, `contentless_delete=1`) stores only the index, not a second copy of the text. That keeps the DB small: the index is estimated at roughly a third of the text size (measure in this phase), e.g. ~25 MB for 500 two-hour sessions. The cost is that FTS5 can't produce snippets itself, so the search route reads the matching blocks from the `.md` files (one file read per result transcript on a page of 20) and builds snippets there.
- **Deletes follow the foreign keys.** FTS tables can't hold foreign keys, so `search_blocks` holds them. Deleting a transcript cascades to `search_blocks`, whose trigger removes the FTS rows. The test suite must confirm the trigger fires on cascade deletes.
- **One FTS column (`text`).** Speaker lives on `search_blocks`, which the speaker filter joins, so it isn't stored twice.
- **Tokenizer:** `porter` stemming (so "fights" matches "fight"), accent-insensitive, with prefix indexes so `Stra*` is fast. Fantasy names aren't damaged by stemming because both the query and the text are stemmed the same way.

*Keeping it fresh:*
- **App writes reindex immediately.** Every path that rewrites a `.md` (wizard renames, the edit page, fix-speaker, refine, summarize, relabel) calls `transcript_store.reindex(stem)`. Indexing one transcript is one short transaction: delete its blocks, insert the new ones, record mtime and size.
- **External edits** (Obsidian, sync) are caught by reconcile when `indexed_mtime_ns`/`indexed_size` differ from the file's. Reconcile only marks the row stale (`indexed_mtime_ns = NULL`); the background backfill worker does the reindex, so no parsing happens in a request.
- **Initial build** runs as a background backfill after startup, outside the migration transaction, one transcript per transaction, so a large archive doesn't delay startup and a crash resumes where it stopped. The search page shows "Indexing N of M" until the backfill finishes.
- **The index is disposable.** `wisper db reindex` drops and rebuilds it from the files. A corrupt or stale index is never data loss.

*Query handling:*
- **Plain input by default:** each word is quoted before `MATCH`, so FTS5 syntax characters (`"`, `*`, `-`, `NEAR`, `:`) are inert and a stray quote can't cause a syntax error. A trailing `*` stays as a prefix search. Phrase search with `"double quotes"` is supported.
- A query FTS5 still rejects returns a generic "couldn't search for that" message, never exception text.
- Ranking uses `bm25()`, grouped by transcript on the results page.
- **Snippets are XSS-safe:** the block text is HTML-escaped first, then the matched terms are wrapped in `<mark>`. No `| safe` on raw transcript text.
- **Stale snippets:** snippets are rebuilt from the current `.md` by `block_idx`, so before building one the route compares the file's mtime and size to `indexed_mtime_ns`/`indexed_size`. On a mismatch it shows "Transcript changed — reindexing" without a snippet, links without an anchor, and marks the row stale.
- **Highlighting is approximate; matching is exact.** Porter matches "fights" to "fight", but Python can't reproduce the stemmer, so each query term is highlighted by prefix: strip a common suffix (`-s`, `-es`, `-ed`, `-ing`), then match `\b<prefix>\w*` case-insensitively over the escaped text. A hit with no highlighted term is acceptable.

*Changes:*
- `transcript_store.py`: `reindex()`, the backfill, and a `search()` query helper.
- New route `web/routes/search.py`: `GET /search?q=&campaign=&speaker=&kind=&page=`. Plus a search box in the sidebar.
- Transcript detail: per-block anchors (`id="b-<index>"`, numbered in the same order as `parse_transcript_blocks()`), so results deep-link with `#b-<index>` and the matched terms are highlighted.
- CLI: `wisper search "query" [--campaign] [--speaker] [--limit]`, printing stem, timestamp, speaker, and snippet.
- Every `.md`-rewriting path listed above gains its `reindex()` call.

*Availability:* contentless-delete needs SQLite ≥ 3.43. The shipped platforms have it: this Mac's venv reports 3.53; python.org 3.13+ builds on macOS and Windows, and Debian trixie (the `python:3.14-slim` base) bundle newer. Older SQLite is **not supported** (decided): Phase 0's startup check refuses to start below 3.43 or without FTS5, with a message naming what's missing. `docs/setup.md` lists the requirement. This affects only a local venv on an old Linux system SQLite (e.g. Ubuntu 22.04's 3.37); Docker is unaffected.

*Tests:*
- Block and summary indexing from synthetic transcripts.
- Reindex on each app write path.
- Reconcile marks a row stale on an mtime or size change, and the backfill reindexes it.
- A snippet for a file changed since indexing shows the "reindexing" state instead of the wrong block.
- Highlighting: a stemmed match ("fights" → "fight") is highlighted.
- Transcript delete leaves no `search_fts` rows (the trigger fires on cascade).
- Query escaping: quotes, `*`, `-`, `NEAR`, and column syntax are inert.
- XSS: `<script>` in transcript text renders escaped in snippets.
- Filters, paging, the backfill resuming after an interruption, and CLI output. The capability check (in `test_db.py`) refuses a mocked 3.42 and a mocked missing FTS5.

*Docs:* `docs/web-ui.md` (search page), `docs/cli-reference.md` (`wisper search`, `wisper db reindex`), architecture.md (index design, freshness rules), CLAUDE.md gotcha ("every `.md` rewrite calls `reindex()`").

**Phase 7 — Cleanup.**
- Remove the remaining JSON code paths (the importers stay, frozen, for old installs).
- Narrow APIs where the stability shims are no longer needed.
- **Documentation review, top to bottom.** Read every doc in full, not just the sections each phase touched: README.md, architecture.md, CLAUDE.md, `.claude/rules/`, every file in `docs/`, and the SQLite section of plan.md (removed once merged, per the Documentation Rules). Check each against the merged code for:
  - stale JSON-era descriptions: `speakers.json`, `campaigns.json`, `recordings.json`, `_diar.json` as the source of truth, `journaled_sessions` frontmatter, `threading.Lock` file-store locking, the CWD `./output` rule, "nothing persists across restarts";
  - wrong paths, commands, flags, config keys, and env vars;
  - contradictions between docs;
  - missing coverage of new behaviour: `wisper db`, `output_dir`, missing transcripts and Relink, name collisions, job history, search, crashed-session recovery, the dev-data-dir rule.

  Docstrings and code comments in changed modules get the same pass.
- **Test suite re-evaluation, end to end.** Review every test file, not only the ones each phase changed:
  - **Delete** tests that only exercised removed JSON code paths.
  - **Rewrite** tests that now pass vacuously: they mock a manager the code no longer calls, assert on a file the DB replaced, or test a foreign-key rule on a connection with `foreign_keys` off.
  - **Consolidate** the per-phase fixtures (legacy seeding, `_diar.json` helpers, DB seeding) into one shared set.
  - **Confirm every invariant has a test:**
    - each schema constraint;
    - each guard test: no `sqlite3.connect` outside `db.py`, no `.md` unlink outside `transcript_store`, atomic writes only, `reindex()` on every `.md` write;
    - each delete path's cascade;
    - each dirty-data import case.
  - **Add end-to-end flows** through `TestClient` with ML mocked, against a real temp DB (no manager mocks):
    - upload → transcribe → assign to campaign → summarize → fold into journal → rename a speaker → search → delete, checking DB rows, files, and the search index at each step;
    - a legacy data dir → import → the same flow;
    - Discord recording → transcribe → delete the transcript → the recording is transcribable again.
  - **Check** the full suite's runtime against the pre-migration baseline, and add the session-scoped template DB if it has grown noticeably.
  - **Run** the coverage report (`--cov`) and review uncovered lines in `db.py`, `transcript_store.py`, and the importers.

### Test strategy

- The autouse `_isolated_data_dir` fixture already gives each test its own `WISPER_DATA_DIR`, so each test gets its own `wisper.db` for free, provided `db.py` caches nothing across calls. The cost is migrations on first connect in each test (a few ms). If the suite slows noticeably, add a session-scoped template DB that is copied per test.
- **`test_db.py`:**
  - Fresh install migrates to the latest version.
  - A re-run is a no-op.
  - The downgrade guard refuses a newer DB.
  - `foreign_keys` is on for every connection `db.connect()` returns.
  - `BEGIN IMMEDIATE` under two threads: the second waits instead of erroring.
  - One cross-process import race under spawn (the macOS default): exactly one import happens.
  - **Schema constraints:** one should-fail insert per rule in "Schema principles" (the prototype's checks, kept as tests): subtype FKs, the journal composite FK (including a move that fails until the entry is deleted), the stale-journal trigger firing on move, unassign, and cascade delete but cleared by either rebuild; `refold_campaign()` folds existing summaries without re-summarizing (and summarizes only sessions missing one), leaving `.summary.md` files untouched,, each `UNIQUE`, each `CHECK`, `STRICT` type rejection, cascades (including the FTS trigger firing on a cascade), and the two-step reorder under `UNIQUE(campaign_id, position)`.
  - Enum `CHECK` lists match the Python constants.
  - `foreign_key_check` is empty after every migration on a synthetic legacy import.
- **Importer tests** use a synthetic legacy data dir built by a frozen copy of today's serializers (`tests/_legacy_store.py`), so the tests don't change when the managers do. Cases: clean import, each dirty-data case, a malformed top-level file (rolls back, version unchanged, backup present), and a re-run.
- **Invariant guards:** no raw `sqlite3.connect` outside `db.py`, no `.md` unlink outside `transcript_store.py`, no plain `write_text()` on transcript, summary, sidecar, or journal paths outside `atomic_write_text()`, and (Phase 6) every function that writes a transcript `.md` calls `reindex()`.
- **Cross-platform paths:** relative-path round-trips through `PureWindowsPath`/`PurePosixPath` and `db.to_rel()`/`from_rel()`, so the Windows logic is tested on macOS and Linux CI. Case-insensitive reconcile is tested by mocking the filesystem probe, so it runs on Linux CI too.
- **One CI change:** a `windows-latest` job for the storage tests (Phase 0). `sqlite3` is stdlib, and the bundled SQLite on 3.13/3.14 (macOS 3.53, Windows 3.50.4, Debian slim 3.46.1) supports `RETURNING`, `DROP COLUMN`, `STRICT`, and contentless-delete FTS5.
- Existing test rules still hold: no GPU, network, or real audio, and synthetic data only.

**Docs touched across the phases:**
- architecture.md: Module Map; Data Storage tree and "Output directory"; "File-store locking" → "Database"; Job Queue ("Nothing persists across restarts" changes in Phase 5); Test Strategy; Known Constraints (host-plus-container DB writes, WAL).
- `CONTRIBUTING`-style note in architecture.md or CLAUDE.md: branch work uses a copied `WISPER_DATA_DIR`; the `SCHEMA_FROZEN` flip is on the merge checklist.
- `docs/configuration.md`: data layout (default paths per OS), backups, `output_dir`/`WISPER_OUTPUT_DIR` (and that the CWD `./output` check is gone), the `WISPER_DATA_DIR` + synced-folder warning.
- `docs/setup.md`: the SQLite ≥ 3.43 + FTS5 requirement.
- `docs/cli-reference.md`: `campaigns journal --rebuild` (re-fold) and `--rebuild --resummarize`, `wisper db`, `wisper search`, the `transcribe --campaign` rule and collision message, `wisper speakers doctor`, `wisper record recover`, `output_dir`.
- `docs/docker.md`: DB location, backup, CLI and web containers sharing `./data` (verified safe), a "pick one way of running" line — Docker (web and CLI both in containers) *or* the native CLI with a local `wisper server`, never a native CLI against the container's `./data` while the container runs (verified to corrupt on Docker Desktop; the runtime guard refuses it), `WISPER_OUTPUT_DIR=/app/output`.
- `docs/scenarios.md`: restore from backup, moving the output dir (`output_dir`), externally deleted or renamed transcripts (Relink), deleting `journal.md`, recovering a crashed recording session.
- `docs/web-ui.md`: missing transcripts and Relink, the stale-journal banner and the two rebuild options, upload name collisions (Overwrite/Cancel), bulk actions, job history, search.
- CLAUDE.md: new gotchas (connect only through `db.py`; `BEGIN IMMEDIATE`; no transaction across ML work; delete transcripts only via `transcript_store`; `speaker_map` location; `get_output_dir()` no longer checks the CWD).
- README: unchanged.

### Decisions (2026-09-30)

1. **Output root:** a setting (`output_dir` / `WISPER_OUTPUT_DIR`, default `<data dir>/output`), no CWD check. Upgrade pins an existing CWD `./output`; Docker sets `/app/output`. Replaces the earlier drift-warning/reconcile-freeze design.
2. **CLI `--campaign` outside the output root:** warn and skip the association.
3. **Embeddings:** DB BLOBs. Size check: 1 KB per profile and 1 KB per label per transcript. 500 transcripts × 8 labels ≈ 4 MB, which is negligible for SQLite.
4. **`_diar.json`:** split. Names, provenance, embeddings, and the audio path go to the DB; segments stay in a slim sidecar.
5. **Missing transcripts:** keep flagged rows until the user removes them.
6. **Legacy files:** delete them after a committed import; keep the `backups/pre-sqlite-*` copies. No downgrade support.
7. **Journal mode:** rollback journal everywhere.
8. **Deleting a recording's transcript:** the recording goes back to `completed` (Discord and local). With derived status this is just `transcript_id` → NULL.
9. **Job history:** keep every job; the dashboard shows the 20 most recent, plus a new paginated history page.
10. **Journal frontmatter:** stop writing `journaled_sessions`; add a journal export that includes it.
11. **Merge cadence:** stack phase PRs on `feat/sqlite-storage`; one PR to `main` when the feature is complete.
12. **Full-text search:** added as Phase 6, as a derived contentless FTS5 index over transcript blocks and summaries.
13. **Search summaries:** yes, with a transcript/summary filter.
14. **Stemming:** `porter`.
15. **SQLite < 3.43 or without FTS5:** unsupported; startup refuses with a clear message.
16. **Scoped in from elsewhere in this file:** missing-transcript detection (Phases 2 and 5), `wisper speakers doctor` (Phase 1), the bulk-actions UI on `/transcripts` (Phase 2), and the Docker alignment check done alongside the Docker DB check.
17. **Failure handling:** atomic writes for every transcript, summary, sidecar, and journal file, with a Windows retry/fallback (Phase 2); crashed recording sessions recoverable via a Recover button and `wisper record recover` (Phase 4); recordings can't get stuck in `transcribing`, because the state is derived (Phase 4).
18. **Name collisions:** blocked, with an explicit Overwrite in the web UI; recording re-transcribe overwrites after confirmation; the CLI keeps `--overwrite`. Overwrite keeps the transcript's identity, campaign, and journal entries.
19. **Profile embeddings in the API:** `SpeakerProfile.embedding_path` → `embedding` (the one Phase 1 API break).
20. **Journal consistency:** the DB commit is the fold's commit point, with `journal_sha256`; a deleted `journal.md` resets its entries, an edited one keeps them. Transcripts move, unassign, and delete freely; a journaled one leaving or being re-transcribed marks the journal stale, and the user chooses whether to rebuild.
21. **Unbound Discord speakers:** one `recording_speakers` table with NULL profile; deleting a profile unbinds its speakers. Keying by profile-key text (no FK) is dropped.
22. **Source-audio paths:** relative to the output root (every app-created copy lives there); anything else imports as NULL.
23. **Schema discipline:** normalized (3NF/BCNF), derived values not stored, `STRICT` tables, DB-enforced constraints and subtypes, indexed FK children, `foreign_key_check` gating every migration. See "Schema principles".
24. **Branch migrations are editable until merge:** nothing ships between phases (decision 11), so each phase edits the migrations in place rather than stacking `ALTER`s. After merge they're frozen.
25. **Windows CI:** a `windows-latest` job for the storage tests, added in Phase 0.
26. **Dev-data guard:** unmerged builds refuse to migrate the default data dir (`SCHEMA_FROZEN`).
27. **Host + container on one DB:** verified to corrupt on Docker Desktop. Guarded by an advisory runtime lease that only enforces when the container is inside Docker Desktop's VM, documented ("use Docker, or the native CLI with a local web server, not both at once on one data dir"), and tracked as an open bug (guarded, not fixed). Container + container is verified safe.
28. **Branch lifetime:** a long-lived branch is accepted; search and the smaller add-ons stay in this PR.
29. **Journal rebuild:** the default re-folds existing summaries (one LLM call per session, keeps summary edits); "Rebuild from transcripts" is the full redrive.

### Open questions

None. Next step: sign-off on the schema, then Phase 0.

---

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal shipped first and set the pattern: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

**Depends on the SQLite migration's Phase 2.** Build these on the transcript registry and `journal_entries` rather than stem lists and `journaled_sessions` frontmatter. Combined-summary and recap outputs get a registry-style row, so deletes cascade.

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
