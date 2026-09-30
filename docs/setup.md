# Setup & Installation

## Getting Started

Pick the path that fits you. All three end up at the same web UI on `http://localhost:8080`.

### Option A — Double-click launcher *(recommended for most users)*

**Requirements:** Python 3.13+ and [ffmpeg](https://ffmpeg.org/download.html) installed.

| Platform | Steps |
|----------|-------|
| **macOS** | Double-click `start.command` in Finder. First run sets everything up automatically. |
| **Windows** | Double-click `start.bat`. First run sets everything up automatically. |
| **Linux** | Run `bash start.sh` in a terminal. |

The first run takes 5–10 minutes (creates a virtualenv and installs ~2 GB of ML models). Subsequent launches are instant.

**Updating:** after `git pull` (or unpacking a new release over the old folder), just launch as usual. When `pyproject.toml` has changed since the last launch, the launcher reinstalls dependencies once before starting. If that fails (e.g. offline), it starts anyway and retries next launch; running `setup.sh` / `setup.ps1` also works. CLI-only installs: `pip install -e .` after pulling. The first start after upgrading from a version without `wisper.db` moves your profiles, campaigns, and recordings into the database, keeping copies in `backups/` (see [Database and backups](configuration.md#database-and-backups)).

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
   - [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) — covers both diarization and voice profiles

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

> **Upgrading from an older version?** Diarization and voice profiles now use `pyannote/speaker-diarization-community-1` (previously `speaker-diarization-3.1` + `pyannote/embedding`). Accept its terms at the link above once, or diarization fails with a message pointing there. Existing voice profiles must be re-enrolled — see [Re-enrolling after the speaker-model upgrade](scenarios.md#re-enrolling-after-the-speaker-model-upgrade).

**Optional — configure an LLM for `refine` / `summarize`:**

```bash
wisper config llm
```

Walks you through provider (Ollama / Ollama Cloud / LM Studio / Anthropic / OpenAI / Google), model, and API key or endpoint. Skip this if you're not planning to use the LLM post-processing commands.

> `setup.sh` / `setup.ps1` detect a running Ollama (`localhost:11434`) or LM Studio (`localhost:1234`) and offer a model picker, so you can skip `wisper config llm` if either is already running.

---

## Requirements

- Python 3.13+ (for Option A/C), whose bundled SQLite is 3.43 or newer with FTS5 full-text search. The python.org installers for macOS and Windows, Homebrew, and the Docker image all qualify. An older Linux system Python (for example Ubuntu 22.04's SQLite 3.37) doesn't: wisper stops at startup and says what's missing. Use Docker, or a newer Python.
- [ffmpeg](https://ffmpeg.org/download.html) on your PATH
- A free [HuggingFace token](https://huggingface.co/settings/tokens)
- GPU recommended but not required (CPU works, just slower). On a GPU (CUDA or Apple Silicon), forced word alignment is on by default and downloads a ~1.7 GB model on first use.
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

# NVIDIA GPU: install CUDA PyTorch FIRST
pip install "torch>=2.8.0" "torchaudio>=2.8.0" --index-url https://download.pytorch.org/whl/cu126

pip install -e .
```

Install CUDA PyTorch before the project; otherwise pip pulls CPU-only PyTorch from PyPI and other packages bind to it. `setup.ps1` does this for you.

**Optional cloud-LLM extras** (Ollama works out of the box — only needed for cloud providers):

```bash
pip install -e '.[llm-anthropic]'   # Anthropic (Claude)
pip install -e '.[llm-openai]'      # OpenAI (GPT)
pip install -e '.[llm-google]'      # Google (Gemini)
pip install -e '.[llm-all]'         # all three
```

**Optional local-recording extra** (mic + system-audio capture, native install only — see below):

```bash
pip install -e '.[live]'
```

**Optional Apple Silicon extra** (MLX Whisper — GPU transcription on M-series Macs):

```bash
pip install -e '.[macos]'
```

---

## Local Recording (mic + system audio)

Records your microphone and the machine's system audio (the other side of a call, a video, a game) on the machine running `wisper server`, without a Discord bot. Requires the `[live]` extra, which installs [`soundcard`](https://github.com/bastibe/SoundCard).

**Native install only** — a Docker container can't reach host audio devices. The Record page hides the Local capture card when `soundcard` or devices are unavailable.

System audio is captured through a loopback device, the same way [OBS Studio](https://obsproject.com/)'s Desktop Audio works:

| Platform | Setup |
|----------|-------|
| **Windows** | Nothing extra needed — WASAPI loopback devices show up automatically in the system-audio dropdown. |
| **Linux** | Nothing extra needed on PulseAudio/PipeWire — the monitor source for your output device shows up automatically. |
| **macOS** | macOS has no built-in loopback capture. Install [BlackHole](https://github.com/ExistentialAudio/BlackHole) (free, 2ch is enough), then create a **Multi-Output Device** in Audio MIDI Setup routing your normal output *and* BlackHole together, and set that Multi-Output Device as your system output. BlackHole then appears as an ordinary input device in the system-audio dropdown. |

Local recordings appear on the Recordings page with a **LOCAL** badge and transcribe like any other. See [web-ui.md](web-ui.md#local-mic--system-audio) for using the Record page.

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
- Apple M-series: `large-v3-turbo` with the `[macos]` extra (MLX transcribes on the GPU); without it transcription runs on CPU, so use `small` or `medium`
- CPU-only machine: `small` or `base`

---

## Running Tests

```bash
.venv/bin/pytest tests/ -v        # Mac/Linux
.venv\Scripts\pytest tests/ -v    # Windows
```

Tests mock all ML models — no GPU, network, or real audio needed. CI runs them on Python 3.13 and 3.14.
