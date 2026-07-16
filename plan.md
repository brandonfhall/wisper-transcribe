`# wisper-transcribe — Open Items

---

# Code Review Findings — 2026-07-15 (senior review)

Full-codebase audit on branch `fix/enrollment-audit`. All of `src/` was read end-to-end; every finding cites file:line and the load-bearing ones were grep-verified. Findings are the actionable work queue for the implementing engineer; suggested execution order at the end.

**Progress:** Phase A (R1, R3, R8, R15, R29, R35) completed 2026-07-15 (`fix: Phase A quick wins`). Phase B (R5, R19, R20, R21, R22, R23, R30, R33, R34) completed 2026-07-15 (config/CLI coherence — sentinel-default refactor, LLM provider metadata dedup, output-dir dedup, skip-logic dedup, config-set validation, record-start config fallback, web form enum validation, Anthropic default model id bump). Phase C (R9 all five, R10, R14, R26, R28) completed 2026-07-15 (leaks + memory — temp/orphan file cleanup incl. converted-WAV finally-unlink and `.mp3` clip lifecycle via shared `remove_profile_files`/`rename_profile_files`, chunked upload streaming, job-retention + log-line caps with SSE index translation, ffprobe-first `get_duration`, per-label embedding cache in `_interactive_enroll`). Phase D (R4, R6, R13, R16, R17, R18, R24, R25, R31) completed 2026-07-16 (web correctness/security — parameter-keyed ML model caches with no-poison-on-failure, standalone/recording enroll moved onto the JobQueue, generic job-error messages, 127.0.0.1 default bind + trust-model docs + open-data-dir POST, sanitizer scheme/tag hardening, X-Frame-Options DENY, shared `find_excerpt_clip` helper, unused `request` param removed, unified web/CLI speaker rename with campaign rekey). The optional subprocess-SIGTERM job cancel stays parked. Completed findings are removed from this list per plan.md rules. Suite was 872 at baseline, 878 after Phase A, 894 after Phase B, 921 after Phase C, 979 after Phase D.

## CRITICAL — broken features / guaranteed runtime errors

### R2 — Recording → transcription hand-off can never succeed (`combined_path` never set)
`Recording.combined_path` is assigned exactly once, to `None` (`recording_manager.py:259`), and never populated anywhere (grep-verified). `record.py:452` (`recording_transcribe_html`) requires `recording.combined_path` to exist → always redirects `?error=no_audio`. `BotManager._finalise` (`discord_bot.py:458`) closes the mixed-track segment writer but never merges `recordings/<id>/combined/*.opus` into a single file nor sets `combined_path`.
**Fix:** in `_finalise`, concatenate/transcode the combined-track segments to `recordings/<id>/combined.wav`, set `recording.combined_path`, save. Or change the hand-off to consume the segment directory directly.

### R7 — `wisper record list/show/transcribe/delete` CLI commands call 501 stubs
`routes/record.py:166-193`: `/api/recordings` (+ detail/transcribe/delete) return `_NOT_IMPLEMENTED` (501). The CLI (`cli.py:1380-1422`) calls exactly these endpoints, so four documented `wisper record` subcommands always fail with "Server returned 501". Working HTML equivalents exist (`/recordings`, `/recordings/{id}/transcribe`, …).
**Fix:** implement the JSON API by delegating to the same code the HTML routes use, or remove the CLI subcommands until it exists. Update `docs/cli-reference.md` accordingly.

## HIGH — correctness bugs and resource leaks

### R11 — `refine.apply_edits` does global substring replacement
`refine.py:189-201`: each accepted edit runs `body.replace(original, corrected)` over the whole body — a short `original` ("Dan" → "Don") also rewrites every occurrence inside longer words ("Dandy" → "Dondy") and inside `**Speaker**` labels. The edit-distance guard validates the *target*, not the *blast radius*.
**Fix:** word-boundary regex replacement (`re.sub(rf"\b{re.escape(original)}\b", ...)`), and skip lines that are speaker-label positions.

### R12 — Discord audio path: the same bytes are treated as both Opus and PCM
`discord_bot._route_frame` (`discord_bot.py:387-416`) takes each sidecar frame and (a) writes it to `SegmentedOggWriter` as an **Opus packet**, and (b) feeds it to `RealtimePCMMixer` as **48 kHz stereo PCM**. These interpretations are mutually exclusive — one of the two consumers is processing garbage. Compounding issues in `audio_writer.py`:
- Ogg page CRC is always 0 (`audio_writer.py:74`) — ffmpeg/pydub validate CRCs; decode of these files is at best warning-laden, at worst rejected (this feeds R1's enrollment path: `PydubSegment.from_file(..., format="opus")`).
- Lacing bug: a packet whose length is an exact multiple of 255 is missing the terminating 0-lace (`audio_writer.py:53-58`) → malformed page.
- `OpusHead` hardcodes mono/48 kHz (`audio_writer.py:83`) regardless of what's actually written; the mixed track writes 16 kHz mono PCM into it.
- `mix()` is called once per **incoming** frame (`discord_bot.py:415`), so with N concurrent speakers the combined track advances N×20 ms per real 20 ms — combined-track duration scales with speaker count.
**Fix:** decide the wire format once (plan.md says the sidecar sends PCM). Then: per-user writers should encode PCM→Opus (or store WAV segments), compute real Ogg CRCs, fix the lacing terminator, and drive `mix()` off a 20 ms clock (or per unique-frame-set), not per frame. This subsystem needs a focused pass with a real end-to-end decode test.

## LOW — smaller bugs, efficiency, style

### R27 — `aligner._assign_word_speakers` is O(words × turns)
`aligner.py:57-82`: linear scan of all diarization turns per word. A 3-hour session (~30k words × ~2k turns) is ~60M overlap computations in pure Python.
**Fix:** sort turns once, walk with a two-pointer/bisect. Keep `_best_overlap_speaker` for the no-words fallback.

### R32 — Minor per-module nits (batch these)
- `speaker_manager.py:128`: `except (RuntimeError, Exception)` — just `Exception`.
- `speaker_manager.py:423`: `None` stored into `dict[str, np.ndarray]` (typed lie; use a separate `failed: set[str]`).
- `transcriber.py:56`: MLX repo fallback f-string will 404 for unmapped sizes — raise a clear error instead.
- `transcriber.py:236`: `pbar.update(seg.end - pbar.n)` can go negative on non-monotonic segments — clamp.
- `routes/dashboard.py:28-29`: `__import__("os")` inline — import normally.
- `routes/speakers.py:20-21`: redundant `import os` inside `_clip_path` (already module-level).
- `formatter.update_speaker_names` (`formatter.py:247`): `**Old**` regex also rewrites matching bold text in the body — worth a docstring warning.
- `app.py:41-43`: `_INPUT_CSS.stat()` raises uncaught `FileNotFoundError` at startup if `input.css` is missing — guard it.
- `web/jobs.py:566`: `sorted(list(...)[::-1], key=..., reverse=True)` — replace with an explicit `(created_at, seq)` key or at least comment the reverse-then-stable-sort trick.
- `debug_log.Logger._patch_tqdm`: repeated `setup_logging()` calls stack tee-wrappers — make idempotent.
- `summarize._linkify`'s `(?<!\[)`/`(?!\])` guards only check one bracket char — double-wrap possible in edge cases.

## PROCESS / environment

### R36 — tqdm monkey-patching is load-bearing in three layers (accepted; document it)
`debug_log.Logger._patch_tqdm` (permanent tee), `jobs._run_transcription_job` (per-job capture + restore), and `pipeline._patch_tqdm_for_queue` (per-subprocess) all patch process-global tqdm state. It works because of the one-job-at-a-time invariant, but any concurrency change breaks all of it, and job cancellation only fires when tqdm writes (already noted elsewhere in plan.md). Action: `architecture.md` note tying the three together; revisit if R6's fix or multi-worker lands.

### R37 — Unlocked read-modify-write on shared JSON stores
`campaign_manager`/`speaker_manager` do unlocked load→modify→save of shared JSON (`recording_manager` got per-record locks; the others didn't). Two simultaneous wizard submits or campaign edits can lose writes. Low likelihood single-user; fix opportunistically by mirroring `recording_manager`'s lock pattern.

### R38 — Docs drift to fix alongside the above
When fixing: `docs/cli-reference.md` (R7), `architecture.md` (R12 audio format contract, R36 tqdm layers), CLAUDE.md Non-Obvious Gotchas if invariants change. (R16 trust model and R4 cache keys were documented in Phase D.)

## Suggested execution order

1. ~~**Phase A (small, surgical, high value):** R1, R3, R8, R15, R29 + env fix R35.~~ ✅ Done 2026-07-15.
2. ~~**Phase B (config/CLI coherence):** R5, R19, R20, R21, R22, R23, R30, R33, R34.~~ ✅ Done 2026-07-15.
3. ~~**Phase C (leaks + memory):** R9 (all five), R10, R14, R26, R28.~~ ✅ Done 2026-07-15.
4. ~~**Phase D (web correctness/security):** R4, R6, R13, R16, R17, R18, R24, R25, R31.~~ ✅ Done 2026-07-16. The scoped-in optional job-cancel option (1) (subprocess + SIGTERM) was NOT done — it stays parked in "Job cancellation — best-effort GPU stop" below.
5. **Phase E (Discord audio subsystem):** R2 + R12 together — needs a wire-format design decision first; do not start piecemeal. **Constraint from "DAVE Sidecar → Python migration" (below):** that section promises the future Java→Python sidecar swap leaves `SegmentedOggWriter` untouched — R12 changes the writer, so pick the wire format with the planned ~100-line Python sidecar in mind and update the migration section's "nothing else changes" claim in the same commit.
6. **Phase F (nits):** R11, R27, R32, R36–R38 opportunistically. (The formerly-empty `## UI Bugs` section was deleted when these findings were scoped — 2026-07-15.)

Each phase = one PR-sized branch, tests green + docs synced per Definition of Done, pause for user review between phases.

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

Nothing else changes — `SegmentedOggWriter`, the web UI, campaigns, CLI, and all tests remain unaffected.

**Structural fallback (Strategy B), if the native-binding ecosystem stalls:** both JDAVE and `davey` are small-maintainer libraries tracking a protocol Discord controls and can change. The only DAVE-churn-immune approach is to *not* implement DAVE at all — run a real Discord client joined to the channel and capture its client-side-decrypted audio via a virtual audio (loopback) device. Heavier operationally and loses per-speaker SSRC separation, so not worth building now — documented as the escape hatch if jdave/davey break on a future protocol bump.

---

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