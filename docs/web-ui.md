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
| Job history | `/jobs/history` | Every job ever run, filterable, with each job's settings, result, and last log lines (see [Job page](#job-page)) |
| Transcripts | `/transcripts` | Recordings awaiting transcription; browse, read, play back (see [Playing a transcript](#playing-a-transcript)), download, edit, and delete transcripts; tick rows to delete them or move them to a campaign in bulk |
| Speakers | `/speakers` | Enroll, rename, and remove speaker profiles; play reference clips. Each card shows how many transcripts name the speaker, when they were last heard, and when the profile was enrolled. Profiles from an older speaker model show **NEEDS RE-ENROLL** |
| Search | `/search` | Full-text search over every transcript and session summary (see below). The box at the top of the sidebar searches from any page |
| Campaigns | `/campaigns` | Campaigns, rosters, episode order, and the rolling journal |
| Record | `/record` | Start and stop Discord or local recording sessions |
| Recordings | `/recordings` | Browse recordings by campaign; detail, transcribe, and delete |
| Config | `/config` | All settings, including LLM provider and Discord bot |

Transcript pages are addressed by their database id (`/transcripts/{id}`). Old links that named a session still work: when exactly one transcript has that name the link redirects to the id; when several do (the same name in two campaigns) it lists them to choose from.

While a recording is active, every page shows a banner with the elapsed time and a **Stop recording** button.

---

## Transcribing

Drag a file onto `/transcribe` and choose options:

- **Whisper model** — preselected from your config (`large-v3-turbo` if the configured model isn't one of the options shown).
- **Detect speakers** — on by default. Turn it off for audiobooks or lectures to skip diarization. When on, pick **?** (auto-detect) or pin a count of 1–10.
- **Campaign** — restricts speaker matching to that campaign's roster, and chooses the folder the transcript is written into.
- **Refine** / **Summarize** — LLM post-processing that runs after transcription in the same job.

Large uploads show a byte-level progress bar ("Uploading… N%", then "Processing…") before the job page opens.

A transcript uploaded with a campaign is written into that campaign's folder in the transcripts folder; without one it goes to the transcripts folder's root. A campaign whose folder name is taken by a folder wisper doesn't own refuses the upload ("folder taken"); the campaign page lists it under Needs attention. An upload, a recording hand-off, or a journal or relabel run that can't be written to job history is refused ("submit failed") and queues nothing, so moving or renaming a session can't miss it.

**Name already taken:** the transcript is named after the file. If a transcript or an audio file (`<name>.flac`) with that name exists in the target folder, the page says so as soon as you pick the file (which campaign it's in, and when the existing file was last modified), before anything uploads. A transcript of that name that is missing (renamed or deleted outside wisper) is flagged too: overwriting replaces its audio and speakers, so relink it first to keep them. Choose **Overwrite it** to replace it — it keeps its campaign place, and if it was folded into the campaign journal the journal is marked as needing a rebuild — **Keep both** to save the new run as `<name> (2)`, or **Cancel** and rename the file. Overwrite is offered only when the existing file is a wisper transcript; a stray `.flac` with no transcript, or a name that would collide with the campaign journal, can only be kept as a new copy. A job never reports success without writing its transcript: if a same-named transcript appears while the job runs, the job fails with "Transcript already exists", and a job whose transcript file isn't there afterwards fails with "Transcript file missing after write" (the job log shows the transcripts folder it used).

### Job page

- A progress bar with per-step pills: **T**ranscribe → **D**iarize → **A**lign → **F**ormat, plus **R**efine / **S**ummarize when requested. **Align** appears only when forced word alignment will run for the job (the setting is fixed when you submit, so changing the Config page while a job is queued doesn't change it). Enrollment jobs show **E**, journal jobs **J**.
- A live log, ETA, and speed. On Apple Silicon (MLX) the Transcribe ETA updates about every 30 s of audio.
- **Stop Job** cancels a pending or running job. A running transcription stops at its next progress update; the GPU may finish its current batch first.
- Failed jobs show a generic message ("Transcription failed — see server logs"). The full error is in the server log (the terminal running `wisper server`, or the `--debug` log file).
- The dashboard shows the 20 newest jobs, including ones from before a restart, each with its campaign (the campaign its transcript is in now, else the one it was submitted with). **all jobs →** opens **Job history** (`/jobs/history`): every job ever run, 50 per page, filterable by type and status, with each job's settings, result, and last log lines. Transcript and campaign pages have a **Jobs** button that filters it to that transcript or campaign; a campaign's list includes the transcriptions of its sessions. Jobs that were queued or running when the server stopped show as "Interrupted by restart"; they are not resumed.

What a job keeps: the transcript, the summary, the speaker clips, and the audio as `<name>.flac` beside the transcript. The uploaded file itself isn't kept: wisper extracts its first audio track, deletes the upload, and saves that track as a 16 kHz mono FLAC. A recording keeps its `combined.wav` and the transcript plays from it.

Transcripts are written to the transcripts folder (`output/` in the data directory unless `output_dir` / `WISPER_OUTPUT_DIR` says otherwise; see [configuration.md](configuration.md#transcripts-folder)) and appear on the Transcripts page as soon as the job finishes. Files you add to that folder yourself appear too.

---

## Playing a transcript

A transcript with audio shows a player bar at the top of its page. An upload's audio is its kept `<stem>.flac`. A transcript made from a recording plays that recording's `combined.wav`. A transcript with no audio on disk shows no player.

- **Highlight.** The block being spoken is highlighted as the audio plays.
- **Follow along.** The page scrolls to keep the highlighted block centered. Scrolling yourself turns it off, and the button turns it back on. The button appears only when the transcript has timestamps.
- **Click to seek.** Click any timestamped block to play from its start. Links inside a block still open normally.
- **Deep links.** Opening a page at `#b-<n>` (a search result does) cues the audio to that block without playing it.
- **Markers.** A transcript made from a recording lists the session's markers in the bar as `H:MM:SS` buttons that seek there. A marker's time is wall-clock time since the session started, while `combined.wav` joins audio frames, so over a long session a marker can land a few seconds off. Use it for "roughly here".

---

## Re-transcribing a transcript

A transcript with audio has a **Re-transcribe** button in its toolbar. It asks first, then runs the session again from the saved audio and replaces the transcript in place.

- **Kept:** the transcript's name, its campaign place, and its recording link.
- **Reset:** speaker names you set by hand. If the transcript was folded into the campaign journal, the journal is marked as needing a rebuild.
- **Reused from the original run:** speaker counts, language, timestamps, and the refine and summarize options.
- **Taken from the current config:** the model, device, VAD, and word alignment.
- **Not reused:** custom vocabulary and prompts from the original upload.
- **No audio:** the page says the transcript has no saved audio to re-transcribe from.
- **Recording-linked:** a transcript made from a recording reruns from the recording's `combined.wav`, the same as **Transcribe** on the recording.

---

## Moving and renaming a transcript

A transcript's page has a **Campaign** box and a **Rename** field. The campaign box moves the session into that campaign's folder (or into the transcripts folder's root for **— Uncategorized —**). **Rename** renames the `.md` and its files together. A **Move files** button appears when the session's files aren't in its campaign's folder yet.

- Both actions move the `.md`, its summary, its speaker data and clips, its audio copy, and its backup together. The database changes first, then the files follow; a file another program holds open stays behind, and the session reads under **Needs attention** until **Move files** finishes it.
- A name already in the target folder is never replaced silently. The page shows the existing file's last-modified time and offers **Overwrite** (only when it's a wisper transcript; it deletes that session first), **Keep both** (saves as `<name> (2)`), or **Cancel**. In a bulk move from the Transcripts page, a clashing session stays put and the result counts it as skipped.
- A move, a rename, or a bulk move is refused while a job is pending or running for the session or its campaign, or while a recording of the campaign is capturing; the page says so and nothing changes.
- A link typed in your notes points at the old file name, so it stops working after a rename; wisper doesn't rewrite the text of your notes.

---

## Searching

Type in the sidebar box or open `/search`. Every transcript's title and text, and its session summary, are searched.

- **Matching.** Words match their other forms ("fights" finds "fight"), and accents are ignored ("cafe" finds "café"). Every word must appear in the same speaker block or summary section. Put `"double quotes"` around a phrase, and end a word with `*` to match a prefix (`Stra*`). Anything else, including `-`, `OR`, `NEAR`, and `:`, is searched as ordinary text.
- **Results** are grouped by transcript, best match first, 20 transcripts per page, with up to three matching blocks each. Each block shows the speaker, the timestamp, and a snippet with the matched words highlighted. Transcripts whose title matches come first, with a **TITLE** row that opens the transcript at the top. Highlighting is approximate; a result can match on a word form that isn't highlighted.
- **Filters:** campaign, speaker, and transcripts or summaries only.
- **Opening a result** jumps to that block in the transcript, or that section of the summary, and highlights the words there.
- **Indexing.** Transcripts written or edited in the web UI or CLI are searchable immediately. After an upgrade, or when files are added while the server is stopped, they are indexed in the background, and the page header shows **INDEXING N OF M** until that finishes. Files edited outside wisper (for example in Obsidian) are reindexed when the Transcripts or Campaign page next loads, or when a search result shows **Transcript changed — reindexing**. Transcripts flagged missing aren't searched.

---

## Naming Speakers (Enrollment)

After a transcription, click **Name Speakers** on the job page, or **Name speakers** in a transcript's sidebar.

- Each detected speaker has a **Play sample** button and the words heard in that clip.
- Existing profiles appear as click-to-fill options, ranked by voice similarity.
- Reopening the wizard later pre-fills the names you already applied, so you can fix one without retyping the rest.
- Submitting renames the transcript immediately, then opens a job page while voice embeddings are extracted.

The wizard enrolls from voice data saved when the transcript was made, so it doesn't re-read the audio. A speaker with no saved voice data is enrolled from the transcript's audio; if that's gone too, the speaker is renamed but not enrolled, and the others still are.

**Across a campaign:** naming someone in one session's wizard also renames them in the campaign's other sessions wherever their name was assigned automatically. Names you typed are never changed. The Campaign page's **Re-match speakers** button runs the full pass as a job: it re-matches every session against the roster (re-extracting voice data from the saved audio for sessions that have none stored), and gives an unknown voice heard in two or more sessions one shared name, **Recurring Speaker N**. Name them once and the other sessions follow.

**Standalone enrollment** (`/speakers` → Enroll) takes a clean reference clip for one speaker and runs as a background job.

**Per-line edits:** `/transcripts/{id}/edit` reassigns individual lines to a different speaker.

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
- **Campaign:** a recording shows the campaign its transcript is in. Moving the transcript to another campaign moves the recording with it. Before a transcript exists, it shows the campaign chosen when recording started.
- **Re-transcribe** asks first, then replaces the recording's transcript (same name, same campaign place). If a transcription fails or you stop it, the recording is simply transcribable again.
- **What a recording keeps:** its `combined.wav`, plus `live_transcript.md` (the live draft) for a session recorded on this computer. When a session ends, wisper checks that `combined.wav` holds all the captured audio, then deletes the one-minute pieces and the separate mic and system tracks. A Discord speaker's own track stays until that speaker is bound to a profile, because **Enroll** reads it. Enrolling a speaker whose track is already gone shows an error.
- **Recover recording:** if wisper stopped unexpectedly mid-session (crash, power loss, killed process), the recording shows **FAILED** but its audio is still on disk. Its page offers **Recover recording**, which stitches the saved one-minute pieces back together; the recording then shows **COMPLETED** and can be transcribed. The last partial minute may be missing.
- Deleting a recording's transcript from `/transcripts` puts the recording back under **Awaiting transcription**.
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
- **View journal** also has **Download**, which saves the journal with the list of folded sessions added to its frontmatter.
- **Rebuild journal** starts the journal over from each session's existing summary — one LLM call per session (plus one for any session not yet summarized). Your edits to summaries are kept. It asks for confirmation, showing the call count.
- **Rebuild from transcripts** re-summarizes every session first, overwriting the summaries, then rebuilds — two LLM calls per session. Use it after changing model or provider, or when the summaries themselves are bad.
- **Stale journal:** moving a folded session to another campaign, removing it, deleting it, or re-transcribing it leaves the journal text alone and shows an amber notice ("This journal mentions sessions that were moved, removed, or re-transcribed…") on the Campaign and Journal pages. **Rebuild journal** is highlighted until you rebuild. Nothing is regenerated automatically.
- The journal is a file named `<campaign folder> Journal.md` in the campaign's folder inside the transcripts folder. Editing it yourself (e.g. in Obsidian) is fine; later folds build on your edits. Deleting it starts a fresh journal.

**Episode order:** the ▲/▼ arrows on the Episodes list set the order sessions are folded in. Order is when a transcript was added to the campaign, not its date, so check it before rebuilding.

**Renaming a campaign** changes its display name, page URL, folder on disk, and journal file together. The Rename form is in the Campaign page header; a name whose folder is taken is refused. If the folder can't move (a file in it is open in another program) the name and folder change stay pending and the campaign's Needs attention entry offers **Retry**; when both the old and new folders are gone, **Finish without folder** commits the new name and folder in the database and makes the folder on the next write.

**Deleted transcripts:** deleting a transcript in wisper (single, **Delete selected**, or a recording with its files) removes it from its campaign and its companion files (summary, speaker clips, audio copy). A transcript file that disappears some other way — deleted in Finder, renamed in Obsidian, a sync still in progress, an unplugged drive — keeps its place and shows as **MISSING** on the Campaign page. If the file comes back, the flag clears on its own. If it was renamed, wisper matches it on the next start when the file is unchanged; otherwise pick the new file in the **Relink** dropdown next to it: the session keeps its place, journal entry, speaker names, and its summary, speaker clips, and audio. Otherwise remove it with ✕. Removing a session from a campaign moves its files to the transcripts folder's root, keeping its name (and ` (2)` on a clash); it is refused while a job is running for it or while a file is open in another program.

**Deleting a campaign** asks which of two you want. **Delete campaign, keep the files** moves its sessions to the transcripts folder's root (a name already there becomes `<name> (2)`); its journal file and folder stay on disk (a folder left empty is removed), and creating a campaign of that name again re-claims the folder and its journal. **Delete campaign and everything in it** lists the sessions and deletes the campaign, every one of them with its summary, speaker clips, and audio, and its journal; the folder is removed when nothing is left in it. If a session can't be deleted or moved (another program has it open), the campaign is kept with what's left and the message names the sessions. wisper never deletes a folder holding files it doesn't own.

**Needs attention:** the Transcripts page shows this panel only when something needs you. Nothing in it is deleted automatically.
- **Missing transcripts** have a **Relink** dropdown (the renamed file brings its summary, speaker clips, and audio along) and a **Remove** button that deletes the transcript and its files.
- **Files gone from disk** (a summary or clip you deleted by hand) have a **Forget** button that stops tracking them.
- **Not in their campaign's folder:** sessions still in the transcripts root (or elsewhere) that a campaign holds. Run `wisper storage trim --apply`, or move them with **Move files** on the session's page.
- **Folder renames to finish:** a campaign folder rename that didn't complete because a file in it is open. Close it and **Retry**; when both the old and new folders are gone, **Finish without folder** commits the database change and makes the folder on the next write.
- **Folder taken:** a folder with the campaign's name already exists and isn't wisper's. Rename the campaign, or **Use this folder** — every `.md` file in it becomes a session of that campaign.
- **Journals to move:** a journal from an older install that couldn't be moved into its campaign folder.
- **Campaign folders missing:** a campaign's folder is gone from the transcripts folder. Rename the campaign to match the folder, or **Recreate folder**.
- **Files with no transcript** show their size and modified time. Summaries, speaker data, clips, backups, and `.flac` audio in a scanned folder have a **Delete** button; a file in wisper's data folder, such as a recording's `combined.wav`, is listed only.
- wisper logs the counts at startup.

---

## Settings (Config page)

- All `config.toml` settings, including forced word alignment (`auto` / `true` / `false`), the LLM provider, model, and API keys. Choice fields ignore values outside their list. A blank API-key field keeps the stored key; env vars take precedence.
- The model field lists installed models for Ollama and LM Studio, the Ollama Cloud catalog, and (once a key is entered) Anthropic, OpenAI, and Google models.
- Discord bot token, default guild/channel, and presets.
- **Open data folder** opens the data directory in your file manager.

---

## Assets

All assets — HTMX, Tailwind CSS, and self-hosted fonts (Newsreader, Geist, JetBrains Mono, Instrument Serif; SIL OFL) — are committed and served locally. The UI makes no external network requests.
