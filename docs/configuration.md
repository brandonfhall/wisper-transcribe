# Configuration Reference

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `HF_TOKEN` | HuggingFace token — preferred name (used by Docker `.env` and all HF libraries) |
| `HUGGINGFACE_TOKEN` | Alias for `HF_TOKEN`; both are accepted and propagated to each other |
| `WISPER_DATA_DIR` | Override the data directory (config, database, profiles) — set automatically in Docker |
| `WISPER_OUTPUT_DIR` | Override the transcripts folder (takes precedence over the `output_dir` setting) — set to `/app/output` in Docker |
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
| `similarity_threshold` | `0.55` | Minimum voice-embedding similarity to match an enrolled speaker. A saved `0.65` (the old default, from the previous speaker model) is read as `0.55`; to keep a stricter value, set something other than exactly `0.65` |
| `min_speakers` / `max_speakers` | `2` / `8` | Diarizer range when no speaker count is given |
| `hf_token` | — | HuggingFace token (env `HF_TOKEN` takes precedence) |
| `hotwords` | `[]` | Custom vocabulary (names, places) fed to Whisper as a prompt |
| `use_mlx` | `auto` | Apple Silicon: `auto` uses MLX Whisper when installed, `true` requires it, `false` always uses faster-whisper |
| `forced_alignment` | `auto` | Re-time Whisper's words against the audio before speaker assignment, so words at speaker changes land with the right person. `auto` = on when diarizing on a GPU (CUDA or Apple Silicon), off on CPU; `true` / `false` force it. Never runs with `--no-diarize`. `--forced-align/--no-forced-align` overrides. |
| `parallel_stages` | `false` | Run transcription and diarization concurrently in two subprocesses. Uses more memory; benchmark before enabling. Ignored while the web job queue delegates to the ML worker (`ml_worker`). |
| `ml_worker` | `true` | Web jobs: run the GPU model calls in a separate process so **Stop Job** terminates it and frees the GPU immediately. Off runs everything in-process (Stop then waits for the current batch). Ignored by the CLI. |
| `llm_provider` | `ollama` | `ollama`, `ollama-cloud`, `lmstudio`, `anthropic`, `openai`, or `google` |
| `llm_model` | — | Blank uses the provider's default model |
| `llm_endpoint` | `http://localhost:11434` | Local LLM server URL (LM Studio default is `:1234`) |
| `llm_temperature` | `0.2` | Sampling temperature for refine/summarize |
| `anthropic_api_key`, `openai_api_key`, `google_api_key`, `ollama_cloud_api_key` | — | Provider keys (the env vars above take precedence) |
| `discord_bot_token` | — | Discord recording bot token (env `DISCORD_BOT_TOKEN` takes precedence) |
| `discord_default_guild` / `discord_default_channel` | — | Used when `record start` gets no `--guild`/`--voice-channel`/`--preset` |
| `discord_presets` | `[]` | Saved guild/channel pairs; manage with `wisper config discord-presets` |
| `output_dir` | — | Transcripts folder. Blank = `output/` in the data directory. `wisper config set` stores it as an absolute path; env `WISPER_OUTPUT_DIR` takes precedence |

---

## Where Data Is Stored

The database, settings, voice samples, and recordings are stored in your OS user data directory — separate from the project folder so they persist across updates.

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\wisper-transcribe\` |
| Mac | `~/Library/Application Support/wisper-transcribe/` |
| Linux | `~/.local/share/wisper-transcribe/` |

```
wisper-transcribe/
├── config.toml          settings
├── wisper.db            database (SQLite)
├── backups/             automatic pre-upgrade copies; `wisper db backup` default
├── profiles/
│   └── embeddings/
│       └── alice.mp3    short voice sample for the Speakers page (profiles are in wisper.db)
├── campaigns/
│   └── <slug>/
│       └── journal.md.v11-adopted   a journal moved into its campaign folder, kept for rolling back
├── recordings/          each recording's combined.flac (or a legacy combined.wav), plus per-user tracks of Discord speakers not yet enrolled (recording details are in wisper.db)
└── output/              transcripts (unless `output_dir` / `WISPER_OUTPUT_DIR` points elsewhere)
```

Override the storage path with `WISPER_DATA_DIR` (set automatically in Docker). Don't point it at a synced folder (OneDrive, Dropbox, iCloud): syncing a database file while it's open can corrupt it.

### Transcripts folder

Transcripts go to the output root: `WISPER_OUTPUT_DIR` if set, else the `output_dir` setting, else `output/` in the data directory. It never depends on the directory you launch wisper from. Each uploaded transcript keeps its audio as `<name>.flac` beside it. An install whose transcripts are in a `./output` folder next to where wisper was started gets that path saved into `output_dir` the first time it runs with the database.

Each campaign's journal is `<campaign folder>/<campaign folder> Journal.md` inside the transcripts folder, in a folder wisper has claimed (it created the folder, or found it absent or empty). wisper never reads or writes a journal in a folder it hasn't claimed. A journal from an older install, at `campaigns/<slug>/journal.md` in the data directory, moves into the campaign folder the first time it is read, and at startup; the old file is kept as `journal.md.v11-adopted`.

A campaign's combined summary and its recaps are `<campaign folder> Combined Summary.md` and `<campaign folder> Recap — <session>.md`, in the same claimed folder, and are written and deleted as files wisper owns.

Large stored audio (whole uploaded videos, extra recording copies) is shrunk by `wisper storage trim`, which also moves existing sessions into their campaign folders; run it once after upgrading.

To move your transcripts: stop the server, move the files, then `wisper config set output_dir <new path>`.

### Database and backups

`wisper.db` in the data directory holds speaker profiles (including voice fingerprints), campaigns and their session order, the list of known transcripts with each one's speaker names, which sessions each journal has folded in, recordings, job history, and the search index. Transcripts, summaries, journals, and audio stay ordinary files you can open and edit. The database also records every file wisper owns: its owner, kind, location, size, and modified time. It is brought in line with the disk at startup and when you open the Transcripts or Campaigns page; it never deletes a file it finds unexpected. The database is created on first use and upgraded automatically; before an upgrade changes an existing database, a copy is saved in `backups/`, and only the newest five snapshots are kept. The server prints `Database upgraded from version N to M` with the copy's name when it upgrades, and points at `wisper storage trim` for what the upgrade left to do: move existing sessions into their campaign folders, and shrink stored audio after an upgrade from a release before the file registry.

Upgrading from a version that stored these as JSON files (`speakers.json`, `campaigns.json`, `.npy` voice files, `recordings.json` and each recording's `metadata.json`, and the speaker data in each transcript's `_diar.json`) imports them once on first start. Copies of the originals go to `backups/pre-sqlite-v<N>-<time>/`, and anything that had to be repaired or dropped (for example a campaign member whose profile no longer exists) is listed in `import-report.txt` there. The JSON files are then deleted; each `_diar.json` keeps only its speaker timings.

- `wisper db status` — schema version, integrity check, and which processes are using it.
- `wisper db backup [DEST]` — a consistent copy, safe while the server is running.
- `wisper db dump` — the whole database as SQL text.
- `wisper db reindex` — rebuild the search index from the transcript files (never loses data).

To restore, stop the server and replace `wisper.db` with the backup. On Windows the database file is locked while wisper runs, so stop the server before moving or restoring the data directory.

Requires SQLite 3.43 or newer with FTS5, which the Python 3.13+ builds from python.org and Homebrew and the Docker image all include. wisper refuses to start, naming what's missing, on an older system SQLite.

**Development builds:** a build from an unmerged development branch refuses to use the default data directory, because its database layout may still change. Point `WISPER_DATA_DIR` at a copy of your data instead.

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

**`wisper config set` validation:** only keys in the table above can be set; a typo fails with `Unknown config key '...'`. Values are converted to the key's default type (bool, int, float, comma-separated list, or string). `model`, `device`, `compute_type`, and `forced_alignment` only accept their listed values.

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

### Going back to an older version

A build older than the campaign-folder schema can't open the upgraded database. To go back:

1. Stop wisper.
2. Restore the newest `backups/wisper-v10-*.db` as `wisper.db`.
3. Rename each `campaigns/<slug>/journal.md.v11-adopted` back to `journal.md`.
4. Move session files out of campaign folders back into the transcripts folder, so the older build finds them.
