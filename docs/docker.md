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

On first run the server downloads the Whisper and pyannote models (~2 GB) into `./cache/` — this only happens once. The GPU image also downloads the word alignment model (~1.7 GB) on its first diarized job.

> **Note on binding:** inside the container the web services run
> `wisper server --host 0.0.0.0 --port 8080` (set explicitly in
> `docker-compose.yml` — the CLI's own default is `127.0.0.1`, which would be
> unreachable from outside the container). Whether the UI is reachable from
> other machines is controlled by Docker's port mapping; the default
> `"8080:8080"` publishes on all host interfaces, so change it to
> `"127.0.0.1:8080:8080"` if the host is on an untrusted network — the UI
> has no authentication (see [web-ui.md](web-ui.md#trust-model)).

### Makefile Targets

| Command | Description |
|---------|-------------|
| `make start` | CPU web UI at `http://localhost:8080` |
| `make start-gpu` | GPU web UI |
| `make stop` | Stop all containers |
| `make logs` | Follow container logs |
| `make build` | (Re)build all images |
| `make build-cpu` / `make build-gpu` | Build one image |
| `make shell` | Shell in the CPU container |
| `make shell-gpu` | Shell in the GPU container |
| `make setup` | Local (non-Docker) setup |
| `make test` | Run the test suite |
| `make tailwind` | Rebuild `tailwind.min.css` |
| `make clean` | Remove caches and coverage output |

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
| `./data/` | `/data` | `config.toml`, `wisper.db` (profiles, campaigns, recordings, jobs, search index), voice samples, journals, `backups/` |
| `./input/` | `/app/input` | Your audio files |
| `./output/` | `/app/output` | Transcribed `.md` files |
| `./recordings/` | `/data/recordings` | Discord recording audio |

All directories are created automatically on first run and persist across container restarts. The compose file sets `WISPER_DATA_DIR=/data` and `WISPER_OUTPUT_DIR=/app/output`.

Back up the database with `docker compose run wisper-cpu wisper db backup` (written to `./data/backups/`).

### One way of running at a time

Use **either** Docker for everything (web UI and CLI both via `docker compose`) **or** the native CLI with a local `wisper server` — not a native CLI against `./data` while a container is running. On Docker Desktop (Mac and Windows), file locks don't cross into the container's VM, so a host process and a container writing `wisper.db` at the same time corrupts it. wisper records which side is using the database and refuses to start the other side with a message naming the running process; stop the container (or the host server) first. Several containers sharing `./data` (e.g. `wisper-cpu` CLI runs alongside `wisper-cpu-web`) are safe, and Docker on native Linux isn't affected.

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
3. Enter the Guild ID and Voice Channel ID (or pick a saved preset), choose a campaign, and click **Start Recording**
4. When the session ends, click **Stop** — the recording appears in **Recordings**
5. On the recording detail page, click **Transcribe** to queue it for processing

To find an ID, enable Developer Mode in Discord, then right-click the server or channel → *Copy ID*.

The bot joins per session (not always-on) and rejoins automatically after transient disconnects. In Docker, recordings are stored in `./recordings/`.

> **CLI equivalent:** `wisper record start --voice-channel <ID> --campaign <slug>` — see [cli-reference.md](cli-reference.md) for all `wisper record` subcommands.

### Known Limitations

See [scenarios.md](scenarios.md#known-limitations).
