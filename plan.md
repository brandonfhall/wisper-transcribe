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
74 tests passing. All ML calls mocked. No GPU required for test suite. README with install, quick start, full CLI reference.

### ✅ pyannote-audio 4.x Upgrade (April 2026)
Upgraded from 3.4.0 → 4.0.4. Removed 5 compatibility shims (torchaudio stubs, hf_hub `use_auth_token`, torch.load default). speechbrain `LazyModule.ensure_module` patch retained — pyannote 4.x still uses speechbrain for ECAPA-TDNN embeddings and the Windows path bug is in speechbrain itself.

One additional fix required post-upgrade: pyannote 4.x wraps diarization output in a `DiarizeOutput` dataclass (`DiarizeOutput.speaker_diarization` is the `Annotation`), breaking the existing `diarization.itertracks()` call. Fixed in `diarizer.py` with a `hasattr` guard for backwards compatibility.

torchcodec still cannot find FFmpeg shared DLLs on this Windows install despite `Gyan.FFmpeg.Shared` being listed in `setup.ps1`. The scipy audio loading bypass (`scipy.io.wavfile` → waveform dict) remains as a workaround. Functionally equivalent; end-to-end test confirmed working (11 speakers enrolled, full `.md` output, CUDA device).

---

## Backlog

### Near-term (ready to build)

- **Parallel folder processing** — `concurrent.futures.ThreadPoolExecutor` for CPU-bound files. Caveat: pyannote and whisper are not thread-safe when sharing a GPU — needs per-worker model instances or CPU-only mode guard.

### ✅ Near-term completed

- **`wisper setup` command** ✅ — guided wizard: ffmpeg, HF token, model pre-download, device detection.
- **Progress header on each file** ✅ — Input/Output/Model line printed before each file processes.
- **Expose data paths in `wisper config show`** ✅ — config file, data dir, profiles dir, HF cache all shown.
- **`wisper config show` model clarity** ✅ — Models section: device, Whisper model, compute type (with auto-resolution), pyannote models.
- **Enrollment speaker order — chronological** ✅ — speakers sorted by first appearance timestamp in `pipeline.py`.
- **Audio playback during enrollment** ✅ — `--play-audio` flag; plays up to 10 s via pydub; silent fallback. (PR #3)
- **`wisper speakers reset`** ✅ — deletes all profiles and embeddings with confirmation prompt.
- **Phase 9 — Compute type / quantization** ✅ — `--compute-type auto|float16|int8_float16|int8|float32`; configurable via `wisper config set compute_type`; shown in run header and `wisper config show`.

### pyannote 4.x upgrade

**Status: ✅ Complete (April 2026). Merged in PR #2.**

### Phase 7 — Docker Containerization (from Whisper-WebUI review)

**Context:** [Whisper-WebUI](https://github.com/jhj0517/Whisper-WebUI) ships Docker support with CUDA GPU passthrough. Their stack is identical to ours (faster-whisper + pyannote 3.1) but they lack speaker enrollment entirely. Our speaker identification engine is a genuine differentiator. Containerization makes deployment reproducible and eliminates the CUDA DLL hunting that plagues Windows installs.

**What to build:**

1. **`Dockerfile`** — Two-target build:
   - **GPU target**: Base image `nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04`. Includes CUDA runtime + cuDNN. No need for `-devel` variant since we're doing inference only, not compiling CUDA code.
   - **CPU target**: Base image `python:3.12-slim`. For users without NVIDIA GPUs or for CI.
   - Install Python 3.12, pip deps from `pyproject.toml`. No system ffmpeg needed — faster-whisper bundles it via PyAV.
   - Expected image size: ~8-10GB (GPU), <1GB (CPU).

2. **`docker-compose.yml`** — GPU passthrough using modern compose syntax:
   ```yaml
   services:
     wisper:
       build: .
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: all
                 capabilities: [gpu]
       volumes:
         - ./models:/root/.cache/huggingface    # persist model downloads (~2GB)
         - ./profiles:/app/profiles             # speaker embeddings + metadata
         - ./input:/app/input                   # audio files to transcribe
         - ./output:/app/output                 # markdown transcripts
       stdin_open: true
       tty: true                                # required for interactive enrollment
   ```

3. **Volume mounts (critical)**:
   - **HuggingFace cache** (`~/.cache/huggingface`): Models are 2+ GB. Must persist across container restarts to avoid re-downloading.
   - **Speaker profiles** (`profiles/`): The `platformdirs` data dir must be overridden inside the container to a bind-mounted path. Without this, enrolled speaker embeddings are lost when the container stops.
   - **Input/output dirs**: User mounts their audio files in and gets transcripts out.

4. **`platformdirs` override**: Inside the container, set `WISPER_DATA_DIR` env var (new) to point to the mounted volume. `config.py` checks this env var before falling back to `platformdirs.user_data_dir()`. This is the only source code change needed.

5. **Host prerequisites** (document in README):
   - NVIDIA GPU with CUDA Compute Capability ≥ 3.5
   - NVIDIA driver installed on host (`nvidia-smi` must work)
   - [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed
   - Docker ≥ 19.03

**What NOT to do:**
- Don't add Gradio or any web UI. The CLI runs inside the container via `docker run -it`.
- Don't support macOS MPS in Docker. Mac users continue running bare-metal. Docker is for Linux server/cloud deployment with NVIDIA GPUs.
- Don't bake models into the image. They're 2+ GB and version-specific. Bind mount the cache.

**Performance notes:** GPU passthrough via NVIDIA Container Toolkit has negligible overhead vs bare-metal. The main cost is image size (~8GB for CUDA runtime layers), not runtime performance.

**Verification:**
- [ ] `docker compose build` completes
- [ ] `docker compose run wisper wisper setup` — guided wizard works with TTY
- [ ] `docker compose run wisper wisper transcribe /app/input/test.mp3 --enroll-speakers` — enrollment works, profiles persist in mounted volume
- [ ] `docker compose run wisper wisper transcribe /app/input/test2.mp3` — automatic speaker matching from previously enrolled profiles
- [ ] `nvidia-smi` visible inside container, transcription uses CUDA
- [ ] Container restart preserves models and profiles (no re-download)

---

### Phase 8 — Silero VAD Preprocessing (from Whisper-WebUI review)

**Context:** Whisper-WebUI runs [Silero VAD](https://github.com/snakers4/silero-vad) before transcription to strip silence and non-speech segments. This is especially valuable for tabletop RPG sessions which have long pauses (thinking, dice rolling, snack breaks, cross-talk). Removing these segments before Whisper processes them improves both speed and accuracy.

**What to build:**

1. **New module: `src/wisper_transcribe/vad.py`**
   - `strip_silence(wav_path: Path, aggressiveness: int = 3) -> Path`
   - Loads Silero VAD model (lightweight PyTorch, ~1MB, no HF token needed)
   - Runs VAD over the 16kHz mono WAV
   - Returns a new WAV with non-speech segments removed (or marked)
   - Must preserve original timestamps for alignment — store a time-mapping so diarization segments still map to the original audio timeline
   - Cache the Silero model at module level (same pattern as `transcriber._model`)

2. **Pipeline integration** — Insert between steps 2 (PREPROCESS) and 3 (TRANSCRIBE):
   ```
   1. VALIDATE
   2. PREPROCESS (convert to WAV)
   3. VAD (strip silence)     ← NEW
   4. TRANSCRIBE
   5. DIARIZE
   6. ALIGN
   7. IDENTIFY
   8. FORMAT
   ```

3. **CLI flag**: `--vad / --no-vad` (default: on). Add to `wisper transcribe`.

4. **Timestamp remapping**: This is the tricky part. If VAD removes a 30-second silence gap, all subsequent timestamps shift. Two approaches:
   - **Option A (simpler)**: Run VAD only to *inform* Whisper's `vad_filter` parameter (faster-whisper already has built-in VAD support via `vad_filter=True`). This avoids timestamp remapping entirely. Start here.
   - **Option B (full)**: Actually strip audio, then remap timestamps post-transcription. More work, better results. Do this only if Option A's quality isn't sufficient.

5. **Tests**: `tests/test_vad.py` — mock Silero model, verify silence stripping logic and timestamp preservation.

**Recommendation:** Start with Option A — faster-whisper's built-in `vad_filter=True` parameter. This is literally a one-line change in `transcriber.py` and gets 80% of the benefit. Only build the full Silero pipeline if the built-in filter isn't aggressive enough for RPG session audio.

**Verification:**
- [ ] `wisper transcribe session.mp3 --vad` — faster transcription than `--no-vad` on same file
- [ ] Timestamps in output still align with original audio (spot-check a few)
- [ ] No accuracy regression on clean speech segments

---

### ✅ Phase 9 — Compute Type / Quantization Flag

**Status: Complete.** `--compute-type` flag added; `compute_type` in config.toml; `resolve_compute_type()` in `config.py`; shown in run header and `wisper config show`.

---

### Phase 10 — Optional GUI

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
