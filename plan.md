# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Missing transcript file after a successful Transcribe job

A local-recording transcription once reported COMPLETED ("Wrote `<id>.md`") but the file never existed; root cause unknown. **Detection is now in place** (SQLite branch): the job fails with "Transcript file missing after write" and logs the transcripts folder, and job history keeps that log across restarts. **Next step:** if it recurs, check `/jobs/history` for the job's log and output root; run with `WISPER_DEBUG=1` to capture more (`LIVE_AUDIO_TEST_PLAN.md` §4a).

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
- **SQLite branch capture path (Phase 4).** Record a few minutes (Discord and local), add markers (including two in quick succession), stop, Transcribe, Re-transcribe, then delete the transcript and confirm the recording is transcribable again. Kill the server mid-session once, restart, and use **Recover recording**. The Live Discord acceptance test below covers the JDA half.
- **SQLite branch UI (Phase 2).** In a browser, with `WISPER_DATA_DIR` pointing at a copy of real data: the stale-journal notice (move a folded session to another campaign) on the Campaign and Journal pages; **Rebuild journal** vs **Rebuild from transcripts** confirmations and their call counts; journal **Download** includes `journaled_sessions`; Job history page, filters, paging, and a historical job's page after a restart; delete a transcript that's in a campaign and on a recording (recording returns to "Awaiting transcription").
- **SQLite branch search (Phase 6).** Deep links and highlighting were checked in a browser on the dev data. Still to check on a real archive: edit a transcript in Obsidian while the server runs and search for the new words (the result shows "changed — reindexing", then matches after a reload); watch "Indexing N of M" on first start after the upgrade; and run a speaker rename in the wizard, then search by the new name with the speaker filter.
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

- **Profile cleanup (user action).** `wisper speakers doctor` (SQLite branch) lists the candidates. Re-enroll every profile after the embedding-model change; delete the `speaker_*` / `SPEAKER_NN` junk and duplicate profiles; enroll Mike and Ben from sessions where they're clearly separated. `wisper speakers doctor` (flags identical or near-identical profiles) is scoped into the SQLite migration's Phase 1.
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

## Storage — SQLite migration (Phases 0–6 done; next: Phase 7)

Branch `feat/sqlite-storage`, pushed; one PR to `main` at the end (ask the user before opening it). Resume by reading this section and `git log` on the branch. Dev work uses a copied data dir (`WISPER_DATA_DIR=~/wisper-dev-data`); unmerged builds refuse the default data dir (`db.SCHEMA_FROZEN = False`). How everything works now is in architecture.md ("Database", "Output directory", "Recording layer", Job Queue, LLM journal).

### Done (Phases 0–5) — what differs from the original plan

- **Phase 0 (v1)** foundation: `db.py`, `wisper db status|backup|dump`, output root as a setting (`output_dir`/`WISPER_OUTPUT_DIR`, no CWD check), capability/downgrade/dev-data/runtime-lease guards, Windows CI job. The pre-upgrade snapshot runs on a second connection after the lock (backup API on the locked connection hangs). "Created by an unmerged build" is detected as schema drift, not a column.
- **Phase 1 (v2)** profiles + campaigns. The `transcripts` registry table was created here (not Phase 2), so `campaign_transcripts` referenced real rows from the start. `rekey_member()` removed. `save_profiles()`/`save_campaigns()` kept as whole-store replace (narrow in Phase 7). Real-data dry run dropped 4 empty roster entries whose profiles no longer existed.
- **Phase 2 (v3)** transcript store (single delete path, atomic writes, register/reconcile/relink), journal entries + stale flag, re-fold rebuild, upload name collisions, bulk actions, missing-file job failure.
- **Phase 3 (v4)** per-transcript speakers (`transcript_speakers`, `audio_rel_path`); `_diar.json` keeps only segments. Callers still use a sidecar-shaped dict via `transcript_store.read_sidecar()`/`write_sidecar()` (narrow in Phase 7).
- **Phase 4 (v5)** recordings with derived status/paths, Recover for crashed sessions. Recording times carry microseconds.
- **Phase 5 (v6)** job history (`job_history.py`), `/jobs/history`, historical job page, "Jobs" links.
- **Phase 6 (v7)** full-text search: `search_index.py`, `/search`, sidebar box, `#b-<index>`/`#s-<n>` deep links with in-page highlighting, `wisper search`, `wisper db reindex`. Differences from the plan: index state is per file (see the schema changes below); stale-marking deletes the blocks along with the state, so a changed transcript drops out of results until the backfill reindexes it (seconds); no prefix indexes; `save_transcript()`/`save_summary()` plus a guard test replace per-site `reindex()` calls; the pipeline's new `.md` is indexed by `register()`; a transcript with neither speakers nor timestamps is indexed per line; snippets center on the densest cluster of query terms, and summary snippets drop markdown markers. `docs/setup.md` now states the SQLite ≥ 3.43/FTS5 requirement (missing since Phase 0).
- **Schema changes after sign-off:** Discord-id CHECKs are digits-only and non-empty (`campaign_members`, `recording_speakers`; the signed-off `GLOB '[0-9]*'` only checked the first character). Recording timestamps have microseconds (still ISO-8601 UTC).
- **Phase 6 schema changes after sign-off (v7):**
  - `search_index_state` is keyed `(transcript_id, kind)`, one row per indexed file, not one per transcript. With a single row stat-keyed on the `.md`, an edit to `<stem>.summary.md` (a summarize job or Obsidian) was never detected.
  - `search_blocks` has a composite FK to `search_index_state(transcript_id, kind)` `ON DELETE CASCADE`, replacing its direct FK to `transcripts`. A block can't outlive the file state it was built from, and marking a transcript stale (deleting its state rows) removes its blocks and, through the trigger, its FTS rows. The cascade chain transcript → state → blocks → trigger → FTS is tested.
  - Added: `CHECK (kind = 'transcript' OR (speaker IS NULL AND start_s IS NULL))`, `speaker <> ''`, and an index on `search_blocks(speaker)` for the speaker filter.
  - No prefix indexes. Measured on 21 MB of real text: `prefix='2 3'` made the index 2.3× larger (1.31× vs 0.57× of the text) and saved about 10 ms on a two-letter prefix query. The plan's "a third of the text" estimate was low; without prefix indexes the index is about 0.6× the text, so 500 two-hour sessions come to roughly 30–40 MB.
- Browser and real-capture checks for all of this are under "Manual verification owed".

### Before merge

- Flip `db.SCHEMA_FROZEN` to `True` (`test_schema_frozen_on_main` fails on `main`/PRs into it otherwise).
- Remove `feat/sqlite-storage` from `.github/workflows/ci.yml` `push` branches; decide whether the Windows storage job stays.
- Remove this section from plan.md (Documentation Rules), keeping only what informs remaining work.
- Ask the user before opening the PR.

### Schema principles (still apply to Phases 6–7)


- **Normalized to 3NF/BCNF.** Every non-key column depends on the whole key and nothing else. Anything derivable is derived, not stored:
  - A recording's `transcribed` state is `transcript_id IS NOT NULL`; `transcribing` is "an active job exists for this recording" (the in-memory `JobQueue` before Phase 5, the `jobs` table after). Only the capture lifecycle is stored (`capture_status`). Decision 8's revert happens by `ON DELETE SET NULL` with no app code, and the stuck-`transcribing` bug can't happen. `Recording.status` stays a computed property so templates don't change.
  - Segment paths (`recordings/<id>/combined/<idx:04d>.wav`), `combined.wav`, and `per-user/` are fixed layout, so no path columns. The manifest's `stream` is always `"mixed"` (`recording_manager.py:377`), so no stream column.
  - Marker `elapsed_s` is `marked_at − started_at` (`started_at` never changes). This reverses the store-once choice commented at `append_marker()`.
  - `Recording.unbound_speakers` is `recording_speakers` rows with NULL `profile_id`.
- **No multi-valued columns (1NF).** `devices_json` becomes `recording_devices`. One documented exception: `jobs.params_json`, an audit snapshot of kwargs that is never queried by field, constrained to a JSON object.
- **Subtypes enforced by the schema.** Discord-only data (`guild_id`, `voice_channel_id`, speakers, rejoins) hangs off `recording_discord`, and local devices off `recording_devices`. Both use a composite FK to `recordings(id, source)`, so a local recording can't have Discord rows and vice versa.
- **Integrity in the DB, not in app code:** `STRICT` tables, explicit `NOT NULL`, `CHECK` on every enum and on paired-nullable columns (embedding + space), `UNIQUE` for every one-to-one rule the code enforces today (one Discord account per member per campaign — `bind_discord_id`'s loop; one campaign per transcript; one recording per transcript; campaign order positions).
- **Journal entries must belong to the campaign that holds the transcript:** composite FK to `campaign_transcripts(campaign_id, transcript_id)`. Transcripts always move freely: moving, unassigning, or deleting one drops its entry, and a trigger on `journal_entries` deletes (which also fires on cascades) marks the old campaign's journal **stale** (`journal_stale_since`). The journal text is never edited automatically; the user chooses when to rebuild.
- **Every FK child column is indexed** (SQLite doesn't do this; unindexed children make cascades full scans).
- **Surrogate integer ids** for everything user-renamable, so a rename is one `UPDATE`. UUIDs stay the key for recordings and jobs (they're already in URLs).
- **Controlled redundancy**, where a repeated value is forced to match by a constraint, so it can't diverge:
  - `journal_entries.campaign_id` is derivable from `campaign_transcripts`, but the composite FK needs it and guarantees it equals the transcript's current campaign. Without it, a move would silently carry the entry into the new campaign.
  - `recording_discord.source` and `recording_devices.source` are constants, pinned by `CHECK`, that exist only so the subtype FK can reference `recordings(id, source)`.
- **Job subject columns** (`jobs.transcript_id`, `campaign_id`, `recording_id`) record only the job's **direct** subject, never one derivable from it: a summarize job sets `transcript_id`, not the transcript's campaign; a journal job sets `campaign_id`; a recording transcription sets `recording_id` and, on success, the `transcript_id` it produced. They can't be `NOT NULL` per type, because `ON DELETE SET NULL` has to keep history rows when the subject is deleted.
- **Search-index state lives with the index** (`search_index_state`), not on `transcripts`, so the whole disposable index (state, blocks, FTS) can be dropped and rebuilt without touching registry rows.
- **Deliberate non-derivations**, each for a stated reason:
  - `profiles.key`: an alternate key (URL slug and reference-clip filename), written only by `rename_profile()`.
  - `transcript_speakers.display_name`: the name as rendered in that `.md`, which a profile rename doesn't rewrite. It's a fact about the file, not a profile FK.
  - `recording_speakers.profile_id`: who that Discord user was when captured. Needed for recordings with no campaign, where there's no membership to derive it from.
  - `search_blocks` and `search_fts`: a derived, disposable index (Phase 6).
- **Types:** timestamps are ISO-8601 UTC `TEXT`; booleans are `INTEGER CHECK IN (0,1)`; stored paths are relative to the output root and POSIX-separated, with a `CHECK` rejecting absolute paths, backslashes, and `..` components.
- **Checks at runtime:** `PRAGMA foreign_key_check` runs before every migration commits (a non-empty result rolls back); `wisper db status` runs it plus `PRAGMA integrity_check`. Imports use `PRAGMA defer_foreign_keys=ON` so load order doesn't matter.
- **Enum CHECKs mirror Python constants** (`JOB_*`, statuses, `SOURCE_AUTO`/`SOURCE_MANUAL`). A test asserts they match, so adding a job type without a migration fails CI.


### Remaining phases

**Phase 7 — Cleanup.**
- **Known issues from Phase 6 to resolve here:**
  - The search progress counter can stick at "Indexing N of M" when a present transcript's `.md` can't be opened by its NFC stem, for example an NFD filename on ext4 (files synced from a Mac into Docker), or a permission error. `reindex()` returns False and the row stays unindexed. First check whether `delete_transcript()` and the other `safe_path(nfc(stem))` readers fail the same way. If so, fix the NFC/NFD handling once for all of them. If not, record an "unindexable" state so the counter excludes it.
  - `test_recording_manager.py::test_concurrent_appends_are_all_kept` flaked once on the Windows job: 15 threads, 30 hot-path transactions, "database is locked", 11 of 15 kept. The hot path gives up after 500 ms by design. Decide whether the test's contention is realistic (live capture has one writer). Either make the test deterministic, or make the hot path retry a dropped marker, which reconcile can't restore.
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
30. **Schema signed off** on the condition that it's normalized and follows best practice. Final audit: every table `STRICT`; every FK child column indexed (the two subtype FKs via their `recording_id` primary key); redundancy only where a constraint pins it (listed under "Controlled redundancy"); derived values not stored except the documented non-derivations.

### Open questions

None. Next step: Phase 7.

---

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal shipped first and set the pattern: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

**Build on the SQLite registry** (on `feat/sqlite-storage` until it merges): the transcript registry and `journal_entries`, not stem lists or `journaled_sessions` frontmatter. Combined-summary and recap outputs get a registry-style row, so deletes cascade.

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
