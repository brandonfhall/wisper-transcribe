# Setup & Installation

## Getting Started

Pick the path that fits you. All three end up at the same web UI on `http://localhost:8080`.

### Option A — Double-click launcher *(recommended for most users)*

**Requirements:** Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) installed.

| Platform | Steps |
|----------|-------|
| **macOS** | Double-click `start.command` in Finder. First run sets everything up automatically. |
| **Windows** | Double-click `start.bat`. First run sets everything up automatically. |
| **Linux** | Run `bash start.sh` in a terminal. |

The first run takes 5–10 minutes (creates a virtualenv and installs ~2 GB of ML models). Subsequent launches are instant.

After the server starts, your browser opens automatically to `http://localhost:8080`. Press `Ctrl+C` in the terminal to stop.

### Option B — Docker *(server / shared use)*

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine (Linux).

```bash
# 1. Copy the env template and fill in your HuggingFace token
cp .env.example .env
#    → open .env in a text editor and set HF_TOKEN=hf_...

# 2. Start the web UI (CPU — works on any machine)
make start

# 3. Open http://localhost:8080
```

For GPU acceleration (NVIDIA only):
```bash
make start-gpu
```

See [docker.md](docker.md) for the full volume layout, Makefile targets, and Discord bot setup.

### Option C — Developer / CLI

```bash
# 1. Run the setup script (creates .venv, installs deps, CUDA PyTorch on Windows)
bash setup.sh      # Mac/Linux
.\setup.ps1        # Windows PowerShell

# 2. First-time wizard (HF token + model download)
.venv/bin/wisper setup        # Mac/Linux
.venv\Scripts\wisper setup    # Windows

# 3. Transcribe
.venv/bin/wisper transcribe session01.mp3 --enroll-speakers

# 4. Or start the web UI
.venv/bin/wisper server
```

---

## First-time Setup (HuggingFace Token)

Speaker diarization (identifying who is speaking) requires a **free** HuggingFace token. You only need to do this once.

1. Create a free account at [huggingface.co](https://huggingface.co) and generate a token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) with **"Read access to contents of all repos under your personal namespace"**.

2. Accept the model license agreements (free, one-time):
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [pyannote/embedding](https://huggingface.co/pyannote/embedding)

3. Enter the token when prompted by `wisper setup`, or set it via the web UI Config page, or via env var:

```bash
# Docker / .env file
HF_TOKEN=hf_abc123...

# Local env var
export HF_TOKEN=hf_abc123...          # Mac/Linux
$env:HF_TOKEN="hf_abc123..."          # Windows PowerShell

# Or store it permanently
wisper config set hf_token hf_abc123...
```

> **Note:** `pyannote/segmentation-3.0` is downloaded automatically as a sub-dependency — you do not need to accept it separately.

**Optional — configure an LLM for `refine` / `summarize`:**

```bash
wisper config llm
```

Walks you through provider (Ollama / LM Studio / Anthropic / OpenAI / Google), model, and API key or endpoint. Skip this if you're not planning to use the LLM post-processing commands.

> The `setup.sh` / `setup.ps1` scripts auto-detect a running Ollama (`localhost:11434`) or LM Studio (`localhost:1234`) instance during first-run setup and offer to pick a model right there — so if either is already running locally, you don't need to run `wisper config llm` separately.

---

## Requirements

- Python 3.10+ (for Option A/C)
- [ffmpeg](https://ffmpeg.org/download.html) on your PATH
- A free [HuggingFace token](https://huggingface.co/settings/tokens)
- GPU recommended but not required (CPU works, just slower)
- **Discord recording bot:** Java 25+ ([Adoptium](https://adoptium.net/) or `apt-get install openjdk-25-jre-headless`)

**Windows CUDA:**
- Install ffmpeg via `winget install Gyan.FFmpeg.Shared`
- `setup.ps1` auto-installs the CUDA 12.6 PyTorch wheels
- *If you see `cublas64_12.dll` / `zlibwapi.dll` errors: place NVIDIA cuDNN DLLs in your CUDA `bin` dir*

**Mac:** `brew install ffmpeg`

---

## Manual Installation (Developer)

```bash
git clone <repo>
cd wisper-transcribe
python -m venv .venv
source .venv/bin/activate       # Mac/Linux
# .venv\Scripts\activate        # Windows
pip install -e .
```

**Optional cloud-LLM extras** (Ollama works out of the box — only needed for cloud providers):

```bash
pip install -e '.[llm-anthropic]'   # Anthropic (Claude)
pip install -e '.[llm-openai]'      # OpenAI (GPT)
pip install -e '.[llm-google]'      # Google (Gemini)
pip install -e '.[llm-all]'         # all three
```

> **Windows CUDA:** `pip install` gives CPU-only PyTorch by default. After setup, run:
> ```powershell
> pip install "torch>=2.8.0" "torchaudio>=2.8.0" --index-url https://download.pytorch.org/whl/cu126 --force-reinstall
> ```
> `setup.ps1` handles this automatically.

---

## Model Size Guide

| Model | Speed | Accuracy | VRAM |
|-------|-------|----------|------|
| `tiny` | Fastest | Lower | ~1 GB |
| `base` | Fast | Decent | ~1 GB |
| `small` | Moderate | Good | ~2 GB |
| `medium` | Moderate | Very good | ~5 GB |
| `large-v3-turbo` | Fast | Near-best | ~4 GB |
| `large-v3` | Slow | Best | ~10 GB |

**Recommended:**
- RTX 3090 (24 GB): `large-v3-turbo --device cuda` (best speed/accuracy tradeoff)
- Apple M-series: `medium` (auto-detects MPS; diarization runs on GPU, transcription on CPU)
- CPU-only machine: `small` or `base`

---

## Running Tests

```bash
.venv/bin/pytest tests/ -v        # Mac/Linux
.venv\Scripts\pytest tests/ -v    # Windows
```

Tests mock all ML models — no GPU, network, or real audio files required.

CI runs the test suite across Python 3.10–3.14 on every push and PR. Python 3.14 is treated as experimental (non-blocking).
