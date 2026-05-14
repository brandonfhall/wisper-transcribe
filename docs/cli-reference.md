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

Entering `r` replays the clip. Entering a number reuses an existing profile. Profiles are ranked by voice similarity — `★` marks any match above the confidence threshold.

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

## Commands

### `wisper setup`

Guided first-run wizard. Run this once after installation:

```bash
wisper setup
```

Checks ffmpeg, detects your GPU (CUDA/MPS/CPU), prompts for your HuggingFace token, and pre-downloads all pyannote models (~700 MB, cached permanently).

---

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

---

### `wisper enroll`

Add a speaker from a clean reference clip (e.g. an interview or isolated recording):

```bash
wisper enroll "Alice" --audio alice_intro.mp3
wisper enroll "Alice" --audio session01.mp3 --segment "0:30-1:15"
wisper enroll "Alice" --audio session08.mp3 --update   # blend with existing profile
```

---

### `wisper speakers`

```bash
wisper speakers list                    # show all enrolled profiles
wisper speakers remove "Alice"          # delete a profile
wisper speakers rename "Alice" "Alicia" # rename a profile
wisper speakers reset                   # delete ALL profiles and embeddings (with confirmation)
wisper speakers test session03.mp3                         # preview match results without writing output
wisper speakers test session03.mp3 --campaign d-d-mondays  # restrict to campaign roster
```

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
wisper campaigns delete d-d-mondays                     # delete campaign (with confirmation)
```

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

Organize and view transcript-to-campaign associations from the command line:

```bash
wisper transcripts list                          # list all transcripts, grouped by campaign
wisper transcripts list --campaign d-d-mondays  # show only transcripts for a specific campaign
wisper transcripts move session12 --campaign d-d-mondays   # assign a transcript to a campaign
wisper transcripts move session12 --no-campaign            # remove campaign association
```

- `session12` is the transcript stem (filename without `.md`).
- A transcript can belong to at most one campaign at a time.

---

### `wisper fix`

Fix a wrong speaker assignment in an existing transcript:

```bash
wisper fix session05.md --speaker "Unknown Speaker 1" --name "Frank"
wisper fix session03.md --speaker "Alice" --name "Diana"
```

Add `--re-enroll` to also update the voice profile (currently prompts manual steps).

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

Options: `--tasks`, `--provider {ollama,lmstudio,anthropic,openai,google}`, `--model NAME`, `--endpoint URL` (ollama/lmstudio), `--dry-run/--apply`, `--no-color`.

Safety: YAML frontmatter is never sent to the LLM and is preserved byte-for-byte. Network failures soft-fail with a warning and leave the transcript untouched.

---

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

With `--refine`, vocabulary edits are applied in place (same `.md.bak` guarantee as `wisper refine --apply`) before summarization. If the refine step fails, the summary is still written with `refined: false` in its frontmatter.

---

### `wisper config`

```bash
wisper config show                        # print all settings (API keys masked as ***)
wisper config set model large-v3          # use the big model by default
wisper config set hf_token hf_abc123...   # store HuggingFace token
wisper config set similarity_threshold 0.70  # stricter speaker matching
wisper config path                        # show where config.toml lives
wisper config llm                         # interactive wizard: provider + model + key/endpoint
```

**`wisper config llm`** is the recommended way to configure `refine` / `summarize`. It walks you through the provider (Ollama / Ollama Cloud / LM Studio / Anthropic / OpenAI / Google), endpoint (local providers), model name, and API key (cloud providers) in one flow. For Ollama and LM Studio the wizard lists installed/loaded models so you can pick by number.

**Ollama Cloud — two paths:**
1. Keep `llm_provider = ollama` and pick a model with `-cloud` suffix (e.g. `gpt-oss:120b-cloud`); the local daemon proxies the call to ollama.com.
2. Set `llm_provider = ollama-cloud` and supply `OLLAMA_API_KEY`; wisper calls `https://ollama.com/api/chat` directly with no local daemon required.

Relevant keys: `llm_provider`, `llm_model`, `llm_endpoint`, `llm_temperature`, `anthropic_api_key`, `openai_api_key`, `google_api_key`, `ollama_cloud_api_key`.

---

### `wisper record`

Control the Discord recording bot from the command line. The wisper server must be running first.

```bash
wisper record start --voice-channel <ID> --guild <ID>           # join a channel and start recording
wisper record start --voice-channel <ID> --guild <ID> --campaign d-d-mondays
wisper record start --preset "Weekly D&D"                       # use a saved preset
wisper record stop                                              # stop the active session
wisper record list                                              # list all recordings
wisper record show <recording_id>                               # show metadata for a recording
wisper record transcribe <recording_id>                         # re-queue transcription
wisper record delete <recording_id>                             # remove recording entry (files kept)
```

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

---

## Supported Formats

**Audio:** `.mp3` `.wav` `.m4a` `.flac` `.ogg`

**Video:** `.mp4` `.m4v` `.mkv` `.mov` `.avi` `.webm` `.flv` `.ts` `.mts` `.m2ts`

Video files are handled by extracting only the **first audio track** (`ffmpeg -map 0:a:0`). This works correctly with multi-track recordings where track 0 is a combined mix. Your original files are never modified.

All formats are converted to 16kHz mono WAV internally before transcription.

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
