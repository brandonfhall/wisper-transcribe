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

## Config Resolution Order (CLI / web transcription options)

`wisper transcribe` and the web upload form share the same resolution order for `model`, `language`, and `timestamps`: **explicit CLI/web value → `config.toml` → hardcoded fallback.** An explicit value always wins, even one that happens to match the hardcoded fallback (e.g. explicitly passing `--model medium` is never silently overridden by a `model = "large-v3-turbo"` in config).

| Setting | CLI flag | Config key | Hardcoded fallback |
|---|---|---|---|
| Whisper model | `-m/--model` | `model` | `large-v3-turbo` |
| Language | `-l/--language` | `language` | `en` |
| Timestamps | `--timestamps/--no-timestamps` | `timestamps` | on (`true`) |

`--language auto` (or a web form value of `auto`) is a separate, explicit "auto-detect" marker — not the same as omitting `--language` — and always wins regardless of what `language` is set to in config.

**Speaker-count fallback:** when diarization is enabled and neither `-n/--num-speakers` nor `--min-speakers`/`--max-speakers` is passed, wisper falls back to the `min_speakers`/`max_speakers` config keys (defaults: `2` / `8`) as a constraint on the diarizer. Pinning `-n/--num-speakers` (an exact expected count) suppresses this fallback entirely — it's the user asserting one label per person, so no min/max range applies.

**`device` and `compute_type`** keep their own separate `"auto"` sentinel (auto-detect hardware / resolve a concrete CTranslate2 dtype) — they are not part of the config-fallback chain above; `--device`/`--compute-type` and their web-form equivalents pass `"auto"` as their own literal default already.

**`wisper config set` validation:** only keys that already exist in the default config schema can be set — `wisper config set some_typo value` fails with `Unknown config key 'some_typo'; run wisper config show to list keys` rather than silently writing an unused key. The value is coerced to match the key's *schema* type (i.e. the type of `DEFAULTS[key]`, not whatever type happens to already be stored — this self-heals a value that was previously stored with the wrong type) using bool → int → float → comma-split list → string, in that check order — bool is checked first since Python's `bool` is an `int` subclass.

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
