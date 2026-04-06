# wisper-transcribe

Local podcast transcription with automatic speaker identification. Built for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers, but works for any multi-speaker audio.

Runs entirely offline. No cloud APIs. Outputs clean markdown files ready for NotebookLM or any text search tool.

---

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) installed and on your PATH
- A free [HuggingFace token](https://huggingface.co/settings/tokens) (for speaker diarization)
- GPU recommended but not required (CPU works, just slower)

**Windows (CUDA):** 
- Install ffmpeg via `winget install Gyan.FFmpeg`
- Install CUDA Toolkit via `winget install Nvidia.CUDA` (Restart your terminal/VS Code after installing)
- *Note: If you encounter `cublas64_12.dll` or `zlibwapi.dll` not found errors, manually download NVIDIA cuDNN and place its `.dll` files in your CUDA `bin` directory (usually `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\bin`).*

**Mac:** Install ffmpeg via `brew install ffmpeg`

---

## Installation

```bash
git clone <repo>
cd wisper-transcribe

python -m venv .venv

# Windows
.venv\Scripts\activate

# Mac/Linux
source .venv/bin/activate

pip install -e .
```

### One-time setup

Store your HuggingFace token (required for speaker detection).

*Note: When creating the token, ensure it has the permission: **"Read access to contents of all repos under your personal namespace"**.*

```bash
wisper config set hf_token hf_abc123...
```

Or set it as an environment variable:

```bash
export HUGGINGFACE_TOKEN=hf_abc123...   # Mac/Linux
$env:HUGGINGFACE_TOKEN="hf_abc123..."  # Windows PowerShell
```

You must also accept the model license agreements on HuggingFace (one-time, free):
- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [pyannote/embedding](https://huggingface.co/pyannote/embedding)
- [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)


---

## Quick Start

### First session — enroll your players

Run this the first time to name the speakers interactively:

```bash
wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6
```

wisper will transcribe, detect speakers, then prompt you for each one:

```
Speaker 1 of 6 (heard at 00:00:12):
  "Welcome back everyone. Last session you had just entered the ruins..."
Who is this? > Alice
Role (DM/Player/Guest, optional)? > DM
Notes (optional)? > Game Master

Speaker 2 of 6 (heard at 00:00:18):
  "Right, I want to check for traps before we go further in."
Who is this? > Bob
Role? > Player
...

✓ Enrolled 6 speakers
✓ Wrote session01.md
```

### All future sessions — fully automatic

```bash
wisper transcribe session02.mp3 --num-speakers 6
```

```
Transcribing... done
Diarizing... done
Speaker matches:
  SPEAKER_00 → Alice  (0.91)
  SPEAKER_01 → Bob    (0.84)
  SPEAKER_02 → Charlie (0.78)
✓ Wrote session02.md
```

### Process a whole folder at once

```bash
wisper transcribe ./recordings/ --num-speakers 6
```

```
Processing 12 files...
[1/12] session01.mp3 → skipped (already exists)
[2/12] session02.mp3 → session02.md ✓
[3/12] session03.mp3 → session03.md ✓
...
Done. 11 transcribed, 1 skipped, 0 errors.
```

---

## Output Format

Each audio file produces a `.md` file in the same directory (or `--output` dir):

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

**Alice** *(00:00:12)*: Welcome back everyone. Last session you had just entered
the ruins of Khar'zul.

**Bob** *(00:00:18)*: Right, I want to check for traps before we go further in.

**Alice** *(00:00:23)*: Go ahead and roll a perception check.
```

The YAML frontmatter makes these files easy to ingest into NotebookLM or query with scripts.

---

## All Commands

### `wisper transcribe`

```
wisper transcribe <path>

  path                     Audio file or folder of audio files

  -o, --output DIR         Output directory (default: same as input)
  -m, --model SIZE         tiny / base / small / medium / large-v3
                           (default: medium; use large-v3 on a good GPU)
  -l, --language LANG      Language code, e.g. en, fr, de (default: en)
                           Use 'auto' to detect automatically
  --device auto|cpu|cuda   Compute device (default: auto-detect)
  -n, --num-speakers INT   Expected speaker count — improves accuracy
  --min-speakers INT       Minimum speaker count
  --max-speakers INT       Maximum speaker count
  --enroll-speakers        Interactively name speakers (use on first run)
  --no-diarize             Skip speaker detection (single-speaker output)
  --timestamps             Include timestamps (default: on)
  --no-timestamps          Omit timestamps
  --overwrite              Re-process files that already have output
  --verbose                Show detailed progress
```

### `wisper enroll`

Add a speaker from a clean reference clip (e.g. an interview or isolated recording):

```bash
wisper enroll "Alice" --audio alice_intro.mp3
wisper enroll "Alice" --audio session01.mp3 --segment "0:30-1:15"
wisper enroll "Alice" --audio session08.mp3 --update   # blend with existing profile
```

### `wisper speakers`

```bash
wisper speakers list                    # show all enrolled profiles
wisper speakers remove "Alice"          # delete a profile
wisper speakers rename "Alice" "Alicia" # rename a profile
wisper speakers test session03.mp3      # preview match results without writing output
```

### `wisper fix`

Fix a wrong speaker assignment in an existing transcript:

```bash
wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank"
wisper fix session03.md --speaker "Alice" --name "Diana"
```

Add `--re-enroll` to also update the voice profile (currently prompts manual steps).

### `wisper config`

```bash
wisper config show                        # print all settings
wisper config set model large-v3          # use the big model by default
wisper config set hf_token hf_abc123...   # store HuggingFace token
wisper config set similarity_threshold 0.70  # stricter speaker matching
wisper config path                        # show where config.toml lives
```

---

## Common Scenarios

### New player joins mid-campaign

They'll appear as `Unknown Speaker N` in the output. Fix and enroll them:

```bash
wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank"
wisper enroll "Frank" --audio session05.mp3 --segment "5:00-6:30"
```

Future sessions will recognize Frank automatically.

### Speaker sounds different (sick, new mic, remote)

Re-enroll with recent audio to blend it into their profile:

```bash
wisper enroll "Alice" --audio session08.mp3 --update
```

The `--update` flag averages the new sample with the existing profile using an exponential moving average, making recognition more robust over time.

### Player absent from a session

No problem — their profile is simply ignored for that file. Unused profiles never cause errors.

### Wrong automatic match

```bash
wisper fix session03.md --speaker "Alice" --name "Diana"
```

---

## Supported Audio Formats

`.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`, `.mp4`

All formats are automatically converted to 16kHz mono WAV internally before processing. Your original files are never modified.

---

## Model Size Guide

| Model | Speed | Accuracy | VRAM |
|-------|-------|----------|------|
| `tiny` | Fastest | Lower | ~1 GB |
| `base` | Fast | Decent | ~1 GB |
| `small` | Moderate | Good | ~2 GB |
| `medium` | Moderate | Very good | ~5 GB |
| `large-v3` | Slow | Best | ~10 GB |

**Recommended:**
- RTX 3090 (24 GB): `large-v3 --device cuda`
- Apple M5: `medium --device cpu`
- CPU-only machine: `small` or `base`

---

## Where Data Is Stored

Speaker profiles and config are stored in your OS user data directory — separate from the project folder so they persist across updates.

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\wisper-transcribe\` |
| Mac | `~/Library/Application Support/wisper-transcribe/` |

```
wisper-transcribe/
├── config.toml          settings
└── profiles/
    ├── speakers.json    speaker registry
    └── embeddings/
        ├── alice.npy    voice fingerprint
        └── bob.npy
```

---

## Running Tests

```bash
.venv/Scripts/pytest tests/ -v    # Windows
.venv/bin/pytest tests/ -v        # Mac/Linux
```

Tests mock all ML models — no GPU, network, or real audio files required.

---

## Roadmap

- [x] Phase 1: Basic transcription
- [x] Phase 2: Speaker diarization
- [x] Phase 3: Speaker profiles + cross-file voice matching
- [x] Phase 4: Batch processing + CLI polish
- [ ] Phase 5: README + test coverage report
- [ ] Phase 6: Optional GUI (Textual or tkinter)
