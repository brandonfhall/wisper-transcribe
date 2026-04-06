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

### Quick setup (recommended)

Run the setup script — it handles the venv, package install, and CUDA PyTorch in one step:

```powershell
# Windows
.\setup.ps1
```

```bash
# Mac/Linux
bash setup.sh
```

### Manual setup

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

> **Windows CUDA users:** `pip install` from PyPI installs the CPU-only PyTorch build by default. After the steps above, run this extra command to get GPU support:
> ```powershell
> pip install "torch>=2.8.0" "torchaudio>=2.8.0" --index-url https://download.pytorch.org/whl/cu126 --force-reinstall
> ```
> `pyannote-audio 4.x` requires `torch>=2.8.0`, which lives on the CUDA 12.6 index (`cu126`). The `cu124` index only goes up to 2.6.0 and will cause a dependency conflict.
>
> Verify it worked: `python -c "import torch; print(torch.cuda.is_available())"` should print `True`.
> The setup script (`setup.ps1`) handles this automatically.

### One-time setup

Run the setup wizard — it checks ffmpeg, prompts for your HuggingFace token, and pre-downloads all models so your first transcription run starts immediately:

```bash
wisper setup
```

*Note: When creating your HuggingFace token, ensure it has **"Read access to contents of all repos under your personal namespace"**.*

You must also accept the model license agreements on HuggingFace (free, one-time — links shown by `wisper setup`):
- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [pyannote/embedding](https://huggingface.co/pyannote/embedding)
- [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
- [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [pyannote/speaker-diarization-community-1 PLDA transform](https://huggingface.co/pyannote/speaker-diarization-community-1/resolve/main/plda/xvec_transform.npz.bz2)

Alternatively, set the token manually or via environment variable:

```bash
wisper config set hf_token hf_abc123...

export HUGGINGFACE_TOKEN=hf_abc123...   # Mac/Linux
$env:HUGGINGFACE_TOKEN="hf_abc123..."  # Windows PowerShell
```



---

## Quick Start

### First session — enroll your players

Run this the first time to name the speakers interactively:

```bash
wisper transcribe session01.mp3 --enroll-speakers --num-speakers 6
```

wisper will transcribe, detect speakers, then prompt you for each one:

```
────────────────────────────────────────────────────────────
  Input  : session01.mp3
  Output : session01.md
  Model  : medium (cuda, float16)
────────────────────────────────────────────────────────────
  Transcribing: 100%|████████| 4823/4823s

  Found 6 speaker(s). Let's name them.

  Speaker 1 of 6 (heard at 00:00:12):
    "Welcome back everyone. Last session you had just entered the ruins..."
  Who is this? Alice
  Role (DM/Player/Guest, optional): DM
  Notes (optional):

  Speaker 2 of 6 (heard at 00:00:18):
    "Right, I want to check for traps before we go further in."
  Who is this? Bob
  Role (DM/Player/Guest, optional): Player
  ...

  Enrolled 6 speakers.
  Wrote session01.md
```

### All future sessions — fully automatic

```bash
wisper transcribe session02.mp3 --num-speakers 6
```

```
────────────────────────────────────────────────────────────
  Input  : session02.mp3
  Output : session02.md
  Model  : medium (cuda, float16)
────────────────────────────────────────────────────────────
  Transcribing: 100%|████████| 4901/4901s
  Speaker matches:
    SPEAKER_00 → Alice
    SPEAKER_01 → Bob
    SPEAKER_02 → Charlie
  Wrote session02.md
```

### Process a whole folder at once

```bash
wisper transcribe ./recordings/ --num-speakers 6
```

```
Processing folder: recordings/
Folder Progress:  75%|████████        | 9/12 [14:23<04:51]
Processing session10.mp3

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

### `wisper setup`

Guided first-run wizard. Run this once after installation:

```bash
wisper setup
```

Checks ffmpeg, detects your GPU (CUDA/MPS/CPU), prompts for your HuggingFace token, and pre-downloads all pyannote models (~700 MB, cached permanently).

### `wisper transcribe`

```
wisper transcribe <path>

  path                     Audio file or folder of audio files

  -o, --output DIR         Output directory (default: same as input)
  -m, --model SIZE         tiny / base / small / medium / large-v3
                           (default: medium; use large-v3 on a good GPU)
  -l, --language LANG      Language code, e.g. en, fr, de (default: en)
                           Use 'auto' to detect automatically
  --device auto|cpu|cuda|mps  Compute device (default: auto-detect; mps = Apple Silicon GPU)
  -n, --num-speakers INT   Expected speaker count — improves accuracy
  --min-speakers INT       Minimum speaker count
  --max-speakers INT       Maximum speaker count
  --enroll-speakers        Interactively name speakers (use on first run)
  --play-audio             Play each speaker's sample clip during enrollment
  --no-diarize             Skip speaker detection (single-speaker output)
  --timestamps             Include timestamps (default: on)
  --no-timestamps          Omit timestamps
  --compute-type TYPE      CTranslate2 dtype: auto|float16|int8_float16|int8|float32
                           (default: auto → float16 on CUDA, int8 on CPU)
  --vad / --no-vad         Voice activity detection — skips silence before transcription
                           (default: on; improves speed and accuracy on audio with pauses)
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
wisper speakers reset                   # delete ALL profiles and embeddings (with confirmation)
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
- Apple M-series: `medium` (auto-detects MPS; diarization runs on GPU, transcription on CPU)
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

Tests mock all ML models — no GPU, network, or real audio files required. (77 tests)

---

## Docker

Run wisper entirely in a container — no Python environment setup, no CUDA DLL hunting.

### Prerequisites

- Docker ≥ 19.03
- For GPU: NVIDIA driver installed on host (`nvidia-smi` must work) + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### Quick start

```bash
# Build the GPU image (~8 GB — includes PyTorch CUDA wheels)
docker compose build

# First-time setup (token + model download — takes a few minutes)
docker compose run wisper wisper setup

# Transcribe with speaker enrollment
# Place audio files in ./input/ first
docker compose run wisper wisper transcribe /app/input/session01.mp3 --enroll-speakers

# Subsequent sessions — automatic speaker matching
docker compose run wisper wisper transcribe /app/input/session02.mp3
# Output appears in ./output/
```

### CPU-only

```bash
docker compose build wisper-cpu
docker compose run wisper-cpu wisper transcribe /app/input/session.mp3
```

### Volume layout

| Local path | Container path | Contents |
|-----------|---------------|----------|
| `./cache/` | `/root/.cache/huggingface` | Downloaded models (~2 GB, persisted) |
| `./data/` | `/data` | `config.toml` + speaker profiles |
| `./input/` | `/app/input` | Your audio files |
| `./output/` | `/app/output` | Transcribed `.md` files |

These directories are created automatically on first run. Speaker profiles and model downloads persist across container restarts.

### Verify GPU passthrough

```bash
docker compose run wisper nvidia-smi
```

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `WISPER_DATA_DIR` | Override config/profile storage path — used automatically in Docker |
| `WISPER_DEBUG` | Set to `1` to disable warning suppression and see raw dependency output |
| `HUGGINGFACE_TOKEN` | HF token as an alternative to `wisper config set hf_token` |

## Debugging / Verbose Warning Output

wisper suppresses informational warnings from its dependencies (speechbrain, pyannote, torch) that are not actionable during normal use. If you need to see the raw output for debugging, set `WISPER_DEBUG=1` before running:

```powershell
# Windows PowerShell
$env:WISPER_DEBUG="1"
wisper transcribe session.mp3
```

```bash
# Mac/Linux
WISPER_DEBUG=1 wisper transcribe session.mp3
```

Unset it (or open a new terminal) to return to clean output.

---

## Roadmap

- [x] Phase 1: Basic transcription
- [x] Phase 2: Speaker diarization
- [x] Phase 3: Speaker profiles + cross-file voice matching
- [x] Phase 4: Batch processing + CLI polish
- [x] Phase 5: Tests (77 passing), coverage reporting, README, setup scripts, CI
- [x] Phase 6: `wisper setup` guided first-run wizard
- [x] Phase 7: Docker containerization (GPU + CPU targets, `WISPER_DATA_DIR` override)
- [x] Phase 8: VAD filter (`--vad/--no-vad`) via faster-whisper built-in Silero VAD
- [x] Phase 9: Compute type / quantization (`--compute-type`)
- [ ] Phase 10: Parallel folder processing (`--workers N`, CPU-only)
- [ ] Phase 11: Optional GUI (Textual or tkinter)
