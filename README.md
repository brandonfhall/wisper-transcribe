# wisper-transcribe

Local podcast transcription with automatic speaker identification. Built for tabletop RPG actual-play recordings (D&D, Pathfinder, etc.) with 5–8 speakers, but works for any multi-speaker audio.

Runs entirely offline. No cloud APIs. Outputs clean markdown files ready for NotebookLM or any text search tool.

---

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

See the [Docker section](#docker) below for the full volume layout and CLI usage.

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

## Installation (Developer / manual)

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

Entering `r` replays the clip. Entering a number reuses an existing profile. Profiles are ranked by voice similarity to the current speaker — `★` marks any match above the confidence threshold. You'll then be offered the option to blend this episode's audio into the existing profile (defaults to No).

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

  path                     Audio or video file, or folder of files
                           Audio: mp3 wav m4a flac ogg
                           Video: mp4 mkv mov avi webm m4v flv ts mts m2ts
                           (video: first audio track extracted automatically)

  -o, --output DIR         Output directory (default: same as input)
  -m, --model SIZE         tiny / base / small / medium / large-v3 / large-v3-turbo
                           (default: large-v3-turbo)
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
  --vocab-file FILE        Text file of custom words/names (one per line) to boost accuracy.
                           Useful for character names, locations, and game-specific terms
                           that Whisper might not recognize (e.g. "Kyra", "Golarion").
                           Lines starting with # are ignored.
                           Overrides hotwords stored in config.
  --initial-prompt TEXT    Text prepended as prior context to guide transcription style
                           and vocabulary. Alternative to --vocab-file for short hints.
  --overwrite              Re-process files that already have output
  --workers INT            Parallel workers for folder processing — CPU only;
                           clamped to 1 on GPU (default: 1)
  --campaign SLUG          Restrict speaker matching to this campaign's roster.
                           Run `wisper campaigns list` to see available slugs.
  --verbose                Show detailed progress; surfaces ML library log output
                           (pyannote, faster-whisper) on the console at DEBUG level
  --debug                  Write a full timestamped log to ./logs/wisper_<timestamp>.log
                           (tqdm.write output + Python logging at DEBUG level)
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
wisper speakers test session03.mp3                         # preview match results without writing output
wisper speakers test session03.mp3 --campaign d-d-mondays  # restrict to campaign roster
```

### `wisper campaigns`

Campaigns let you track multiple games with separate player rosters. Speaker voice embeddings stay global — adding a player to a second campaign reuses their existing voice profile with no re-enrollment required.

```bash
wisper campaigns list                                    # show all campaigns
wisper campaigns create "D&D Mondays"                   # create a campaign (prints the slug)
wisper campaigns show d-d-mondays                       # roster table with roles/characters
wisper campaigns add-member d-d-mondays alice --role DM # add a player (must be enrolled)
wisper campaigns add-member d-d-mondays bob --role Player --character "Theron"
wisper campaigns remove-member d-d-mondays charlie      # remove from roster only (keeps voice profile)
wisper campaigns delete d-d-mondays                     # delete campaign (with confirmation)
```

**Scoping transcription to a campaign:**

```bash
wisper transcribe session12.mp3 --campaign d-d-mondays --num-speakers 5
```

With `--campaign`, speaker matching is restricted to that campaign's enrolled members — players from other campaigns won't appear in the output. Omitting `--campaign` uses all enrolled profiles as before.

**Voice transfer between campaigns:** Because embeddings are stored globally, adding an existing speaker profile to a new campaign automatically gives that campaign the benefit of all previously recorded voice data. No re-enrollment needed.

**Binding Discord IDs to campaign members:**

When using the Discord recording bot, you can link each campaign member to their Discord user ID so their audio track is automatically labelled without manual intervention.

1. Go to **Campaigns → [your campaign]** in the web UI.
2. In the roster table, paste the member's Discord user ID (a numeric snowflake, e.g. `123456789012345678`) into the **Discord ID** column and click **Link**.
3. When the bot records a session and that user speaks, their per-user track is automatically tagged with their wisper profile name in `Recording.discord_speakers`.

To find a Discord user ID: enable Developer Mode in Discord → right-click the user → *Copy User ID*. Each ID can only be bound to one roster member per campaign (linking to a new member clears the old binding automatically).

---

### `wisper transcripts`

Organize and view transcript-to-campaign associations from the command line:

```bash
wisper transcripts list                          # list all transcripts, grouped by campaign
wisper transcripts list --campaign d-d-mondays  # show only transcripts for a specific campaign
wisper transcripts move session12 --campaign d-d-mondays   # assign a transcript to a campaign
wisper transcripts move session12 --no-campaign            # remove campaign association
```

Notes:
- `session12` is the transcript stem (filename without `.md`).
- `wisper transcripts list` without `--campaign` shows ungrouped transcripts first, then each campaign folder.
- A transcript can belong to at most one campaign at a time; assigning to a new campaign automatically removes the previous association.

---

### `wisper fix`

Fix a wrong speaker assignment in an existing transcript:

```bash
wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank"
wisper fix session03.md --speaker "Alice" --name "Diana"
```

Add `--re-enroll` to also update the voice profile (currently prompts manual steps).

### `wisper refine`

LLM-assisted cleanup of an existing transcript. Two tasks:

- **`vocabulary`** *(default)* — fixes proper-noun misspellings (Whisper renders "Kyra" as "Kira", "Golarion" as "Golarian"). Edits are validated against your configured `hotwords` + enrolled character names — freeform rewrites are rejected.
- **`unknown`** — suggests identities for `Unknown Speaker N` labels based on surrounding dialogue. Suggestions are **never auto-applied** (rendered to stdout / sidecar only); confirm with `wisper fix`.

```bash
wisper refine session05.md                              # dry-run; prints coloured diff
wisper refine session05.md --apply                      # writes session05.md.bak, updates in place
wisper refine session05.md --tasks vocabulary,unknown   # run both passes
wisper refine session05.md --provider anthropic         # override default provider
```

Options: `--tasks`, `--provider {ollama,lmstudio,anthropic,openai,google}`, `--model NAME`, `--endpoint URL` (ollama/lmstudio), `--dry-run/--apply`, `--no-color`.

Safety: YAML frontmatter is never sent to the LLM and is preserved byte-for-byte. Network failures soft-fail with a warning and leave the transcript untouched.

### `wisper summarize`

Generate campaign notes from a transcript — a session recap, loot/inventory changes, notable NPCs, and follow-up plot hooks — written to `<stem>.summary.md` as an Obsidian-ready sidecar.

```bash
wisper summarize session05.md                        # writes session05.summary.md
wisper summarize session05.md --overwrite            # replace existing sidecar
wisper summarize session05.md --refine               # refine-then-summarize (atomic)
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
model: claude-sonnet-4-6
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

With `--refine`, vocabulary edits are applied in place (same `.md.bak` guarantee as `wisper refine --apply`) before summarization. If the refine step fails, the summary is still written with `refined: false` recorded in its frontmatter.

### `wisper config`

```bash
wisper config show                        # print all settings (API keys masked as ***)
wisper config set model large-v3          # use the big model by default
wisper config set hf_token hf_abc123...   # store HuggingFace token
wisper config set similarity_threshold 0.70  # stricter speaker matching
wisper config path                        # show where config.toml lives
wisper config llm                         # interactive wizard: provider + model + key/endpoint
```

**`wisper config llm`** is the recommended way to configure `refine` / `summarize`. It walks you through the provider (Ollama / Ollama Cloud / LM Studio / Anthropic / OpenAI / Google), endpoint (local providers), model name, and API key (cloud providers) in one flow. For Ollama and LM Studio the wizard lists installed/loaded models so you can pick by number. API keys can alternatively be set via the `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, or `OLLAMA_API_KEY` environment variables — env vars always take precedence over the stored value, and stored values are masked as `***` in `wisper config show`.

**Ollama Cloud — two paths.** You can either (1) keep `llm_provider = ollama` and pick a model with `-cloud` suffix (e.g. `gpt-oss:120b-cloud`); the local daemon proxies the call to ollama.com using credentials from `ollama signin`. Or (2) set `llm_provider = ollama-cloud` and supply `OLLAMA_API_KEY` / `ollama_cloud_api_key`; wisper then calls `https://ollama.com/api/chat` directly with no local daemon required. The web config page populates the model combobox from the public ollama.com catalog in both cases.

Relevant keys: `llm_provider`, `llm_model`, `llm_endpoint`, `llm_temperature`, `anthropic_api_key`, `openai_api_key`, `google_api_key`, `ollama_cloud_api_key`.

### `wisper record`

Control the Discord recording bot from the command line. The wisper server must be running first.

```bash
wisper record start --voice-channel <ID> --guild <ID>           # join a channel and start recording
wisper record start --voice-channel <ID> --guild <ID> --campaign d-d-mondays  # associate with campaign
wisper record start --preset "Weekly D&D"                       # use a saved preset
wisper record stop                                              # stop the active session
wisper record list                                              # list all recordings
wisper record show <recording_id>                               # show metadata for a recording
wisper record transcribe <recording_id>                         # re-queue transcription
wisper record delete <recording_id>                             # remove recording entry (files kept)
```

Requires the wisper server to be running (`wisper server`). Server location is read from `data_dir/server.json` automatically; override with `WISPER_SERVER_URL`.

**Managing channel presets:**

```bash
wisper config discord                                           # set bot token, default guild/channel
wisper config discord-presets add --name "Weekly D&D" --guild <ID> --channel <ID>
wisper config discord-presets list
wisper config discord-presets remove "Weekly D&D"
```

Presets are also manageable via the web UI — the Record page has an inline "Save as preset" form.

---

### `wisper server`

Start the browser-based web UI:

```bash
wisper server                  # default: http://0.0.0.0:8080
wisper server --port 9000      # custom port
wisper server --reload         # dev mode — auto-reloads on code changes
```

Open `http://localhost:8080` in your browser. All features available via the CLI are also accessible through the web UI: transcription, speaker enrollment, transcript browsing, LLM post-processing, config management.

---

## Web UI

A full-featured browser interface for wisper. No separate install — included in the same package.

### Quick start

```bash
wisper server
# → Open http://localhost:8080
```

### Features

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Job queue, system status (device, model, HF token), quick upload |
| Transcribe | `/transcribe` | Drag-and-drop upload, all transcription options, live progress stream; optional "Refine vocabulary" and "Generate campaign summary" post-processing checkboxes |
| Transcripts | `/transcripts` | Browse output files, view rendered markdown, download, delete; green notes icon on cards that have a campaign summary |
| Speakers | `/speakers` | Enroll, rename, remove speaker profiles |
| Campaigns | `/campaigns` | Create and manage campaigns; add/remove roster members; scope transcription to a campaign |
| Record | `/record` | Start and stop live Discord voice channel recording sessions; shows active session with live speaker and segment counts via SSE; **Browse bot's channels** panel lists available guilds and voice channels so you can click-to-fill IDs without leaving the page |
| Recordings | `/recordings` | Browse all recordings, grouped by campaign; view per-recording detail (status, speakers, segments); delete entries |
| Config | `/config` | View and edit all settings |

### Speaker enrollment in the web UI

The interactive CLI enrollment prompt is replaced by a post-job wizard. After transcription completes, click **Name Speakers** on the job detail page. Each detected speaker has a **Play sample** button so you can hear the voice before assigning a name. Existing profiles are shown as click-to-fill options ranked by voice similarity.

### Auto-enrollment from recordings

When the Discord bot records a session, any speaker whose Discord user ID is **not** bound to a campaign member is added to the recording's "Unknown Speakers" list. After the session ends, open the recording's detail page (`/recordings/{id}`) to see the panel. Enter a display name next to each unknown Discord ID and click **Enroll** — wisper extracts a voice embedding from their per-user audio track and creates a new speaker profile. The Discord ID is then bound to that profile in the campaign roster automatically, so future sessions tag them correctly without manual intervention.

### LLM Post-processing in the web UI

**Option 1 — at transcription time:**
In the Transcribe form, expand the Options panel and tick "Refine vocabulary" and/or "Generate campaign summary" under LLM Post-processing. Both run automatically after transcription completes as part of the same job, with Ollama status messages streamed to the progress log.

**Option 2 — from the Transcript detail page:**
Open any transcript and expand "LLM Post-processing". Click **Refine Vocabulary** or **Generate Campaign Summary** to queue a standalone LLM job. You are redirected to the job progress page, which streams status messages in real time.

**Campaign Notes:**
When a `.summary.md` sidecar exists, the transcript detail page shows a green "Campaign Notes available" panel with **View Notes** and **Download** buttons. Campaign notes are also accessible via the transcript list card (green notes icon). The notes page shows the session recap, loot, NPCs, and follow-up items rendered as HTML.

### Job management

- The job detail page shows a **real-time progress bar** with per-phase step indicators. For transcription jobs: Transcribing → Diarizing → Formatting with ETA and speed counter. For LLM jobs: a single step indicator (R for Refine, S for Summarize) with Ollama streaming messages in the log.
- A **Stop Job** button lets you cancel any pending or running job.
- Transcripts are saved to `./output/` (or `data_dir/output`) and are immediately visible on the Transcripts page after the job completes.
- Transcripts can be **deleted** from the Transcripts page (trash icon with confirmation). Deleting a transcript also removes its `.summary.md` sidecar if present.

### Web UI assets

All web UI assets (HTMX, Tailwind CSS, fonts) are served from local files — the page itself loads without any external network requests.

The UI uses three self-hosted fonts (all SIL OFL licensed, committed to the repository):
- **Newsreader** — serif display font for titles and long-form reading
- **Geist** — sans-serif body font
- **JetBrains Mono** — monospace font for IDs, timestamps, and CLI flags

HTMX, the fonts, and Tailwind CSS are all committed directly — no download step needed for local dev or Docker builds.

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

### Improve transcription accuracy for character names and locations

Pass a custom word list to boost recognition of proper nouns Whisper doesn't know:

```bash
wisper transcribe session01.mp3 --vocab-file characters.txt
```

`characters.txt` — one word per line, `#` comments ignored:
```
# Glass Cannon characters
Kyra
Golarion
Zeldris
Korvosa
```

To apply hotwords to every future transcription automatically, save them to config:

```bash
wisper config set hotwords "Kyra, Golarion, Zeldris, Korvosa"
```

The `--vocab-file` flag takes precedence over the stored config when both are present.

---

## Supported Formats

**Audio:** `.mp3` `.wav` `.m4a` `.flac` `.ogg`

**Video:** `.mp4` `.m4v` `.mkv` `.mov` `.avi` `.webm` `.flv` `.ts` `.mts` `.m2ts`

Video files are handled by extracting only the **first audio track** (`ffmpeg -map 0:a:0`). This works correctly with multi-track recordings where track 0 is a combined mix — the separate mic/system audio tracks are ignored. Your original files are never modified.

All formats are converted to 16kHz mono WAV internally before transcription.

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

---

## Running Tests

```bash
.venv/Scripts/pytest tests/ -v    # Windows
.venv/bin/pytest tests/ -v        # Mac/Linux
```

Tests mock all ML models — no GPU, network, or real audio files required.

CI runs the test suite across Python 3.10–3.14 on every push and PR. Python 3.14 is treated as experimental (non-blocking). A weekly job also runs with the latest available package versions to catch forward-compatibility issues early.

---

## Docker

Run wisper entirely in a container — no Python environment setup, no CUDA DLL hunting.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine + Compose v2 (Linux)
- For GPU: NVIDIA driver on host (`nvidia-smi` must work) + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### Quick start

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

On first run the server will download the Whisper and pyannote models (~2 GB) into `./cache/` — this only happens once.

### Makefile targets

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

### Volume layout

| Local path | Container path | Contents |
|-----------|---------------|----------|
| `./cache/` | `/root/.cache/huggingface` | Downloaded models (~2 GB, persisted) |
| `./data/` | `/data` | `config.toml` + speaker profiles |
| `./input/` | `/app/input` | Your audio files |
| `./output/` | `/app/output` | Transcribed `.md` files |

All directories are created automatically on first run and persist across container restarts.

### Discord recording bot

Record Discord voice channel sessions directly from the web UI. The bot joins your server's voice channel, captures per-user audio, and hands the recording off to the transcription pipeline — no manual file shuffling.

**Prerequisites:**

1. **Create a Discord bot** at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Give it a name (e.g. "Wisper") and go to the **Bot** tab
3. Under **Privileged Gateway Intents**, enable **Server Members Intent** and **Message Content Intent**
4. Copy the bot token — set it as `DISCORD_BOT_TOKEN` in your `.env` file
5. **Invite the bot** to your server: go to **OAuth2 → URL Generator**, select `bot` + `applications.commands`, bot permissions: **View Channels**, **Connect**, **Speak**. Paste the generated URL in a browser.

**Usage:**

1. Start the server: `make start` (Docker) or `wisper server` (local)
2. Open `http://localhost:8080/record`
3. Optional: expand **Browse bot's channels** to see all guilds and voice channels the bot can see — click any channel to auto-fill the Guild ID and Voice Channel ID fields
4. Select a campaign and voice channel, then click **Start Recording**
5. When the session ends, click **Stop** — the recording appears in **Recordings**
6. On the recording detail page, click **Transcribe** to queue it for processing

The bot joins per-session (not always-on) and auto-rejoins on transient disconnects. Recordings are stored at `./recordings/` (bind-mounted in Docker) alongside your other data.

> **CLI equivalent:** `wisper record start --voice-channel <ID> --campaign <slug>` — see `wisper record --help` for all subcommands.

### Verify GPU passthrough

```bash
docker compose run wisper nvidia-smi
```

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `DISCORD_BOT_TOKEN` | Discord bot token for the recording bot (see [Discord recording bot](#discord-recording-bot)) |
| `WISPER_SIDECAR_JAR` | Absolute path to the JDA sidecar fat JAR (`discord-bot-all.jar`). Overrides the default search path used by `_find_sidecar_jar()`. Useful when the JAR is not in the standard repo or Docker location. |
| `HF_TOKEN` | HuggingFace token — preferred name (used by Docker `.env` and all HF libraries) |
| `HUGGINGFACE_TOKEN` | Alias for `HF_TOKEN`; both are accepted and propagated to each other |
| `WISPER_DATA_DIR` | Override config/profile storage path — set automatically in Docker |
| `WISPER_DEBUG` | Set to `1` to disable warning suppression and see raw dependency output |
| `ANTHROPIC_API_KEY` | Anthropic API key for `refine` / `summarize` — takes precedence over stored config |
| `OPENAI_API_KEY` | OpenAI API key — takes precedence over stored config |
| `GOOGLE_API_KEY` | Google (Gemini) API key — takes precedence over stored config |
| `OLLAMA_API_KEY` | Ollama Cloud API key (used by `llm_provider = ollama-cloud` for direct ollama.com calls) — takes precedence over stored config |

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

---

## Known Limitations (v1)

- **One active recording at a time.** `BotManager` manages a single Discord voice session — starting a second recording while one is active returns an error.
- **No multi-guild / multi-channel.** The bot connects to one voice channel in one guild per session.
- **DAVE E2EE voice receive depends on JDAVE (Java).** Discord's DAVE protocol encrypts per-user voice — only JDA+JDAVE has confirmed working decrypt as of 2026-05. When [Pycord PR #3159](https://github.com/Pycord-Development/pycord/pull/3159) ships DAVE support, the Java sidecar can be replaced with a ~100-line Python implementation. The Unix-socket wire protocol is the stable interface.
- **Live transcription is deferred to v2.** Recordings are batch-transcribed after the session stops. Five file-format invariants are honoured so v2 can add live transcription without rewriting the recording layer.
- **No auth on web routes.** The existing "trust your LAN" posture applies to recording start/stop controls. Project-wide auth is tracked in the backlog.

