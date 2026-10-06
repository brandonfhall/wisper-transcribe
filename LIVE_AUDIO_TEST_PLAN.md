# Manual Test Plan — Live Recording + Campaign Journal

Real-device and browser checks that unit tests can't cover. Everything here is
already covered by `pytest`; this list confirms it behaves on real hardware and
in a real browser.

When a box fails, note: expected vs. actual, the server log at that moment, and
OS + GPU/CPU.

---

## 0. Setup

```powershell
.venv\Scripts\pip install -e '.[live]'
$env:WISPER_DEBUG=1; .venv\Scripts\wisper server --reload
```

Run with `WISPER_DEBUG=1` so a debug log exists if the missing-transcript bug
(see 4a) recurs.

Open `http://localhost:8080/record`.

- [ ] **Local capture card** is visible beside the Discord card. If missing,
      check the server log for an `enumerate_devices()` warning.
- [ ] Mic dropdown lists your real microphone(s); the OS default is preselected.
- [ ] System-audio dropdown lists at least one loopback device; the OS default
      output's loopback is preselected.
      - **Windows / Linux (PulseAudio or PipeWire):** works out of the box.
      - **macOS:** needs [BlackHole](https://github.com/ExistentialAudio/BlackHole)
        and a Multi-Output Device — see `docs/setup.md`.
- [ ] With at least one enrolled speaker, the **"This is me"** dropdown appears.
      With none, it is absent (not empty).

---

## 1. Live audio

### 1a. Capture

- [ ] Pick a mic + loopback device and set a session name. The name shows on
      the active toolbar, the recording banner on *other* pages, and
      `/recordings`.
- [ ] Talk into the mic while playing system audio for ~15s.
- [ ] While recording, `.wav` files exist and grow under the recording directory (they are deleted once the session ends and the combined track is verified):
      ```powershell
      dir $env:APPDATA\wisper-transcribe\recordings\<id>\per-user\mic
      dir $env:APPDATA\wisper-transcribe\recordings\<id>\per-user\system
      dir $env:APPDATA\wisper-transcribe\recordings\<id>\combined
      ```
- [ ] **Stop recording** returns the toolbar to idle within a couple of seconds.
- [ ] `/recordings` shows a **LOCAL** badge, status **NEEDS TRANSCRIBE**, and the
      session name.
- [ ] Detail page shows **Source: LOCAL** (device names are no longer stored), duration,
      and a populated Segments panel.
- [ ] `combined.flac` has mic + system mixed with no gaps or time compression.
- [ ] After stop, `combined/` and `per-user/` are gone and `combined.flac` remains.
      To check the separate tracks, play `per-user/mic/0000.wav` (only your voice)
      and `per-user/system/0000.wav` (only system audio, with silence in any gaps)
      before pressing stop.
- [ ] Starting a Discord recording while a local session is active is rejected.

**Switching devices mid-session** (needs two inputs, e.g. built-in mic + a USB
mic or headset; on macOS, BlackHole for system audio):

- [ ] Start a session on mic A. Mid-session, pick mic B in the active panel's
      **Switch** selects and press **Switch**: the status line says
      "Switched." and the session keeps recording (no new recording in
      `/recordings`).
- [ ] Speak on mic B: the mic level meter moves and the live ticker keeps
      producing lines. Changing only the system track leaves the mic track
      untouched (no gap in it).
- [ ] Pressing **Switch** without changing a select says "Pick a different
      device first." and does nothing.
- [ ] Unplug mic A mid-session: the session goes **DEGRADED**. Switch the mic
      to another device: it returns to recording. If instead you switch only
      the *system* track, it stays degraded.
- [ ] After stop, `combined.flac` plays through the switch with a short gap at
      most, and the transcript covers speech from both mics.

### 1b. Live transcript + noise floor

- [ ] Rename a speaker profile to `<b>Ann</b>` and record with it bound: the
      live ticker shows the name as literal text `<b>Ann</b>`, not bold.

- [ ] A new local session shows the **"Live · near-real-time"** pane, initially
      "Waiting for speech…".
- [ ] Moving the noise-floor slider moves the threshold marker on both level
      gauges; a bar turns green when its level clears the marker.
- [ ] Mic speech appears as **You** within a few seconds of each pause; system
      speech appears as **Other**.
- [ ] Talking over system audio labels each line by whichever track was louder.
- [ ] No repeating exceptions in the server log (per-chunk errors are logged,
      not fatal).
- [ ] Lines lag no more than ~15–20s on CPU. If they do, try `base` or `small`.
- [ ] **Add marker** drops a rose, speaker-less line into the ticker.
- [ ] After stopping, `recordings/<id>/live_transcript.md` matches the UI, and
      reloading the detail page shows the persisted draft.

### 1c. "This is me"

*Skip if you have no enrolled profiles.*

- [ ] With your name selected, mic-dominant lines show your name instead of
      "You"; system lines still show "Other".

### 1d. Full transcribe hand-off

- [ ] **Transcribe** on a stopped session produces a diarized transcript.
- [ ] The transcript's title is the session name, not the recording UUID.
- [ ] The transcript `.md` exists at the path the job page links to (see 4a).
- [ ] `/transcripts` → **"Awaiting transcription"** lists un-transcribed local
      recordings, each with a Transcribe button.

---

### 1e. Discord channel picker

- [ ] With a bot token set, the Record page's Discord form shows a **Guild**
      select, then a **Voice channel** select filtered to that guild. Starting
      a recording uses the chosen channel.
- [ ] A preset or the configured default guild/channel is pre-selected.
- [ ] **Enter IDs manually** shows the raw ID fields; **Show channel list**
      switches back.
- [ ] Remove the bot token (or set a wrong one): the form says so and shows the
      raw ID fields, with no way to the (empty) list. Save-as-preset still
      picks up typed IDs.

---

## 2. Campaign journal

**Setup:** a campaign with at least one transcript that has a `.summary.md`
sidecar (`wisper summarize`, or "Generate campaign summary" at upload).

### 2a. CLI

- [ ] `wisper campaigns journal <slug>` folds the next session into
      `<campaign folder>/<campaign folder> Journal.md` in the transcripts folder.
- [ ] Running it again reports nothing pending.
- [ ] `--all` folds every pending session, oldest first.
- [ ] `--session <stem>` folds one specific session.
- [ ] The journal body has Story So Far / Active Threads / NPCs /
      Party & Decisions / Loot & Resources. Folded sessions are tracked in the
      database; `--export` prints the journal with `journaled_sessions:` added.

### 2b. Web

- [ ] `/campaigns/<slug>` shows the **Rolling journal** panel with a pending
      count.
- [ ] **Update journal** opens a job page with a single **Journal (J)** step.
- [ ] On completion the job page links **View journal** (not "View transcript").
- [ ] `/campaigns/<slug>/journal` renders sanitized HTML with no raw
      frontmatter.
- [ ] **Fold all** with 2+ pending sessions folds them all in one job.
- [ ] With nothing pending, the panel says so instead of queuing a no-op job.
- [ ] **Rebuild journal** (with confirm) re-summarizes every session and rebuilds
      the journal from scratch.
- [ ] ▲/▼ on the Episodes list reorders sessions and persists on reload.

### 2c. Combined summary and recaps

**Setup:** the same campaign, with a real LLM configured and 2+ summarized
sessions.

- [ ] **Generate combined summary** runs a job; the Campaign page then links
      the summary, and `<folder> Combined Summary.md` sits in the campaign
      folder. The page renders it as sanitized HTML with no raw frontmatter;
      **Download** saves it under that name.
- [ ] Re-summarize one session (or add a summarized one): the Campaign page
      shows the combined summary as stale. Regenerating clears it.
- [ ] **Write recap** with the selector at 2: a 200–400 word player-facing
      recap of the last two sessions, no DM-only notes or future plans, saved
      as `<folder> Recap — <newest session>.md`. Download works (the name has
      an em dash).
- [ ] Write another recap for the same newest session: it replaces that file.
      After a newer session is summarized, a new recap adds a second file;
      both are listed, newest first.
- [ ] `wisper campaigns summarize <slug>` and
      `wisper campaigns recap <slug> --sessions 3` do the same from the CLI.
- [ ] The buttons are disabled for a campaign with no summarized session.

### 2d. Campaign delete and re-create

- [ ] **Delete campaign, keep the files**: the journal, combined summary, and
      recaps stay in the folder; sessions move to the transcripts root.
- [ ] Create a campaign with the same name: it re-claims the folder, and its
      journal and documents are back (journal panel populated; the documents
      are registered, so a later full delete removes them).
- [ ] **Delete campaign and everything in it**: the folder's documents and the
      folder go.

---

## 3. Recordings management

- [ ] `/recordings` has a checkbox per row (none on active or degraded
      sessions) and a select-all checkbox.
- [ ] Selecting rows shows a **Delete selected** bar with the count.
- [ ] Deleting shows a "permanent" confirm, then removes both the rows and the
      files:
      ```powershell
      dir $env:APPDATA\wisper-transcribe\recordings\<id>   # gone
      dir $env:APPDATA\wisper-transcribe\output\<id>.md    # gone, if it was transcribed
      ```
- [ ] Single delete on a detail page says files will be deleted, and deletes
      them.
- [ ] `wisper record delete <id> --yes` removes the index entry and the files.

---

## 4. Cross-feature

### 4a. Missing transcript file (unresolved — watch for it)

A local-recording Transcribe job once reported COMPLETED ("Wrote `<id>.md`")
but the file never existed on disk. It could not be root-caused afterwards.

- [ ] If it recurs, **don't restart the server**. Save the job's log from its
      job page and the `WISPER_DEBUG` log first.

### 4b. Live transcript on a busy queue

- [ ] Start a long job (e.g. **Rebuild journal**, or a long transcription).
- [ ] Start a local session while it runs. The Record page shows an amber
      "another job is already running" notice.
- [ ] Stop before the other job finishes. The live-transcript job shows
      **FAILED** ("job queue was busy for the whole session"), not COMPLETED.
- [ ] With nothing else running, no notice appears and the live pane fills
      normally.

### 4c. General

- [ ] `/campaigns` and `/record` both load normally.
- [ ] `wisper --help` lists `campaigns journal` and `record` with no import
      errors.

---

## 5. Stop frees the GPU (`ml_worker`)

Rehearsed on Apple Silicon/MPS with real models on 2026-10-06. Still owed on
an NVIDIA/CUDA machine and on Windows.

- [ ] Upload a ~10 min file. While **Transcribe** runs, press **Stop job**: the
      job ends "Cancelled" within a second or two, and the GPU drops to idle
      (`nvidia-smi`, or Activity Monitor's GPU history). On Windows, Task
      Manager shows the `python` worker process gone.
- [ ] Repeat, pressing Stop during **Diarize**.
- [ ] The next job starts normally (the first one after a Stop reloads models,
      so it is slower to start) and completes.
- [ ] Stop the server mid-job: no `python` worker process is left running.
- [ ] Set **Run GPU work in a separate process** off on the Config page: jobs
      still run (in-process), and Stop only takes effect at the next log line.
