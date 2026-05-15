# Web UI Guide

A full-featured browser interface for wisper. No separate install — included in the same package.

## Starting the UI

```bash
wisper server
# → Open http://localhost:8080
```

---

## Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Job queue, system status (device, model, HF token), quick upload |
| Transcribe | `/transcribe` | Drag-and-drop upload, all transcription options, live progress stream; **Detect speakers** toggle (on by default — turn off for audiobooks/lectures to skip diarization entirely) reveals a count selector with **?** (auto-detect, default) or pinned **1–10**; optional "Refine vocabulary" and "Generate campaign summary" post-processing checkboxes |
| Transcripts | `/transcripts` | Browse output files, view rendered markdown, download, delete; green notes icon on cards that have a campaign summary |
| Speakers | `/speakers` | Enroll, rename, remove speaker profiles |
| Campaigns | `/campaigns` | Create and manage campaigns; add/remove roster members; scope transcription to a campaign |
| Record | `/record` | Start and stop live Discord voice channel recording sessions; shows active session with live speaker and segment counts via SSE; **Browse bot's channels** panel lists available guilds and voice channels so you can click-to-fill IDs without leaving the page |
| Recordings | `/recordings` | Browse all recordings, grouped by campaign; view per-recording detail (status, speakers, segments); delete entries |
| Config | `/config` | View and edit all settings |

---

## Speaker Enrollment

The interactive CLI enrollment prompt is replaced by a post-job wizard. After transcription completes, click **Name Speakers** on the job detail page. Each detected speaker has a **Play sample** button so you can hear the voice before assigning a name. Existing profiles are shown as click-to-fill options ranked by voice similarity.

If you reopen the wizard later from the transcript detail page (**Name speakers** in the sidebar), the input fields are pre-filled with the names you applied previously — so you can fix a typo or change an assignment without re-typing every speaker.

---

## Auto-Enrollment from Recordings

When the Discord bot records a session, any speaker whose Discord user ID is **not** bound to a campaign member is added to the recording's "Unknown Speakers" list. After the session ends, open the recording's detail page (`/recordings/{id}`) to see the panel. Enter a display name next to each unknown Discord ID and click **Enroll** — wisper extracts a voice embedding from their per-user audio track and creates a new speaker profile. The Discord ID is then bound to that profile in the campaign roster automatically, so future sessions tag them correctly without manual intervention.

---

## LLM Post-Processing

**Option 1 — at transcription time:**
In the Transcribe form, expand the Options panel and tick "Refine vocabulary" and/or "Generate campaign summary" under LLM Post-processing. Both run automatically after transcription completes as part of the same job, with Ollama status messages streamed to the progress log.

**Option 2 — from the Transcript detail page:**
Open any transcript and expand "LLM Post-processing". Click **Refine Vocabulary** or **Generate Campaign Summary** to queue a standalone LLM job. You are redirected to the job progress page, which streams status messages in real time.

**Campaign Notes:**
When a `.summary.md` sidecar exists, the transcript detail page shows a green "Campaign Notes available" panel with **View Notes** and **Download** buttons. Campaign notes are also accessible via the transcript list card (green notes icon). The notes page shows the session recap, loot, NPCs, and follow-up items rendered as HTML.

---

## Job Management

- The job detail page shows a **real-time progress bar** with per-phase step indicators. For transcription jobs: Transcribing → Diarizing → Formatting with ETA and speed counter. For LLM jobs: a single step indicator (R for Refine, S for Summarize) with Ollama streaming messages in the log.
- A **Stop Job** button lets you cancel any pending or running job.
- Transcripts are saved to `./output/` (or `data_dir/output`) and are immediately visible on the Transcripts page after the job completes.
- Transcripts can be **deleted** from the Transcripts page (trash icon with confirmation). Deleting a transcript also removes its `.summary.md` sidecar if present.

---

## Assets & Fonts

All web UI assets (HTMX, Tailwind CSS, fonts) are served from local files — the page itself loads without any external network requests.

The UI uses three self-hosted fonts (all SIL OFL licensed, committed to the repository):
- **Newsreader** — serif display font for titles and long-form reading
- **Geist** — sans-serif body font
- **JetBrains Mono** — monospace font for IDs, timestamps, and CLI flags

HTMX, the fonts, and Tailwind CSS are all committed directly — no download step needed for local dev or Docker builds.
