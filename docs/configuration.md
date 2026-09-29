# Configuration Reference

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `HF_TOKEN` | HuggingFace token — preferred name (used by Docker `.env` and all HF libraries) |
| `HUGGINGFACE_TOKEN` | Alias for `HF_TOKEN`; both are accepted and propagated to each other |
| `WISPER_DATA_DIR` | Override config/profile storage path — set automatically in Docker |
| `WISPER_DEBUG` | Set to `1` to disable warning suppression and see raw dependency output |
| `DISCORD_BOT_TOKEN` | Discord bot token for the recording bot (see [docker.md](docker.md)) |
| `WISPER_SIDECAR_JAR` | Absolute path to the JDA sidecar fat JAR (`discord-bot-all.jar`). Overrides the default search path. Useful when the JAR is not in the standard repo or Docker location. |
| `ANTHROPIC_API_KEY` | Anthropic API key for `refine` / `summarize` — takes precedence over stored config |
| `OPENAI_API_KEY` | OpenAI API key — takes precedence over stored config |
| `GOOGLE_API_KEY` | Google (Gemini) API key — takes precedence over stored config |
| `OLLAMA_API_KEY` | Ollama Cloud API key (for `llm_provider = ollama-cloud`) — takes precedence over stored config |
| `WISPER_SERVER_URL` | Server URL used by `wisper record` commands (e.g. `http://192.168.1.10:8080`). Overrides the `server.json` that a running `wisper server` writes to the data dir — set it when the CLI and server are on different machines or containers. |

---

## Config Keys

Stored in `config.toml`. View with `wisper config show`, change with `wisper config set <key> <value>` (or `wisper config llm` / `wisper config discord` for the guided wizards).

| Key | Default | Purpose |
|-----|---------|---------|
| `model` | `large-v3-turbo` | Whisper model size |
| `language` | `en` | Transcription language (`auto` to detect) |
| `device` | `auto` | `auto`, `cpu`, `cuda`, or `mps` |
| `compute_type` | `auto` | CTranslate2 precision (`auto` picks per device) |
| `vad_filter` | `true` | Skip silence before transcription (`--vad/--no-vad` overrides) |
| `timestamps` | `true` | Include timestamps in transcript output |
| `similarity_threshold` | `0.65` | Minimum voice-embedding similarity to match an enrolled speaker |
| `min_speakers` / `max_speakers` | `2` / `8` | Diarizer range when no speaker count is given |
| `hf_token` | — | HuggingFace token (env `HF_TOKEN` takes precedence) |
| `hotwords` | `[]` | Custom vocabulary (names, places) fed to Whisper as a prompt |
| `use_mlx` | `auto` | Apple Silicon: `auto` uses MLX Whisper when installed, `true` requires it, `false` always uses faster-whisper |
| `parallel_stages` | `false` | Run transcription and diarization concurrently in two subprocesses. Uses more memory; benchmark before enabling. |
| `llm_provider` | `ollama` | `ollama`, `ollama-cloud`, `lmstudio`, `anthropic`, `openai`, or `google` |
| `llm_model` | — | Blank uses the provider's default model |
| `llm_endpoint` | `http://localhost:11434` | Local LLM server URL (LM Studio default is `:1234`) |
| `llm_temperature` | `0.2` | Sampling temperature for refine/summarize |
| `anthropic_api_key`, `openai_api_key`, `google_api_key`, `ollama_cloud_api_key` | — | Provider keys (the env vars above take precedence) |
| `discord_bot_token` | — | Discord recording bot token (env `DISCORD_BOT_TOKEN` takes precedence) |
| `discord_default_guild` / `discord_default_channel` | — | Used when `record start` gets no `--guild`/`--voice-channel`/`--preset` |
| `discord_presets` | `[]` | Saved guild/channel pairs; manage with `wisper config discord-presets` |

---

## Where Data Is Stored

Speaker profiles and config are stored in your OS user data directory — separate from the project folder so they persist across updates.

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\wisper-transcribe\` |
| Mac | `~/Library/Application Support/wisper-transcribe/` |
| Linux | `~/.local/share/wisper-transcribe/` |

```
wisper-transcribe/
├── config.toml          settings
├── profiles/
│   ├── speakers.json    speaker registry (global — one entry per person)
│   └── embeddings/
│       ├── alice.npy    voice fingerprint
│       └── bob.npy
├── campaigns/
│   ├── campaigns.json   campaign rosters (additive layer over global profiles)
│   └── <slug>/
│       └── journal.md   rolling campaign journal (`wisper campaigns journal`)
├── recordings/          Discord and local recordings (audio + metadata)
└── output/              transcripts, when ./output doesn't exist in the working directory
```

Override the storage path with `WISPER_DATA_DIR` (set automatically in Docker).

---

## Config Resolution Order (CLI / web transcription options)

`wisper transcribe` and the web upload form resolve `model`, `language`, and `timestamps` as **explicit value → `config.toml` → built-in default.** An explicit value always wins.

| Setting | CLI flag | Config key | Hardcoded fallback |
|---|---|---|---|
| Whisper model | `-m/--model` | `model` | `large-v3-turbo` |
| Language | `-l/--language` | `language` | `en` |
| Timestamps | `--timestamps/--no-timestamps` | `timestamps` | on (`true`) |

`--language auto` explicitly requests auto-detection and overrides the config `language`.

**Speaker count:** with diarization on and no `-n`/`--min-speakers`/`--max-speakers`, the `min_speakers`/`max_speakers` config keys (default `2`/`8`) bound the diarizer. `-n/--num-speakers` pins an exact count and ignores them.

**`device` and `compute_type`** default to `auto` (detect hardware / pick a dtype per device) rather than following the chain above.

**`wisper config set` validation:** only keys in the table above can be set; a typo fails with `Unknown config key '...'`. Values are converted to the key's default type (bool, int, float, comma-separated list, or string).

---

## Debugging and Verbose Output

wisper suppresses informational warnings from its dependencies (speechbrain, pyannote, torch) that are not actionable during normal use. Two CLI flags give you more visibility:

### `--verbose`

Surfaces ML library log output (pyannote, faster-whisper, Lightning) on the console at DEBUG level alongside normal status messages. Use this when something is misbehaving and you want to see what the libraries are doing:

```bash
wisper transcribe session.mp3 --verbose
```

### `--debug`

Writes a full timestamped log to `./logs/wisper_<YYYYMMDD_HHmmss>.log`. Every `tqdm.write()` status message and Python logging output at DEBUG level is captured — including output forwarded from parallel subprocess workers. The log path is printed when the run starts:

```bash
wisper transcribe session.mp3 --debug
#  Debug log: logs/wisper_20260409_134105.log
```

Both flags can be combined:

```bash
wisper transcribe session.mp3 --verbose --debug
```

### `WISPER_DEBUG` env var

Sets the same warning-suppression override as `--debug` without creating a log file. Use when you want raw dependency output in the terminal without a file:

```powershell
# Windows PowerShell
$env:WISPER_DEBUG="1"
wisper transcribe session.mp3
```

```bash
# Mac/Linux
WISPER_DEBUG=1 wisper transcribe session.mp3
```
