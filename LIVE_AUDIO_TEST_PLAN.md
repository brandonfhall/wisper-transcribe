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
- [ ] `.wav` files exist and grow under the recording directory:
      ```powershell
      dir $env:APPDATA\wisper-transcribe\recordings\<id>\per-user\mic
      dir $env:APPDATA\wisper-transcribe\recordings\<id>\per-user\system
      dir $env:APPDATA\wisper-transcribe\recordings\<id>\combined
      ```
- [ ] **Stop recording** returns the toolbar to idle within a couple of seconds.
- [ ] `/recordings` shows a **LOCAL** badge, status **NEEDS TRANSCRIBE**, and the
      session name.
- [ ] Detail page shows device names, duration, and a populated Segments panel.
- [ ] `combined.wav` has mic + system mixed with no gaps or time compression.
- [ ] `per-user/mic/0000.wav` is only your voice; `per-user/system/0000.wav` is
      only system audio, with silence in any gaps.
- [ ] Starting a Discord recording while a local session is active is rejected.

### 1b. Live transcript + noise floor

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

## 2. Campaign journal

**Setup:** a campaign with at least one transcript that has a `.summary.md`
sidecar (`wisper summarize`, or "Generate campaign summary" at upload).

### 2a. CLI

- [ ] `wisper campaigns journal <slug>` folds the next session into
      `campaigns/<slug>/journal.md`.
- [ ] Running it again reports nothing pending.
- [ ] `--all` folds every pending session, oldest first.
- [ ] `--session <stem>` folds one specific session.
- [ ] `journal.md` frontmatter lists `journaled_sessions:`; the body has Story
      So Far / Active Threads / NPCs / Party & Decisions / Loot & Resources.

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
