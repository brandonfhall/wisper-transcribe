# Wisper-Transcribe: Podcast Transcription with Speaker Diarization

## Context

The user runs tabletop RPG actual-play podcasts (D&D-style) with 5-8 speakers (GM + players). They want to transcribe sessions into markdown transcripts with consistent speaker labeling across files. The transcripts will be fed into a NotebookLM-style system for querying game events and tracking stats.

**Hardware**: NVIDIA RTX 3090 (24GB VRAM) on Windows, Apple M5 Mac. Both platforms must be supported.
**Processing**: All local, no cloud APIs. CLI-driven.

## Technical Stack

**Custom pipeline: faster-whisper + pyannote-audio**

- faster-whisper: 4× faster than OpenAI whisper via CTranslate2, lower VRAM usage
- pyannote-audio: speaker diarization + voice embedding extraction
- Chose this over WhisperX due to chronic dependency pinning issues in WhisperX
- Direct embedding access is critical for cross-file speaker ID

**Key dependency: HuggingFace token** (free) required for pyannote models. One-time setup.
**System requirement: ffmpeg** for audio format conversion.

## Project Structure

```
wisper-transcribe/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── plan.md
├── src/
│   └── wisper_transcribe/
│       ├── __init__.py
│       ├── __main__.py            # python -m wisper_transcribe
│       ├── cli.py                 # Click CLI commands
│       ├── config.py              # Config loading, platform paths, ffmpeg check
│       ├── pipeline.py            # Main orchestrator
│       ├── transcriber.py         # faster-whisper wrapper
│       ├── diarizer.py            # pyannote diarization wrapper
│       ├── speaker_manager.py     # Speaker profiles, enrollment, matching
│       ├── aligner.py             # Merge transcription + diarization segments
│       ├── formatter.py           # Markdown output generation
│       ├── audio_utils.py         # Audio validation, conversion
│       └── models.py              # Data classes
└── tests/
    ├── test_models.py
    ├── test_config.py
    ├── test_audio_utils.py
    ├── test_transcriber.py
    ├── test_formatter.py
    ├── test_aligner.py
    ├── test_diarizer.py
    ├── test_pipeline.py
    ├── test_pipeline_folder.py
    └── test_speaker_manager.py
```

User data stored via `platformdirs` (outside the repo, never committed):
- Windows: `%APPDATA%\wisper-transcribe\`
- Mac: `~/Library/Application Support/wisper-transcribe/`

```
wisper-transcribe/          # user data dir
├── config.toml
└── profiles/
    ├── speakers.json       # name -> metadata mapping
    └── embeddings/         # .npy voice fingerprint files (gitignored)
```

## Processing Pipeline

```
1. VALIDATE     → audio_utils: check file exists, supported format
2. PREPROCESS   → audio_utils: convert to 16kHz mono WAV (if needed)
3. TRANSCRIBE   → transcriber: faster-whisper → text segments with timestamps
4. DIARIZE      → diarizer: pyannote → speaker-labeled time regions
5. ALIGN        → aligner: merge text segments with speaker labels
6. IDENTIFY     → speaker_manager: match anonymous labels to enrolled profiles
7. FORMAT       → formatter: produce markdown output
8. WRITE        → save .md file (one per input file)
```

## Speaker Labeling Lifecycle

### First run — enroll speakers interactively
```
$ wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6
```
After transcription + diarization, prompts for each speaker's name/role. Saves voice embeddings to profiles directory for future matching.

### Subsequent runs — automatic matching
```
$ wisper transcribe session02.mp3 --num-speakers 6
```
Extracts embeddings for each detected speaker, compares via cosine similarity against enrolled profiles (threshold: 0.65 default), assigns names. Unknown speakers labeled "Unknown Speaker N".

### Edge cases
- **New player:** appears as "Unknown Speaker N" → `wisper fix` + `wisper enroll`
- **Absent player:** their profile is simply ignored
- **Voice drift:** `wisper enroll --update` blends new sample via EMA (alpha=0.3)
- **Wrong match:** `wisper fix session.md --speaker "Alice" --name "Diana"`

## CLI Reference

```
wisper transcribe <path>          # file or folder
  -o, --output DIR
  -m, --model SIZE                # tiny/base/small/medium/large-v3 (default: medium)
  -l, --language LANG             # language code or 'auto'
  -n, --num-speakers INT
  --min-speakers / --max-speakers INT
  --enroll-speakers               # interactive first-run naming
  --play-audio                    # play each speaker's excerpt during enrollment
  --no-diarize
  --timestamps / --no-timestamps
  --device cpu|cuda|auto
  --compute-type auto|float16|int8_float16|int8|float32
  --vad / --no-vad                # voice activity detection to skip silence (default: on)
  --overwrite
  --verbose

wisper enroll <name> --audio <file>
  --segment START-END
  --notes TEXT
  --update                        # EMA blend with existing embedding

wisper speakers list|remove|rename|reset|test

wisper config show|set|path

wisper fix <transcript.md>
  --speaker NAME --name NEW_NAME [--re-enroll]
```

## Output Format

```markdown
---
title: Session 01 - The Dragon's Keep
source_file: session01.mp3
date_processed: '2026-04-05'
duration: 1:23:45
speakers:
- name: Alice
  role: DM
- name: Bob
  role: Player
---

# Session 01 - The Dragon's Keep

**Alice** *(00:00:12)*: Welcome back everyone. Last session you had just entered the ruins.

**Bob** *(00:00:18)*: Right, I want to check for traps before we go further in.
```

## HuggingFace Model Notes

Models are downloaded once on first use and cached to `~/.cache/huggingface/hub/`. Subsequent runs are fully offline.

| Model | Purpose | Size |
|-------|---------|------|
| `openai/whisper-*` (via faster-whisper) | Transcription | 75MB–1.5GB depending on size |
| `pyannote/speaker-diarization-3.1` | Speaker diarization | ~400MB |
| `pyannote/embedding` | Voice fingerprinting | ~200MB |
| `pyannote/segmentation-3.0` | Voice activity detection | ~100MB |

To check what's cached: `huggingface-cli scan-cache`

Required one-time license agreements (free, HuggingFace account):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/embedding
- https://huggingface.co/pyannote/segmentation-3.0

## Cross-Platform Notes

- Always use `pathlib.Path` for all file ops
- `platformdirs` for config/data directory resolution
- **Windows (RTX 3090)**: CUDA auto-detected. Use `large-v3` model. CUDA DLL path fix applied in `transcriber.py` for CTranslate2 compatibility.
- **Mac (M5)**: CPU-only (MPS unreliable for these models). Use `medium` model for speed.
- ffmpeg check on startup with platform-specific install instructions

---

## Implementation Status

### ✅ Phase 1 — Project Skeleton + Basic Transcription
All modules created, CLI entry point, single-file transcription to markdown, tests.

### ✅ Phase 2 — Speaker Diarization
pyannote pipeline wrapper, max-overlap aligner, HF token management, `--num-speakers` / `--no-diarize` flags.

### ✅ Phase 3 — Speaker Profiles & Cross-File Identification
`speaker_manager.py`: profile CRUD, embedding extraction, cosine-similarity matching with greedy assignment, EMA updates. `wisper enroll`, `wisper speakers`, `wisper fix` commands.

### ✅ Phase 4 — Batch Processing & Polish
`process_folder()` with tqdm progress bars, per-file error recovery, skip-existing, `--verbose` flag. Windows CUDA DLL path resolution. `wisper config` commands.

### ✅ Phase 5 — Tests & README
77 tests passing. All ML calls mocked. No GPU required for test suite. README with install, quick start, full CLI reference.

### ✅ pyannote-audio 4.x Upgrade (April 2026)
Upgraded from 3.4.0 → 4.0.4. Removed 5 compatibility shims (torchaudio stubs, hf_hub `use_auth_token`, torch.load default). speechbrain `LazyModule.ensure_module` patch retained — pyannote 4.x still uses speechbrain for ECAPA-TDNN embeddings and the Windows path bug is in speechbrain itself.

One additional fix required post-upgrade: pyannote 4.x wraps diarization output in a `DiarizeOutput` dataclass (`DiarizeOutput.speaker_diarization` is the `Annotation`), breaking the existing `diarization.itertracks()` call. Fixed in `diarizer.py` with a `hasattr` guard for backwards compatibility.

torchcodec still cannot find FFmpeg shared DLLs on this Windows install despite `Gyan.FFmpeg.Shared` being listed in `setup.ps1`. The scipy audio loading bypass (`scipy.io.wavfile` → waveform dict) remains as a workaround. Functionally equivalent; end-to-end test confirmed working (11 speakers enrolled, full `.md` output, CUDA device).

---

## Backlog

### Near-term (ready to build)

*(No remaining near-term items — see completed list below.)*

### ✅ Near-term completed

- **`wisper setup` command** ✅ — guided wizard: ffmpeg, HF token, model pre-download, device detection.
- **Progress header on each file** ✅ — Input/Output/Model line printed before each file processes.
- **Expose data paths in `wisper config show`** ✅ — config file, data dir, profiles dir, HF cache all shown.
- **`wisper config show` model clarity** ✅ — Models section: device, Whisper model, compute type (with auto-resolution), pyannote models.
- **Enrollment speaker order — chronological** ✅ — speakers sorted by first appearance timestamp in `pipeline.py`.
- **Audio playback during enrollment** ✅ — `--play-audio` flag; plays up to 10 s via pydub; silent fallback. (PR #3)
- **`wisper speakers reset`** ✅ — deletes all profiles and embeddings with confirmation prompt.
- **Phase 7 — Docker containerization** ✅ — `Dockerfile` (gpu/cpu targets), `docker-compose.yml`, `WISPER_DATA_DIR` env override in `config.py`. 77 tests.
- **Third-party warning suppression** ✅ — speechbrain/pyannote/torch noise suppressed by default; `WISPER_DEBUG=1` restores raw output. (PR #6)
- **Phase 8 — VAD filter** ✅ — `--vad/--no-vad` flag; faster-whisper built-in `vad_filter`; `None`-sentinel so unset falls through to config default (on). 76 tests.
- **Phase 9 — Compute type / quantization** ✅ — `--compute-type auto|float16|int8_float16|int8|float32`; configurable via `wisper config set compute_type`; shown in run header and `wisper config show`.

### pyannote 4.x upgrade

**Status: ✅ Complete (April 2026). Merged in PR #2.**

### ✅ Phase 7 — Docker Containerization

**Status: Complete.**

- `Dockerfile`: two targets — `gpu` (PyTorch cu126 wheels, `python:3.12-slim` base) and `cpu`. PyTorch CUDA wheels bundle the CUDA runtime; no NVIDIA base image required. pydub still needs system `ffmpeg`, installed via apt.
- `docker-compose.yml`: `wisper` (GPU, default) and `wisper-cpu` services. GPU passthrough via modern `deploy.resources.reservations.devices` syntax (NVIDIA Container Toolkit on host).
- `config.py`: `get_data_dir()` checks `WISPER_DATA_DIR` env var before `platformdirs`. Set to `/data` in the image; bind-mounted to `./data/` on host.
- Volume layout: `./cache` → HF model cache, `./data` → config + profiles, `./input` → audio, `./output` → transcripts.
- `.dockerignore` excludes `.venv`, tests, example-file, docs, and user data dirs.

**Verification:**
- [ ] `docker compose build` completes
- [ ] `docker compose run wisper wisper setup` — guided wizard works with TTY
- [ ] `docker compose run wisper wisper transcribe /app/input/test.mp3 --enroll-speakers` — enrollment works, profiles persist in `./data/`
- [ ] `docker compose run wisper wisper transcribe /app/input/test2.mp3` — speaker matching from persisted profiles
- [ ] `docker compose run wisper nvidia-smi` — GPU visible in container
- [ ] Container restart: no re-download of models

---

### ✅ Phase 8 — VAD Filter (from Whisper-WebUI review)

**Status: Complete.** Used faster-whisper's built-in `vad_filter=True` (Option A). Avoids timestamp remapping entirely — faster-whisper's Silero VAD integration keeps timestamps original-relative. `--vad/--no-vad` flag added to CLI; `vad_filter` in config.toml; `None`-sentinel in `process_file()` so unset flag falls through to config default.

---

### ✅ Phase 9 — Compute Type / Quantization Flag

**Status: Complete.** `--compute-type` flag added; `compute_type` in config.toml; `resolve_compute_type()` in `config.py`; shown in run header and `wisper config show`.

---

### Phase 10 — Parallel Folder Processing (CPU-only)

**Context:** GPU processing is always the bottleneck — faster-whisper and pyannote are not thread-safe when sharing a GPU, and loading duplicate model copies would exhaust VRAM. Parallelism only makes sense on CPU-only deployments (e.g. a Linux server processing a large queue of files overnight).

**What to build:** `--workers N` flag on `wisper transcribe <folder>`. Uses `concurrent.futures.ThreadPoolExecutor`. Each worker gets its own model instance (no sharing). Guard: if `device != "cpu"`, emit a warning and clamp workers to 1. Default workers=1 (current behavior unchanged for all GPU users).

**When to build:** Only if there's an actual CPU-server use case. Not worth building for the primary RTX 3090 / M5 Mac workflow.

---

### Phase 11 — Optional GUI

- **Optional GUI** — Textual (terminal) or tkinter/PyQt. Wraps the same `pipeline.process_file()` and `speaker_manager` calls. Keep CLI/library separation clean.

---

### Long-Term — Intel GPU Support

**Status:** Research complete (April 2026). Not actionable yet — blocked by upstream dependencies.

**The problem:** Our two core inference engines don't support Intel GPUs:
- **CTranslate2** (powers faster-whisper): NVIDIA CUDA only. Open issue [#1715](https://github.com/OpenNMT/CTranslate2/issues/1715), no work planned.
- **pyannote-audio**: No Intel XPU backend. No upstream interest.

PyTorch itself supports Intel Arc/Data Center GPUs via `torch.xpu` (production-ready since PyTorch 2.5), but that doesn't help when our deps use CUDA-specific code paths.

**Viable paths if this becomes a real need:**

1. **OpenVINO backend for transcription** — Intel's inference engine has official Whisper support (1.4-5x faster than PyTorch). Would require an abstraction layer in `transcriber.py` that dispatches to either faster-whisper (CUDA/CPU) or OpenVINO (Intel GPU/CPU) based on detected hardware. Model conversion step needed (Whisper → ONNX → OpenVINO IR). Static-shape constraint on GPU execution.

2. **whisper.cpp with SYCL** — C++ Whisper implementation with full Intel GPU acceleration via SYCL/oneAPI. Python bindings exist (`pywhispercpp`). Different integration surface from faster-whisper but avoids the model conversion step.

3. **Diarization alternatives** — If transcription moves to OpenVINO, diarization could either:
   - Convert pyannote models to OpenVINO (manual, static-shape constraints, fragile)
   - Switch to SpeechBrain ECAPA-TDNN for speaker embeddings (actually faster on CPU than pyannote on GPU — 6.7x speedup reported)
   - Wait for pyannote to add XPU support upstream

**Architecture note:** If we ever add a second backend, the right design is an abstract `TranscriptionBackend` interface in `transcriber.py` with `FasterWhisperBackend` and `OpenVINOBackend` implementations. Same for `DiarizationBackend` in `diarizer.py`. Keep the pipeline module backend-agnostic.

**When to revisit:** Check back when either (a) CTranslate2 adds Intel GPU support, (b) a user actually needs this, or (c) OpenVINO's Whisper API stabilizes enough to be a drop-in. Don't build speculatively.

---

## Verification Checklist

- [x] `pip install -e .` succeeds
- [x] `wisper transcribe single_speaker.mp3` → readable .md without speaker labels
- [x] `wisper transcribe multi_speaker.mp3 --num-speakers 4` → .md with SPEAKER_XX labels
- [x] `wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6` → interactive enrollment + .md with real names
- [x] `wisper transcribe session02.mp3 --num-speakers 6` → automatic speaker matching from profiles
- [x] `wisper speakers list` → shows enrolled profiles
- [x] `wisper fix session.md --speaker "Unknown Speaker 1" --name "Frank"` → updates transcript
- [x] `wisper transcribe ./recordings/` → batch processing with progress, skip existing, error recovery
- [x] `wisper setup` → guided first-run wizard
- [x] `wisper transcribe <file> --enroll-speakers --device cuda` on pyannote 4.0.4 → 11 speakers enrolled, full `.md` produced (4/6/2026)
- [ ] Parallel folder processing with `--workers N`
