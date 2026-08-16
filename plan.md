# wisper-transcribe — Open Items

---

## Live local recording + live transcription (mic + system audio)

**Context (2026-08-15):** New capture path, distinct from the Discord bot. Capture the local microphone *and* the system audio output (the other side of a call, a video, a game session) on the machine running the server, store them with the existing recording infrastructure, and show a near-real-time transcript while recording. The storage layer was explicitly designed for this — the five v1 file-format invariants in architecture.md ("Recording layer") and the stubbed `GET /recordings/{id}/live` route (currently 501) are the v2 hooks this feature cashes in.

### Decisions (locked 2026-08-15)

1. **Cross-platform from day one.** Windows (WASAPI loopback, native), Linux (PulseAudio/PipeWire monitor source, native), macOS (CoreAudio has no loopback — user installs [BlackHole](https://github.com/ExistentialAudio/BlackHole) and routes output through a Multi-Output Device; BlackHole then appears as an ordinary input device and flows through the same code path). "Cross-platform" therefore means one abstraction — *system audio is just another input device* — plus per-OS setup docs, not three capture backends. This is the same architecture OBS uses: its Desktop Audio source is the identical WASAPI-loopback call on Windows and the identical monitor-source capture on Linux, so we match OBS 1:1 there. On macOS, OBS 30+ uses native ScreenCaptureKit audio capture (macOS 13+; macOS 14.4 added Core Audio process taps). Calling those same APIs from Python was investigated (2026-08-15) and rejected for MVP: PyObjC *has* a ScreenCaptureKit wrapper, but system-audio capture through it is documented-broken (pyobjc issue #647 — `SCStreamErrorDomain -3805` or audio callbacks never fire, macOS 15/PyObjC 11), CMSampleBuffer→PCM extraction is C-level plumbing PyObjC handles poorly, and the Screen Recording TCC permission would attach to the bare `python` binary ("Terminal would like to record this computer's screen", with periodic re-prompts on macOS 15). ctypes against Core Audio process taps (14.4+) is unshipped territory ecosystem-wide. Hence macOS MVP = BlackHole. The clean native upgrade later is a small **signed Swift helper** speaking ScreenCaptureKit and piping PCM over stdout (same sidecar pattern as the Discord bot's Java JAR — own TCC identity, no bridge bugs); parked as a post-MVP option.
2. **Two separate tracks** (`mic`, `system`), mirroring the Discord per-user model, plus a mixed combined track for the transcribe hand-off. Clean per-track separation makes you-vs-them attribution trivial and keeps the post-session diarization pass honest.
3. **Rolling-window near-real-time transcription** reusing the existing faster-whisper pipeline — no new ML dependency. A few seconds of latency is accepted; true streaming ASR is out of scope.
4. **Web UI surface.** Device pickers + start/stop on the Record page; live transcript lines pushed over SSE on the recording detail page via the existing `/recordings/{id}/live` stub.

### Research findings

- **Capture library: `soundcard` (bastibe/SoundCard).** CFFI-based, no C extension, and effectively the only Python library that does WASAPI loopback *and* PulseAudio monitor capture out of the box behind one API (`get_microphone(id, include_loopback=True)`). PyAudioWPatch does WASAPI loopback but is Windows-only; stock sounddevice/PortAudio has no loopback. Make it an **optional extra** (`pip install wisper-transcribe[live]`), import-guarded like `mlx-whisper` — it is useless inside Docker (no host audio devices) and on headless boxes. Known quirk to handle at implementation time: capture threads on Windows need COM init (`CoInitialize`) — soundcard has threading caveats documented in its issues.
- **Resampling.** Devices deliver 44.1/48 kHz stereo float32. The existing `downsample_48k_stereo_to_16k_mono()` only handles the integer 48k→16k case; 44.1k→16k is non-integer. `scipy` is already a dependency (audio pre-loading bypass) — use `scipy.signal.resample_poly` in a generalized `resample_to_16k_mono()` helper alongside the existing one.
- **Storage reuse is near-total.** `SegmentedWavWriter` per track (layout `recordings/<id>/per-user/mic/NNNN.wav`, `.../system/NNNN.wav` — track names in place of Discord IDs), combined track via a third writer, `concat_wav_segments()` at finalise, `recording_manager` manifest + crash recovery (`reconcile_on_startup` is already source-agnostic). Invariant 5 ("live ticker watches for new segments while status is `recording`/`degraded`") was written for exactly this.
- **Mixing is our job this time.** Discord's JDA pre-mixes (`__mixed__`); locally we must mix mic+system ourselves. Apply the R12 lesson directly: drive the combined writer from a real-time tick (one mix per 20 ms of wall clock, pulling the latest frame from each track's buffer), never once-per-incoming-frame, and clip to int16 on sum. Mic and loopback devices run on different hardware clocks — for MVP, tolerate the drift (tracks are written independently; the tick-driven mixer absorbs it), no resync logic.
- **VAD for chunk cutting: no new dependency.** faster-whisper bundles Silero VAD (`faster_whisper.vad.get_speech_timestamps`) — usable standalone on the ring buffer to find silence boundaries.
- **One-job-at-a-time interaction.** `_model` is a module-level global; live transcription must own it. Run the live session's transcriber as a **`JOB_LIVE` job in the existing `JobQueue`** — the live session holds the queue slot for its duration, and refine/summarize/enroll jobs queue behind it. That is consistent with the invariant, not a violation of it; document it in the UI ("jobs will wait while live transcription is running").
- **Docker exclusion.** Containers cannot reach host audio devices on Windows/macOS at all. Feature is native-install only; the Record page hides the Local section when `soundcard` is not importable.

### Design sketch

- **`live_capture.py`** — device enumeration (mics + loopback candidates) and `LocalCaptureManager`, mirroring `BotManager`'s shape (`start_session` / `stop_session` / `_route_frame` / `_finalise`) but thread-based (soundcard recorders are blocking). Registers recordings through `recording_manager` like the bot does.
- **`Recording.source` field** — `"discord" | "local"` (default `"discord"` for legacy JSON). `voice_channel_id`/`guild_id` become empty strings for local recordings; add a `devices` metadata dict (chosen mic/system device names) for the detail page.
- **Live transcriber loop (`JOB_LIVE`)** — in-memory ring buffer of the mixed 16 kHz mono stream. Commit policy: cut a chunk at a VAD silence ≥ ~0.6 s, or force-cut at 15 s. Transcribe the committed chunk with the existing transcriber; **attribution by per-track energy** — compare mic vs system RMS/VAD over the chunk's span, label the line `You` / `Other` (dominant track). No pyannote in the live path (too heavy per chunk). Backpressure rule: if transcription falls behind capture, merge/skip pending windows — never queue unboundedly.
- **Live lines persistence** — append committed lines to `recordings/<id>/live_transcript.md` as they land (crash value), but the *authoritative* transcript remains the post-session full pipeline pass: the existing `POST /recordings/{id}/transcribe` hand-off already works unchanged because finalise produces `combined.wav`, and that pass gets real diarization + speaker ID.
- **Routes** — `GET /api/record/devices` (enumeration for the pickers), `POST /api/record/start-local` (device ids + optional campaign), stop via the existing stop flow; implement `GET /recordings/{id}/live` as SSE of committed lines with index-based resume (same pattern as the R14 job log stream).
- **Record page** — a "Local" section beside the Discord one: mic dropdown, system-output dropdown, Start/Stop, reusing the existing 1 Hz status SSE. Recording detail page shows the live transcript pane while status is `recording`/`degraded`.
- **Latency/model guidance** — chunk length + inference must stay under real-time. GPU: any model fine. CPU: recommend `base`/`small` for live mode; document in docs/scenarios.md.

### Open decisions (resolve during implementation)

- Exact loopback-device presentation in the picker (soundcard exposes loopbacks as microphones with `isloopback`-ish naming — verify per-OS labels and filter sensibly).
- Whether `You` lines auto-tag to an enrolled profile via a "this is me" selector at start (cheap win — mic track is single-speaker by construction). Leaning yes, Phase 3.
- `initial_prompt` chaining of prior committed text between chunks for context continuity — try it, drop if it causes repetition artifacts.

### Phases

1. **Capture layer (recording only, no live transcription).** `[live]` extra + import guard, `resample_to_16k_mono()`, `LocalCaptureManager` + real-time tick mixer, `Recording.source`, device-enumeration + start-local routes, Record page Local section, per-OS setup docs (incl. BlackHole on macOS). Tests: fake device sources modeled on `tests/_discord_fakes.py`; `soundcard` mocked via `sys.modules` injection (same trick as the LLM client tests). Already valuable standalone — recordings flow into the existing transcribe hand-off.
2. **Live transcription.** `JOB_LIVE`, ring buffer + Silero VAD chunker, energy attribution, `live_transcript.md`, SSE `/recordings/{id}/live`, live view pane, backpressure. Tests: mocked `WhisperModel`, scripted PCM buffers.
3. **Polish.** "This is me" profile auto-tag, model-size guidance in docs, scenarios/web-ui/setup doc updates, Known Constraints row (native-install only, live session holds the job queue).

---

## Senior review — closed 2026-07-16

The full-codebase senior review (findings R1–R38, opened 2026-07-15) is **complete**: all 38 findings remediated across six phase commits on `docs/senior-review` (Phase A quick wins → B config/CLI coherence → C leaks/memory → D web correctness/security → E Discord audio rebuild → F nits/R7/docs/locking). Suite grew 872 → 1025 over the review. Details live in the six phase commit messages.

**Open follow-ups from the review:**

- **Live Discord acceptance test (Phase E).** The recording pipeline rebuild (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is fully covered by synthesized-PCM decode tests, but the JDA→socket→Python path can only be proven with one real Discord session: record a few minutes with 2+ speakers, play the per-user WAVs, and run Transcribe on the recording.
- **`segment_manifest` never populated.** `_route_frame`/writer rotation never calls `recording_manager.append_segment`, so the UI's segment counters always read 0. Pre-existing latent bug found during Phase E, deliberately out of scope.

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

Four distinct features share the same infrastructure (reading multiple `.summary.md` files, writing a campaign-level output, running through the LLM pipeline):

---

### 1. Rolling campaign journal (incremental, bounded context)

A living document that grows with each new session. On each run the LLM receives `[current journal.md] + [new session.summary.md]` and rewrites the journal to incorporate the new session.

**Why this is the right default:** Context stays bounded — even session 50 only sends one session's worth of new material plus the current journal (~2–5 k tokens each). The journal acts as a compressed campaign memory.

**What it tracks across sessions:**
- Story arc progression and where each thread stands
- Active plot hooks (opened vs resolved)
- NPC roster: who appeared, what role they played, how the relationship evolved
- PC decisions that had lasting consequences
- Running loot/resource ledger (net gains/losses per session)

**Storage:** `data_dir/campaigns/<slug>/journal.md` — a single file that gets overwritten each time a new session is folded in. The individual session `.summary.md` files are never touched; they remain the source of truth.

**Entry point:** "Update journal" button on the Campaign page, enabled when new sessions exist that have not yet been folded in. Track this via a `journal_through: <session_stem>` frontmatter key in `journal.md` — compare against the campaign transcript list to know what's new.

**CLI:** `wisper campaign journal <slug> [--session <stem>]` — folds one session (default: latest un-journalled) into the journal.

---

### 2. Combined summary (batch, full campaign)

Takes all session summaries for a campaign in one LLM call and produces a single consolidated document. Useful for retrospectives, onboarding a returning/new player, or a campaign wiki entry.

**Context ceiling:** A 20-session campaign with typical summaries (~1 k tokens each) is ~20 k tokens of input. Most providers handle this fine. At 50+ sessions it starts to strain context limits — the rolling journal (above) is the better choice at that scale.

**Output:** `data_dir/campaigns/<slug>/combined_summary.md`

**Entry point:** "Generate combined summary" button on the Campaign page. Warn the user if session count is high.

---

### 3. "Previously on..." recap (player-facing, one-pager)

A short (200–400 word) player-facing doc generated before each session. Different tone from the DM journal — no spoilers, no DM-only info, focused on what the players experienced and remember.

**Input:** The most recent 1–3 session summaries (not the full journal).

**Output:** Displayed inline on the Campaign page or exported as a `.recap.md`. Shareable with players — could also be posted to a campaign Discord.

**Distinction from the journal:** The journal accumulates everything (DM view); the recap is a short selective retelling (player view) of the last session or two.

---

### 4. Hierarchical summaries (arc → campaign, scales to any length)

For very long campaigns (30+ sessions), group sessions into arcs, summarize each arc, then combine arc summaries into a campaign overview. Two-level LLM pipeline.

**When to build this:** Only if the rolling journal hits context limits in practice. The journal's incremental design means this is unlikely to be needed for typical campaigns. Defer indefinitely.

---

### Shared implementation notes

- All four read from the same `.summary.md` sidecar files written by `wisper summarize`
- Campaigns without any summarized sessions silently show nothing (the buttons are disabled or hidden)
- The `summarize.py` `SummaryNote` dataclass already captures loot, NPCs, follow-ups — the campaign-level LLM just needs to receive multiple of these and synthesize
- The rolling journal is the highest-value, most technically tractable feature — build it first; the others follow naturally from the same infrastructure
- All three non-hierarchical features fit into the existing `JobQueue` as new `JOB_CAMPAIGN_*` types, giving them the same SSE progress page as transcription and summarize jobs

---