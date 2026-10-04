# CLI Reference

## Quick Start: Enrolling Speakers

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
  Model  : large-v3-turbo (cuda, float16)
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

Add `--play-audio` to hear a short clip of each speaker before naming them. If you already have enrolled profiles, the prompt shows a numbered list so you can select by number instead of retyping:

```
  Speaker 1 of 6 (heard at 00:00:12):
    "Welcome back everyone..."
  [playing audio excerpt...]
  Existing speakers:
    1. Alice (DM) — 89% ★
    2. Charlie (Player) — 71%
    3. Bob (Player) — 43%
  Enter a number to select, or type a new name.
  Who is this? (or 'r' to replay): 1
  Using existing profile for Alice.
  Add this episode's audio to improve future recognition of Alice? [y/N]:
```

Entering `r` replays the clip. Entering a number reuses an existing profile. Profiles are ranked by voice similarity — `★` marks any match above the confidence threshold.

### All future sessions — fully automatic

```bash
wisper transcribe session02.mp3 --num-speakers 6
```

```
────────────────────────────────────────────────────────────
  Input  : session02.mp3
  Output : session02.md
  Model  : large-v3-turbo (cuda, float16)
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

## Commands

### `wisper setup`

Guided first-run wizard. Run this once after installation:

```bash
wisper setup
```

Checks ffmpeg, detects your GPU (CUDA/MPS/CPU), prompts for your HuggingFace token, and pre-downloads the pyannote diarization model (~30 MB, cached permanently). Whisper models download on first transcription. When forced alignment will be on for your device (see `forced_alignment`), it also pre-downloads the word alignment model (~1.7 GB).

---

### `wisper transcribe`

```
wisper transcribe <path>

  path                     Audio or video file, or folder of files
                           Audio: mp3 wav m4a m4b flac ogg
                           Video: mp4 mkv mov avi webm m4v flv ts mts m2ts
                           (video: first audio track extracted automatically)

  -o, --output DIR         Output directory (default: same as input)
  -m, --model SIZE         tiny / base / small / medium / large-v3 / large-v3-turbo
                           (default: from config — `model` key, itself defaulting
                           to large-v3-turbo. An explicit --model always wins over
                           config, even if it happens to match config's own value.)
  -l, --language LANG      Language code, e.g. en, fr, de
                           (default: from config — `language` key, itself
                           defaulting to en). Use 'auto' to detect automatically —
                           this is a distinct explicit marker, not the "unset" default.
  --device auto|cpu|cuda|mps  Compute device (default: auto-detect; mps = Apple Silicon GPU)
  -n, --num-speakers INT   Expected speaker count — improves accuracy. Pinning this
                           suppresses the min/max-speakers config fallback below.
  --min-speakers INT       Minimum speaker count (default: from config —
                           `min_speakers` key — when neither this, --max-speakers,
                           nor --num-speakers is passed)
  --max-speakers INT       Maximum speaker count (default: from config —
                           `max_speakers` key — same fallback rule as above)
  --enroll-speakers        Interactively name speakers (use on first run)
  --play-audio             Play each speaker's sample clip during enrollment
  --no-diarize             Skip speaker detection (single-speaker output)
  --timestamps             Include timestamps
  --no-timestamps          Omit timestamps
                           (default: from config — `timestamps` key, itself
                           defaulting to on, when neither flag is passed)
  --compute-type TYPE      CTranslate2 dtype: auto|float16|int8_float16|int8|float32
                           (default: auto → float16 on CUDA, int8 on CPU)
  --vad / --no-vad         Voice activity detection — skips silence before transcription
                           (default: from config, on; improves speed and accuracy on
                           audio with pauses)
  --forced-align / --no-forced-align
                           Re-time words against the audio before speaker assignment
                           (default: from config; auto = on when diarizing on a GPU)
  --vocab-file FILE        Text file of custom words/names (one per line) to boost accuracy.
                           Useful for character names, locations, and game-specific terms
                           that Whisper might not recognize (e.g. "Kyra", "Golarion").
                           Lines starting with # are ignored.
                           Overrides hotwords stored in config.
  --initial-prompt TEXT    Text prepended as prior context to guide transcription style
                           and vocabulary. Alternative to --vocab-file for short hints.
  --overwrite              Re-process files that already have output. Without it an existing
                           transcript is skipped ("already processed (in campaign 'x')"); with it
                           the transcript keeps its campaign place, and a folded journal is
                           marked as needing a rebuild.
  --workers INT            Parallel workers for folder processing — CPU only;
                           clamped to 1 on GPU (default: 1)
  --campaign SLUG          Restrict speaker matching to this campaign's roster, and add the
                           transcript to the campaign. Run `wisper campaigns list` for slugs.
                           Only when the output lands in the transcripts folder (the default
                           output is next to the input, so pass -o <transcripts folder> unless
                           the audio is already there); elsewhere it prints a note and skips the
                           association. Transcripts written there are also indexed for search.
  --verbose                Show detailed progress; surfaces ML library log output
                           (pyannote, faster-whisper) on the console at DEBUG level
  --debug                  Write a full timestamped log to ./logs/wisper_<timestamp>.log
                           (tqdm.write output + Python logging at DEBUG level)
```

---

### `wisper enroll`

Add a speaker from a clean reference clip (e.g. an interview or isolated recording):

```bash
wisper enroll "Alice" --audio alice_intro.mp3
wisper enroll "Alice" --audio session01.mp3 --segment "0:30-1:15"
wisper enroll "Alice" --audio session08.mp3 --update   # blend with existing profile
wisper enroll "Alice" --audio alice_intro.mp3 --notes "DM, usually on mic 2"
```

```
Options:
  --audio PATH       Audio file to extract the voice from (required)
  --segment TEXT     Time range to use, e.g. "0:30-1:15"
  --notes TEXT       Free-text notes stored with the speaker profile
  --update           Average with the existing embedding instead of replacing it
```

`--update` on a profile enrolled with the older speaker model replaces its embedding instead of averaging, since the two can't be mixed.

---

### `wisper speakers`

```bash
wisper speakers list                    # show all enrolled profiles
wisper speakers remove "Alice"          # delete a profile
wisper speakers rename "Alice" "Alicia" # rename a profile (campaign roles and Discord bindings follow)
wisper speakers doctor                  # report likely duplicates, old-model and placeholder-named profiles
wisper speakers reset                   # delete ALL profiles and embeddings (with confirmation)
wisper speakers test session03.mp3                         # preview match results without writing output
wisper speakers test session03.mp3 --campaign d-d-mondays  # restrict to campaign roster
```

`speakers doctor` only reports; it never changes profiles. It lists pairs of profiles whose voices score above 0.95 similarity (probably one person enrolled twice), profiles from an older speaker model or with no voice sample (never matched until re-enrolled), and profiles named like a placeholder (`SPEAKER_03`, `Unknown Speaker 2`).

`speakers test` prints each label's similarity score, or for an unmatched label the closest profile and its score (e.g. `SPEAKER_03 → Unknown Speaker 1 (closest: Ben 0.48)`). Use it to tune `similarity_threshold`.

---

### `wisper campaigns`

Campaigns let you track multiple games with separate player rosters. Speaker voice embeddings stay global — adding a player to a second campaign reuses their existing voice profile with no re-enrollment required.

```bash
wisper campaigns list                                    # show all campaigns
wisper campaigns create "D&D Mondays"                   # create a campaign (prints the slug)
wisper campaigns show d-d-mondays                       # roster table with roles/characters
wisper campaigns add-member d-d-mondays alice --role DM # add a player (must be enrolled)
wisper campaigns add-member d-d-mondays bob --role Player --character "Theron"
wisper campaigns remove-member d-d-mondays charlie      # remove from roster only (keeps voice profile)
wisper campaigns delete d-d-mondays                     # delete campaign, keep its transcripts and journal file (with confirmation)
wisper campaigns delete d-d-mondays --delete-transcripts  # also delete its transcripts, their files, and its journal
wisper campaigns reorder d-d-mondays s02 --up           # move a session one position earlier
wisper campaigns reorder d-d-mondays s02 --down         # move a session one position later
wisper campaigns reorder d-d-mondays --set "s01,s02,s03" # replace the whole order in one shot
```

`reorder` sets the order sessions are folded into the journal and numbered on the campaign page. The order is when each transcript was added to the campaign, not its date, so a late-added session can land out of place. `--set` takes every transcript stem in the campaign, comma-separated, and errors unless it is an exact permutation of the current list.

#### `wisper campaigns relabel`

Re-matches speakers across every session in a campaign, so identities stay consistent between sessions:

```bash
wisper campaigns relabel d-d-mondays --dry-run      # show what would change
wisper campaigns relabel d-d-mondays                # apply
wisper campaigns relabel d-d-mondays --no-backfill  # only use voice data already stored
```

- Automatically assigned names are matched against the campaign roster again, so a player enrolled after a session was transcribed gets named in it.
- An unknown voice heard in two or more sessions gets one shared name, `Recurring Speaker N`. Name them once in any session's wizard and the others follow.
- Names you set by hand are never changed.
- Sessions with no stored voice data have it re-extracted from the saved source audio when that still exists (web uploads keep it next to the transcript). Sessions without either are skipped and listed.

```
Options:
  --dry-run       Show what would change without writing anything
  --no-backfill   Don't re-read source audio for sessions without stored voice data
  --device        Device for voice extraction (auto, cpu, cuda, mps)
```

#### `wisper campaigns journal`

Maintains a **rolling campaign journal** — a single living document the LLM rewrites as each new session is folded in. It reads the per-session `.summary.md` sidecars (from `wisper summarize`) and accumulates them into `campaigns/<slug>/journal.md`, tracking story arcs, open plot threads, NPCs, party decisions, and a running loot ledger. Context stays bounded: each fold sends only the current journal plus one new session summary.

```bash
wisper campaigns journal d-d-mondays                  # fold the next unjournalled session
wisper campaigns journal d-d-mondays --all            # fold every pending session (oldest first)
wisper campaigns journal d-d-mondays --session s05    # fold a specific session stem
wisper campaigns journal d-d-mondays --provider openai --model gpt-4o-mini
wisper campaigns journal d-d-mondays --rebuild        # start over from the existing summaries (asks to confirm)
wisper campaigns journal d-d-mondays --rebuild --yes  # same, skip the confirmation prompt
wisper campaigns journal d-d-mondays --rebuild --resummarize  # re-summarize every transcript first
wisper campaigns journal d-d-mondays --export         # print it with the folded-session list
wisper campaigns journal d-d-mondays --export -o journal.md
```

A session is "pending" once it has a `.summary.md`. With no flags the command folds the single oldest unjournalled session; re-run (or use `--all`) to catch up the rest. Sessions already folded are tracked in wisper's database and skipped. `--export` adds that list back as `journaled_sessions:` in the frontmatter (`journal.md` itself doesn't carry it).

`--rebuild` starts the journal over from each session's existing `.summary.md`, in campaign order — one LLM call per session, plus one for any session that has no summary yet. Your edits to summaries are kept. Add `--resummarize` to re-summarize every transcript first, overwriting the summaries (two calls per session) — for when the summaries themselves are bad. Both ask for confirmation, showing the call count, unless `--yes` is passed. Sessions whose transcript is missing or whose summary fails are skipped and reported. `--session`, `--all`, `--rebuild`, and `--export` are mutually exclusive.

**Stale journal:** moving a folded session to another campaign, removing it from the campaign, deleting it, or re-transcribing it never edits the journal text; it marks the journal stale instead. `wisper campaigns show <slug>` prints `Journal: STALE since …` with the rebuild command. Deleting `journal.md` by hand starts a fresh journal (every session becomes pending again); editing it by hand is fine, and later folds build on your edits.

**Scoping transcription to a campaign:**

```bash
wisper transcribe session12.mp3 --campaign d-d-mondays --num-speakers 5
```

With `--campaign`, speaker matching is restricted to that campaign's enrolled members — players from other campaigns won't appear in the output.

**Voice transfer between campaigns:** Because embeddings are stored globally, adding an existing speaker profile to a new campaign automatically gives that campaign the benefit of all previously recorded voice data. No re-enrollment needed.

**Binding Discord IDs to campaign members:**

When using the Discord recording bot, you can link each campaign member to their Discord user ID so their audio track is automatically labelled without manual intervention.

1. Go to **Campaigns → [your campaign]** in the web UI.
2. In the roster table, paste the member's Discord user ID (a numeric snowflake, e.g. `123456789012345678`) into the **Discord ID** column and click **Link**.
3. When the bot records a session and that user speaks, their per-user track is automatically tagged with their wisper profile name.

To find a Discord user ID: enable Developer Mode in Discord → right-click the user → *Copy User ID*. Each ID can only be bound to one roster member per campaign.

---

### `wisper transcripts`

`list` shows each campaign's transcripts in fold order; an entry whose file isn't in the transcripts folder is marked `(missing — file not found)` (relink it on the web Campaign page, or remove it). Listing also registers `.md` files you added to the folder yourself.

Organize and view transcript-to-campaign associations from the command line:

```bash
wisper transcripts list                          # list all transcripts, grouped by campaign
wisper transcripts list --campaign d-d-mondays  # show only transcripts for a specific campaign
wisper transcripts move session12 --campaign d-d-mondays   # assign a transcript to a campaign
wisper transcripts move session12 --no-campaign            # remove campaign association
```

- `session12` is the transcript stem (filename without `.md`).
- A transcript can belong to at most one campaign at a time.
- When a transcript or file needs a decision (a missing transcript, a file gone from disk, a file with no transcript), the listing ends with "N items need attention; see the Transcripts page".

---

### `wisper search`

Search every transcript's title and text, and every session summary, in the transcripts folder:

```bash
wisper search strahd                               # every block that mentions Strahd
wisper search "fights the dragon"                  # all three words in one block
wisper search '"take your mask off"'               # an exact phrase
wisper search 'Stra*' --campaign curse-of-strahd   # prefix, one campaign
wisper search loot --kind summary                  # session summaries only
wisper search castle --speaker "Alice" --limit 20  # one speaker, up to 20 transcripts
```

Each result prints the transcript name, its campaign, and the number of matches. Up to three blocks follow, each with timestamp, speaker (or `summary`, or `title` for a match on the transcript's name, listed first), and a snippet with the matched words highlighted.

- Words match their other forms ("fights" finds "fight") and ignore accents. Every word must appear in the same block. Double quotes match a phrase, and a trailing `*` matches a prefix. Other symbols and words like `OR` or `NEAR` are searched as ordinary text.
- `--speaker` is the exact name shown in the transcript. `--limit` (default 10) is the number of transcripts shown.
- Transcripts not yet indexed (added while nothing was running, or just after an upgrade) are indexed before the search runs.

---

### `wisper fix`

Fix a wrong speaker assignment in an existing transcript:

```bash
wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank"
wisper fix session03.md --speaker "Alice" --name "Diana"
```

`--re-enroll` prints the `wisper enroll <name> --audio <file> --update` command that updates the voice profile; it doesn't run it.

---

### `wisper refine`

LLM-assisted cleanup of an existing transcript. Two tasks:

- **`vocabulary`** *(default)* — fixes proper-noun misspellings (Whisper renders "Kyra" as "Kira"). Edits are validated against your configured `hotwords` + enrolled character names — freeform rewrites are rejected.
- **`unknown`** — suggests identities for `Unknown Speaker N` labels based on surrounding dialogue. Suggestions are **never auto-applied**; confirm with `wisper fix`.

```bash
wisper refine session05.md                              # dry-run; prints coloured diff
wisper refine session05.md --apply                      # writes session05.md.bak, updates in place
wisper refine session05.md --tasks vocabulary,unknown   # run both passes
wisper refine session05.md --provider anthropic         # override default provider
```

Options: `--tasks`, `--provider {ollama,ollama-cloud,lmstudio,anthropic,openai,google}`, `--model NAME`, `--endpoint URL` (ollama/lmstudio), `--dry-run/--apply`, `--no-color`.

Safety: YAML frontmatter is never sent to the LLM and is preserved byte-for-byte. Network failures soft-fail with a warning and leave the transcript untouched.

---

### `wisper summarize`

Generate campaign notes from a transcript — a session recap, loot/inventory changes, notable NPCs, and follow-up plot hooks — written to `<stem>.summary.md` as an Obsidian-ready sidecar.

```bash
wisper summarize session05.md                        # writes session05.summary.md
wisper summarize session05.md --overwrite            # replace existing sidecar
wisper summarize session05.md --refine               # refine in place, then summarize
wisper summarize session05.md --sections summary,loot  # only these sections
wisper summarize session05.md --output recap.md      # custom output path
wisper summarize session05.md --provider openai --model gpt-4o-mini
```

Options: `--provider`, `--model`, `--endpoint`, `--output PATH`, `--sections summary,loot,npcs,followups`, `--overwrite`, `--refine`, `--refine-tasks`.

Output format:
```markdown
---
type: session-summary
source: "Episode 47.md"
refined: true
provider: anthropic
model: claude-sonnet-5
---
# Session 47 — Summary
## Summary
…
## Loot & Inventory
- [[Thorin]] gained **+120 gp** from the chest
## NPCs
- Aziel — dragon, guarding the hoard (first at 14:22)
## Follow-ups
- [ ] Who sent the letter?
```

Character names are wrapped in `[[wiki-links]]` only when they match an enrolled speaker profile — unknown names stay plain so they don't create orphan Obsidian pages.

With `--refine`, vocabulary edits are applied in place (same `.md.bak` guarantee as `wisper refine --apply`) before summarization. If the refine step fails, the summary is still written with `refined: false` in its frontmatter.

---

### `wisper config`

```bash
wisper config show                        # print all settings (API keys masked as ***)
wisper config set model large-v3          # use the big model by default
wisper config set hf_token hf_abc123...   # store HuggingFace token
wisper config set similarity_threshold 0.60  # stricter speaker matching
wisper config set min_speakers 2          # min speaker count when diarizing (int)
wisper config set max_speakers 8          # max speaker count when diarizing (int)
wisper config path                        # show where config.toml lives
wisper config set output_dir ~/Transcripts  # transcripts folder (stored as an absolute path)
wisper config llm                         # interactive wizard: provider + model + key/endpoint
```

`wisper config set` rejects unknown keys and converts the value to the key's type (bool, int, float, or a comma-separated list such as `hotwords`). Choice keys (`model`, `device`, `compute_type`, `forced_alignment`) reject values outside their list. See [configuration.md](configuration.md#config-keys) for every key.

**`wisper config llm`** is the recommended way to configure `refine` / `summarize`. It walks you through the provider (Ollama / Ollama Cloud / LM Studio / Anthropic / OpenAI / Google), endpoint (local providers), model name, and API key (cloud providers) in one flow. For Ollama and LM Studio the wizard lists installed/loaded models so you can pick by number.

**Ollama Cloud — two paths:**
1. Keep `llm_provider = ollama` and pick a model with `-cloud` suffix (e.g. `gpt-oss:120b-cloud`); the local daemon proxies the call to ollama.com.
2. Set `llm_provider = ollama-cloud` and supply `OLLAMA_API_KEY`; wisper calls `https://ollama.com/api/chat` directly with no local daemon required.

Relevant keys: `llm_provider`, `llm_model`, `llm_endpoint`, `llm_temperature`, `anthropic_api_key`, `openai_api_key`, `google_api_key`, `ollama_cloud_api_key`.

---

### `wisper record`

Control recordings from the command line. A `wisper server` must be running: the CLI finds it via `server.json` in the data dir, or `WISPER_SERVER_URL` if set. `start`/`stop` control the Discord bot; `list`/`show`/`transcribe`/`delete` cover Discord and local recordings.

```bash
wisper record start --voice-channel <ID> --guild <ID>           # join a channel and start recording
wisper record start --voice-channel <ID> --guild <ID> --campaign d-d-mondays
wisper record start --preset "Weekly D&D"                       # use a saved preset
wisper record start                                             # falls back to discord_default_guild/
                                                                  # discord_default_channel from config
wisper record stop                                              # stop the active session
wisper record list                                              # list all recordings
wisper record show <recording_id>                               # show metadata for a recording
wisper record transcribe <recording_id>                         # re-queue transcription
wisper record delete <recording_id>                             # delete recording + its files on disk (permanent)
wisper record recover <recording_id>                            # rebuild a crashed session's audio so it can be transcribed
```

`record start` resolves the guild and channel from: explicit flags → `--preset` → the `discord_default_guild`/`discord_default_channel` config keys (set via `wisper config discord`, also used by the web Record page).

**Managing channel presets:**

```bash
wisper config discord                                           # set bot token, default guild/channel
wisper config discord-presets add --name "Weekly D&D" --guild <ID> --channel <ID>
wisper config discord-presets list
wisper config discord-presets remove "Weekly D&D"
```

Presets are also manageable via the web UI — the Record page has an inline "Save as preset" form.

---

### `wisper db`

Inspect, back up, and reindex the database (`wisper.db` in the data directory).

```bash
wisper db status                  # schema version, integrity and foreign-key checks, runtime leases
wisper db backup                  # copy to <data dir>/backups/wisper-<time>.db
wisper db backup ~/wisper.db.bak  # copy to a chosen file (refuses to overwrite)
wisper db dump                    # whole database as SQL text
wisper db dump -o dump.sql
wisper db reindex                 # drop and rebuild the search index from the transcript files
```

`status` is read-only: it never upgrades the database, so it also works when startup refuses (for example, a database from a newer wisper). `backup` uses SQLite's backup API and is safe while the server is running. `reindex` loses nothing: the search index is built from the `.md` files, so rebuilding it fixes a stale or damaged index.

Every command that uses the database stops with a clear message, not a traceback, when it can't: a database newer than this wisper, an unmerged development build pointed at the default data directory, an SQLite older than 3.43 or without FTS5, or a native process while a Docker Desktop container is using the same data directory (see [docker.md](docker.md#one-way-of-running-at-a-time)).

---

### `wisper server`

Start the browser-based web UI:

```bash
wisper server                   # default: http://127.0.0.1:8080 (localhost only)
wisper server --port 9000       # custom port
wisper server --host 0.0.0.0    # expose on the network — trusted networks only, see below
wisper server --reload          # dev mode — auto-reloads on code changes
```

> **Security:** the web UI has **no authentication** — anyone who can reach the
> port has full read-write control (uploads, deletions, configuration,
> recording control). The server therefore binds `127.0.0.1` by default.
> Pass `--host 0.0.0.0` only on networks you trust (the Docker setup does
> this explicitly inside the container, publishing the port via Docker).
> See [web-ui.md](web-ui.md#trust-model) for the full trust model.

---

## Supported Formats

**Audio:** `.mp3` `.wav` `.m4a` `.m4b` `.flac` `.ogg`

**Video:** `.mp4` `.m4v` `.mkv` `.mov` `.avi` `.webm` `.flv` `.ts` `.mts` `.m2ts`

Video files are handled by extracting only the **first audio track** (`ffmpeg -map 0:a:0`). This works correctly with multi-track recordings where track 0 is a combined mix. Your original files are never modified.

All formats are converted to 16kHz mono WAV internally before transcription.

---

## Output Format

`wisper transcribe` writes one `.md` per audio file next to the input (or in `--output`); web uploads and recordings go to the transcripts folder ([configuration.md](configuration.md#transcripts-folder)). Timestamps are `mm:ss`, or `hh:mm:ss` past the first hour:

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

**Alice** *(00:12)*: Welcome back everyone. Last session you had just entered
the ruins of Khar'zul.

**Bob** *(00:18)*: Right, I want to check for traps before we go further in.

**Alice** *(00:23)*: Go ahead and roll a perception check.
```

The YAML frontmatter makes these files easy to ingest into NotebookLM or query with scripts.
