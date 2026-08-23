# Manual Test Plan — PR #58 (live local recording + campaign journal)

Covers everything on `feat/live-audio-recording` as of the merge with
`feat/campaign-journal` (2026-08-23). All of it is unit-tested (1260 passing,
one pre-existing unrelated Windows-only failure — see PR #58's description).
**No real audio device or real Whisper model has touched the live-capture
code yet**, and neither feature has had a real browser click-through this
session — that's what this checklist is for.

Work through it top to bottom. Section 1 is the fast pass — it specifically
exercises the 6 bugs found and fixed today, and shouldn't take more than a
few minutes. Sections 2–4 are the fuller walkthroughs (live-audio hardware,
campaign journal, cross-feature sanity) for whenever you have more time.

---

## 0. Setup

```powershell
.venv\Scripts\pip install -e '.[live]'
.venv\Scripts\wisper server --reload
```

Open `http://localhost:8080/record`.

- [ ] **Local capture card is visible** beside the Discord card. If it's
      missing, `soundcard` failed to import or no mic/loopback devices were
      found — check the terminal log for a warning from `enumerate_devices()`.
- [ ] Mic dropdown lists your real microphone(s).
- [ ] System-audio dropdown lists at least one loopback device.
      - **Windows**: should just work (WASAPI loopback).
      - **Linux**: should just work if PulseAudio/PipeWire is running.
      - **macOS**: requires [BlackHole](https://github.com/ExistentialAudio/BlackHole)
        installed + a Multi-Output Device set as your output first — see `docs/setup.md`.
- [ ] If you have an enrolled speaker profile already, the **"This is me"**
      dropdown appears with your name in it. Zero profiles → dropdown absent
      entirely (not just empty).

**If the device lists look wrong** (loopback devices showing up as
microphones, or vice versa) — `enumerate_devices()`'s `isloopback` filter
was never verified against the real `soundcard` package. Note what you see;
it may need a filter tweak.

---

## 1. Fast pass — today's fixes (do this first)

Each of these is unit-tested, but none has been clicked through in a real
browser. All 6 came out of one review session on 2026-08-22/23.

### 1a. Live pane no longer doubles lines on reconnect
*(fixes: recording-detail page had its own hand-rolled SSE handler missing
the reconnect de-dupe the `/record` ticker already had)*

- [ ] Start a local session, open the recording detail page
      (`/recordings/<id>`), let 3-4 lines come in.
- [ ] Reload the page (F5) while still recording.
- [ ] Confirm every line appears **exactly once** — not doubled. (Before the
      fix, a reload would replay the full history on top of what was
      already there.)

### 1b. Segments manifest is populated
*(fixes: `append_segment()` existed but nothing ever called it — the
Segments section was always empty for every recording, ever)*

- [ ] After a session with **real audio** (local or Discord), open the
      recording detail page and check the **"Segments"** panel — it should
      list actual rows (index, timestamp, duration, sealed/open), not be
      empty.

### 1c. Markers survive to the end of the session
*(fixes: `BotManager`/`LocalCaptureManager` held a long-lived `Recording`
object that silently overwrote markers/segments appended concurrently from
a different call site — confirmed reproducible: a marker added mid-session
came back empty after the session ended)*

- [ ] Start a session, click **"Add marker"** once or twice while it's
      running.
- [ ] Stop the recording.
- [ ] Reload the recording detail page — confirm the marker(s) are **still
      there** under "Markers" (elapsed-time pills). This is the one most
      worth double-checking; it was a real, silent data-loss bug.

### 1d. No-audio session doesn't show a phantom segment
*(fixes: `SegmentedWavWriter.finalize()` always returns a path even for an
empty 0-frame segment — the first version of the fix above appended a
manifest entry for it unconditionally)*

- [ ] Start a local (or Discord) session and stop it **immediately**, before
      any audio is captured.
- [ ] Recording detail page should show **"No combined audio file found"**
      *and* an empty/absent Segments panel — not a contradictory
      "Segments: 1" next to the no-audio error.

### 1e. `ollama-cloud` shows up everywhere it should (merge fix)
*(fixes: after merging in the campaign-journal branch, `cli.py` had two
competing provider-choice lists — the journal command would have silently
kept using a stale one missing `ollama-cloud`)*

```powershell
.venv\Scripts\wisper campaigns journal --help
.venv\Scripts\wisper summarize --help
```

- [ ] Both `--provider` option lists include `ollama-cloud` and match each
      other exactly.

### 1f. Journal job log line count is capped like every other job
*(fixes: `_run_journal_job()` bypassed the R14 log-line retention cap —
no user-visible effect at normal scale, skip unless you're curious)*

- [ ] Nothing to click through here; covered by
      `test_web_jobs.py`. Skip.

---

## 2. Live-audio full walkthrough

### 2a. Phase 1 — Capture layer

- [ ] Pick a mic + a loopback device. Try naming the session
      ("Session name (optional)" field) — confirm the name shows up on the
      active toolbar, the global recording-status banner (visible on
      *other* pages while this session runs), and `/recordings`.
- [ ] Play audio through your system output while talking into the mic for
      ~10-15s.
- [ ] Check the recording directory grew:
      ```powershell
      dir $env:APPDATA\wisper-transcribe\recordings\<recording-id>\per-user\mic
      dir $env:APPDATA\wisper-transcribe\recordings\<recording-id>\per-user\system
      dir $env:APPDATA\wisper-transcribe\recordings\<recording-id>\combined
      ```
      `.wav` files should exist in all three and grow over time.
- [ ] Click **Stop recording**. Toolbar returns to idle within a couple
      seconds.
- [ ] `/recordings` — your session shows a **LOCAL** badge, status
      **NEEDS TRANSCRIBE** (not the old "TRANSCRIBED" mislabel), your
      session name if you set one.
- [ ] Recording detail page — device names shown under "Devices", duration
      shown in the status strip, Segments panel populated (see 1b above).
- [ ] Play `combined.wav` — mic + system mixed continuously, no gaps mid-
      sentence, no time-compression artifacts.
- [ ] Play `per-user/mic/0000.wav` — just your voice.
- [ ] Play `per-user/system/0000.wav` — just system audio, silence during
      any gap (silence-substitution keeping tracks wall-clock aligned).

**Cross-manager exclusion:**
- [ ] Start a local session, then try starting a Discord recording from the
      same page (or `POST /api/record/start`) — should get rejected
      (already-active error), not silently run both.

### 2b. Phase 2 — Live transcript preview + noise floor

- [ ] Start a new local session. The **"Live · near-real-time"** pane
      appears on the detail page, initially "Waiting for speech…".
- [ ] On `/record`'s sidebar, try the **noise-floor slider** — watch the
      live mic/system level gauge (0-2000 RMS scale) and confirm the bar
      turns green once the live value clears the current threshold marker.
- [ ] Talk in short sentences with pauses — lines labeled **You** (cyan)
      should appear within a few seconds of each pause.
- [ ] Play speech through system output — lines labeled **Other**.
- [ ] Talk over the system audio simultaneously — whichever track was
      louder over that segment should win the label (RMS energy compare,
      not real diarization — don't expect perfection).
- [ ] **Watch the server terminal for exceptions** — this is the first time
      real audio hits `find_commit_boundary`'s Silero VAD call and
      `transcribe_array`'s WhisperModel call. Per-chunk errors are caught
      and logged, never crash the session — note anything that repeats.
- [ ] If lines lag more than ~15-20s behind real speech on CPU, your
      configured model is probably too large for real-time — try `base` or
      `small`.
- [ ] Click **"Add marker"** a couple of times — a rose, italic, speaker-
      less flagged line should drop into the ticker immediately.
- [ ] Stop the session. `recordings/<id>/live_transcript.md` should exist
      and match what you saw in the UI.
- [ ] Reload the recording detail page after stopping — the pane should now
      show the **static, persisted draft** (parsed from
      `live_transcript.md`) instead of vanishing — this replaced the old
      "pane disappears the instant status leaves recording" behavior.

### 2c. Phase 3 — "This is me" label

*Skip if you have no enrolled speaker profiles.*

- [ ] Start a session, select your name in **"This is me"**.
- [ ] Mic-dominant live lines show **your name** instead of "You".
- [ ] System-audio lines still show "Other".

### 2d. The payoff — full diarized transcribe hand-off

- [ ] After stopping any session above, click **Transcribe**.
- [ ] Confirms it queues a normal transcription job and produces a real
      diarized transcript for a `combined.wav` from local capture.
- [ ] If you set a session name, confirm the finished transcript's title
      uses it (not the recording's raw UUID).
- [ ] **While that job runs**, try starting a new live session — it should
      queue behind the running job, not run concurrently (one-job-at-a-time
      invariant).
- [ ] `/transcripts` — check the **"Awaiting transcription"** section lists
      any completed-but-not-yet-transcribed local recordings, each with a
      direct Transcribe button.

---

## 3. Campaign journal walkthrough

*No real-device dependency here — this is pure web/LLM, should be low-risk,
but has never been clicked through in a browser either.*

**Setup:** you need a campaign with at least one transcript that has a
`.summary.md` sidecar (`wisper summarize` run on it, or the web
"Generate campaign summary" checkbox at upload time).

### 3a. CLI

```powershell
.venv\Scripts\wisper campaigns journal <slug>
```

- [ ] Folds the next un-journalled session, writes
      `campaigns/<slug>/journal.md`, prints a confirmation.
- [ ] Run it again immediately — should report nothing pending (already
      up to date), not error.
- [ ] `--all` folds every pending session in one run (oldest first) if you
      have multiple summarized sessions queued up.
- [ ] `--session <stem>` folds a specific one out of order.
- [ ] Open `journal.md` — YAML frontmatter has `journaled_sessions:` listing
      what's been folded; body has the five expected sections (Story So
      Far / Active Threads / NPCs / Party & Decisions / Loot & Resources).

### 3b. Web

- [ ] Campaign detail page (`/campaigns/<slug>`) shows the **"Rolling
      journal"** panel with a pending-session count.
- [ ] Click **"Update journal"** — redirects to a live job progress page
      showing a single **"Journal (J)"** step (not the generic
      Transcribe/Diarize/Format steps).
- [ ] On completion, the page shows **"View journal"** (never "View
      transcript" — that was a real bug caught and fixed on the journal
      branch before this merge).
- [ ] Click through — `/campaigns/<slug>/journal` renders the journal as
      sanitized HTML, no raw markdown/frontmatter leaking through.
- [ ] Try **"Fold all"** if you have 2+ pending sessions — one job folds
      all of them, journal reflects all.
- [ ] With nothing pending, confirm the panel/button reflects that state
      sensibly (disabled or a clear "up to date" message) rather than
      queuing a no-op job.

---

## 4. Cross-feature sanity (post-merge)

- [ ] `/campaigns` and `/record` both still load correctly and don't step
      on each other — the merge touched `jobs.py`/`cli.py` fairly deeply on
      both sides.
- [ ] Start a local recording *and* fold a campaign journal around the same
      time — since `JobQueue` is single-worker, the journal job should
      simply queue behind (or ahead of) the live job rather than causing
      any crash or mixed-up state.
- [ ] `wisper --help` — confirm both `campaigns journal` and the live-audio
      CLI surface (if any) show up with no import errors.

---

## What to report back

For each unchecked/failed box, note:
1. What you expected vs. what happened.
2. Anything in the server terminal log at that moment.
3. OS + whether GPU or CPU transcription.

Ranked by how much guesswork was involved (most likely trouble spots first):
1. `enumerate_devices()`'s mic-vs-loopback split (Section 0) — never
   verified against the real `soundcard` API.
2. `_soundcard_capture_factory`'s `mic.recorder(samplerate=...)` /
   `record(numframes=None)` calls (Section 2a) — if broken, per-track WAVs
   will be empty or capture threads will silently die.
3. Live transcript timing/lag on CPU (Section 2b).
4. Section 1c (marker persistence) — highest-value regression check from
   today's fixes; worth confirming even if you skip everything else.

Everything else (mixing math, silence substitution, chunk-cutting logic,
JOB_LIVE lifecycle, cross-manager exclusion, SSE resume, segment-manifest
bookkeeping, journal folding logic) is unit-tested against synthetic data
and should just work.
