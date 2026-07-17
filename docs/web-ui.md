# Web UI Guide

A full-featured browser interface for wisper. No separate install — included in the same package.

## Starting the UI

```bash
wisper server
# → Open http://localhost:8080
```

---

## Trust Model

The web UI is a **single-user tool with no authentication and no CSRF
protection** — anyone who can reach the port has full read-write control:
uploading and deleting files, changing configuration (including stored API
keys), managing speaker profiles, and starting/stopping Discord recordings.

- By default `wisper server` binds **`127.0.0.1`** (localhost only), so
  nothing on your network can reach it.
- Use `wisper server --host 0.0.0.0` **only on trusted networks** (e.g. a
  home LAN you control). Do not expose the port to the internet.
- The Docker services pass `--host 0.0.0.0` explicitly inside the container;
  access from outside the machine is then governed by Docker's port
  publishing — the default `"8080:8080"` in `docker-compose.yml` publishes on
  **all host interfaces**; change it to `"127.0.0.1:8080:8080"` to keep the UI
  host-local (see [docker.md](docker.md)).
- Adding full CSRF tokens is an explicit non-goal for this single-user tool;
  state-changing endpoints use POST and responses carry defensive headers
  (CSP, `X-Frame-Options: DENY`, `nosniff`), but that is not a substitute
  for network-level trust.

---

## Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Job queue, system status (device, model, HF token), quick upload |
| Transcribe | `/transcribe` | Drag-and-drop upload with a byte-level progress bar (XHR-based, so large files don't leave the browser looking frozen — shows "Uploading… N%" then "Processing…" while the server spools the file and creates the job, then navigates to the job page), all transcription options, live progress stream; **Detect speakers** toggle (on by default — turn off for audiobooks/lectures to skip diarization entirely) reveals a count selector with **?** (auto-detect, default) or pinned **1–10**; the **Whisper model** radio preselects whichever model is set in `wisper config` (falling back to `large-v3-turbo` if the configured model isn't one of the three offered); optional "Refine vocabulary" and "Generate campaign summary" post-processing checkboxes. Submitting an unrecognized model/device/compute-type value (not reachable through the UI itself) redirects back with a generic `?error=invalid_option` rather than queuing a job. |
| Transcripts | `/transcripts` | Browse output files, view rendered markdown, download, delete; green notes icon on cards that have a campaign summary |
| Speakers | `/speakers` | Enroll, rename, remove speaker profiles. Renaming uses the same semantics as `wisper speakers rename`: the profile is re-keyed (embedding and sample clip files move with it) and campaign rosters — including Discord ID bindings — follow automatically. A rename fails with a notice if the new name collides with an existing profile or contains unsupported characters. |
| Campaigns | `/campaigns` | Create and manage campaigns; add/remove roster members; scope transcription to a campaign |
| Record | `/record` | Start and stop live Discord voice channel recording sessions; shows active session with live speaker and segment counts via SSE; **Browse bot's channels** panel lists available guilds and voice channels so you can click-to-fill IDs without leaving the page |
| Recordings | `/recordings` | Browse all recordings, grouped by campaign; view per-recording detail (status, speakers, segments); delete entries |
| Config | `/config` | View and edit all settings |

---

## Speaker Enrollment

The interactive CLI enrollment prompt is replaced by a post-job wizard. After transcription completes, click **Name Speakers** on the job detail page. Each detected speaker has a **Play sample** button so you can hear the voice before assigning a name. Existing profiles are shown as click-to-fill options ranked by voice similarity.

If you reopen the wizard later from the transcript detail page (**Name speakers** in the sidebar), the input fields are pre-filled with the names you applied previously — so you can fix a typo or change an assignment without re-typing every speaker.

Submitting the wizard renames the transcript immediately, then takes you to a live job progress page instead of leaving the tab hanging — extracting a voice embedding per speaker (and, on the first enrollment after a restart, loading the embedding model) used to block the browser for 30–120 seconds with no feedback. The job page streams a log line per speaker being processed and links back to the transcript once enrollment finishes.

For a web upload, the source audio file is kept alongside its transcript in the output folder (instead of being deleted from the temp folder) so the wizard keeps working even after a server restart; it's removed automatically when you delete the transcript. If that audio file is ever missing, the wizard still lets you rename speakers — you'll just see a notice that voice enrollment was skipped (no enrollment job is created in that case).

**Standalone enrollment** (`/speakers/enroll`) — uploading a clean reference clip for a single speaker — also runs as a background job now: submitting the form takes you to a live job progress page (Converting audio → Detecting speech → Extracting embedding) instead of blocking the browser tab while the ML work runs. The new profile appears on the Speakers page once the job completes.

---

## Auto-Enrollment from Recordings

When the Discord bot records a session, any speaker whose Discord user ID is **not** bound to a campaign member is added to the recording's "Unknown Speakers" list. After the session ends, open the recording's detail page (`/recordings/{id}`) to see the panel. Enter a display name next to each unknown Discord ID and click **Enroll** — wisper extracts a voice embedding from their per-user audio track and creates a new speaker profile. This runs as a background job (you're taken to a live progress page); when it completes, the Discord ID is bound to that profile in the campaign roster automatically, so future sessions tag them correctly without manual intervention.

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
- Transcripts can be **deleted** from the Transcripts page (trash icon with confirmation). Deleting a transcript also removes its `.summary.md` sidecar and any per-speaker preview clips (`<stem>_excerpt_*.mp3`/`.txt`) generated for the enrollment wizard.
- The job list keeps at most the 50 most recently finished jobs (completed or failed) — older ones are dropped to bound server memory, oldest first. Pending and running jobs are never affected by this limit. Transcripts themselves are unaffected; this only prunes entries from the in-memory job list.
- When a job fails, the page shows a **generic error message** ("Transcription failed — see server logs", "Enrollment failed", …) rather than the raw exception — exception text can contain server file paths. The full exception and traceback are written to the server log (the terminal running `wisper server`, or the debug log with `--debug`).

---

## Assets & Fonts

All web UI assets (HTMX, Tailwind CSS, fonts) are served from local files — the page itself loads without any external network requests.

The UI uses three self-hosted fonts (all SIL OFL licensed, committed to the repository):
- **Newsreader** — serif display font for titles and long-form reading
- **Geist** — sans-serif body font
- **JetBrains Mono** — monospace font for IDs, timestamps, and CLI flags

HTMX, the fonts, and Tailwind CSS are all committed directly — no download step needed for local dev or Docker builds.
