# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Missing transcript file after a successful Transcribe job

A local-recording transcription once reported COMPLETED ("Wrote `<id>.md`") but the file never existed; root cause unknown. **Detection is in place:** the job fails with "Transcript file missing after write" and logs the transcripts folder, and job history keeps that log across restarts. **Next step:** if it recurs, check `/jobs/history` for the job's log and output root; run with `WISPER_DEBUG=1` to capture more (`LIVE_AUDIO_TEST_PLAN.md` §4a). The recording hand-off no longer copies `combined.wav` into the output dir (it reads it in place), so the job's input is `recordings/<id>/combined.wav` and the output is named by `output_stem`.

### Docker Desktop + native CLI on one data dir can corrupt the DB

With the web server in Docker Desktop (Mac or Windows) and `./data` bind-mounted, a native `wisper` command pointed at the same `./data` (via `WISPER_DATA_DIR`) and writing at the same time can corrupt `wisper.db`: file locks don't cross the Docker Desktop VM boundary. Reproduced 2026-09-30 (one host writer plus one container writer gave `database disk image is malformed` and lost updates). Container + container is fine (15,000/15,000 writes), and native Linux Docker is unaffected.

**Status:** guarded, not fixed. The runtime lease (`db.py`, `runtime_leases`) makes the second runtime refuse to start when the container is inside Docker Desktop's VM; it is advisory (two processes starting in the same second could both pass). Documented in `docs/docker.md` ("One way of running at a time"). A real fix would need a lock that crosses the VM (e.g. routing host CLI writes through the container's server API).

### Ollama: empty responses from reasoning models

`ollama.py`'s `_post_chat` reads only `message.content` from the stream. A reasoning model can spend its whole budget on the `thinking` field and return no content, and a streamed `"error"` field is never checked. Both cases surface as `Ollama JSON response did not parse: ... Raw: ''`, which blames the parse layer. Seen intermittently when summarizing a ~150k-char transcript with `ollama-cloud`; a plain retry succeeded. A non-thinking local model instead ignored the `format` schema and returned prose.

**Decision needed:** add retry-on-empty-content and/or surface a streamed error distinctly. Either change touches the shared client code used by every provider.

---

## Manual verification owed

- **Live recording + campaign journal:** `LIVE_AUDIO_TEST_PLAN.md` — real-device capture, live transcript, journal browser flows, bulk delete, busy-queue notice.
- **Live Discord acceptance test.** The recording pipeline (WAV segments, `__mixed__` combined track, `combined_path` hand-off) is covered by synthesized-PCM tests, but the JDA → socket → Python path needs one real session: record a few minutes with 2+ speakers, play the per-user WAVs before binding any speaker (a bound user's track is deleted when the session ends or on binding), and run Transcribe.
- **Windows launcher dependency refresh.** `start.bat` reinstalls dependencies when `pyproject.toml` is newer than `.venv\.wisper-deps`. The one-time reinstall after a `git pull` is confirmed on Windows (2026-10-03); still owed: a second launch skips it.
- **GPU Docker image on an NVIDIA host.** The "Docker Build" workflow is disabled on GitHub, and the GPU image can only be built (not run) without NVIDIA hardware. Confirm a diarized job on the GPU image downloads the alignment model into `./cache/` and logs "Aligned words".
- **SQLite storage in a browser and on real capture** (automated coverage: `test_e2e.py`, `test_schema.py`). With `WISPER_DATA_DIR` pointing at a copy of real data:
  - Journal: the stale-journal notice (move a folded session to another campaign) on the Campaign and Journal pages; **Rebuild journal** vs **Rebuild from transcripts** confirmations and their call counts; journal **Download** includes `journaled_sessions`.
  - Job history page, filters, paging, and a historical job's page after a restart.
  - Recording and wizard checks moved into "Storage trim" Phase 9 step 9: Phases 3–5 change both flows, so checking them first would be wasted.
  - Search: edit a transcript in Obsidian while the server runs and search for the new words (the result shows "changed — reindexing", then matches after a reload).
- **macOS loopback.** Record page on a Mac with BlackHole installed: BlackHole appears under System Audio and captures audio.

---

## Dependency pins

- **Drop `av<19`** (pyproject) once a faster-whisper release stops passing `metadata_errors=` to `av.open()`; check with a CPU Docker build and one transcription.

---

## Storage trim — keep one audio copy per transcript (`feat/storage-trim`)

**Goal:** every transcript keeps exactly one compact copy of the audio it was made from, and nothing else. Renaming a transcript never loses its files.
- **Today** a web upload is kept whole next to its transcript (`_move_upload_to_output`). On real data that's 13 GB for two sessions, ~96% of it H.264 video, plus two audio tracks that are never read.
- **Uploads** keep `<stem>.flac`: 16 kHz mono, the one track the pipeline used. That's ~85 MB per hour instead of ~2.8 GB per hour of video.
- **Recordings** keep `combined.wav`.
- **Both behave the same:** the transcript page plays the audio back and can re-transcribe from it, and the wizard and Re-match can always re-read it (Brandon, 2026-10-03).
- **Renames:** a transcript renamed outside wisper is matched automatically where possible, its companion files follow its name, and anything left unclear is listed for Brandon to resolve. Nothing is deleted silently.
- **Campaign subfolders** in the output root are a separate plan ("Campaign folders"), built after this one on its rename helper.

### Status

| Phase | State | Commit |
|---|---|---|
| 1 — File registry | done | 05d0b52 |
| 2 — Renames / Needs attention / campaign delete | done | b538301 |
| 3 — Enroll from stored embeddings | done | 2a135af |
| 4 — Extract audio, keep FLAC | done | f97a271 |
| 5 — Trim recordings | done | (this commit) |
| 6 — `wisper storage trim` | not started | |
| 7 — Playback | not started | |
| 8 — Re-transcribe | not started | |
| 9 — Final review | not started | |

Run on the Mac (2026-10-03). Rehearsals use a scratch copy of the Mac's own data, not the Windows data. Brandon waived the between-phase review pause for this run. Each phase is still committed and pushed separately.

**Phase notes for later phases.** Phase 1:
- **Rehearsal (Mac data, v8 → v10):** clean. `files` counts matched the disk: 4 transcripts, 1 summary, 4 sidecars, 30 excerpt pairs, 4 audio, 6 clips, plus the recording's `combined`, 2 `per_user` and `live_draft`. Pages rendered, and the guard refused the server with `output_dir` outside the copy. Docker rehearsal not done.
- **API beyond the plan:** `file_registry.forget_id`, `drop_from_report` (for Needs attention's Forget/Delete), `sync_if_due`, `reset_state`, and `recording_manager.register_capture_files` (used by both finalisers).
- **Deviations accepted:**
  - `move()` treats a case-only target as the same file only via `os.path.samefile`.
  - `save_summary`, `save_transcript`, and `.md.bak` registration resolve against the configured output root, not the `.md`'s folder, so a CLI `--output` file is never registered.
  - No test for "a profile key failing the `files.rel_path` CHECK aborts the rename": no key passing `validate_path_component` can fail it.
- **Minor, open:** with `WISPER_OUTPUT_DIR` unset, a CLI command creates the configured output folder (`path_utils.get_output_dir` mkdir) before the guard refuses. It's empty and harmless; the server path creates nothing.

Phase 2:
- **Rehearsal (Mac data):** a `.md` renamed in the shell was matched on restart, and all 18 of its files and rows followed. An orphan `ghost.summary.md` was listed, then deleted through the route; `../wisper.db` got 400.
- **API beyond the plan:** `relink()` returns the conflict list; `file_registry.is_registered`; `Attention.total`; `delete_campaign` raises `KeyError` for an unknown slug before deleting anything.
- **Behaviour:** `reconcile(sync="never")` stays "never" after a rename.

Phase 3:
- **Rehearsal (Mac data):** with one transcript's audio deleted, the wizard enrolled a speaker from saved voice data in under a second. The excerpt was copied as the profile's clip, and the clip was registered.
- **Behaviour:** a job-page redirect after a partial enrollment shows the `enroll_audio_missing` notice too.

Phase 4:
- **Rehearsal (Mac data, real MLX transcription on MPS):**
  - Uploaded a 45 s `.mp4` with video and two audio tracks. The upload was gone within 3 s of submit.
  - The job kept `Test Session.flac` (16 kHz mono, ~1 MB) plus the transcript files, all registered. The temp folder was removed, and `source_file` named the `.mp4`.
  - `name-check` returned the `.md` clash with its modified time, and a re-upload without Overwrite was refused.
  - A recording hand-off wrote no `<id>.wav`, left `combined.wav` alone, and gave the transcript no `audio` row.
- **Seen, not caused by this branch:** speaker matching printed a `nan` similarity for a speaker with a very short excerpt (`SPEAKER_00 → Announcer (nan)`). Worth a look separately.
- **Implementation notes:** `_keep_audio()` in `jobs.py` holds step 6.2. `job.upload_dir` is left set after cleanup.

Phase 5:
- **Rehearsal (Mac data):**
  - The real `trim_recording_audio` on the local recording freed 2.4 MB (`combined/` plus both `per-user` tracks) and kept `combined.wav` and `live_transcript.md`.
  - A second run freed 0.
  - It trims a recording whose `capture_status` is `failed` too, since `combined.wav` verified complete.
- **Beyond the plan:** the recording page's `no_audio` banner reads "The audio for this action is not on disk." Test modules for capture patch `trim_recording_audio` with an autouse fixture.

### How to run this plan

- **Roles:** Opus is the orchestrator; Sonnet workers each implement one phase.
  - The orchestrator hands a worker one phase, then reviews the diff against that phase's **Done when** and the documentation standard below. It runs the phase's tests, then commits.
  - A worker never starts the next phase.
- **Branch and commits:** branch `feat/storage-trim`, one commit per phase. Push after each commit, then **pause for Brandon's review before the next phase**. Don't open a PR until Phase 9 passes and Brandon approves it.
- **Busy machine:** don't commit, and don't run the full suite, while Brandon has transcription jobs running. Ask him first. Edit files only.
- **Read before starting:** `CLAUDE.md`, the Findings and Decisions below, and the phase's **Read first** list. For phases that add or change a web route, also `.claude/rules/web-security.md`.
- **Data safety:** never run anything against the real data dir **or the real transcripts folder**. Manual checks use a copy and always set **both** variables: `WISPER_DATA_DIR=<copy>/data` and `WISPER_OUTPUT_DIR=<copy>/output`.
  - A copied `config.toml` still names Brandon's real `output_dir`, so `WISPER_DATA_DIR` alone would act on his real transcripts.
  - Phase 1 makes a branch build refuse to start unless both are set; see below.
- **Migrations on a live install:** Phase 1 adds migrations v9 and v10. From that commit until Phase 9, the branch keeps `db.SCHEMA_FROZEN = False`. Never set it to `True` before Phase 9.
  - With it `False`, a branch build refuses Brandon's default data dir, and (Phase 1 adds this) refuses to start unless `WISPER_OUTPUT_DIR` is set.
  - **No separate worktree** (Brandon, 2026-10-03). While the Mac's repo folder is on this branch, his everyday wisper there won't open his real data; he switches to `main` to use it. Before switching, commit or stash.
  - Every phase that changes the schema, or data files, rehearses on a fresh scratch copy of Brandon's data dir and output folder first.
- **Rehearsal servers must not disturb a live server on the same machine.**
  - Startup cleanup sweeps the system temp dir (`app._cleanup_orphaned_uploads`, `app.py:70`, run at startup), which would delete a live server's in-progress upload. So a rehearsal server runs with `TEMP`/`TMP`/`TMPDIR` pointed at a scratch folder and `--port 8090`, and only while Brandon's queue is idle.
  - **Docker (`wisper-cpu-web`)** may run against a *separate* scratch copy to check Linux paths, through a compose override that mounts all three (`data`, `output`, **and** `recordings`; see `docker-compose.yml:21`) from that copy. It never mounts the repo's `./data`, `./output`, or `./recordings`. A host process and a container never share one copy: the runtime lease refuses it, and file locks don't cross the Docker Desktop VM.
- **Concurrency scope** (Brandon, 2026-10-03): wisper is single-user. The only concurrent writers to design for are one web server and one CLI command on the same machine (plus the Docker Desktop host/container case the runtime lease already guards). Don't design for multiple users or multiple servers.
- **Database code convention:** every new function that touches the database takes `conn: Optional[sqlite3.Connection] = None`. Given one, it runs inside the caller's transaction; without one, it opens its own `db.transaction()`. **Never call `db.connect()`, `db.connection()`, or `db.transaction()` while this thread holds a transaction; pass `conn` instead.** A second transaction waits out the 5 s busy timeout and fails ("database is locked"). Even a read-only `connect()` can take the write lock: it refreshes the runtime lease (`_refresh_lease`, `db.py:797–799`, `876–883`) at most every 30 s. Existing private helpers (`ensure_row(conn, …)`) already follow this rule.
  - Functions that move or delete *files* take no `conn`: files change only after the owner's transaction commits ("companion files follow the row").
- **Working on the Mac:** this branch is built on Brandon's Mac, but the live install it was planned against is his Windows PC.
  - **Rehearsal data:** copy the Windows data dir and output folder to the Mac as the rehearsal reference (the expectations in Phase 6's manual check describe that data), or rehearse against the Mac's own data and say which in the phase report.
  - **Windows-only behaviour can't be reproduced on macOS:** `os.replace` and `unlink` fail on a file another program holds open (Obsidian, Explorer, the audio player), with WinError 5/32/33. Every `file_registry` move, rename, and delete catches `PermissionError`/`OSError`, reports it (Needs attention, or the job log), and leaves the row consistent with the disk. Test it on the Mac by monkeypatching `os.replace`/`Path.unlink` to raise `PermissionError`.
  - **CI's `windows storage` job** (`.github/workflows/ci.yml:65`) runs a fixed list of test files on a real Windows filesystem. Add every new storage test file to it: `tests/test_file_registry.py` (Phase 1), `tests/test_storage_trim.py` (Phase 6), plus `tests/test_audio_utils.py` and `tests/test_web_jobs.py` (Phase 4: `encode_flac`'s `os.replace`, upload-folder cleanup), and any other test that renames, moves, or deletes files.
  - **Unicode:** macOS may store file names in NFD. `file_registry` stores the on-disk spelling and compares NFC (Phase 1, "Unicode and case"). Test it with an NFD-named file.
  - **Case:** macOS and Windows filesystems are normally case-insensitive and Linux (Docker) is not, so the case-only rename tests patch `_is_case_insensitive` both ways.
  - **Embedding backfill** in `wisper storage trim` runs on MPS on the Mac.
  - **Before Brandon launches the merged build on Windows:** rehearse the migration and `wisper storage trim` on a copy of the Windows data, *on the Windows PC* (`WISPER_DATA_DIR`/`WISPER_OUTPUT_DIR`), as the last check (Phase 9).
- **Test rules** (CLAUDE.md "Testing Rules"):
  - No GPU, network, or real audio.
  - Make tiny synthetic WAVs with the stdlib `wave` module.
  - Mock ffmpeg at the helper named in each phase, and have the mock create the output file it was asked for.
  - Seed data with `tests/_seed.py` (`seed_recording`, `seed_sidecar`, `seed_job`, …). `seed_sidecar` stores `input_path` only when it's inside the `.md`'s folder, and stores embeddings only when `embedding_space` is set.
  - Functions imported inside other functions (`convert_to_wav`, `extract_embedding`) are patched on their source module (`wisper_transcribe.audio_utils.convert_to_wav`), as existing tests do.
- **Existing tests:** each phase lists the existing tests it must rewrite. A test outside that list that starts failing means the step was misread: stop and report.
- **Line numbers** below are from `main` at `abf0c6d`; find each function by name if they've drifted. `static/` means `src/wisper_transcribe/static/`; templates are `src/wisper_transcribe/web/templates/`.
- **When the code disagrees with this plan** (a function moved, behaviour differs), stop and report it to the orchestrator. Don't improvise a different design.

### Documentation standard (every phase; the orchestrator rejects a phase that breaks it)

1. **State current facts and the reason, never history.**
   - Banned in docs, docstrings, and comments: "now", "no longer", "used to", "previously", "changed to", "instead of the old …", "new:", dates, phase numbers, finding or PR numbers, "storage trim".
   - History goes in the commit message.
   - `plan.md` is the one exception, and its content is removed when the work ships.
2. **One fact per bullet or sentence.** Comments are one line of *why*, and never restate the code. Docstrings say what and why, not how it was debugged.
3. **Edit the doc where the fact already lives.** Don't append a "Storage changes" section anywhere.
4. **Remove every statement the phase makes false.** Each phase lists the known stale lines; also `grep` for the old behaviour's key words.
5. **`README.md`** changes only its description paragraph, quick start, or docs table (CLAUDE.md "Documentation Rules"). The one exception is the first-audio-track paragraph in Phase 4, which Brandon asked for.
6. **`architecture.md`:** flat facts plus why. When a Module Map line grows past ~3 clauses, move the detail into a bulleted subsection.
7. **Scar-tissue check before each commit.** Each hit must be rewritten, or clearly be a different sense of the word (e.g. "now" meaning the current time).
   ```bash
   git diff -U0 -- . ':(exclude)plan.md' | grep -E '^\+' \
     | grep -niE "\b(now|no longer|used to|previously|changed to|instead of the old|phase [0-9]|storage trim|20[0-9]{2}-[0-9]{2})\b"
   ```

### Findings (verified on `main` at `abf0c6d`, 2026-10-03)

- **The pipeline reads only the first audio track, at 16 kHz mono.**
  - `audio_utils._extract_first_audio_track` runs `-map 0:a:0 -ac 1 -ar 16000` into a `NamedTemporaryFile`.
  - `convert_to_wav` returns a 16 kHz mono WAV input unchanged.
  - `process_file` deletes only a WAV it created (`pipeline.py:693`, `if wav_path != path`).
  - Excerpts are then cut from `job.input_path` (the original upload).
- **`process_file` names its output after the input:** `out_dir / (path.stem + ".md")` (`pipeline.py:477`). The default title (`:647`) and the frontmatter `source_file` (`:648`, shown as SOURCE on the transcript page) also come from the input path. `parallel_stages` derives no names; `_folder_output_path` (`:717`) is CLI-only.
- **`JobQueue.submit()` renames *any* input** whose stem differs from `original_stem` (`jobs.py:579–584`), not just uploads.
- **Who reads an upload's stored audio after the job:**

  | Reader | Needs full audio? |
  |---|---|
  | Transcript page | No; it serves only `<stem>_excerpt_<label>.mp3` and the profile clip. |
  | Re-transcribe | Recordings only, from `recordings/<id>/combined.wav`. Uploads have none. |
  | Wizard enrollment (`_run_wizard_enroll` → `enroll_profiles`) | **Yes, redundantly.** It re-decodes the whole file to run `extract_embedding` per label. A web job already stored that exact vector in `transcript_speakers.embedding` (same function, model, and 30-segment selection, via `match_speakers`). |
  | Profile reference clip (`_save_reference_clip`) | Yes; 12 s cut from the audio. An excerpt clip of the same speaker already exists. |
  | Re-match backfill (`speaker_registry._backfill_embeddings`) | Only for transcripts with no stored embeddings (made before #63). |

- **Embeddings are always stored for web jobs.** `match_speakers` extracts every label's embedding even with no eligible profiles (`speaker_manager.py:565`), and `write_sidecar` stores a row for every label with a name or an embedding (`transcript_store.py:657`). `speaker_registry.embeddings_from_sidecar(diar)` (`:40`) already returns them, current space only (`None` otherwise).
- **`read_sidecar()` returns `None` when `<stem>_diar.json` doesn't exist** (`transcript_store.py:572`).
  - No sidecar is written without diarization (`_write_enrollment_sidecar` returns early, `jobs.py:172`), and `register(origin="job")` unlinks the old one on every overwrite (`:248–252`).
  - It also returns early before resolving `input_path` (`:597`, `:600`).
- **`write_sidecar` deletes the previous audio** when `audio_rel_path` changes, including to NULL (`transcript_store.py:674`). An input outside the output root stores NULL, but only because `db.to_rel` raises `ValueError`. An output root that contains `recordings/` would store `combined.wav` as `audio_rel_path`, and a transcript delete would then delete it.
- **Companion files are tracked by name only.** `<stem>.summary.md`, `<stem>_diar.json`, and `<stem>_excerpt_<label>.mp3/.txt` are derived from `transcripts.stem`. Only the audio has a stored path (`audio_rel_path`), because its name can carry a suffix. `_companion_paths(stem, output_dir, audio_rel_path)` (`transcript_store.py:273`) lists all of them except the `.md`.
- **Renaming a transcript outside wisper loses its companions.**
  - `relink()` (`:486`) renames only the row.
  - Reconcile's case-only rename (`:437–443`) renames only the row.
  - The startup sweep (`:456–471`) deletes any companion whose stem matches neither a `.md` nor a row, comparing case-sensitively. So after either rename, the next start deletes the summary, sidecar, and excerpts.
  - A rename keeps a file's size and mtime, so they identify a renamed file. The plan keeps them in the `files` registry (Phase 1). `search_index_state` also has them, but only for indexed files.
- **CLI runs** pass `embeddings=None` and never copy, move, or delete the user's input file.
- **Recordings are 16 kHz mono PCM**, ~345 MB per 3 h copy. A transcribed recording holds up to five copies:
  - `combined/NNNN.wav` segments
  - `combined.wav`
  - `per-user/mic` and `per-user/system` (local)
  - `output/<id>.wav`: the hand-off copy (`record._submit_recording_transcription`, `record.py:963`). `_delete_temp_upload` skips non-upload jobs, so a failed or abandoned hand-off leaks; two were found on real data.
- **Segment `duration_s` is wall-clock time**, not media time (`recording_manager.py:441`). `concat_wav_segments` (`web/audio_writer.py:202`) joins frames exactly, skipping empty and unreadable files.
- **A recording's campaign and its transcript's campaign are stored separately.**
  - `recordings.campaign_id` is set when recording starts and never changes.
  - Moving a transcript writes only `campaign_transcripts` (`campaign_manager.py:314`).
  - Re-transcribing a recording passes `campaign=recording.campaign_slug` (`record.py:986`), which moves a since-moved transcript back to the end of the recording's original campaign.
- **Leaks:**
  - **Crash:** `JobQueue.submit()` renames an upload to `<original_stem><suffix>` in the tempdir, so `_cleanup_orphaned_uploads` (glob `wisper_upload_*`) never matches it after a crash.
  - **Cancel while pending:** `JobQueue.cancel()` marks a pending job FAILED without deleting its upload (`jobs.py:891–897`).
- **`Job.needs_extraction`** (`jobs.py:490`) is computed from `input_path`'s suffix, and the job page builds its step list from it (`job_detail.html:58`, `:171`).
- **No transcript → recording lookup exists.** `recording_manager` has `load_recording(id)` and `load_recordings()`. `Marker.elapsed_s` is computed in `_load` (~`:182`).
- **`bind_recording_speaker`** is at `recording_manager.py:492`. It's called by `_run_recording_enroll` (`jobs.py:1376–1380`) and by the bot at capture time (`discord_bot.py:444`).
- **`wisper record transcribe`** (`cli.py:1763`) POSTs to `/api/recordings/{id}/transcribe`, which reaches the same `_submit_recording_transcription`.
- **Name collisions:**
  - `GET /transcribe/name-check` (`transcribe.py:176`) returns `{"exists", "campaign"}`.
  - The page offers Overwrite or Cancel (`transcribe.html:234–259`).
  - `POST /transcribe` refuses a taken name without `overwrite=on`.
- **Web layer facts:**
  - The transcript sanitizer `_HtmlSanitizer` is a denylist, so `data-*` attributes survive.
  - The CSP allows `media-src 'self'`.
  - Page scripts belong in `static/app.js` (`app.py:103` comment).
  - Starlette `FileResponse` serves HTTP Range requests.
  - Confirm dialogs use `onsubmit="return confirm('…')"`, so text with an apostrophe must be passed through `|tojson`.
- **Guard tests:**
  - `test_only_transcript_store_deletes_transcripts` flags lines matching `(md_path|transcript_path|transcript|\.md\b)[^\n]*\.unlink\(` outside `transcript_store.py`.
  - `test_file_writes_are_atomic` flags direct `write_text()`.
  - Deleting other output-root files through `transcript_store` is a convention; the orchestrator checks it by eye.

### Decisions

| Decision | Why |
|---|---|
| A web upload is deleted right after its audio is extracted to a 16 kHz mono WAV. The WAV is deleted when the job ends. | The WAV is all the pipeline, excerpts, and embeddings read. A 6 GB video then lives minutes, not hours. A failed job needs a re-upload, which is accepted (Brandon, 2026-10-03). |
| Every file wisper owns is a row in the `files` registry (v9): owner, kind, path, size, and mtime. Renames, moves, deletes, and cleanup act on the registry. | More reliable than matching names on disk (Brandon, 2026-10-03: "track it all"). It's placed first so later phases build on it. |
| Every upload transcript keeps `<stem>.flac` (16 kHz mono, encoded from the job's WAV), recorded as the transcript's `audio` row in `files`. This holds with or without diarization, and it's deleted with the transcript. | Same treatment as a recording's `combined.wav`: playback, the wizard fallback, Re-match backfill, and Re-transcribe always have audio. FLAC is lossless against what the models hear, and ~3% of the size of the video. |
| Only the first audio track of an upload is transcribed and kept, and the README says so. | That's all the pipeline reads. Brandon keeps his originals. |
| One lookup, `transcript_store.audio_path(md_path)`, answers "where is this transcript's audio": its `audio` row, else the linked recording's `combined.wav`. A recording's `combined.wav` is owned by the recording, never by a transcript. | Phases 4–8 all need it, and it must work without `_diar.json`. `delete_transcript` deletes the transcript's own files, so `combined.wav` must never be one. |
| New helpers take the transcript's `.md` path or row id, not a bare name. | Campaign folders will allow the same name in two campaigns. |
| A database migration is rehearsed on a scratch copy of Brandon's data before it's committed. The branch keeps `db.SCHEMA_FROZEN = False` until the final review, so a branch build never migrates the live database. | This is a live install (Brandon, 2026-10-03). |
| Wizard enrollment uses stored embeddings and copies the excerpt as the profile clip; it reads audio only for a label without a stored embedding. With audio gone, the labels that have embeddings still enroll and the rest are skipped with a notice. | Same vector as re-extracting it (same code path), and seconds instead of minutes. Partial enrollment beats none (Brandon, 2026-10-03). |
| Frontmatter `source_file` names the original upload (`Session 12.mp4`), or the recording's name. | That's what Brandon recognises; the transcript was made from it (Brandon, 2026-10-03). |
| An upload never overwrites a file wisper doesn't own. A clash with an existing `<stem>.md` or `<stem>.flac` triggers the Overwrite/Cancel prompt, which shows the existing file's last-modified time. | The output root can be a user's own folder. Brandon wants to choose, with the date to choose by (2026-10-03). |
| A transcript's files always follow its name: wisper's own renames, relink, and reconcile's automatic match all carry the summary, sidecar, excerpts, and audio. | They're tied to the transcript only by name. |
| Reconcile treats a missing transcript plus a new `.md` with the same size and mtime as a rename. Anything it can't match, and any companion file with no transcript, is listed for Brandon on the Transcripts page and by `wisper storage trim`. Nothing is deleted automatically. | Match as well as possible; ask about the rest (Brandon, 2026-10-03). |
| A transcribed recording's campaign is derived from its transcript; `recordings.campaign_id` is only the campaign chosen when recording started, used until a transcript exists. | One fact stored once: no second write to keep in sync. Re-transcribe, the Recordings filter, and speaker matching then use the transcript's current campaign (Brandon, 2026-10-03). |
| Deleting a transcript removes every record of it: the row, its files, speakers, search entries, and campaign place. The campaign's journal is flagged for rebuild. A recording whose transcript is deleted returns to "Awaiting transcription"; deleting the recording deletes its files and its transcript. | Brandon, 2026-10-03. The recording's `combined.wav` is the only copy of its audio, so only an explicit recording delete removes it. |
| Every transcript with audio can be re-transcribed from its transcript page. Session settings (speaker counts, language, post-processing) come from its last job; engine settings (model, device, VAD, alignment) come from the current config. | The kept audio makes a rerun possible without the original file. Re-transcribing is how a better model reaches an old session. |
| A recording's `combined.wav` stays until the recording is deleted. Its `combined/` segments and local `per-user/` tracks go once `combined.wav` is verified complete. | A recording has no other source (option A, Brandon, 2026-10-03). Recover runs only when `combined.wav` is missing. |
| A Discord recording's `per-user/<uid>/` stays until that uid is bound to a profile. | Unbound-speaker enrollment (`enroll_speaker_from_audio_dir`) reads it. |
| Speaker sample clips stay: `profiles/embeddings/<key>.mp3` (~95 KB per profile) and `<stem>_excerpt_<label>.mp3/.txt` (~45 KB per speaker per transcript). | They're what you listen to on the Speakers page and in the wizard, and they're tiny. |
| `_diar.json` stays. | Small. Audio-fallback enrollment and Re-match backfill read its segments. |
| Existing data is converted by `wisper storage trim`, a CLI command only (no web UI; not part of a migration). | One-off: new data is already compact after Phases 4–5. Migrations are frozen and must not run ffmpeg or ML. A web job type would need a migration to rebuild `jobs` (its `type` CHECK). |
| `wisper storage trim` deletes only files it can tie to a transcript or recording, never an arbitrary audio file in the output root. | The output root can be a user's own folder (`output_dir`). |
| `transcripts.audio_rel_path` is dropped by a DDL-only migration (v10) right after v9 copies it into `files`. | A column left behind as NULL is a second source of truth that stray code keeps reading (Brandon, 2026-10-03). Campaign folders becomes v11. |
| Refine's `<stem>.md.bak` backups (`backup` kind) and a local session's `live_transcript.md` (`live_draft` kind) are tracked too. Backups follow renames and are deleted with their transcript. | "Track it all" (Brandon, 2026-10-03). |
| Deleting a campaign asks: **Delete everything** (its transcripts, their files, and its journal), or **Keep the files**. Keep the files removes the campaign, leaves its transcripts unassigned, and lists its journal under Needs attention as unclaimed. | Brandon, 2026-10-03. |
| `wisper storage trim --apply` refuses while the server is running at all, not only while jobs run. | A user could start an upload mid-trim (Brandon, 2026-10-03). |
| Old upload audio that isn't 16 kHz mono (including a `.flac` kept whole) is converted to 16 kHz mono FLAC. | The models only ever hear 16 kHz mono, so a larger copy doesn't improve re-transcription (Brandon, 2026-10-03). |
| An upload whose name matches a **missing** transcript gets the Overwrite/Cancel prompt, worded as "A transcript with this name is missing (renamed or deleted outside wisper)…". | Overwriting would otherwise replace that session's only audio and speakers silently (Brandon, 2026-10-03). |
| Campaign **Delete everything**, when a transcript can't be deleted (a locked file): delete the rest, keep going, and list what's left under Needs attention. | Brandon, 2026-10-03. |
| Unregistered `.flac` (and other pattern) files in the output root are listed under Needs attention **with** a Delete button. `sync` never registers an unregistered `.flac` as a transcript's audio. | It may be the user's own file; Brandon decides (2026-10-03). |
| Docker: `storage trim --apply` runs in a one-off container with the web service stopped (GPU: `docker compose stop wisper-web && docker compose run --rm wisper storage trim --apply`; CPU: `docker compose stop wisper-cpu-web && docker compose run --rm wisper-cpu storage trim --apply`). | `--apply` refuses while a server runs (Brandon, 2026-10-03). |
| Re-transcribing a transcript (upload or recording) writes to its **current** name and replaces it in place. Its row, id, campaign place, and recording link are kept, and nothing is written under an old or `<recording-id>` name. | A rename must never produce a duplicate or orphan (Brandon, 2026-10-03). |
| Registration is central and best-effort. `save_transcript`, `save_summary`, `write_sidecar`, and `transcript_store.register` register their files; a write outside wisper's roots (a CLI `--output`) is simply not tracked, never an error. | One place per kind instead of ~12 call sites, and CLI use outside the roots keeps working. |

---

### Phase 1 — File registry: every file wisper owns is a database row

**Goal:** one table records every file wisper writes. Each row holds the file's owner, kind, path, last-seen size, and modified time.
- Renames, moves, deletes, playback, and cleanup act on what the registry lists.
- "Needs attention" is the difference between the registry and the disk.
- It replaces `transcripts.audio_rel_path`, which v10 drops.

Every later phase builds on it (Brandon, 2026-10-03: "track it all").

**Read first:**
- **`db.py`:**
  - `MIGRATIONS` and `_V1_DDL`–`_V8_DDL` (233–602);
  - `migrate` (627–706, especially `_open` at 650, `BEGIN IMMEDIATE` at 659, `defer_foreign_keys` at 666, `foreign_key_check` at 682);
  - `_open` (761), `expected_schema` (989), `SCHEMA_FROZEN` (49), the `DevDataDirRefused` check (638–648), `to_rel`/`from_rel` (~110–130), `transaction` (816);
  - the module docstring (19–21).
- **Tests:** `tests/test_schema.py` (the `conn` fixture at 22–55, `VIOLATIONS` from ~60, cascade tests ~176–235, `test_every_fk_child_column_is_indexed` at 237), `tests/test_db.py` (`test_every_table_is_strict` at 247, `SCHEMA_FROZEN` tests at 300–331, `_fake_migration`, `_seed_lease` at 338), `tests/conftest.py` (35–41), `tests/_seed.py`.
- **Every place that writes or deletes a file wisper owns:**
  - **`transcript_store.py`:** `register` (212; it unlinks a stale sidecar at 250–252), `save_transcript`, `save_summary`, `write_sidecar` (620–680), `read_sidecar` (559–617; note 600 and 613–616), `_companion_paths` (273), `delete_transcript` (315), `relink` (486), `reconcile` (405), `_replace`, `_is_case_insensitive` (~367);
  - **`web/jobs.py`:** `_extract_speaker_excerpts` (271–367; the label is sanitised at 341), and the `.md.bak` write (~1567);
  - **`cli.py`:** the `.md.bak` writes (~1508, ~1601);
  - **`speaker_manager.py`:** `_save_reference_clip` (452), `remove_profile` (130), `rename_profile` (142; the clip move at 179–185), `reset_profiles` (189);
  - **`journal.py`:** `sync_journal` (~195–199, including the `write_bytes` fallback), `update_journal` (~395, ~431–435), `reset_journal` (~237–238);
  - **recordings:**
    - `web/local_capture.py`: per-user writers created at start (356–357), finalise (~540–570);
    - `web/discord_bot.py`: per-user writers (424–431), async `_finalise` (~495–550);
    - `recording_manager.py`: `recover_recording` (519), `delete_recording` (565);
    - `web/routes/record.py`: `_purge_recording_files` (526), and `live_transcript.md` (~196);
  - **`campaign_manager.py`:** `delete_campaign` (~197–204).
- `tests/test_transcript_store.py`: `test_only_transcript_store_deletes_transcripts` (~307–311), `_src_lines()` (~280)
- `architecture.md` "Database" section: 3NF rules at ~356–360, the ordering rule at ~365 and ~453, Migrations at ~340.

**Design: v9 creates `files` (additive; nothing rebuilt), v10 drops `transcripts.audio_rel_path` (DDL only).**

The DDL goes in a **non-raw** `"""` string like `_V2_DDL`, so `'*\\*'` reaches SQLite as `*\*`.
```sql
CREATE TABLE files (                           -- every file wisper owns; exactly one owner; root follows kind
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('transcript', 'summary', 'sidecar', 'excerpt', 'excerpt_text', 'audio',
                                              'backup', 'combined', 'per_user', 'live_draft', 'reference_clip', 'journal')),
  root          TEXT NOT NULL CHECK (root IN ('output', 'data')),
  rel_path      TEXT NOT NULL CHECK (rel_path <> '' AND rel_path NOT GLOB '/*' AND rel_path NOT GLOB '*\\*'
                                     AND rel_path NOT GLOB '[A-Za-z]:*' AND rel_path NOT GLOB '*/'
                                     AND rel_path NOT GLOB '*//*'
                                     AND '/' || rel_path || '/' NOT GLOB '*/../*'
                                     AND '/' || rel_path || '/' NOT GLOB '*/./*'),   -- on-disk spelling, POSIX separators
  label         TEXT CHECK (label IS NULL OR (label <> '' AND label NOT GLOB '*[/\\:]*')),
  transcript_id INTEGER REFERENCES transcripts(id) ON DELETE CASCADE,
  recording_id  TEXT    REFERENCES recordings(id)  ON DELETE CASCADE,
  profile_id    INTEGER REFERENCES profiles(id)    ON DELETE CASCADE,
  campaign_id   INTEGER REFERENCES campaigns(id)   ON DELETE CASCADE,
  size          INTEGER CHECK (size IS NULL OR size >= 0),   -- last observed; NULL for directories and until first stat
  mtime_ns      INTEGER,                                     -- last observed
  UNIQUE (root, rel_path),
  CHECK ((transcript_id IS NOT NULL) + (recording_id IS NOT NULL)
         + (profile_id IS NOT NULL) + (campaign_id IS NOT NULL) = 1),
  CHECK ((kind IN ('transcript', 'summary', 'sidecar', 'excerpt', 'excerpt_text', 'audio', 'backup'))
         = (transcript_id IS NOT NULL)),
  CHECK ((kind IN ('combined', 'per_user', 'live_draft')) = (recording_id IS NOT NULL)),
  CHECK ((kind = 'reference_clip') = (profile_id IS NOT NULL)),
  CHECK ((kind = 'journal') = (campaign_id IS NOT NULL)),
  CHECK ((root = 'output') = (transcript_id IS NOT NULL)),
  CHECK ((kind IN ('excerpt', 'excerpt_text', 'per_user')) = (label IS NOT NULL)),
  CHECK (kind <> 'per_user' OR label IN ('mic', 'system') OR label NOT GLOB '*[^0-9]*'),
  CHECK ((size IS NULL) = (mtime_ns IS NULL)),
  CHECK (kind <> 'per_user' OR size IS NULL),
  CHECK (kind <> 'transcript'   OR (lower(rel_path) GLOB '*.md' AND lower(rel_path) NOT GLOB '*.summary.md')),
  CHECK (kind <> 'summary'      OR lower(rel_path) GLOB '*.summary.md'),
  CHECK (kind <> 'sidecar'      OR rel_path GLOB '*_diar.json'),
  CHECK (kind <> 'excerpt'      OR rel_path GLOB '*_excerpt_*.mp3'),
  CHECK (kind <> 'excerpt_text' OR rel_path GLOB '*_excerpt_*.txt'),
  CHECK (kind <> 'audio'        OR lower(rel_path) NOT GLOB '*.md'),
  CHECK (kind <> 'backup'       OR lower(rel_path) GLOB '*.md.bak'),
  CHECK (kind <> 'combined'     OR rel_path = 'recordings/' || recording_id || '/combined.wav'),
  CHECK (kind <> 'per_user'     OR rel_path = 'recordings/' || recording_id || '/per-user/' || label),
  CHECK (kind <> 'live_draft'   OR rel_path = 'recordings/' || recording_id || '/live_transcript.md'),
  CHECK (kind <> 'reference_clip' OR rel_path GLOB 'profiles/embeddings/*.mp3'),
  CHECK (kind <> 'journal'      OR rel_path GLOB 'campaigns/*/journal.md')
) STRICT;
-- One file per (owner, kind[, label]); each also serves as the owner FK's index.
CREATE UNIQUE INDEX files_transcript ON files(transcript_id, kind, coalesce(label, ''));
CREATE UNIQUE INDEX files_recording  ON files(recording_id, kind, coalesce(label, ''));
CREATE UNIQUE INDEX files_profile    ON files(profile_id, kind);
CREATE UNIQUE INDEX files_campaign   ON files(campaign_id, kind);
-- A clip's path is its profile key: the row moves with the key, the file follows after commit.
CREATE TRIGGER files_profile_key_au AFTER UPDATE OF key ON profiles BEGIN
  UPDATE files SET rel_path = 'profiles/embeddings/' || new.key || '.mp3'
   WHERE profile_id = new.id AND kind = 'reference_clip';
END;
```
- **Root follows kind.**
  - `output`: `transcript`, `summary`, `sidecar`, `excerpt`, `excerpt_text`, `audio`, `backup`.
  - `data`: `combined`, `per_user`, `live_draft`, `reference_clip`, `journal`.

  The default output root (`<data>/output`) is inside the data dir, so the path alone can't decide.
- **One row per file**, except `per_user`: one row per track *directory* (`recordings/<id>/per-user/<track>`), with `size` NULL. Combined segments aren't rows: `recording_segments` records them, and they're deleted once `combined.wav` is verified (Phase 5).
- **Paths are stored, not derived.** Campaign folders will move files, so this is a deliberate exception to "derived values aren't stored". The derived ones (`combined`, `per_user`, `live_draft`, `reference_clip`) are pinned by CHECKs or the trigger.
- **The `transcript` row is the authoritative location of the `.md`.** Existence stays with `transcripts.missing_since`.
- **`size`/`mtime_ns` mean "last observed".** They keep their values when the file goes missing; Phase 2's rename match relies on that.
- **Unicode and case:** `rel_path` stores the on-disk spelling. Lookups compare NFC, and also casefold where `transcript_store._is_case_insensitive(dir)` is true. The schema has no `COLLATE NOCASE`: it folds ASCII only, and it's wrong on case-sensitive Linux/Docker mounts.

**Steps:**
1. **Migrations.**
   - `Migration(9, "file-registry", _V9_DDL, _v9_import)` and `Migration(10, "drop-audio-rel-path", "ALTER TABLE transcripts DROP COLUMN audio_rel_path;")`. An import step runs after its own DDL, so v9 can't drop the column it copies from.
   - **`_v9_import`** is self-contained SQL and Python in `db.py`, and never imports `file_registry` (migrations are frozen). It reads `SELECT id, audio_rel_path FROM transcripts WHERE audio_rel_path IS NOT NULL ORDER BY id` and inserts one `audio` row per value (`root='output'`, `size`/`mtime_ns` NULL; `sync` fills them).
     - It skips, with `ctx.note`, any value that is `''`, contains `/`, or ends in `.md` (case-insensitive). Web audio is always a top-level `<stem><suffix>`; a subfolder file is the user's own.
     - It uses `INSERT … ON CONFLICT DO NOTHING`, with a `ctx.note` when nothing was inserted (two transcripts pointing at one file: the lower id keeps it).
     - Each insert is wrapped in `try/except sqlite3.IntegrityError` with a `ctx.note`. `ON CONFLICT` doesn't cover CHECK failures, and a name like `C:x.mp4` (legal on macOS and Linux) fails the drive-prefix CHECK; one failure must not abort the migration. A statement-level abort leaves the transaction usable.
   - **In `migrate()`:** run `conn.execute("PRAGMA foreign_keys=OFF")` right after `_open` (650) and before `BEGIN IMMEDIATE` (659); the pragma is a no-op inside a transaction. Keep the final `foreign_key_check`: it's now the only FK enforcement during migrations, which lets later rebuilds (`DROP TABLE` would otherwise cascade-delete children) stay safe. Remove the now-redundant `PRAGMA defer_foreign_keys=ON` (666). Update the module docstring (19–21), `legacy_import.py:4` ("with foreign keys deferred"), and architecture.md's Migrations bullet. Rename `test_deferred_foreign_keys_allow_any_import_order` (`test_db.py:178`) to say foreign keys are off.
   - **Branch safety:** set `db.SCHEMA_FROZEN = False` (Phase 9 sets it back). Add `db.REQUIRE_OUTPUT_ENV = not SCHEMA_FROZEN`. While it's true, `db.connect()` on its `migrate_schema=True` path (before the version probe, `db.py:~784`, cached per data dir) raises `DevDataDirRefused` when `WISPER_OUTPUT_DIR` is unset **and** that data dir's output root is outside it. **No cache on the decision:** read the environment variable on every call (it's cheap). Cache only the config-derived fact "is the output root inside this data dir", keyed by `(realpath(data_dir), st_mtime_ns of <data_dir>/config.toml)`. Resolve it from **`connect()`'s `data_dir` argument**, not `config.get_output_root()`, which reads the environment's data dir. Add a pure helper `config.resolve_output_root(cfg: dict, data_dir: Path, env: Optional[str]) -> Path` holding `get_output_root`'s rules unchanged: the env var `expanduser()`ed as is (never joined to `data_dir`); else the `output_dir` setting `expanduser()`ed, joined to `data_dir` when relative; else `<data_dir>/output`. `get_output_root(config=None)` calls it with `get_data_dir()`, passing its `config` argument (or `load_config()`) as `cfg`, and `{}` when the env var is set, so that path still never reads `config.toml`. The guard reads `<data_dir>/config.toml` with `tomllib` directly (`load_config()` follows the environment's data dir), using `{}` and a `None` cache mtime when the file doesn't exist; a parse error raises `DevDataDirRefused` (fail closed). It calls `resolve_output_root(cfg, data_dir, None)` and compares `os.path.realpath`s (macOS `/var` vs `/private/var`). One helper means the guard and the app can't disagree; `output_dir = "~/Transcripts"` in a copied `config.toml` is exactly the hazard. Test it with `output_dir = "~/elsewhere"` (monkeypatch `HOME` and `USERPROFILE`). A Windows `output_dir = "C:\..."` read on the Mac is relative there and lands inside the copy, which is harmless.
     - The message names both variables: "This is an unmerged development build; set WISPER_DATA_DIR and WISPER_OUTPUT_DIR to copies of your data."
     - It lives in `connect()`, not `migrate()`, so it also fires on a copy that has already migrated. `db status|backup|dump` use `migrate_schema=False` and are unaffected; Docker sets `WISPER_OUTPUT_DIR` (`docker-compose.yml:25`).
   - **`tests/conftest.py`:** leave its `WISPER_OUTPUT_DIR` deletion (`:41`) alone. Add an autouse fixture that monkeypatches `db.REQUIRE_OUTPUT_ENV = False`; the guard's own tests set it to `True`. Also reset `file_registry`'s cached report and throttle (step 3) between tests.
2. **New module `src/wisper_transcribe/file_registry.py`.** It's the only code that writes `files`, apart from the v9 import.
   - **Constants:** `KINDS`, `ROOT_OF_KIND: dict[str, str]`, `SINGLE_KINDS` (every kind except `excerpt`, `excerpt_text`, `per_user`, which are unique per label).
   - **`Owner(kind: Literal["transcript", "recording", "profile", "campaign"], id: int | str)`** (frozen dataclass). `id` is `transcripts.id`, `profiles.id`, `campaigns.id` (int), or `recordings.id` (str).
   - **Owner lookups** (each takes `conn=None` and returns `Optional[Owner]`): `Owner.for_stem(stem)` (NFC; casefolded where case-insensitive), `Owner.for_profile_key(key)`, `Owner.for_campaign_slug(slug)`, `Owner.for_recording(rec_id)`. Write sites use these; they only know a stem, key, or slug.
   - Import `transcript_store` lazily inside functions: `reconcile` calls `sync`, so module-level imports would be circular.
   - **`FileRow(id, kind, root, rel_path, label, owner: Owner, size, mtime_ns, path: Path)`.** `path` is resolved from root plus `rel_path`, using the current output root and data dir.
   - Every function takes `conn=None, data_dir=None, output_dir=None`. With `conn`, it runs in the caller's transaction; without one, it opens its own.
   - **`output_dir` is the root for output-root kinds,** in `add()` and when resolving a row's path (`FileRow.path`). Its default is the configured output root. Every `transcript_store` caller passes `output_dir=Path(md_path).parent`, which keeps today's semantics: `write_sidecar` and `read_sidecar` work relative to the `.md`'s folder (`transcript_store.py:632`, `646`, `614`). Many tests keep `.md` files and audio in a bare `tmp_path` and rely on that, e.g. `tests/test_transcript_enroll.py:86–126` and the `tests/test_speaker_registry.py` `world` fixture.
   - **`add(path, *, kind, owner, label=None, conn=None, …) -> Optional[Path]`.** Registers a file and returns the path it replaced, if any. Only `set_audio` ever unlinks a returned path: for `transcript`, the "replaced" path can be the same file spelled differently (case or NFD).
     - `root` comes from `ROOT_OF_KIND[kind]`; `rel_path = db.to_rel(path, <that root dir>)` (realpath-based). A path outside that root raises `ValueError`.
     - It refuses `audio` for any path under `<data>/recordings/`.
     - **Order** (an `ON CONFLICT(root, rel_path)` upsert can't express it: the owner's existing row conflicts on `files_transcript` instead):
       1. Look up `(root, rel_path)`, comparing NFC (and casefolded where the filesystem is case-insensitive). A row with a *different* owner, kind, or label raises `OwnershipConflict`; the same row is just re-statted.
       2. Otherwise look up `(owner, kind, coalesce(label, ''))`. If found, `UPDATE` its `rel_path` and stats, and return the old path; map an `IntegrityError` from `UNIQUE(root, rel_path)` there to `OwnershipConflict`.
       3. Otherwise `INSERT`.
     - This applies to every kind, including the per-label ones (`excerpt`, `excerpt_text`, `per_user`).
     - Stats the file (`size`/`mtime_ns`; both NULL for a directory, and both NULL without raising when the file doesn't exist, as today's `audio_rel_path` tolerated: tests seed audio paths that don't exist, e.g. `tests/test_transcript_enroll.py:597`).
   - **`add_if_owned(path, *, kind, owner: Optional[Owner], label=None, conn=None, output_dir=None) -> Optional[Path]`:** the best-effort form used at write sites. It returns `None` without raising when `owner` is `None`, the path is outside the kind's root (a CLI `--output` file), or the add fails (`IntegrityError`, e.g. a stem ending `.summary` fails the `transcript` CHECK; `OwnershipConflict`; or any `OSError`). It logs at DEBUG, or WARNING for a failed add. Catching a statement-level `IntegrityError` inside the caller's transaction is safe.
   - **`forget(path, conn=None)`**, **`forget_kind(owner, kind, label=None, conn=None)`**. Both return the absolute paths of the rows they removed (`list[Path]`), so a caller like `set_audio` can unlink the replaced file after commit.
   - **`files_for(owner, conn=None) -> list[FileRow]`**, **`file_for(owner, kind, label=None, conn=None) -> Optional[FileRow]`**.
   - **`paths_for_delete(owner, conn) -> list[Path]`:** called *inside* the caller's delete transaction, before the owner row is deleted (the cascade removes the rows).
   - **`unlink_paths(paths) -> list[Path]`:** called after commit. Files are unlinked; directories (`per_user`) are removed with `shutil.rmtree`. It catches `OSError` per path and returns the failures; `sync` later lists them as unclaimed.
   - **`move(row, new_path) -> MoveResult`** (`"moved" | "conflict" | "missing" | "error"`). No `conn`: it moves a file, so callers call it after their own transaction commits.
     - If the source is gone: forget the row and return `"missing"`.
     - If the target exists and isn't the same file (`os.path.samefile`, or casefold-equal names on a case-insensitive filesystem): return `"conflict"`.
     - Otherwise move the file first with `transcript_store._replace` (Windows retry). On failure, including `_replace` returning `False` (a persistent sharing violation), return `"error"` with the row unchanged.
     - Then update `rel_path` and re-stat in one short transaction. If that fails, move the file back (best effort) and raise.
   - **`repoint(row, new_path, conn=None)`:** updates `rel_path` and re-stats, for a file that has already moved (the user renamed it).
   - **`refresh(row, conn=None)`:** re-stat.
   - **`sync(output_dir=None, data_dir=None) -> SyncReport(registered, refreshed, missing, unclaimed, errors)`.**
     - **Scan outside any transaction:**
       - the output root's top level (the configured output root): `<stem>.md`, `<stem>.summary.md`, `<stem>_diar.json`, `<stem>_excerpt_<label>.mp3|.txt`, `<stem>.md.bak`, and `<stem>.flac`. **`sync` never registers an `audio` row:** an unregistered `<stem>.flac` may be the user's own file, so it's only ever listed as unclaimed. Only `set_audio` creates `audio` rows; v9 imports today's web audio.
       - in the data dir: `recordings/<id>/combined.wav`, `recordings/<id>/per-user/<track>/`, `recordings/<id>/live_transcript.md`, `profiles/embeddings/<key>.mp3`, `campaigns/<slug>/journal.md`;
       - when the output root is inside the data dir, the data-dir scan skips it.
     - **Owners** come from `transcripts.stem` (NFC; casefolded where case-insensitive), `recordings.id`, `profiles.key`, and `campaigns.slug`.
     - **Skip recordings** whose `capture_status` is `recording` or `degraded`.
     - **Changes:** a pattern file is registered only when its owner exists **and has no row of that kind and label**. `sync` never re-points an existing row; a second file for an owned kind is reported as unclaimed. A row whose stat changed is refreshed. Changes are applied in short transactions of at most ~50 rows, each with compare-and-set on `(id, rel_path, size, mtime_ns)`, re-statting each new path just before inserting it. **No transaction at all when nothing changed.**
     - **`missing`:** rows whose file is gone (stats kept). A missing `.md` is reported through `transcripts.missing_since`, not here. **`unclaimed`:** pattern files with no owner. Neither is ever deleted by `sync`.
     - Errors are isolated per file (`OSError`, `IntegrityError`, `OwnershipConflict`) and collected in `errors`. Table-level errors around each write transaction (`sqlite3.OperationalError`/`DatabaseError`, e.g. a busy database) are caught too. `sync` never raises.
     - It also reports any `transcript` row whose `rel_path` doesn't equal `stem + ".md"` (NFC; casefolded where case-insensitive) under `errors`, since the two are kept in step by hand.
3. **`reconcile(..., sync: Literal["always", "throttled", "never"] = "always")`** runs `sync()` after its own transaction has closed.
   - Startup and the CLI use `"always"`. Page loads (`transcripts.py:244`, `campaigns.py:79`) pass `"throttled"`: at most once every 30 s per `(data_dir, output_dir)`, with `busy_timeout_ms=500` so a page view never waits long.
   - Keep the last `SyncReport` per realpath `(data_dir, output_dir)` (`file_registry.last_report(data_dir, output_dir)`) for Phase 2's Needs attention. Forget and Delete actions (Phase 2) drop that entry from the cached report.
4. **Register centrally, and best-effort** (`add_if_owned`):
   - **`transcript_store.register()`:**
     - inside its transaction (`conn=`), `add_if_owned(md, kind="transcript")`;
     - where it unlinks the stale sidecar (250–252), `forget_kind(…, "sidecar")` too.
   - **`relink()`:** after the row rename, `repoint` the `transcript` row to the new `.md` in the same transaction. If the transcript has no `transcript` row yet (it went missing before the first `sync`), `add_if_owned` it instead. The same fallback applies to the case-only rename below and to Phase 2's auto-match. Its new-name row is deleted first, as today, so its `files` rows cascade away. Companion files and rows move in Phase 2.
   - **`reconcile()`'s case-only rename (437–443):** `repoint` the `transcript` row too.
   - **`save_transcript`:** `refresh` the `transcript` row.
   - **`save_summary`:** `add_if_owned(kind="summary")`, only when the file is `<stem>.summary.md` beside a registered transcript.
   - **`write_sidecar`:** `add_if_owned(kind="sidecar")` **after** its `atomic_write_text` (~670), not inside its transaction. Its audio handling (643–680) moves to `set_audio` (step 5). **Rule for every write site: register after the file exists;** `add()` also tolerates a missing file (NULL stats).
   - **`_extract_speaker_excerpts`:** after its loop, one transaction registering every `.mp3`/`.txt` that exists (`is_file()`; ffmpeg failures are swallowed), with `label` = the sanitised name used in the filename (`safe_name`, 341) and `owner=Owner.for_stem(stem, conn=conn)`. Two labels that sanitise to the same name wrote the same file, so the second registration updates the row in place.
   - **`.md.bak` writes** (`cli.py` ~1508 and ~1601, `jobs.py` ~1567): `add_if_owned(kind="backup")`.
   - **Reference clip:** register in `enroll_speaker`, right after its `_save_reference_clip` call (`speaker_manager.py:375–377`; `_save_reference_clip` has no profile key), with `Owner.for_profile_key(name)`, only if the file exists afterwards (ffmpeg failures are swallowed). Phase 3 extends the same spot.
   - **`rename_profile`:** the trigger re-points the row in its transaction; the clip move (179–185) stays after commit. `rename_profile` already validates the new key (`validate_path_component`); a key that would still fail the `files.rel_path` CHECK aborts the rename. Accept that, and test it. If that move fails (`OSError`, swallowed today), `repoint` the row back at the old-key file, so the registry matches the disk.
   - **Journal** (all inside the transaction that's already open there, with `conn=conn`; a second connection would deadlock):
     - `sync_journal` (`journal.py:188–198`): `add_if_owned(kind="journal", owner=Owner.for_campaign_slug(slug, conn=conn), conn=conn)` after the `_replace` (or `write_bytes` fallback). In its "journal.md was deleted, fresh start" branch (`_reset_rows`, ~204–211), `forget_kind(…, "journal", conn=conn)`;
     - `update_journal`: register **after** the `_replace(pending_file, jpath)` that creates `journal.md` (~431), with its own short transaction. On a first fold the file doesn't exist inside the commit transaction (~399), and registering there would abort the fold (`tests/test_journal.py`);
     - `reset_journal`: `forget_kind` inside its transaction (~232), then unlink after commit.
   - **Local finalise:** `add_if_owned` for `combined.wav` (when written) and each `per-user/<track>/` directory. `live_transcript.md` is written by the live job (`jobs.py:~1481–1499`), so register it when that job ends; `sync` covers a crash.
   - **Discord `_finalise`:** the same, via `await asyncio.to_thread(...)`.
   - **`recover_recording`:** `combined.wav`.
5. **Audio through the registry** (Phase 4 builds on these). Both are defined here, because v10 drops the column:
   - **`transcript_store.audio_path(md_path, conn=None, …) -> Optional[Path]`:**
     - the transcript's `audio` row's file, if it exists;
     - else, if a recording has `transcript_id` = this transcript, `recording_manager.combined_path_for(rec_id)` if it exists. That's the fixed layout, not a registry row, so seeded recordings work;
     - else `None`.
   - **`transcript_store.set_audio(md_path, path: Optional[Path], *, data_dir=None, output_dir=None)`** (no `conn`: it unlinks after its own commit, so callers call it after theirs):
     - `add(path, kind="audio", owner=transcript)`, or `forget_kind(…, "audio")` for `None`;
     - after the commit, unlink the replaced file if it differs from the new one;
     - it never accepts a path under `<data>/recordings/`.
   - **`write_sidecar`** calls `set_audio(md_path, <input_path if inside the .md's folder and not under <data>/recordings/, else None>, output_dir=md_path.parent)` after its transaction. That's today's behaviour (`db.to_rel(input_path, md_path.parent)`): an absent or outside path clears the audio and deletes the replaced file. `set_audio` raises `ValueError` for a path under `<data>/recordings/`; `write_sidecar` never passes one.
   - **`read_sidecar`:**
     - the early return at 600 tests "no speaker rows and no `audio` row";
     - `input_path` (613–616) comes from `audio_path(md_path, conn=conn)` (its open connection; a nested `connect()` can refresh the lease), computed right after the row lookup, before both early returns. It overrides the legacy fallback's own `input_path` (597, 600–601) only when `audio_path()` returns a path.
   - **`_companion_paths(md_path, output_dir, conn=None)`** returns the union of:
     - the transcript's registry rows (except the `transcript` row);
     - the pattern-derived paths it builds today (summary, sidecar, `glob.escape`d excerpts);
     - the old-sidecar `input_path` fallback (inside the output root, and never under `<data>/recordings/`).

     Unregistered files and existing tests keep working.
   - **`delete_transcript`**, in order: compute the pattern paths (the glob) and read the registry rows with one read connection, *before* any write lock; unlink the `.md` (today's existence marker); delete the row in a short transaction (its `files` rows cascade); unlink the companions after commit. A file registered between the read and the delete is picked up by `sync` as unclaimed.
   - **Other owner deletes** take the same read-first shape: `paths_for_delete` inside the transaction, delete the owner row, `unlink_paths` after commit.
     - `remove_profile`, `reset_profiles`;
     - `delete_recording` via `_purge_recording_files` (`record.py:526–550`): keep its current order. The `rmtree(rec_dir)` (segments and anything untracked) and `delete_transcript` run before `delete_recording`'s transaction, and the cascade then removes the recording's rows;
     - `delete_campaign`: today it keeps the journal file. Its row cascades and `sync` lists the file as unclaimed; Phase 2 adds the delete-everything / keep-the-files choice.
6. **Guard tests** (copy `_src_lines()` from `test_transcript_store.py`):
   - no module other than `file_registry.py` and `db.py` contains `INSERT INTO files`, `UPDATE files`, or `DELETE FROM files`;
   - each write site in step 4 calls `add_if_owned`/`add` (a list of expected file:function pairs);
   - add `file_registry.py` to the allowlist of `test_only_transcript_store_deletes_transcripts`;
   - `audio_rel_path` appears in `src/` only in `db.py` and `legacy_import.py`.

**Migration rehearsal (orchestrator, before committing):**
1. Copy a reference data dir and output folder to scratch: Brandon's Windows data copied to the Mac, or the Mac's own data. The 6–7 GB `.mp4`s can be skipped here; Phase 6 needs them.
2. With `WISPER_DATA_DIR=<scratch>/data`, `WISPER_OUTPUT_DIR=<scratch>/output`, `TEMP`/`TMP`/`TMPDIR=<scratch>/tmp`, and only while Brandon's queue is idle:
   - `wisper db status` reports v8;
   - `wisper server --port 8090` migrates the copy to v10 and runs `sync`;
   - stop it.
3. Check:
   - `wisper db status`: version 10, integrity ok, no schema drift;
   - `files` row counts by kind match the files on disk;
   - every former `audio_rel_path` value has an `audio` row, or a note in the import report;
   - `transcripts` has no `audio_rel_path` column;
   - with `WISPER_OUTPUT_DIR` unset (and the copied config's `output_dir` outside the copy), the branch build refuses to start, even though the copy is already at v10;
   - the Transcripts and Speakers pages render.
4. **Optional, with Docker:** a second scratch copy, mounted (`data`, `output`, `recordings`) through a compose override into `wisper-cpu-web`. It migrates and syncs with Linux paths.
5. Delete the scratch copies.

**Tests:**
- **`tests/test_schema.py`.** Add `files` rows of every kind to the `conn` fixture, so `test_baseline_satisfies_every_rule` covers them.
  - **`VIOLATIONS`, one entry each:**
    - zero owners, and two owners;
    - kind/owner mismatch in both directions;
    - root/owner mismatch;
    - a missing excerpt label, and a label on a summary;
    - an empty label, and a label with a separator;
    - an invalid `per_user` label;
    - duplicate `(root, rel_path)`;
    - a second `audio`, `summary`, or `sidecar` for one transcript;
    - a duplicate excerpt label;
    - a second `combined`, clip, or journal;
    - `rel_path` that is absolute, `..`, has a backslash, has a drive prefix, or ends in `/`;
    - a mismatched `combined`, `per_user`, or `live_draft` path;
    - an `audio` path ending `.md`, and a `backup` not ending `.md.bak`;
    - `size` without `mtime_ns`.
  - **Positive:**
    - deleting a transcript, recording, profile, or campaign cascades its rows (add `files` to `test_recording_delete_cascades_to_every_child`, ~225);
    - renaming a profile key rewrites its clip row.
  - **Delete:** the three `audio_rel_path` violations (90–92), since v10 drops the column.
- **`tests/test_db.py`:**
  - **Upgrade v8 → v10.** Monkeypatch `db.MIGRATIONS` to the first 8 and `LATEST_VERSION=8`, `migrate()`, seed transcripts with `audio_rel_path` values (normal, `''`, a subfolder, `.md`, a duplicate) using raw SQL in `db.transaction()`, restore, and `migrate()`. Then:
    - normal values become `audio` rows, and the skips appear in the import report;
    - `foreign_key_check` is empty;
    - `_normalized_schema` equals `expected_schema(10)`, and `db.status().schema_drift is False`.
  - **Fresh legacy install, v0 → 10:** a legacy sidecar's `input_path` ends up as an `audio` row.
  - **FKs off:**
    - inside a test import step (`_fake_migration`), `PRAGMA foreign_keys` reads 0;
    - a test migration that drops and recreates a parent table keeps its child rows;
    - a migration inserting an orphan child still raises `MigrationFailed`;
    - `db.connect()` afterwards reads 1.
  - **Branch guard** (`REQUIRE_OUTPUT_ENV=True`; set it explicitly, since it's fixed at import time):
    - with a non-default data dir, no `WISPER_OUTPUT_DIR`, and config `output_dir` pointing outside the data dir, `db.connect()` raises `DevDataDirRefused` naming both variables;
    - **already migrated:** `db.migrate()` explicitly with the variable set, then `connect()` once with it set (it passes), then `delenv` and `connect()` again: it raises (no stale cache);
    - with the default `<data>/output`, it doesn't raise.
- **`tests/test_file_registry.py`** (new):
  - root by kind, including an output root equal to and inside the data dir;
  - outside the root raises (`add`) or returns `None` (`add_if_owned`);
  - re-owning raises `OwnershipConflict`;
  - a single-kind replacement returns the old path;
  - `paths_for_delete` + delete + `unlink_paths` removes files and rows; an unlink failure (monkeypatch `Path.unlink` to raise `PermissionError`) is returned;
  - `move`: moved, conflict, a case-only rename on a case-insensitive filesystem, a missing source, and an `os.replace` failure (`PermissionError`) leaving the row unchanged;
  - `repoint`;
  - **`sync`:**
    - registers pattern files for existing owners and skips active recordings;
    - opens no transaction when nothing changed (monkeypatch `db.transaction`);
    - doesn't register a file deleted between scan and write;
    - lists missing and unclaimed;
    - isolates per-file errors;
    - an NFD-named file maps to its NFC owner;
    - a second run changes nothing.
- **Write-site tests:**
  - a web job registers its `.md`, sidecar, and excerpts (with sanitised labels);
  - `save_summary` registers;
  - enrolling registers a reference clip, and renaming the profile moves the row;
  - a journal fold registers `journal.md`;
  - local finalise registers `combined.wav`, the per-user dirs, and `live_transcript.md`;
  - a refine registers its `.md.bak`;
  - `delete_transcript` removes registered *and* unregistered companions;
  - a CLI `save_summary` outside the roots works and registers nothing.
- **`tests/test_job_history.py`** `test_enum_checks_mirror_python_constants` (~84–100): add `file_registry.KINDS` against the `files.kind` CHECK. Run its `check_values` over **the v9 migration's `ddl` only**: its regex takes the first `kind TEXT NOT NULL CHECK (kind IN` across all DDL, which is v7's `search_index_state`.

**Existing tests to update:**
- `tests/test_e2e.py:122–159` and `tests/test_transcript_store.py:~492`: assert the `audio` row (`file_registry.file_for`) instead of `audio_rel_path`. (`tests/test_legacy_import.py:~290` only mentions the column in a comment; update the comment.)
- `tests/test_db.py::test_upgrade_pins_cwd_output` (~561) and `tests/test_path_utils.py::test_output_dir_from_config` (~70) / `::test_relative_output_dir_setting_is_relative_to_data_dir` (~78): keep passing, because conftest still deletes `WISPER_OUTPUT_DIR` and turns `REQUIRE_OUTPUT_ENV` off. Confirm they do.
- `tests/test_schema.py`: the fixture and the `audio_rel_path` violations (above).
- `tests/conftest.py`: `WISPER_OUTPUT_DIR`.
- `tests/_seed.py`: add `seed_file(path, kind, owner, label=None)`.
- **Canaries that must pass unchanged:**
  - `tests/test_legacy_import.py:304–367` (they run the v9 import through a full migrate);
  - `test_delete_removes_file_row_links_and_companions`, `test_delete_keeps_audio_outside_output_root`, and `test_delete_escapes_glob_in_stem` (`tests/test_transcript_store.py:~178–220`). They pass because `_companion_paths` keeps its pattern union.

**Docs:**
- **`architecture.md` "Database":**
  - v9 and v10 entries: the `files` table, owner and root rules, why paths are stored (the exception to "derived values aren't stored", ~356), last-observed stats;
  - the Migrations bullet: FKs off, `foreign_key_check` as the check;
  - `audio_rel_path` is gone;
  - the ordering rule (~365, ~453): read file paths inside the delete transaction, unlink after commit;
  - a Module Map entry for `file_registry.py`.
- **`CLAUDE.md` Non-Obvious Gotchas, add:** "**Every file wisper owns is a `files` row.** Register a file right after writing it (`file_registry.add_if_owned`), move it with `file_registry.move`, and delete by reading `paths_for_delete` inside the owner's delete transaction, then `unlink_paths` after commit. `sync()` (run by reconcile) picks up files written before a crash and never deletes."
- **`CLAUDE.md` "Migrations are frozen" bullet:** an unmerged branch with a new migration refuses to start unless `WISPER_DATA_DIR` and `WISPER_OUTPUT_DIR` both point at copies.
- **`docs/configuration.md`, data storage:** the database records every file wisper owns.

**Done when:**
- The listed tests pass, plus the full `tests/test_schema.py`, `tests/test_db.py`, `tests/test_transcript_store.py`, `tests/test_legacy_import.py`, and `tests/test_e2e.py`.
- `tests/test_file_registry.py` is in CI's `windows storage` list.
- The rehearsal is done.
- `SCHEMA_FROZEN` is `False` on the branch.
- Docs are clean.

---

### Phase 2 — Renames keep a transcript's files; Needs attention; campaign delete choice

**Goal:**
- A transcript renamed outside wisper keeps its summary, sidecar, excerpts, backup, and audio.
- Reconcile matches renames it can prove, relink carries companions, and nothing is swept silently. This fixes a live data-loss bug, so it comes right after the registry.
- Anything unclear is listed for Brandon on the Transcripts page.
- Deleting a campaign offers Delete everything or Keep the files.

**Read first:**
- `transcript_store.py`: `_transcript_files` (384), `_companion_stem` (394), `reconcile` (405–483), `relink` (486), `relink_candidates` (532), `_companion_paths`, `delete_transcript`, `safe_path` (157), `_is_case_insensitive`, `existing_form`
- `file_registry.py` (Phase 1): `files_for`, `move`, `repoint`, `sync`, `last_report`
- `campaign_manager.py`: `delete_campaign` (~197–204); `web/routes/campaigns.py`: campaign delete, relink route (~441–472), `missing_transcripts` (~79–104)
- `web/routes/transcripts.py`: the transcripts list route (~243; it lists `out_dir.glob("*.md")`, so missing transcripts never appear there today), and `@router.post("/{name}/delete")` (400)
- `templates/campaigns.html:170–190`, `templates/transcripts.html`
- `cli.py`: `transcripts list` (1262), `campaigns delete` (~897)

**Steps:**
1. **`transcript_store.rename_companions(transcript_id, old_stem, new_stem, *, output_dir=None, data_dir=None) -> list[Path]`** (no `conn`: it moves files, after the caller's commit). It returns the files it couldn't move (conflicts).
   - Callers capture `old_stem` before they update `transcripts.stem`.
   - The list is `file_registry.files_for(Owner("transcript", transcript_id))`, excluding the `transcript` row, plus any pattern-derived companion of `old_stem` that isn't registered (`_companion_paths` logic). Unregistered files are registered first (`add_if_owned`).
   - For each file whose name starts with `old_stem` (compared NFC, casefolded where case-insensitive), the new name replaces that prefix with `new_stem`, keeping the rest (`_excerpt_SPEAKER_00.mp3`, `.summary.md`, `.md.bak`, `_1.flac`).
   - Move with `file_registry.move`. `"conflict"` and `"error"` go into the returned list (the old file stays); `"missing"` just forgets the row.
   - Moves run after the stem change has committed (companion files follow the row).
2. **`relink()`** captures the old stem, and after its transaction calls `rename_companions(old_id, old_stem, new_stem)`, returning the conflicts. Both relink routes show a notice when there are any: "Relinked. Some files kept their old name because a file with the new name already exists; see Needs attention on the Transcripts page."
3. **Reconcile's automatic rename match.**
   - **Before** opening its transaction, `reconcile()` reads the registered stems with one `db.connection`, stats each `.md` not among them, and groups those by `(st_size, st_mtime_ns)`.
   - Inside the transaction, for each such file whose group has exactly one file: candidates are transcripts with no `.md` on disk (missing now or earlier) whose registry `transcript` row has `size == st_size` and `mtime_ns == st_mtime_ns`. That's the last stat the registry saw; Phase 1's `sync` keeps it while the file exists, and keeps it after the file goes missing.
   - **Exactly one candidate:**
     - `UPDATE transcripts SET stem = ?, missing_since = NULL`;
     - `repoint` its `transcript` row (same transaction);
     - count it as `renamed`, and force the post-commit `sync` past the 30 s throttle so Needs attention doesn't keep old-stem paths;
     - an `IntegrityError` or `OwnershipConflict` from `repoint` is caught (as `add_if_owned` does) and the file falls through to `ensure_row`, so one bad row never rolls back the whole reconcile;
     - remove the old stem from reconcile's `rows` snapshot and `missing_by_fold`, and from the candidate pool, exactly as the case-only branch does with `rows.pop(twin["stem"], None)` (442). Otherwise the final loop (450–453) sets `missing_since` on the transcript it just renamed, and a second file could claim the same row;
     - queue `rename_companions(id, old_stem, new_stem)` for after the commit.

     Its search index stays valid: the content is unchanged.
   - **Zero or several:** fall through to `ensure_row` as today. The missing row stays missing, for Brandon to relink.
   - The existing case-only rename branch (437–443) also queues `rename_companions`.
   - **Order after the transaction commits:** the queued `rename_companions` calls, then `sync()` (Phase 1, step 3), then `check_freshness` (`transcript_store.py:474–478`). `sync` after the match means it never refreshes a fingerprint away first; `sync` after the moves means old-stem files aren't cached as unclaimed; moves before `check_freshness` mean a moved summary isn't seen as deleted and reindexed without it. The case-only branch also drops its twin from the auto-match candidate pool.
   - The first start after the upgrade has no `transcript` rows yet (`sync` creates them after this match), so a rename made before that first start can't be matched automatically; it's listed for relink. That's accepted.
4. **The sweep never deletes a companion.** Remove the companion branch's `entry.unlink()` (465–471), keeping the deletion of stale atomic-write temp files. Change `delete_transcript`'s docstring: crash leftovers are listed under Needs attention.
5. **`transcript_store.needs_attention(output_dir=None, data_dir=None) -> Attention`**, a dataclass with three lists:
   - `missing_transcripts`: rows with `missing_since` set, each with its campaign display name or `None`;
   - `missing_files`: `file_registry.last_report().missing` (registered companions, clips, or journals whose file is gone);
   - `unclaimed`: `file_registry.last_report().unclaimed` (pattern files with no owner).

   If no report exists yet for this `(data_dir, output_dir)`, `needs_attention` runs `file_registry.sync()` first. It reads the report via `last_report(data_dir, output_dir)` for those dirs. Phase 6's read-only dry run doesn't call `needs_attention`; it builds the same lists from `sync(scan_only=True)` and the `missing_since` query.
6. **Transcripts page "Needs attention" panel**, shown only when any list is non-empty. It needs its own query, since the page's list comes from `glob("*.md")`.
   - **Missing transcripts:**
     - a Relink dropdown (`relink_candidates()`) posting to `POST /transcripts/relink`. It copies the campaigns relink route's guards (`_get_safe_content_path` on both names), and errors show as `?error=relink_failed` on the Transcripts page;
     - a **Remove** button (`delete_transcript`). Its confirm (rendered with `|tojson`) says: "Remove this transcript? Its summary, speaker clips, and audio are deleted too."
   - **Missing files:** listed, with a **Forget** button posting `files.id` (an integer) to `POST /transcripts/needs-attention/forget` (declared before every `/{name}/…` route).
   - **Unclaimed files:**
     - output-root items: name, size, last modified, and a **Delete** button. That covers summary, sidecar, excerpts, `.md.bak`, and `.flac`; an unregistered `.flac` may be the user's own file, and Brandon decides (2026-10-03);
     - data-dir items (a `combined.wav`, a clip, a journal): **listed only, never deleted from the page**. An unowned `combined.wav` can be the only copy of a session.
   - **Delete route:** `POST /transcripts/needs-attention/delete-file`, declared **before** every `/{name}/…` route; `POST /transcripts/relink` is declared before them too. It takes the basename only. The name must match one of the output-root patterns above (extend `_companion_stem` to recognise `.flac` and `.md.bak`), and the file must be in the output root and not registered. It deletes through `transcript_store.delete_unowned_file(path)`, which Phase 6 reuses. That function checks only that the path is a regular file inside the output root with no `files` row; the name-pattern check lives in the route (Phase 6 uses it for `<uuid>.wav`, which matches no pattern).
   - One line explains: "Relinking a missing transcript to its renamed file brings its files along."
7. **Campaign delete choice.** Phase 1 has `delete_campaign` keep its journal file (listed as unclaimed).
   - The Campaigns page's delete confirm becomes a choice:
     - **Delete everything:** each transcript via `delete_transcript`, then the campaign, then the journal file (`paths_for_delete` inside the transaction, `unlink_paths` after commit);
     - **Keep the files:** the campaign is deleted, its transcripts stay unassigned, and the journal stays on disk and appears under Needs attention as unclaimed.
   - `campaign_manager.delete_campaign(slug, *, delete_transcripts: bool = False, data_dir=None)`. The default keeps today's behaviour for its callers (`cli.py:912`, `web/routes/campaigns.py:133`) and tests (`tests/test_campaign_manager.py:119`, `:131`).
   - **Delete everything with a locked file:** `delete_transcript` keeps a transcript whose `.md` it can't unlink (`transcript_store.py:340–344`). Delete the rest, then the campaign; that transcript ends up unassigned and is listed under Needs attention so Brandon can retry (Brandon, 2026-10-03).
   - CLI `wisper campaigns delete` gets `--delete-transcripts` (default keep, matching today), and its prompt says which.
8. **Startup and CLI:**
   - `app.py`'s `reconcile(sweep=True)` logs the `needs_attention` counts at WARNING when non-zero;
   - `wisper transcripts list` prints "N items need attention; see the Transcripts page" when non-zero. Phase 6 adds "or `wisper storage trim`".

**Existing tests to rewrite:**
- **The sweep tests in `tests/test_transcript_store.py`:** `test_reconcile_sweeps_only_true_orphans_and_old_temps` (~370), and the case-only and sweep tests at ~327 and ~338. Companions are kept and listed.
- **Relink tests that rename a registered `.md`,** which auto-match now handles. Make them break the fingerprint before reconcile (`os.utime` to a new mtime, or append a byte), so the row goes missing as they expect:
  - `_missing_entry` (`tests/test_transcript_store.py:~416–422`), used by `test_relink_moves_identity_to_new_file` (~425) and `test_relink_refuses_present_source_and_linked_target` (~440);
  - `tests/test_search_index.py::test_relink_reindexes_new_file` (~236);
  - `tests/test_web_routes.py::test_campaign_page_offers_relink_and_relink_route` (~3370);
  - `tests/test_path_traversal.py::test_campaign_relink_rejects_unsafe_stems` (~558), if its setup renames;
  - `tests/test_search_index.py::test_title_index_follows_register_rename_and_delete` (~465): its `_add` registers the `.md` with stats, and `md.rename()` keeps the mtime.
- **Campaign delete tests:** none need changing, thanks to the `False` default; add new ones for `True`.

**Tests** (`tests/test_transcript_store.py`, `tests/test_campaign_manager.py`, `tests/test_web_routes.py`, `tests/test_path_traversal.py`):
- **Relink carries companions:**
  - `Session 5` has a summary, sidecar, two excerpts, a `.md.bak`, and `Session 5.flac`, all registered;
  - rename the `.md` to `Hanataz 05.md` with a changed mtime, reconcile, then relink;
  - all companions are renamed, every row points at the new names, and the sweep deletes nothing.
- **Relink conflict:** a pre-existing `Hanataz 05.summary.md` is returned as a conflict, and both files survive.
- **Unregistered companion** (written with `write_text`): it's registered and renamed too.
- **Automatic match:**
  - register the `.md`, then rename it on disk (`os.replace` keeps the mtime);
  - `reconcile()` renames the transcript (`counts["renamed"] == 1`), leaves its `missing_since` NULL, keeps its campaign position and speakers, and moves the companions and their rows.
- **Ambiguous match:** two missing rows with the same size and mtime, or two new files with the same stat, mean no automatic rename.
- **Case-only rename on a case-insensitive filesystem** (patch `_is_case_insensitive` to True): companions follow, with no conflicts.
- **Windows lock:** `os.replace` raising `PermissionError` during `rename_companions` returns a conflict and leaves the row on the old name.
- **Needs attention:** an orphan `ghost.summary.md` is listed, never deleted by the sweep, and deleted by the Delete route. A registered summary deleted by hand is listed under missing files, and Forget clears it. A data-dir orphan has no Delete button.
- **Route safety:** a transcript named `needs-attention` survives a POST to the delete-file route.
- **Campaign delete:**
  - Delete everything removes the transcripts, their files, and the journal;
  - Keep the files leaves the transcripts unassigned and lists the journal as unclaimed.
- **Path traversal:** the standard payloads on `/transcripts/relink` and `/transcripts/needs-attention/delete-file`.

**Docs:**
- **`architecture.md`** (find by text: the Reconcile and Relink bullets, the `delete_transcript` note, campaign delete):
  - the automatic rename match;
  - companions follow;
  - no companion sweep;
  - Needs attention;
  - the campaign delete choice.
- **`docs/web-ui.md`:** the Needs attention panel, and the campaign delete choice.
- **`docs/scenarios.md`:** new scenario "I renamed a transcript outside wisper". Rename it in Explorer or Obsidian; wisper matches it on the next start, or lists it under Needs attention to relink. Relinking renames its other files for you.
- **`docs/cli-reference.md`:** the `transcripts list` note, and `campaigns delete --delete-transcripts`.

**Done when:**
- The listed tests pass, plus `pytest tests/test_transcript_store.py tests/test_campaign_manager.py tests/test_web_routes.py tests/test_search_index.py tests/test_path_traversal.py`.
- Docs are clean.

---

### Phase 3 — Enroll from stored embeddings

**Read first:**
- `web/enroll_shared.py`: `enroll_profiles` (342), `_load_diar_sidecar`, `find_excerpt_clip` (39), `build_legacy_label_map`
- `web/jobs.py`: `_run_enroll_job`, and `_run_wizard_enroll` (~1400), which it dispatches to (~1251)
- `speaker_manager.py`: `enroll_speaker` (338), `update_embedding`, `_save_reference_clip` (452), `reference_clip_path` (40)
- `speaker_registry.embeddings_from_sidecar` (40)
- `web/routes/transcribe.py`: job-based wizard GET (~350–410) and submit (~470–490)
- `web/routes/transcripts.py`: transcript-based wizard (~675–790), including the `_excerpt_candidates` closure (677–684)
- `templates/speaker_enroll.html:29` (banner), `templates/transcript_detail.html:39` (notice)

**Steps:**
1. **`enroll_shared.stored_embeddings(diar: Optional[dict]) -> dict[str, np.ndarray]`:** `embeddings_from_sidecar(diar) or {}`, and `{}` for `None`. Don't duplicate the space check.
2. **`enroll_shared.audio_available(diar: Optional[dict]) -> bool`:** `diar` is not None, `diar.get("input_path")` is non-empty, and `Path(...).is_file()`. `Path("")` is `.`, which exists, hence the non-empty check.
3. **`enroll_shared.enrollable_labels(diar, labels) -> tuple[list[str], list[str]]`** returns `(enrollable, skipped)`. A label is enrollable if it has a stored embedding, or if audio is available. With `diar is None`, every label is skipped.
4. **`enroll_shared.excerpt_candidates(raw_label: str, legacy_label_map: dict) -> list[str]`:** move the body of the `_excerpt_candidates` closure here and call it from `transcripts.py`. Callers without a legacy map pass `{}`.
5. **`enroll_profiles`** gets new keyword arguments, all defaulting to `None`, so existing callers and tests keep working: `stored: Optional[dict] = None`, `md_path: Optional[Path] = None`. Make `input_path: Optional[Path] = None`.
   - A label's vector is `stored[label]` when present. Otherwise it's extracted from the audio, converted with `convert_to_wav(input_path)` (one argument, so existing test lambdas keep working) lazily and at most once.
   - A **group** enrolls if all its raw labels are enrollable. Otherwise the whole group is skipped and named in the progress log ("Skipped Brad: no saved voice data and the source audio is gone").
   - The single-label new-profile branch (`enroll_shared.py:394–404`) passes `embedding=` and `audio_path=None` when the vector came from `stored`.
   - Keep the existing averaging (`np.mean` of the group's vectors) and the existing-profile EMA path (`update_embedding`).
   - Return the skipped display names.
6. **`enroll_speaker`** gets keyword arguments `clip_source: Optional[Path] = None` and `source_name: Optional[str] = None`. `audio_path`, `segments`, and `speaker_label` become `Optional[...] = None` (they come before `device=`, so they need defaults).
   - With `embedding is None and audio_path is None`, raise `ValueError`.
   - Clip: with `clip_source` existing, `mkdir` the clips dir and `shutil.copyfile(clip_source, reference_clip_path(name))`. Otherwise, with `audio_path`, `segments`, and `speaker_label`, use `_save_reference_clip`. Otherwise no clip.
   - Phase 1 already registers the clip in `enroll_speaker`; extend that same call so it covers the copied clip too (one call, whichever way the clip was made).
   - `enrollment_source = source_name or (Path(audio_path).name if audio_path else "")`. For wizard enrollment that becomes the transcript name instead of a temp WAV name; that's intended (it's shown on the Speakers page).
   - CLI and standalone callers are unchanged.
7. **`enroll_profiles` passes** `clip_source=find_excerpt_clip(md_path.parent, md_path.stem, excerpt_candidates(label, build_legacy_label_map(md_path, segments)))` for the group's first raw label, and `source_name=md_path.stem`. Without `md_path`, it passes neither.
8. **`_run_wizard_enroll`:**
   - Flatten `job.enroll_groups.values()` into labels.
   - If `enrollable_labels` returns none, fail with "Source audio not available", as today.
   - Otherwise pass `stored=stored_embeddings(diar)`, `md_path`, and `input_path` (or `None` when `audio_available` is False). Log the skipped names; the job still completes.
9. **Routes:**
   - Both wizard GETs set `audio_missing = bool(enrollable_labels(diar, <all labels>)[1])`.
   - The job-based wizard loads `diar` with `_load_diar_sidecar(Path(job.output_path))` (import it in `transcribe.py`), not from `job.input_path`.
   - **Both submit routes** (`transcribe.py:~470–485`, `transcripts.py:~765–783`) currently enqueue only `if input_path.exists()`. They change to: compute `enrollable_labels(diar, <submitted labels>)` (labels from `rename_result.groups.values()`, flattened; `diar` from `_load_diar_sidecar(md_path)`, not `job.input_path`).
     - Enqueue the enroll job when any group is fully enrollable.
     - Redirect with `?notice=enroll_audio_missing` when any label is skipped.
     - Renames still apply first.
   - Keep the `audio_missing` and `enroll_audio_missing` names.
10. **Banner and notice text:** "Some speakers have no saved voice data and the source audio is gone. They were renamed but not enrolled; the others were enrolled."

**Existing tests to update:**
- `tests/test_web_jobs.py:~897` `fake_enroll_profiles`: give it `**kw`, so the new keyword arguments don't raise `TypeError`.
- `tests/test_transcript_enroll.py:1359`, `:1385`, `:1420`, `:~1458` keep working unchanged through the `None` defaults. Confirm they still pass.

**Tests** (`tests/test_transcript_enroll.py`, `tests/test_web_jobs.py`, `tests/test_speaker_manager.py`; seed with `seed_sidecar(md, {"diarization_segments": [...], "speaker_map": {...}, "speaker_embeddings": {label: unit_vector}, "embedding_space": EMBEDDING_SPACE})`; for "audio absent", use an output-dir path that doesn't exist):
- **All labels stored, audio absent:** the profile's embedding equals the seeded unit vector. Patch `wisper_transcribe.audio_utils.convert_to_wav` and `wisper_transcribe.speaker_manager.extract_embedding` to raise; they're never called.
- **Two labels in one group:** the profile vector is the unit mean of the two.
- **Existing profile:** `update_embedding` receives the stored vector.
- **Old `embedding_space`:**
  - with audio, extraction runs;
  - without audio, the job fails with "Source audio not available" and the wizard GET shows the banner.
- **Partial:** group A is stored, group B is not, and there's no audio. A enrolls, B is skipped and named in the job log, the job completes, and the submit redirect carries the notice.
- **Mixed with audio:** `extract_embedding` is called once, for the missing label only.
- **Reference clip:**
  - a byte-for-byte copy of the excerpt;
  - with no excerpt and no audio, the profile is created with no clip and no error;
  - `enroll_speaker(embedding=None, audio_path=None)` raises `ValueError`.

**Docs:**
- `architecture.md` (find by text; Phases 1–2 move the line numbers):
  - the enrollment steps ("New profiles are one `profiles` row") and the reference-clip bullet: where a new profile's clip comes from;
  - "Diarization data": embeddings are what enrollment reads;
  - "Converts once": enrollment uses stored embeddings and converts audio only for labels without one;
  - the wizard submit flow: partial enrollment.
- `docs/web-ui.md:88`: replace it with "The wizard enrolls from voice data saved when the transcript was made, so it doesn't re-read the audio. A speaker with no saved voice data is enrolled from the transcript's audio; if that's gone too, the speaker is renamed but not enrolled, and the others still are."

**Done when:**
- The listed tests pass, plus `pytest tests/test_transcript_enroll.py tests/test_web_jobs.py tests/test_speaker_manager.py`.
- Docs are clean.

---

### Phase 4 — Extract audio, drop the upload, keep a FLAC

**Read first:**
- `web/jobs.py`: `submit` (539–604), `cancel` (886–899), `_run_transcription_job` (~1100–1200), `_move_upload_to_output` (197), `_delete_temp_upload` (232), `_extract_speaker_excerpts` (271), `_write_enrollment_sidecar` (160), the `Job` dataclass (~370–430), and `needs_extraction` (490)
- `audio_utils.py`: `convert_to_wav`, `_extract_first_audio_track` (57–155)
- `pipeline.py`: `process_file` (382–480, 640–695)
- `transcript_store.py`: `write_sidecar` (620–680), `read_sidecar` (559–617), `_companion_paths` (273)
- `recording_manager.py`: `load_recording`, `_load`
- `web/app.py`: `_cleanup_orphaned_uploads` (70)
- `web/routes/transcribe.py`: POST `/transcribe` (60–170), `name_check` (176), `_existing_transcript`
- `templates/transcribe.html:234–259`
- `web/routes/record.py`: `_submit_recording_transcription` (949–995)
- `templates/job_detail.html:58`, `:171`

**Steps:**
1. **Shared helpers.** Phase 1 already defines `transcript_store.audio_path(md_path)` and `set_audio(md_path, path)`; use them. Add:
   - **`recording_manager.recording_for_transcript(transcript_id, data_dir=None) -> Optional[Recording]`:** the recording whose `transcript_id` is this transcript, via `load_recording`. Phases 7 and 8 use it.
   - **Lazy imports:** new code in `web/jobs.py` imports `convert_to_wav` and `encode_flac` inside the function (`from wisper_transcribe.audio_utils import …`), so tests patching `wisper_transcribe.audio_utils.*` take effect.
   - **`audio_utils.encode_flac(src: Path, dst: Path) -> None`:**
     - run ffmpeg `-y -i <src> -map 0:a:0 -ac 1 -ar 16000 -c:a flac` into `dst.with_name(transcript_store.TEMP_PREFIX + dst.name)`;
     - then `os.replace` that onto `dst`;
     - on any failure, delete the temp file and raise.

     This is the one ffmpeg call Phases 4 and 6 use, and tests patch it by name.
2. **Temp upload folder.** In `JobQueue.submit()`:
   - Generate the job id first.
   - When the input's name starts with `wisper_upload_` **and the file exists** (`tests/test_job_history.py:185` submits a nonexistent one; it gets no folder and no `upload_dir`):
     - create `Path(input_path).parent / f"wisper_upload_{job_id}"`;
     - if the file exists, move it to `<that dir>/<original_stem><suffix>`. If that move fails (`OSError`, e.g. a Windows-reserved name like `CON`), move it to `<that dir>/upload<suffix>` instead; `output_stem` still names the transcript;
     - set `job.input_path` to the moved file, and record `Job.upload_dir` (new field, default `""`). `upload_dir` and `needs_extraction_flag` are in-memory only, like `is_web_upload`; `job_history`'s allowlist keeps them out of `params_json`.
   - `submit()` rejects an `original_stem` containing `/` or `\` (use its basename). On POSIX, `Path(file.filename).stem` can contain `\`, which would reach `out_dir / (stem + ".md")` and fail the stem CHECK.
   - `source_name` is a submit kwarg, forwarded to `process_file` (step 5). The upload route passes `source_name=os.path.basename(file.filename or "")`, falling back to `original_stem + suffix` when that's empty.
   - **Delete the unconditional rename at `jobs.py:579–584`.** Non-upload inputs are never moved or renamed; the recording hand-off depends on this (step 9).
   - Freeze `Job.needs_extraction` at submit: a new field `needs_extraction_flag: Optional[bool] = None`, set from the original input's suffix. The property returns the flag when it isn't `None`, else today's suffix check, so tests that build `Job(...)` directly keep working. The job page's Extract step then doesn't flicker when `input_path` changes.
3. **`convert_to_wav(path, out_path: Optional[Path] = None)`.** Thread `out_path` into `_extract_first_audio_track`.
   - With `out_path`, `mkdir(parents=True)` and write there. Without it, use the `NamedTemporaryFile` as today.
   - Wrap the whole Popen-and-read section in an outer `try/except BaseException`, which runs `proc.kill(); proc.wait()` if `proc` exists, deletes the partial output, then re-raises.
     - The existing inner `finally: pbar.close()` can itself raise `InterruptedError` on cancel, so the kill must not depend on it.
     - The `FileNotFoundError` branch also deletes the output file.
4. **Extract, then delete the upload.** In `_run_transcription_job`, inside the `try` after the tqdm patch is installed, before `process_file`, when `job.is_web_upload and job.upload_dir`:
   - `wav = convert_to_wav(Path(job.input_path), out_path=Path(job.upload_dir) / "extracted" / "audio.wav")`. The fixed name keeps user-filename characters out of this path, and the `extracted/` subfolder means it can never equal the moved upload (an upload named `audio.wav` or `Audio.WAV` that isn't 16 kHz mono would otherwise make ffmpeg read and write one file). Test: an `audio.wav` upload at 44.1 kHz stereo extracts and transcribes.
   - If `wav != Path(job.input_path)`: unlink the upload and log `job.append_log("Extracted audio; deleted the uploaded file")`.
   - Set `job.input_path = str(wav)`. (Step 2 already set `input_path` to the moved upload.)
   - A web-upload job with an empty `upload_dir` (tests that build `Job` directly) skips extraction and cleanup.
5. **`process_file(..., output_stem: Optional[str] = None, source_name: Optional[str] = None)`.**
   - The `.md` is `out_dir / ((output_stem or path.stem) + ".md")`.
   - The default title uses the same stem.
   - `source_file` is `source_name or path.name`.
   - `JobQueue.submit()` sets `kwargs["output_stem"] = original_stem` for every transcription job, and `kwargs["source_name"]` from the caller (it falls back to unset).
   - Neither key is in `job_history._PARAM_ALLOWLIST`; keep it that way.
6. **Post-pipeline order.** This replaces `jobs.py:1157–1172`; delete `_move_upload_to_output`.
   1. `_extract_speaker_excerpts(...)`, reading `job.input_path`: the WAV, or a recording's `combined.wav`.
   2. **Only when `job.upload_dir` is non-empty** (an extracted upload). Steps 6.2–6.5 key on `upload_dir`, not `is_web_upload`: existing tests build `Job(is_web_upload=True, …)` directly with no extraction (`tests/test_web_jobs.py:598–823`). Target `<output_dir>/<stem>.flac`.
      - If that file exists and isn't this transcript's registered `audio` file, and `job.kwargs.get("overwrite")` isn't true: no new audio is kept ("Kept no new audio: <name> already exists and belongs to something else"). The pre-upload prompt makes this rare.
      - A `<stem>.flac` registered to a **different** transcript is never overwritten, even with `overwrite=True`: no new audio is kept, and it's logged. Overwrite only replaces this transcript's own file, or an unregistered one.
      - Otherwise `encode_flac(wav, target)` and set `job.input_path = str(target)`.
      - On encode failure: log a warning, no new audio is kept, and the job completes.
      - **"No new audio" never deletes the old audio.** Set `job.input_path` to the transcript's existing registered `audio` file if the transcript already exists and that file exists (`file_registry.file_for(owner, "audio")`, resolved with `output_dir=md_path.parent`, where `owner = Owner.for_stem(stem)`; a `None` owner means no previous audio), and log "Kept the previous audio"; else `""`. Steps 6.3 and 6.4 then re-register that same file, so `set_audio` replaces nothing. Without this, an Overwrite whose encode fails (on Windows, the player streaming `<stem>.flac` is enough to make `os.replace` fail) would delete the transcript's only audio. Tests: Overwrite with `encode_flac` raising keeps the old FLAC and its row, both with and without diarization; and a transcript whose `audio` row is a legacy `<stem>.mp4`, rerun without diarization and with Overwrite while `encode_flac` raises, keeps the `.mp4` and its row. `_write_enrollment_sidecar` (`jobs.py:160–195`) swallows exceptions, so make it return `True` only after `write_sidecar` succeeds (`False` on an early return or a swallowed exception). When `job.diarization_segments and job.upload_dir and job.input_path` and it returned `False`, call `set_audio(md_path, Path(job.input_path))` in its own try/except, so a new FLAC is never left unregistered. Test: `write_sidecar` raising after a successful encode still leaves the FLAC as the `audio` row. Step 6.1 stays first, so excerpts always cut from the WAV.
   3. `_write_enrollment_sidecar(...)`, which includes `"input_path"` only when `job.input_path` is non-empty **and `job.recording_id` is not set**. A recording's audio is found through `audio_path()` and must never become a transcript's `audio` row. With diarization, `write_sidecar` calls `set_audio` with `<stem>.flac`, which deletes a re-run's previous audio (e.g. an old `<stem>.mp4`).
   4. When `not job.diarization_segments` (the exact condition: no sidecar was written; `kwargs["no_diarize"]` is not enough, since diarization is also skipped without an HF token) and `job.upload_dir` is non-empty: `transcript_store.set_audio(md_path, Path(job.input_path) if job.input_path else None)` (whatever 6.2 left in `job.input_path`: the new FLAC, or the kept previous audio such as a legacy `<stem>.mp4`; never assume `<stem>.flac`), wrapped in `try/except` (log a warning; the transcript is already written).
   5. If `job.upload_dir` is non-empty and its basename starts with `wisper_upload_`: `shutil.rmtree(job.upload_dir, ignore_errors=True)`. Then `job.is_web_upload = False`.
7. **Failure and cancel.**
   - `_delete_temp_upload(job)` removes `job.upload_dir` under the same guard (rmtree, ignore errors) and leaves every other path alone. A FLAC is written only after the pipeline succeeds, so a failed job leaves nothing in the output dir.
   - `JobQueue.cancel()`'s PENDING branch calls `_delete_temp_upload(job)`.
8. **Name clash prompt with the last-modified time.**
   - `name_check` returns `{"exists": bool, "campaign": str|None, "modified": str|None, "missing": bool, "clashes": list}`, where `clashes` holds any of `"md"`, `"flac"`, `"missing"`, and `exists = bool(clashes)`. `POST /transcribe` refuses any clash without `overwrite=on`. The `.md` clash also covers a **missing** transcript of that name (`missing_since` set): `register()` would otherwise reuse its row and overwrite its audio and speakers.
     - `exists` is True when `<stem>.md` exists, or `<stem>.flac` exists and isn't the registered `audio` of the transcript named `<stem>` (a FLAC owned by another transcript, or by none, both count). The response lists each clash.
     - `modified` is the clashing file's mtime: `datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")`. The `.md`'s is used when both clash.
   - `transcribe.html` shows it: "A transcript with this name already exists (in campaign "X"), last modified 2026-09-12 21:40." For a missing transcript: "A transcript with this name is missing (renamed or deleted outside wisper). Overwriting replaces its audio and speakers; relinking it first keeps them." For an audio-only clash: "An audio file with this name already exists in the transcripts folder, last modified …".
   - `POST /transcribe` refuses either clash without `overwrite=on` (`?error=name_exists`), and passes `overwrite=True` to the job when it's on.
9. **Recording hand-off.** `_submit_recording_transcription` submits `str(recording.combined_path)` with no copy into the output dir. That's the only hand-off: `wisper record transcribe` (`cli.py:1763`) posts to the same route.
   - `original_stem`/`output_stem` is the recording's **linked transcript's current stem** when it has one (a re-transcribe replaces that transcript in place, even after a rename), else `recording.id`.
   - `source_name=recording.name or recording.id`.
   - Test: rename a recording's transcript, re-transcribe, and check there's still exactly one transcript for the recording, under the new name, in its campaign place.
   - `is_web_upload` stays False (the name doesn't start with `wisper_upload_`), so nothing deletes `combined.wav`.
   - Keep the "Transcript file missing after write" check (`TranscriptMissingError`) exactly as it is. It's the detection for the open bug "Missing transcript file after a successful Transcribe job"; note in that bug's entry that the hand-off no longer copies `combined.wav`.
   - `link_transcript` runs in `on_complete`, after the sidecar is written. Until then, `audio_path()` finds no recording for the transcript. That's harmless: nothing reads it in that window.
10. **Startup sweep.** `_cleanup_orphaned_uploads` handles `wisper_upload_*` directories with `shutil.rmtree` (an `is_dir()` branch), keeping the file globs. Update its docstring.

**Existing tests to rewrite or delete:**
- `tests/test_web_jobs.py`:
  - `:571–595`: submit rename assertions;
  - `:598–640` and `:743–790`: the upload moving next to the transcript, and the `_1` collision counter;
  - `:684–740`: failed/cancelled tests, which check the folder rather than the file;
  - `:790–823`: no-diarization deletes the upload;
  - `:181–210`: the shutdown-mid-job assertion on `input_path`.
- `tests/test_record_routes.py`: the tests asserting `<id>.wav` is copied (near `:1179`, `:1481`, `:1673`), and the `input_path=…/output/<id>.wav` fixtures (~`:1463–1803`).
- `tests/test_web_routes.py:1208`: the sweep test.
- `tests/test_job_history.py:185`: it submits a nonexistent `wisper_upload_x.mp3`; it must still pass, since the folder and move happen only when the file exists.
- Exact-dict assertions on `name-check`: `tests/test_path_traversal.py:551` and `tests/test_web_routes.py:3365`, `:3367`. Update them for the new keys.
- **`tests/test_e2e.py`:** the `ml` fixture patches `audio_utils.convert_to_wav` as `lambda p, *a, **k: Path(p)`. Make it honour `out_path` (write a tiny WAV there and return it), and patch `wisper_transcribe.audio_utils.encode_flac` to create `dst`. `test_full_session_lifecycle`'s audio assertion (~`:122`) then checks the `<stem>.flac` `audio` row.

**Tests:**
- **`tests/test_web_jobs.py`** (patch `wisper_transcribe.audio_utils.convert_to_wav` to write a tiny WAV at `out_path`; patch `process_file` to write the `.md` and fill `_result_store`; patch `wisper_transcribe.audio_utils.encode_flac` to create `dst`):
  1. `.mp4` upload with diarization:
     - the upload no longer exists when `process_file` runs (assert inside the mock);
     - excerpts are cut from the WAV;
     - afterwards the output dir holds `<stem>.md`, `<stem>.flac`, and the excerpts/sidecar; the `audio` row is `<stem>.flac`; `upload_dir` is gone; the frontmatter `source_file` is the original filename.
  2. No diarization: `<stem>.flac` is kept, and the `audio` row is `<stem>.flac` (through `set_audio`), and the folder is gone.
  3. `encode_flac` raises: the job completes, there's no `audio` row, the warning is in the job log, and the folder is gone.
  4. An unregistered `<stem>.flac` already exists, without overwrite: it's untouched, there's no `audio` row, and the job log says why. With overwrite, it's replaced. A `<stem>.flac` registered to a different transcript stays untouched even with overwrite.
  5. `process_file` raises: the folder is gone, and there's no FLAC in the output dir.
  6. Cancel while running: patch `convert_to_wav` to set `job._cancel_event` and call `tqdm.write`. The folder is gone.
  7. Cancel while pending: the folder is gone.
  8. 16 kHz mono `.wav` upload: used in place, not deleted before the pipeline, FLAC written, folder gone after.
  9. Re-run of a transcript whose `audio` row was `<stem>.mp4`: that file is deleted, and the `audio` row is `<stem>.flac`.
  10. A non-upload input (a `combined.wav` path with `original_stem=<id>`, `recording_id` set) stays at its original path after `submit()` and after the job. No FLAC is written, and the transcript has no `audio` row.
  11. `needs_extraction` stays True for a `.mp4` job before, during, and after the job.
- **`tests/test_transcript_store.py`:**
  - `audio_path`: returns the FLAC; returns `combined.wav` for a recording-linked transcript with no `audio` row and **no `_diar.json`**; returns `None` when unlinked.
  - `read_sidecar` exposes the same path as `input_path`, including a recording with no speaker rows.
  - `set_audio` updates the `audio` row and deletes the previously registered file; it never deletes a file outside the registry.
  - With the output root set to the data dir, a recording transcription gives the transcript no `audio` row, and `delete_transcript` leaves `combined.wav`.
- **`tests/test_audio_utils.py`:**
  - `out_path` is honoured;
  - a progress loop that raises kills the mocked `Popen` and removes the partial file;
  - `encode_flac` writes through a temp name and leaves no temp file on failure.
- **`tests/test_pipeline.py`:** `output_stem` names the `.md` and the default title; `source_name` sets `source_file`.
- **`tests/test_record_routes.py`:**
  - the hand-off creates no `<id>.wav` in the output dir;
  - the job's input is `combined.wav`;
  - `combined.wav` still exists after a simulated job end and after a simulated failure.
- **`tests/test_web_routes.py`:**
  - the sweep removes `wisper_upload_*` folders and files, and leaves other temp entries untouched;
  - `name-check` returns `modified` for a `.md` clash and for a foreign-FLAC clash;
  - `POST /transcribe` refuses a FLAC-only clash without overwrite.

**Docs:**
- **`CLAUDE.md:135`**, replace with:
  > - **Startup cleanup** — `app._cleanup_orphaned_uploads()` runs on every startup and deletes `wisper_upload_*` folders and files plus `wisper_enroll_*` and `wisper_enrollsrc_*` temp files. Each upload lives in `wisper_upload_<job-id>/` until its job ends, and the job deletes that folder on success, failure, or cancel (pending or running), so the sweep only catches crashes; nothing is running at startup. Never point anything long-lived at these temp paths.
- **`CLAUDE.md:136`**, replace its first two sentences ("Web-upload audio lives next to its transcript … Deleted together with the transcript.") with the following, keeping the rest of the bullet:
  > - **Each transcript keeps one compact audio copy** — a web job extracts the upload's first audio track to a 16 kHz mono WAV, deletes the upload, then keeps the audio as `<stem>.flac` in the output dir (the transcript's `audio` row in `files`), deleted with the transcript. Find a transcript's audio with `transcript_store.audio_path()`: recording transcripts have no `audio` row and resolve to `recordings/<id>/combined.wav`, which a transcript delete must never touch.
- **`architecture.md`** (find each by text; earlier phases move the line numbers):
  - the Re-match backfill step: it reads the transcript's kept audio;
  - the output tree: `<stem>.flac   16 kHz mono audio of an uploaded source (the track the pipeline used)`;
  - "Diarization data": the audio copy sentence (FLAC for uploads; `audio_path()` resolves recordings to `combined.wav`);
  - "Name collisions": the FLAC clash and the modified time;
  - the recording hand-off (`POST /recordings/{id}/transcribe`);
  - rewrite the "Web uploads" paragraph;
  - the post-job steps ("Moves web-upload audio next to the transcript");
  - the startup sweep.
  - Add a Known Constraints row: "The original upload (video, extra audio tracks) isn't kept, only the 16 kHz mono FLAC of its first audio track. To transcribe a file again with a different track, export that track and upload it."
- **`docs/cli-reference.md`** (the `campaigns relabel` notes, ~`:251`): sessions without stored voice data have it re-extracted from the transcript's kept audio.
- **`docs/web-ui.md`:**
  - the Transcribe and job sections say what's kept: the transcript, summary, speaker clips, and the audio as `<stem>.flac`; the uploaded file itself isn't kept;
  - the name-clash prompt shows the existing file's last-modified time.
- **`docs/configuration.md`:** in "Transcripts folder" (~`:80`), add "Each uploaded transcript keeps its audio as `<name>.flac` beside it."
- **`docs/scenarios.md`:** add a known limitation: "Only the first audio track of an uploaded file is transcribed and kept. To use a different track, export it from the original and upload that."
- **`README.md`**, "What You Get" section, after the paragraph ending "…ingest into NotebookLM or Obsidian.", add this paragraph exactly:
  > Only the first audio track of an uploaded file is transcribed. wisper keeps that track as a compact audio copy for playback and re-transcription, not the original file: a video's picture and any other audio tracks are discarded, so keep your originals.
- **Stale grep:** `grep -rn "next to its transcript\|next to the transcript\|durable copy\|durable source audio\|_move_upload_to_output\|copies .combined.wav" CLAUDE.md architecture.md docs/ src/` must come back empty, apart from intended new text.

**Done when:**
- The listed tests pass, plus `pytest tests/test_web_jobs.py tests/test_record_routes.py tests/test_web_routes.py tests/test_transcript_store.py tests/test_pipeline.py tests/test_audio_utils.py tests/test_job_history.py tests/test_e2e.py`.
- `tests/test_audio_utils.py` and `tests/test_web_jobs.py` are added to CI's `windows storage` list (`.github/workflows/ci.yml`).
- The stale grep is clean, and so is the scar-tissue check.

---

### Phase 5 — Trim recordings to `combined.wav`

**Read first:**
- `recording_manager.py`:
  - the layout docstring and `combined_path_for` (64);
  - `record_completed_wav_segment` (430) and `bind_recording_speaker` (492);
  - `recover_recording` (519) and `_restore_segment_rows` (600).
- `web/audio_writer.py`: `concat_wav_segments` (202)
- `web/local_capture.py`: finalise (~540–570)
- `web/discord_bot.py`: `_finalise` (~495–550; async) and the capture-time bind (444)
- `models.Recording.recoverable`
- `web/routes/record.py`: the enroll route (900–941)
- `web/jobs.py`: `_run_recording_enroll` (~1340–1395)
- `file_registry` (Phase 1): `file_for`, `forget_kind`, `paths_for_delete`, `unlink_paths`

**Steps:**
1. Add `recording_manager.trim_recording_audio(recording_id: str, data_dir=None) -> int` (bytes freed). It returns 0 and changes nothing unless `combined.wav` is **complete**:
   - `combined.wav` opens with `wave` and has more than 0 frames (a `b"fake"` file or an empty one returns 0 without raising);
   - **only while `combined/` still exists:** its frame count equals the sum of `getnframes()` over the readable, non-empty `combined/*.wav` files, which is exactly what `concat_wav_segments` joined;
   - **only while `combined/` still exists:** there are no fewer readable segment files than `recording_segments` rows. "Readable" means `wave.open` succeeds and `getnframes() > 0`, the same test `concat_wav_segments` applies. A last segment that never got a row only adds frames to both sides.

   Don't use `duration_s`: it's wall-clock time.

   When `combined/` is gone, an earlier trim already verified `combined.wav`, so only the first check applies. That's what lets the `_run_recording_enroll` call (step 2) delete a Discord speaker's `per-user/<uid>/` after the user is bound later; with the segment checks applied, the sum would be 0 and nothing would ever be deleted.

   Then:
   - delete `combined/` (segments aren't registry rows);
   - read `recordings.source` from the DB. `local` deletes `per-user/`. `discord` deletes `per-user/<uid>/` for each uid with a non-NULL `recording_speakers.profile_id`, and keeps unbound uids;
   - rename each directory it deletes to `.wisper-trash-<n>` inside `recordings/<id>/` (outside any transaction, with `_replace`'s Windows retry), then forget their `per_user` rows in one short transaction, then `rmtree` the trash after commit. A long delete never holds the write lock (capture uses a 500 ms busy timeout). A new `app._cleanup_recording_trash()`, called next to `_cleanup_orphaned_uploads()` at startup, removes leftover `get_data_dir()/recordings/*/.wisper-trash-*` directories after a crash (`_cleanup_orphaned_uploads` only sweeps the temp dir);
   - a directory whose rename fails (`_replace` returns `False`) is skipped and its row kept; `per_user` rows whose directory is already gone (a crash between rename and forget) are forgotten in the same transaction;
   - guard every path with `os.path.abspath(...).startswith(<recordings/<id>/> + os.sep)`.
2. **Call sites.** Log failures; never fail the caller.
   - local finalise, after `combined.wav` is written;
   - Discord `_finalise`, via `await asyncio.to_thread(trim_recording_audio, ...)`;
   - `recover_recording`, only after it succeeds (`combined.wav` rebuilt and its status updated);
   - `_run_recording_enroll`, after `bind_recording_speaker`.

   **Never** from the capture-time bind at `discord_bot.py:444`. Users the bot bound during capture are trimmed at finalise.
3. **Enroll an unbound speaker whose `per-user/<uid>/` is gone:** the enroll route checks `per_user_dir.is_dir()` where it builds it (`record.py:~925`), **before** `queue.submit_recording_enroll`, and redirects with `?error=no_audio`. (Enroll is Discord-only, `jobs.py:1340–1398`, so deleting local `per-user/` never affects it.)
4. **The readers already tolerate missing segment files; confirm, no change:**
   - `_restore_segment_rows` skips a missing dir;
   - `recoverable` applies only without `combined.wav`;
   - the recording page reads segments from the DB.

   Segment rows stay as metadata.

**Tests:**
- **`tests/test_recording_manager.py`.** Add a local helper `_write_segments(rec_dir, frames_list)` that writes `combined/NNNN.wav` files with real `wave` headers, plus a matching `combined.wav`.
  - No-op: without `combined.wav`; with an empty one; with `b"fake"`; with one short of its segments by one frame.
  - A local recording loses `combined/` and `per-user/`.
  - A Discord recording keeps an unbound uid and loses a bound one.
  - Bytes freed are reported, and the deleted directories' `per_user` rows are gone.
  - `recover_recording` trims after rebuilding.
  - **Bind after trim** (`seed_recording` only creates `combined.wav`, so the test builds the `per-user/<uid>/` dirs, their `per_user` rows via `seed_file`, and the `recording_speakers` rows itself): trim a Discord recording, then bind a kept uid and trim again; that uid's `per-user/<uid>/` and its `per_user` row go, and other unbound uids stay.
- **`tests/test_web_routes.py`** (next to `test_cleanup_orphaned_uploads_removes_all_prefixes`, ~1208): a `.wisper-trash-0` dir inside `recordings/<id>/` is removed by `_cleanup_recording_trash()`, and nothing else in that folder is touched.
- **`tests/test_local_capture.py`, `tests/test_discord_bot.py`:** finalise calls trim (mocked; the Discord one through `asyncio.to_thread`).
- **`tests/test_record_routes.py`:**
  - a trimmed recording's page renders;
  - Transcribe and Re-transcribe still submit `combined.wav`;
  - enroll with a missing per-user dir redirects with `error=no_audio`.

**Docs:**
- `architecture.md` (find by text):
  - the recordings tree: `combined/` and local `per-user/` exist only until `combined.wav` is verified complete;
  - the recording layer.
- `recording_manager.py` module docstring layout.
- `CLAUDE.md`'s "Startup cleanup" bullet (as Phase 4 rewrote it): add that `_cleanup_recording_trash()` removes `recordings/*/.wisper-trash-*` left by an interrupted trim.
- `docs/configuration.md`: the `recordings/` line.
- `docs/web-ui.md`: the recordings page.
- `LIVE_AUDIO_TEST_PLAN.md`: grep for `per-user` and `combined/`; reword any step that expects those files after stop.
- `plan.md`, "Manual verification owed" → "Live Discord acceptance test": "play the per-user WAVs" must happen before binding, since a bound user's track is deleted.

**Done when:**
- The listed tests pass, plus `pytest tests/test_recording_manager.py tests/test_local_capture.py tests/test_discord_bot.py tests/test_record_routes.py`.
- Docs are clean.

---

### Phase 6 — `wisper storage trim` (CLI only)

**Read first:**
- `cli.py`: the `db` group (1967–2060) for style; `tests/test_cli.py` for `CliRunner` tests
- `speaker_registry._backfill_embeddings` (134), and how `relabel_campaign` builds `DiarizationSegment`s (~205) and stores results (`:220`, `set_speaker_embeddings`)
- `transcript_store`: `audio_path`, `set_audio`, `needs_attention`, `reconcile`, `delete_unowned_file` (Phase 2); `file_registry` (Phase 1)
- `audio_utils.encode_flac`
- `recording_manager.trim_recording_audio`
- `db.py`: `Heartbeat` (~934), `runtime_leases`, `status()` (its `leases`), `connect(migrate_schema=…, claim_runtime=…)` (771); `tests/test_db.py` `_seed_lease` (338)
- `web/app.py`: startup (~150–190), where `wisper server` takes `db.ServerLock`

**Steps:**
1. **New module `src/wisper_transcribe/storage_trim.py`:**
   - `plan(data_dir=None, output_dir=None) -> TrimPlan` **only reads**: no `reconcile`, and no registry writes. For the Needs-attention list it calls `file_registry.sync(scan_only=True)`, which computes the report without writing (add that mode to `sync`). Its helpers' connections may refresh the lease row; that's harmless now that the server check uses `server.lock`.
   - `apply(plan, device="auto", progress=None) -> TrimReport` executes them.
2. **Actions, in order:**
   1. **Reconcile** (in `apply()` only): `reconcile(output_dir, sweep=True)`, so renames are matched first; then recompute the plan and put `needs_attention()` in the report.
   2. **Transcripts with an existing `audio` row file.** Read the registry row directly (`file_registry.file_for(transcript, "audio")`), never `audio_path()`, which would return a recording's `combined.wav`.
      - Labels come from `transcript_speakers`, else from the `_diar.json` segments.
      - When labels lack a current-`EMBEDDING_SPACE` embedding and segments exist: backfill with `_backfill_embeddings(diar, segments, device)` (it returns a dict of the labels whose extraction succeeded, or `None` when the audio is missing or every extraction fails; wrap the backfill per transcript in `try/except Exception` (`convert_to_wav` raises on a corrupt file); on `None` or an exception, report the transcript under errors and still continue to the audio step; `storage_trim` keeps only the labels that lacked a current-space embedding), building `diar` and segments as `relabel_campaign` does. **Set `diar["input_path"]` to this `audio` row's file**, because `read_sidecar` may resolve a recording to `combined.wav` instead. Store **only the missing labels** with `set_speaker_embeddings`. This runs first, while the original audio still exists.
      - Then shrink the audio to the form new jobs keep:
        - **Recording-linked transcript whose `combined.wav` exists:** `set_audio(md_path, None)`. The file is the old hand-off copy.
        - **Already `<stem>.flac` at 16 kHz mono:** leave it. If probing fails (`ffprobe` missing or erroring), convert rather than abort; setup checks only `ffmpeg`. Check with a new `audio_utils.probe_format(path) -> tuple[int, int]` (sample rate, channels), using `ffprobe`, which tests patch. `soundfile` isn't a dependency. A `.flac` at any other rate or channel count is converted like the rest (Brandon, 2026-10-03).
        - **Otherwise:** `encode_flac(<file>, <stem>.flac)` (same clash rule as Phase 4: never overwrite a file that isn't this transcript's; when the source *is* `<stem>.flac`, encode to a temp name first and replace it), then `set_audio(md_path, <the FLAC>)`, which deletes the original when it's a different file.
        - **Encoding fails:** leave the original and its `audio` row untouched, and report it.
   3. **Orphaned recording hand-off copies in the output root.** Only `<uuid>.wav`, where `Path.stem` is a `recordings.id` and no registry row names the file. Delete it through `transcript_store.delete_unowned_file` (Phase 2). **Never any other file.** Action 2 runs first, so a hand-off copy that is a registered `audio` row has already been handled there.
      - Not `<stem><ext>` next to a transcript: CLI transcripts have no `audio` row, so a CLI user's own `ep1.mp3` beside `ep1.md` would match.
      - Not `<stem>_<n><ext>` either: `Session_1.mp4` may be another transcript's audio.
   4. **Recordings:** `trim_recording_audio` for each.
3. **CLI.** Add a new group `storage` with the command `trim`: `wisper storage trim [--apply] [--device auto|cpu|cuda|mps]`.
   - Without `--apply`, it prints each action (kind, path, size), the total, and the Needs-attention list, and changes nothing.
   - **`--apply`:**
     - **refuses while a server is running** (Brandon, 2026-10-03), through an **OS file lock**, not the runtime lease. The lease table has one row per runtime, which any CLI `connect()` overwrites and never releases, so a dry run followed by `--apply` would be refused.
       - Add `db.ServerLock(data_dir)`: an exclusive non-blocking lock on `<data>/server.lock` (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), released when the process exits.
       - The `wisper server` CLI command (`cli.py:~185–223`) takes it **before** its `db.connect().close()` (`cli.py:210`), so a server started mid-trim refuses before it migrates, creating the data dir first since `server.lock` lives there. It holds it through `uvicorn.run`, in the parent process, and holds it for its lifetime. A dead `--reload` worker's lock can release late on Windows, so the parent holds it, never the workers.
       - **Mechanics:** open with `os.open(path, os.O_RDWR | os.O_CREAT)` (never `'w'`, which truncates, or `'a+'`) and keep the handle referenced for the process's life (garbage collection would release it).
         - On Windows: `os.lseek(fd, 0, 0)` before locking and unlocking, then `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`. Never read or write the locked byte: Windows locks are mandatory.
         - On macOS/Linux: `fcntl.flock(fd, LOCK_EX | LOCK_NB)`, never `fcntl.lockf`, whose POSIX record locks don't conflict within one process (the in-process tests would falsely pass).
         - Close the fd on a failed acquire. The file's existence never means "held". Not the app lifespan: tests start `create_app()` constantly and would collide on the lock. `--reload` child processes must not take it again. If it's held, startup fails with "wisper storage trim is running; try again when it finishes".
       - `--apply` takes it (non-blocking) before doing anything, and holds it until done. If it's held, the message is "Stop the wisper server first, then run this again."
       - **Host/container case:** mirror `_refresh_lease`. When `--apply` runs in a container, read `db.status().leases` (read-only; never creates `wisper.db`) and refuse only on a fresh lease from the *other* runtime when the container crosses the Docker Desktop VM. On the host, `_refresh_lease` already raises `RuntimeConflict` in that case. A recent lease from *this* runtime (an earlier CLI command) never blocks: CLI connections don't release their lease.
       - Never probe liveness with `os.kill(pid, 0)`: on Windows it terminates the process.
     - Then open normally (`db.connect()`), and wrap the run in `with db.Heartbeat(...)`.
   - **Docker:** stop whichever web service is running (`wisper-web` or `wisper-cpu-web`), then run the matching one-off container (Brandon, 2026-10-03):
     - GPU: `docker compose stop wisper-web && docker compose run --rm wisper storage trim --apply`;
     - CPU: `docker compose stop wisper-cpu-web && docker compose run --rm wisper-cpu storage trim --apply`. The image's `ENTRYPOINT` is `wisper` (`Dockerfile:76`, `:94`), so the command never repeats it. When adding this to `docs/docker.md`, also fix its existing commands that repeat it (`:59`, `:62`, `:77`). Also fix the same doubled `wisper wisper` in the `docker-compose.yml` comments (`:37`, `:56`). Leave `docker compose run wisper nvidia-smi` (`:86`) and the Makefile's `bash` targets (`:49`, `:52`) alone; they're outside this work.
   - A second run finds nothing to do, apart from Needs-attention items Brandon hasn't resolved.

**Tests** (`tests/test_storage_trim.py`, plus a `CliRunner` test in `tests/test_cli.py`; mock `wisper_transcribe.audio_utils.encode_flac` to create `dst`):
- **Upload transcript with a `.mp4`, all labels embedded:** `<stem>.flac` is created, the `audio` row is `<stem>.flac`, and the `.mp4` is deleted.
- **Upload transcript with no embeddings:** with `_backfill_embeddings` patched to return vectors, the embeddings are stored before `encode_flac` is called. Assert the order with a parent `mock.Mock()` and `mock_calls`.
- **Already `<stem>.flac` at 16 kHz mono:** untouched. A 48 kHz stereo `<stem>.flac` is converted in place, through a temp file.
- **`encode_flac` fails:** the original and its `audio` row are untouched, and the report lists the failure.
- **Recording-linked transcript with an `output/<id>.wav` copy and an existing `combined.wav`:** the copy is deleted, there's no `audio` row, and `audio_path()` then returns `combined.wav`.
- **Orphans:** an unreferenced `<recording-id>.wav` is deleted. Untouched: `unrelated.mp3`, `ep1.mp3` beside a registered `ep1.md`, `<stem>_1.mp4`, a `<uuid>.wav` whose uuid is not a recording, `<stem>.md`, and any referenced audio.
- **Dry run:** changes nothing. Compare a recursive file listing and `SELECT` rows from `transcripts`, `transcript_speakers`, and `files` before and after.
- **`--apply`:**
  - refused while another process holds `server.lock` (take it in a subprocess, or monkeypatch `db.ServerLock` to report held);
  - **a dry run followed immediately by `--apply` is allowed** (the dry run leaves a fresh lease row; it must not matter);
  - `wisper server` startup refuses while trim holds the lock;
  - in a container that crosses the VM, refused on a fresh `host` lease (`_seed_lease`); a fresh lease from its own runtime (a just-finished one-off command) is not refused.
- **Idempotence:** a second run is a no-op.

**Docs:**
- `docs/cli-reference.md`: a new `wisper storage trim` section covering:
  - what it converts, deletes, keeps, and lists;
  - that it's a dry run by default;
  - the flags;
  - that it refuses while the server is running ("stop the server first").
- `docs/scenarios.md`: a new scenario, "Free up disk space used by older transcripts" (run `wisper storage trim`, review, then add `--apply`).
- `docs/configuration.md`: in the data storage section, one line pointing to `wisper storage trim`.
- `docs/docker.md`: the stop-then-`docker compose run --rm` form.
- `cli.py` `transcripts list`: its Needs-attention line adds "or `wisper storage trim`".
- `architecture.md`:
  - a Module Map entry for `storage_trim.py`;
  - a short subsection stating the conversion and deletion rules, the "embeddings before conversion" order, and the "only files tied to a row" safety rule.
- `README.md`: no change. The docs table row "Every `wisper` command" covers it; Phase 9 confirms.

**Manual check (orchestrator, on a copy of real data):** the dry run lists what's expected:
- Hanataz 09-12 `.mp4` (7 GB): backfill, then convert to `<stem>.flac` (~220 MB);
- Hanataz 09-19: already `<stem>.flac` if transcribed after Phase 4, else converted the same way;
- `13e7f889-….wav` and `b7de8d0a-….wav`: orphans, deleted;
- `8acff998-….wav`: a recording-linked copy, deleted (resolves to `combined.wav`);
- recording trims;
- any Needs-attention items.

Then `--apply`, and a re-run is empty.

**Done when:**
- The listed tests pass, plus `pytest tests/test_storage_trim.py tests/test_transcript_store.py tests/test_cli.py`.
- `tests/test_storage_trim.py` is added to CI's `windows storage` list (`.github/workflows/ci.yml`).
- The manual check is done and docs are clean.

---

### Phase 7 — Play back a session on its transcript page

Every transcript with audio gets the player: uploads (`<stem>.flac`) and recordings (`combined.wav`) through the same route.

**Read first:**
- `web/routes/transcripts.py`: `transcript_detail` (336–375), `_anchor_blocks` (132), `_get_safe_content_path`, `_HtmlSanitizer`
- `formatter.searchable_blocks` (144), `time_utils.parse_timestamp`
- `templates/transcript_detail.html`
- `static/app.js` (`wisperPlayExcerpt` for style), `static/input.css:448` (`.block-anchor`)
- `transcript_store.audio_path`, `recording_manager.recording_for_transcript`
- `templates/recording_detail.html:241` (markers)
- `.claude/rules/web-security.md`

**Steps:**
1. **Audio route `GET /transcripts/{name}/audio`.**
   - Resolve `md_path` with `_get_safe_content_path(name, ".md")`.
   - `audio = transcript_store.audio_path(md_path)`. Return 404 if `None`.
   - Before serving, guard it: `os.path.abspath(audio)` must start with the output root or `<data dir>/recordings/` plus `os.sep`.
   - Media type: `.wav` → `audio/wav`, `.flac` → `audio/flac`, otherwise `mimetypes.guess_type` (old `.mp4`/`.mp3` copies until `storage trim` runs). If that's unknown, 404.
   - Return `FileResponse`. On Windows it holds the file open while streaming, so a rename or delete during playback hits the locked-file path (reported, retried later); that's expected.
2. **Block timing.** `_anchor_blocks` adds `data-start="<seconds>"` to each `<span id="b-N">` whose block has a timestamp that `parse_timestamp` parses. Blocks without one get no attribute.
3. **Player.** `transcript_detail.html` renders a sticky bar only when audio exists (the route passes `audio_url` or `None`). It has:
   - `<audio controls preload="metadata" src="{{ audio_url }}">`;
   - a "Follow along" toggle, shown only when the page has any `[data-start]` block. Transcripts without timestamps get a plain player.
4. **Script in `static/app.js`** (not inline):
   - On load, collect `[data-start]` spans into a sorted array.
   - On `timeupdate`, binary-search for the last block with `start <= currentTime` and toggle a `block-playing` class on it.
   - While Follow along is on, `scrollIntoView({block: "center", behavior: "smooth"})` on block change. The page scrolls inside `<div style="flex:1;overflow-y:auto">` (`transcript_detail.html:36`), not the window: the sticky bar sits inside that container, and the scroll listeners attach to it.
   - A user `wheel`/`touchmove`/`keydown` scroll turns Follow along off; the button turns it back on.
   - **A click anywhere on a `[data-start]` span** sets `currentTime` to its start and plays. Ignore clicks on links inside it.
   - On load with `#b-N`, set `currentTime` to that block's start, without autoplay.
5. **Style** `.block-playing` in `static/input.css` with the existing tokens, then rebuild Tailwind.
6. **Markers.** For a recording-linked transcript (`recording_for_transcript`) with markers, the player bar lists them as buttons labelled with `marker.elapsed_s` as `H:MM:SS` (`recording.markers`, already computed in `_load`); clicking one seeks there. `elapsed_s` is wall-clock time since the session started, while `combined.wav` joins audio frames, so over a long session a marker can land a few seconds off. That's acceptable for "jump to roughly here"; say so in `docs/web-ui.md`. This also covers "Replay markers into the ticker on reload" for finished sessions; that item stays open for the *live* ticker only.

**Tests:**
- **`tests/test_web_routes.py`.** Set `WISPER_OUTPUT_DIR` with `monkeypatch.setenv`, as at `:2275`. Link a recording with `seed_recording(...)` and `link_transcript(rec.id, md_path)`; its 10 ms `combined.wav` is big enough for `Range: bytes=0-99`.
  - the route serves a linked recording's `combined.wav` as `audio/wav`, with a Range request returning 206;
  - it serves an upload transcript's `<stem>.flac` as `audio/flac`, also with Range;
  - it serves a recording-linked transcript with no `_diar.json`;
  - it returns 404 when the transcript has no audio, or the file is gone;
  - the detail page renders the player only when audio exists, and Follow along only when blocks have timestamps;
  - `data-start` survives sanitizing, with correct seconds for `MM:SS` and `H:MM:SS`;
  - a recording with two markers renders two marker buttons with the right times; one with none renders no marker list.
- **`tests/test_path_traversal.py`:** add `/audio` to the existing parametrized payload tests.

**Docs:**
- `docs/web-ui.md`: the transcript page section covers playback, follow-along, click-to-seek, and markers.
- `architecture.md`: the route, `audio_path()`, and how block timing works (one fact per bullet).
- `README.md` (description paragraph, line 5): after "every transcript and session summary is full-text searchable.", add "Each transcript plays its audio back in the browser, highlighting the passage being spoken."

**Done when:**
- The listed tests pass.
- Tailwind is rebuilt and staged.
- On a data copy, the orchestrator has played both a seeded recording and an uploaded transcript in a browser: the highlight follows, a click seeks, and a deep link cues the right block.
- Docs are clean.

---

### Phase 8 — Re-transcribe from the saved audio; a recording's campaign follows its transcript

**Goal:**
- A **Re-transcribe** button on every transcript page with audio. An upload reruns from its `<stem>.flac`; a recording reruns through the recording hand-off. Both replace the transcript in place.
- A transcribed recording's campaign is read from its transcript, so moving the transcript moves the recording with it.

**Read first:**
- `web/routes/record.py`: `_submit_recording_transcription` (~949–995) and the confirm in `templates/recording_detail.html:135–137`
- `web/routes/transcripts.py`: `transcript_detail`, `_get_safe_content_path`
- `templates/transcript_detail.html`
- `job_history.py`: `_PARAM_ALLOWLIST` (30), `_params`, `list_jobs`, `get_job`
- `recording_manager.py`: `_load` (~105–190), `save_recording` (~270–330)
- `job_history.py:182–186` (`_FROM`): it derives a job's campaign with `coalesce(j.campaign_id, ct.campaign_id, rec.campaign_id, …)`. That's a different question (which campaign did this *job* serve?), so it keeps the fallback; don't copy it.
- `pipeline.process_file`: `overwrite`, `TranscriptExistsError`
- `transcript_store.audio_path`, `recording_manager.recording_for_transcript`

**Steps:**
1. **Derive a transcribed recording's campaign.** In `recording_manager._load` (the `LEFT JOIN campaigns c ON c.id = r.campaign_id` at ~`:115`), add `LEFT JOIN campaign_transcripts ct ON ct.transcript_id = r.transcript_id` **before** the `campaigns` join, and use:
   ```sql
   LEFT JOIN campaigns c ON c.id = CASE WHEN r.transcript_id IS NOT NULL THEN ct.campaign_id ELSE r.campaign_id END
   ```
   - Use `CASE`, not `coalesce`: a transcript in no campaign must show no campaign, not fall back to the capture-time one.
   - In `save_recording`'s upsert (`:279–289`), change the `DO UPDATE` to `campaign_id = CASE WHEN recordings.transcript_id IS NULL THEN excluded.campaign_id ELSE recordings.campaign_id END`. An untranscribed recording's campaign can still be set (`tests/test_web_jobs.py:1293–1305` relies on it), and a transcribed one's derived value is never written back. Document the column as "the campaign chosen when recording started, until it has a transcript; a transcribed recording's campaign is its transcript's".
   - What then works unchanged: the Recordings page filter (`record.py:444`), the recording page, recording enroll's `add_member` (`jobs.py:1382`), and the hand-off's `campaign=recording.campaign_slug` (`record.py:986`), which then always matches the transcript's campaign, so `move_transcript_to_campaign` is a no-op. Speaker matching on Re-transcribe (`pipeline.py:599`) uses the current campaign's roster.

   
2. **`job_history.last_transcription_params(transcript_id, data_dir=None) -> dict`.** The `params_json` of the most recent *completed* `transcription` job with this `transcript_id` (ids survive renames, stems don't), filtered to the **session** settings:
   - `language`, `num_speakers`, `min_speakers`, `max_speakers`, `no_diarize`, `include_timestamps`, `post_refine`, `post_summarize`.
   - `post_refine` and `post_summarize` are stored only when True; absent means False.
   - **Engine settings come from the current config:** `model_size`, `device`, `compute_type`, `vad_filter`, `forced_alignment`. Leave them unset.
   - Never return `overwrite` or `campaign`. `overwrite` is in `_PARAM_ALLOWLIST` and the route passes its own, so leaking it raises `TypeError`.
   - Returns `{}` with no history; config defaults then apply.
   - Hotwords and prompts aren't in history (free text is never stored). A rerun uses the config's vocabulary, if any.
3. **Route `POST /transcripts/{name}/retranscribe`:**
   - `md_path = _get_safe_content_path(name, ".md")`; 404 if missing.
   - **Recording-linked** (`recording_for_transcript`): `_submit_recording_transcription(recording, request, data_dir)`. The new route declares `request: Request` like the other routes and passes it on. On an error code, redirect to the transcript page with `?error=<code>`.
   - **Otherwise:** `audio = transcript_store.audio_path(md_path)`. If it's `None`, redirect with `?error=no_audio`. Else:
     ```python
     queue.submit(str(audio), original_stem=md_path.stem, output_dir=str(md_path.parent),
                  title=<current frontmatter title, if any>,
                  source_name=<current frontmatter source_file, else audio.name>,
                  overwrite=True, **last_transcription_params(<the transcript's row id>))
     ```
     - Pass the transcript's **current** campaign slug as `campaign=` (`campaign_manager.get_campaign_for_transcript`), or nothing when it has none. `process_file` then matches speakers against that campaign's roster, as the first run did (`pipeline.py:599–603`), and `move_transcript_to_campaign` is a no-op for its own campaign (`campaign_manager.py:328–329`), so its position is kept.
     - `is_web_upload` is False, so nothing deletes the FLAC. The pipeline converts it to a temp WAV and deletes only that. `write_sidecar` sees the same `audio` file and deletes nothing.
   - Redirect to `/transcribe/jobs/{job.id}` (server-generated id). Error redirects use `urllib.parse.quote(md_path.stem)`.
4. **Button** on `transcript_detail.html`, shown only when audio exists (Phase 7's `audio_url`), as a POST form. Render the confirm text with `|tojson`, since it contains apostrophes:
   > Re-transcribe this session from its saved audio? The transcript is replaced (it keeps its name and campaign place), speaker names you set by hand are reset, and custom vocabulary or prompts from the original upload aren't reused. If it was folded into the campaign journal, the journal is marked as needing a rebuild.
5. **Error display:** `transcript_detail.html` shows `no_audio` ("This transcript has no saved audio to re-transcribe from.") and `not_ready` ("The recording isn't ready to transcribe.").

**Tests:**
- **`tests/test_recording_manager.py`:** moving a recording's transcript to another campaign changes the `campaign_slug` `load_recording` returns, while `recordings.campaign_id` stays unchanged; removing the transcript from all campaigns gives `None`; deleting the transcript gives back the capture-time campaign; `save_recording` on a transcribed recording leaves `campaign_id` unchanged.
- **`tests/test_job_history.py`:** `last_transcription_params`:
  - returns the latest completed job's session settings;
  - ignores failed jobs and other job types;
  - drops engine keys, `overwrite`, `campaign`, and `output_root`;
  - returns `{}` with no history.
- **`tests/test_web_routes.py`.** Mock `queue.submit` with `patch.object(client.app.state.job_queue, "submit")`, as at ~`:1780–1853`.
  - Upload transcript: submit gets the FLAC path, `original_stem`, `overwrite=True`, the session settings, `source_name`, and `campaign=<its current campaign>` (none when it has no campaign); the redirect goes to the job page. A transcript in campaign B at position 3 is still at B position 3 afterwards.
  - Recording-linked transcript: the recording hand-off is called.
  - No audio: redirect with `error=no_audio`, and no submit.
  - The button renders only when audio exists, and its confirm text survives `|tojson`.
- **`tests/test_web_jobs.py`:** a job whose input is the output-root `<stem>.flac` (`Job(is_web_upload=False)`) completes with the FLAC still present, the `audio` row unchanged, and no second FLAC written.
- **`tests/test_path_traversal.py`:** the standard payloads on `/transcripts/{name}/retranscribe`.
- **`tests/test_e2e.py`:** copy the pipeline mocks at the top of that file. Re-transcribing a transcript that's in a campaign and folded into the journal keeps its campaign position and marks the journal stale.

**Docs:**
- `docs/web-ui.md`:
  - on the transcript page, **Re-transcribe**, with what it keeps and resets (mirror the recordings line at `:135`);
  - a transcribed recording shows its transcript's campaign.
- `docs/scenarios.md`: "Re-run a session with a different model": change the model in Config, then Re-transcribe. It reuses the session's speaker counts, language, and post-processing, and takes the model, device, VAD, and alignment from the current config.
- `architecture.md`:
  - the route;
  - settings reuse from job history;
  - why the transcript's current campaign is passed (speaker matching uses its roster; its position is kept);
  - a transcribed recording's campaign is derived from its transcript; `recordings.campaign_id` is the capture-time choice.

**Done when:**
- The listed tests pass, plus `pytest tests/test_web_routes.py tests/test_web_jobs.py tests/test_job_history.py tests/test_e2e.py tests/test_path_traversal.py`.
- Docs are clean.

---

### Phase 9 — Final review (orchestrator; no new features)

1. **Full suite:** `.venv/bin/pytest tests/ -q` (Mac) or `.venv\Scripts\pytest tests/ -q` (Windows). It must be green. Check `--cov` for any public function in `file_registry.py` or `storage_trim.py` with no test.
2. **Tailwind:** `python -m wisper_transcribe.tailwind`; `git diff --exit-code src/wisper_transcribe/static/tailwind.min.css` must be clean or staged.
3. **Full-branch rehearsal** on a fresh scratch copy of the reference data dir and output folder, *including* the `.mp4`s. Free disk space must be at least the size of the copy plus ~20%.
   - Use the "How to run" settings: `WISPER_DATA_DIR`, `WISPER_OUTPUT_DIR`, `TEMP`/`TMP`/`TMPDIR` pointed at scratch, and `--port 8090`, with Brandon's queue idle.
   - Start the branch build (v8 → v10). `wisper db status` reports v10, integrity ok, no drift; under Docker too (the first `ALTER` stores SQL from whichever SQLite ran it).
   - With the server stopped, run `wisper storage trim`, then `--apply`; a re-run is empty.
   - Walk step 9's checks there.
   - Repeat the migration on a second copy under Docker (`wisper-cpu-web`, compose override mounting `data`, `output`, and `recordings`).
4. **CI:** `tests/test_file_registry.py`, `tests/test_storage_trim.py`, `tests/test_audio_utils.py` and `tests/test_web_jobs.py` are in the `windows storage` job's list (`.github/workflows/ci.yml:65`), and that job passes on the PR.
5. **Docs vs code.** For each statement below, find where the docs say it and confirm the code does it:
   - every file wisper owns is a `files` row: registered at write, moved and deleted through the registry, and `sync` never deletes;
   - renames keep a transcript's files, and Needs attention lists what reconcile couldn't match;
   - deleting a campaign offers Delete everything or Keep the files;
   - enrollment uses stored embeddings and enrolls partially;
   - an upload is deleted after extraction, and its transcript keeps `<stem>.flac`;
   - only the first audio track of an upload is transcribed and kept;
   - a recording keeps `combined.wav` (plus `live_transcript.md` for local sessions), and `audio_path()` resolves its transcript to it;
   - Discord per-user tracks are kept until bound;
   - the startup sweep covers folders;
   - the name-clash prompt shows the modified time;
   - `wisper storage trim` rules, including the refusal while the server runs;
   - playback;
   - Re-transcribe (session settings from history, engine settings from config, current campaign);
   - a transcribed recording's campaign is its transcript's;
   - a branch build refuses to start on a copied data dir whose config points at an outside transcripts folder without `WISPER_OUTPUT_DIR`. This is described in `CLAUDE.md` and goes away when the schema is frozen;
   - `wisper server` and `storage trim --apply` exclude each other through `server.lock`.
6. **Stale grep across `CLAUDE.md architecture.md README.md docs/ LIVE_AUDIO_TEST_PLAN.md src/`:** `next to its transcript|next to the transcript|durable copy|durable source audio|_move_upload_to_output|copies .combined\.wav|output/<id>\.wav|audio_rel_path|defer_foreign_keys`. Every hit is either intended current text (`audio_rel_path` stays in `db.py`'s frozen migrations and `legacy_import.py`) or gets fixed.
7. **Scar-tissue check** over the whole branch diff: `git diff main...HEAD -- . ':(exclude)plan.md'` through the grep in the documentation standard.
8. **README:**
   - the description paragraph mentions playback;
   - "What You Get" has the first-audio-track paragraph from Phase 4;
   - the docs table needs no new row.
9. **Manual end-to-end on the rehearsal copy.** This absorbs the Recording and wizard items from "Manual verification owed".
   - Rename a transcript's `.md` in Finder or Explorer and restart: it's matched automatically (or listed under Needs attention), and its summary, excerpts, backup, and audio follow its name.
   - Upload a short `.mp4`:
     - the upload is gone right after extraction;
     - the job finishes with only `<stem>.flac` kept, and its transcript page plays it back;
     - uploading the same name again shows the Overwrite prompt with the existing file's modified time.
   - Change the model in Config, then **Re-transcribe** that upload from its transcript page:
     - the job log shows the new model;
     - the transcript keeps its name and campaign place;
     - `<stem>.flac` is unchanged.
   - Enroll a speaker from its wizard: it completes without reading the audio (the job log shows no "Converting audio"). Then rename a speaker in the wizard and search by the new name with the speaker filter.
   - Record a few minutes, local and (when the bot is available) Discord:
     - add markers, including two in quick succession;
     - edit the notes mid-session, then stop;
     - after stop, only `combined.wav` and (local) `live_transcript.md` remain (Discord: plus unbound users' `per-user/<uid>/`).
   - Transcribe, play it back, jump to a marker, then Re-transcribe.
   - Move that transcript to another campaign: the Recordings page shows the new campaign, and Re-transcribe keeps it there.
   - Delete the transcript: the recording is back under "Awaiting transcription" with its notes intact, and Transcribe works again.
   - Delete a test campaign both ways: Delete everything, and Keep the files (its journal appears under Needs attention).
   - Kill the server mid-session, restart, and use **Recover recording**: segments are kept until `combined.wav` is rebuilt and verified, then trimmed.
   - Kill the server mid-upload and restart: the temp folder is swept.
10. **Freeze the schema:** set `db.SCHEMA_FROZEN = True` in the commit that opens the PR (`test_schema_frozen_on_main` checks it). Tell Brandon that merging migrates his live database on his next launch, after an automatic snapshot in `backups/`.
11. **`plan.md`:** delete this section. Move any still-open item (`combined.wav` as FLAC, backup pruning) to a short "Storage — open" entry.
12. **PR:** ask Brandon before opening it, then open it with a summary and a test plan.
13. **Windows rehearsal (after merge, before Brandon's next `start.bat` launch):** on the Windows PC, run the merged build against a copy of the Windows data dir and output folder, with the step 3 settings. Check the migration and `wisper storage trim` against Phase 6's manual-check expectations, then delete the copy.

**Still open (not in these phases):**
- **Store `combined.wav` as FLAC** (about half the size)? It touches the fixed `recordings/<id>/combined.wav` layout and every reader of it.
- **Prune old `backups/` snapshots** (keep the newest N)? Small today, but it grows with each migration.

---

## Campaign folders — transcripts in a folder per campaign (`feat/campaign-folders`, after storage trim)

**Status:** design decisions taken (Brandon, 2026-10-03). It builds on storage trim's `rename_companions`, `audio_path`, and Needs-attention panel.

**Before any work starts (gate, in order):**
1. **Re-review every decision below against the code as merged** after storage trim: helpers, schema, routes, and anything storage trim changed or learned. Update or drop decisions that no longer fit, and confirm changes with Brandon.
2. **Write the detailed, worker-ready design**, like storage trim's: phases with Read first, Steps, the existing tests to rewrite, new tests, Docs, and Done when.
3. **Run the three reviews again** on that design and fold in the results:
   - an Opus reviewer checking correctness and safety against the code;
   - a Sonnet reviewer walking each phase as the worker who will build it;
   - a database reviewer checking the migration and schema.
4. **Brandon approves** the design. Only then does Phase 1 go to a worker.

**Decisions:**
- A transcript in a campaign lives in `<output root>/<campaign folder>/`. A transcript in no campaign stays in the output root.
- The campaign folder is named after the campaign's display name. Renaming a campaign renames its folder and updates the database.
- Moving a transcript to another campaign (or out of all of them) moves its `.md` and every companion file: summary, sidecar, excerpts, and audio.
- The same transcript name may exist in two campaigns (`A/Session 1.md` and `B/Session 1.md`).
- A file's location is campaign folder + transcript name. The `files` registry (storage trim Phase 1) stores each file's path, and campaign moves update it through `file_registry.move`.
- **Dupes:** any move, rename, or upload that would land on an existing file prompts Keep or Overwrite, showing that file's last-modified time.
- **Renames:** a Rename action in wisper renames the `.md` and its companions together (`rename_companions`), with the same clash prompt.

**Known consequences, for the design pass:**
- `transcripts.stem` is `UNIQUE` across all transcripts (`db.py:323`), and its CHECK forbids path separators. Uniqueness becomes per campaign, while the campaign lives in `campaign_transcripts`, a separate table. One option is moving `campaign_id` onto `transcripts` with a unique index on `(coalesce(campaign_id, 0), stem)`. Either way it needs a new migration that rebuilds `transcripts`; every table that references it needs care.
- URLs identify a transcript by name (`/transcripts/{name}`, campaign and journal routes, search deep links). With duplicate names they need the campaign or the row id.
- 28 path builders in 11 files assume `<output root>/<stem><suffix>`. They go through one location helper.
- `reconcile()` and `_transcript_files` scan only the output root. They scan campaign folders too, and a file found in a campaign folder that doesn't match its row's campaign is a move to resolve, or a Needs-attention item.
- Display names may contain characters Windows forbids in folder names, and two display names may map to the same folder name. Sanitizing and uniqueness rules are needed.
- Obsidian links Brandon typed by name keep working after a move. Links written with a path, or ambiguous names shared by two campaigns, may not.
- The existing flat output root needs a one-time migration of files into campaign folders: a CLI command with a dry run, like `wisper storage trim`.
**Database review input (2026-10-03, against v1–v8 plus storage trim's planned v9 `files` table).** Re-check each item at the gate.
- **Migration number:** v11 (storage trim adds v9, the `files` registry, and v10, which drops `audio_rel_path`).
- **Foreign keys off during migrations:** storage trim Phase 1 adds `PRAGMA foreign_keys=OFF` to `migrate()`. Without it, rebuilding `transcripts` would cascade-delete its speakers, search data, campaign places, journal entries, and `files` rows, and the final `foreign_key_check` would still pass. The v8→v10 upgrade test must seed every child table and assert their row counts are unchanged.
- **Per-campaign uniqueness:** move `campaign_id` and `position` onto `transcripts` and drop `campaign_transcripts`.
  - `UNIQUE (campaign_id, stem)`, plus a partial unique index on `stem` for the root (`WHERE campaign_id IS NULL`).
  - `CHECK ((campaign_id IS NULL) = (position IS NULL))`, `UNIQUE (campaign_id, position)`, and `UNIQUE (campaign_id, id)` as the journal's FK target.
  - `campaign_id REFERENCES campaigns(id)` with no action (RESTRICT): a campaign's transcripts move out (with the clash prompt) before it's deleted.
  - Rebuild `journal_entries` to reference `transcripts(campaign_id, id)`. A `BEFORE UPDATE OF campaign_id` trigger deletes the journal entry on a move, which marks the journal stale via `journal_entries_ad`. Pin its ordering with a test.
- **`campaigns.folder` is stored:** `TEXT NOT NULL COLLATE NOCASE UNIQUE`, with a CHECK forbidding `/ \ : * ? " < > |`, a leading dot, and trailing spaces or dots.
  - Rename order: `display_name` changes at once; `folder` changes only after the directory rename succeeds (Obsidian or Explorer may hold it open).
  - v11 seeds `folder` from `slug`. Its import step sets sanitized display names with a frozen sanitizer, de-duplicated with `casefold()`.
- **Rebuild procedure:** create `X_new`, copy (keeping `id`), `DROP X`, `ALTER TABLE X_new RENAME TO X`. Never rename the old table first: SQLite then rewrites child FKs to point at it.
  - Recreate the `transcript_titles_*` triggers and run `INSERT INTO transcript_titles(transcript_titles) VALUES('rebuild')`.
  - All structural changes go in the DDL string, because `expected_schema()` replays only DDL.
- **`transcripts.id` becomes `INTEGER PRIMARY KEY AUTOINCREMENT`,** so ids are never reused, and URLs key transcripts by id.
- `transcripts.audio_rel_path` is already gone (v10).
- **`legacy_root` (a flag for files not yet moved into their campaign folder)** is unnecessary only if every path lookup resolves through `files.rel_path` (the `transcript` row is the authoritative `.md` location; storage trim Phase 1). If any helper builds paths from campaign folder + stem instead, it comes back. Decide at the gate: one source of truth for locations.
- **Code that ships with v11:**
  - every `campaign_transcripts` query (`campaign_manager`, `journal`, `search_index`, `transcript_store`, `job_history`);
  - `_purge_recording_files` (`record.py:526`) and every other output-root-only path check: `recording_manager._transcript_id` (`:256`), `search_index.reindex_path` (`:215`), `job_history._subject_ids` (`:82`), and `JobRecord.output_path`. These move to the registry.
  - `recording_manager._load`'s join on `campaign_transcripts` (storage trim Phase 8) becomes `transcripts.campaign_id`.
  - A campaign-folder rename rewrites `files.rel_path` by prefix with `substr(rel_path, 1, length(:old) + 1) = :old || '/'`, not GLOB (folder names can contain `[`, `*`, `?`).

**Questions for the gate (second review round, 2026-10-03):**
1. **One source of truth for locations:** `files.rel_path`, or campaign folder + stem (see `legacy_root` above)?
2. **Campaign delete:**
   - `files.campaign_id` (v9) cascades, but v11 makes `transcripts.campaign_id` RESTRICT; reconcile the two;
   - storage trim Phase 2's Delete everything / Keep the files choice must also move or delete the campaign folder;
   - does the journal live in the data dir or in the campaign folder?
3. **URLs:** `/transcripts/{id}`; what happens to old stem URLs, search deep links, and Obsidian links.
4. **`process_file` must know the campaign before writing,** so the `.md` lands in the right folder. Define the CLI behaviour too.
5. **Folder names:**
   - Windows rules: reserved names (CON, PRN, AUX, NUL, COM1–9, LPT1–9), trailing dots and spaces, MAX_PATH (260) with long names;
   - Unicode case folding (`COLLATE NOCASE` folds ASCII only);
   - what to do when the output root already holds a user folder with that name. Never adopt or rename a folder wisper didn't create.
6. **Folder renames on Windows** fail while any file inside is open, including by the search indexer. They need retry, plus a crash rule between the directory rename and the `rel_path` update. A per-file campaign move isn't atomic across N files either.
7. **Refuse moves** while a job targets that transcript.
8. **"Overwrite" on a clash** never replaces a file wisper doesn't own (no `files` row).
9. **Vault clutter:** companions (excerpts, `_diar.json`, `.flac`, `.bak`) sit in each campaign folder. Consider a hidden `.wisper/` subfolder.
10. **Scanning:** reconcile's scanning depth; whether the size/mtime rename match also detects cross-folder moves.
11. **Moving existing files:** the flat-to-folders command (dry run, rollback, and its order relative to `wisper storage trim`).
12. **Every flat-root check** must move to the location helper: `pipeline._under_output_root` (707–712), `campaigns.py` `_transcript_missing`/`summarized` (~89–104), `transcribe._existing_transcript`/`name_check`, `search_index.check_freshness`/`_paths`, `find_excerpt_clip`, `link_transcript`/`_transcript_id`, and every stem-keyed API.
13. **Case sensitivity across moves:** a data dir moved between case-insensitive (Windows, macOS) and case-sensitive (Linux) filesystems. Should per-campaign stem uniqueness ignore case?
14. **Storage trim's flat-root assumptions** must all change: `_v9_import`'s "contains `/`" skip; `sync`'s top-level-only scan; the basename-only Needs-attention routes and `_companion_stem`; Phase 6's orphan rule; Phase 7's audio-route guard.
15. **`files` CHECKs:** `(root = 'output') = (transcript_id IS NOT NULL)` and the `journal` path pin forbid a journal inside a campaign folder. Changing them means rebuilding `files` in v11 (Q2 decides).
16. **`campaigns.folder` renames:** rewrite `files.rel_path` by trigger (like `files_profile_key_au`) or by code?
17. **CLI:** do `wisper transcribe --campaign` and folder runs (`_folder_output_path`, `pipeline.py:717`) write into campaign folders?

---

## Forced word alignment — follow-ups

Design in `architecture.md` ("Forced word alignment"). The `forced_alignment = auto` default rests on spot-check evidence, not a full labelled set: on a 2 h episode, aligned and unaligned runs disagreed on 155 of 20,675 words; one-word "islands" inside another speaker's run were 10 (unaligned) vs 3 (aligned); 4 of 4 hand-checked disputed words were right with alignment.

- **If attribution at speaker changes regresses, or before changing the aligner, smoothing thresholds, or the default,** run `scripts/alignment_eval.py` (see `docs/scenarios.md`) on a live-table excerpt and label its sheet. Arms already exist for the open questions: `aligned-guard-1s` (on edited podcast audio, words moved >1 s favoured Whisper 8 vs 1, the opposite of the Hanataz spike's 28 vs 1), `aligned-smooth-1w` / `aligned-nosmooth`, and `aligned-exclusive`.
- **Fallback engine:** `torchaudio` MMS_FA + star token (no new dependency, but CC-BY-NC weights and weaker on crosstalk). Only if the `transformers` dependency becomes a problem; recipe in git history (`6286e21`).
- **Watch:** `Qwen3ASR*` is new in transformers 5.x and may be renamed; the calls are covered by tests.

---

## Speaker consistency — remaining

Design in `architecture.md`. Open items:

- **Diarization measurement set.** 8–12 hand-corrected excerpts of 2–3 min, stratified by speaker count (2–3 / 4–5 / 6–8), in-room vs remote, low vs high overlap. Report DER split into missed / false alarm / confusion (`pyannote.metrics`) plus JER, and compare configs with a paired bootstrap over recordings (B ≥ 1000). The 0.55 threshold and the community-1 choice rest on one session pair until this exists.
- **Ruled out for now:** Sortformer (4-speaker cap), DiariZen (CC-BY-NC), NVIDIA Nemotron diarization (no independent validation). Revisit only if community-1 plateaus on the measurement set.

---

## Live recording — feature requests

- **Change input devices mid-session.** `LocalCaptureManager.start_session()` binds both capture threads to fixed device IDs; switching today means Stop + Start (a new `Recording` and a gap in the transcript). Needs capture threads that can restart against a new device while the tick thread, segment writers, and `Recording` keep running. Needs a design pass first.
- **Per-speaker diarization on the live system track.** The system track is a single RMS-attributed "Other", because OS loopback is already a mixed-down stream. pyannote only gives consistent labels across a whole-file pass; live use would need an incremental layer (embed each turn, match against a running per-session speaker pool) plus added latency in the live loop. Revisit if the You/Other split becomes limiting.
- **Channel picker on the Record page.** `GET /api/record/channels` already lists the guilds and voice channels the bot can see; the Record page still takes raw IDs or a preset.
- **Replay markers into the ticker on reload.** Markers persist (`recording_markers`) and show on the detail page, but a page reload doesn't re-insert them into the live ticker (only transcript lines come back through SSE). For finished sessions, the transcript-page player lists markers ("Storage trim" Phase 7); this item is the live ticker only.

---

## Intel Arc GPU support (planned — not started)

Design only; no code written. File:line references date from the original design spike — re-locate them before implementing.

### Context

wisper-transcribe today accelerates on **NVIDIA (CUDA)** and **Apple Silicon (MPS)**. We want **Intel discrete Arc GPUs** (Alchemist + Battlemage, validated on an **A310**) to be a first-class target — "as flawless as CUDA."

The hard constraint that shapes everything: **faster-whisper / CTranslate2 has no Intel backend** — it runs CPU or CUDA only. So transcription on an Arc card requires a *second inference engine*, not a device-string tweak. Diarization/embedding (pyannote on PyTorch) **can** run on Intel via the PyTorch `xpu` device, so those are mostly plumbing.

**The codebase already has the pattern we need.** The MLX path for Apple Silicon (transcriber.py:171-186) dispatches to an alternate backend (`_transcribe_mlx`) based on device, returning the same `list[TranscriptionSegment]`. The OpenVINO backend mirrors this almost 1:1.

### Locked decisions (from PM)

| Decision | Choice | Implication |
|---|---|---|
| User-facing device token | **`intel`** | `--device intel`, `device = "intel"`. Translated internally: torch `xpu`, OpenVINO `GPU`. |
| `device=auto` behavior | **Auto-select** | Resolve order: CUDA → **intel** → MPS → CPU. No flags needed when an Arc card is present. |
| XPU diarization op-gap | **Warn loudly, continue on CPU** | Transcription stays on the Arc GPU; diarization/embedding retry on CPU with a prominent per-run warning. |

### Architect-level calls (documented for the record)

- **Transcription engine = `optimum-intel` (`OVModelForSpeechSeq2Seq`).** Auto-converts HF Whisper models to OpenVINO IR on first run and caches them (mirrors MLX's auto-download UX). `openvino-genai`'s `WhisperPipeline` is faster but lower-level — a **future perf lever**, not v1.
- **OpenVINO uses HF-format Whisper models** (e.g. `openai/whisper-large-v3-turbo`), a *separate download* from faster-whisper's CTranslate2 models. Documented; not a blocker.
- **`compute_type` does not apply to the OpenVINO path.** CT2 quant types are CT2-only. On `intel`, OpenVINO defaults to FP16 on GPU; INT8 (NNCF) is future. The intel path **skips `resolve_compute_type()`**.
- **Optional dependency, mirroring `[macos]`/mlx.** Core install unchanged; Intel is `pip install "wisper-transcribe[intel]"` + a torch XPU-index install (handled by Docker/setup scripts, like cu126).

### Device translation model (the heart of the change)

One user token (`intel`) fans out to two frameworks. Centralize the mapping in one place.

```
user "intel"
   ├─ transcription  → OpenVINO  device="GPU"   (optimum-intel)
   └─ diarization /  → PyTorch   torch.device("xpu")
      embedding
```

**Add to `config.py`:**

```python
def torch_device_string(device: str) -> str:
    """Map the user-facing device token to a torch device string.
    'intel' is exposed to users but PyTorch/IPEX call Intel GPUs 'xpu'.
    Everything else passes through unchanged.
    """
    return "xpu" if device == "intel" else device
```

**Extend `get_device()` (config.py:148) — the order encodes the auto-select decision:**

```python
def get_device() -> str:
    """Return 'cuda', 'intel', 'mps', or 'cpu' based on available hardware."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            return "intel"          # ← Intel Arc, auto-selected after CUDA
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except ImportError:
        return "cpu"
```

> **Builder note:** `torch.xpu` is provided natively by the **PyTorch XPU wheel** (`https://download.pytorch.org/whl/xpu`, the Intel analog of cu126). IPEX adds extra op coverage/perf but `torch.xpu.is_available()` works without it on the XPU build. The `hasattr(torch, "xpu")` guard keeps CPU/CUDA wheels (no `torch.xpu`) safe.

### Implementation phases

Each phase is committed separately and **pauses for PM review**. **Docs are updated in the same commit as the code** (CLAUDE.md rule). Hardware-validation milestones run on the Proxmox A310 (native Linux build or the Docker `intel` target); Windows-native is validated-by-proxy.

#### Phase 1 — Device plumbing & detection (no hardware; 100% unit-testable)

**Goal:** `intel` is a recognized device everywhere CUDA/MPS are, `auto` detects it, and selecting it without the backend gives a clean "not installed" error.

| File | Change |
|---|---|
| config.py | Add `torch_device_string()`; extend `get_device()`. `resolve_compute_type()` unchanged (CT2-only). |
| cli.py:29 | Add `"intel"` to `--device` `click.Choice`. |
| cli.py:202-208 | `setup` command: add `"intel": "Intel Arc GPU (XPU/OpenVINO)"` label + note (transcription=OpenVINO, diarization=XPU). |
| web/routes/config.py:28 | Add `"intel"` to the device choices array. |
| docs/configuration.md, docs/cli-reference.md | Document the `intel` device value. |

**Tests** (test_config.py, test_cli.py): `get_device()` → `"intel"` when `torch.xpu.is_available()` mocked True (cuda False); `torch_device_string("intel") == "xpu"` + passthrough; CLI/web accept `--device intel`.

#### Phase 2 — OpenVINO transcription backend ⭐ core feature

**Goal:** `--device intel` transcribes Whisper on the Arc GPU. **Mirror the MLX structure exactly.**

`transcriber.py` additions (model after `_MLX_MODEL_MAP` / `_is_mlx_available` / `_transcribe_mlx`):

```python
_OPENVINO_MODEL_MAP = {
    "tiny":            "openai/whisper-tiny",
    "base":            "openai/whisper-base",
    "small":           "openai/whisper-small",
    "medium":          "openai/whisper-medium",
    "large-v3":        "openai/whisper-large-v3",
    "large-v3-turbo":  "openai/whisper-large-v3-turbo",
}

def _is_openvino_available() -> bool:
    """True if optimum-intel + openvino are importable. Cheap find_spec check
    (mirrors _is_mlx_available) so it's safe to call from the uvicorn process;
    the heavy import happens inside _transcribe_openvino (worker/subprocess)."""
    import importlib.util
    return (importlib.util.find_spec("optimum") is not None
            and importlib.util.find_spec("openvino") is not None)

def _transcribe_openvino(audio_path, model_size="medium", language="en",
                         initial_prompt=None, hotwords=None):
    """Transcribe on an Intel GPU via OpenVINO (optimum-intel).
    Returns list[TranscriptionSegment] — same contract as faster-whisper/MLX."""
    from optimum.intel import OVModelForSpeechSeq2Seq
    from transformers import AutoProcessor, pipeline as hf_pipeline
    from tqdm import tqdm

    repo = _OPENVINO_MODEL_MAP.get(model_size, f"openai/whisper-{model_size}")
    tqdm.write(f"  Using OpenVINO backend ({repo}) on Intel GPU")

    # export=True converts HF → OpenVINO IR on first run and caches it.
    # device="GPU" targets the Intel Arc.
    model = OVModelForSpeechSeq2Seq.from_pretrained(repo, export=True, device="GPU")
    processor = AutoProcessor.from_pretrained(repo)
    asr = hf_pipeline(
        "automatic-speech-recognition",
        model=model, tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        chunk_length_s=30, return_timestamps=True,
    )
    # hotwords → initial_prompt prefix, same trick as MLX (no native hotwords param).
    prompt = initial_prompt or ""
    if hotwords:
        hw = ", ".join(hotwords)
        prompt = f"{hw}. {prompt}".strip() if prompt else hw
    gen = {"language": language} if language else {}
    if prompt:
        gen["initial_prompt"] = prompt   # passed via generate_kwargs where supported
    result = asr(str(audio_path), generate_kwargs=gen)

    segs = []
    for ch in result.get("chunks", []):
        start, end = ch.get("timestamp", (None, None))
        text = (ch.get("text") or "").strip()
        if text and start is not None and end is not None:
            segs.append(TranscriptionSegment(start=float(start), end=float(end), text=text))
    return segs
```

Dispatch in `transcribe()` — add a branch beside the MLX one (transcriber.py:167-187):

```python
    if device == "auto":
        device = get_device()

    if device == "intel":
        if not _is_openvino_available():
            raise RuntimeError(
                "Intel GPU transcription needs the OpenVINO backend.\n"
                "Install it with: pip install 'wisper-transcribe[intel]'\n"
                "Or use --device cpu."
            )
        return _transcribe_openvino(audio_path, model_size=model_size,
                                    language=language, initial_prompt=initial_prompt,
                                    hotwords=hotwords)
    # ... existing MLX (mps) branch and faster-whisper path unchanged ...
```

> **Builder notes:**
> - `load_model()` / CTranslate2 are **never touched** on the intel path — `_transcribe_openvino` returns first. Leave `load_model` as-is.
> - `compute_type` is intentionally ignored on intel. If user set non-`auto`, `tqdm.write` a one-line note it doesn't apply to OpenVINO.
> - `vad_filter` has no OpenVINO equivalent here (like MLX) — silently skipped.
> - Confirm the exact `generate_kwargs` prompt key against the installed transformers version during hardware bring-up; `initial_prompt` support varies. **Flag for hardware milestone.**

| File | Change |
|---|---|
| pyproject.toml:44 | New extra: `intel = ["optimum-intel[openvino]>=1.20", "openvino>=2024.4", "transformers>=4.45"]`. (torch XPU build is index-url — Phase 4.) |
| docs/setup.md, architecture.md | Add OpenVINO backend to the component table + a design-decision section. |

**Tests** (test_transcriber.py): patch `_is_openvino_available`→True and mock the optimum/HF pipeline factory; assert `transcribe(..., device="intel")` returns mapped `TranscriptionSegment`s; assert chunk→segment mapping (drop empty/None); assert clean RuntimeError when unavailable. **No real model load** — mock the pipeline as `test_transcriber.py` mocks `WhisperModel`.

**🔌 Hardware milestone #1 (A310):** real transcription on GPU; verify IR conversion + cache; sanity-check WER vs CPU.

#### Phase 3 — XPU diarization & embedding (warn-and-fallback)

**Goal:** pyannote diarization + speaker embeddings run on the Arc GPU via `xpu`, with a loud CPU fallback on op gaps.

`diarizer.py` — `load_pipeline()` (diarizer.py:91-121):

```python
    import torch
    from .config import torch_device_string
    if device == "intel":
        if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            raise RuntimeError("Intel XPU not available. Install the PyTorch XPU "
                               "build, or use --device cpu.")
    # ... existing cuda / mps validation ...
    _pipeline.to(torch.device(torch_device_string(device)))   # 'intel' → 'xpu'
```

`diarize()` — wrap execution with warn-and-fallback (realizes the PM decision):

```python
    try:
        diarization = _pipeline(audio_dict, hook=hook, **kwargs)
    except Exception as exc:
        if device == "intel":
            from tqdm import tqdm
            tqdm.write("⚠  INTEL GPU DIARIZATION FAILED — falling back to CPU for "
                       f"diarization (transcription stays on GPU). Reason: {exc}")
            load_pipeline(hf_token, "cpu")          # reload pipeline on CPU
            diarization = _pipeline(audio_dict, hook=hook, **kwargs)
        else:
            raise
```

`speaker_manager.py` — `_load_embedding_model()` (speaker_manager.py:101-121): mirror the translation (`device in ("cuda","mps","intel")` → `.to(torch.device(torch_device_string(device)))`) and the same try/warn/CPU-fallback around `inference.crop(...)` in `extract_embedding()`.

> **Builder notes:**
> - The fallback **reloads** the module-level `_pipeline` on CPU (can't reliably move a partially-failed pipeline). After a fallback the global stays CPU for the rest of the run — acceptable; resets next process.
> - Keep the warning prominent and **every run** (PM decision), not once-per-session.

**Tests** (test_diarizer.py, test_speaker_manager.py): mock `torch.xpu.is_available()`→True, assert `_pipeline.to` called with `torch.device("xpu")` for `device="intel"`; simulate the pipeline call raising once, assert warning + CPU reload+retry returns `DiarizationSegment`s.

**🔌 Hardware milestone #2 (A310):** diarization on GPU; deliberately exercise the fallback and confirm warning + CPU completion.

#### Phase 4 — Packaging: Docker, Linux, Windows

**Docker** — new `intel` stage in `Dockerfile` mirroring `gpu`, plus the **Intel GPU runtime** the OpenVINO GPU plugin needs (the extra step CUDA doesn't have):

```dockerfile
# ── intel target ──────────────────────────────────────────────────────────
FROM base AS intel
# Intel GPU runtime for the OpenVINO GPU plugin + level-zero (apt, from Intel repo):
#   intel-opencl-icd, libze1, libze-intel-gpu1   (package names per Intel's docs)
RUN pip install --no-cache-dir -e ".[intel]" \
 && pip install --no-cache-dir --upgrade "torch>=2.8.0" "torchaudio>=2.8.0" \
        --index-url https://download.pytorch.org/whl/xpu \
 && python -m wisper_transcribe.tailwind
ENTRYPOINT ["wisper"]
CMD ["--help"]
```

**docker-compose.yml** — `wisper-intel` + `wisper-intel-web` services with `/dev/dri` passthrough (Intel analog of the nvidia `deploy.resources` block):

```yaml
  wisper-intel-web:
    build: { context: ., target: intel }
    image: wisper-transcribe:intel
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "render"        # host 'render' gid; may need a numeric gid on some hosts
    # ... shared volumes/env/ports as the other services ...
```

**Makefile:** `start-intel` (`docker compose up wisper-intel-web`), `build-intel`, `shell-intel`.

**setup scripts:**
- `setup.sh` (Linux): detect Arc (`lspci | grep -i 'VGA.*Intel.*Arc'` or `/dev/dri/renderD*` + `clinfo`); install torch from xpu index; `pip install -e ".[intel]"`; verify `python -c "import torch; print(torch.xpu.is_available())"`.
- `setup.ps1` (Windows): detect via `Get-CimInstance Win32_VideoController` name match `Arc|Intel`; install xpu-index torch; `[intel]` extra. **Windows = code-complete, validated-by-proxy.**

| File | Doc |
|---|---|
| docs/docker.md | Intel section: `/dev/dri` passthrough, `render` group, runtime packages, `make start-intel`, `wisper --device intel` verify. |
| docs/setup.md | Intel install paths (Docker / Linux / Windows), model-storage note (separate HF-format models), A310/4 GB model-size guidance. |

> **Builder notes / risks:**
> - Intel **compute-runtime apt package names drift** across base-image versions — confirm against Intel's current install guide during the Docker build; most failure-prone step. Pin versions once known good.
> - On Proxmox: the A310 must be **passed through to the VM/LXC** running Docker, and `/dev/dri/renderD128` visible inside the container; the container user needs the host `render` gid.
> - torch XPU wheels bundle the SYCL runtime, but the **OpenVINO GPU plugin** still needs the system `intel-opencl-icd`/level-zero — don't assume torch wheels cover OpenVINO.

**🔌 Hardware milestone #3 (A310):** build + run the Docker `intel` target on the Proxmox host end-to-end; repeat with a native Linux `setup.sh` build.

#### Phase 5 — Final docs consolidation

`architecture.md`: module-map entries for the OpenVINO backend + `torch_device_string`; a "Design Decisions" entry (engine choice, device-token mapping, warn-and-fallback); **Known Constraints** rows (Intel transcription requires OpenVINO not CT2; XPU diarization op gaps → CPU fallback; Windows validated-by-proxy). `README.md`: add Intel to the accelerator list if the quickstart mentions GPUs. Remove these Intel entries from `plan.md` as phases complete.

### Complete device-touchpoint inventory (so nothing is missed)

Representative branch points (most handled via the `auto`-resolve + `torch_device_string` helper, so few need bespoke logic):

- **Choice lists:** cli.py:29, web/routes/config.py:28 — add `"intel"`.
- **Detection/resolve:** config.py:148 (`get_device`), pipeline.py:375 & pipeline.py:594 (`auto`→`get_device`, already generic), transcriber.py:167.
- **Backend dispatch:** transcriber.py:171 (add intel branch beside MLX).
- **torch `.to(device)`:** diarizer.py:120, speaker_manager.py:118-120 — route through `torch_device_string`.
- **Validation blocks:** diarizer.py:108-119, transcriber.py:120-128 — add intel/xpu checks.
- **Parallel-workers guard** pipeline.py:596 (`workers>1 and device!="cpu"` → clamp to 1): **no change** — `intel` is non-cpu so correctly clamped to a single worker, like cuda.
- **MLX gates** pipeline.py:410, transcriber.py:171: stay `mps`-only — **no change**.

### Testing strategy (CI stays GPU-free)

All ML mocked, per CLAUDE.md. New seams are patchable like the existing ones:

- `_is_openvino_available()` and the optimum/HF pipeline factory are the mock points for transcription (parallel to `WhisperModel`).
- `torch.xpu.is_available()` is patched for detection + diarization device tests.
- Fallback paths tested by making the mocked pipeline call raise once, then asserting the warning + CPU retry.
- New/extended files: test_transcriber.py, test_diarizer.py, test_speaker_manager.py, test_config.py, test_cli.py, plus test_web_routes.py for the device choice.

**Hardware validation runbook** (A310, outside CI — Linux native or Docker `intel`):
1. `wisper setup` reports `Intel Arc GPU (XPU/OpenVINO)`.
2. `wisper transcribe sample.mp3 --device intel` → transcript; first run converts/caches IR; GPU utilized (`intel_gpu_top`).
3. `--device auto` selects intel automatically.
4. Force an XPU diarization op gap → loud warning + CPU completion (transcription still on GPU).
5. Docker `make start-intel` → web UI transcribes a file end-to-end with `/dev/dri` passthrough.

### Risks & open items

- **R1 — Intel compute-runtime packaging** (Docker apt names / Proxmox passthrough). Highest-risk; resolved empirically at Phase 4 hardware bring-up.
- **R2 — `generate_kwargs` prompt key** for OpenVINO transcription varies by transformers version (Phase 2 hardware check).
- **R3 — IPEX vs native `torch.xpu`** op coverage for pyannote; warn-and-fallback (Phase 3) is the safety net by design.
- **R4 — Windows-native is validated-by-proxy** (no A310 on the Windows box) — documented, not claimed as proven.

---

## Job cancellation — best-effort GPU stop

Stopping an in-flight transcribe job marks it Failed, but the GPU keeps running until the current CTranslate2 batch finishes.

**Why cancellation is cooperative-only:**
- `cancel_event.is_set()` is checked only inside `capturing_write()` and `ProgressCatcher.write()`, which fire when tqdm emits output.
- Between tqdm ticks the worker is inside CTranslate2's C++ code, which has no Python yield points or cancel hook.
- `pipeline.py` has no awareness of the job's cancel event.

**Options:**
1. **Run transcription in a subprocess and terminate it on cancel.** `parallel_stages = true` already does this for concurrent transcribe + diarize. Generalizing it costs ~1–2 s of startup per job but releases the GPU cleanly.
2. **Check the cancel event between segments in `pipeline.process_file()`.** Cheaper, but doesn't help mid-batch.
3. **Document cancel as best-effort** and add a force-quit button that terminates at the OS level.

**Recommendation:** option 1, reusing the parallel-stages subprocess plumbing. Deferred until cancellation is used often enough to justify it.

---

## DAVE sidecar → Python migration (parked)

The Java sidecar (JDA 6.3.0 + JDAVE 0.1.8) receives and decrypts DAVE-encrypted audio end-to-end. DAVE is mandatory for non-stage voice, so the only question is where it's implemented. DAVE is MLS over OpenMLS and every path depends on a native (Rust/JNI) binding; the choice is which language wraps it.

**Python readiness (as of 2026-06):**
- **pycord PR #3159** — DAVE receive for pycord, which has native voice receive. Approved but still a draft, milestoned for 2.9.0rc1. The right target once released.
- **discord.py PR #10300** — shipped in 2.7.x but flagged tentative. discord.py has no first-class voice receive, so it doesn't fit a recording bot.
- **`davey`** — the OpenMLS binding both use; beta (v0.1.5) with no usage docs.

**Verdict:** keep the sidecar. Revisit when pycord 2.9 ships #3159 as stable.

**Migration path:**
1. Delete `discord-bot/` (the Gradle/Java project).
2. Write a ~100-line Python replacement that emits the same wire format over the existing Unix socket: length-prefixed user_id + 48 kHz stereo PCM, including the pre-mixed `__mixed__` stream.
3. Point `BotManager` at the Python script instead of the JAR.
4. Remove the Java builder stages from `Dockerfile` and the Java 25 requirement from launchers and docs.

Nothing else changes; the wire protocol is the stable interface.

**Fallback if the native-binding ecosystem stalls:** skip DAVE entirely — run a real Discord client in the channel and capture its decrypted output through a loopback device. Heavier to operate and loses per-speaker separation; kept only as an escape hatch.

---

## Campaign-level LLM summaries (DM tools)

The rolling campaign journal sets the pattern: slug-scoped storage under `campaigns/<slug>/`, `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

**Build on the database:** the transcript registry and `journal_entries`, not stem lists or frontmatter. Combined-summary and recap outputs get their own table with FKs to the campaign (and the sessions they cover), so deletes cascade; add it as a new migration and extend `test_schema.py`. The search index could cover them too (a new `search_index_state.kind`).

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
