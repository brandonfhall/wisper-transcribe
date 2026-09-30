# Web UI Guide

A browser interface for everything the CLI does, plus recording. It ships in the same package.

```bash
wisper server
# → http://localhost:8080
```

---

## Trust Model

The web UI is a **single-user tool with no authentication and no CSRF protection**. Anyone who can reach the port can upload and delete files, change configuration (including stored API keys), manage speaker profiles, and start or stop recordings.

- `wisper server` binds **`127.0.0.1`** by default, so nothing else on your network can reach it.
- Use `--host 0.0.0.0` **only on trusted networks** (e.g. a home LAN you control). Never expose the port to the internet.
- Docker passes `--host 0.0.0.0` inside the container. Reachability is then set by Docker's port mapping: the default `"8080:8080"` publishes on all host interfaces; use `"127.0.0.1:8080:8080"` to keep it local (see [docker.md](docker.md)).
- State-changing endpoints are POST-only and responses carry defensive headers (CSP, `X-Frame-Options: DENY`, `nosniff`), but that doesn't replace network-level trust.

---

## Pages

| Page | URL | What it does |
|------|-----|--------------|
| Dashboard | `/` | Job queue, system status (device, model, HF token, LLM provider), quick upload |
| Transcribe | `/transcribe` | Upload and transcribe (see below) |
| Transcripts | `/transcripts` | Recordings awaiting transcription; browse, read, download, edit, and delete transcripts |
| Speakers | `/speakers` | Enroll, rename, and remove speaker profiles; play reference clips. Profiles from an older speaker model show **NEEDS RE-ENROLL** |
| Campaigns | `/campaigns` | Campaigns, rosters, episode order, and the rolling journal |
| Record | `/record` | Start and stop Discord or local recording sessions |
| Recordings | `/recordings` | Browse recordings by campaign; detail, transcribe, and delete |
| Config | `/config` | All settings, including LLM provider and Discord bot |

While a recording is active, every page shows a banner with the elapsed time and a **Stop recording** button.

---

## Transcribing

Drag a file onto `/transcribe` and choose options:

- **Whisper model** — preselected from your config (`large-v3-turbo` if the configured model isn't one of the options shown).
- **Detect speakers** — on by default. Turn it off for audiobooks or lectures to skip diarization. When on, pick **?** (auto-detect) or pin a count of 1–10.
- **Campaign** — restricts speaker matching to that campaign's roster.
- **Refine** / **Summarize** — LLM post-processing that runs after transcription in the same job.

Large uploads show a byte-level progress bar ("Uploading… N%", then "Processing…") before the job page opens.

### Job page

- A progress bar with per-step pills: **T**ranscribe → **D**iarize → **A**lign → **F**ormat, plus **R**efine / **S**ummarize when requested. **Align** appears only when forced word alignment will run for the job (the setting is fixed when you submit, so changing the Config page while a job is queued doesn't change it). Enrollment jobs show **E**, journal jobs **J**.
- A live log, ETA, and speed. On Apple Silicon (MLX) the Transcribe ETA updates about every 30 s of audio.
- **Stop Job** cancels a pending or running job. A running transcription stops at its next progress update; the GPU may finish its current batch first.
- Failed jobs show a generic message ("Transcription failed — see server logs"). The full error is in the server log (the terminal running `wisper server`, or the `--debug` log file).
- The job list keeps the 50 most recently finished jobs. Transcripts themselves are never pruned.

Transcripts are written to `./output/` (or `<data dir>/output`) and appear on the Transcripts page as soon as the job finishes.

---

## Naming Speakers (Enrollment)

After a transcription, click **Name Speakers** on the job page, or **Name speakers** in a transcript's sidebar.

- Each detected speaker has a **Play sample** button and the words heard in that clip.
- Existing profiles appear as click-to-fill options, ranked by voice similarity.
- Reopening the wizard later pre-fills the names you already applied, so you can fix one without retyping the rest.
- Submitting renames the transcript immediately, then opens a job page while voice embeddings are extracted.

For web uploads, the source audio is kept next to its transcript in the output folder so the wizard works after a server restart; it's deleted with the transcript. If that audio is missing, renames still apply and a notice says voice enrollment was skipped.

**Across a campaign:** naming someone in one session's wizard also renames them in the campaign's other sessions wherever their name was assigned automatically. Names you typed are never changed. The Campaign page's **Re-match speakers** button runs the full pass as a job: it re-matches every session against the roster (re-extracting voice data from the saved audio for sessions transcribed before this feature), and gives an unknown voice heard in two or more sessions one shared name, **Recurring Speaker N**. Name them once and the other sessions follow.

**Standalone enrollment** (`/speakers` → Enroll) takes a clean reference clip for one speaker and runs as a background job.

**Per-line edits:** `/transcripts/{name}/edit` reassigns individual lines to a different speaker.

---

## Recording

Only one recording — Discord or local — can run at a time.

### Discord

The bot joins a voice channel, records each participant on their own track plus a mixed track, and hands the result to the normal transcription pipeline. Setup (bot token, invite, Java 25) is in [docker.md](docker.md#discord-recording-bot).

- Save a guild/channel pair as a preset from the Record page.
- The Record page shows who is talking in real time.

**Auto-enrollment:** Discord users not yet bound to a campaign member are listed under "Unknown Speakers" on the recording's detail page. Enter a name and click **Enroll** to create a profile from their track; the Discord ID is then bound in the campaign roster, so future sessions tag them automatically.

### Local (mic + system audio)

Records your microphone and the machine's system audio (the other side of a call, a video, a game) as two tracks plus a mix, with a live transcript preview. Requires the `[live]` extra and a native install — see [setup.md](setup.md#local-recording-mic--system-audio) for per-OS setup, including BlackHole on macOS.

The **Local capture** card appears only when the extra is installed and devices are detected. To start:

1. Pick a microphone and a system-audio device (your OS defaults are preselected).
2. Optionally set a **session name** (used as the transcript title) and a campaign.
3. Optionally pick yourself under **This is me** so your lines show your name instead of "You". The choice is remembered.
4. Click **Start local recording**.

While recording:

- **Live preview** — lines appear a few seconds after each pause, labelled **You** (mic) or **Other** (system audio) by whichever track is louder. It is a draft, not diarization: several people on the system side all appear as "Other".
- **Noise floor** slider and **level gauges** — audio below the floor is ignored, so room noise isn't transcribed as speech. A gauge turns green when its level clears the floor. Changes apply immediately.
- **Add marker** — bookmarks the current moment; markers are listed on the recording's detail page.

On CPU-only machines, use `base` or `small` so the preview keeps up.

If another job is already running when you start, the Record page warns that the live preview won't begin until that job finishes. The recording itself is unaffected.

### After recording

- Stopping never starts transcription automatically. Click **Transcribe** on the recording (or from **Transcripts → Awaiting transcription**) to run the full diarized pass. The live draft stays on the recording's detail page until then.
- **Deleting a recording is permanent.** Single delete and **Delete selected** both remove the audio and, if it was transcribed, the transcript and its sidecars. Active sessions can't be deleted.

---

## LLM Post-Processing

Configure a provider on the Config page first (or run `wisper config llm`).

**At transcription time:** tick **Refine** and/or **Summarize** on the Transcribe form.

**Afterwards:** open a transcript and click **Refine** or **Summarize**. Each queues a job.

**Campaign notes:** when a `.summary.md` exists, the transcript card shows a notes icon and the detail page offers **View Summary** (recap, loot, NPCs, follow-ups) and a download.

### Campaign journal

The Campaign page's **Rolling journal** panel combines session summaries into one living document per campaign: story so far, open threads, NPCs, party decisions, and a loot ledger.

- A session is ready to fold in once it has a `.summary.md`.
- **Update journal** folds the next session; **Fold all** folds every pending one. Each fold is a job.
- **View journal** shows the rendered result.
- **Rebuild journal** re-summarizes every session and rebuilds the journal from scratch — two LLM calls per session, so it asks for confirmation. Use it after changing model or provider.

**Episode order:** the ▲/▼ arrows on the Episodes list set the order sessions are folded in. Order is when a transcript was added to the campaign, not its date, so check it before rebuilding.

**Deleted transcripts:** deleting a transcript (or a recording with its files) also removes it from its campaign. Entries left behind by older versions show as **MISSING** on the Campaign page; remove them with ✕.

---

## Settings (Config page)

- All `config.toml` settings, including forced word alignment (`auto` / `true` / `false`), the LLM provider, model, and API keys. Choice fields ignore values outside their list. A blank API-key field keeps the stored key; env vars take precedence.
- The model field lists installed models for Ollama and LM Studio, the Ollama Cloud catalog, and (once a key is entered) Anthropic, OpenAI, and Google models.
- Discord bot token, default guild/channel, and presets.
- **Open data folder** opens the data directory in your file manager.

---

## Assets

All assets — HTMX, Tailwind CSS, and self-hosted fonts (Newsreader, Geist, JetBrains Mono, Instrument Serif; SIL OFL) — are committed and served locally. The UI makes no external network requests.
