# Live Local Recording — Manual Test Plan

Everything in `feat/live-audio-recording` is implemented, unit-tested (1131 passing,
one pre-existing unrelated Windows failure), and documented. **No real audio device
or real Whisper model has touched this code yet** — that's what this checklist is for.
Work through it top to bottom; each section builds on the last.

Branch: `feat/live-audio-recording` (6 commits for Phase 1, 3 for Phase 2, 2 for Phase 3).

---

## 0. Setup

```powershell
.venv\Scripts\pip install -e '.[live]'
.venv\Scripts\wisper server --reload
```

Open `http://localhost:8080/record`.

- [ ] **Local capture card is visible** beside the Discord card. If it's missing,
      `soundcard` failed to import or no mic/loopback devices were found — check the
      terminal log for a warning from `enumerate_devices()`.
- [ ] Mic dropdown lists your real microphone(s).
- [ ] System-audio dropdown lists at least one loopback device.
      - **Windows**: should just work (WASAPI loopback).
      - **Linux**: should just work if PulseAudio/PipeWire is running.
      - **macOS**: requires [BlackHole](https://github.com/ExistentialAudio/BlackHole)
        installed + a Multi-Output Device set as your output first — see `docs/setup.md`.
- [ ] If you have an enrolled speaker profile already, the **"This is me"** dropdown
      appears with your name in it. If you have zero enrolled profiles, confirm the
      dropdown is absent entirely (not just empty).

**If the device lists look wrong** (e.g. loopback devices showing up as microphones,
or vice versa) — that's the one open item flagged in `plan.md`: `enumerate_devices()`'s
`isloopback` filter was never verified against the real `soundcard` package. Note what
you see; it may need a filter tweak.

---

## 1. Phase 1 — Capture layer (no live transcript yet, just recording)

- [ ] Pick a mic + a loopback device, leave campaign blank, click **Start local recording**.
- [ ] Toolbar switches to the rose "RECORDING" state, source label shows "Local capture".
- [ ] Play some audio through your system output (YouTube, music, anything) while
      also talking into the mic for ~10-15 seconds.
- [ ] Open a second terminal and check the recording directory grew:
      ```powershell
      dir $env:APPDATA\wisper-transcribe\recordings\<recording-id>\per-user\mic
      dir $env:APPDATA\wisper-transcribe\recordings\<recording-id>\per-user\system
      dir $env:APPDATA\wisper-transcribe\recordings\<recording-id>\combined
      ```
      (adjust the data dir path if you've overridden `WISPER_DATA_DIR`) — `.wav` files
      should exist in all three and grow over time.
- [ ] Click **Stop recording**. Toolbar returns to idle within a couple seconds.
- [ ] Go to `/recordings` — your session shows a **LOCAL** badge, status **COMPLETED**.
- [ ] Open the recording detail page — device names you picked are shown under "Devices".
- [ ] Play `combined.wav` (in the recording's root folder) in any media player — you
      should hear your mic AND the system audio mixed together, continuously (no gaps
      where you were mid-sentence, no time-compression artifacts).
- [ ] Play `per-user/mic/0000.wav` — should be just your voice.
- [ ] Play `per-user/system/0000.wav` — should be just the system audio, with silence
      during any gap where nothing was playing (this is the silence-substitution
      behavior — confirms the tracks stayed wall-clock aligned).

**Cross-manager exclusion check:**
- [ ] Start a local session, then try to start a Discord recording from the same page
      (or `POST /api/record/start`) — should get rejected (already-active error), not
      silently start a second session.

---

## 2. Phase 2 — Live transcript preview

- [ ] Start a new local session (same as above).
- [ ] On the recording detail page (`/recordings/<id>`), a **"Live · near-real-time"**
      pane should appear, initially showing "Waiting for speech…".
- [ ] Talk into your mic in short sentences with pauses between them.
- [ ] Within a few seconds of each pause, a line should appear labeled **You** (cyan)
      followed by roughly what you said.
- [ ] Play some speech through the system output (a podcast clip, a YouTube video with
      talking) — lines should appear labeled **Other** (not "You").
- [ ] Talk over the system audio simultaneously — check which label wins (should be
      whichever track was louder over that segment; don't expect perfection, it's RMS
      energy comparison, not real diarization).
- [ ] **This is the first time any real audio has hit `find_commit_boundary`'s Silero
      VAD call and `transcribe_array`'s WhisperModel call — watch the server terminal
      for exceptions.** They should never crash the session (per-chunk errors are
      caught and logged), but if you see repeated warnings, note what triggered them.
- [ ] Check CPU load while this runs, especially if you're on CPU-only. If lines lag
      noticeably behind real speech (more than ~15-20s), your configured model
      (`wisper config`) is probably too large for real-time — try `base` or `small`.
- [ ] Stop the session. Check `recordings/<id>/live_transcript.md` exists and has the
      lines you saw in the UI (this is the crash-safety copy, separate from the SSE
      stream).
- [ ] Reload the recording detail page after stopping — the live pane should be gone
      (only shows while `status` is `recording`/`degraded`).

---

## 3. Phase 3 — "This is me" label

*Skip this section if you have no enrolled speaker profiles — enroll one first via
`/speakers/enroll` with a short clean voice sample, or skip and just confirm the
dropdown correctly stays hidden.*

- [ ] Start a local session, this time selecting your name in the **"This is me"**
      dropdown.
- [ ] Confirm mic-dominant live lines now show **your name** instead of "You".
- [ ] System-audio lines still show "Other" (this field only relabels the mic side).

---

## 4. The real payoff — full diarized transcribe hand-off

- [ ] After stopping any of the above sessions, click **Transcribe** on the recording
      detail page.
- [ ] Confirm it queues a normal transcription job (same job page/progress UI as any
      other upload) and produces a real transcript with actual speaker diarization —
      this path was already proven end-to-end by the existing Discord recording tests,
      but confirm it still works for a `combined.wav` that came from local capture.
- [ ] **While that transcription job is running**, try starting a new live session —
      it should queue behind the running job rather than running concurrently
      (one-job-at-a-time invariant — `JOB_LIVE` holds the slot the same as any job).

---

## What to report back

For each unchecked/failed box above, note:
1. What you expected vs. what happened.
2. Anything in the server terminal log at that moment.
3. OS + whether GPU or CPU transcription.

The most likely trouble spots, ranked by how much I had to guess at the real
`soundcard` API without being able to test it:
1. `enumerate_devices()`'s mic-vs-loopback split (Section 0).
2. `_soundcard_capture_factory`'s `mic.recorder(samplerate=...)` / `record(numframes=None)`
   calls actually working as I assumed (Section 1 — if this is broken, per-track WAVs
   will be empty or the capture threads will silently die).
3. Live transcript timing/lag on CPU (Section 2).

Everything else (mixing math, silence substitution, chunk-cutting logic, JOB_LIVE
lifecycle, cross-manager exclusion, SSE resume) is unit-tested against synthetic data
and should just work.
