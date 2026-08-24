# wisper-transcribe — Open Items

---

## Live local recording + live transcription (mic + system audio)

**Status: shipped (2026-08-15).** All three phases complete on `feat/live-audio-recording`
(11 feature commits + a review-driven fix pass — see commit messages and
`architecture.md`'s Module Map / Job Queue / Known Constraints sections for the
implemented design; the full research/design-sketch history that used to live here
has been trimmed per this file's own doc rule now that the work is done).

**Remaining open items:**

- **Real-device smoke test — Section 1 (fast pass) done 2026-08-23; Sections
  2-4 still owed.** `LIVE_AUDIO_TEST_PLAN.md` (repo root, untracked) has the
  full manual walkthrough. Section 1 was run live in Chrome against real mic
  (Yeti) + real loopback (K5 DAC) hardware and found two real bugs, both
  fixed same day (see `architecture.md`'s Known Constraints and the
  `record.py`/`local_capture.py` module entries for detail):
  1. The mic/system-audio pickers had no default-selection logic at all —
     silently defaulted to the first device in enumeration order rather
     than the OS's actual default, so a session recorded with defaults
     captured genuine silence on the system-audio track.
  2. `{% block extra_scripts %}` was nested inside `{% block page %}` in
     both `record.html` and `recording_detail.html` (Jinja renders a
     nested child block twice under `{% extends %}`), and separately the
     live-ticker's `wisperConnectLiveStream()` call ran before `app.js`
     (loaded `defer`) had defined it — between the two, the live ticker
     never rendered a single line on a fresh page load and the level
     gauges pulsed erratically. Both fixed; verified live afterward
     (smooth gauges, ticker renders correctly, F5 reconnect shows each
     line exactly once, markers survive stop).
  Sections 2-4 (real-device audio-quality listening, "This is me"
  attribution with real simultaneous mic+system speech, campaign-journal
  browser click-through) still need a human — see the "how much can you
  test on your own" breakdown from that session for what's left.
  `enumerate_devices()`'s `isloopback`-based mic/loopback split itself
  checked out correct against the real `soundcard` package (Stereo Mix
  correctly bucketed as a microphone, not a loopback).
- **`initial_prompt` chaining — field-tested 2026-08-16, misbehaved, dropped.**
  Confirmed on real speech to send faster-whisper into repetition loops
  ("column column column column...", worsening chunk over chunk as each
  hallucinated repeat re-primed the next prompt) plus at least one
  out-of-order segment timestamp. `run_live_loop` no longer chains —
  `initial_prompt` is now passed through unchanged on every chunk. Test:
  `test_run_live_loop_does_not_chain_initial_prompt` in
  `tests/test_live_transcribe.py`.

**`JOB_LIVE` starvation by the single-worker queue — fixed (2026-08-24).**
Real-world hit during Section 1 follow-up testing: user reported "I did not
see any audio come through the live transcription side" on a session
recorded while a campaign-journal rebuild (also LLM-only, but submitted
through the same `JobQueue`) was running. Root-caused by direct repro
against the session's own archived audio — `commit_and_transcribe()` on the
real per-track WAVs produced correct transcript lines, ruling out the
transcription/attribution path — down to `stop_live()` only setting
`live_stop_event` on a job that was still `PENDING` behind the rebuild job;
`run_live_loop`'s `while not stop_event.is_set()` then exits on its first
check once the worker eventually dequeues it, "completing" with zero lines
under a misleadingly clean `COMPLETED` status. Fixed in `jobs.py`/`record.py`
(see `architecture.md`'s Record page section for the full mechanism and both
fixes); not a concurrency-model change — `JOB_LIVE` still deliberately holds
the queue's one worker slot for its own duration, a job queued *before* a
live session starts can still delay it for the whole session, it's just no
longer silent (explicit `FAILED` job + an upfront Record-page notice).

**Missing transcript file — found, NOT yet root-caused (2026-08-24).**
Same investigation surfaced a second, separate bug: a full post-session
`JOB_TRANSCRIPTION` job for the same recording reported `COMPLETED` with a
log showing successful diarization and "Wrote `<id>.md`", and the
recording's metadata/API say `has_transcript: true` pointing at that path —
but the file does not exist anywhere on disk. Ruled out: CWD-relative write
(no `./output/` exists in the repo checkout), `WISPER_DATA_DIR` mismatch
(unset), every deletion code path in `jobs.py` (`_move_upload_to_output`/
`_delete_temp_upload` are gated on `job.is_web_upload`, which a local
recording's copied `<recording.id>.wav` source never sets), and a recursive
search of the entire user profile + repo (nothing named `4a4af921*` exists
except the recording's own directory). Could not be root-caused further:
`JobQueue` has zero persistence between restarts (by design — see
`jobs.py`'s own docstring) and no debug log was enabled at the time, so the
job history that would show what actually happened is gone. Next occurrence
is the only way forward — enable `WISPER_DEBUG=1` before the next local
recording + Transcribe pass so a repeat is capturable.

**Session note (2026-08-15):** the post-implementation review pass (code-review
skill, high effort, full branch diff) found and fixed 6 real bugs — JOB_LIVE
threads surviving server shutdown, the generic job-cancel button being a no-op
(and leaking an unbounded ring buffer) for live sessions, a hardcoded 'You'
string breaking "this is me" coloring, dropped final SSE lines at session end,
missing degraded-status signaling on capture-thread failure, and O(n²) VAD
rescanning — plus this doc trim. Two findings were reviewed and deliberately
left as-is: `SegmentedWavWriter`'s per-write `flush()` (pre-existing, documented
crash-safety tradeoff, not something this branch introduced) and the
`append_log`/`append_live_line` + SSE-resume-math duplication across 4 call
sites (real DRY violation, low risk, but a refactor wasn't worth the remaining
session budget — ~$9 in credits when this note was written).

**Duplication cleanup — delivered (2026-08-22).** Picked back up per the note
above. `_append_capped(items, item, cap)` and `resume_slice(items, dropped,
last_idx)` (both new, `jobs.py`) consolidate the trim-and-count-drops
arithmetic (`append_log`/`append_live_line`) and the absolute-index-to-
retained-slice arithmetic (previously hand-rolled identically in
`transcribe.py`'s job-log stream and twice in `record.py`'s live-transcript
stream) into one implementation each. Pure refactor, no behavior change — all
1222 pre-existing tests still pass unmodified; added
`test_resume_slice_normal_case_no_drops`, `test_resume_slice_translates_past_
dropped_prefix`, `test_resume_slice_client_fell_behind_the_cap_resumes_from_
retained` in `test_web_jobs.py` as direct coverage for the newly-public
helper. Found while auditing this area for the reconnect-de-dupe fix above.
Real-device smoke test is now the only remaining open item in this section.

**Manual smoke-test follow-ups (2026-08-16, real-device testing in progress
per `LIVE_AUDIO_TEST_PLAN.md`) — fixed same day:**

- **Default "this is me" mic profile — fixed.** `_remember_mic_profile_default()`
  in `web/routes/record.py` now persists whatever profile (including blank)
  was selected on local-session start as the `default_mic_profile_key` config
  value, and `/record` pre-selects it on the dropdown next time — still
  overridable per-session.
- **`/record` page's "Heard so far" ticker never updated — fixed.** It was
  listening for a `partial_transcript` SSE event on `/record/sse` that the
  endpoint never emits (only `type: status` payloads on the default
  `message` event). Live transcription itself was working the whole time —
  confirmed via `live_transcript.md` on disk during the smoke test — just
  invisible on this page. Now opens its own `EventSource` at the same
  `GET /recordings/{id}/live` stream the recording detail page already used
  correctly.
- **Recording detail page now shows total duration** (`ended_at - started_at`,
  dash while still recording) in the status strip — same computation
  `recordings.html`'s list view already used, added as a 7th status-strip
  cell on the detail page too.

**More fixes (2026-08-16, same smoke test):**

- **A failed transcribe job left its `Recording` stuck at status
  `"transcribing"` forever, with no retry path in the UI — fixed.**
  `JobQueue.submit()` now accepts a symmetric `on_error` callback
  (invoked from `_run_transcription_job`'s except blocks — both the real
  exception and cancellation paths — via the new `_run_on_error_callback()`,
  which also discards any now-dangling `on_complete` for that job).
  `_submit_recording_transcription()` wires it to revert `recording.status`
  back to whatever it was before the attempt (`"completed"` for a first
  transcribe, `"transcribed"` for a failed re-transcribe — not a bare
  `"completed"` that would hide the old transcript's actions). Tests:
  `test_on_error_callback_*` in `test_web_jobs.py`,
  `test_transcribe_recording_reverts_status_on_job_failure` /
  `test_retranscribe_recording_reverts_to_transcribed_on_job_failure` in
  `test_record_routes.py`.
- **"This is me" mic-dominant lines were tagging as the profile name even
  during system audio, once with the system track measured at exactly
  0.0 RMS across an entire session.** Diagnosed by computing RMS on the
  actual per-track WAV segments from two real sessions: one had system
  audio literally silent the whole time (likely the wrong loopback device
  selected — nothing was actually routed to it), the other had real but
  much quieter system signal (RMS ~200 vs mic RMS ~1440, so mic legitimately
  dominated every window, possibly with acoustic mic bleed from speaker
  output if headphones weren't used). `attribute_speaker()`'s RMS-compare
  logic itself checked out correct at the time — no code bug found there.
  Confirmed with the user (attribution itself is working); the real
  follow-up request was a noise floor (below), not a change to the
  attribution comparison.
- **Noise floor added — mic/system self-noise was getting hallucinated as
  real speech.** User report: "my mic with nothing going is getting random
  audio transcribed, probably just background noise." Root cause: Silero
  VAD's chunk-boundary pass can flag room tone / mic self-noise as
  "speech," and faster-whisper tends to hallucinate plausible-looking text
  for that kind of near-silent audio rather than returning nothing.
  `attribute_speaker()` now takes a `noise_floor` (int16 RMS, default
  `NOISE_FLOOR_RMS = 150.0` in `live_transcribe.py`) and returns `None`
  when *neither* track clears it; `commit_and_transcribe()` drops that
  segment instead of mislabeling it under `mic_label` by the old
  both-silent tie-break. 150.0 is a coarse starting point (real speech
  observed well above it on a USB condenser mic; true silence measures
  exactly 0.0) — may need tuning per mic/gain, see the slider feature
  request below. Tests: `test_attribute_speaker_*noise_floor*`,
  `test_commit_and_transcribe_drops_hallucination_on_noise_floor` in
  `tests/test_live_transcribe.py`.

**Noise-floor slider — delivered (2026-08-16, same day as the request):**
`NOISE_FLOOR_RMS` (150.0 at the time, since raised to 300.0 — see below) is a guess, and different mics/gain/rooms will
want it higher or lower — the slider lets the user tune it without
restarting the session. `commit_and_transcribe()` takes a `noise_floor`
param now (forwarded to `attribute_speaker()`); `run_live_loop()` takes a
`get_noise_floor` zero-arg callable, called fresh at the top of *every*
iteration rather than once at loop start; `submit_live(..., noise_floor=)`
seeds `job.kwargs["noise_floor"]`, and the new `JobQueue.set_live_noise_floor
(job_id, value)` overwrites that same key on a job that's already
`RUNNING`. Route: `POST /api/record/live-noise-floor` (resolves the active
local session + its `JOB_LIVE` job, 400/404 on no session/no job/bad
value). UI: a range slider in `/record`'s active-session sidebar
(local-source only), debounce-POSTing on `input`. Resets to the library
default on page reload — not persisted across sessions. Tests:
`test_run_live_loop_reads_noise_floor_live_each_chunk`,
`test_commit_and_transcribe_custom_noise_floor_overrides_default` in
`test_live_transcribe.py`; `test_submit_live_seeds_noise_floor_kwarg`,
`test_set_live_noise_floor_updates_running_job_kwargs`,
`test_run_live_job_wires_get_noise_floor_reading_job_kwargs_live` in
`test_web_jobs.py`; `test_live_noise_floor_*` in
`test_record_live_routes.py`.

**Live level gauge — delivered (2026-08-16, follow-up to the slider):**
User: "can we include a gauge or better yet show the current noise floor
on the same gauge?" — the slider had no visual feedback for what level the
mic/system tracks were actually sitting at relative to the threshold.
`LocalCaptureManager._do_tick()` now feeds each track's RMS (reusing
`live_transcribe._rms()`) into `self._level_peaks`, tracking the *peak*
since the last read rather than an instantaneous snapshot (a 1s poll
interval would otherwise miss short transients); `get_and_reset_levels()`
reads and clears it. `GET /record/sse`'s status payload carries
`mic_rms`/`system_rms` for a local session (idle/Discord payloads omit
the keys entirely). UI: two horizontal bar gauges (Mic, System) under the
noise-floor slider in `/record`'s sidebar, 0-2000 RMS scale (wider than
the slider's 0-1000 so speech peaks don't constantly pin the bar); each
bar has a rose threshold-marker line at the current noise-floor value and
turns green when the live level clears it — same gauge shows both the
signal and the floor, rather than the floor only being a number next to
the slider. Threshold marker updates live as the slider moves (no extra
request; client-side only) and is re-synced whenever a level update
arrives. Tests: `test_get_and_reset_levels_*`, `test_do_tick_updates_
level_peaks`, `test_start_session_resets_stale_level_peaks` in
`test_local_capture.py`; `test_record_sse_includes_level_gauge_for_local_
session`, `test_record_sse_idle_status_omits_level_gauge_fields` in
`test_record_live_routes.py` (the latter pulls one chunk off `record_sse()`'s
`StreamingResponse.body_iterator` directly via `asyncio.run` rather than
over a live TestClient HTTP stream, since the endpoint polls forever).

**Default noise floor raised 150 → 300 (2026-08-16):** With the gauge
visible, 150 was still visibly letting mic self-noise clear the floor on
the user's setup. `NOISE_FLOOR_RMS` in `live_transcribe.py` and the
slider's initial `value`/displayed number in `record.html` both moved to
300 — still just a starting point, adjustable live via the slider per
session.

**Global recording-status banner — delivered (2026-08-16):** User report:
"I can navigate to other pages and the recording seems to keep going, but
in the upper right corner it shows 'start' instead of 'stop recording'."
`/record`'s own toolbar already reflected live state correctly, but every
*other* page (Dashboard, Recordings, Transcripts, etc.) had no idea a
session was active — Dashboard/Recordings still showed a static
"Start session" CTA. Rather than editing every page's toolbar, filled in
the long-dead `GET /api/record/status` 501 stub (`_current_active_
recording()` -> `{"active": false}` or `_recording_to_dict()` + `"active":
true`) and added an empty `#global-recording-banner` container to
`base.html`, populated client-side by a new `app.js` poller (every 4s,
skipped on `/record` itself since its own toolbar already covers this):
a rose banner with a live elapsed timer and a real `Stop recording` form
(routes to `/record/stop-local` or `/record/stop` per `source`) appears
at the top of every other page while a session is active. No per-page
template edits needed. Tests: `test_record_status_idle_when_no_active_
session`, `test_record_status_reports_active_local_session` in
`test_record_routes.py`.

**Global banner follow-up, part 1 — misleading "queued automatically" copy
fixed (2026-08-16):** User re-tested the banner and separately asked "I
don't see any of the live transcriptions under the transcripts page. Does
live transcribe only persist until I stop the recording then it goes
through a normal transcribe cycle?" Answer: no auto-transcribe ever
happens on stop — `/record/stop-local` and `/record/stop` never call
`_submit_recording_transcription()`; only the recording detail page's
explicit Transcribe button does (`recording_detail.html` already says so
correctly at line 144: "generated after you stop and click Transcribe").
But `record.html`'s "When you stop" sidebar box claimed "Transcription
queued automatically," which was simply wrong — fixed the copy to say
"Click Transcribe on the recording to run the full, diarized pass"
instead of changing behavior (auto-queuing full diarization on every stop
is a real design decision, not implemented here — would need the user to
explicitly want that traded off against every stop costing GPU time even
for a throwaway recording).

**Global banner follow-up, part 2 — the banner actually was broken, root
cause found by live-reproducing in Chrome (2026-08-16):** The user's
banner report ("upper right still shows 'start'... still doesn't show")
turned out to be a real bug, not user error — reproduced by starting a
real local session via the running dev server and navigating to
Dashboard in an actual Chrome tab (not just re-reading the code). Root
cause: `base.html` cache-busts `app.js`/`tailwind.min.css` with
`?v={{ app_version }}`, and `app_version` is the static package version —
it never changes across a `--reload` dev session, because static-file
edits (unlike `.py` edits) don't restart the uvicorn process, so nothing
ever bumps it. The browser kept serving a stale cached `app.js` (fetched
before the banner code existed) on every subsequent page load all
session, silently — `curl` against the same URL confirmed the *server*
was serving the correct up-to-date file the whole time; only the
browser's own cached copy was stale. Fix: new `static_mtime(filename)`
Jinja global (`routes/__init__.py`) stats the file fresh per render and
returns its mtime as the cache-buster instead of `app_version`, falling
back to `app_version` if the file's missing; `base.html` now does
`?v={{ static_mtime('app.js') }}` / `?v={{ static_mtime('tailwind.min.css') }}`.
Caught a real bug in the fix itself before it shipped: the first version
of `_STATIC_DIR` in `routes/__init__.py` used `Path(__file__).parent.parent`
(→ `web/`), one level too shallow — `app.py`'s existing `_STATIC_DIR` needs
`.parent.parent` from `web/app.py` to reach `wisper_transcribe/static/`,
but `routes/__init__.py` is one directory deeper (`web/routes/__init__.py`),
so it needs `.parent.parent.parent`. Writing the test
(`test_static_mtime_matches_real_file_mtime`) caught this immediately — the
`except OSError` fallback in `_static_mtime()` would have silently masked
it in production and the exact bug being fixed would have persisted.
Tests: `test_static_mtime_*`, `test_dashboard_html_includes_mtime_cache_
buster` in `test_web_routes.py`. **Lesson for this session going forward:**
when the user reports something not showing up after a change, verify
with a live reproduction (browser or curl) before trusting a code read —
this bug was invisible from reading the diff alone.

**"Awaiting transcription" section on /transcripts + session naming —
delivered (2026-08-16):** User: "let's add a section to transcripts that
includes all the live recordings so I can easily transcribe them if I
want. For the ones that have not been through the full transcribe option
is the live transcribe kept?" Answer to the second question: yes,
indefinitely — `live_transcript.md` is never cleaned up by anything;
`delete_recording()` only pops the index entry, never touches files on
disk. New `_pending_recordings(data_dir)` in `web/routes/transcripts.py`
returns recordings with `status == "completed"` (finished capturing,
never auto-queued — see the copy-fix entry above) and audio on disk,
newest first, plus which of them still have a live-draft preview file;
`/transcripts` renders this as a new "Awaiting transcription" section
above the campaign archive, each row posting straight to the existing
`POST /recordings/{id}/transcribe` hand-off.

Mid-turn follow-up request: "i'd also like to be able to name the live
recordings at the start of the session. that way it's easier to identify
it." Added `Recording.name: Optional[str] = None` (display-only, never
touches a file path — `id`, a server-generated uuid4, still backs the
directory) threaded through `create_recording()` ->
`LocalCaptureManager.start_session(name=)` -> a new "Session name
(optional)" field on the Record page's local-capture start form ->
`_clean_session_name()` in `record.py` (trim + cap at 200 chars, blank ->
`None`). Shown wherever a recording appears: the active-session toolbar,
the global recording-status banner (`app.js` — HTML-escaped before
`innerHTML`, since that's client-supplied text and JS `innerHTML` isn't
auto-escaped the way Jinja is), `recordings.html`'s rows, `recording_
detail.html`'s header, and the new `/transcripts` section — falls back to
a truncated id everywhere when unset.

Bonus fix found while touching `recordings.html` for the name display:
its captured-table status pill said "TRANSCRIBED" for `status ==
"completed"`, which is backwards — "completed" means the capture
finished but the full diarized pass hasn't run, "transcribed" is the
separate post-pipeline status. The table also had no branch at all for
the real "transcribed"/"transcribing" statuses (empty Action cell).
Fixed: "completed" now shows "NEEDS TRANSCRIBE" + a direct Transcribe
button; added the missing status branches.

Tests: `test_create_recording_name_roundtrips` etc. in `test_recording_
manager.py`; `test_start_session_passes_through_name` etc. in `test_
local_capture.py`; `test_start_local_json_api_accepts_and_trims_name`,
`test_clean_session_name_*` etc. in `test_record_routes.py`;
`test_pending_recordings_*`, `test_transcripts_page_shows_awaiting_
transcription_section` etc. in `test_web_routes.py`.

**Two more from a fresh smoke-test screenshot (2026-08-16):** User asked
whether "No speakers mapped yet. Participants will appear as they join."
under Voices/"At the table" is accurate for a local session, and pointed
out a screenshot showing the live ticker with the same 6 lines rendered
twice back-to-back.

1. Not accurate — real bug. `discord_speakers` is populated only by the
   Discord bot's auto-tag path; `LocalCaptureManager` never touches it, so
   for a local session that dict is permanently empty and "participants
   will appear as they join" can never happen (there's no join concept
   for a fixed mic+system capture). Fixed: the whole speaker-meters
   section now renders only for `source != "local"`.
2. Real bug, confirmed from the screenshot's exact pattern (two identical
   6-line blocks, oldest-line-first within each block — precisely what
   two independent from-scratch replays of the same backlog produce).
   Root cause: `GET /recordings/{id}/live`'s resume cursor (`last_idx`)
   is per-connection server state starting at 0, with no client-side "up
   to where have I already rendered" signal — so any reconnect (a
   dev-server restart mid-session, a network blip, a tab waking from
   sleep) is indistinguishable from a brand-new stream and replays the
   entire line history from the top. Rather than chase down exactly what
   triggered the reconnect this particular time, fixed the general case:
   `wisperTickerAppend()` now de-dupes on a `timestamp|speaker|text` key
   before rendering, so a replayed line is silently dropped instead of
   doubled. Tests: `test_record_page_hides_speaker_meters_for_local_
   session`, `test_record_page_shows_speaker_meters_for_discord_session`
   in `test_record_routes.py` (the dedupe itself is untested — this repo
   has no JS test harness; verify by hand: reload `/record` mid-session
   and confirm no duplicate lines appear).

**"Add marker" — delivered (2026-08-16).** No documented intent existed
anywhere (checked plan.md, architecture.md, README, docs/, git history —
the button traces back to a single bulk commit, `fc1e6cd` "Studio
redesign," that rebuilt all 9 screens from a design mockup handoff in
one pass; no spec for markers specifically, never wired to a backend).
User picked: silent bookmark (one click, no typing) + a visible flagged
line in the live ticker, after confirming no reusable prior art existed
in the codebase (`Recording.segment_manifest`/`rejoin_log` are the closest
*pattern* match — list-of-dataclass, per-recording-mutex-guarded — but
track different things).

New `Marker(timestamp, elapsed_s)` on `Recording.markers`; `append_marker()`
in `recording_manager.py` mirrors `append_segment()`'s mutex pattern,
computing `elapsed_s` once at creation time. `POST /record/marker`
resolves the active recording via the existing `_current_active_recording()`
(so it works for Discord too, not just local) and returns `{"elapsed_s"}`.
The "Add marker" button is a plain `fetch()`, not a form POST — a full
page reload would reset the ticker's scroll position mid-session — and a
successful response calls the new `wisperTickerAppendMarker()` to drop a
rose, speaker-less flagged line straight into the ticker. Markers also
show as elapsed-time pills on `recording_detail.html` under a new
"Markers" section. Known v1 gap: a page reload doesn't replay past
markers into the ticker (only real transcript lines come back through the
SSE snapshot) — they're still safely on `Recording.markers` and visible
on the detail page, just not re-inserted into the ticker on a fresh load.
Tests: `test_append_marker_*` in `test_recording_manager.py`;
`test_record_marker_*`, `test_recording_detail_*_markers*` in
`test_record_routes.py`.

**Feature requests (2026-08-16, from the same live smoke-test session):**

- **Change mic/system input devices without stopping the recording.**
  Today `LocalCaptureManager.start_session()` binds two capture threads to
  fixed device IDs for the whole session; switching requires Stop then
  Start (a new `Recording`, a new `combined.wav`, a gap in the transcript).
  This is a bigger architectural change than the slider above — would need
  the capture threads to be individually restartable against a new device
  ID mid-session while the tick thread / segment writers / `Recording`
  keep running continuously underneath (segment rotation already handles
  a *track* going silent via FIFO starvation, but not swapping which
  physical device feeds a track). Not scoped in detail yet — worth a
  design pass before implementation given the size.
- **Real per-speaker diarization on the live system-audio track**
  (2026-08-16). Today the system track is a single RMS-attributed "Other"
  — everyone else on the call is lumped together, since OS-level loopback
  capture is already a mixed-down stream with no per-participant
  separation (true even in the full post-session pipeline; the only real
  question is whether *diarization itself* can run live). User: "I will
  probably come back to the idea eventually." Not a small addition:
  pyannote's clustering only gives consistent speaker labels across a
  *whole-file* pass, which is exactly what the full pipeline does and the
  live loop's independent ~15s chunks don't — running pyannote per chunk
  would need an incremental layer on top (embed each turn, compare against
  a running per-session speaker pool, match-or-create), which pyannote
  doesn't provide out of the box, plus real added latency/GPU load in the
  live loop's hot path. Not scoped further; revisit if the "You/Other"
  split starts being a real limitation in sessions with several other
  people talking.
- ~~Live transcript ticker on `/record` should never drop old lines within
  a session ("stay and not roll over")~~ — **delivered same session.**
  User's stated use case: running this during a tabletop game so they can
  scroll back if they missed something someone said. `wisperTickerAppend()`
  in `static/app.js` hard-capped the DOM at 12 entries, deleting older
  ones — cap removed, `#live-ticker` now scrolls internally
  (`max-height` + `overflow-y`) instead.

**`UnicodeEncodeError` crashing transcription jobs — fixed (2026-08-16).**
Confirmed reproducing for real: the user hit it transcribing their local
recordings (job `0e7f9bd7...` and others), traceback landing at
`pipeline.py:458`'s `tqdm.write("─" * 60)` — a Unicode box-drawing
separator hitting a `cp1252` console codec that can't encode it. The
codebase has other tqdm.write() calls with non-ASCII content too (`→` in
the speaker-match log line, `path.name`/exception messages that could
carry non-ASCII text) — patching the two known call sites wouldn't have
closed off the whole class of failure. Fixed at the root instead: new
`_ensure_utf8_stdio()` in `cli.py`, called unconditionally at import time
(every command, not just `--debug`) — reconfigures `sys.stdout`/`stderr`
to UTF-8 with `errors="replace"`. Tests: `test_ensure_utf8_stdio_*` in
`test_cli.py`.

First deploy of that fix didn't actually work — re-tested against the
same recording and it failed identically. Root cause: `wisper server
--reload` runs `uvicorn.run(..., reload=True)`, which spawns a *fresh
subprocess* that imports `"wisper_transcribe.web.app:app"` directly and
never re-runs `cli.py`'s module-level code at all, so the fix never
reached the process transcription jobs actually run in. Added the same
reconfigure loop independently in `web/app.py` too (right next to the
existing `tqdm.monitor_interval = 0` line, which has the identical
"uvicorn --reload spawns a fresh subprocess" duplication problem and is
already duplicated into `jobs.py` for the same reason — see that
comment). Re-verified live end-to-end this time: re-triggered
transcription on the same previously-failing recording and it completed
with a real transcript.

**Recording name now persists to the finished transcript's title —
delivered (2026-08-16).** User: "the name I set when I start the
recording should persist to the actual finished transcript." Previously
`_submit_recording_transcription()` always passed `original_stem=
recording.id`, so `process_file()`'s default filename-derived title
became the recording's UUID, title-cased — the session name was never
used. Fixed by decoupling title from filename rather than reusing
`original_stem` for both: new `title=` param on `pipeline.process_file()`
overrides the default title, and `_submit_recording_transcription()`
passes `title=recording.name` alongside the unchanged `original_stem=
recording.id`. Deliberately kept the two separate — `original_stem`
becomes a real filename via a raw rename with no sanitization
(`tmp_path.with_name(original_stem + suffix)` in `JobQueue.submit()`),
so a free-text name with filesystem-unsafe characters must never flow
through it; `title` only ever reaches YAML frontmatter, no such
constraint. "Only overwrite it if I do a full transcribe job" was
already true structurally — nothing else sets `transcript_path`/title,
so it's not touched again until the next explicit Transcribe/Re-transcribe
click. Tests: `test_process_file_title_override` in `test_pipeline.py`;
`test_transcribe_recording_passes_name_as_title`, `test_transcribe_
recording_no_name_passes_none_title` in `test_record_routes.py`.

**Live-transcript draft now persists on the recording detail page after
stop — delivered (2026-08-16).** User: "is the live transcription kept
anywhere after I stop the recording? It would be nice to keep that with
the recording file on the recording page. Only overwrite it if I do a
full transcribe job." Answer to the first question: yes, already —
`live_transcript.md` is never cleaned up. What wasn't true: the detail
page's live-transcript pane only rendered while `status in ("recording",
"degraded")`, so it vanished from the page the instant the session
stopped even though the file was still on disk; separately, its SSE
snapshot handler discarded the actual content and showed a static
"Session ended." string (found while investigating, not fixed — see
below).

`recording_detail_html` now also renders a static, server-side-parsed
(`formatter.parse_transcript_blocks()`, the same parser transcript
editing already uses) view of `live_transcript.md` whenever `source ==
"local"`, the file exists, and `transcript_path` is still `None` — i.e.
exactly until a real Transcribe job sets it, matching "only overwrite it
if I do a full transcribe job" precisely (nothing else ever sets
`transcript_path`). While still `recording`/`degraded` the existing
SSE-driven pane keeps ownership of the view, mutually exclusive by
construction (the new branch's route-level condition explicitly excludes
those two statuses). Tests: `test_recording_detail_shows_persisted_
live_draft_after_stop`, `test_recording_detail_omits_draft_once_
transcribed`, `test_recording_detail_omits_draft_when_no_file_on_disk`,
`test_recording_detail_omits_draft_for_discord_source`, `test_recording_
detail_active_session_uses_live_pane_not_static_draft` in `test_record_
routes.py`.

**Follow-up cleanup (2026-08-22): reconnect de-dupe gap fixed + snapshot
dead code removed.** Auditing this area turned up a real bug the
"minor gap" note above missed: `recording_detail.html`'s live pane had
its own hand-rolled `EventSource` + parsing, separate from
`record.html`'s ticker — and unlike the ticker, it never got the
reconnect-replay de-dupe fix from the "stay and not roll over" entry
above. A reload/reconnect mid-session (dev-server restart, network blip,
tab wake) would have doubled every line in the detail-page pane, silently.

Fixed by extracting the shared connection+parsing+de-dupe logic into
`window.wisperConnectLiveStream(url, onLine, onSnapshot)` in `app.js`
(keyed on raw `start_s|speaker|text`, not a caller's display-formatted
fields, since the two callers format differently); both `record.html`
and `recording_detail.html` now call it instead of duplicating the
connection logic. `wisperTickerAppend()` no longer does its own de-dupe.

Separately resolved the "minor gap" itself, but by deletion rather than
implementation: `payload.markdown` was dead code — grepped and confirmed
no client anywhere ever read it (only the bare `type === 'snapshot'`
check mattered, to flip the placeholder to "Session ended."). Rather than
add markdown parsing in JS just to satisfy an unused payload, the server
route (`GET /recordings/{id}/live` in `record.py`) now sends a bare
`{"type": "snapshot"}` signal and no longer reads `live_transcript.md`
into the response at all — the recording-detail page's own
server-rendered static-draft fallback (above) is what actually shows the
draft's content, so the SSE snapshot never needed to carry it. Updated
`test_recording_live_snapshot_from_disk_when_no_active_job` accordingly.

---

## Senior review — closed 2026-07-16

The full-codebase senior review (findings R1–R38, opened 2026-07-15) is **complete**: all 38 findings remediated across six phase commits on `docs/senior-review` (Phase A quick wins → B config/CLI coherence → C leaks/memory → D web correctness/security → E Discord audio rebuild → F nits/R7/docs/locking). Suite grew 872 → 1025 over the review. Details live in the six phase commit messages.

**Open follow-ups from the review:**

- **Live Discord acceptance test (Phase E).** The recording pipeline rebuild (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is fully covered by synthesized-PCM decode tests, but the JDA→socket→Python path can only be proven with one real Discord session: record a few minutes with 2+ speakers, play the per-user WAVs, and run Transcribe on the recording.
- ~~`segment_manifest` never populated.~~ **Fixed 2026-08-22** (picked up during a live-audio-recording branch audit). `record_completed_wav_segment()` (`recording_manager.py`) is now called from both `BotManager._route_frame` and `LocalCaptureManager._do_tick` whenever the combined writer rotates, and once more from each `_finalise()` for the final segment — see architecture.md's Data Storage section. Also surfaced two more bugs while fixing this, both caught and fixed the same session before reaching `main`:
  1. `BotManager`/`LocalCaptureManager` hold one long-lived `Recording` object for the whole session and periodically `save_recording()` it directly, which silently clobbered anything `append_marker()`/`append_segment()` had written concurrently from a different call site (confirmed reproducible: a marker added mid-session came back empty after the session ended normally). Fixed with `save_recording_merged()`, now used at every such call site in both managers.
  2. A no-audio session (bot/session starts and stops with zero frames ever received) still got a phantom `segment_manifest` entry — `SegmentedWavWriter.finalize()` always returns a path even for the empty 0-frame segment it closes, and the first version of `record_completed_wav_segment()` appended a record for it unconditionally. This made the recording-detail page show a contradictory "Segments: 1" next to `combined_path` correctly staying `None` (`?error=no_audio`). Fixed by skipping zero-frame/unreadable segments, mirroring the check `concat_wav_segments()` already does when building `combined.wav`.

  Tests: `test_combined_track_rotation_and_finalize_populate_segment_manifest`, `test_marker_added_mid_session_survives_finalise`, and a `segment_manifest == []` assertion added to the existing no-audio test, in both `test_discord_bot.py` and `test_local_capture.py`; `test_record_completed_wav_segment_*` (including the zero-frame and unreadable-file cases) in `test_recording_manager.py`.

---

## Deferred parity gaps

### D5 — Refine/summarize CLI vs web asymmetry
CLI runs these synchronously with `--dry-run` preview. Web runs them as async JobQueue jobs with no dry-run. Both work; the asymmetry reflects the surface (terminal vs. browser), not a missing feature.

---

## Job cancellation — best-effort GPU stop

**Observed (2026-05-11):** clicking Stop on an in-flight transcribe job in the web UI marks the job `Failed` in the queue, but the GPU keeps running hard for the duration of the in-flight CTranslate2 batch. The Python worker exits on the next tqdm tick (cooperative cancel via `job._cancel_event` in `web/jobs.py`), but in-flight inference inside faster-whisper's internal thread pool continues until the batch finishes.

**Why the current mechanism is cooperative-only:**
- `cancel_event.is_set()` is checked inside `capturing_write()` and `ProgressCatcher.write()` — both only fire when tqdm emits output.
- Between tqdm ticks the worker thread is blocked inside CTranslate2's C++ code, which has no Python yield points and no public cancel hook.
- `pipeline.py` itself has no awareness of the job's cancel event.

**Options for true interrupt:**
1. **Run transcription in a subprocess and SIGTERM on cancel.** The `parallel_stages = true` config already does this for the transcribe+diarize concurrency path. Generalising it to single-stage mode would mean every job spawns a subprocess (small startup cost, ~1–2 s) but gives clean GPU release on cancel.
2. **Plumb the cancel event into `pipeline.process_file()`** so it's checked between segments inside the generator loop. Faster than (1) for very short batches; doesn't help mid-batch on the GPU.
3. **Document cancel as best-effort** and add a "Force-quit" button that issues the OS-level termination (Windows-aware, no JVM-style hard kill on POSIX).

Recommendation: option (1) — reuse the parallel-stages subprocess plumbing for the single-stage path too. Tracked here until a user explicitly cancels often enough to justify the work.

---

## DAVE Sidecar → Python migration (parked; not yet viable)

**Issue #39 (DAVE blocking audio receive) is CLOSED** — the original "bot is broken" premise is resolved. The Java JDA 6.3.0 + JDAVE 0.1.8 sidecar receives and decrypts DAVE-encrypted audio today and works end-to-end. DAVE itself is mandatory and unavoidable (Discord enforced E2EE for non-stage voice on March 2, 2026; there is no per-channel opt-out), so the only open question is *where* DAVE is implemented, not *whether*.

**Key fact:** DAVE is MLS over OpenMLS — there is no pure-Python implementation and never will be. Every path depends on a native (Rust/JNI) MLS binding. The choice is which language wraps that binding, not Java-native vs. Python-pure.

**Python DAVE-receive readiness (as of 2026-06-15):**
- **pycord PR #3159** — DAVE *receive* for pycord. Approved by 2 reviewers but still a **draft**, milestoned for **2.9.0rc1** (last activity 2026-06-08). pycord has native voice receive, so this is the right target — but it is **unreleased**.
- **discord.py PR #10300** — **merged 2026-01-07**, shipped in discord.py **2.7.0 / 2.7.1** (2026-03-03), but flagged *"tentative"*. discord.py has **no first-class voice receive**, so it is not a fit for a recording bot regardless.
- **`davey`** (Snazzah's OpenMLS binding, the Rust native lib both discord.py and pycord use) — **v0.1.5, beta, 2026-03-29**, with "proper usage documentation does not exist yet."

**Verdict:** Migrating now would trade a working Java sidecar for an unreleased Python one on a beta native lib. **Keep the sidecar.** Revisit when **pycord 2.9 ships #3159 as a stable release**.

**Migration path** (execute once pycord 2.9 stable lands):
1. Delete `discord-bot/` (the Gradle/Java project)
2. Write ~100-line Python replacement emitting the same length-prefixed PCM wire format over the existing Unix socket
3. Update `BotManager` to launch the Python script instead of the JAR
4. Remove the Java builder stages from `Dockerfile` and the Java 25 requirement from launchers + README

Nothing else changes — the Unix-socket wire protocol (length-prefixed user_id + 48 kHz stereo PCM) is unchanged and remains the stable interface for the sidecar swap; the web UI, campaigns, and CLI are unaffected. (Note, post-R12/R2 fix 2026-07-16: Python-side storage is now `SegmentedWavWriter` — WAV segments, 16 kHz mono, downsampled at write time — and JDA's `__mixed__` pre-mixed track is written directly as the combined track; `SegmentedOggWriter`/`RealtimePCMMixer` no longer exist. A future Python sidecar only needs to emit the same wire format, including a pre-mixed `__mixed__` stream.)

**Structural fallback (Strategy B), if the native-binding ecosystem stalls:** both JDAVE and `davey` are small-maintainer libraries tracking a protocol Discord controls and can change. The only DAVE-churn-immune approach is to *not* implement DAVE at all — run a real Discord client joined to the channel and capture its client-side-decrypted audio via a virtual audio (loopback) device. Heavier operationally and loses per-speaker SSRC separation, so not worth building now — documented as the escape hatch if jdave/davey break on a future protocol bump.

---

## Storage architecture — SQLite full migration (future consideration)

**Context (2026-05-14):** The job queue is in-memory only. When the server restarts, in-progress enrollment wizards break because `diarization_segments` and `input_path` are lost. The immediate fix is JSON sidecars written alongside the transcript (Option 2, implemented). This section records the case for a full SQLite migration if the app grows.

**Current storage model — "files are the database":**
- `speakers.json` + `.npy` embedding files
- `campaigns.json`
- `.md` transcript files + `.summary.md` sidecars
- `_diar.json` enrollment sidecars (added by Option 2)
- Job queue: in-memory only (ephemeral)

**Why full SQLite would be worth doing at some future point:**
- Transactional writes across related data (e.g., add campaign member + transcript association atomically) — currently `campaigns.json` and `speakers.json` can drift if a crash happens mid-write
- Persistent job history across restarts — past transcription runs, their logs, and enrollment data would all survive
- Relational queries if features grow (e.g., "all transcripts for a speaker", "jobs by campaign")
- Eliminates the proliferating sidecar pattern (`_diar.json`, `.summary.md`, `_excerpt_*.mp3`, `_excerpt_*.txt`) in favour of a single source of truth

**Why we're not doing it now:**
- Requires migrating existing installs (`campaigns.json`, `speakers.json` → tables) with a one-time migration script
- Embedding `.npy` files still live on disk regardless — SQLite would store the path, not the blob
- Loses "just open the file" inspectability; needs `sqlite3` CLI or a viewer
- Schema migrations become a maintenance burden as the codebase evolves (would want `peewee` or similar rather than raw `sqlite3`)
- "Jobs-only SQLite + JSON for everything else" was considered and rejected — the hybrid model is the worst of both worlds, creating two storage patterns to reason about

**Trigger conditions** — revisit when any of these are true:
- Multi-user or networked deployments are needed (SQLite WAL mode handles concurrent reads but not concurrent writes from multiple processes)
- Job history browsing across restarts becomes a user need
- A third JSON file with cross-cutting relationships appears (campaigns.json + speakers.json are already two; a third is the smell)

---

## Campaign-level LLM summaries (DM tools)

**Context (2026-05-14):** Per-session `wisper summarize` already produces `.summary.md` sidecars with recap, loot, NPCs, and follow-ups. These are session-scoped. The next level is campaign-scoped documents — aggregations across sessions that are most useful to the DM managing an ongoing story.

The **rolling campaign journal** — the first of these — has shipped (`journal.py`, `wisper campaigns journal`, the web Campaign-page journal panel). It established the shared infrastructure the remaining features reuse: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery (`unjournalled_sessions()`), and `JobQueue` `JOB_CAMPAIGN_*` types with the standard SSE progress page. See `architecture.md` / `docs/web-ui.md` for its as-built shape; mirror it when building the three below.

**Merged into `feat/live-audio-recording` (2026-08-23).** `feat/campaign-journal` (developed independently, based on an older `main`) merged cleanly into the live-audio branch's much-evolved `jobs.py`/`cli.py` — both branches added purely additive job types/CLI commands, so conflicts were mechanical (interleaved insertions, not logical clashes). One real bug caught during resolution: `cli.py` had two competing `_LLM_PROVIDER_CHOICE` definitions after the merge — campaign-journal's own hardcoded `click.Choice(["ollama", "anthropic", "openai", "google"])` (predating the R20 fix that made this dynamic) plus live-audio-recording's `_llm_provider_choice()` deriving from `config.LLM_PROVIDERS`. Since Click decorators evaluate top-to-bottom at import time, `wisper campaigns journal --provider` would have silently kept using the stale hardcoded list (missing `ollama-cloud`) while every other LLM command used the dynamic one. Fixed by consolidating to one definition, moved above its first use. Also found `_run_journal_job()` used raw `job.log_lines.append(...)` in 4 places instead of `job.append_log()`, bypassing the R14 line-count cap every other job type goes through — low real-world impact (a journal fold's log is tiny) but inconsistent with the established pattern; fixed to match. Full suite green post-merge (1260 tests).

**Real-content pipeline test (2026-08-23).** Ran `wisper transcribe` →
`wisper summarize` → `wisper campaigns journal` end-to-end against 5 real
`GooeyCube-Hanataz` session recordings (2026-07-11 through 2026-08-22,
~13.7h of audio total) on real GPU hardware (RTX 3090, `large-v3-turbo`,
~25-60x realtime). All 5 now fully transcribed, summarized, and journaled;
`campaigns/gooeycube-hanataz/journal.md` reads coherently across sessions.

- **Found: `ollama.py`'s `complete_json`/`_post_chat` has no retry on an
  empty final response, and a reasoning/"thinking" model can burn its
  whole generation budget on the `thinking` stream field (silently
  discarded, correctly) and never reach `content` at all** — happened
  3 of 5 attempts summarizing one ~152k-char transcript against
  `ollama-cloud`/`deepseek-v4-flash:0731` with the strict `_SUMMARY_SCHEMA`
  format constraint; a bare retry of the same CLI command succeeded both
  times it was retried. A different model (local `gemma4:26b`, non-thinking)
  failed the same transcript a different way — ignored the `format`
  constraint entirely and returned prose instead of JSON. Root cause not
  fully isolated: `_post_chat` never checks a streamed chunk for an
  `"error"` field (only `message.content`), so a genuine swallowed API
  error and a clean `done:true` with zero content tokens both surface
  identically as `Ollama JSON response did not parse: ... Raw: ''`,
  misdirecting the error at the parse layer. Two follow-up diagnostic
  replays (same transcript, same model) both succeeded, so this is
  intermittent/content-dependent, not deterministic. Not fixed —
  flagged for a decision on whether to add retry-on-empty-content and/or
  surface the swallowed-error case distinctly before touching the shared
  client code (touches every provider).
- Not yet run: Section 3b of `LIVE_AUDIO_TEST_PLAN.md` (the web
  browser click-through — "Update journal" button, live job progress page,
  "View journal" not "View transcript", sanitized HTML rendering, "Fold
  all", nothing-pending empty state). The campaign is fully populated and
  the server is up, so this is ready to run whenever wanted.
- **`rebuild_campaign()` / "Rebuild journal" — shipped (2026-08-23),
  requested directly by the user after the Hanataz real-content test.**
  Redrives a whole campaign: re-summarizes every session transcript from
  scratch and rebuilds the journal from a clean start (two LLM calls per
  session), gated by a confirmation (CLI `--rebuild`/`--yes`; web button's
  client-side `confirm()`, same pattern as the existing "Fold all"
  button). `wisper campaigns journal <slug> --rebuild` / the campaign
  page's "Rebuild journal" button. Solid unit coverage (journal.py, CLI,
  job runner, web route, template) — full suite green. Deliberately NOT
  yet run live against the real `gooeycube-hanataz` campaign (would
  overwrite its already-good summaries/journal with real LLM cost) — that
  live run is still owed whenever wanted, and would also double as a real
  stress test of the `ollama.py` empty-response gap noted above (up to
  10 LLM calls for 5 sessions).
- **Transcript reorder — shipped (2026-08-23), requested directly by the
  user after noticing Hanataz's journal read out of order.** Traced to
  `Campaign.transcripts` being insertion order, not chronological — the
  interrupted-and-retried Hanataz transcription run appended session 2
  last instead of 2nd. `campaign_manager.reorder_campaign_transcript()`
  (single up/down swap) and `set_campaign_transcript_order()` (bulk
  replace, validated as a permutation) plus a CLI `wisper campaigns
  reorder <slug> <stem> --up/--down` / `--set` and ▲/▼ arrows on the
  campaign page's Episodes list. Hanataz's own order was fixed live
  through the new web buttons (07-11, 07-18, 07-25, 08-15, 08-22 —
  correctly chronological now); the journal itself hasn't been rebuilt
  against the corrected order yet (that's what "Rebuild journal" is for,
  whenever wanted).

The three remaining features all read the same `.summary.md` sidecars written by `wisper summarize`:

---

### 1. Combined summary (batch, full campaign)

Takes all session summaries for a campaign in one LLM call and produces a single consolidated document. Useful for retrospectives, onboarding a returning/new player, or a campaign wiki entry.

**Context ceiling:** A 20-session campaign with typical summaries (~1 k tokens each) is ~20 k tokens of input. Most providers handle this fine. At 50+ sessions it starts to strain context limits — the rolling journal is the better choice at that scale.

**Output:** `data_dir/campaigns/<slug>/combined_summary.md`

**Entry point:** "Generate combined summary" button on the Campaign page. Warn the user if session count is high.

---

### 2. "Previously on..." recap (player-facing, one-pager)

A short (200–400 word) player-facing doc generated before each session. Different tone from the DM journal — no spoilers, no DM-only info, focused on what the players experienced and remember.

**Input:** The most recent 1–3 session summaries (not the full journal).

**Output:** Displayed inline on the Campaign page or exported as a `.recap.md`. Shareable with players — could also be posted to a campaign Discord.

**Distinction from the journal:** The journal accumulates everything (DM view); the recap is a short selective retelling (player view) of the last session or two.

---

### 3. Hierarchical summaries (arc → campaign, scales to any length)

For very long campaigns (30+ sessions), group sessions into arcs, summarize each arc, then combine arc summaries into a campaign overview. Two-level LLM pipeline.

**When to build this:** Only if the rolling journal hits context limits in practice. The journal's incremental design means this is unlikely to be needed for typical campaigns. Defer indefinitely.

---

### Shared implementation notes

- All three read from the same `.summary.md` sidecar files written by `wisper summarize`
- Campaigns without any summarized sessions silently show nothing (the buttons are disabled or hidden)
- The `summarize.py` `SummaryNote` dataclass already captures loot, NPCs, follow-ups — the campaign-level LLM just needs to receive multiple of these and synthesize
- Reuse the shipped journal's patterns: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions`, and `JobQueue.submit_journal` + `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types (same SSE progress page as transcription/summarize/journal jobs). The combined summary and recap (#1, #2) fit this directly

---