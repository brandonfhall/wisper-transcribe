# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Docker Desktop + native CLI on one data dir can corrupt the DB

With the web server in Docker Desktop (Mac or Windows) and `./data` bind-mounted, a native `wisper` command pointed at the same `./data` (via `WISPER_DATA_DIR`) and writing at the same time can corrupt `wisper.db`: file locks don't cross the Docker Desktop VM boundary. Reproduced 2026-09-30 (one host writer plus one container writer gave `database disk image is malformed` and lost updates). Container + container is fine (15,000/15,000 writes), and native Linux Docker is unaffected.

**Status:** guarded, not fixed. The runtime lease (`db.py`, `runtime_leases`) makes the second runtime refuse to start when the container is inside Docker Desktop's VM; it is advisory (two processes starting in the same second could both pass). Documented in `docs/docker.md` ("One way of running at a time"). A real fix would need a lock that crosses the VM (e.g. routing host CLI writes through the container's server API).

## Manual verification owed

- **Live recording + campaign journal:** `LIVE_AUDIO_TEST_PLAN.md` — real-device capture, live transcript, journal browser flows, bulk delete, busy-queue notice.
- **Live Discord acceptance test.** The recording pipeline (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is covered by synthesized-PCM tests, but the JDA → socket → Python path needs one real session: record a few minutes with 2+ speakers, play the per-user WAVs before binding any speaker (a bound user's track is deleted when the session ends or on binding), and run Transcribe.
- **GPU Docker image on an NVIDIA host.** The "Docker Build" workflow is disabled on GitHub, and the GPU image can only be built (not run) without NVIDIA hardware. Confirm a diarized job on the GPU image downloads the alignment model into `./cache/` and logs "Aligned words".
- **SQLite storage in a browser and on real capture** (automated coverage: `test_e2e.py`, `test_schema.py`). With `WISPER_DATA_DIR` pointing at a copy of real data:
  - Journal: the stale-journal notice (move a folded session to another campaign) on the Campaign and Journal pages; **Rebuild journal** vs **Rebuild from transcripts** confirmations and their call counts; journal **Download** includes `journaled_sessions`.
  - Job history page, filters, paging, and a historical job's page after a restart.
  - Recording end to end: record a few minutes, local and Discord. Add markers, including two in quick succession, and edit the notes mid-session, then stop. After stop, only `combined.wav` and (local) `live_transcript.md` remain; Discord also keeps unbound users' `per-user/<uid>/`. Transcribe it, play it back, and jump to a marker.
  - Search: edit a transcript in Obsidian while the server runs and search for the new words (the result shows "changed — reindexing", then matches after a reload).
- **Campaign folders, real journal fold:** fold a session into a campaign journal with a real LLM and confirm `<folder> Journal.md` updates in the campaign folder (rehearsals covered it only with a mocked LLM).
- **macOS loopback.** Record page on a Mac with BlackHole installed: BlackHole appears under System Audio and captures audio.

---

## Dependency pins

- **Drop `av<19`** (approved, Brandon 2026-10-05) (pyproject) once a faster-whisper release stops passing `metadata_errors=` to `av.open()`; check with a CPU Docker build and one transcription. Blocked as of 2026-10-06: 1.2.1 is the latest release and still passes it (`faster_whisper/audio.py:46`).

---

## Storage — open

- **Store `combined.wav` as FLAC** (about half the size). Approved (Brandon, 2026-10-05); not started. Touches the fixed `recordings/<id>/combined.wav` layout and every reader of it.

**Decided (Brandon, 2026-10-06)**
- New captures write `combined.flac` at Stop (both `_finalise()` methods and `recover_recording()`). If the encode fails, keep the `.wav` and register it, so a capture is never lost to an ffmpeg failure.
- Existing `combined.wav` stays readable forever. Every reader accepts either suffix and prefers `.flac` when both exist. `storage trim --apply` converts a `.wav` it finds; nothing else does.
- Schema: a new `_V12` that only rebuilds `files` to accept `combined.wav` or `combined.flac`, mirroring the v11 `files` rebuild. Develop it with `SCHEMA_FROZEN = False` and freeze at merge, as v11 did (`37e0d64`).
- `combined/NNNN.wav` segments stay WAV (deleted at trim); `live_transcript.md` is text.
- Segments are already 16 kHz mono s16 (`audio_writer` `RATE = 16000`), the format `encode_flac` writes, so conversion is lossless. Verify the FLAC's frame count equals the WAV's before deleting the WAV.

**Current behaviour**
- The path is fixed and derived: `recording_manager.combined_path_for()` (`recording_manager.py:74-75`) returns `recordings/<id>/combined.wav`; `_check_derived()` rejects any other path (`:250-251`).
- The DB pins the path twice: `files` CHECK `kind='combined' → rel_path='recordings/'||recording_id||'/combined.wav'` (`db.py:649`, and the v11 rebuild at `:858`).
- Writers build it by frame-concatenating WAV segments, never re-encoding: `web/audio_writer.concat_wav_segments()` (`audio_writer.py:202-240`) is called by `discord_bot._finalise()` (`discord_bot.py:531-538`) and `local_capture._finalise()` (`local_capture.py:551-558`), and by `recording_manager.recover_recording()` (`recording_manager.py:579-580`).
- `register_capture_files()` registers it as `kind="combined"` (`recording_manager.py:544`); `file_registry._scan_data()` finds it by the literal name (`file_registry.py:718`).
- Verifiable-WAV readers: `recording_manager._wav_frames()` uses stdlib `wave.open` (`:600-606`), so `trim_recording_audio`/`_trim_targets` (`:627-668`) only ever verify a WAV; `load_recordings` sets `combined_path` from `combined.exists()` (`:168-187`).
- Other readers: `transcript_store.audio_path()`/`_audio_for()` (`transcript_store.py:2499-2531`); `storage_trim.plan()` (`storage_trim.py:263`) and `_drop_copy()` (`:396`); playback route `_AUDIO_TYPES` already maps `.flac` (`web/routes/transcripts.py:163,694-710`, `audio_path` served as `audio/flac`); `record.py` `has_audio=bool(rec.combined_path)` (`:64`) and the hand-off (`:963,1015`); `transcripts.py:354` "Awaiting transcription"; the recording detail page. ffmpeg readers (`_extract_speaker_excerpts`, `convert_to_wav`) handle FLAC unchanged.
- `scripts/alignment_eval.py` reads only its own `clip.wav` (`:148`), never `combined.wav`; `convert_to_wav` re-encodes FLAC inputs.

**Change**
- New `audio_utils` helper (e.g. `concat_wav_segments_flac()`, or `encode_flac()` after `concat_wav_segments()`) producing `<id>.flac`, since segments are already 16 kHz mono s16 and `encode_flac` (`audio_utils.py:201-242`) re-encodes to exactly that.
- `recording_manager.combined_path_for()` gives the `.flac` path for new writes; a resolver (e.g. `combined_path_existing()`) returns `.flac`, else `.wav`, else None for readers; `_check_derived` accepts either suffix; `_wav_frames` is replaced by a FLAC frame/duration probe (`probe_format` exists at `audio_utils.py:244`; a FLAC frame count needs `ffprobe` or `soundfile`-free parsing) so trim verification still works.
- New migration (`_V12`, a new version — shipped migrations are frozen, `db.py:54,965-977`) rebuilds `files` only to relax the `combined` CHECK to accept `combined.wav` or `combined.flac`. **It must not move or rewrite any file** (migrations never touch user files); it only widens the constraint so a converted `.flac` row validates.
- Conversion of existing recordings is a **storage-trim action, not a migration**: extend `trim_recording_audio`/`plan` with a convert step that verifies the WAV (existing `_wav_frames` logic), encodes to `.flac`, `set`/`move`s the `combined` row, then deletes the `.wav` after commit. Do this before or as part of the existing trim so `combined/` deletion and per-user trim still run.
- `file_registry._scan_data()` matches both suffixes; `sync()` already refuses to guess an unregistered `.flac`, so a recording-owned `combined.flac` with an existing `combined` row is the one that gets picked up.
- `recover_recording()` and both `_finalise()` methods write the FLAC form going forward.

**Every caller/reader affected**
- Capture hand-off: `discord_bot._finalise` (`:531-538`), `local_capture._finalise` (`:551-558`), `recover_recording` (`:579-580`).
- Storage trim: `plan` DROP_COPY/CONVERT decision (`storage_trim.py:263`), `_drop_copy` (`:396`), `trim_recording_audio` verification and targets (`recording_manager.py:627-748`).
- Transcript audio resolution: `transcript_store.audio_path`/`_audio_for` (`:2499-2531`), `_companion_paths` already excludes anything under `<data>/recordings/` (`:1539-1546`) so delete is unaffected.
- Playback: `transcripts.py` route and `_AUDIO_TYPES` (`.flac` already handled); MIME stays `audio/flac`.
- Recording UI/API: `record.py:64,963,1015`, `transcripts.py:354`, `models.Recording.combined_path` comment (`models.py:132`).
- File registry: `register_capture_files` (`:544`), `_scan_data` (`:718`), `db.py` CHECKs (`:649,858`), `test_schema.py` baselines (`:62,225,251`).
- Tests referencing the literal path: `test_recording_manager.py` (many, e.g. `:137,546,622`), `test_discord_bot.py:444`, `test_local_capture.py`, `test_record_routes.py` (many), `test_web_routes.py:518`, `test_web_jobs.py:1047`, `test_file_registry.py`, `test_storage_trim.py:204,210`, `test_audio_writer.py`.
- Docs: `architecture.md` (Recording layer, Data Storage tree, Known Constraints, Storage trim), `docs/configuration.md:74`, `docs/cli-reference.md:529-530`, `docs/scenarios.md:133`, `docs/web-ui.md:67,75,95,180,226`, `docs/docker.md` if it names the file.

**Tests**
- `encode_flac`/concat helper: mocked ffmpeg writes the FLAC through a temp name; failure leaves the WAV and its row.
- Trim verifies a converted FLAC and still deletes `combined/` + per-user; a truncated/zero-frame FLAC blocks the trim (mirror `test_recording_manager.py:531-743`).
- Conversion action converts an existing `.wav` recording in place, updates the `combined` row (path + size), deletes the `.wav`, and is idempotent (`test_storage_trim.py`, `test_schema.py` for the new CHECK).
- `combined_path` is None when neither form exists; `.flac` preferred when both.
- Recording transcribe hand-off submits the `.flac` (`test_record_routes.py`); playback serves `audio/flac`; recover writes FLAC (`test_recording_manager.py:460-466,672-716`).
- No real audio: FFmpeg mocked as today.

- **Minor** (approved fix, Brandon 2026-10-05; not started): with an unfrozen schema and `WISPER_OUTPUT_DIR` unset, a CLI command creates the configured output folder (empty) before the dev guard refuses (`path_utils.get_output_dir` mkdir; guard at `db.py:1183`). The server path creates nothing. Fix before the FLAC branch, which runs unfrozen.

---

## Forced word alignment — follow-ups

Design in `architecture.md` ("Forced word alignment"). The `forced_alignment = auto` default rests on spot-check evidence, not a full labelled set: on a 2 h episode, aligned and unaligned runs disagreed on 155 of 20,675 words; one-word "islands" inside another speaker's run were 10 (unaligned) vs 3 (aligned); 4 of 4 hand-checked disputed words were right with alignment.

- **If attribution at speaker changes regresses, or before changing the aligner, smoothing thresholds, or the default,** run `scripts/alignment_eval.py` (see `docs/scenarios.md`) on a live-table excerpt and label its sheet. Arms already exist for the open questions: `aligned-guard-1s` (on edited podcast audio, words moved >1 s favoured Whisper 8 vs 1, the opposite of the Hanataz spike's 28 vs 1), `aligned-smooth-1w` / `aligned-nosmooth`, and `aligned-exclusive`.
- **Fallback engine:** `torchaudio` MMS_FA + star token (no new dependency, but CC-BY-NC weights and weaker on crosstalk). Only if the `transformers` dependency becomes a problem; recipe in git history (`6286e21`).
- **Watch:** `Qwen3ASR*` is new in transformers 5.x and may be renamed; the calls are covered by tests.

---

## Speaker consistency — remaining

Design in `architecture.md`. Open items:

- **Diarization measurement set.** 8–12 hand-corrected excerpts of 2–3 min, stratified by speaker count (2–3 / 4–5 / 6–8), in-room vs remote, low vs high overlap. Report DER split into missed / false alarm / confusion (`pyannote.metrics`) plus JER, and compare configs with a paired bootstrap over recordings (B ≥ 1000). The 0.55 threshold and the community-1 choice rest on one session pair until this exists.
- **Ruled out for now:** Sortformer (4-speaker cap), DiariZen (CC-BY-NC), NVIDIA Nemotron diarization (no independent validation). Revisit only if community-1 plateaus on the measurement set.

---

## Live recording — feature requests

- **Change input devices mid-session.** `LocalCaptureManager.start_session()` binds both capture threads to fixed device IDs; switching today means Stop + Start (a new `Recording` and a gap in the transcript). Needs capture threads that can restart against a new device while the tick thread, segment writers, and `Recording` keep running. Needs a design pass first.
- **Per-speaker diarization on the live system track.** The system track is a single RMS-attributed "Other", because OS loopback is already a mixed-down stream. pyannote only gives consistent labels across a whole-file pass; live use would need an incremental layer (embed each turn, match against a running per-session speaker pool) plus added latency in the live loop. Revisit if the You/Other split becomes limiting.
- **Channel picker on the Record page.** `GET /api/record/channels` already lists the guilds and voice channels the bot can see; the Record page still takes raw IDs or a preset.
- **Replay markers into the ticker on reload.** Markers persist (`recording_markers`) and show on the detail page, but a page reload doesn't re-insert them into the live ticker (only transcript lines come back through SSE).

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

## Job cancellation — stop the GPU on cancel (approved)

**Decided (Brandon, 2026-10-05): option 1** — run the job's ML work in a subprocess and terminate it on cancel. Not started. The `parallel_stages` pool can't be reused as is: it never terminates its workers (below), so this needs a worker process the job owns.

**Decided (Brandon, 2026-10-06)** — these override the options and recommendations further down:
- **The whole ML pipeline runs in the worker:** transcribe, diarize, align, and embeddings. The module-level caches (`transcriber._model`, `diarizer._pipeline`, `speaker_manager._embedding_model`, `word_alignment._fa_model`/`_fa_processor`) live in the worker, not the server.
- **One persistent warm worker,** reused across jobs. Stop kills it, and the next job respawns it lazily and pays the model reload.
- **Gated by a new config key, `transcribe_subprocess`, on by default.** Off runs today's in-process path. Wire it like `parallel_stages` (`CONFIG_CHOICES`, web Config page, `docs/configuration.md`).
- **Web jobs only.** CLI `wisper transcribe` stays in-process.
- **Transcription, enrollment, and relabel jobs** use the worker, so batch jobs share one copy of the models. `JOB_LIVE` stays in-process: its real-time loop can't take a cross-process hop.
- **Boundary:** the worker computes and returns plain picklable results (segments, embeddings, alignment output). The parent does every DB, `file_registry`, and transcript-store write, and reads speaker profiles (`load_profiles`) to pass in. The worker never opens `wisper.db`.
- `parallel_stages` inside the worker is out of scope for the first cut; decide while implementing whether it stays in-process-only.

**Current behaviour**
- Cancel sets `job._cancel_event` (`web/jobs.py:1009-1011`); the running job thread only sees it at `capturing_write()`/`ProgressCatcher.write()` (`jobs.py:1216-1245`), i.e. on a tqdm write.
- `pipeline.process_file()` (`pipeline.py:382-697`) calls `transcribe()` in-process (`:545`) or through `_run_parallel_transcribe_diarize()` (`:541`); neither knows about the cancel event.
- `_run_parallel_transcribe_diarize()` (`pipeline.py:141-215`) runs `_transcribe_worker`/`_diarize_worker` in a `with ProcessPoolExecutor(...)` block (`:197-206`) and blocks on `future.result()`; its drain thread calls `tqdm.write` (`:183`), so a cancel raises `InterruptedError` in the drain thread only — the pool keeps running.
- So today `parallel_stages` does **not** actually stop the GPU on cancel. plan.md's "already does this" is not accurate; option 1 needs new plumbing.
- `_transcribe_worker` (`pipeline.py:121-126`) is already module-level/picklable and calls `_patch_tqdm_for_queue(queue, "transcribe")` (`:75-118`) before ML imports.
- `jobs._run_transcription_job` wraps `process_file` in layer 2 of the tqdm patching (`jobs.py:1205-1340`); `debug_log.Logger` is layer 1; `_patch_tqdm_for_queue` is layer 3 (`architecture.md` "tqdm patching is load-bearing in three layers").

**Change**
- Give the ML pipeline its own long-lived worker process, not a `with`-scoped pool, so the parent holds a `Process` object and can call `.terminate()` (or `.kill()` on Windows) when `job._cancel_event` is set.
- Add a module-level watcher in `jobs._run_transcription_job`: while the transcription subprocess runs, poll `_cancel_event` (e.g. a `threading.Timer`/thread or a loop with `proc.join(timeout)`) and terminate the process; then raise `InterruptedError` in the job thread so the existing `except InterruptedError` path runs (`jobs.py:1325-1329`).
- Reuse `_transcribe_worker` + `_patch_tqdm_for_queue` for logs/progress; parent drain thread forwards `"log"` via `tqdm.write` (so layers 1 and 2 still capture) and `"bar"` to `job.progress_channels`.
- Pass `_cancel_event` awareness **only** in the parent; the subprocess needs no cancel hook — termination is the mechanism.
- Terminating the worker must leave the job's WAV and any already-written files intact; the job ends Cancelled with nothing half-written.
- Config: decide whether this is always-on or gated by a new key (e.g. `transcribe_subprocess`), rather than overloading `parallel_stages`. If a key is added, wire `CONFIG_CHOICES`/web Config like `parallel_stages` (`config.py:121-126`, `web/routes/config.py:35`).

**Model-caching cost (the main trade-off)**
- Today `transcriber._model` lives in the server process and survives across jobs (`transcriber.py:106-175`; `architecture.md` "Module-level model caches"). A per-job subprocess reloads the multi-GB Whisper model every job (~seconds to tens of seconds).
- Options: (a) accept the reload; (b) a persistent worker subprocess kept warm between jobs, restarted only when it is terminated by cancel or crashes; (c) keep the model in the parent and only offload decode (not possible with CTranslate2).
- Recommend (b): a single idle worker reused across jobs, killed on cancel and lazily respawned. Keeps the one-job-at-a-time invariant.

**Windows spawn semantics**
- Windows (and macOS) use `spawn`: the child re-imports the package and re-runs module top-level code. `_transcribe_worker` and `_patch_tqdm_for_queue` are already module-level and picklable (`pipeline.py:75-126`), and `_noise_suppress` must run before ML imports in the child (as `_diarize_worker` does, `:129-136`).
- A persistent worker needs an explicit `multiprocessing.get_context("spawn")` and `freeze_support()` where a frozen entry point exists; the server has no `__main__` guard today.
- Passing a `multiprocessing.Manager().Queue()` works under spawn (the existing comment at `pipeline.py:162-164` explains why a plain `Queue` can't be pickled).

**One-job-at-a-time invariant**
- Unchanged: exactly one worker slot (`web/jobs.py:797`). The subprocess is a per-job resource; nothing new runs concurrently.
- `JobQueue.stop()` (`jobs.py:594-601`) cancels the worker task but cannot stop `asyncio.to_thread`; the new process must be terminated on shutdown too, or it outlives the server and holds the GPU. Add it to the `CancelledError` path (`jobs.py:1074-1082`) and to `stop_all_live`-style shutdown.

**tqdm layers (all three)**
- Layer 1 `debug_log.Logger` — unchanged; parent `tqdm.write` from the drain thread still tees to the log.
- Layer 2 `jobs._run_transcription_job` — must keep patching the parent's `tqdm` for `job.log_lines`/`job.progress`; the drain thread calls into it, so a cancel check there can also stop the process.
- Layer 3 `_patch_tqdm_for_queue` — runs in the child only; never touches the parent's tqdm. Check all three before changing any.

**SSE log streaming / progress**
- Unchanged path: child → IPC queue → parent drain thread → `tqdm.write`/stderr → `job.log_lines` + `job.progress_channels` → `GET /transcribe/jobs/{id}/stream` (`web/routes/transcribe.py:418-492`).
- The "done" event and `Cancelled` error must still be emitted (`transcribe.py:463-482`; test `test_record_live_routes.py:399-413`).
- Parallel-mode UI already reads `progress_channels` (`jobs.py:820`); the single transcribe process should feed the `transcribe` channel so pills/percent still work.

**Tests** (`test_web_jobs.py`, `test_pipeline.py`; no GPU/network/real audio)
- A fake subprocess whose `terminate()` is asserted when `_cancel_event` is set; the job ends `FAILED`/`Cancelled` and the upload is deleted.
- Normal completion still runs `process_file` semantics and the post-processing chain.
- Worker reuse: two jobs, one process (if option (b)); after a cancel the next job respawns.
- Shutdown mid-job terminates the process and records the job interrupted (`test_web_jobs.py:204-238` pattern).
- `pipeline` unit tests stay on the in-process path unless `transcribe_subprocess` is explicitly enabled, so existing `process_file` tests are unaffected.
- `parallel_stages` tests (`test_pipeline.py:807+`, `test_pipeline_folder.py`) must still pass.

**Docs**
- `architecture.md`: "Parallel stage processing" (or a new "Job cancellation" subsection), the three-layer tqdm note, and the Known Constraints "Cooperative cancellation" row.
- `docs/web-ui.md:63` (Stop Job wording), `docs/scenarios.md:213` (best-effort cancellation), `docs/configuration.md` if a new key is added, `docs/cli-reference.md` if the CLI gains the subprocess path.


---

## DAVE sidecar → Python migration (parked)

The Java sidecar (JDA 6.3.0 + JDAVE 0.1.8) receives and decrypts DAVE-encrypted audio end-to-end. DAVE is mandatory for non-stage voice, so the only question is where it's implemented. DAVE is MLS over OpenMLS and every path depends on a native (Rust/JNI) binding; the choice is which language wraps it.

**Python readiness (as of 2026-10-05):**
- **pycord PR #3159** — DAVE receive for pycord, which has native voice receive. Out of draft but still open, milestoned for 2.9.0rc1; latest release is 2.8.1 (2026-07-25), which has DAVE for sending only. The right target once 2.9 ships.
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

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal sets the pattern: storage in the campaign's folder beside the journal (`<folder> Combined Summary.md`, `<folder> Recap.md`), registered as `files` rows owned by the campaign (new `files.kind` values in this plan's migration; a campaign folder rename then carries them, since it rewrites every row under the folder), `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

**Build on the database:** the transcript registry and `journal_entries`, not stem lists or frontmatter. Combined-summary and recap outputs get their own table with FKs to the campaign (and the sessions they cover), so deletes cascade; add it as a new migration and extend `test_schema.py`. The search index could cover them too (a new `search_index_state.kind`).

### 1. Combined summary

One LLM call over every session summary in a campaign → `<folder> Combined Summary.md` in the campaign folder. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
