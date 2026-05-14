# Docker & Discord Bot

## Docker

Run wisper entirely in a container — no Python environment setup, no CUDA DLL hunting.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine + Compose v2 (Linux)
- For GPU: NVIDIA driver on host (`nvidia-smi` must work) + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### Quick Start

```bash
# 1. Configure your tokens
cp .env.example .env
#    Open .env and set HF_TOKEN=hf_...  (and any LLM API keys you need)

# 2. Build and start (CPU — works everywhere)
make start
# → http://localhost:8080

# OR — GPU (NVIDIA only)
make start-gpu
```

On first run the server downloads the Whisper and pyannote models (~2 GB) into `./cache/` — this only happens once.

### Makefile Targets

| Command | Description |
|---------|-------------|
| `make start` | CPU web UI at `http://localhost:8080` |
| `make start-gpu` | GPU web UI |
| `make stop` | Stop all containers |
| `make logs` | Follow container logs |
| `make build` | (Re)build all images |
| `make shell` | Shell in the CPU container |
| `make shell-gpu` | Shell in the GPU container |
| `make setup` | Local (non-Docker) setup |
| `make test` | Run the test suite |

### CLI via Docker

```bash
# Place audio files in ./input/ first
docker compose run wisper-cpu wisper transcribe /app/input/session01.mp3 --enroll-speakers

# GPU variant
docker compose run wisper wisper transcribe /app/input/session01.mp3 --enroll-speakers
```

### Volume Layout

| Local path | Container path | Contents |
|-----------|---------------|----------|
| `./cache/` | `/root/.cache/huggingface` | Downloaded models (~2 GB, persisted) |
| `./data/` | `/data` | `config.toml` + speaker profiles |
| `./input/` | `/app/input` | Your audio files |
| `./output/` | `/app/output` | Transcribed `.md` files |

All directories are created automatically on first run and persist across container restarts.

### Verify GPU Passthrough

```bash
docker compose run wisper nvidia-smi
```

---

## Discord Recording Bot

Record Discord voice channel sessions directly from the web UI. The bot joins your server's voice channel, captures per-user audio, and hands the recording off to the transcription pipeline — no manual file shuffling.

### Prerequisites

1. **Create a Discord bot** at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Give it a name (e.g. "Wisper") and go to the **Bot** tab
3. Under **Privileged Gateway Intents**, enable **Server Members Intent** and **Message Content Intent**
4. Copy the bot token — set it as `DISCORD_BOT_TOKEN` in your `.env` file
5. **Invite the bot** to your server: go to **OAuth2 → URL Generator**, select `bot` + `applications.commands`, bot permissions: **View Channels**, **Connect**, **Speak**. Paste the generated URL in a browser.

**Additional requirement:** Java 25+ ([Adoptium](https://adoptium.net/) or `apt-get install openjdk-25-jre-headless`) — required for the JDA sidecar that handles Discord's DAVE E2EE voice protocol.

### Usage

1. Start the server: `make start` (Docker) or `wisper server` (local)
2. Open `http://localhost:8080/record`
3. Optional: expand **Browse bot's channels** to see all guilds and voice channels the bot can see — click any channel to auto-fill the Guild ID and Voice Channel ID fields
4. Select a campaign and voice channel, then click **Start Recording**
5. When the session ends, click **Stop** — the recording appears in **Recordings**
6. On the recording detail page, click **Transcribe** to queue it for processing

The bot joins per-session (not always-on) and auto-rejoins on transient disconnects. Recordings are stored at `./recordings/` (bind-mounted in Docker) alongside your other data.

> **CLI equivalent:** `wisper record start --voice-channel <ID> --campaign <slug>` — see [cli-reference.md](cli-reference.md) for all `wisper record` subcommands.

### Known Limitations

- **One active recording at a time.** Starting a second recording while one is active returns an error.
- **No multi-guild / multi-channel.** The bot connects to one voice channel in one guild per session.
- **DAVE E2EE voice receive depends on JDAVE (Java).** Discord's DAVE protocol encrypts per-user voice — only JDA+JDAVE has confirmed working decrypt as of 2026-05. When [Pycord PR #3159](https://github.com/Pycord-Development/pycord/pull/3159) ships DAVE support, the Java sidecar can be replaced with a ~100-line Python implementation.
