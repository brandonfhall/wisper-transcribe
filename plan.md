`# wisper-transcribe — Open Items

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

## UI Bugs

---

## AUDIT (2026-07-12) — Enrollment & speaker identification pipeline

**Reported symptoms:** after the name-enrollment wizard, (a) not all speaker values in the transcript get updated; (b) voices are not always properly separated/identified in subsequent transcriptions.

Full trace of the web transcription→enrollment flow. Findings ordered by severity; each maps to at least one reported symptom.

### F1/F2/F3 — Fixed in phase 1 (2026-07-12, branch `fix/enrollment-audit`)

Unified both wizard submit paths (job-centric `transcribe.py` + transcript-centric `transcripts.py`) onto a single shared handler in `web/enroll_shared.py`:
- **F1** (job-path renames silently no-op once any profile exists): both GET and POST job-path routes now resolve `current_names` via `build_legacy_label_map()` the same way the transcript path always did, so a rename targets the transcript's *current* display name instead of a raw `SPEAKER_XX` string that no longer exists in the body.
- **F2** (untouched prefilled fields enroll junk "SPEAKER_XX" profiles): the template only prefills a field when a real (non-raw-label) current name is known — otherwise it's left empty with the raw label shown as a placeholder — and the server refuses any submission whose *new* name matches `^SPEAKER_\d+$`, regardless of what the template did.
- **F3** (every submit overwrites existing profile embeddings): the shared handler now skips the enroll step entirely when a submitted name is unchanged and a profile already exists; when a profile exists and something *did* change, it extracts and merges via `update_embedding()` (EMA) instead of calling `enroll_speaker()`; two raw labels assigned the same display name in one submit have their embeddings averaged (via a new optional `embedding=` param on `enroll_speaker()`) before being saved/merged.

See `architecture.md` → "Speaker Enrollment Web Flow" for the full current design. F4–F11 below are unaffected and still open at the time of this phase (F4 fixed in phase 4, see below).

### F4 — Fixed in phase 4 (2026-07-13, branch `fix/enrollment-audit`)

Reworked `match_speakers()` (`speaker_manager.py`) from per-label-best-only greedy assignment to pair-scored greedy assignment: every (label, profile) similarity is computed up front, pairs are sorted by similarity descending (ties broken by label then profile name), and consumed in an exclusive pass that assigns whenever both sides are still free — so a label whose top choice was already claimed naturally falls back to its next-best *unused* profile above threshold instead of going straight to Unknown. A new `allow_many_to_one: bool = False` keyword adds a second pass letting any label still unassigned claim its best profile even if another label already has it (still threshold-gated), for pyannote over-segmenting one real speaker into two labels. Both call sites — `pipeline.process_file()` (~line 512) and `cli.py`'s `speakers test` (~line 720, which now also prints a note when many-to-one is active) — pass `allow_many_to_one=(num_speakers is None)`, since pinning the count is the user asserting one label per person. Unassigned labels (failed embedding, below threshold, or an exclusivity loser with many-to-one off) become "Unknown Speaker N" numbered by sorted label order, not similarity order, so numbering is deterministic.

See `architecture.md` → "Shared voice embeddings + per-campaign rosters" for the full current design. F8–F11 below are unaffected and still open.

### F5 — Fixed in phase 2 (2026-07-12, branch `fix/enrollment-audit`)

Moved the temp web-upload audio next to its transcript at job completion instead of leaving it in the OS tempdir:
- `JobQueue.submit()` now captures `Job.is_web_upload` from the *original* `wisper_upload_*` basename before the friendly-name rename strips that prefix. On completion with diarization data, `_move_upload_to_output()` (`web/jobs.py`) moves the file into the output dir as `<stem><suffix>` (collision-safe counter suffix) and `job.input_path` is updated to that durable path before the `_diar.json` sidecar is written — so the sidecar's `input_path` survives restarts instead of pointing at a file `_cleanup_orphaned_uploads()` (or a lucky crash) already deleted.
- When a job completes with no diarization data (no sidecar will ever be written to record a moved file's path) or fails/is cancelled, the temp file is deleted instead of moved — closing the original disk leak.
- `apply_enrollment_submit()` now returns an `EnrollmentResult(current_names, audio_missing)` instead of a bare dict; both POST routes redirect with a generic `?notice=enroll_audio_missing` flag (no paths/exception text) when enrollment was skipped, and `transcript_detail.html` shows a banner explaining that names were updated but enrollment didn't run. Both GET wizard routes also show a pre-submit warning banner when the recorded audio path is missing.
- `POST /transcripts/{name}/delete` (and the bulk-delete route) now also delete `<stem>_diar.json` and the durable audio file it references — that audio exists only to back the wizard for the (now-deleted) transcript, so leaving it behind would be a permanent leak in the same spot F5 just fixed. The delete only targets paths that resolve inside the output dir, so legacy sidecars still pointing at a tempdir path are left untouched.

See `architecture.md` → "Job Queue" and "Speaker Enrollment Web Flow" for the full current design. F4, F6–F11 below are unaffected and still open at the time of this phase (F4 fixed in phase 4, see below).

### F6/F7 — Fixed in phase 3 (2026-07-12, branch `fix/enrollment-audit`)

Replaced the reconstructed, fragile raw-label→display-name map with a persisted, authoritative one, and made renames single-pass instead of a sequential mutating loop:
- **F7** (`_build_legacy_label_map` interval matching is fragile): `pipeline.process_file()` now exports the exact `speaker_map` local it hands to the formatter into `_result_store["speaker_map"]`; `jobs.py` carries it on `Job.speaker_map` and `_write_enrollment_sidecar()` persists it into `_diar.json`'s `speaker_map` key. A new `enroll_shared.resolve_current_names(md_path, diar, segments)` is the single resolution path every caller (both wizard GET routes' prefill, and `apply_renames`'s old-name resolution) now goes through: it prefers the sidecar's `speaker_map` and only falls back to `build_legacy_label_map()`'s interval-matching heuristic for sidecars written before this key existed. `apply_renames()` updates the sidecar's `speaker_map` after every successful rename, keeping it authoritative across repeated wizard visits.
- **F6** (sequential global renames cross-contaminate): `apply_renames()` no longer loops `update_speaker_names()` over a mutating content string. It now attributes every markdown block to a raw pyannote label exactly once from the *original* content (`_attribute_block_to_label()` — interval containment, with an unambiguous-name-match fallback for low-confidence blocks), then rewrites only the blocks whose raw label was actually renamed via `formatter.rewrite_transcript_blocks()`. This handles a same-submit swap (Alice↔Bob) and a shared-display-name rename (two raw labels both currently "Dan", only one renamed) correctly — neither is representable as a single global find/replace. The YAML frontmatter `speakers:` list is still rewritten by name (`_rewrite_frontmatter_names()`), but as one simultaneous-pass substitution rather than a sequential loop, and skipping any old name shared by more than one raw label (ambiguous — left alone rather than guessed at). F11's underlying regex limitations (quoted YAML, prefix collisions) are unchanged and still out of scope.

See `architecture.md` → "Speaker Enrollment Web Flow" for the full current design.

### F8 — Fixed in phase 5 (2026-07-13, branch `fix/enrollment-audit`)

`aligner.py` used to assign each whisper segment to the single diarization turn with max overlap, so a segment spanning 2+ speaker turns had the minority speaker's words attributed to the majority speaker — the structural ceiling on "properly separate voices," since no enrollment fix could recover text merged at alignment time.

Fixed by requesting word timestamps on both transcription backends and rewriting `align()` to split at word boundaries:
- `transcriber.py` now passes `word_timestamps=True` to the faster-whisper `_model.transcribe()` call (the MLX path already requested it but discarded the words) and both paths build a `words: list[Word]` on each `TranscriptionSegment` (`Word(start, end, text)`, leading-space stripped from faster-whisper's `.word`).
- `aligner.align()`: when a segment has words, each word is assigned to the diarization turn with max time overlap; a word overlapping no turn inherits the nearest turn's speaker by word-midpoint distance; with no diarization at all, a word inherits the previous word's speaker (UNKNOWN for the first). Consecutive same-speaker words are grouped into one `AlignedSegment` per run, so a segment inside one turn still yields exactly one segment, and an A/B/A sandwich yields three. Segments without word data (`None`/`[]` — legacy callers, mocked tests) take the original whole-segment max-overlap fallback, unchanged.
- `formatter._merge_consecutive()` already collapses consecutive same-speaker blocks, so the extra splitting can't fragment the rendered transcript — no formatter changes needed.
- `pipeline.py`'s `parallel_stages` path moves segments across a `ProcessPoolExecutor` boundary via pickle (not JSON), so the new `words` field survives automatically — no serialization code needed there. The `_diar.json` sidecar (`web/jobs.py`) only ever serialized `DiarizationSegment` (start/end/speaker), never `TranscriptionSegment`/`AlignedSegment`, so `words` never crosses a JSON boundary either.

See `architecture.md` → module map (`aligner.py`, `models.py`) and the Processing Pipeline diagram's ALIGN step for the current design.

### F9/F10/F11 — Fixed in phase 6 (2026-07-13, branch `fix/enrollment-audit`)

Final hardening batch — three small, independent fixes shipped together:

- **F9** (excerpt fallback could serve another transcript's audio): `speaker_excerpt` (`web/routes/transcribe.py`) globbed `*_excerpt_{label}.mp3` across the **entire output dir** when the in-memory job's `clip_path` was missing/stale, potentially serving a different transcript's same-labelled clip. Now scoped: if the job is present and has `output_path`, the fallback only looks for `{Path(job.output_path).stem}_excerpt_{label}.mp3`. If the job is gone entirely (server restarted), the route 404s rather than globbing blindly — confirmed the transcript-centric wizard (`transcripts.py`'s `GET /transcripts/{name}/enroll`) passes its own `excerpt_base_url` (`/transcripts/{name}/excerpt`), a separately-implemented, already-transcript-stem-scoped route, so this route 404ing on a gone job doesn't break that path.
- **F10a** (excerpt cut at wrong segment): `_extract_speaker_excerpts()` (`jobs.py`) now cuts each speaker's clip at their LONGEST aligned segment (by `end - start`) instead of the first occurrence, and persists that same segment's text to the `.txt` sidecar — avoids a short misattributed interjection dominating the sample clip.
- **F10b** (embedding source quality): `extract_embedding()` (`speaker_manager.py`) delegates segment selection to a new pure helper, `_select_embedding_segments()`: prefers up to 5 "solo" segments (no strict time-overlap with any other speaker's segment) in the 2.0-20.0s range, longest-first; falls back to all solo segments longest-first if none fit that band; falls back to the old longest-5-regardless-of-overlap behavior if there are no solo segments at all. Avoids averaging in segments where cross-talk/music bleeds into a turn.
- **F11** (frontmatter rename regex defects): added `formatter.rewrite_frontmatter_speakers()` — parses the frontmatter as YAML (`yaml.safe_load`/`yaml.dump`) and matches/replaces `speakers[].name` values exactly, instead of a regex that both corrupted prefix matches ("Dan" renaming into "Dan Smith") and silently missed `yaml.dump`-quoted names (`- name: 'O''Brien'`). `formatter.update_speaker_names()` and `enroll_shared.apply_renames()` (which deleted its own `_rewrite_frontmatter_names()`) both now call this one shared helper. The document body is preserved byte-for-byte; all renames apply in one simultaneous pass so a same-submit swap still works correctly (F6 property preserved).

See `architecture.md` → "Speaker Enrollment Web Flow" (excerpt fallback, embedding segment selection) and module map (`formatter.py`, `speaker_manager.py`) for the current design.

### Suggested fix order

1. ~~**F1 + F2 + F3**~~ — done, see "F1/F2/F3 — Fixed in phase 1" above.
2. ~~**F5**~~ — done, see "F5 — Fixed in phase 2" above.
3. ~~**F6 + F7**~~ — done, see "F6/F7 — Fixed in phase 3" above.
4. ~~**F4**~~ — done, see "F4 — Fixed in phase 4" above.
5. ~~**F8**~~ — done, see "F8 — Fixed in phase 5" above.
6. ~~**F9/F10/F11**~~ — done, see "F9/F10/F11 — Fixed in phase 6" above.

Related plan item ("Enrollment wizard — synchronous embedding extraction blocks the browser", JOB_ENROLL) is implemented — see "Phase 2.5 split" in `architecture.md` → "Speaker Enrollment Web Flow" and the "Job Queue" section's `JOB_ENROLL` entry.

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