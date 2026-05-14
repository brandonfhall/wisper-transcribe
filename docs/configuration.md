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
├── profiles/
│   ├── speakers.json    speaker registry (global — one entry per person)
│   └── embeddings/
│       ├── alice.npy    voice fingerprint
│       └── bob.npy
└── campaigns/
    └── campaigns.json   campaign rosters (additive layer over global profiles)
```

Override the storage path with `WISPER_DATA_DIR` (set automatically in Docker).

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
