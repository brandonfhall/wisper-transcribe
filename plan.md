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
- **GPU Docker image on an NVIDIA host.** The "Docker Build" workflow is disabled on GitHub, and the GPU image can only be built (not run) without NVIDIA hardware. Confirm a diarized job on the GPU image downloads the alignment model into `./cache/` and logs "Aligned words".
- **SQLite storage in a browser and on real capture** (automated coverage: `test_e2e.py`, `test_schema.py`). With `WISPER_DATA_DIR` pointing at a copy of real data:
  - Journal: the stale-journal notice (move a folded session to another campaign) on the Campaign and Journal pages; **Rebuild journal** vs **Rebuild from transcripts** confirmations and their call counts; journal **Download** includes `journaled_sessions`.
  - Job history page, filters, paging, and a historical job's page after a restart.
  - Recording end to end: record a few minutes, local and Discord. Add markers, including two in quick succession, and edit the notes mid-session, then stop. After stop, only `combined.wav` and (local) `live_transcript.md` remain; Discord also keeps unbound users' `per-user/<uid>/`. Transcribe it, play it back, and jump to a marker.
  - Search: edit a transcript in Obsidian while the server runs and search for the new words (the result shows "changed — reindexing", then matches after a reload).
- **macOS loopback.** Record page on a Mac with BlackHole installed: BlackHole appears under System Audio and captures audio.

---

## Dependency pins

- **Drop `av<19`** (pyproject) once a faster-whisper release stops passing `metadata_errors=` to `av.open()`; check with a CPU Docker build and one transcription.

---

## Storage — open

- **Store `combined.wav` as FLAC** (about half the size)? It touches the fixed `recordings/<id>/combined.wav` layout and every reader of it.
- **Prune old `backups/` snapshots:** planned in "Campaign folders" Phase 8.
- **Minor:** with an unfrozen schema and `WISPER_OUTPUT_DIR` unset, a CLI command creates the configured output folder (empty) before the dev guard refuses (`path_utils.get_output_dir` mkdir). The server path creates nothing.

---

## Campaign folders — transcripts in a folder per campaign (`feat/campaign-folders`)

**Goal:** a transcript in a campaign lives in `<output root>/<campaign folder>/`, with all its files and the campaign's journal. A transcript in no campaign stays in the output root.
- **Moves:** moving a transcript to another campaign moves its files.
- **Renames:** renaming a campaign renames its folder.
- **Names:** the same session name may exist in two campaigns.
- **Obsidian:** the output root can be an Obsidian vault, and each campaign reads as one folder of sessions plus its journal.

### Status

| Phase | State | Commit |
|---|---|---|
| Planning: gate review, design, reviewer cycles | in progress | |
| 1 — Schema v11; campaign queries; journal into the campaign folder | not started | |
| 2 — One location lookup (`transcript_store.locate`) | not started | |
| 3 — Transcript URLs by id; lists from the database | not started | |
| 4 — Scan campaign folders: reconcile, sync, Needs attention | not started | |
| 5 — Write into campaign folders: uploads, recordings, re-transcribe, CLI | not started | |
| 6 — Moving a transcript moves its files; clash prompt; Rename | not started | |
| 7 — Campaign create, rename, and delete with folders | not started | |
| 8 — `wisper storage trim` organizes folders; prune backups | not started | |
| 9 — Final review and rehearsals (orchestrator) | not started | |

### How to run this plan

- **Roles:**
  - An Opus orchestrator hands one phase at a time to a Sonnet worker.
  - It then reviews the diff against that phase's **Done when** and the documentation standard, runs the phase's tests, rehearses (below), and commits.
  - A worker never starts the next phase.
  - Review fixes go back to the **same** worker (SendMessage), not a fresh one.
- **Branch and commits:**
  - Branch `feat/campaign-folders`, one commit per phase. Push after each commit, and update this section's Status table and phase notes in the same commit.
  - Pause for Brandon's review between phases unless he waives it for the run.
  - Don't open a PR until Phase 9 passes and Brandon approves.
- **Worker tool calls stay small:**
  - Edits are short; one Write is at most ~60 lines; a test file grows in several Edits.
  - Long writes have stalled workers on the watchdog.
- **Tests:**
  - Never call `monkeypatch.undo()`. It drops the autouse `WISPER_DATA_DIR` fixture, and the test then hits the real data dir.
  - While iterating, run only the phase's test files; the pre-commit hook runs the full suite.
- **Busy machine:** don't commit or run the full suite while Brandon has transcription jobs running; ask first.
- **Read before starting:** `CLAUDE.md`, this plan's Findings, Decisions, and Schema v11 sections, and the phase's **Read first** list. For a phase that adds or changes a web route, also `.claude/rules/web-security.md`.
- **Data safety:**
  - Never run anything against the real data dir **or the real transcripts folder**.
  - Manual checks use a scratch copy and always set **both** `WISPER_DATA_DIR=<copy>/data` and `WISPER_OUTPUT_DIR=<copy>/output`. A copied `config.toml` still names Brandon's real `output_dir`.
- **Unfrozen schema:**
  - Phase 1 adds migration v11 and sets `db.SCHEMA_FROZEN = False` until Phase 9.
  - With it `False`, a branch build refuses the default data dir and refuses to start without `WISPER_OUTPUT_DIR`.
  - While the Mac repo is on this branch, Brandon's everyday wisper there won't open his real data; he switches to `main` to use it.
- **Rehearsals (orchestrator, every phase):**
  - Run the phase on a fresh scratch copy of Brandon's Mac data dir and output folder: `TMPDIR` pointed at scratch, `--port 8090`, and only while his queue is idle. Startup cleanup sweeps the temp dir.
  - Click through the pages the phase changed.
  - Docker checks use a *separate* copy with `data`, `output`, and `recordings` all mounted from it, never the repo's `./data`.
- **Concurrency scope:**
  - wisper is single-user. Design only for one web server plus one CLI command on the same machine.
  - The Docker Desktop host/container case is already refused by the runtime lease.
- **Database code convention (unchanged from `main`):**
  - Every function that touches the database takes `conn: Optional[sqlite3.Connection] = None`. With one, it runs in the caller's transaction; without one, it opens its own `db.transaction()`.
  - **Never open a second connection or transaction while this thread holds one; pass `conn`.**
  - Functions that move or delete *files* take no `conn`: files change only after the owner's transaction commits ("files follow rows").
- **Windows can't be reproduced on macOS:** `os.replace`, `os.rename`, and `unlink` fail on a file or folder another program holds open (Obsidian, Explorer, a player, the search indexer).
  - Every move and rename catches `PermissionError`/`OSError`, reports it, and leaves the database matching the disk.
  - Test it by monkeypatching `os.replace`/`os.rename` to raise `PermissionError`.
- **CI's `windows storage` job** (`.github/workflows/ci.yml`, the `pytest tests/test_db.py …` line) runs a fixed list of test files on Windows. Add every new test file that moves, renames, or deletes files.
- **Unicode and case:**
  - macOS may store names in NFD: compare NFC.
  - Case-only tests patch `transcript_store._is_case_insensitive` and `file_registry._fold` both ways.
- **Test rules:**
  - No GPU, network, or real audio.
  - Seed with `tests/_seed.py`. Phase 1 adds `campaign_id` to `seed_transcript`/`seed_campaign` helpers as needed.
  - Patch lazily imported functions on their source module.
- **Existing tests:** each phase lists the existing tests it must rewrite. A test outside that list that starts failing means a step was misread: stop and report.
- **Line numbers** are from `main` at `feabab7`. Find each function by name if they've drifted.
  - `static/` means `src/wisper_transcribe/static/`; templates are `src/wisper_transcribe/web/templates/`.
- **When the code disagrees with this plan,** stop and report to the orchestrator. Don't improvise a different design.

### Documentation standard (every phase; the orchestrator rejects a phase that breaks it)

1. **State current facts and the reason, never history.**
   - Banned in docs, docstrings, and comments: "now", "no longer", "used to", "previously", "changed to", "instead of the old …", "new:", dates, phase numbers, finding numbers, "campaign folders" as a project name.
   - History goes in the commit message. `plan.md` is the exception, and its content is removed when the work ships.
2. **One fact per bullet or sentence.** Comments are one line of *why*, never a restatement of the code.
3. **Edit the doc where the fact already lives.** Don't append a "Campaign folder changes" section.
4. **Remove every statement the phase makes false.** Each phase lists known stale lines; also `grep` for the old behaviour's key words: "output root" next to "flat", `campaigns/<slug>/journal.md`, `/transcripts/{name}`, `campaign_transcripts`.
5. **`README.md`** changes only its description paragraph, quick start, or docs table.
6. **Scar-tissue check before each commit:**
   ```bash
   git diff -U0 -- . ':(exclude)plan.md' | grep -E '^\+' \
     | grep -niE "\b(now|no longer|used to|previously|changed to|instead of the old|phase [0-9]|20[0-9]{2}-[0-9]{2})\b"
   ```
   Each hit is rewritten, unless it's clearly another sense of the word (e.g. "now" meaning the current time).

### Findings (verified on `main` at `feabab7`, 2026-10-04)

- **Every location is derived from a name.** The `files` registry stores each file's path, but nearly every reader rebuilds `<output root>/<stem><suffix>` instead:
  - `transcript_store.safe_path` (185) has 17 callers;
  - 28 lookups go from a stem to a row (`file_registry.Owner.for_stem` at 157, `WHERE stem = ?`);
  - 11 scans read only the output root's top level:
    - `_transcript_files` (439), the `reconcile` sweep (~576), `rename_companions`' `scandir` (~691), and `_companion_paths`' excerpt glob (~365);
    - `file_registry._scan_output` (648) and `storage_trim._find_orphans` (~139);
    - `legacy_import` (~288);
    - `cli.transcripts_list` (~1299), and `web/routes/transcripts.py` `recent` (~227) and the list (~274);
    - `dashboard.py` (~87).
  - Checks that a path is "directly in the output root":
    - `pipeline._under_output_root` (716);
    - `search_index.reindex_path` (~215);
    - `recording_manager._transcript_id` (272);
    - `record._purge_recording_files` (~548);
    - `transcript_store.delete_unowned_file` (841, rejects any separator);
    - `web/routes/campaigns.py` `_transcript_missing` and the `summarized` count (~89–104).
- **Transcript web routes take a name:** `/transcripts/{name}/…`, 19 routes in `web/routes/transcripts.py` from line 457. They resolve it with `_get_safe_content_path(name, suffix)` (193), a flat-root path guard.
  - Twelve templates build `/transcripts/{{ stem }}` links: `index`, `job_detail`, `recording_detail`, `transcript_detail`, `search`, `campaigns`, `job_history_detail`, `transcripts`, `summary_detail`, `transcript_edit`, `partials/job_rows`, `partials/recent_transcripts`.
  - Tests reference these URLs about 180 times, 94 of them in `tests/test_web_routes.py`.
- **There is no campaign rename and no transcript rename.**
  - Campaigns have create, delete, and members only.
  - Transcripts have only *relink* (`transcript_store.relink`, 723), which adopts a file someone renamed on disk.
  - `campaign_manager._make_slug` (40) derives the slug from the display name at create time.
- **The upload clash prompt exists.** `GET /transcribe/name-check` (`transcribe.py` ~217) returns `{exists, campaign, modified, missing, clashes}`, and the page offers Overwrite or Cancel (`transcribe.html` ~66–86). It checks only the output root (`_name_clashes`, ~186).
- **Where a new transcript is written:**
  - **Web upload:** the route passes `output_dir=get_output_dir()` (`transcribe.py` ~129).
  - **`process_file`** (`pipeline.py` 382):
    - writes `out_dir/<output_stem>.md` (481–484), calls `register(stem)` only when `_under_output_root` (683);
    - then calls `move_transcript_to_campaign(stem, campaign)` (688).
  - **Recording hand-off** (`record._submit_recording_transcription`, ~958):
    - writes to the output root;
    - passes `campaign=recording.campaign_slug`, which is the transcript's current campaign once one exists;
    - names a re-transcribe after the transcript's current stem.
  - **CLI `wisper transcribe`:**
    - writes next to the input file unless `-o` is given;
    - `--campaign` only narrows speaker matching to that campaign's roster (`cli.py` 92).
- **Pending and running jobs are rows in `jobs`:** `_record_history` runs at submit and on each status change (`jobs.py` 68, 562), and `job_history.mark_interrupted` (132) fails leftovers at startup.
  - A pending upload's campaign is only in `params_json` (`$.campaign` = slug).
  - Journal and relabel jobs set `jobs.campaign_id`.
  - Queued journal and relabel jobs carry the slug in `job.kwargs["slug"]`.
- **Campaign slugs are stored only in `campaigns`.** Discord presets (`config.discord_presets`) hold guild and channel ids only. The CLI and the routes take slugs as arguments.
- **The journal** is `<data>/campaigns/<slug>/journal.md` (`journal.journal_path`, 109), with an in-progress `journal.md.wisper-pending` beside it (`PENDING_SUFFIX`, 62).
  - `sync_journal` (177) resets the journal's entries when the file is gone.
  - The `files` CHECKs (`db.py` v9) pin it to the data root (`rel_path GLOB 'campaigns/*/journal.md'`, `(root = 'output') = (transcript_id IS NOT NULL)`).
- **Any `.md` in the output root is a transcript.** There's no wisper marker check: reconcile registers every `*.md` that isn't `*.summary.md` or a temp file. Campaign folders inherit this rule (see Decisions).
- **Within one campaign, session names stay unique**, so campaign-scoped APIs can keep using stems:
  - `Campaign.transcripts` (`models.py` 78, a list of stems);
  - `get_transcripts_for_campaign`, `journal.journaled_stems`, `unjournalled_sessions`, the reorder and remove routes.
  - Only stem → transcript lookups across campaigns become ambiguous.
- **`campaign_transcripts` is read or written in** `campaign_manager` (17 places, including `_write_order` 82 and `_stems` 125), `journal` (254, 420), `search_index` (581, 628), `job_history` (182), `recording_manager._load` (127), `transcript_store` (758, 780, 831, 913, 1028), and `legacy_import` (5).
  - `legacy_import` runs only inside migrations v2–v5, when the table still exists, so it keeps its queries.
- **`migrate()` already runs with `foreign_keys=OFF`** (`db.py` ~766) and ends with `PRAGMA foreign_key_check`.
  - `expected_schema()` (1220) replays only each migration's DDL, so every structural change goes in the DDL string.
  - `db.schema_version()` and the startup upgrade notice (`web/app.py`, PR #69) report the v11 upgrade without changes.
- **v11 prototype (scratch copy of the Mac `wisper.db`, SQLite 3.53.4, every child table seeded):**
  - **Row counts:** transcript_speakers, search_index_state, search_blocks, jobs, recordings, journal_entries, campaign_members, profiles, recording_segments, recording_markers, non-journal `files` rows, transcripts, placements, and FTS matches were all unchanged. `integrity_check` was ok and `foreign_key_check` was empty.
  - **Trigger pitfall:** dropping and renaming `campaigns` fails with `error in trigger journal_entries_ad: no such table: main.campaigns`. The migration therefore drops every trigger it recreates **first**.
  - **Behaviour confirmed:**
    - same stem in two campaigns ok, twice in one campaign or twice in the root refused;
    - a campaign move deletes the journal entry and marks the journal stale, while a reorder keeps it;
    - deleting a campaign that still holds transcripts is refused;
    - a deleted id is never reused;
    - the folder CHECKs and NOCASE uniqueness hold;
    - `files` rows deeper than one folder, and journals outside a campaign folder, are refused;
    - the folder-prefix rewrite works on names containing `[`, `*`, and `?`.
  - Script: `run.py` beside `v11.sql` in that session's scratchpad. The DDL is reproduced in "Schema v11" below.
- **Real data:**
  - Mac: 1 campaign and 4 transcripts, all assigned, no journal file, and no subfolders in the output root.
  - Windows: at v10 and migrated (Brandon, 2026-10-04). Its counts are needed for the Phase 9 rehearsal.

### Decisions

Brandon's answers are dated 2026-10-04 unless noted. Rows marked *default* are the architect's call, open to Brandon's veto.

| Decision | Why |
|---|---|
| A transcript in a campaign lives in `<output root>/<campaign folder>/`; one in no campaign lives in the root. Folders are one level deep. | Brandon, 2026-10-03. One level keeps scanning cheap and the `files` CHECK simple. |
| **Where a transcript is** comes from its `transcript` row in `files` (`rel_path`). Where it **should be** comes from `transcripts.campaign_id`: `campaigns.folder`, or the root. A difference is a *misplaced* transcript: it still works everywhere, and Needs attention and `storage trim` offer to move it. | One source of truth for location. A partial move (a file locked on Windows) leaves a consistent state instead of a broken one, and the registry already follows renames. |
| Companion files stay next to their `.md` in the campaign folder; there is no hidden `.wisper/` subfolder. | Brandon. |
| The campaign journal lives in the campaign folder as `<folder> Journal.md`. Planned campaign-level outputs (combined summary, recap) go there too. | Brandon: keep it with the campaign. A per-campaign file name keeps Obsidian `[[links]]` unambiguous across campaigns (*default*). |
| A session can't be named `<folder> Journal` inside that campaign. | It would collide with the journal. |
| The same transcript name may exist in two campaigns. Within one folder, names are unique **ignoring case** (casefold, NFC). The database enforces exact uniqueness per campaign; the app adds the case-insensitive check for new names. | Windows and macOS folders ignore case. Existing Linux data may already hold case-only twins in the root, and a migration must not fail on them. |
| A campaign's folder name is its display name, made safe:<br>• forbidden characters and control characters → a space;<br>• runs of spaces collapsed;<br>• leading dots and trailing dots or spaces trimmed;<br>• a Windows reserved name (CON, PRN, AUX, NUL, COM1–9, LPT1–9, also before a dot) gets a trailing `_`;<br>• at most 80 characters;<br>• `Campaign` if nothing is left.<br>Two campaigns whose names map to one folder get ` (2)`, ` (3)`. | Windows rules. 80 characters leave room under MAX_PATH (260) for a long session name. |
| Renaming a campaign renames it everywhere: display name, slug, and folder, and the journal file with the folder. Old `/campaigns/<old-slug>` URLs stop working. | Brandon. |
| Creating or renaming a campaign is refused when its folder name matches (ignoring case) an existing folder in the output root that isn't that campaign's. wisper never adopts or renames a folder it didn't create. | The output root can be the user's vault. |
| A campaign folder rename runs in two steps: the display name and slug change at once, the folder changes on disk, then `campaigns.folder` and every path under it. `campaigns.folder_pending` records the target, so a crash or a locked folder leaves a state that startup finishes, or that Needs attention retries. | On Windows a folder can't be renamed while any file in it is open. |
| Transcript URLs use the row id: `/transcripts/{id}/…`. An old `/transcripts/{name}` URL redirects when exactly one transcript has that name (*default*). | Names aren't unique across campaigns. An integer path parameter also removes the path-traversal surface from 19 routes. |
| Moving a transcript to another campaign, or out of all of them, moves its `.md` and every registered file (summary, sidecar, excerpts, audio, backup) into the target folder. The database changes first, then the files follow. A file that can't move (locked) stays, and the transcript reads as misplaced. | "Files follow rows". The registry keeps a partial move consistent. |
| A move, rename, or upload that would land on an existing name prompts **Overwrite**, **Keep both** (saved as `<name> (2)`), or **Cancel**, showing the existing file's last-modified time. Overwrite is offered only when the existing file is a wisper transcript (it has a `files` row); otherwise only Keep both or Cancel. A bulk move doesn't prompt: a clashing transcript stays where it is and is listed in the result (*default*). | Brandon, 2026-10-03 (the prompt). wisper never overwrites a file it doesn't own. |
| wisper gets a **Rename** action for transcripts (web and CLI). It renames the `.md` and every companion together, with the same clash prompt. | Brandon, 2026-10-03 (*default*: in scope). |
| A move, rename, or campaign rename is refused while a job is pending or running for that transcript or campaign. | A job writes to a path decided at submit. |
| **Deleting a campaign:**<br>• **Delete everything** deletes its transcripts, their files, and the journal, then removes the folder only if it's empty.<br>• **Keep the files** moves its transcripts to the output root (a clash becomes `<name> (2)` and is reported). The journal and the folder stay on disk, untracked.<br>(*default*) | `transcripts.campaign_id` refuses deleting a campaign that still holds transcripts. wisper never deletes a folder holding files it doesn't own. |
| A new transcript's campaign is the folder it's written into. An upload chosen for a campaign is written into that campaign's folder. | One rule for the web, recordings, the CLI, and files that appear on disk. |
| A `.md` that appears in a campaign folder (copied there or dropped in Obsidian) becomes a transcript of that campaign, exactly as a `.md` in the root becomes an unassigned one. A transcript dragged from one folder to another (matched by size and mtime, as renames are) changes campaign, and its companions follow. | Folders mean campaigns. Brandon keeps personal notes outside the output folder (2026-10-04), so no wisper-marker check is needed. |
| `wisper transcribe --campaign X` without `-o` writes into X's folder and adds the transcript to X. With `-o`, behaviour is unchanged: roster-only `--campaign`, and outside the output root the file isn't registered (*default*). | Brandon asked that CLI runs land in the right folder. |
| Existing transcripts move into their campaign folders through `wisper storage trim` (dry run, then `--apply`), as one more action. The migration moves no files. Until then they're misplaced, and everything still works. | Brandon: fold it into storage trim, no new command. Migrations are frozen and must not move user files. |
| A journal still at `<data>/campaigns/<slug>/journal.md` moves into the campaign folder the first time its campaign's journal is read. That happens on any page or command that touches the journal, and at startup. | No journal is ever reset by a missing-file check before it's moved. |
| v11 keeps the last 5 `backups/wisper-v*.db` snapshots after a migration (*default*). | Each migration adds a snapshot; Brandon agreed to fold this in. |
| Out of scope: storing `combined.wav` as FLAC; the Ollama empty-response bug. | Brandon. |

### Schema v11

One migration, `Migration(11, "campaign-folders", _V11_DDL, _v11_import)`. The DDL below ran clean on the prototype (see Findings).

**Python string:** `_V11_DDL` is a normal (non-raw) string like `_V9_DDL`, so every SQL `\` is written `\\` in `db.py`. For example, SQL `'*[/\]*'` is Python `'*[/\\]*'`. The test `test_schema.py::test_v11_ddl_matches_plan` compares a few CHECK texts from `sqlite_master` to catch a lost backslash.

**What it does, in order:**
1. **Drop the triggers it recreates.** Otherwise a rename re-parses a trigger that names a dropped table and fails.
2. **Rebuild `campaigns`** with `folder` (stored, unique ignoring ASCII case) and `folder_pending` (a folder rename in progress). `folder` is filled with a placeholder (`'#' || id`) in DDL, and with the real name by `_v11_import`.
3. **Rebuild `transcripts`:**
   - it gains `campaign_id` and `position` (copied from `campaign_transcripts`);
   - `id` becomes `AUTOINCREMENT`, so a deleted id is never reused in a URL;
   - stem uniqueness becomes per campaign (`transcripts_stem` on `(coalesce(campaign_id, 0), stem)`).
4. **Rebuild `journal_entries`** so its composite FK targets `transcripts(campaign_id, id)`, then drop `campaign_transcripts`.
5. **Recreate the triggers** and rebuild the title index. A new `transcripts_campaign_bu` deletes a moved transcript's journal entry, and `journal_entries_ad` then marks the old campaign's journal stale.
6. **Rebuild `files`:**
   - journals live under the output root, in a campaign folder;
   - output paths are at most one folder deep;
   - the old data-root journal rows are dropped (the file moves on first read; see Phase 1).

`campaigns.folder` and `transcripts.campaign_id` are separate facts: the folder is a stored name, and the campaign is the assignment. The derived pair "this transcript's expected folder" is computed, never stored.

```sql
-- Triggers are recreated below; dropping them first keeps the renames from re-parsing them.
DROP TRIGGER journal_entries_ad;
DROP TRIGGER transcript_titles_ai;
DROP TRIGGER transcript_titles_ad;
DROP TRIGGER transcript_titles_au;
DROP TRIGGER files_profile_key_au;
-- v11 prototype: campaign folders.
CREATE TABLE campaigns_new (
  id             INTEGER PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE CHECK (slug <> ''),
  display_name   TEXT NOT NULL CHECK (display_name <> ''),
  folder         TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (folder <> '' AND length(folder) <= 80
                        AND folder NOT GLOB '*[/\:*?"<>|]*'
                        AND folder NOT GLOB '.*'
                        AND folder NOT GLOB '* ' AND folder NOT GLOB '*.'
                        AND folder NOT GLOB ' *'
                        AND NOT (folder GLOB '*[' || char(1) || '-' || char(31) || ']*')),
  folder_pending TEXT COLLATE NOCASE UNIQUE CHECK (folder_pending IS NULL OR (folder_pending <> '' AND length(folder_pending) <= 80
                        AND folder_pending NOT GLOB '*[/\:*?"<>|]*'
                        AND folder_pending NOT GLOB '.*'
                        AND folder_pending NOT GLOB '* ' AND folder_pending NOT GLOB '*.'
                        AND folder_pending NOT GLOB ' *'
                        AND NOT (folder_pending GLOB '*[' || char(1) || '-' || char(31) || ']*'))),
  created_at     TEXT NOT NULL,
  journal_sha256 TEXT CHECK (journal_sha256 IS NULL OR length(journal_sha256) = 64),
  journal_stale_since TEXT,
  CHECK (folder_pending IS NULL OR folder_pending <> folder COLLATE BINARY)
) STRICT;
INSERT INTO campaigns_new (id, slug, display_name, folder, created_at, journal_sha256, journal_stale_since)
  SELECT id, slug, display_name, '#' || id, created_at, journal_sha256, journal_stale_since FROM campaigns;
DROP TABLE campaigns;
ALTER TABLE campaigns_new RENAME TO campaigns;

CREATE TABLE transcripts_new (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stem          TEXT NOT NULL CHECK (stem <> '' AND stem NOT GLOB '*[/\]*'),
  campaign_id   INTEGER REFERENCES campaigns(id),
  position      INTEGER CHECK (position IS NULL OR position >= 0),
  created_at    TEXT NOT NULL,
  missing_since TEXT,
  CHECK ((campaign_id IS NULL) = (position IS NULL)),
  UNIQUE (campaign_id, position),
  UNIQUE (campaign_id, id)
) STRICT;
INSERT INTO transcripts_new (id, stem, campaign_id, position, created_at, missing_since)
  SELECT t.id, t.stem, ct.campaign_id, ct.position, t.created_at, t.missing_since
  FROM transcripts t LEFT JOIN campaign_transcripts ct ON ct.transcript_id = t.id;
CREATE TABLE journal_entries_new (
  transcript_id INTEGER PRIMARY KEY,
  campaign_id   INTEGER NOT NULL,
  folded_at     TEXT NOT NULL,
  FOREIGN KEY (campaign_id, transcript_id)
    REFERENCES transcripts(campaign_id, id) ON DELETE CASCADE
) STRICT;
INSERT INTO journal_entries_new SELECT transcript_id, campaign_id, folded_at FROM journal_entries;
DROP TABLE journal_entries;
DROP TABLE campaign_transcripts;
DROP TABLE transcripts;
ALTER TABLE transcripts_new RENAME TO transcripts;
ALTER TABLE journal_entries_new RENAME TO journal_entries;
CREATE UNIQUE INDEX transcripts_stem ON transcripts(coalesce(campaign_id, 0), stem);

CREATE TRIGGER transcript_titles_ai AFTER INSERT ON transcripts BEGIN
  INSERT INTO transcript_titles (rowid, stem) VALUES (new.id, new.stem);
END;
CREATE TRIGGER transcript_titles_ad AFTER DELETE ON transcripts BEGIN
  INSERT INTO transcript_titles (transcript_titles, rowid, stem) VALUES ('delete', old.id, old.stem);
END;
CREATE TRIGGER transcript_titles_au AFTER UPDATE OF stem ON transcripts BEGIN
  INSERT INTO transcript_titles (transcript_titles, rowid, stem) VALUES ('delete', old.id, old.stem);
  INSERT INTO transcript_titles (rowid, stem) VALUES (new.id, new.stem);
END;
INSERT INTO transcript_titles (transcript_titles) VALUES ('rebuild');

CREATE INDEX journal_entries_campaign ON journal_entries(campaign_id, transcript_id);
CREATE TRIGGER journal_entries_ad AFTER DELETE ON journal_entries BEGIN
  UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
   WHERE id = old.campaign_id;
END;
CREATE TRIGGER transcripts_campaign_bu BEFORE UPDATE OF campaign_id ON transcripts
  WHEN old.campaign_id IS NOT new.campaign_id BEGIN
  DELETE FROM journal_entries WHERE transcript_id = old.id;
END;

CREATE TABLE files_new (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('transcript', 'summary', 'sidecar', 'excerpt', 'excerpt_text', 'audio',
                                              'backup', 'combined', 'per_user', 'live_draft', 'reference_clip', 'journal')),
  root          TEXT NOT NULL CHECK (root IN ('output', 'data')),
  rel_path      TEXT NOT NULL CHECK (rel_path <> '' AND rel_path NOT GLOB '/*' AND rel_path NOT GLOB '*\*'
                                     AND rel_path NOT GLOB '[A-Za-z]:*' AND rel_path NOT GLOB '*/'
                                     AND rel_path NOT GLOB '*//*'
                                     AND '/' || rel_path || '/' NOT GLOB '*/../*'
                                     AND '/' || rel_path || '/' NOT GLOB '*/./*'),
  label         TEXT CHECK (label IS NULL OR (label <> '' AND label NOT GLOB '*[/\:]*')),
  transcript_id INTEGER REFERENCES transcripts(id) ON DELETE CASCADE,
  recording_id  TEXT    REFERENCES recordings(id)  ON DELETE CASCADE,
  profile_id    INTEGER REFERENCES profiles(id)    ON DELETE CASCADE,
  campaign_id   INTEGER REFERENCES campaigns(id)   ON DELETE CASCADE,
  size          INTEGER CHECK (size IS NULL OR size >= 0),
  mtime_ns      INTEGER,
  UNIQUE (root, rel_path),
  CHECK ((transcript_id IS NOT NULL) + (recording_id IS NOT NULL)
         + (profile_id IS NOT NULL) + (campaign_id IS NOT NULL) = 1),
  CHECK ((kind IN ('transcript', 'summary', 'sidecar', 'excerpt', 'excerpt_text', 'audio', 'backup'))
         = (transcript_id IS NOT NULL)),
  CHECK ((kind IN ('combined', 'per_user', 'live_draft')) = (recording_id IS NOT NULL)),
  CHECK ((kind = 'reference_clip') = (profile_id IS NOT NULL)),
  CHECK ((kind = 'journal') = (campaign_id IS NOT NULL)),
  CHECK ((root = 'output') = (kind IN ('transcript', 'summary', 'sidecar', 'excerpt', 'excerpt_text',
                                       'audio', 'backup', 'journal'))),
  CHECK (root <> 'output' OR rel_path NOT GLOB '*/*/*'),
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
  CHECK (kind <> 'journal'      OR (rel_path GLOB '?*/?* Journal.md' AND rel_path NOT GLOB '*/*/*'))
) STRICT;
INSERT INTO files_new SELECT * FROM files WHERE kind <> 'journal';
DROP TABLE files;
ALTER TABLE files_new RENAME TO files;
CREATE UNIQUE INDEX files_transcript ON files(transcript_id, kind, coalesce(label, ''));
CREATE UNIQUE INDEX files_recording  ON files(recording_id, kind, coalesce(label, ''));
CREATE UNIQUE INDEX files_profile    ON files(profile_id, kind);
CREATE UNIQUE INDEX files_campaign   ON files(campaign_id, kind);
CREATE TRIGGER files_profile_key_au AFTER UPDATE OF key ON profiles BEGIN
  UPDATE files SET rel_path = 'profiles/embeddings/' || new.key || '.mp3'
   WHERE profile_id = new.id AND kind = 'reference_clip';
END;
```

**`_v11_import(conn, ctx)`** (self-contained: it imports no application module, like `_v9_import`):
- For each campaign in `id` order: `folder = _v11_folder_name(display_name)`. While `folder.casefold()` is already taken by an earlier campaign, append ` (2)`, ` (3)`, … to the base, then `UPDATE campaigns SET folder = ?`.
- `_v11_folder_name` is a private copy of the sanitizer in Decisions, frozen in `db.py`. `campaign_folders.folder_name` (Phase 7) may evolve; v11's output may not.
- A campaign whose folder name had to change (sanitized or de-duplicated) gets a `ctx.note("campaign <slug>: folder <name>")`, so the import report says what was chosen.
- It touches no files.

**Why each constraint:**
- `UNIQUE (campaign_id, id)` is the journal's composite FK target. The journal can only hold a session of its own campaign.
- `CHECK ((campaign_id IS NULL) = (position IS NULL))` means a session in a campaign always has an order, and an unassigned one never does.
- `transcripts.campaign_id` has no ON DELETE action (restrict): a campaign is emptied before it's deleted (Decisions, campaign delete).
- `files.campaign_id ON DELETE CASCADE` stays: only `journal` rows have it, and a deleted campaign's journal file is left on disk untracked.
- `folder COLLATE NOCASE UNIQUE` catches ASCII case clashes in the database. Unicode case folding is checked in the app (`campaign_folders.check_available`), since SQLite's NOCASE folds ASCII only.
- `folder_pending <> folder COLLATE BINARY` lets a case-only rename (`hanataz` → `Hanataz`) be pending.
- `files` `rel_path NOT GLOB '*/*/*'` under the output root means one folder level only. It's simple to scan, and a stray deep path is a bug.

**Tests for v11 (`tests/test_schema.py`, `tests/test_db.py`):**
- **Upgrade:** a v10 database seeded through every child table (`transcript_speakers`, `search_index_state` + `search_blocks` + FTS, `jobs`, `recordings` linked to a transcript, `journal_entries`, `campaign_members`, `files` of every output kind plus a data-root journal) migrates to v11.
  - Every row count is unchanged, except the journal `files` row, which is dropped.
  - `campaign_transcripts` placements become `transcripts.campaign_id`/`position`.
  - `integrity_check` is ok, `foreign_key_check` is empty, and a title search still finds a transcript.
- **Folder names:** two campaigns named `Hanataz: Act I?` and `HANATAZ: act i?` get `Hanataz Act I` and `HANATAZ act i (2)`; `CON` gets `CON_`. The report notes both.
- **Constraints:** one test per prototype check (Findings, "Behaviour confirmed").
- **Schema drift:** `db.status()` reports no drift on a migrated database (`expected_schema(11)` replays the DDL).
- **Backups pruning** (Phase 8) has its own tests.

---

### Phase 1 — Schema v11; campaign queries; journal into the campaign folder

**Goal:**
- The database is at v11.
- Every query that used `campaign_transcripts` uses `transcripts.campaign_id`/`position`.
- A campaign's journal is `<output root>/<folder>/<folder> Journal.md`.
- Nothing else changes: transcripts are still written to, and found in, the output root.

**Read first:**
- `db.py`: the v9 and v10 sections, `MIGRATIONS` (703), `migrate()` (739), `_exec_ddl` (824), `expected_schema` (1220)
- `campaign_manager.py` (whole file); `models.py` `Campaign` (72); `tests/_seed.py` `save_campaigns` (135)
- `journal.py` 96–320 and `update_journal` (345, how it writes the pending file and replaces it); `file_registry.py` `KINDS`/`ROOT_OF_KIND`/`_OWNER_OF_KIND` (44–64), `_scan_data` (680), `_sync` (757)
- The `campaign_transcripts` users listed in Findings
- `web/app.py`: the startup lifespan (migrate, `mark_interrupted`, `reconcile(sweep=True)`)

**Steps:**
1. **Migration.**
   - Add `_V11_DDL` (Schema v11, verbatim, with `\` doubled), `_v11_folder_name`, `_v11_import`, and `Migration(11, "campaign-folders", _V11_DDL, _v11_import)`.
   - Set `SCHEMA_FROZEN = False`.
   - `_v11_folder_name(display_name: str) -> str` implements the sanitizer in Decisions exactly:
     - NFC first;
     - each of ``/ \ : * ? " < > |`` and U+0000–U+001F becomes a space;
     - whitespace runs collapse to one space;
     - strip; strip leading dots; strip trailing dots and spaces;
     - truncate to 80, then strip trailing dots and spaces again;
     - if the part before the first dot, uppercased, is a reserved device name, append `_`;
     - empty becomes `Campaign`.
2. **New module `campaign_folders.py`** (this phase: names only; Phase 7 adds the rest).
   - `folder_name(display_name) -> str`: same rules. It calls nothing in `db.py`, so the frozen copy stays separate.
   - `journal_name(folder) -> str`: `f"{folder} Journal.md"`.
   - `unique_folder(conn, name, *, exclude_id=None) -> str`: returns `name`, or `name (2)`, `(3)`, … until its casefold matches no other campaign's `folder` or `folder_pending` casefold.
   - New `tests/test_campaign_folders.py`.
3. **`Campaign` gains `id: int = 0` and `folder: str = ""`.** `load_campaigns` fills both.
4. **`campaign_manager` against `transcripts`:**
   - `_write_order(conn, cid, tids)`:
     - unassign rows of `cid` not in `tids` (`campaign_id = NULL, position = NULL`);
     - shift every remaining row of `cid` by `max(position) + 1 + len(tids)`;
     - then for each `(pos, tid)`: `UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?`.

     A transcript moving in from another campaign changes `campaign_id` in that update; `transcripts_campaign_bu` deletes its journal entry. Keep the docstring's two-step reason.
   - `_stems`, `_transcript_id`, `move_transcript_to_campaign`, `remove_transcript_from_campaign`, `reorder_campaign_transcript`, `set_campaign_transcript_order`, `get_campaign_for_transcript`, `get_transcripts_for_campaign`, and `load_campaigns` read and write `transcripts.campaign_id`/`position`. Appending uses `(SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?)`.
   - `create_campaign` sets `folder = unique_folder(conn, folder_name(display_name))`. It creates no directory yet (Phase 7).
   - `delete_campaign`, in the transaction that deletes the campaign, first runs `UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE campaign_id = ?`; the schema refuses the delete otherwise. Delete-everything still deletes the transcripts first (unchanged), so this only catches the ones it couldn't delete.
5. **Every other `campaign_transcripts` query** (Findings list, except `legacy_import`) joins `transcripts.campaign_id` instead.
   - `job_history._FROM`: `coalesce(j.campaign_id, t.campaign_id, rec.campaign_id, …)`.
   - `recording_manager._load`: join `transcripts t` before `campaigns c`, then `c.id = CASE WHEN r.transcript_id IS NOT NULL THEN t.campaign_id ELSE r.campaign_id END`.
   - `transcript_store.relink`'s "already linked" check: `campaign_id IS NOT NULL` on the new row, or a journal entry.
   - Update the comments that name `campaign_transcripts`: `campaigns.py` 264, 303; `job_history.py` 178; `transcript_store.py` 1028.
6. **Journal location** (`journal.py`):
   - `journal_path(slug, data_dir=None) -> Optional[Path]`: the campaign's `journal` row path if registered, else `get_output_dir() / folder / journal_name(folder)`. `JOURNAL_FILENAME` goes; `PENDING_SUFFIX` stays (the pending file sits beside the journal).
   - **`adopt_legacy_journal(slug, data_dir=None) -> None`:**
     - if `<data>/campaigns/<slug>/journal.md` exists and the new path doesn't: `mkdir(parents=True, exist_ok=True)` the campaign folder, `transcript_store._replace` the file, and register it (`add_if_owned`, kind `journal`, owner the campaign);
     - a legacy pending file is deleted (only a committed fold's file matters, and `sync_journal`'s hash check handles that);
     - remove `<data>/campaigns/<slug>/` if it's empty;
     - both files present: keep both, log a warning, change nothing;
     - an `OSError` is logged and leaves the legacy file in place.

     The journal reads as missing until the move succeeds, but **its entries are not reset** (next bullet).
   - `sync_journal` calls `adopt_legacy_journal` first. If the legacy file still exists after that (the move failed), it returns early **before** the "journal deleted → reset" branch.
   - `adopt_legacy_journals(data_dir=None)` runs it for every campaign, and `web/app.py` calls it at startup right after the migration, before `reconcile`.
   - `update_journal`: `jpath.parent.mkdir(parents=True, exist_ok=True)` before writing the pending file (keep it if it's already there).
   - `journaled_stems` (254) and the 420 query order by `t.position` from `transcripts`.
7. **`file_registry`:**
   - `ROOT_OF_KIND["journal"] = "output"`: build `ROOT_OF_KIND` from `_OUTPUT_KINDS | {"journal"}`. `_OWNER_OF_KIND["journal"]` stays `"campaign"`.
   - `_scan_data` drops the `campaigns/*/journal.md` scan.
   - `_sync` adds, for each campaign row `(id, folder)`: if `output / folder / journal_name(folder)` is a file, a `_Found("journal", path, "output", Owner("campaign", id))`.
8. **Seeds:** `tests/_seed.save_campaigns` already goes through `_write_order`. Add `seed_campaign(display_name, slug=None, data_dir=None) -> int`, a thin `create_campaign` wrapper returning the id, for later phases.

**Existing tests to rewrite** (find with `grep -n "campaign_transcripts\|journal.md\|get_campaigns_dir\|journal_path" tests/*.py`):
- `tests/test_schema.py`: the `campaign_transcripts` constraint cases (~32, 90, 107–115, 264, 293–301) become the same rules on `transcripts`.
- `tests/test_campaign_manager.py` ~578–594; `tests/test_e2e.py` ~146, ~182; `tests/test_journal.py` ~810.
- Journal-path tests in `test_journal.py`, `test_file_registry.py`, `test_web_routes.py`, `test_campaign_manager.py`, and `test_legacy_import.py` expect the journal under the campaign folder in the output root.
- `test_legacy_import`'s journal import runs at v3, so its *imported entries* are unchanged; only a path assertion moves.

**New tests:**
- All "Tests for v11" in Schema v11. The upgrade test builds a v10 database with `monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:10])`, as `test_upgrade_v8_to_latest_moves_audio_paths_into_files` does with `[:8]`.
- **`test_campaign_folders.py`:** the sanitizer table (each rule, plus `CON.txt` → `CON.txt_`, an 85-character name, NFD input), `unique_folder` with a case-only and a Unicode-case twin (`ÉTÉ` vs `été`), and `journal_name`.
- **Journal adoption:**
  - a legacy journal with two folded sessions moves on the first `unjournalled_sessions` call; its entries and hash survive, and the `files` row points at the new path;
  - with `_replace` patched to fail, the entries are **not** reset and the legacy file stays;
  - both files present: nothing moves.
- **Moving into another campaign** via `move_transcript_to_campaign` deletes the journal entry and marks the old campaign's journal stale; reorder doesn't.
- **`delete_campaign` keep-files** leaves its transcripts with `campaign_id IS NULL`.

**Docs:**
- `architecture.md`:
  - the schema section: v11 tables and the campaign assignment on `transcripts`;
  - the journal's location; the Module Map line for `campaign_folders`;
  - Known Constraints: `transcripts.campaign_id` restricts campaign delete.
- `docs/configuration.md`: the data storage table, where the journal moves from `campaigns/<slug>/journal.md` to the campaign folder.
- `docs/web-ui.md` and `docs/cli-reference.md`, wherever they name the journal's path.

**Done when:**
- `pytest tests/test_db.py tests/test_schema.py tests/test_campaign_manager.py tests/test_campaign_folders.py tests/test_journal.py tests/test_file_registry.py tests/test_e2e.py tests/test_legacy_import.py tests/test_job_history.py tests/test_recording_manager.py tests/test_search_index.py` pass.
- `grep -rn campaign_transcripts src/` shows only `db.py` (v2/v3 DDL, v11) and `legacy_import.py`.
- `tests/test_campaign_folders.py` is added to CI's `windows storage` list.
- **Rehearsal:** on a scratch copy, the Mac data migrates v10 → v11, `wisper db status` shows integrity ok and no drift, and the Campaigns and Journal pages render. Seed a legacy journal in the copy to see it move into `Impossible Landscapes/`.

---

### Phase 2 — One location lookup (`transcript_store.locate`)

**Goal:**
- Every reader finds a transcript and its files through one lookup, keyed by row id or by an existing `.md` path. Nothing outside `transcript_store` and `file_registry` builds `<output root>/<stem><suffix>`.
- All transcripts are still in the output root, so behaviour is unchanged; signatures move from bare stems to ids and paths.
- Web routes keep their `{name}` URLs until Phase 3.

**Read first:**
- `transcript_store.py`: `safe_path` (185), `ensure_row` (221), `register` (240), `_companion_paths` (308), `delete_transcript` (370), `rename_companions` (657), `relink` (723), `relink_candidates` (774), `read_sidecar` (884), `audio_path` (951), `set_audio` (991), `write_sidecar` (1022), `_register_summary` (151), `_refresh_transcript_row` (138)
- `file_registry.py`: `Owner.for_stem` (157), `_find_by_path` (236), `file_for` (415), `_fold`/`_key`
- Every call site in the Findings inventory (`safe_path`, `Owner.for_stem`, `WHERE stem = ?`, `_under_output_root`, `reindex_path`)

**Steps:**
1. **`transcript_store` location API** (new section "Locations"):
   ```python
   @dataclass(frozen=True)
   class Located:
       id: int
       stem: str                   # transcripts.stem (NFC)
       campaign_id: Optional[int]
       expected_dir: Path          # output root, or output root / campaigns.folder
       md: Path                    # its `transcript` files row; else expected_dir / f"{stem}.md"
       missing: bool               # missing_since IS NOT NULL
       @property
       def dir(self) -> Path: ...  # md.parent
       @property
       def misplaced(self) -> bool: ...   # normcase(realpath(dir)) != normcase(realpath(expected_dir))
       def companion(self, suffix: str) -> Path: ...   # safe_path(stem, suffix, dir); raise ValueError if None
   ```
   - `locate(transcript_id, conn=None, *, data_dir=None, output_dir=None) -> Optional[Located]`: one query joining `transcripts`, `campaigns`, and the `transcript` row of `files`.
   - `locate_path(md_path, conn=None, *, data_dir=None, output_dir=None) -> Optional[Located]`, for code that holds a path (jobs, the pipeline, CLI `fix`/`refine`/`summarize`):
     - first the `files` row with `root = 'output'` and `rel_path` = the path relative to the output root (NFC; casefolded when `file_registry._fold`);
     - else, when `dir_campaign(md_path.parent)` says it's a transcript folder, the row with that `campaign_id` and stem;
     - else `None`.
   - `dir_campaign(directory, conn=None, *, data_dir=None, output_dir=None) -> tuple[bool, Optional[int]]`:
     - `(True, None)` for the output root;
     - `(True, id)` for a campaign's `folder` or `folder_pending` directly under it;
     - `(False, None)` otherwise.

     It compares `os.path.normcase(os.path.realpath(...))`. This replaces `pipeline._under_output_root`, the parent check in `search_index.reindex_path`, `recording_manager._transcript_id`, and `record._purge_recording_files`.
   - `expected_dir(campaign_id, conn=None, *, output_dir=None) -> Path`.
   - `find_by_stem(stem, conn=None, *, campaign_id=..., data_dir=None) -> list[Located]`: matches NFC stem, then casefold when `_fold`. `campaign_id` is a keyword with a sentinel default, meaning "any campaign". It's for the legacy URL redirect (Phase 3) and CLI arguments.
2. **Keyed by id or path, not stem:**
   - `delete_transcript(transcript_id, data_dir=None, output_dir=None)`: companions via `_companion_paths(loc)`, which globs excerpts in `loc.dir`. `DELETE … WHERE id = ?`.
   - `register(md_path, *, origin, data_dir=None) -> int`:
     - the row for `(dir_campaign(md_path.parent), nfc(md_path.stem))`;
     - a new row gets that campaign and the next position (or none in the root);
     - `ensure_row(conn, stem, output_dir=None, *, campaign_id=None)` keys by `(campaign_id, stem)` the same way.
   - `rename_companions(transcript_id, old_stem, new_stem, *, src_dir, dst_dir=None, data_dir=None) -> list[Path]`:
     - scans `src_dir` for unregistered companions;
     - moves each file to `(dst_dir or src_dir) / (new_stem + tail)`;
     - `dst_dir` is for Phase 6, and a call with `old_stem == new_stem` and a `dst_dir` still moves.
   - `relink(old_id, new_md, data_dir=None) -> list[Path]` and `relink_candidates() -> list[Located]`.
   - `audio_path`, `set_audio`, `read_sidecar`, `write_sidecar`, `set_speaker_names`, `set_speaker_embeddings`, `_refresh_transcript_row`, `_register_summary`: keep their `md_path` parameter, but resolve the owner with `locate_path` instead of `Owner.for_stem` + `output_dir=md.parent`.
3. **`file_registry`:**
   - remove `Owner.for_stem`;
   - add `Owner.for_path(md_path, conn=None, *, data_dir=None, output_dir=None)`, a thin `locate_path` wrapper (import inside the function, since `transcript_store` imports `file_registry`).
4. **Callers** (each listed in the Findings inventory):
   - **`search_index`:** `reindex(transcript_id)`; `_paths(loc)`; `reindex_path` via `locate_path`; `check_freshness` builds `Located` from one joined query, not one call per row; `ResultGroup.transcript_id`.
   - **`journal`:** `_summary_path`/`_transcript_path` take `(campaign_id, stem)` → `locate` via `find_by_stem(stem, campaign_id=cid)`.
   - **`speaker_registry.relabel_campaign`:** paths from `find_by_stem(stem, campaign_id=cid)`.
   - **`job_history._subject_ids`:** `locate_path`.
   - **`recording_manager`:** `_transcript_id` via `register`/`locate_path`; `Recording.transcript_path` from `loc.md`; add `Recording.transcript_id: Optional[int]`.
   - **`record._purge_recording_files`:** `delete_transcript(recording.transcript_id)` when set, with no path check (a linked transcript is always wisper's).
   - **`web/jobs.py`:** `_keep_audio` and the excerpt registration (~399) via `locate_path(output_path)`; the backup at ~1686 via `Owner.for_path`.
   - **`storage_trim`:** `Action.transcript_id`; `_convert` targets `loc.companion(".flac")`; `apply` uses `loc.md`.
   - **`cli.py`:** journal rebuild's summary count (~1216) via `find_by_stem(…, campaign_id=…)`; backup registration (~1540, ~1635) via `Owner.for_path`.
   - **`campaign_manager`:** `move_transcript_to_campaign(transcript_id, slug)`, `remove_transcript_from_campaign(transcript_id)`, `get_campaign_for_transcript(transcript_id)`. The campaign-scoped functions (`get_transcripts_for_campaign`, `reorder_campaign_transcript(slug, stem, …)`) keep stems, which are unique within one campaign.
   - **`pipeline`:** `_under_output_root` and `_campaign_note` via `dir_campaign`/`locate_path`.
   - **Routes:** keep `_get_safe_content_path` for now, but call the new id/path APIs (resolve with `locate_path(md_path)`).
5. **Rule:**
   - `safe_path(stem, suffix, base_dir)` remains the name-validation guard for building a path *inside a known directory*.
   - The directory always comes from a `Located` (`loc.dir`) or `expected_dir`, never from `get_output_dir()` directly, except in the routes Phase 3 rewrites.

**Existing tests to rewrite:**
- Every test calling the changed signatures. Find them with `grep -n "delete_transcript(\|register(\|rename_companions(\|relink(\|for_stem(\|reindex(\|move_transcript_to_campaign(\|remove_transcript_from_campaign(\|get_campaign_for_transcript(" tests/*.py`.
- Each gets the id from a new seed helper, `tests/_seed.transcript_id(stem, *, campaign_slug=None, data_dir=None) -> int`.
- Expect many mechanical edits in `test_transcript_store.py`, `test_search_index.py`, `test_campaign_manager.py`, `test_web_jobs.py`, `test_storage_trim.py`, and `test_e2e.py`. A test that needs a *behaviour* change means a step was misread: stop.

**New tests (`tests/test_transcript_store.py`):**
- `locate` for a root and a campaign transcript;
- `misplaced` true for a campaign transcript whose `.md` is in the root (the state v11 leaves);
- `locate_path` by registered path, by folder + stem without a `files` row, NFD spelling, and case-folded;
- `dir_campaign` for the root, a campaign folder, a pending folder, an unrelated subfolder, and a path outside;
- `find_by_stem` with two campaigns holding the same stem;
- `delete_transcript(id)` deletes only that campaign's copy when two campaigns share a stem: seed both rows and files by hand (files in two folders), since writes into folders arrive in Phase 5.

**Docs:**
- `architecture.md`: the transcript_store section gets "Locations" (`Located`, `locate`, `locate_path`, `dir_campaign`, the misplaced state, and why the registry is the source of truth).
- Remove statements that companion names are derived from the stem in the output root.

**Done when:**
- The listed test files pass.
- `grep -rn "for_stem\|_under_output_root" src/` is empty.
- `grep -rn "get_output_dir()" src/wisper_transcribe` shows only:
  - `path_utils`, `db`, `config`, `job_history`'s `output_root` param;
  - `transcript_store`'s defaults, `file_registry._dirs`;
  - the route files (Phase 3), `cli.py`'s output-root defaults, `recording_manager`/`record` hand-off target (Phase 5).
- **Rehearsal:** every page renders on the scratch copy, and a transcript's audio plays (the `/audio` route).

---

### Phase 3 — Transcript URLs by id; lists from the database

**Goal:**
- Transcript pages are `/transcripts/{id}/…`. Their path parameter is an integer, so no transcript route builds a path from user input.
- The transcript lists (Transcripts page, recent partial, dashboard, `wisper transcripts list`) come from the database, not from `glob("*.md")`.
- An old `/transcripts/{name}` link redirects when the name is unique.

**Read first:**
- `web/routes/transcripts.py` (whole file), `web/routes/campaigns.py` 250–330 (remove and reorder by stem), `web/routes/search.py`, `web/routes/dashboard.py` ~80–100, `web/routes/record.py` (recording detail context)
- The twelve templates in Findings; `static/app.js` (any `/transcripts/` fetch)
- `job_history.JobRecord` (150–170), and the in-memory `Job.output_path`
- `.claude/rules/web-security.md`; `tests/test_path_traversal.py`

**Steps:**
1. **Routes** (`web/routes/transcripts.py`):
   - every `/{name}…` route becomes `/{transcript_id}…` with `transcript_id: int`;
   - `_located_or_redirect(transcript_id) -> Located | RedirectResponse`: a missing row redirects to `/transcripts?error=not_found`; a row flagged missing renders as today's missing state;
   - delete `_get_safe_content_path`;
   - the summary, sidecar, excerpt, and audio paths come from `loc.companion(...)` and the registry;
   - the excerpt route keeps validating `speaker_name` with today's guard, applied in `loc.dir`;
   - every `Location` header uses `loc.id` (an int).
2. **Legacy redirect.** `GET /transcripts/{name}` is declared **last** in the router, after every static and integer route:
   - `find_by_stem(nfc(name))` over present transcripts;
   - exactly one → 303 to `/transcripts/{loc.id}`;
   - several → a small page listing each with its campaign and link;
   - none → redirect to `/transcripts?error=not_found`.

   The name is used only as a bound SQL parameter. Starlette tries routes in order, so `/transcripts/12` hits the integer route. A transcript literally named `12` is reachable through the lists, not the legacy URL; that's accepted.
3. **Forms post ids:**
   - `bulk-delete` and `bulk-campaign` take `transcript_id` lists, parsed with `int()` and dropping bad values;
   - `relink` takes `old_id` and `new_id`;
   - the campaign page's remove and reorder take `transcript_id` (ints), replacing the stem guards at `campaigns.py` 264–330;
   - Needs attention's Forget keeps `files.id`.
4. **View models carry ids:**
   - `Campaign.transcript_ids: list[int]`, in the same order as `transcripts`;
   - the list items' `id`, `ResultGroup.transcript_id`, `JobRecord.transcript_id`, `Recording.transcript_id` (Phase 2);
   - the in-memory job page resolves `locate_path(job.output_path)` when it renders.

   Templates link `/transcripts/{{ x.id }}`; drop `| urlencode` on those links.
5. **Lists from the database:**
   - `transcript_store.list_transcripts(conn=None, *, campaign_id=..., data_dir=None, output_dir=None) -> list[Located]`: present transcripts ordered by the `transcript` row's `mtime_ns` (newest first), then `created_at`;
   - the Transcripts page, recent partial, dashboard, and `wisper transcripts list` use it, reading frontmatter from `loc.md`.

   The page keeps its campaign column and filter.
6. **Search results** link `/transcripts/{id}#b-N`, and show the campaign name when there is one.

**Existing tests to rewrite:**
- Every test that requests `/transcripts/<name>`: about 180 references, 94 in `test_web_routes.py`, plus `test_transcript_enroll.py`, `test_transcript_edit.py`, `test_search_routes.py`, `test_owasp.py`, `test_path_traversal.py`, and `test_e2e.py`.
- Use `_seed.transcript_id(stem)`, or read the id from the redirect after a create.
- The path-traversal cases for transcript routes become:
  - a non-integer segment on a POST route returns 404 or 405 and touches no file;
  - the legacy GET with each payload (null byte, `../`, CRLF) returns a redirect to `/transcripts?error=not_found` or the chooser, never a path.

**New tests:**
- **Legacy redirect:** unique name → 303 to the id; two campaigns with the same name → the chooser lists both; unknown → not_found.
- **Not found:** `/transcripts/999999` redirects with `error=not_found`.
- **Bulk delete:** with one valid and one bogus id, it deletes only the valid one.
- **Transcripts list order** follows the files' modified times.
- **Search hit links** use ids.

**Docs:**
- `docs/web-ui.md`: transcript links are by id, and old name links redirect.
- `architecture.md`: the web layer's route list, and the "URL-encode transcript stems" convention, which becomes ids.
- **`CLAUDE.md` Key Conventions:**
  - replace the row "URL-encode transcript stems in templates with `| urlencode`" with "Link transcripts by id (`/transcripts/{{ t.id }}`); stems aren't unique across campaigns";
  - keep `urlencode` for campaign slugs.
- `.claude/rules/web-security.md`: transcript routes take an integer id and resolve paths from the database; `safe_path` is for building a path inside a known directory.

**Done when:**
- The listed test files pass, plus `pytest tests/test_path_traversal.py tests/test_owasp.py`.
- `grep -rn "_get_safe_content_path" src/` is empty.
- `grep -rn 'transcripts/{{' src/wisper_transcribe/web/templates` shows only `.id` links.
- **Rehearsal:** click through the dashboard, the Transcripts list, a transcript, its summary, edit, the enroll wizard, search hits (the anchor scrolls), a job page, a recording page, and an old `/transcripts/<name>` URL.

---

### Phase 4 — Scan campaign folders: reconcile, sync, Needs attention

**Goal:**
- Reconcile and the file registry see the output root **and** each campaign's folder.
- A transcript dragged between folders outside wisper (Obsidian, Explorer) changes campaign, and its companions follow.
- Needs attention lists misplaced transcripts.
- No code writes into campaign folders yet: the tests create those files by hand.

**Read first:**
- `transcript_store.py`: `_transcript_files` (439), `_companion_stem` (449), `reconcile` (468–602), `_match_renamed` (604), `_point_transcript_row` (636), `needs_attention` (808), `delete_unowned_file` (841)
- `file_registry.py`: `_output_candidates` (622), `_scan_output` (648), `_sync` (757–846), `sync_if_due` (605)
- `web/routes/transcripts.py`: the Needs attention routes (`/needs-attention/forget`, `/needs-attention/delete-file`) and the panel in `templates/transcripts.html`
- `storage_trim._find_orphans` (~135)

**Steps:**
1. **`transcript_store.transcript_dirs(conn=None, *, data_dir=None, output_dir=None) -> list[tuple[Path, Optional[int]]]`:**
   - the output root with `None`;
   - then each campaign's `folder` and, when set, `folder_pending` that exists as a directory, with its id.

   Other subfolders are never scanned.
2. **`_transcript_files(dirs) -> dict[tuple[str, str], tuple[Path, Optional[int]]]`**, keyed by `(normcase(realpath(dir)), nfc(stem))`. It skips:
   - `*.summary.md`;
   - `TEMP_PREFIX` files;
   - in a campaign folder, the file named `journal_name(folder)` (compared casefolded).
3. **`reconcile` across folders.** Same rules as today, applied to `(dir, stem)` instead of `stem`:
   - **Present:** a row whose current location (its `transcript` files row, else `expected_dir`) has its `.md` is present; un-flag it if it was missing.
   - **Case-only rename:** the twin match applies within one directory only.
   - **Moved or renamed** (`_match_renamed`): an unregistered `.md` in any scanned dir whose `(size, mtime_ns)` uniquely matches one row with no `.md` on disk is that transcript. In one transaction:
     - `UPDATE transcripts SET stem = ?, campaign_id = ?, position = ?, missing_since = NULL` — the campaign and next position of the dir it was found in, or `NULL, NULL` in the root;
     - repoint its `transcript` row;
     - queue `rename_companions(tid, old_stem, new_stem, src_dir=old_dir, dst_dir=new_dir)`.

     A campaign change fires `transcripts_campaign_bu`, which un-journals the transcript and marks the journal stale.
     - **Clash:** the target campaign may already have a row with that stem (exact match). Catch `sqlite3.IntegrityError`, log it, and fall through to "new", which then also fails. That file is listed under Needs attention as **unclaimed**, never registered. It can't happen on disk (same folder, same name), so it's only the casefold edge on Linux.
   - **New:** `ensure_row(conn, stem, campaign_id=<dir's>)`, appended at the end of that campaign.
   - **Missing:** rows not found anywhere are flagged missing, as today.
   - **Sweep:** stale temp files are swept in every scanned dir.
4. **`file_registry.sync`:**
   - `_scan_output` scans every dir from `transcript_dirs`.
   - The owner of a companion named `<S><suffix>` in dir `D` is the transcript whose current `.md` is `D/<S>.md`. Build that map from the `transcript` rows' `rel_path` (dir + stem), falling back to `expected_dir` for rows without one.
   - The journal file in each campaign folder is found by name (Phase 1) and is never read as a transcript.
   - The "transcript row rel_path matches its stem" consistency check compares the basename only.
5. **Needs attention** (`Attention`) gains two fields:
   - `misplaced: list[Located]`: present transcripts with `loc.misplaced`;
   - `pending_folders: list[tuple[str, str, str]]`: `(display_name, folder, folder_pending)` for campaigns with a pending rename. Phase 7 creates them; listing them belongs here.

   `total` counts both.
   - **Panel, misplaced:** one grouped line, "N sessions aren't in their campaign's folder yet", with each name and campaign. The action text says "Run `wisper storage trim --apply`, or use Move files on the session's page" (that button arrives in Phase 6).
   - **Panel, pending renames:** each one, with its text and a Retry button that Phase 7 adds.
6. **Delete-file route and `delete_unowned_file`** accept one level:
   - `name` may be `"<file>"` or `"<folder>/<file>"`;
   - `<folder>` must equal a current campaign's `folder`;
   - each component passes the basename + abspath guard;
   - the basename must match `_companion_stem`'s patterns, now also `<stem>.flac` and `.md.bak` in campaign folders.

   `delete_unowned_file(path)` checks that `path`'s parent is a `transcript_dirs` entry.
7. **`storage_trim._find_orphans`** stays root-only: `<recording-id>.wav` hand-off copies only ever landed in the root.

**Existing tests to rewrite:**
- Reconcile and sync tests that assert a full set of scanned files may need the journal-file exclusion. Otherwise none; root behaviour is unchanged.

**New tests** (`tests/test_transcript_store.py`, `tests/test_file_registry.py`):
- A `.md` copied into a campaign folder becomes that campaign's transcript, at the end.
- **Drag between campaigns** (`os.replace` keeps mtime): the transcript moves from A to B with its summary, sidecar, excerpts, and audio. Its journal entry is gone, and A's journal is stale.
- A drag into the root unassigns the transcript.
- Two campaigns with `Session 1.md` each: reconcile keeps both rows apart, and sync assigns each folder's companions to its own transcript.
- `<folder> Journal.md` is never registered as a transcript.
- An unrelated subfolder of the output root is ignored.
- The misplaced listing for a campaign transcript whose files are in the root.
- **Delete-file route:** a campaign-folder orphan is deleted; `Other/x.summary.md` with `Other` not a campaign folder → 400; `A/../x` → 400; `A/B/x` → 400.

**Docs:**
- `architecture.md`: Reconcile scans the root and campaign folders, and a cross-folder move changes campaign.
- `docs/scenarios.md`: "I moved a session to another campaign's folder in Obsidian": wisper follows it and its files.
- `docs/web-ui.md`: the Needs attention entries.

**Done when:**
- `pytest tests/test_transcript_store.py tests/test_file_registry.py tests/test_web_routes.py tests/test_path_traversal.py tests/test_storage_trim.py` pass.
- **Rehearsal:**
  - on the scratch copy, `mkdir "<copy>/output/Impossible Landscapes"` and move one session's `.md` and companions into it in the shell;
  - after a page load the session is in place and no longer misplaced;
  - the other three are listed as misplaced.

---

### Phase 5 — Write into campaign folders: uploads, recordings, re-transcribe, CLI

**Goal:**
- A new transcript for a campaign is written into that campaign's folder.
- A re-transcribe writes where the transcript is.
- The upload clash check looks in the target folder and offers Keep both.

**Read first:**
- `web/routes/transcribe.py` 60–250 (`POST /transcribe`, `_upload_stem`, `_name_clashes`, `name_check`) and `templates/transcribe.html` (the clash prompt, ~40–110 and its script)
- `pipeline.process_file` (382–700), `process_folder` (735), `_folder_output_path` (724)
- `web/jobs.py` `submit` (575–700), `_keep_audio` (205), the transcription runner around `TranscriptMissingError` and "Transcripts folder:" (~1237)
- `web/routes/record.py` `_submit_recording_transcription` (958); `web/routes/transcripts.py` `retranscribe`
- `cli.py` `transcribe` (~80–160)

**Steps:**
1. **`campaign_folders.ensure_folder(campaign_id, conn=None, *, data_dir=None, output_dir=None) -> Path`:**
   - returns `output / folder`, creating it if absent;
   - if it exists and is not a directory, or is a non-empty directory holding no file registered to this campaign (no `files` row whose `rel_path` starts with `folder + "/"`), it raises `FolderTakenError(folder)`, a new exception in `campaign_folders`.

   Callers turn that into a fixed error code (`folder_taken`), never the path.
2. **Upload:**
   - `POST /transcribe` resolves the campaign (if any) to its id, and `out_dir = ensure_folder(id)` (else the root). It passes `output_dir=out_dir`.
   - The form's `overwrite` field becomes `clash` ∈ `{"", "overwrite", "keep_both"}`:
     - `keep_both` → `original_stem = next_free_stem(out_dir, stem, campaign_id)`;
     - `overwrite` → today's overwrite.
   - `transcript_store.next_free_stem(directory, stem, campaign_id, conn=None) -> str`: `stem`, else `stem (2)`, `(3)`, …, the first with no file `<s>.md` in `directory` (casefold when `_fold`) and no row `(campaign_id, s)` (casefold).
3. **`_name_clashes(filename, campaign_slug)` and `GET /transcribe/name-check?filename=…&campaign=…`:**
   - check the target folder's `.md`/`.flac`;
   - check the target campaign's rows for a casefold match (`"md"` when it's present, `"missing"` when flagged);
   - a stem equal to that folder's journal name casefold is a clash `"reserved"`.
   - **Response:** adds `"overwrite_allowed": bool`. It's false for `"reserved"`, and for an existing `.md` with no `files` row (a user's file).
   - **The page's script:**
     - re-checks when the campaign select changes;
     - offers **Overwrite** only when `overwrite_allowed`, plus **Keep both** and **Cancel**;
     - shows the modified time as today.
   - Rendering any text from the response goes through `textContent`.
4. **`process_file`:**
   - after `atomic_write_text`, calls `register(out_path, origin="job")` when `dir_campaign(out_path.parent)[0]`;
   - **remove** the post-write `move_transcript_to_campaign` call (683–695): the folder decides the campaign;
   - `campaign` remains the roster filter only;
   - the "not added to campaign" note becomes "Note: {out_path.parent} isn't the transcripts folder; the transcript isn't tracked".
5. **Recording hand-off** (`_submit_recording_transcription`):
   - **re-transcribe:** with a linked transcript, `loc = locate(recording.transcript_id)`, `output_dir = loc.dir`, `original_stem = loc.stem`;
   - **first run:** `output_dir = ensure_folder(recording's campaign id)` (or the root), `original_stem = recording.id`.
   - `FolderTakenError` → the existing `not_ready` error path, with log text naming the campaign.
   - `campaign=` stays, for the roster.
6. **Re-transcribe route:** `output_dir = loc.dir`, `original_stem = loc.stem`, `overwrite=True` (replace in place; the row, campaign, and id are kept).
7. **Jobs:** the "Transcripts folder:" log line prints the job's `output_dir`. `_keep_audio` already writes beside the `.md`.
8. **CLI:**
   - `wisper transcribe --campaign X` without `-o`: `output_dir = ensure_folder(id of X)`. An unknown slug is a `ClickException`. Help text: "Campaign slug: write into its folder and use its roster".
   - With `-o`, unchanged.
   - `process_folder` passes the same `output_dir`.

**Existing tests to rewrite:**
- Upload tests posting `overwrite=on` post `clash=overwrite`.
- `name-check` tests gain `overwrite_allowed`.
- Pipeline tests asserting `move_transcript_to_campaign` was called now assert the file is in the campaign folder and the row has that campaign.
- Recording hand-off tests assert `output_dir`.

**New tests:**
- **Uploads:**
  - an upload with a campaign lands in `<output>/<folder>/`, is registered there, and belongs to that campaign;
  - the same name in another campaign is no clash;
  - the same name in the same campaign is a clash, and `keep_both` writes `Name (2).md`;
  - a name equal to the journal name is `reserved`;
  - `FolderTakenError` (a pre-existing non-empty user folder) → `?error=folder_taken`.
- **Recordings:** a first run lands in the campaign folder; a re-transcribe of a misplaced transcript writes where its `.md` is.
- **CLI:** `--campaign` writes into the folder; with `-o`, beside the input.
- **Missing transcript after write** still fails the job with "Transcript file missing after write" for a campaign-folder target.

**Docs:**
- `docs/web-ui.md`: uploads go into the campaign's folder, and the clash prompt.
- `docs/cli-reference.md`: `transcribe --campaign`.
- `architecture.md`: the job flow's output dir.
- `plan.md` "Open bugs → Missing transcript file": the job's input and output paths now include the campaign folder.

**Done when:**
- `pytest tests/test_web_routes.py tests/test_web_jobs.py tests/test_pipeline.py tests/test_cli.py tests/test_record_routes.py tests/test_path_traversal.py` pass. Use the names that exist; find the hand-off tests with `grep -ln _submit_recording_transcription tests/`.
- **Rehearsal:** upload the 45 s test file into "Impossible Landscapes" (a real transcription on MPS), try again for the clash prompt and choose Keep both, then re-transcribe it.

---

### Phase 6 — Moving a transcript moves its files; clash prompt; Rename

**Goal:**
- Changing a transcript's campaign moves its files.
- A Rename action renames them.
- Both share one clash rule and the busy-job guard.
- Misplaced transcripts can be put in place.

**Read first:**
- `transcript_store` Locations (Phase 2), `rename_companions`, `delete_transcript`, `next_free_stem` (Phase 5)
- `file_registry.move` (471)
- `campaign_manager.move_transcript_to_campaign`, `remove_transcript_from_campaign`, `set_campaign_transcript_order`
- `web/routes/transcripts.py`: `assign_campaign` (749), `bulk-campaign` (368); `web/routes/campaigns.py` remove (253); `templates/transcript_detail.html` (the campaign select)
- `job_history` (the `jobs` columns; `mark_interrupted`)

**Steps:**
1. **`job_history.active_jobs(*, transcript_id=None, campaign_id=None, campaign_slug=None, conn=None, data_dir=None) -> int`:** the number of `jobs` rows with `status IN ('pending','running')` matching any of:
   - `transcript_id`;
   - `campaign_id`;
   - a transcript in that campaign;
   - `json_extract(params_json, '$.campaign') = campaign_slug`.
2. **`transcript_store.move_transcript(transcript_id, campaign_slug: Optional[str], *, clash: Literal["ask", "overwrite", "keep_both", "skip"] = "ask", data_dir=None, output_dir=None) -> MoveOutcome`.** `MoveOutcome` is a dataclass:
   - `status`: `"moved" | "unchanged" | "clash" | "busy" | "folder_taken" | "partial"`;
   - `new_stem`;
   - `clash_modified` (an ISO time or None);
   - `overwrite_allowed`;
   - `kept: list[Path]`.

   Steps:
   1. Same campaign → `unchanged`.
   2. `active_jobs(transcript_id=…)` → `busy`.
   3. `dst_dir = ensure_folder(target)` or the root (`FolderTakenError` → `folder_taken`).
   4. **Clash:** a `<stem>.md` in `dst_dir` (casefold when `_fold`), a target row with that stem (casefold), or the reserved journal name. Then:
      - `ask` → `clash`, with the existing file's mtime and whether it's a registered transcript;
      - `skip` → `clash`, with nothing changed;
      - `overwrite` → only for a registered transcript (`delete_transcript(existing_id)` first), else `clash`;
      - `keep_both` → `new_stem = next_free_stem(...)`.
   5. **One transaction:** `UPDATE transcripts SET campaign_id = ?, position = <end or NULL>, stem = ? WHERE id = ?`. The trigger un-journals it.
   6. **After commit:**
      - `file_registry.move` the `.md` to `dst_dir/<new_stem>.md`;
      - then `rename_companions(tid, old_stem, new_stem, src_dir=old_dir, dst_dir=dst_dir)`;
      - anything not moved goes in `kept` and the status is `partial`. The transcript is misplaced, which still works and is listed.
   7. No reindex is needed: the content and mtimes are unchanged, and the title trigger follows a stem change.
3. **`move_files_home(transcript_id, *, data_dir=None, output_dir=None) -> MoveOutcome`:** steps 3–6 without a database change, for a misplaced transcript.
   - A clash in the expected dir → `clash`; it is never overwritten or renamed automatically.
   - Used by Needs attention's **Move files** and by `storage trim` (Phase 8).
4. **`transcript_store.validate_new_stem(name) -> Optional[str]`:**
   - NFC and strip;
   - non-empty, at most 150 characters;
   - no ``/ \ : * ? " < > |`` or control characters;
   - no trailing dot or space;
   - not ending in `.summary` or `.md` (case-insensitive);
   - not a Windows reserved device name.

   Returns the cleaned name or None.
5. **`rename_transcript(transcript_id, new_name, *, clash="ask", data_dir=None, output_dir=None) -> MoveOutcome`:**
   - validate;
   - case-only renames are allowed;
   - busy check; clash in `loc.dir` as in step 2.4;
   - in one transaction, update `stem`;
   - after commit, move the `.md`. If that move returns `error` or `conflict`, restore the old stem in a new transaction and return `status="error"` (add it to the Literal), with nothing renamed;
   - then `rename_companions` within `loc.dir`.
6. **Routes** (all `transcript_id: int`, with `Location` built from ints and fixed codes):
   - **`POST /transcripts/{id}/campaign`** (`campaign`, `clash`): on `clash`, redirect to `/transcripts/{id}?clash=move&to=<slug>`. The page recomputes the clash server-side (`move_transcript(..., clash="ask")` is read-only up to step 4), and shows Overwrite (if allowed), Keep both, and Cancel with the modified time. The slug in the redirect comes from the database row, not the form.
     - `busy` → `?error=busy`; `folder_taken` → `?error=folder_taken`; `partial` → `?notice=partial_move`.
   - **`POST /transcripts/{id}/rename`** (`new_name`, `clash`): same pattern (`clash=rename`). The confirm text says links typed in your notes don't follow a rename made in wisper.
   - **`POST /transcripts/{id}/move-files`:** `move_files_home`.
   - **`POST /transcripts/bulk-campaign`:** `clash="skip"`, redirecting with `?moved=N&skipped=M&busy=K`, all ints.
   - **The campaign page's remove** → `move_transcript(tid, None, clash="keep_both")`.
7. **`campaign_manager`:**
   - `move_transcript_to_campaign` and `remove_transcript_from_campaign` become private helpers (`_assign`) used only by `move_transcript`;
   - `set_campaign_transcript_order` refuses (`ValueError`) a transcript from another campaign. Moving between campaigns always goes through `move_transcript`.
   - Update their callers, including `tests/_seed.save_campaigns`. Seeding may keep calling `_write_order` directly, since it moves no files.
8. **CLI:**
   - `wisper transcripts move <name> (<campaign-slug> | --none) [--from <slug>] [--keep-both | --overwrite]`;
   - `wisper transcripts rename <name> <new-name> [--campaign <slug>] [--keep-both | --overwrite]`.
   - `<name>` is resolved with `find_by_stem`. When it's ambiguous, `--from`/`--campaign` is required; the error lists the campaigns.
   - Without a flag, a clash prints the existing file's time and exits 1.

**Existing tests to rewrite:**
- `assign_campaign` tests now see files move.
- Bulk-campaign tests assert the counts.
- Tests that moved transcripts through `move_transcript_to_campaign` use `move_transcript` (or the seed helper when files don't matter).

**New tests** (`tests/test_transcript_store.py`, `tests/test_web_routes.py`, `tests/test_cli.py`, `tests/test_path_traversal.py`):
- **Moves:**
  - a move carries every registered file and leaves no row pointing at the old folder;
  - a move to the root;
  - a move into a campaign that has the same name: `ask` → clash (and nothing changed), `keep_both` → `Name (2)`, `overwrite` → the old target is deleted;
  - an `overwrite` against an unregistered `.md` is refused.
- **Locks:** a locked `.md` (patch `os.replace` → `PermissionError` for it) gives `partial`, and the transcript is misplaced; `move_files_home` later completes it.
- **Busy:** a pending job on the transcript, or a pending upload with `$.campaign`, gives `busy`.
- **Journal:** a move out of a journaled campaign marks it stale.
- **Rename:** carries companions; case-only works on a case-insensitive filesystem; a locked `.md` restores the old stem; reserved and invalid names are refused.
- **Bulk:** with one clash, it moves the rest.
- **CLI:** an ambiguous name is refused without `--from`.
- **Path traversal:** the rename route's `new_name` payloads (null byte, `../x`, `a/b`, CRLF) are refused with no file touched.

**Docs:**
- `docs/web-ui.md`: moving between campaigns moves files, the clash prompt, Rename, and Move files.
- `docs/cli-reference.md`: `transcripts move`, `transcripts rename`.
- `architecture.md`: `move_transcript`, `rename_transcript`, the partial-move state, and the busy guard.
- `docs/scenarios.md`: "A session's files didn't all move" (a file was open; use Move files).

**Done when:**
- `pytest tests/test_transcript_store.py tests/test_campaign_manager.py tests/test_web_routes.py tests/test_cli.py tests/test_path_traversal.py` pass.
- `tests/test_transcript_store.py` is in CI's `windows storage` list (it already is; confirm).
- **Rehearsal:** move a session between two campaigns and back; rename it; try a clash with each choice; run a bulk move with one clash.

---

### Phase 7 — Campaign create, rename, and delete with folders

**Goal:**
- Creating a campaign makes its folder.
- Renaming one changes its name, slug, folder, and journal file, and survives a locked folder or a crash.
- Deleting one handles its folder.

**Read first:**
- `campaign_folders.py` (Phases 1, 5); `campaign_manager.create_campaign` (176), `delete_campaign` (197), `_make_slug` (40)
- `journal.journal_path`, `sync_journal`, `adopt_legacy_journal` (Phase 1)
- `web/routes/campaigns.py`: create (41), delete (126), and the page context (62–125); `templates/campaigns.html` (header, delete dialog)
- `cli.py` `campaigns` group (~863–1060)
- `web/app.py` startup

**Steps:**
1. **`campaign_folders.check_available(name, conn, *, output_dir, exclude_id=None) -> Optional[str]`:** returns an error code, or None.
   - `"taken"`: `name.casefold()` equals another campaign's `folder` or `folder_pending` casefold.
   - `"folder_exists"`: an entry in the output root whose name casefold equals `name.casefold()`, and that isn't this campaign's current `folder` (`os.scandir` of the root, comparing NFC casefold).
2. **`create_campaign`:**
   - `folder = folder_name(display_name)`; `check_available`;
   - refuse with `ValueError` codes the routes map to `folder_taken` / `campaign_exists`. No ` (2)` suffix on create: the user picks another name;
   - insert the row, commit, then `ensure_folder`. If the mkdir fails, the folder is made lazily later.
3. **`campaign_folders.rename_campaign(slug, new_display_name, *, data_dir=None, output_dir=None) -> RenameOutcome`.** `status` is one of `"renamed" | "pending" | "busy" | "invalid" | "slug_taken" | "folder_taken"`, plus `new_slug`. Steps:
   1. Strip the display name; `new_slug = _make_slug`; empty → `invalid`. A `new_slug` used by another campaign → `slug_taken`.
   2. `new_folder = folder_name(new_display_name)`; `check_available(new_folder, exclude_id=self)`. An exact match with the current folder means no folder change.
   3. `job_history.active_jobs(campaign_id=…, campaign_slug=slug)` → `busy`.
   4. `journal.sync_journal(slug)` (moves a legacy journal first).
   5. **One transaction:** `UPDATE campaigns SET display_name = ?, slug = ?, folder_pending = ? WHERE id = ?`. `folder_pending` is NULL when `new_folder == folder`.
   6. If a folder change is pending: `finish_folder_rename(campaign_id)`. Return `renamed`, or `pending` if that left it pending.
4. **`finish_folder_rename(campaign_id, *, data_dir=None, output_dir=None) -> bool`** (True when done):
   1. `old = output / folder`, `new = output / folder_pending`.
   2. **Neither exists:** go to step 4.
   3. **`old` exists:**
      - `new` exists and isn't the same directory (`os.path.samefile`, for case-only renames) → return False, leaving it pending;
      - else `os.rename(old, new)`, retried 3× with 0.5 s between attempts on `PermissionError`/`OSError`; still failing → return False.
   4. **If only `new` exists, or after the rename,** one transaction:
      - `UPDATE files SET rel_path = :new || substr(rel_path, length(:old) + 1) WHERE root = 'output' AND substr(rel_path, 1, length(:old) + 1) = :old || '/'`, where `:old`/`:new` are `folder`/`folder_pending` (the on-disk spellings);
      - `UPDATE campaigns SET folder = folder_pending, folder_pending = NULL`.

      If the transaction raises after a rename, `os.rename(new, old)` back, then re-raise.
   5. **After commit:** the journal file `<old folder> Journal.md`, now inside `new`, moves to `journal_name(new folder)` (`file_registry.move`). A conflict or error leaves it under its old name. `journal_path` follows the registry row, so it still works; the next rename or a Needs attention Retry fixes the name.
5. **Startup** (`web/app.py`, after `adopt_legacy_journals`, before `reconcile`): `campaign_folders.finish_pending_renames()` calls `finish_folder_rename` once for every campaign with `folder_pending`. It never loops or sleeps beyond step 4.3's retries.
   - CLI commands that scan (`wisper transcripts list`, `storage trim`) call it too.
6. **Needs attention** pending renames get a **Retry** button: `POST /campaigns/{slug}/finish-rename`, with `slug` validated as today. The panel text: "Rename of folder X to Y didn't finish: a file in it is open in another program. Close it, then Retry."
7. **`delete_campaign(slug, *, delete_transcripts=False, …)`:**
   - **Delete everything:**
     - `delete_transcript(id)` for each;
     - if any is kept (its `.md` was locked), **stop**: the campaign stays, holding what's left, and the result says how many;
     - otherwise delete the journal file and the campaign;
     - then `os.rmdir(folder)`, ignoring `OSError` (a non-empty folder stays).
   - **Keep the files:**
     - `move_transcript(id, None, clash="keep_both")` for each, one at a time;
     - any `partial`, `busy`, or `folder_taken` outcome **stops** the delete: the campaign stays, and the result names the sessions that didn't move;
     - otherwise delete the campaign. Its journal row cascades, and the journal file and folder stay on disk, untracked.
   - Returns a `DeleteOutcome(status, kept: list[str])`. Routes map it to `?error=delete_incomplete` with the count, and the CLI prints the names.
8. **Routes and UI:**
   - `POST /campaigns/{slug}/rename` (`display_name`) → 303 to `/campaigns/<new slug from the database>`, or `?error=<code>`;
   - a Rename form in the campaign page header;
   - the delete dialog's two choices keep their wording, plus "If a session's file is open in another program, the campaign is kept until you retry."
9. **CLI:**
   - `wisper campaigns rename <slug> "<New Name>"` prints the new slug and folder, or the pending notice;
   - `wisper campaigns show` prints `Folder: <folder>` (and `Rename pending → <folder_pending>`).

**Existing tests to rewrite:**
- `create_campaign` tests now see a folder created.
- Delete tests: keep-files moves the files to the root.
- Delete-everything with a locked file keeps the campaign (a deliberate change from the unassign behaviour; see Decisions).

**New tests** (`tests/test_campaign_folders.py`, `tests/test_campaign_manager.py`, `tests/test_web_routes.py`, `tests/test_cli.py`, `tests/test_path_traversal.py`):
- **Create:** create makes the folder. With a pre-existing `hanataz` folder (any case), `Hanataz` is refused. A display name sanitizing to an existing campaign's folder is refused.
- **Rename:**
  - renames the folder, rewrites every `files.rel_path` under it (including names with `[`, `*`, `?`), renames the journal file, and changes the slug; the old slug URL gives not_found;
  - a case-only rename works (patch `_fold` True; on a case-sensitive test filesystem, assert the database is right);
  - `os.rename` raising `PermissionError` → `pending`, and nothing under `files` changes; `finish_folder_rename` after "closing" completes it.
- **Crash recovery:**
  - simulate a crash after the rename, before the transaction (rename the dir by hand with `folder_pending` set); `finish_pending_renames` completes it;
  - neither directory present → the database is updated only.
- **Rename refusals:** busy (a pending relabel job, or a pending upload with `$.campaign`), and `slug_taken`.
- **Delete:**
  - keep-files with a name clash in the root → `Name (2)`;
  - with a locked file, the campaign is kept;
  - delete-everything removes an empty folder and keeps a folder holding a user file.
- **Path traversal:** the rename route's `display_name` with `../`, null byte, and CRLF payloads is rejected or sanitized, and never escapes the output root.

**Docs:**
- `docs/web-ui.md`: rename, and the delete outcomes.
- `docs/cli-reference.md`: `campaigns rename`, `campaigns show`.
- `architecture.md`: the folder rename's two steps and crash rule, `check_available`, and never adopting a user folder.
- `docs/scenarios.md`: "Renaming a campaign says the folder is busy".

**Done when:**
- The listed test files pass.
- `tests/test_campaign_folders.py` is in CI's `windows storage` list.
- **Rehearsal:**
  - rename "Impossible Landscapes" to "Impossible Landscapes: Delta Green" (the colon is sanitized) and back;
  - hold a file open with a background process (`tail -f` on macOS doesn't lock, so patch-only here; the Windows check is in Phase 9);
  - delete a scratch campaign each way.

---

### Phase 8 — `wisper storage trim` organizes folders; prune backups

**Goal:**
- Existing transcripts move into their campaign folders through `wisper storage trim`.
- Old migration snapshots are pruned.

**Read first:**
- `storage_trim.py` (whole file); `cli.py` `storage trim` (find by name)
- `transcript_store.move_files_home` (Phase 6), `Attention.misplaced` (Phase 4)
- `db.migrate`, `_snapshot` (727)

**Steps:**
1. **New action `ORGANIZE = "organize"`,** labelled "move into campaign folder", first in `_ORDER`.
   - `plan()` adds one action per misplaced transcript: `path = loc.md`, `size` = total bytes of its registered files, `transcript_id`, `note = f"→ {folder}/"`.
   - `TrimPlan.total_bytes` and `convert_bytes` exclude it. A new `move_bytes` property sums it.
   - The dry-run summary prints "Move N sessions into their campaign folders (X MB, nothing deleted)".
2. **`plan()` also lists legacy journals** still at `<data>/campaigns/<slug>/journal.md` as `ORGANIZE` actions, with `note="journal"`.
3. **`apply()` runs organize first,** so the FLAC conversion writes into the final folder:
   - for each, `move_files_home(tid)`;
   - `clash` or `folder_taken` → a report error ("<name>: a file with that name is already in <folder>"), skipped;
   - `partial` → an error naming the files kept;
   - journals → `journal.adopt_legacy_journal(slug)`;
   - it calls `campaign_folders.finish_pending_renames()` before planning.

   `TrimReport.organized: list[str]`.
4. **Backup pruning** (`db.py`):
   - after `COMMIT` in `migrate()`, when a snapshot was taken, `_prune_snapshots(data_dir, keep=5)` deletes all but the newest 5 files matching `backups/wisper-v*-*.db`, sorted by the timestamp in the name;
   - it never touches other files or directories in `backups/` (legacy import dirs, reports);
   - an `OSError` is logged.
5. **`wisper storage trim` output** lists organize actions first. A re-run after `--apply` prints nothing to organize.

**Existing tests to rewrite:** `test_storage_trim.py` tests that assert the exact action list or order.

**New tests:**
- **Organize:**
  - a v11 database with assigned transcripts in the root: the dry run lists them and moves nothing;
  - `--apply` moves every file into the folder and leaves none misplaced, and a re-run plans nothing;
  - a clash in the folder is reported and skipped;
  - a legacy journal is moved;
  - organize runs before convert, so the FLAC lands in the folder.
- **Pruning:** 7 snapshots plus a legacy backup dir and a report → 5 snapshots remain, and the dir and report are untouched.

**Docs:**
- `docs/cli-reference.md` and `docs/scenarios.md`: `storage trim` also moves sessions into their campaign folders, and run it once after upgrading.
- `docs/configuration.md`: backups keep the newest 5 snapshots.
- `architecture.md`: the storage trim actions.

**Done when:**
- `pytest tests/test_storage_trim.py tests/test_db.py` pass.
- **Rehearsal:** on a fresh scratch copy (v10), start the server (it migrates to v11), stop it, run `wisper storage trim` (lists 4 sessions), then `--apply`. `Impossible Landscapes/` holds the 4 sessions with their FLACs, summaries, sidecars, and excerpts; pages and playback work; a re-run is clean.

---

### Phase 9 — Final review and rehearsals (orchestrator; no new features)

1. **Full suite**, with `--cov`. New code (`campaign_folders`, the Locations section, the move and rename functions) has only error branches uncovered.
2. **Greps, each clean or explained:**
   - `campaign_transcripts` (only frozen migrations and `legacy_import`);
   - `_get_safe_content_path`, `for_stem`, `_under_output_root` (none);
   - `glob("*.md")` (only in `transcript_store`'s scan);
   - `get_output_dir() /` (none outside `transcript_store`/`file_registry`/`path_utils`);
   - templates' `/transcripts/{{` (ids only);
   - the scar-tissue grep.
3. **Docs vs code:** audit every statement this plan's phases added or changed, and fix any mismatch.
4. **Mac rehearsal** on a fresh copy:
   - v10 → v11;
   - `storage trim --apply`;
   - upload into a campaign; move, rename, and rename-campaign;
   - a shell `mv` of a session between folders (followed on reload);
   - a recording hand-off into a campaign folder;
   - journal fold;
   - search hits, playback, and an old `/transcripts/<name>` URL.
5. **Docker rehearsal** (CPU image, separate copy, case-sensitive filesystem):
   - v10 → v11, `storage trim --apply` in a one-off container with the web service stopped, pages render;
   - two campaigns with `Session 1.md` each.
6. **Windows rehearsal (Brandon, on a copy of the Windows data):**
   - v10 → v11, `wisper db status` clean;
   - `storage trim` dry run, then `--apply`;
   - rename a campaign while one of its files is open in Obsidian → pending; close it → Retry completes;
   - move a session while its `.md` is open → partial, then Move files.
7. **Steps that need Brandon:** set `SCHEMA_FROZEN = True` in the PR commit; remove this section from `plan.md`; open the PR (ask first); the Windows rehearsal (step 6) before his next `start.bat`.

---

### Interactions with other plans

- **Campaign-level LLM summaries (DM tools):** their outputs go in the campaign folder beside the journal (`<folder> Combined Summary.md`, `<folder> Recap.md`), registered as `files` rows owned by the campaign. That needs new `files.kind` values and a new migration in that plan. Folder renames carry them, because Phase 7's prefix rewrite covers every row under the folder; the per-name rename in Phase 7 step 4.5 must include them too.
- **Open bug, missing transcript after write:** the job's output dir may be a campaign folder (Phase 5); the detection reads the job's own `output_path`, so it holds.
- **Live recording:** a recording's first transcript lands in its campaign's folder (Phase 5). The live draft (`recordings/<id>/live_transcript.md`) stays in the data dir.
- **Startup upgrade notice (PR #69):** reports the v11 upgrade with no change. The README/docs "run `wisper storage trim` after upgrading" note (Phase 8) is the follow-up the notice can point to.
- **Storage — open, "Prune old `backups/` snapshots":** done in Phase 8; remove it from that list then.

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
- **Replay markers into the ticker on reload.** Markers persist (`recording_markers`) and show on the detail page, but a page reload doesn't re-insert them into the live ticker (only transcript lines come back through SSE).

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

The rolling campaign journal sets the pattern: storage in the campaign's folder (see "Campaign folders", Interactions), `.summary.md` discovery via `unjournalled_sessions()`, and `JobQueue.submit_journal` / `_run_journal_job` as the template for new `JOB_CAMPAIGN_*` types on the standard SSE progress page. All three features below read the same `.summary.md` sidecars (`SummaryNote` already carries loot, NPCs, and follow-ups). Campaigns with no summarized sessions hide or disable the buttons.

**Build on the database:** the transcript registry and `journal_entries`, not stem lists or frontmatter. Combined-summary and recap outputs get their own table with FKs to the campaign (and the sessions they cover), so deletes cascade; add it as a new migration and extend `test_schema.py`. The search index could cover them too (a new `search_index_state.kind`).

### 1. Combined summary

One LLM call over every session summary in a campaign → `campaigns/<slug>/combined_summary.md`. For retrospectives, onboarding a player, or a campaign wiki. ~20 sessions ≈ 20k input tokens; at 50+ the rolling journal is the better tool. Entry point: "Generate combined summary" on the Campaign page, with a warning at high session counts.

### 2. "Previously on…" recap

A 200–400 word, spoiler-free, player-facing recap built from the last 1–3 session summaries. Shown on the Campaign page or exported as `.recap.md`; shareable with players (e.g. to a campaign Discord). The journal is the DM's cumulative view; the recap is a short retelling for players.

### 3. Hierarchical summaries

Group sessions into arcs, summarize each arc, then combine arcs into a campaign overview. Only needed if the rolling journal hits context limits in practice — deferred indefinitely.
