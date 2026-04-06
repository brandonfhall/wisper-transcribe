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
  --no-diarize
  --timestamps / --no-timestamps
  --device cpu|cuda|auto
  --overwrite
  --verbose

wisper enroll <name> --audio <file>
  --segment START-END
  --notes TEXT
  --update                        # EMA blend with existing embedding

wisper speakers list|remove|rename|test

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
64 tests passing. All ML calls mocked. No GPU required for test suite. README with install, quick start, full CLI reference.

---

## Backlog

### Near-term (ready to build)

- **`wisper setup` command** — walk through ffmpeg check, HF token prompt, model pre-download, CUDA detection. Better first-run experience than hitting errors mid-transcription.

- **Parallel folder processing** — `concurrent.futures.ThreadPoolExecutor` for CPU-bound files. Caveat: pyannote and whisper are not thread-safe when sharing a GPU — needs per-worker model instances or CPU-only mode guard.

- **Progress header on each file** — show Input path, Output path, active model/device, and tqdm bar in one clean block before processing starts. *(In progress)*

- **Expose data paths in `wisper config show`** — print config dir, profiles dir, and HF cache dir so users know where things live without digging.

- **`wisper config show` model clarity** — surface which Whisper model and pyannote models are active, not just config key/value pairs.

### Future / Phase 6

- **Optional GUI** — Textual (terminal) or tkinter/PyQt. Wraps the same `pipeline.process_file()` and `speaker_manager` calls. Keep CLI/library separation clean.

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
- [ ] `wisper setup` → guided first-run wizard
- [ ] Parallel folder processing with `--workers N`
