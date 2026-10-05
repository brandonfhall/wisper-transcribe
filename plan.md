# wisper-transcribe — Open Items

Active plans, open bugs, and parked designs. Shipped work is removed; its design lives in `architecture.md` and its history in git.

---

## Open bugs

### Missing transcript file after a successful Transcribe job

A local-recording transcription once reported COMPLETED ("Wrote `<id>.md`") but the file never existed; root cause unknown. **Detection is in place:** the job fails with "Transcript file missing after write" and logs the job's output dir (the transcripts folder, or the campaign's folder in it), and job history keeps that log across restarts. **Next step:** if it recurs, check `/jobs/history` for the job's log and output root; run with `WISPER_DEBUG=1` to capture more (`LIVE_AUDIO_TEST_PLAN.md` §4a). The recording hand-off no longer copies `combined.wav` into the output dir (it reads it in place), so the job's input is `recordings/<id>/combined.wav` and the output is named by `output_stem`.

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
| Planning: gate review, design, reviewer cycles | complete (5 review cycles) | |
| 1 — Schema v11; campaign queries; journal into the campaign folder | done | ffd78f4 |
| 2a — Location API (`transcript_store.locate` and friends), no callers changed | done (opencode; Claude review fixes) | a051bf1 + next |
| 2b — Every caller resolves through the location API | done (opencode; Claude review fixes) | |
| 3 — Transcript URLs by id; lists from the database | done (opencode; Claude review fixes) | 121ea73 |
| 4 — Scan campaign folders: reconcile, sync, Needs attention | done (opencode; Claude review fixes) | 950048e |
| 5 — Write into campaign folders: uploads, recordings, re-transcribe, CLI | done (opencode; Claude review fixes) | 63d3357 |
| 6 — Moving a transcript moves its files; clash prompt; Rename | done (opencode; Claude review fixes) | 4223250 |
| 7 — Campaign create, rename, and delete with folders | done (opencode; Claude review fixes) | 11b89bf |
| 8 — `wisper storage trim` organizes folders; prune backups | done (opencode; Claude review fixes) | c01f510 |
| 9 — Holistic docs, comments, and tests review | done (opencode; Claude review fixes) | 67f08c0 |
| 10 — Final review and rehearsals (orchestrator) | in progress (orchestrator) | |

### Hand-offs to opencode (2026-10-05)

Brandon is trying a non-Claude agent (opencode with DeepSeek) on the mechanical phases. Claude launches each hand-off headless (`opencode run`), reviews the diff, sends fixes back to the same opencode session, and commits. Phase 2a went this way (`a051bf1`, review fixes `553b9d8`).

**opencode: do only the hand-off named in your prompt, then stop.**
- **Setup:** none. Work in Brandon's Mac checkout with its existing `.venv` (`.venv/bin/pytest`, `.venv/bin/python`). Stay on `feat/campaign-folders`.
- **Read first:** `CLAUDE.md`; this section's "How to run this plan", "Test standard", "Documentation standard", "Findings", "Decisions", "Schema v11"; then your phase, its **Read first** list, and the code your hand-off changes. For a phase that changes a web route, also `.claude/rules/web-security.md`.
- **Scope:** only your hand-off's steps, modules, and their tests. Don't edit any migration in `db.py`. When the code disagrees with the plan, stop and say so in your final message instead of improvising a design.
- **Lessons from the Phase 2a review:**
  - Look rows up through the indexes: `campaign_id = ? AND stem = ?` or `campaign_id IS NULL AND stem = ?`, never `campaign_id IS ?`, and never load a whole table to filter it in Python on a per-call path.
  - Reuse the existing helpers (`file_registry._find_by_path`, `transcript_store._stem_row`, `campaign_folders.holds_only_wisper`, `locate`/`locate_path`/`find_by_stem`) instead of re-implementing them.
- **No real data:** tests use tmp dirs only; never set `WISPER_DATA_DIR` or `WISPER_OUTPUT_DIR` to a real folder, and never run `wisper` against Brandon's data dir (`~/Library/Application Support/wisper-transcribe`).
- **Tests:** while iterating run your modules' test files; at the end run `.venv/bin/pytest tests/ -q -p no:cacheprovider`. Between hand-offs of one phase, failures in modules a later hand-off converts are expected; list them in your final message.
- **One fresh session per hand-off:** resuming a long session hung opencode at startup once (Phase 3c).
- **Don't commit, push, or stage.** Leave your changes in the working tree; Claude reviews `git diff` and stages what it accepts. Don't edit this file's Status table.
- **Final message** (at most ~400 words): files changed; each step done or not; test counts; expected failures; any disagreement with the plan.

### How to run this plan

- **Roles:**
  - An Opus orchestrator hands one phase at a time to a Sonnet worker.
  - It then reviews the diff against that phase's **Done when** and the documentation standard, runs the phase's tests, rehearses (below), and commits.
  - A worker never starts the next phase.
  - Review fixes go back to the **same** worker (SendMessage), not a fresh one.
- **Branch and commits:**
  - Branch `feat/campaign-folders`, one commit per phase. Push after each commit.
  - **Mark progress in this file (Brandon, 2026-10-05):** each phase commit sets that phase's Status row to `done` with its commit hash, and adds a one-line note under the phase heading if anything differed from the plan. When a hand-off of a multi-hand-off phase is graded, its row reads `in progress (1a done)` and so on, in the next commit. The orchestrator checks the row before declaring the phase complete.
  - Brandon waived the between-phase pauses for this run (2026-10-05): run straight through, stopping only for decisions that need him, and pause before the PR.
  - Don't open a PR until Phase 10 passes and Brandon approves.
- **Worker tool calls stay small:**
  - Edits are short; one Write is at most ~60 lines; a test file grows in several Edits.
  - Long writes have stalled workers on the watchdog.
- **Tests:**
  - Never call `monkeypatch.undo()`. It drops the autouse `WISPER_DATA_DIR` fixture, and the test then hits the real data dir.
  - While iterating, run only the phase's test files; the pre-commit hook runs the full suite.
- **Phase close-out (worker, before handing back):**
  - rebuild Tailwind (`.venv/bin/python -m wisper_transcribe.tailwind`) and stage `tailwind.min.css` if it changed;
  - templates, docs, and `plan.md` text can all change it (CLAUDE.md Definition of Done).
- **Busy machine:** the orchestrator, not the worker, runs the full suite and commits, and only while Brandon has no transcription job running (it asks him first). A worker runs only the phase's test files.
- **Large phases run as several hand-offs** (Phases 1, 2b, and 6 name them). Each hand-off is graded on its own; the suite may be red between hand-offs of one phase, and the phase commits once, green, after the last.
- **Read before starting:** `CLAUDE.md`, this plan's Findings, Decisions, and Schema v11 sections, and the phase's **Read first** list. For a phase that adds or changes a web route, also `.claude/rules/web-security.md`.
- **Data safety:**
  - Never run anything against the real data dir **or the real transcripts folder**.
  - Manual checks use a scratch copy and always set **both** `WISPER_DATA_DIR=<copy>/data` and `WISPER_OUTPUT_DIR=<copy>/output`. A copied `config.toml` still names Brandon's real `output_dir`.
- **Unfrozen schema:**
  - Phase 1 adds migration v11 and sets `db.SCHEMA_FROZEN = False` until Phase 10.
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
- **Output root resolution:**
  - New code (`campaign_folders`, the Locations section, `journal`, move/rename, the folder rename) resolves the root with `config.get_output_root()`, which never creates it. `path_utils.get_output_dir()` creates the root and stays only in existing callers. Every "is the output root there?" check is `get_output_root().is_dir()`.
  - Every `file_registry` call (`add`, `add_if_owned`, `move`, `forget`, `repoint`) receives the **output root** as `output_dir`, never a session's own folder: `file_registry` builds `rel_path` relative to that argument, so passing `md.parent` for a session in `<root>/Folder/` stores `S.md` instead of `Folder/S.md`, and a later delete can hit another session's file.
- **Stem lookups use the indexes:** branch in Python, `campaign_id = ? AND stem = ?` for a campaign and `campaign_id IS NULL AND stem = ?` for the root. Never bind `campaign_id IS ?` in these lookups: neither partial unique index serves it (`EXPLAIN QUERY PLAN` shows a campaign scan).
- **Windows can't be reproduced on macOS:** `os.replace`, `os.rename`, and `unlink` fail on a file or folder another program holds open (Obsidian, Explorer, a player, the search indexer).
  - Every move and rename catches `PermissionError`/`OSError`, reports it, and leaves the database matching the disk.
  - Test it by monkeypatching `os.replace`/`os.rename` to raise `PermissionError`.
- **CI's `windows storage` job** (`.github/workflows/ci.yml`, the `pytest tests/test_db.py …` line) runs a fixed list of test files on Windows. Add every new test file that moves, renames, or deletes files.
- **Unicode and case:**
  - macOS may store names in NFD: compare NFC.
  - Case-only tests patch `transcript_store._is_case_insensitive` and `file_registry._fold` both ways.
- **Test rules:**
  - No GPU, network, or real audio.
  - **The output root in tests:** Phase 1 changes `tests/conftest.py`'s autouse `_isolated_data_dir` to also `mkdir` `<data dir>/output`, the root `get_output_root()` resolves to with `WISPER_OUTPUT_DIR` unset (it stays unset). A test that needs a different root sets `monkeypatch.setenv("WISPER_OUTPUT_DIR", str(dir))` (and creates it), never patches a module's `get_output_dir`: new code resolves through `config.get_output_root()`, which such a patch doesn't reach. Each phase converts the `get_output_dir` patches in the test files its code reaches: Phase 1 `test_journal.py`'s `out_dir` fixture (~49–55); Phase 2b and 3 the patches of `routes.transcripts.get_output_dir` (~38 in `test_web_routes.py`, ~6 elsewhere), `routes.transcribe` (~6), and `routes.dashboard` (~5). Tests that check the unset-and-absent case remove the directory themselves.
  - Seed with `tests/_seed.py`. Phase 1 adds `seed_campaign` and `seed_transcript`, and Phase 2b adds `transcript_id(stem, …)`.
  - `ON CONFLICT` against a partial unique index must repeat the index's `WHERE` (`ON CONFLICT (campaign_id, stem) WHERE campaign_id IS NOT NULL DO NOTHING`); without it SQLite errors "does not match any UNIQUE constraint". `INSERT OR IGNORE` also works.
  - Patch lazily imported functions on their source module.
- **Existing tests:** each phase lists the existing tests it must rewrite. A test outside that list that starts failing means a step was misread: stop and report.
- **Line numbers** are from `main` at `feabab7`. Find each function by name if they've drifted.
  - `static/` means `src/wisper_transcribe/static/`; templates are `src/wisper_transcribe/web/templates/`.
- **When the code disagrees with this plan,** stop and report to the orchestrator. Don't improvise a different design.

### Test standard (every phase; the orchestrator rejects a phase that breaks it)

1. **The full suite is green at the end of every phase.** Not only the phase's files: the pre-commit hook runs everything.
2. **Every behaviour a phase's Steps add or change has a test** that fails without the change. The phase's **New tests** list is the minimum.
3. **Existing tests are rewritten, not deleted or skipped.**
   - A test that asserted old behaviour is changed to assert the new behaviour, with its name and docstring updated to match (no "was"/"used to").
   - Deleting a test needs the orchestrator's OK and a reason in the commit message.
4. **Every new module gets `tests/test_<module>.py`** (CLAUDE.md). Any test file that moves, renames, or deletes files is added to CI's `windows storage` list.
5. **Security controls get `tests/test_path_traversal.py` cases** for every new or changed route parameter: null byte, `../`, separators, CRLF on redirects.
6. **Schema changes get `tests/test_schema.py` cases** (one per constraint or trigger). Upgrades get a seeded-upgrade test in `tests/test_db.py`. Whole flows go in `tests/test_e2e.py` (Phase 10 adds the campaign-folder flow if Phase 9 didn't).
7. **Seed through `tests/_seed.py`**, never raw JSON-era files. No real audio, network, or GPU.
8. **Guard tests stay green and are extended, never loosened:**
   - `test_transcript_store.py::test_only_transcript_store_deletes_transcripts` and `::test_file_writes_are_atomic`, `test_search_index.py::test_transcript_writes_reindex`;
   - `test_db.py::test_only_db_module_opens_sqlite`;
   - `test_recording_manager.py::test_capture_code_uses_targeted_writers`.

   A new direct `unlink`/`write_text` goes through `transcript_store`.

### Documentation standard (every phase; the orchestrator rejects a phase that breaks it)

1. **State current facts and the reason, never history.** This applies to `architecture.md`, `docs/`, `CLAUDE.md`, code comments, docstrings, **test docstrings and test names**, and the PR body. Commit messages may describe the change.
   - Banned in all of those: "now", "no longer", "used to", "previously", "changed to", "instead of the old …", "new:", dates, phase numbers, finding numbers, "campaign folders" as a project name.
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
- **Transcript web routes take a name:** `/transcripts/{name}/…`, 16 routes in `web/routes/transcripts.py`, lines 457–1007. They resolve it with `_get_safe_content_path(name, suffix)` (193), a flat-root path guard.
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
  - Windows: at v10 and migrated (Brandon, 2026-10-04). Its counts are needed for the Phase 10 rehearsal.

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
| A campaign's folder name is its display name, made safe:<br>• NFC;<br>• forbidden characters (``/ \ : * ? " < > |``), control characters, and DEL → a space;<br>• runs of whitespace collapsed;<br>• then **repeatedly** strip spaces, leading dots, and trailing dots or spaces until nothing changes;<br>• cut to the room left (80 characters and 200 UTF-8 bytes, less a ` (n)` suffix) and trim again;<br>• a Windows reserved device name before the first dot gets `_` inserted there (`CON` → `CON_`, `con.txt` → `con_.txt`);<br>• `Campaign` if nothing is left.<br>The ` (2)`, ` (3)` suffix is used only by the v11 import for existing campaigns. Creating or renaming a campaign whose folder name is taken is refused, and the user picks another name. | Windows rules. 80 characters leave room under MAX_PATH (260) for a long session name. A frozen migration must never produce a name its own CHECK rejects. |
| Renaming a campaign renames it everywhere: display name, slug, and folder, and the journal file with the folder. Old `/campaigns/<old-slug>` URLs stop working. | Brandon. |
| **wisper owns a campaign folder only when it claimed it:** it created the folder, or found it absent or empty (`campaigns.folder_claimed`). Only a claimed folder is scanned, written to (sessions, journal), renamed, or removed. An existing non-empty folder with the campaign's name is never adopted automatically; it's listed under Needs attention as "folder taken", where the user either renames the campaign or chooses **Use this folder**, which claims it after saying that every `.md` in it becomes a session (Brandon, 2026-10-05). wisper never creates the output root itself, and an absent campaign folder or output root means "unavailable", never "deleted". Creating or renaming a campaign is refused when the folder name matches (ignoring case) an existing entry in the output root, except when that folder holds only this campaign's `<folder> Journal.md` (left by a keep-files delete), which is re-claimed. | The output root can be the user's vault. A scanned folder registers every `.md` as a session, and a registered file can later be deleted by wisper. |
| A campaign folder rename runs in two steps: the display name and slug change at once, the folder changes on disk, then `campaigns.folder` and every path under it. `campaigns.folder_pending` records the target, so a crash or a locked folder leaves a state that startup finishes, or that Needs attention retries. | On Windows a folder can't be renamed while any file in it is open. |
| Transcript URLs use the row id: `/transcripts/{id}/…` (the `{transcript_id:int}` path convertor, so a name falls through). An old `/transcripts/{name}` page URL redirects when exactly one transcript has that name; old sub-page URLs (`/summary`, `/audio`, `/edit`) don't (*default*). | Names aren't unique across campaigns. An integer path parameter also removes the path-traversal surface from 16 routes. |
| Overwrite deletes the existing session first, then moves the chosen one in. If the chosen one's `.md` then can't move (locked), the move is refused: the overwritten session is gone and the chosen one is unchanged where it was (Brandon, 2026-10-05). | The user chose to replace that session; holding both through a temp file adds a third state to recover from. The prompt's Overwrite text says the existing session is deleted. |
| A claimed campaign folder that disappears (renamed or deleted outside wisper, or an unmounted drive) is never recreated silently. Writes into it are refused (`folder_missing`), its sessions read missing, and Needs attention offers **Recreate folder** or renaming the campaign (Brandon, 2026-10-05). | Recreating it on an empty remount would let the journal check see "folder present, journal gone" and reset the journal's fold state. |
| New session names (uploads, keep-both, Rename) are at most 100 characters, don't start with `.`, and aren't Windows reserved names. Existing longer names are kept (Brandon, 2026-10-05). | A typical Windows vault path (~45) + folder (80) + separators + name (100) + `_excerpt_SPEAKER_00.mp3` (23) stays under MAX_PATH (260). A leading `.wisper-tmp-` name would be swept as a temp file. |
| Adopting a legacy journal keeps the old file renamed to `journal.md.v11-adopted` beside it, instead of deleting it (Brandon, 2026-10-05). `docs/configuration.md` documents a rollback: restore the `wisper-v10-*.db` snapshot, rename each `journal.md.v11-adopted` back, and move session files back to the output root. | A v10 build that finds no `journal.md` resets the journal. |
| Moving a transcript to another campaign, or out of all of them, moves its `.md` and every registered file (summary, sidecar, excerpts, audio, backup) into the target folder. The database changes first, then the files follow. If the `.md` can't move (locked), the database change is undone and the move is refused. A companion that can't move stays, and the transcript reads as misplaced until Move files finishes it. A session flagged missing moves in the database only. | "Files follow rows". The registry keeps a partial move consistent. |
| A move, rename, or upload that would land on an existing name prompts **Overwrite**, **Keep both** (saved as `<name> (2)`), or **Cancel**, showing the existing file's last-modified time. Overwrite is offered only when the existing file is a wisper transcript (it has a `files` row); otherwise only Keep both or Cancel. A bulk move doesn't prompt: a clashing transcript stays where it is and is listed in the result (*default*). | Brandon, 2026-10-03 (the prompt). wisper never overwrites a file it doesn't own. |
| wisper gets a **Rename** action for transcripts (web and CLI). It renames the `.md` and every companion together, with the same clash prompt. | Brandon, 2026-10-03 (*default*: in scope). |
| A move, rename, campaign rename or delete, or finishing a pending folder rename is refused while a job is pending or running for that transcript, its source or target campaign, or an upload into either, or while a recording of either campaign is capturing. A transcription job records its target transcript at submit, so a queued re-transcribe counts. The check runs inside the write transaction. A CLI `wisper transcribe` run has no `jobs` row and isn't seen (accepted, single user). | A job writes to a path decided at submit. |
| Renaming a session (in wisper, or adopted by reconcile) marks its campaign's journal stale, like a move or a re-transcribe. | The journal text names the old session. |
| When a session's stored name and its registered `.md` disagree (a crash between the database update and the file move), the registry wins: reconcile sets the stem from the `.md`'s name, or lists it under Needs attention when that name is taken. | The registry is the source of truth for where and what a file is. |
| After a campaign rename, job history rows that only knew the old slug (`params_json`) lose their campaign link, and a journal's frontmatter shows the old slug until its next fold. Accepted. | History is informational; rewriting stored job parameters isn't worth the risk. |
| **Deleting a campaign:**<br>• **Delete everything** deletes its transcripts, their files, and the journal, then removes the folder only if it's empty.<br>• **Keep the files** moves its transcripts to the output root (a clash becomes `<name> (2)` and is reported). The journal and the folder stay on disk, untracked; creating a campaign of the same name later re-claims that folder and its journal.<br>• If any session can't be deleted or moved (a file open in another program), the campaign is **kept** with what's left, and the result says which. This replaces the earlier "delete the rest and leave the locked one unassigned" rule, which can't hold now that a campaign can't be deleted while it holds sessions.<br>(*default*) | `transcripts.campaign_id` refuses deleting a campaign that still holds transcripts. wisper never deletes a folder holding files it doesn't own. |
| A new transcript's campaign is the folder it's written into. An upload chosen for a campaign is written into that campaign's folder. | One rule for the web, recordings, the CLI, and files that appear on disk. |
| A `.md` that appears in a campaign folder (copied there or dropped in Obsidian) becomes a transcript of that campaign, exactly as a `.md` in the root becomes an unassigned one. A transcript dragged from one folder to another (matched by size and mtime, as renames are) changes campaign, and its companions follow. | Folders mean campaigns. Brandon keeps personal notes outside the output folder (2026-10-04), so no wisper-marker check is needed. |
| `wisper transcribe --campaign X` without `-o`, or with `-o` naming the output root, writes into X's folder (so it joins X). With `-o` elsewhere, `--campaign` is roster-only and a file outside the output root isn't registered, as today (*default*). A name already in the campaign is refused unless `--keep-both` or `--overwrite` is given: `--overwrite` writes to that session's `.md` (keeping its row), and `--keep-both` names the new copy after the run's local start time — `f"{stem} ({start:%Y-%m-%d %H%M})"`, e.g. `Session 3 (2026-10-05 0142)`, with the colon-free suffix Windows requires; a second clash adds ` (2)`. The web's Keep both keeps ` (2)`. | Brandon asked that CLI runs land in the right folder and that its Keep both not clash on ` (2)`. |
| Existing transcripts move into their campaign folders through `wisper storage trim` (dry run, then `--apply`), as one more action. The migration moves no files. Until then they're misplaced, and everything still works. | Brandon: fold it into storage trim, no new command. Migrations are frozen and must not move user files. |
| A journal still at `<data>/campaigns/<slug>/journal.md` moves into the campaign folder the first time its campaign's journal is read. That happens on any page or command that touches the journal, and at startup. | No journal is ever reset by a missing-file check before it's moved. |
| v11 keeps the last 5 `backups/wisper-v*.db` snapshots after a migration (*default*). | Each migration adds a snapshot; Brandon agreed to fold this in. |
| Out of scope: storing `combined.wav` as FLAC; the Ollama empty-response bug. | Brandon. |

### Schema v11

One migration, `Migration(11, "campaign-folders", _V11_DDL, _v11_import)`. The DDL below ran clean on the prototype (see Findings), including the edge-case names the reviewers found.

**Python string:** `_V11_DDL` is a normal (non-raw) string like `_V9_DDL`, so every SQL `\` is written `\\` in `db.py` (SQL `'*[/\]*'` is Python `'*[/\\]*'`). `test_schema.py::test_v11_ddl_backslashes` reads the `transcripts` and `campaigns` CHECK texts from `sqlite_master` and asserts that `[/\]` and `[/\:` appear, catching a lost backslash.

**What it does, in order:**
1. **Drop the triggers it recreates.** Otherwise a rename re-parses a trigger that names a dropped table and fails.
2. **Rebuild `campaigns`:**
   - adds `folder` (stored; unique, ignoring ASCII case), `folder_pending` (a folder rename in progress), and `folder_claimed` (wisper owns that folder; 0 for every migrated campaign);
   - `folder` gets a placeholder (`'#' || id || char(127)`) that the sanitizer can never produce (it removes DEL), so the import never collides with a placeholder; `_v11_import` sets the real name.
3. **Rebuild `transcripts`:**
   - adds `campaign_id` and `position`, copied from `campaign_transcripts`;
   - `id` becomes `AUTOINCREMENT`;
   - stem uniqueness becomes per campaign, through two partial unique indexes (no sentinel value), plus a plain index on `stem` for lookups across campaigns.
4. **Rebuild `journal_entries`** so its composite FK targets `transcripts(campaign_id, id)`, then drop `campaign_transcripts`.
5. **Recreate the triggers and rebuild the title index.**
   - `transcripts_campaign_bu` deletes a moved session's journal entry, and `journal_entries_ad` then marks the old campaign's journal stale.
   - `transcripts_stem_journal_au` marks the journal stale on a rename.
   - `campaigns_folder_bi`/`_bu` refuse a `folder` or `folder_pending` that equals (ignoring ASCII case) another campaign's `folder_pending` or `folder`.
6. **Rebuild `files`:**
   - journals live under the output root, in a campaign folder;
   - output paths are at most one folder deep;
   - the old data-root journal rows are dropped (the file moves on first read; see Phase 1);
   - any existing output row deeper than one folder is dropped and listed in the import report, via `temp.v11_dropped`. None are expected (`register` runs only for files directly in the output root); the drop keeps the CHECK from failing on a hand-made row.
7. **Give every transcript a `transcript` row** (`_v11_import` step 3), so `Located.md` finds a misplaced session's root `.md` by its registered path.

`campaigns.folder` and `transcripts.campaign_id` are separate facts: the folder is a stored name, and the campaign is the assignment. The derived pair "this session's expected folder" is computed, never stored.

```sql
-- Triggers are recreated below; dropping them first keeps the renames from re-parsing them.
DROP TRIGGER journal_entries_ad;
DROP TRIGGER transcript_titles_ai;
DROP TRIGGER transcript_titles_ad;
DROP TRIGGER transcript_titles_au;
DROP TRIGGER files_profile_key_au;
CREATE TABLE campaigns_new (
  id             INTEGER PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE CHECK (slug <> ''),
  display_name   TEXT NOT NULL CHECK (display_name <> ''),
  folder         TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (folder <> '' AND length(folder) <= 80
                        AND length(CAST(folder AS BLOB)) <= 200
                        AND folder NOT GLOB '*[/\:*?"<>|]*'
                        AND folder NOT GLOB '.*'
                        AND folder NOT GLOB '* ' AND folder NOT GLOB '*.'
                        AND folder NOT GLOB ' *'
                        AND NOT (folder GLOB '*[' || char(1) || '-' || char(31) || ']*')
                        AND instr(CAST(folder AS BLOB), x'00') = 0),
  folder_pending TEXT COLLATE NOCASE UNIQUE CHECK (folder_pending IS NULL OR (folder_pending <> '' AND length(folder_pending) <= 80
                        AND length(CAST(folder_pending AS BLOB)) <= 200
                        AND folder_pending NOT GLOB '*[/\:*?"<>|]*'
                        AND folder_pending NOT GLOB '.*'
                        AND folder_pending NOT GLOB '* ' AND folder_pending NOT GLOB '*.'
                        AND folder_pending NOT GLOB ' *'
                        AND NOT (folder_pending GLOB '*[' || char(1) || '-' || char(31) || ']*')
                        AND instr(CAST(folder_pending AS BLOB), x'00') = 0)),
  folder_claimed INTEGER NOT NULL DEFAULT 0 CHECK (folder_claimed IN (0, 1)),   -- wisper made it, or found it absent or empty
  created_at     TEXT NOT NULL,
  journal_sha256 TEXT CHECK (journal_sha256 IS NULL OR length(journal_sha256) = 64),
  journal_stale_since TEXT,
  CHECK (folder_pending IS NULL OR folder_pending <> folder COLLATE BINARY)
) STRICT;
INSERT INTO campaigns_new (id, slug, display_name, folder, created_at, journal_sha256, journal_stale_since)
  SELECT id, slug, display_name, '#' || id || char(127), created_at, journal_sha256, journal_stale_since FROM campaigns;
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
CREATE UNIQUE INDEX transcripts_stem      ON transcripts(campaign_id, stem) WHERE campaign_id IS NOT NULL;
CREATE UNIQUE INDEX transcripts_root_stem ON transcripts(stem) WHERE campaign_id IS NULL;
CREATE INDEX transcripts_stem_any ON transcripts(stem);

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
CREATE TRIGGER transcripts_stem_journal_au AFTER UPDATE OF stem ON transcripts
  WHEN old.stem IS NOT new.stem BEGIN
  UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
   WHERE id IN (SELECT campaign_id FROM journal_entries WHERE transcript_id = new.id);
END;
CREATE TRIGGER campaigns_folder_bi BEFORE INSERT ON campaigns
  WHEN EXISTS (SELECT 1 FROM campaigns c WHERE c.folder_pending = new.folder COLLATE NOCASE
                  OR c.folder = new.folder_pending COLLATE NOCASE
                  OR c.folder_pending = new.folder_pending COLLATE NOCASE) BEGIN
  SELECT RAISE(ABORT, 'campaign folder taken');
END;
CREATE TRIGGER campaigns_folder_bu BEFORE UPDATE OF folder, folder_pending ON campaigns
  WHEN EXISTS (SELECT 1 FROM campaigns c WHERE c.id <> new.id
                AND (c.folder_pending = new.folder COLLATE NOCASE
                  OR c.folder = new.folder_pending COLLATE NOCASE)) BEGIN
  SELECT RAISE(ABORT, 'campaign folder taken');
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
                                     AND '/' || rel_path || '/' NOT GLOB '*/./*'
                                     AND instr(CAST(rel_path AS BLOB), x'00') = 0),
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
-- temp.v11_dropped and temp.v11_journals are read and dropped by _v11_import; later migrations must not reuse the names.
CREATE TEMP TABLE v11_dropped AS
  SELECT id, kind, rel_path FROM files WHERE root = 'output' AND rel_path GLOB '*/*/*';
CREATE TEMP TABLE v11_journals AS
  SELECT c.slug FROM files f JOIN campaigns c ON c.id = f.campaign_id WHERE f.kind = 'journal';
INSERT INTO files_new SELECT * FROM files
  WHERE kind <> 'journal' AND NOT (root = 'output' AND rel_path GLOB '*/*/*');
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
CREATE INDEX jobs_active ON jobs(status) WHERE status IN ('pending', 'running');
```

**`_v11_import(conn, ctx)`** (self-contained: it imports no application module, like `_v9_import`):
1. For each row of `temp.v11_dropped`: `ctx.note(f"file record {rel_path!r} ({kind}) was more than one folder deep; dropped")`. Then `DROP TABLE temp.v11_dropped`. Also note each campaign whose data-root journal row was dropped (from `temp.v11_journals`, which the DDL fills before the `files` copy; drop it after): `ctx.note(f"campaign {slug}: journal record dropped; the file moves into the campaign folder on first read")`.
2. For each campaign in `id` order:
   - `folder = _v11_folder_name(display_name)`;
   - while `folder.casefold()` is taken by an earlier campaign, try `_v11_folder_name(display_name, room=80 - len(sfx), byte_room=200 - len(sfx.encode())) + sfx` for `sfx = " (2)", " (3)", …`;
   - `UPDATE campaigns SET folder = ?`.

   A campaign whose folder differs from its display name gets `ctx.note(f"campaign {slug}: folder {folder!r}")`.
3. **Backfill `transcript` rows.** For each transcript with no `kind = 'transcript'` row in `files`, in `id` order: `INSERT INTO files (kind, root, rel_path, transcript_id) VALUES ('transcript', 'output', stem || '.md', id) ON CONFLICT DO NOTHING`. The row is stat-less (`size`/`mtime_ns` NULL); `file_registry.sync` fills the stat on its next pass. An `IntegrityError` (a stem the `transcript` CHECK rejects, such as one ending `.summary`) gets `ctx.note(f"transcript {id}: {stem!r}.md not registered ({exc})")` and is skipped. A `rowcount` of 0 (the path is already registered to another transcript, e.g. after a relink) gets `ctx.note(f"transcript {id}: {stem!r}.md already registered to another transcript; not registered")`. This is `_v9_import`'s pattern.
4. It touches no files (it reads none either).
5. `db.py` gains `import re` and `import unicodedata` for the sanitizer.

**`_v11_folder_name(display_name: str, room: int = 80) -> str`** is the sanitizer in Decisions, frozen in `db.py`. Reference implementation (prototyped):
```python
# Microsoft "Naming Files, Paths, and Namespaces": COM0-9, LPT0-9, and the superscript ¹²³ forms.
_V11_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                 *(f"{p}{d}" for p in ("COM", "LPT") for d in "0123456789\u00b9\u00b2\u00b3")}

def _v11_trim(s: str) -> str:
    while True:
        t = s.strip().lstrip(".").rstrip(". ")
        if t == s:
            return s
        s = t

def _v11_cut(s: str, room: int, byte_room: int) -> str:
    # 80 characters, and at most byte_room UTF-8 bytes: ext4 and APFS cap a name at 255 bytes.
    s = s[:room]
    while len(s.encode("utf-8")) > byte_room:
        s = s[:-1]
    return s

def _v11_folder_name(display_name: str, room: int = 80, byte_room: int = 200) -> str:
    s = unicodedata.normalize("NFC", display_name)
    s = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', " ", s)
    s = re.sub(r"\s+", " ", s)
    s = _v11_trim(_v11_cut(_v11_trim(s), room, byte_room))
    head, dot, tail = s.partition(".")
    core = head.rstrip()  # Windows ignores spaces before the extension: "nul .txt" is NUL
    if core.upper() in _V11_RESERVED:
        s = _v11_trim(_v11_cut(core + "_" + head[len(core):] + dot + tail, room, byte_room))
    return s or "Campaign"
```
`campaign_folders.folder_name` starts as a copy of this, and may evolve; v11's copy may not.

**Why each constraint:**
- `UNIQUE (campaign_id, id)` is the journal's composite FK target: a journal can only hold a session of its own campaign.
- `CHECK ((campaign_id IS NULL) = (position IS NULL))`: a session in a campaign always has an order, and an unassigned one never does.
- Partial unique indexes, not `coalesce(campaign_id, 0)`: there's no sentinel to collide with a real id, and plain `campaign_id = ? AND stem = ?` and `campaign_id IS NULL AND stem = ?` queries use them (`EXPLAIN QUERY PLAN` checked).
- `transcripts.campaign_id` has no ON DELETE action (restrict): a campaign is emptied before it's deleted (Decisions, campaign delete).
- `files.campaign_id ON DELETE CASCADE` stays: only `journal` rows have it, and a deleted campaign's journal file is left on disk, untracked.
- `folder COLLATE NOCASE UNIQUE` plus the two folder triggers catch ASCII case clashes in the database. Unicode case folding is checked in the app (`campaign_folders.check_available`), since SQLite's NOCASE folds ASCII only.
- `folder_pending <> folder COLLATE BINARY` lets a case-only rename (`hanataz` → `Hanataz`) be pending.
- `folder_claimed` is a fact the app can't recompute later: once a folder holds registered files, "did wisper create it?" can no longer be told from the disk.
- `files` `rel_path NOT GLOB '*/*/*'` under the output root: one folder level only, so scanning stays simple.
- `length(CAST(folder AS BLOB)) <= 200`: ext4 (Docker) and APFS cap one name at 255 bytes, and `<folder> Journal.md` must fit.
- `jobs_active` (partial index on pending/running jobs): the busy check runs inside every move, rename, and delete, and `jobs` is unbounded history.
- Unicode case twins (`été`/`ÉTÉ`) are refused by the app only (`check_available`, the import's casefold); SQLite's NOCASE folds ASCII only. The folder rename's prefix rewrite also refuses (raises) when another campaign's folder casefolds equal to `:old`.
- `instr(CAST(folder AS BLOB), x'00') = 0`: GLOB and `length()` stop at an embedded NUL, so the other CHECKs can't see one.
- A journal's `rel_path` repeats its campaign's folder name. It isn't pinned to `campaigns.folder` by a constraint because a folder rename is two steps (the folder renames on disk, then every row) and the rows must be able to lag the disk while it's pending.
- `AUTOINCREMENT` starts after the largest id present at migration. An id deleted at v10 above that maximum can be reused once; that's harmless, since v10 URLs used names.

**Tests for v11 (`tests/test_schema.py`, `tests/test_db.py`):**
- **Upgrade:** a v10 database built inside `with monkeypatch.context() as m:` that sets **both** `db.MIGRATIONS` to `db.MIGRATIONS[:10]` and `db.LATEST_VERSION` to 10 (`migrate()` stamps `LATEST_VERSION`, fixed at import; this is `test_db.py`'s existing pattern), seeded through every child table. The upgrade then runs through `db.migrate()` itself, never `executescript`, so `_exec_ddl`'s statement splitting is what's tested:
  - `transcript_speakers`; `search_index_state` + `search_blocks` + FTS; `jobs`; `recordings` linked to a transcript; `journal_entries`; `campaign_members`;
  - `files` of every output kind, plus a data-root journal and one output row two folders deep;
  - one assigned transcript with no `transcript` files row, and one root transcript named `odd.summary` with none.

  It migrates to v11 with:
  - every row count unchanged, except the journal row and the deep row, which are dropped (the deep one noted in the report), and the backfilled `transcript` rows;
  - the unregistered transcript has a stat-less `transcript` row at `<stem>.md`; `odd.summary` has none and is noted;
  - placements carried into `campaign_id`/`position`;
  - `integrity_check` ok, `foreign_key_check` empty, and a title search still finding a transcript;
  - `folder_claimed = 0` everywhere.
- **Folder names at import:**
  - `Hanataz: Act I?` and `HANATAZ: act i?` → `Hanataz Act I` and `HANATAZ act i (2)`;
  - `CON` → `CON_`; `con.txt` → `con_.txt`; `COM0` → `COM0_`; `COM¹` → `COM¹_`; `nul .txt` → `nul_ .txt`;
  - `. Hidden Tomb` → `Hidden Tomb`; ` .. x .. ` → `x`;
  - `#9` imports cleanly;
  - two 85-character case twins → 80 characters, and 80 characters including ` (2)`.
- **Constraints and triggers:** one `test_schema.py` case per prototype check (Findings, "Behaviour confirmed"), plus:
  - a stem rename marks the journal stale;
  - `folder_pending` equal to another campaign's `folder` is refused;
  - campaign id 0 and the root may share a stem.
- **Schema drift:** `db.status()` reports no drift on a migrated database (`expected_schema(11)` replays the DDL; the temp table isn't in `sqlite_master`).
- **Backslashes:** `test_v11_ddl_backslashes` (above), plus `test_db_py_has_no_invalid_escapes`: `compile()` `db.py`'s source under `warnings.simplefilter("error", SyntaxWarning)`. Python keeps an invalid escape like `\]` literally, so only the warning catches a single backslash.
- **Bytes:** 80 × `😀` → 50 characters (200 bytes); its case twin → 49 + ` (2)`; a `folder` over 200 bytes is refused by the CHECK.
- **`CONIN$`** → `CONIN$_`.
- **`jobs_active`:** `EXPLAIN QUERY PLAN` of `active_jobs`' own SQL (Phase 6 adds a test that captures it) uses the index. SQLite uses a partial index only when the query repeats its condition exactly, so `active_jobs` writes `status IN ('pending', 'running')` in that order and spelling.
- **NUL:** a `folder` or a `files.rel_path` containing `char(0)` is refused (GLOB stops at NUL, so `'a.md' || char(0) || '/b/c.md'` would otherwise pass the depth CHECK).
- **Reserved names** are enforced by the sanitizer and the app only; the `folder` CHECK accepts `CON`. Say so in the "Why each constraint" list.

---

### Phase 1 — Schema v11; campaign queries; journal into the campaign folder

*Done. Differences from the plan: `export_journal` raises `ValueError` for a bad slug (it returned `None`). After a keep-files campaign delete the journal stays on disk untracked, since no campaign claims the folder; Phase 4 lists the leftover folder. Rehearsed on a scratch copy of the Mac data: v10 → v11, every page 200, a legacy journal adopted into `Impossible Landscapes/`.*

**Goal:**
- The database is at v11.
- Every query that used `campaign_transcripts` uses `transcripts.campaign_id`/`position`.
- A campaign's journal is `<output root>/<folder>/<folder> Journal.md`, in a folder wisper has claimed.
- Nothing else changes: sessions are still written to, and found in, the output root.

**Read first:**
- `db.py`: the v9 and v10 sections, `MIGRATIONS` (703), `migrate()` (739), `_exec_ddl` (824), `expected_schema` (1220)
- `campaign_manager.py` (whole file); `models.py` `Campaign` (72); `tests/_seed.py` `save_campaigns` (135–166)
- `journal.py` 96–320 and `update_journal` (345, how it writes the pending file and replaces it); `transcript_store._replace` (68)
- `file_registry.py`: `KINDS`/`ROOT_OF_KIND`/`_OWNER_OF_KIND` (44–64), `_scan_data` (680), `_sync` (757)
- The `campaign_transcripts` users listed in Findings
- `web/app.py`: the startup lifespan (migrate, `mark_interrupted`, `reconcile(sweep=True)`)
- `docker-compose.yml` (the volume comments), `docs/docker.md`

**Hand-offs:** 1a is steps 1–5 and 8 (schema, `campaign_folders`, queries, seeds) and the `conftest.py` output-root change. 1b is steps 6–7 (journal, registry) and the docs. One commit after 1b.
- **Between hand-offs of any phase,** failures in tests of modules a later hand-off of the same phase converts are expected and aren't a stop condition. At the end of each hand-off, that hand-off's own modules' tests pass; at the end of the phase, the full suite passes.

**Steps:**
1. **Migration.**
   - Add `_V11_DDL` (Schema v11, verbatim, with `\` doubled), `_V11_RESERVED`, `_v11_trim`, `_v11_folder_name`, `_v11_import`, and `Migration(11, "campaign-folders", _V11_DDL, _v11_import)`.
   - Set `SCHEMA_FROZEN = False`.
2. **New module `campaign_folders.py`:**
   - `folder_name(display_name, room=80, byte_room=200) -> str`: a copy of `_v11_folder_name`. It imports nothing from `db.py`, so the frozen copy stays separate. `campaign_folders` imports `transcript_store` (for `TEMP_PREFIX`) only inside functions, because `transcript_store` imports `campaign_folders` from Phase 2a.
   - `journal_name(folder) -> str`: `f"{folder} Journal.md"`.
   - `unique_folder(display_name, conn=None, *, exclude_id=None) -> str`: v11's suffix rule, used only by `create_campaign` until Phase 7 replaces it with a refusal. It compares casefold against every other campaign's `folder` and `folder_pending`.
   - `class FolderTakenError(Exception)`.
   - **`ensure_folder(campaign_id, *, data_dir=None, output_dir=None) -> Path`** takes **no** `conn`: callers call it with no transaction open (it opens its own short one for the claim, and from Phase 7 may finish a folder rename). `output_dir` defaults to `get_output_root()`. Checked in this order:
     1. **Output root absent** (an unmounted drive or vault) → raise `FileNotFoundError`. Nothing here passes `parents=True`, so wisper never creates the output root itself.
     2. **Pending rename:** if `folder_pending` is set, raise `FolderPendingError(folder)`. Phase 7 makes this try `finish_folder_rename` first.
     3. **Claimed:** if `folder_claimed = 1`: the folder exists → return `output / folder`; it doesn't → raise `FolderMissingError(folder)`. A claimed folder that vanished (renamed or deleted outside wisper, or a drive remounted empty) is never recreated here.
     4. **Unclaimed:**
        - absent → `mkdir(exist_ok=False)`; on `FileExistsError` (it appeared meanwhile) fall through to the next two checks. Then claim it and return it;
        - an empty directory → claim it;
        - a directory that holds only `journal_name(folder)` **whose frontmatter has `type: campaign-journal`** (what `render_journal` writes) → claim it (a keep-files delete left it); a user's own note of that name doesn't qualify;
        - anything else → raise `FolderTakenError(folder)`.
        - Any other `OSError` from `mkdir` (e.g. `ENAMETOOLONG`, `EACCES`) → `FolderTakenError(folder)` with the errno logged.
     - **Claiming is a compare-and-swap:** `UPDATE campaigns SET folder_claimed = 1 WHERE id = ? AND folder = :folder COLLATE BINARY AND folder_pending IS NULL`, where `:folder` is the name it just checked on disk. 0 rows → re-read the row and run the checks once more; still 0 → raise `FolderPendingError` if a rename is pending, else `FolderTakenError`. `claim_folder` and `recreate_folder` use the same statement.

        "Empty" ignores OS clutter: `.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`, and `TEMP_PREFIX` files (`is_clutter(name) -> bool`, also used by Phase 7's `check_available`).
   - **`recreate_folder(campaign_id, …) -> Path`:** for a claimed campaign whose folder is gone and whose output root exists, `mkdir` it. Only the Needs attention **Recreate folder** action (Phase 4) calls it.
   - **`claim_folder(campaign_id, conn=None, …) -> None`:** sets `folder_claimed = 1` on an existing non-empty folder. Only the Needs attention "Use this folder" action (Phase 4) calls it, after the user confirms that every `.md` in it becomes a session.
   - `is_claimed(campaign_id, conn=None) -> bool`.
   - `class FolderPendingError(FolderTakenError)` and `class FolderMissingError(FolderTakenError)`: callers that refuse on `FolderTakenError` refuse on both; routes map them to `folder_pending` and `folder_missing` codes.
   - `is_reserved(name) -> bool`: the part before the first dot, right-stripped, upper-cased, is in the reserved set (the same set as `_V11_RESERVED`). Phase 6's `validate_new_stem` and Phase 5's upload name check use it.
   - New `tests/test_campaign_folders.py`.
3. **`Campaign` gains `id: int = 0`, `folder: str = ""`, and `transcript_ids: list[int]`** (same order as `transcripts`). `load_campaigns` fills them.
4. **`campaign_manager` against `transcripts`:**
   - **`_transcript_id(conn, campaign_id, stem) -> int`:** read-only, `SELECT id FROM transcripts WHERE campaign_id = ? AND stem = ?`. `KeyError` if absent; it never calls `ensure_row`. The campaign-scoped functions use it: reorder, set order, journal stems.
     - `move_transcript_to_campaign(stem, slug)` keeps today's create-if-absent behaviour until Phase 2b: it finds the row with `WHERE stem = ?` (`LIMIT 2`, `ValueError` when ambiguous), or creates it with `ensure_row(conn, stem)` when there is none, then assigns it. Over 100 tests rely on this.
   - **`_write_order(conn, cid, tids)`:**
     - shift every row of `cid` by `max(position) + 1 + len(tids)`;
     - then, for each `(pos, tid)`, `UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?`;
     - rows of `cid` not in `tids` are unassigned (`NULL, NULL`). Phase 6 restricts this function to reordering.

     A session moving in from another campaign changes `campaign_id` in that update, and `transcripts_campaign_bu` deletes its journal entry. Keep the docstring's two-step reason.
   - **The other functions:** `_stems`, `remove_transcript_from_campaign`, `reorder_campaign_transcript`, `set_campaign_transcript_order`, `get_campaign_for_transcript`, `get_transcripts_for_campaign`, and `load_campaigns` read and write `transcripts.campaign_id`/`position`. Appending uses `(SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?)`.
   - **`create_campaign`** sets `folder = unique_folder(display_name, conn)`, and creates no directory.
   - **`delete_campaign`:** in the transaction that deletes the campaign, first `UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE campaign_id = ?`, since the schema refuses the delete otherwise. Phase 7 replaces it.
     - It deletes only journal paths read from the registry (`paths_for_delete`). The `journal_path(slug)` fallback at `campaign_manager.py:226-230` goes: after v11 that path can be a user's own note in a folder wisper never claimed.
     - After commit it also unlinks `legacy_journal_path(slug)`, its pending file, and `journal.md.v11-adopted` (wisper's own data dir), so a later campaign with the same slug doesn't adopt a deleted campaign's journal.
   - **Interim name clash (until Phases 6–7 replace these paths):** from Phase 4 on, two campaigns, or a campaign and the root, can hold the same stem. So the unassign in `delete_campaign`, `remove_transcript_from_campaign`, and `_write_order`'s drop can hit `transcripts_root_stem`, and `move_transcript_to_campaign` into a campaign that already has the stem can hit `transcripts_stem`. Each catches that `sqlite3.IntegrityError`, rolls back, and raises `ValueError(f"a session named {stem!r} is already there")`. The callers show the existing generic error flash or a `ClickException`: `web/routes/transcripts.py` ~390 already catches `Exception`; **add** `except ValueError` at `web/routes/transcripts.py` ~768 and `cli.py` ~1377 (they catch only `KeyError` today) and in the campaign routes that call these functions. Tests seed both clashes and check the refusal.
   - `delete_campaign`'s `path.parent.rmdir()` loop (~237–241) goes: after v11 the parent is a campaign folder, and removing folders is Phase 7's job (claimed folders only).
5. **Every other `campaign_transcripts` query** (`grep -n campaign_transcripts src/` minus `db.py` and `legacy_import.py`: `journal.py` 254, 420; `search_index.py` 581, 628; `job_history.py` 182; `recording_manager.py` 127; `transcript_store.py` 758, 780, 831, 913, 1028) joins `transcripts.campaign_id` instead. The mechanical rule: `JOIN campaign_transcripts ct ON ct.transcript_id = t.id` → drop the join, `ct.campaign_id` → `t.campaign_id`, `ct.position` → `t.position`; a `LEFT JOIN … WHERE ct.campaign_id IS NULL` ("no campaign") → `t.campaign_id IS NULL`. The ones that need more than that:
   - `job_history._FROM`: `coalesce(j.campaign_id, t.campaign_id, rec.campaign_id, …)`;
   - `recording_manager._load`: join `transcripts t` before `campaigns c`, then `c.id = CASE WHEN r.transcript_id IS NOT NULL THEN t.campaign_id ELSE r.campaign_id END`;
   - `transcript_store.relink`'s "already linked" check: `campaign_id IS NOT NULL` on the new row, or a journal entry.

   Update the comments and docstrings naming `campaign_transcripts`: `campaign_manager.py` 8 (module docstring); `campaigns.py` 264, 303; `job_history.py` 178; `transcript_store.py` 1028.
6. **Journal location** (`journal.py`):
   - **`journal_path(slug, data_dir=None, *, conn=None, output_dir=None) -> Optional[Path]`** (`data_dir` stays second, so existing positional callers keep working; callers inside a transaction pass its `conn`; `output_dir` defaults to `get_output_root()`):
     - `None` for an invalid slug or a slug with no campaign;
     - the campaign's `journal` row path if registered;
     - else, **only when `folder_claimed = 1`**, `output / folder / journal_name(folder)`;
     - else `None`: wisper never reads, adopts, or deletes a file in a folder it hasn't claimed.
     - `output` is `output_dir`, or `get_output_root()`. The journal-location code (`journal_path`, adoption, `sync_journal`, `update_journal`, `reset_journal`) never calls `get_output_dir()` (`_summary_path`/`_transcript_path` at `journal.py` 119 and 124 still do until Phase 2b rewrites them), and every journal `file_registry.add_if_owned` (`journal.py` ~202, ~449) passes `output_dir=<that root>`: without it `file_registry._dirs` falls back to another root and `db.to_rel` raises `ValueError`.

     Every caller handles `None` as "no journal yet": `journal.py` 185, 235, 276, 367; `campaign_manager.py` 228 (removed above); `cli.py` 962; `web/routes/campaigns.py` 83, 404. Slug validation moves out of the path lookup: `reset_journal` and `export_journal` validate the slug (`ValueError("Invalid campaign slug")` as today) and then treat a `None` path as "no journal", never as an invalid slug.
   - **Order of calls** in `export_journal`, `update_journal`, and the campaign page: `adopt_legacy_journal` (no transaction) → for `update_journal` only, `ensure_folder` (no transaction) → `sync_journal` → `journal_path` read **after** sync (it can change from `None` to a path when adoption claims the folder) → for `update_journal`, the LLM call outside any transaction.

     `JOURNAL_FILENAME` goes, and `PENDING_SUFFIX` stays: the pending file sits beside the journal.
   - **`legacy_journal_path(slug, data_dir=None) -> Path`:** `get_campaigns_dir(data_dir) / slug / "journal.md"`.
   - **`adopt_legacy_journal(slug, data_dir=None, output_dir=None) -> str`.** Call it with no transaction open. It returns `"none" | "adopted" | "kept" | "folder_taken" | "unavailable" | "failed"`:
     1. No legacy `journal.md` and no legacy pending file → `"none"`.
     2. **Pick the file to adopt.** If a legacy pending file exists and its sha256 equals `campaigns.journal_sha256`, it's a fold that committed before its file move, so it is the journal; otherwise delete the pending file. (This is `sync_journal`'s rule, applied at the legacy location.)
     3. `ensure_folder(campaign_id)`. `FolderTakenError` (including pending and missing) → `"folder_taken"`; `FileNotFoundError` (output root absent) → `"unavailable"`; nothing moved either way.
     4. **Target exists already:**
        - its sha256 equals the chosen legacy file's, or `campaigns.journal_sha256` → a previous adoption crashed after `os.replace` and before the legacy file was set aside: register it, set the legacy file aside (below), return `"adopted"`;
        - otherwise → `"kept"`: log a warning, change nothing.
     5. **Copy across volumes** (the data dir and output root are separate Docker bind mounts, or separate Windows drives):
        - copy to a `TEMP_PREFIX` temp file in the target folder, `fsync`, and verify its sha256 equals the source's;
        - `os.replace` it to the target;
        - register it (`add_if_owned`, kind `journal`, owner the campaign);
        - then set the legacy file aside: `os.replace(journal.md, journal.md.v11-adopted)` (same directory, same volume), and unlink the legacy pending file. The `.v11-adopted` copy is what a rollback restores (Decisions).
        - Any `OSError` → `"failed"`, leaving the legacy file in place (and removing the temp file).
     6. Return `"adopted"`.
   - **`sync_journal(slug)`** calls `adopt_legacy_journal` **before** opening its transaction. Then:
     - if the legacy file still exists (any result other than `"none"`/`"adopted"`), it returns at once;
     - if `journal_path` is `None` (unclaimed folder), or the output root (`get_output_root()`) or the campaign folder doesn't exist, it returns at once: an absent folder means "unavailable", not "deleted";
     - only when the claimed folder exists and the journal file doesn't does the existing "journal deleted → reset" branch run.
   - **`reset_journal`** deletes the journal and pending file only when `journal_path` is not `None`, and always deletes the legacy file, its pending file, and `journal.md.v11-adopted`, so a rebuild never re-adopts the old text.
   - **`update_journal`** calls `adopt_legacy_journal` first and continues only on `"none"` or `"adopted"`; otherwise it fails the job with a fixed code: `journal_folder_taken` for `"folder_taken"`, `journal_output_unavailable` for `"unavailable"`, `journal_legacy_pending` for `"kept"` or `"failed"`.
   - **`adopt_legacy_journals(data_dir=None, output_dir=None) -> dict[str, str]`** runs it for every campaign, catching and logging any exception per campaign as `"failed"`, so startup never fails on it. It logs non-`none` results.
   - **Startup order** in `web/app.py`'s lifespan (~198–224): `migrate` → `_report_upgrade` → `job_history.mark_interrupted` → `adopt_legacy_journals` → (Phase 7) `finish_pending_renames` → `reconcile`. `mark_interrupted` comes first so crash-leftover job rows don't read as busy.
   - **Needs attention text for `"kept"`:** "The journal for X wasn't moved: `<folder>/<folder> Journal.md` already exists and differs from `<data>/campaigns/<slug>/journal.md`. Keep the one you want and delete the other." (Phase 4 shows it.)
   - **Pending-file location:** `update_journal` writes its pending file beside the target, so it calls `ensure_folder` first (see the order above). `FolderTakenError` (including pending and missing) fails the journal job with `journal_folder_taken`; `FileNotFoundError` (output root absent) with `journal_output_unavailable`. The target is `journal_path(...)` read after `ensure_folder`, which is the registered row or `<folder>/<folder> Journal.md`.
   - `journaled_stems` (254) and the 420 query order by `t.position` from `transcripts`.
7. **`file_registry`:**
   - `ROOT_OF_KIND["journal"] = "output"`: add a separate `_OUTPUT_ROOT_KINDS = _OUTPUT_KINDS | {"journal"}` for `ROOT_OF_KIND` only. `_OUTPUT_KINDS` itself is unchanged, because `_OWNER_OF_KIND` spreads it as transcript-owned; `_OWNER_OF_KIND["journal"]` stays `"campaign"`.
   - `_scan_data` drops the `campaigns/*/journal.md` scan.
   - `_sync` adds, for each campaign with `folder_claimed = 1` and `folder_pending IS NULL`: if `output / folder / journal_name(folder)` is a file, a `_Found("journal", path, "output", Owner("campaign", id))`.
8. **Seeds and raw inserts:**
   - `tests/_seed.save_campaigns`:
     - new campaigns get `folder = campaign_folders.unique_folder(display_name, conn)`;
     - before deleting a campaign it unassigns that campaign's transcripts (`campaign_id = NULL, position = NULL`);
     - it assigns members with its own copy of `_write_order`'s two steps, not through `cm._transcript_id`/`_write_order` (which become read-only and reorder-only), so re-saving a campaign in a new order never collides with `UNIQUE (campaign_id, position)`:
       1. `shift = coalesce(max(position), -1) + 1 + len(stems)` (in SQL; an empty campaign has no max); `UPDATE transcripts SET position = position + shift WHERE campaign_id = ?`;
       2. for each `(pos, stem)`: find the row by stem in **any** campaign or the root (`WHERE stem = ?`, `LIMIT 2`, `ValueError` when two match), else `ensure_row(conn, stem)` (root); then `UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?`;
       3. `UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE campaign_id = ? AND position >= shift`: rows of the campaign that aren't listed are unassigned.
   - Add `seed_campaign(display_name, slug=None, *, claimed=False, data_dir=None) -> int`. Without `slug` it wraps `create_campaign`; with one it inserts the row directly (`slug`, `display_name`, `folder = unique_folder(...)`, `created_at`), since `create_campaign` always derives the slug. With `claimed`, it also calls `ensure_folder`. Returns the id.
   - Add `seed_transcript(stem, *, campaign=None, write_md=False, data_dir=None, output_dir=None) -> int`: find the row by stem in any campaign or the root with its own `WHERE stem = ?` (`LIMIT 2`, `ValueError` when two match), else `ensure_row(conn, stem)` (root); then assign to campaign slug `campaign` (appended position) with a direct `UPDATE`. With `write_md`, it writes `<stem>.md` in the output root and registers it (`file_registry.add`), which is where every session lives until Phase 5. Returns the id.
   - The raw `INSERT INTO campaigns` statements in `tests/test_schema.py` (~28, ~92) and `tests/_seed.py` (~152) gain a `folder` value. The "campaign slug unique" negative case gives its second row a distinct `folder`, so it fails on the slug and not on the folder.

**Existing tests to rewrite** (find with `grep -n "campaign_transcripts\|journal.md\|get_campaigns_dir\|journal_path\|INSERT INTO campaigns" tests/*.py`):
- `tests/test_schema.py`: the `campaign_transcripts` constraint cases (~32, 90, 107–115, 264, 293–301) become the same rules on `transcripts`.
- `tests/test_db.py` ~435–484 (`test_upgrade_v8_to_latest_moves_audio_paths_into_files`): `db.migrate() == [9, 10]` → `[9, 10, 11]`; its assertion that the `files` rows joined to `transcripts` are exactly the 2 `audio` rows gains the backfilled stat-less `transcript` row per transcript; `expected_schema(10)` (~458) → `expected_schema(11)`; ~484 likewise. `tests/test_web_routes.py` ~3725–3760 (`_start_after_v8`): the upgrade runs through v11.
- `tests/test_journal.py`'s `out_dir` fixture (~49–55) sets `WISPER_OUTPUT_DIR` (Test rules).
- `tests/test_schema.py:69`: the `journal` `files` row `campaigns/game/journal.md` in the data root becomes `<folder>/<folder> Journal.md` in the output root.
- `tests/test_schema.py` cases `"one campaign per transcript"` and `"position unique"` (and every other insert into `campaign_transcripts`; find them with `grep -n campaign_transcripts tests/test_schema.py`) become the same rules on `transcripts.campaign_id`/`position`.
- `tests/test_schema.py:103` (`"stem unique"`): the fixture's `s1` is in campaign 1, so a second root `s1` is legal at v11. It becomes two cases: a duplicate root stem is refused, and a duplicate stem within one campaign is refused.
- `tests/test_campaign_manager.py` ~578–594; `tests/test_e2e.py` ~146, ~182; `tests/test_journal.py` ~810.
- Journal-path tests in `test_journal.py`, `test_file_registry.py`, `test_web_routes.py`, `test_campaign_manager.py`, and `test_legacy_import.py` expect the journal in the campaign folder.
  - `test_journal.py:70–76`: `journal_path` for a slug with no campaign returns `None`.
  - `test_legacy_import`'s journal import runs at v3, so its *imported entries* are unchanged; only a path assertion moves.

**New tests:**
- All "Tests for v11" in Schema v11.
- **`test_campaign_folders.py`:**
  - the sanitizer table: every rule, including `con.txt` → `con_.txt`, `. Hidden Tomb`, ` .. x .. `, an 85-character name, `room=76`, NFD input, and DEL;
  - `unique_folder` with case-only and Unicode-case twins (`ÉTÉ`/`été`) and with long names;
  - `journal_name`;
  - **`ensure_folder`:** absent → created and claimed; empty → claimed; holds only its journal → claimed; holds a user `.md` → `FolderTakenError`, unclaimed, nothing written; claimed → no checks.
- **Journal adoption:**
  - a legacy journal with two folded sessions moves on the first `unjournalled_sessions` call; its entries and hash survive, and the `files` row points at the new path;
  - `os.replace` patched to raise `OSError(errno.EXDEV)` the first time still adopts (copy path), or, if the copy fails too, the entries are **not** reset and the legacy file stays;
  - a legacy pending file whose hash matches is the one adopted;
  - a non-matching pending file is deleted;
  - both files present and different → `"kept"`;
  - both present and the target's hash equals the legacy file's (a crash after `os.replace`) → `"adopted"`, legacy file removed;
  - `update_journal` with a `"kept"` legacy journal fails with `journal_legacy_pending` and changes nothing;
  - a foreign folder → `"folder_taken"`, nothing written into it;
  - `reset_journal` removes the legacy file.
- **Unclaimed folders are never touched:** a user's own `Hanataz/Hanataz Journal.md` in an unclaimed folder survives a campaign-page load (`sync_journal`), a rebuild (`reset_journal`), and `delete_campaign`; `journal_path` returns `None`.
- **Unavailable output root:** with the claimed folder (or the output root) removed, `sync_journal` keeps `journal_entries` and `journal_sha256`; with the folder present and only the journal file gone, it resets.
- **`ensure_folder`:** the output root absent → `FileNotFoundError` and nothing created, **through the default path** (no `output_dir` argument, `WISPER_OUTPUT_DIR` pointing at a removed directory); a folder holding only `.DS_Store` and `desktop.ini` is claimed; `folder_pending` set → `FolderPendingError`; claimed but deleted → `FolderMissingError`, nothing created.
- **Adoption with the output root absent** → `"unavailable"`, legacy file untouched; `adopt_legacy_journals` with one campaign raising still returns results for the others.
- **Set aside:** after adoption, `<data>/campaigns/<slug>/journal.md.v11-adopted` holds the old text and `journal.md` is gone.
- **Deleting an unadopted campaign** removes its legacy journal; a new campaign with that slug starts with no journal.
- **`save_campaigns`** re-saving a campaign in reverse order, and with one stem dropped, leaves exactly the listed order and unassigns the dropped stem; saving an empty campaign works.
- **Claim CAS:** a rename committed between `ensure_folder`'s disk check and its claim → no claim on the stale name.
- **Journal-only re-claim:** a folder holding only a user's `X Journal.md` without wisper's frontmatter is `FolderTakenError`.
- **Move-in clash:** `move_transcript_to_campaign` into a campaign that already has that stem raises `ValueError`, and the route shows the error flash.
- **`export_journal`** on a migrated campaign with a legacy journal returns the adopted text on the first call; `reset_journal` on a campaign with no journal yet doesn't raise.
- **Campaign moves:** moving into another campaign via `move_transcript_to_campaign` deletes the journal entry and marks the old campaign's journal stale; a reorder doesn't.
- **`delete_campaign` keep-files** leaves its transcripts with `campaign_id IS NULL`.

**Docs:**
- **`architecture.md`:**
  - the schema section (v11 tables, the campaign assignment on `transcripts`, `folder_claimed`);
  - the journal's location and adoption;
  - the Module Map line for `campaign_folders`;
  - Known Constraints: `transcripts.campaign_id` restricts a campaign delete.
- `docs/configuration.md`: the data storage table, where the journal moves from `campaigns/<slug>/journal.md` to the campaign folder in the transcripts folder; and a "Going back to an older version" section: restore `backups/wisper-v10-*.db` as `wisper.db`, rename each `campaigns/<slug>/journal.md.v11-adopted` back to `journal.md`, and move session files from campaign folders back into the output root.
- **`docs/docker.md` and the `docker-compose.yml` volume comment** ("config, wisper.db, voice samples, journals"): journals live in `./output`, so back up both.
- `docs/web-ui.md` and `docs/cli-reference.md`, wherever they name the journal's path.

**Done when:**
- `pytest tests/test_db.py tests/test_schema.py tests/test_campaign_manager.py tests/test_campaign_folders.py tests/test_journal.py tests/test_file_registry.py tests/test_e2e.py tests/test_legacy_import.py tests/test_job_history.py tests/test_recording_manager.py tests/test_search_index.py` pass, and the full suite is green.
- `grep -rn campaign_transcripts src/` shows only `db.py` (v2/v3 DDL, v11) and `legacy_import.py`.
- `tests/test_campaign_folders.py` is in CI's `windows storage` list.
- **Rehearsal:**
  - on a scratch copy, the Mac data migrates v10 → v11, and `wisper db status` shows integrity ok and no drift;
  - the Campaigns and Journal pages render;
  - a legacy journal seeded into the copy moves into `Impossible Landscapes/`, which is then claimed.

---

### Phase 2a — Location API, no callers changed

**Goal:** the lookups every later phase uses, fully tested. Nothing calls them yet, so the suite stays green with no other edits.

*Done (opencode). Nothing in the code disagreed with the plan. Two readings of the spec: `Located.companions` holds every non-`transcript` `files` row keyed `(kind, label or "")` (the `transcript` row is `md`), and `target_blocked` shares `campaign_folders.holds_only_wisper` with the claim check (imported inside the function, since `campaign_folders` imports this module). Claude review: stem and path lookups now use the indexes (`_stem_row`, `file_registry._find_by_path`) instead of scanning every row.*

**Read first:**
- `transcript_store.py`: `safe_path` (185), `existing_form` (171), `_is_case_insensitive` (423)
- `file_registry.py`: `_find_by_path` (236), `file_for` (415), `files_for` (403), `_fold`/`_key` (83–100), `_dirs` (113)
- `campaign_folders.py` (Phase 1)

**Steps** (a new "Locations" section in `transcript_store.py`):
1. **The `Located` dataclass:**
   ```python
   @dataclass(frozen=True)
   class Located:
       id: int
       stem: str                   # transcripts.stem (NFC)
       campaign_id: Optional[int]
       expected_dir: Path          # output root, or output root / campaigns.folder
       md: Path                    # its `transcript` files row; else expected_dir / f"{stem}.md"
       missing: bool               # missing_since IS NOT NULL
       companions: dict            # (kind, label or "") -> Path, from its files rows
       target_blocked: bool        # its campaign's folder is taken, pending, or claimed-and-missing
       fold: bool                  # the output root's file_registry._fold
       @property
       def dir(self) -> Path: ...  # md.parent
       @property
       def misplaced(self) -> bool: ...
       def companion(self, suffix: str) -> Path: ...
   ```
   - **`md`:** v11 backfills a `transcript` row for every transcript, and every write registers one, so the fallback is reached only by a row created without a file (`ensure_row` for a missing session, test seeds).
   - **`misplaced`:** false when `target_blocked` (the target is a campaign folder that can't be written to now: unclaimed and existing non-empty, i.e. folder taken; `folder_pending` set; or claimed and missing); those states have their own Needs attention entries. `locate` fills `target_blocked` and `fold` (a property can't query the database). Otherwise true when `dir` differs from `expected_dir`, **or** any registered companion's directory differs from `dir` (a move that left a locked `.flac` behind). Directories compare by `realpath`, then `file_registry._key(…, fold)` with the output root's `_fold`. `os.path.normcase` is a no-op on macOS, so it isn't used.
   - **`companion(suffix)`:**
     - the registered path when a row of the matching kind exists (`.summary.md` → summary, `_diar.json` → sidecar, `.flac` → audio, `.md.bak` → backup). For `.flac` it returns the registered `audio` row whatever its suffix (v9 registered audio such as `<stem>.mp4`);
     - else `safe_path(stem, suffix, dir)`;
     - `ValueError` if that is `None`.

     Registered rows win, so a partial rename or move still finds its files.
2. **`locate(transcript_id, conn=None, *, data_dir=None, output_dir=None) -> Optional[Located]`:** one query joining `transcripts`, `campaigns`, and the transcript's `files` rows.
3. **`locate_path(md_path, conn=None, *, data_dir=None, output_dir=None) -> Optional[Located]`:**
   1. the `transcript` row in `files` whose `rel_path`, relative to the output root, matches (NFC, and casefold when `_fold`);
   2. else, when `dir_campaign(md_path.parent)` is a transcript folder, the row with that `campaign_id` (`IS NULL` for the root) and stem, **but only if that row has no `transcript` files row** (a row whose registered `.md` is some other file isn't this file's session: this file is a newcomer);
   3. else `None`.
4. **`dir_campaign(directory, conn=None, *, data_dir=None, output_dir=None) -> tuple[bool, Optional[int]]`:**
   - `(True, None)` for the output root;
   - `(True, id)` for a **claimed** campaign's `folder`;
   - `(True, id)` for its `folder_pending`, but only when `folder` doesn't exist on disk, or both are the same directory (`os.path.samefile`);
   - `(False, None)` otherwise.

   Compare with `realpath` + `_key(fold)`.
5. **`expected_dir(campaign_id, conn=None, *, output_dir=None) -> Path`:** no mkdir.
6. **`find_by_stem(stem, conn=None, *, campaign_id=ANY, present_only=False, data_dir=None, output_dir=None) -> list[Located]`:**
   - NFC match first, then casefold when `_fold`;
   - "present" means `missing_since IS NULL` (no disk check);
   - every Locations function's `output_dir` defaults to `config.get_output_root()` and `data_dir` to `get_data_dir()`;
   - `ANY: Final = object()` is defined once, in `transcript_store`, meaning any campaign; `None` means the root. Every other `campaign_id=` default that means "any" (`list_transcripts`, `_seed.transcript_id`) imports this same object.

**New tests (`tests/test_transcript_store.py`):**
- `locate` for a root and a campaign session.
- `misplaced` is true for a campaign session whose `.md` is in the root (the state v11 leaves), and false when it differs only in case on a case-insensitive filesystem.
- `locate_path` by registered path, by folder + stem without a `files` row, NFD spelling, and case-folded.
- `locate_path` of `<folder>/S.md` when the campaign's row `S` is registered at `S.md` in the root returns `None` (a newcomer), not that row.
- `dir_campaign` for:
  - the root;
  - a claimed folder, and an unclaimed folder with the right name;
  - a pending folder, while the old one exists and once it's gone;
  - an unrelated subfolder, and a path outside.
- `find_by_stem` with two campaigns sharing a stem.
- `companion` prefers the registered path over the derived one.

Seed rows and files by hand: writes into folders arrive in Phase 5.

**Docs:** `architecture.md`, the transcript_store section, gets "Locations": `Located`, `locate`, `locate_path`, `dir_campaign`, the misplaced state, and why the registry is the source of truth.

**Done when:**
- `pytest tests/test_transcript_store.py` passes, and the full suite is green.
- No existing caller changed.

---

### Phase 2b — Every caller resolves through the location API

*Done (opencode, three hand-offs). Claude's review fixed `register`'s stale-sidecar fallback (it looked in the root, so a campaign-folder re-transcribe could delete a same-named root session's sidecar) and a test that set `WISPER_OUTPUT_DIR` without `monkeypatch`. Rehearsed on a scratch copy of the Mac data: every page 200, including a transcript and its edit page.*

**Goal:**
- Nothing outside `transcript_store`, `file_registry`, and `campaign_folders` builds `<output root>/<stem><suffix>` or looks a session up by bare stem.
- All sessions are still in the output root (or misplaced there), so behaviour is unchanged.
- Web routes keep their `{name}` URLs until Phase 3.

**Read first:**
- `transcript_store.py`: `ensure_row` (221), `register` (240), `_companion_paths` (308), `delete_transcript` (370), `rename_companions` (657), `relink` (723), `relink_candidates` (774), `read_sidecar` (884), `audio_path` (951), `set_audio` (991), `write_sidecar` (1022), `_register_summary` (151), `_refresh_transcript_row` (138)
- `file_registry.Owner.for_stem` (157)
- Every call site in the Findings inventory, plus those in step 4

**Hand-offs:** (i) steps 1–2 plus `search_index`, `journal`, `speaker_registry`; (ii) step 3 plus `job_history`, `recording_manager`, `record`, `web/jobs.py`, `storage_trim`, `pipeline`; (iii) `cli.py` and the routes. Each hand-off rewrites the tests of the modules it changed. One commit after (iii).

**Steps:**
1. **Keyed by id or path:**
   - **`delete_transcript(transcript_id, data_dir=None, output_dir=None) -> Literal["deleted", "kept", "absent"]`:**
     - `"kept"` when the `.md` couldn't be unlinked (row kept);
     - `"absent"` when there's no such row.

     Companions come from `_companion_paths(loc)`, whose derived names and excerpt glob are in `loc.dir`. `DELETE … WHERE id = ?`.
   - **`_row_for_path(conn, md_path, *, data_dir=None, output_dir=None) -> Optional[int]`** (new, side-effect free, runs in the caller's `conn`):
     1. `locate_path(md_path, conn)`: an existing row (by registered path, or by folder + stem when that row has no registered `.md`) is that session, **whatever its campaign**. This is how a misplaced session re-transcribed in place keeps its row.
     2. Otherwise, when `dir_campaign(md_path.parent)` is a transcript folder: if a row `(that campaign, stem)` exists whose registered `.md` is a **different file that is present**, return `None` (a newcomer must never take over a misplaced session's row; the file shows under Needs attention). Else `ensure_row(conn, stem, campaign_id=<dir's>)`.
     3. Otherwise `None` (not a transcript folder).
   - **`register(md_path, *, origin, data_dir=None) -> Optional[int]`:** `_row_for_path` in its own transaction, then today's side effects for that row (clear speakers, forget the sidecar, mark the journal stale, reindex). `None` → nothing registered, logged. Its paths come from `md_path` and the row, never from the output root: the stale sidecar is the path `forget_kind` returns for that row (or `loc.companion("_diar.json")`), not `safe_path(stem, SIDECAR_SUFFIX)` (~278), and the `.md` is `md_path`, not `safe_path(stem, ".md")` (~281).
   - **`set_audio`, `write_sidecar`, `set_speaker_names`, `set_speaker_embeddings`** (and ~564) call `_row_for_path(conn, md_path)` inside their own transaction where they call `ensure_row(conn, stem)` today (~1003, 1057, 1098, 1121). They never call `register`, which opens a second transaction and would clear the speakers they just wrote.
   - **`ensure_row(conn, stem, output_dir=None, *, campaign_id=None) -> int`:** keys by `(campaign_id, stem)`. A new row in a campaign gets `position = max + 1`; one in the root gets `NULL`. Its `missing_since` check looks for `expected_dir(campaign_id) / f"{stem}.md"`, not the output root.
   - **`rename_companions(transcript_id, old_stem, new_stem, *, src_dir, dst_dir=None, data_dir=None, output_dir=None) -> list[Path]`** (`output_dir` is the root, for `file_registry`):
     - scans `src_dir` for unregistered companions of `old_stem`;
     - moves each registered or found file whose name starts with `old_stem` to `(dst_dir or src_dir) / (new_stem + tail)`, where `tail` is the name after `old_stem`;
     - with a `dst_dir`, also moves every other registered row of the transcript (one kept under an older name by an earlier partial rename), keeping its file name;
     - the early `if old_stem == new_stem: return []` (~688) applies only when there's no `dst_dir`.
   - **`_companion_paths(loc, conn=None) -> list[Path]`** replaces `_companion_paths(md_path, output_dir, conn, data_dir)`.
   - **`relink(old_id, new_md, data_dir=None) -> list[Path]`** and **`relink_candidates() -> list[Located]`.** `MissingTranscript` gains `id: int`. Callers: `web/routes/transcripts.py` 316 and 408, `web/routes/campaigns.py` 121 and 476–481 (they resolve the posted name to `old_id` and the new file's path until Phase 3 switches the forms to ids).
   - **The `md_path` functions** keep that parameter but resolve the owner with `locate_path` instead of `Owner.for_stem` + `output_dir=md.parent`: `audio_path`, `set_audio`, `read_sidecar`, `write_sidecar`, `set_speaker_names`, `set_speaker_embeddings`, `_refresh_transcript_row`, `_register_summary`.
     - Where one creates a row today (`ensure_row(conn, stem)` in `set_audio`, `write_sidecar`, `set_speaker_names`, `set_speaker_embeddings` at `transcript_store.py` ~1003, 1057, 1098, 1121, and ~564), it uses `_row_for_path(conn, md_path)` in its own transaction (step 1 above; never `register`), which supplies the campaign from `dir_campaign`; `None` → the function does nothing to the database and logs. A bare `ensure_row(conn, stem)` would key a campaign-folder `.md` to a wrong root row.
   - **The output root, not the `.md`'s folder, goes to `file_registry`** (run rule "Output root resolution"). Today's sites passing the folder: `transcript_store.py` 285 (`add_if_owned(..., output_dir=md.parent)`), 962 and 1001 (`output_dir = md_path.parent if output_dir is None`); `web/jobs.py` 225–233 and 399–405 (`output_dir=out_dir`); `web/routes/transcripts.py` 159–162 and 807–817. Each passes `get_output_root()` (or the caller's root parameter). `audio_path`/`set_audio` drop the `md_path.parent` default.
2. **`file_registry`:** remove `Owner.for_stem`, and add `Owner.for_path(md_path, conn=None, *, data_dir=None, output_dir=None)`. It's a thin `locate_path` wrapper, imported inside the function because `transcript_store` imports `file_registry`.
3. **`campaign_manager`:**
   - `move_transcript_to_campaign(transcript_id, slug)`, `remove_transcript_from_campaign(transcript_id)`, `get_campaign_for_transcript(transcript_id)`. With an id there's nothing to create: an unknown id raises `KeyError`;
   - campaign-scoped functions keep `(slug, stem)` and resolve through `_transcript_id(conn, cid, stem)` (Phase 1).
4. **Callers** (Findings inventory plus these):
   - **`search_index`:**
     - `reindex(transcript_id)`; `_paths(loc)`; `reindex_path` via `locate_path`;
     - `check_freshness` builds `Located` from one joined query, not one call per row;
     - `ResultGroup.transcript_id`.
   - **`journal`:** `_summary_path` and `_transcript_path` take `(campaign_id, stem)` → `find_by_stem(stem, campaign_id=cid)`.
   - **`speaker_registry.relabel_campaign`:** paths from `find_by_stem(stem, campaign_id=cid)`.
   - **`job_history._subject_ids`:** `locate_path`.
   - **`recording_manager`:**
     - add `Recording.transcript_id: Optional[int]`; `_load` fills it from `recordings.transcript_id`, and `Recording.transcript_path` from `locate(transcript_id).md`;
     - `save_recording` writes `rec.transcript_id` as is and never creates a transcript row (today it derives one from `transcript_path` via `ensure_row`, which on a stale path would create a phantom row);
     - `link_transcript(recording_id, transcript_id: int, data_dir=None)` replaces `link_transcript(recording_id, transcript_path, …)` (~763); its callers pass the id `register` returned;
     - tests building `Recording(transcript_path=…)` (`test_web_jobs.py`, `test_legacy_import.py`) set `transcript_id` from a seeded row;
     - `_transcript_id` is deleted.
   - **`record._purge_recording_files`** (`web/routes/record.py` ~548–550): `delete_transcript(recording.transcript_id)` when set, with no path check (a linked transcript is always wisper's).
   - **`campaign_manager.delete_campaign`** (~218, `delete_transcript(stem, …)`): passes the id.
   - **`search_index._paths` and `transcript_store._reindex(stem)`:** take a `Located` / an id.
   - **`web/jobs.py`:**
     - `_keep_audio` and the excerpt registration (~399) via `locate_path(output_path)`;
     - the backup (~1686) via `Owner.for_path`;
     - the enroll follow-up (~1539) gets the campaign from `get_campaign_for_transcript(loc.id)`.
   - **`storage_trim`:** `Action.transcript_id`; `_convert` targets `loc.companion(".flac")`; `apply` uses `loc.md`.
   - **`cli.py`:**
     - the journal rebuild's summary count (~1216) via `find_by_stem(…, campaign_id=…)`;
     - backup registration (~1540, ~1635) via `Owner.for_path`;
     - `transcripts move` (~1352–1377) resolves its stem argument with `find_by_stem`, and refuses with "name is in several campaigns" when more than one matches (possible from Phase 4 on; Phase 6 adds `--from`). No match → `ClickException("No transcript named X. Run `wisper transcripts list`; a file just added is picked up by the next scan.")` (today it creates the row).
   - **`pipeline`:** `_under_output_root` and `_campaign_note` via `dir_campaign`/`locate_path`; the post-write `register` and `move_transcript_to_campaign` (687–688) pass the id `register` returns.
   - **Routes:** `dashboard.py` (~60), `transcribe.py` (~194–232: `_name_clashes`, `name_check`), `campaigns.py` (~283), and **`web/routes/transcripts.py`** (162, 364, 390, 392, 486, 554, 768, 776, 807, 826: `Owner.for_stem`, `delete_transcript(stem)`, `move_transcript_to_campaign(stem)`, …) call the id or path APIs. Resolve with `locate_path(md_path)` from the current `{name}` handling.
5. **Rule:**
   - `safe_path(stem, suffix, base_dir)` stays the name-validation guard for building a path *inside a known directory*.
   - The directory always comes from a `Located` (`loc.dir`) or `expected_dir`, never from `get_output_dir()` directly, except in the routes Phase 3 rewrites.

**Existing tests to rewrite:**
- Every test calling the changed signatures. **This grep is the authoritative list** (the files named below are examples; any file it finds may be edited this phase): `grep -ln "delete_transcript(\|register(\|rename_companions(\|relink(\|for_stem(\|reindex(\|move_transcript_to_campaign(\|remove_transcript_from_campaign(\|get_campaign_for_transcript(\|link_transcript(\|transcript_path=" tests/*.py`.
- `tests/_seed.py`: `seed_transcript` and `save_campaigns` keep their Phase 1 lookups (stem in any campaign); `transcript_id(stem, campaign_slug=ANY)` is added here.
- Each gets the id from a new seed helper: `tests/_seed.transcript_id(stem, *, campaign_slug=ANY, data_dir=None) -> int` (a lookup; `KeyError` if absent).
- The ~140 `move_transcript_to_campaign("stem", slug)` calls on stems with no row become `seed_transcript("stem", campaign=slug)` (Phase 1). Tests that assign a stem and then write its companions by hand in the output root (e.g. `test_journal.py:102-110`, `s1.summary.md`) use `seed_transcript(..., write_md=True)`, so the session's registered `.md` puts `loc.dir` in the root where the companions are.
- Expect mechanical edits in `test_transcript_store.py`, `test_search_index.py`, `test_campaign_manager.py`, `test_web_jobs.py`, `test_storage_trim.py`, `test_cli.py`, and `test_e2e.py`. A test that needs a *behaviour* change means a step was misread: stop.

**New tests:**
- `delete_transcript` returns `"kept"` when `Path.unlink` raises `PermissionError` for the `.md`, and `"absent"` for an unknown id.
- `delete_transcript(id)` deletes only that campaign's copy when two campaigns share a stem (rows and files seeded by hand).
- **`register` of a misplaced session's root `.md`** returns its existing campaign row; no second row is created and no `OwnershipConflict` is raised.
- `register` of a file in an unrelated folder returns `None`.
- `ensure_row` in a campaign appends a position.

- `file_registry._dirs` (~113) and `transcript_store`'s seven `output_dir = get_output_dir()` defaults (~198, 387, 500, 676, 739, 823, 851) use `config.get_output_root()` (resolving a path must not create the root). `reconcile` (~500) returns early, changing nothing, when the root isn't a directory; `register` returns `None` and logs.

**Docs:** remove statements that companion names are derived from the stem in the output root (`architecture.md`).

**Done when:**
- Every test file the grep above finds passes, and the full suite is green.
- `grep -rn "for_stem\|_under_output_root" src/` is empty.
- `grep -rnE "output_dir=(md|md_path|out)\.parent|parent if output_dir is None" src/` is empty, and the orchestrator checks by hand the named sites: `web/jobs.py` 225–233 and 399–405, `web/routes/transcripts.py` 159–162 and 807–817, and `write_sidecar`'s `output_dir = md_path.parent` (~1037), whose "audio is inside the `.md`'s folder" guard becomes "inside `loc.dir`" with the root passed separately to `file_registry`.
- Every `safe_path(` call in `src/` passes a directory argument (orchestrator checks the grep output by hand).
- **Registry paths in a folder:** a session seeded in `Folder/` (rows and files by hand) goes through `register`, `set_audio`, `write_sidecar`, and excerpt registration, and every resulting `rel_path` starts with `Folder/`.
- **No second transaction:** `set_speaker_names` on an existing session keeps its other speakers and doesn't block (run with a 1 s busy timeout).
- **Same stem, two places:** root session `S` and `Folder/S.md`; registering the folder one leaves the root `S_diar.json` in place.
- **Newcomer vs misplaced:** campaign B's `S` is registered at `S.md` in the root; `register(B/S.md)` returns `None` and B's row still points at the root file.
- **Partial-rename leftovers:** a registered sidecar named after an older stem moves with a `dst_dir` move.
- `grep -rn "get_output_dir()" src/wisper_transcribe` shows only:
  - `path_utils`, `db`, `config`, `job_history` (`output_root` param);
  - `search_index`'s defaults (214, 245, 293, 578) and `speaker_registry` (189);
  - `recording_manager` (155);
  - `web/jobs.py` (1237; Phase 5);
  - the route files (Phase 3);
  - `cli.py`'s output-root defaults;
  - `record.py` (972; Phase 5).
- **Rehearsal:** every page renders on the scratch copy, and a session's audio plays.

---

### Phase 3 — Transcript URLs by id; lists from the database

*Done (opencode, three hand-offs). Claude's review: the legacy-name chooser shows the stored stem instead of reflecting the URL's name. The test "campaign page shows a folder session as present and summarized" moves to Phase 4, since nothing scans campaign folders before it. Rehearsed on a scratch copy of the Mac data: every page 200, `/transcripts/<name>` redirects to its id, the Transcripts page renders in 13 ms.*

**Goal:**
- Transcript pages are `/transcripts/{id}/…`. Their path parameter is an integer, so no transcript route builds a path from user input.
- The transcript lists (Transcripts page, recent partial, dashboard, `wisper transcripts list`) come from the database, not from `glob("*.md")`.
- An old `/transcripts/{name}` link redirects when the name is unique.

**Read first:**
- `web/routes/transcripts.py` (whole file), `web/routes/campaigns.py` (whole file: the detail page's missing/summarized checks 62–125, remove and reorder 250–330, relink 452–490), `web/routes/transcribe.py` ~533, `web/routes/search.py`, `web/routes/dashboard.py` ~80–100, `web/routes/record.py` (recording detail context)
- The twelve templates in Findings; `static/app.js` (any `/transcripts/` fetch)
- `job_history.JobRecord` (150–170), and the in-memory `Job.output_path`
- `.claude/rules/web-security.md`; `tests/test_path_traversal.py`

**Hand-offs:** 3a is steps 1–3 (routes, legacy redirect, forms) and `test_path_traversal.py`/`test_owasp.py`; 3b is steps 4–7 (view models, job page, lists, campaign page, search) and the templates; 3c rewrites the remaining URL references in tests. One commit after 3c.

**Steps:**
1. **Routes** (`web/routes/transcripts.py`):
   - every `/{name}…` route becomes `/{transcript_id:int}…` (Starlette's `int` path convertor, not only a type annotation), so a non-integer segment doesn't match and falls through to the legacy route instead of returning 422;
   - `_located_or_redirect(transcript_id) -> Located | RedirectResponse`: a missing row redirects to `/transcripts?error=not_found`; a row flagged missing renders as today's missing state;
   - delete `_get_safe_content_path`;
   - the summary, sidecar, excerpt, and audio paths come from `loc.companion(...)` and the registry;
   - the excerpt route keeps validating `speaker_name` with today's guard, applied in `loc.dir`;
   - `transcribe.py` (~533, the enroll redirect) builds `/transcripts/{loc.id}`;
   - every `Location` header uses `loc.id` (an int).
2. **Legacy redirect.** `GET /transcripts/{name}` is declared **last** in the router, after every static and integer route:
   - `find_by_stem(nfc(name))` over present transcripts;
   - exactly one → 303 to `/transcripts/{loc.id}`;
   - several → a small page listing each with its campaign and link;
   - none → redirect to `/transcripts?error=not_found`.

   The name is used only as a bound SQL parameter. Starlette tries routes in order, so `/transcripts/12` hits the integer route. A transcript literally named `12` is reachable through the lists, not the legacy URL; that's accepted.
3. **Forms post ids:**
   - `bulk-delete` and `bulk-campaign` take `transcript_id` lists, parsed with `int()` and dropping bad values;
   - `relink` takes `old_id` and `new_id`; the route calls `relink(old_id, locate(new_id).md)`;
   - the campaign page's remove and reorder take `transcript_id` (ints), replacing the stem guards at `campaigns.py` 264–330. Each route calls `locate(transcript_id)` and refuses (`?error=not_found`) unless `loc.campaign_id` is this campaign's id. Remove calls `remove_transcript_from_campaign(loc.id)` (Phase 6 changes it to a move); reorder calls the slug-scoped `reorder_campaign_transcript(slug, loc.stem, …)`, which is unambiguous within one campaign;
   - the campaign relink route (`campaigns.py` ~452–490) takes `old_id`/`new_id`, like `/transcripts/relink`;
   - Needs attention's Forget keeps `files.id`.
4. **View models carry ids:**
   - `Campaign.transcript_ids` (added in Phase 1) is what the campaign templates link with;
   - the list items' `id`, `ResultGroup.transcript_id`, `Recording.transcript_id` (Phase 2b);
   - **`JobRecord.transcript_id: Optional[int]`** is new: `job_history.py` ~149–160 has only `transcript_stem`; add the field, select `j.transcript_id` in `_COLUMNS`, and fill it in `_record`;
   - the in-memory job page resolves `locate_path(job.output_path)` when it renders;
   - **`Job` gains `transcript_id: Optional[int] = None`** (`web/jobs.py` Job dataclass ~411) in this phase; Phase 5 fills it at submit;
   - **`JobRecord.output_path`** (`job_history.py` ~166–170, returns `f"{stem}.md"`) and the links built from it in `partials/job_rows.html` and `job_detail.html` ~135, 140 (`job.output_path | stem | urlencode`) link by `transcript_id` instead;
   - **the job page's completion links are built in JavaScript** from the SSE `done` event (`templates/job_detail.html` ~327–332, `/transcripts/${enc}` and `/transcripts/${enc}/summary`; payload at `web/routes/transcribe.py` ~352). The `done` payload gains `"transcript_id"`: `job.transcript_id` when set (Phase 5 sets it at submit), else `locate_path(Path(job.output_path)).id` resolved when the event is built, else `null`. The script links `/transcripts/${evt.transcript_id}` and `…/summary`, and shows no transcript buttons when it's `null`;
   - **job history's transcript filter** (`job_history.list_jobs(transcript=stem)`, `t.stem = ?` at ~219; `dashboard.py` ~183; the "Jobs" button in `transcript_detail.html:21`; the paging links in `job_history.html:60`) becomes `transcript_id: Optional[int]` (`t.id = ?`), query parameter `transcript_id`, parsed with `int()` and ignored when bad.

   Templates link `/transcripts/{{ x.id }}`; drop `| urlencode` on those links.
5. **Lists from the database:**
   - `transcript_store.list_transcripts(conn=None, *, campaign_id=ANY, data_dir=None, output_dir=None) -> list[Located]`: present transcripts ordered by the `transcript` row's `mtime_ns` (newest first), then `created_at`;
   - the Transcripts page, recent partial, dashboard, and `wisper transcripts list` use it, reading frontmatter from `loc.md`.

   The page keeps its campaign column and filter.
   - Order by the `transcript` row's `mtime_ns` descending, rows with `NULL` last; then `created_at` descending, then `id`.
   - The lists show present sessions only. Missing ones appear under Needs attention and on their campaign page, as today.
   - v11's backfilled `transcript` rows have `mtime_ns` NULL until `file_registry.sync` fills them (at startup), so they sort last until then.
6. **Campaign page** (`campaigns.py` `campaign_detail`, ~62–125): replace `_transcript_missing` and the `summarized` count with `locate` per `transcript_ids`:
   - missing means `loc.missing`;
   - summarized means `loc.companion(".summary.md").exists()`.

   The template links sessions by id.
7. **Search results** link `/transcripts/{id}#b-N`, and show the campaign name when there is one. `templates/search.html` ~67–71 builds these hrefs with `~` concatenation (`'/transcripts/' ~ (g.stem | urlencode)`); they become `'/transcripts/' ~ g.transcript_id`.

**Existing tests to rewrite:**
- Every test that requests `/transcripts/<name>` (about 180 references), 94 in `test_web_routes.py`, plus `test_transcript_enroll.py`, `test_transcript_edit.py`, `test_search_routes.py`, `test_owasp.py`, `test_path_traversal.py`, and `test_e2e.py`.
- Use `_seed.transcript_id(stem)`, or read the id from the redirect after a create. A test that writes a raw `<stem>.md` with no row (it worked through `_get_safe_content_path`) seeds it with `seed_transcript(stem, write_md=True)` instead, so it has an id.
- The path-traversal cases for transcript routes become:
  - a non-integer segment on a POST route returns 404 or 405 (no untyped POST route exists) and touches no file;
  - the legacy GET with each payload (null byte, `../`, CRLF) returns a redirect to `/transcripts?error=not_found` or the chooser, never a path.

**New tests:**
- **Legacy redirect:**
  - `/transcripts/Session%201` reaches the legacy handler (not a 422) and gets a 303 to the id when the name is unique;
  - two campaigns with the same name → the chooser lists both;
  - unknown → not_found.
- **The campaign page** shows a session whose `.md` and summary are in the campaign folder as present and summarized (seeded by hand).
- **Not found:** `/transcripts/999999` redirects with `error=not_found`.
- **Bulk delete:** with one valid and one bogus id, it deletes only the valid one.
- **Transcripts list order** follows the files' modified times.
- **Search hit links** use ids.
- **Job page:** the SSE `done` event of a completed transcription and of a summarize job carries `transcript_id`, and the rendered script has no `${enc}` stem links.
- **Job history filter:** `/jobs/history?transcript_id=<id>` lists only that transcript's jobs when two campaigns share its name.

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
- `grep -rn 'f"/transcripts/{quote' src/` is empty.
- `grep -rn "/transcripts/" src/wisper_transcribe/web/templates src/wisper_transcribe/static` is reviewed hit by hit by the orchestrator: every transcript link uses an id (Jinja `{{ … }}`, `~` concatenation, and JavaScript template strings alike). Static routes (`/transcripts`, `/transcripts/relink`, `/transcripts/bulk-…`, `/transcripts/needs-attention/…`) are fine.
- **Rehearsal:** click through the dashboard, a campaign page, the Transcripts list, a transcript, its summary, edit, the enroll wizard, search hits (the anchor scrolls), a job page, a recording page, and an old `/transcripts/<name>` URL.

---

### Phase 4 — Scan campaign folders: reconcile, sync, Needs attention

*Done (opencode, one hand-off). Claude's review fixed two reconcile cases: a misplaced session moved into its folder **and edited** (so its size and time no longer match) now keeps its row instead of being flagged missing beside an unclaimed copy; and "target taken" counts any row of the target campaign with that name, present or not, casefolded. Added path-traversal tests for the folder form of delete-file and the two new folder routes. A folder-pending change by another process skips the whole pass rather than one campaign. Rehearsed on a scratch copy of the Mac data: 4 sessions listed as misplaced; moving one session's files into the claimed folder by hand put it in place with its 17 companions, nothing flagged missing.*

**Goal:**
- Reconcile and the file registry see the output root **and** each campaign's folder.
- A transcript dragged between folders outside wisper (Obsidian, Explorer) changes campaign, and its companions follow.
- Needs attention lists misplaced transcripts.
- No session is written into a campaign folder yet (only Phase 1's journal is): the tests create those files by hand.

**Read first:**
- `transcript_store.py`: `_transcript_files` (439), `_companion_stem` (449), `reconcile` (468–602), `_match_renamed` (604), `_point_transcript_row` (636), `needs_attention` (808), `delete_unowned_file` (841)
- `file_registry.py`: `_output_candidates` (622), `_scan_output` (648), `_sync` (757–846), `sync_if_due` (605)
- `web/routes/transcripts.py`: the Needs attention routes (`/needs-attention/forget`, `/needs-attention/delete-file`) and the panel in `templates/transcripts.html`
- `storage_trim._find_orphans` (~135)

**Steps:**
1. **`transcript_store.transcript_dirs(conn=None, *, data_dir=None, output_dir=None) -> list[tuple[Path, Optional[int]]]`:**
   - the output root with `None`;
   - then, for each **claimed** campaign **with no `folder_pending`** whose folder exists, its `folder`.

   An unclaimed folder, a campaign mid-rename, and every other subfolder are never scanned. A campaign with `folder_pending` set is skipped entirely by reconcile and sync (its rows are neither matched nor flagged missing): between `os.rename` and the compare-and-swap commit (Phase 7) its rows still name the old folder, and scanning either directory would flag them all missing. Startup finishes pending renames before reconcile, and Needs attention lists the ones that stay pending.
2. **`_transcript_files(dirs) -> dict[tuple[str, str], tuple[Path, Optional[int]]]`**, keyed by `(file_registry._key(realpath(dir), fold), nfc(stem))` (not `os.path.normcase`, which is a no-op on macOS). It skips:
   - `*.summary.md`;
   - `TEMP_PREFIX` files;
   - in a campaign folder, the file named `journal_name(folder)` (compared casefolded);
   - **any file registered to a campaign** (`files.campaign_id IS NOT NULL`, by path): a journal left under its old name after a failed post-rename move, and the planned campaign-level outputs, are never read as sessions. `file_registry.sync`'s transcript candidates skip the same paths.
3. **`reconcile` across folders.** Same rules as today, applied to `(dir, stem)` instead of `stem`:
   - **Present:** a row whose current location (its `transcript` files row, else `expected_dir`) has its `.md` is present; un-flag it if it was missing.
   - **Case-only rename:** the twin match applies within one directory only.
   - **Moved or renamed** (`_match_renamed`): an unregistered `.md` in any scanned dir whose `(size, mtime_ns)` uniquely matches one row with no `.md` on disk (the *candidate*) is that transcript. In one transaction:
     - `UPDATE transcripts SET stem = :stem, campaign_id = :cid, missing_since = NULL, position = CASE WHEN :cid IS NULL THEN NULL WHEN campaign_id IS :cid THEN position ELSE (SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = :cid) END WHERE id = :id` (`:cid` is the dir's campaign, `NULL` in the root). A rename within one folder keeps its place in the campaign's order;
     - repoint its `transcript` row;
     - after `RELEASE`, queue `rename_companions(tid, old_stem, new_stem, src_dir=old_dir, dst_dir=new_dir)`. A companion the user moved along with the `.md` (already present at the destination, gone from the source) is repointed (`file_registry.repoint`), not moved, so an audio row isn't forgotten.

     A campaign change fires `transcripts_campaign_bu`, which un-journals the transcript and marks the journal stale.
     - **Target taken:** before matching, check whether a target `(campaign_id, stem)` row exists (exact, or casefold when `_fold`) **other than the candidate itself**. If one does, don't match: the file is **unclaimed**.
       - This happens when campaign B has a misplaced `Session 1` (its `.md` still in the root) and the user drags `A/Session 1.md` into `B/`.
       - When the target row *is* the candidate (the user dragged B's misplaced `Session 1.md` from the root into `B/`), it matches: the row keeps its id, campaign, and position, and is no longer misplaced.
       - Each match runs inside `SAVEPOINT m`; success ends with `RELEASE m`. Any `sqlite3.IntegrityError` runs `ROLLBACK TO m; RELEASE m` (which also undoes the `repoint`), queues no companion moves, and the file is unclaimed.
   - **A registered `.md` is never new.** A file whose path is any row's `transcript` files row belongs to that row, whatever the row's campaign (including a campaign mid-rename, step 1, whose misplaced sessions sit in the root).
   - **New** (only after matching found nothing): `ensure_row(conn, stem, campaign_id=<dir's>)`, appended at the end of that campaign.
     - Exception: a row for `(dir's campaign, stem)` already exists with its registered `.md` elsewhere **and present** (a misplaced session). Then the newcomer is **unclaimed**, not a second file for that row.
   - **Name and file disagree:** a row whose registered `.md` exists but whose basename differs from `stem` (a crash between a stem update and its file move). The registry wins:
     - set `stem` from the file name;
     - or, when that name is taken in its campaign, list it under Needs attention ("name doesn't match file").
   - **Missing:** rows not found anywhere are flagged missing, as today (except rows of a campaign mid-rename, step 1).
   - **Companion moves run after the outer commit.** `RELEASE m` doesn't commit; reconcile's `db.transaction()` can still roll back. Matches append their `rename_companions` calls to a list that runs only after that transaction commits ("files follow rows"); a rollback discards the list.
   - **One writer at a time in this process:** `transcript_store._LOCATION_LOCK` (a module `threading.RLock`) covers reconcile's **scan and write** (from listing the dirs until its transaction commits), Phase 6's move and rename (acquired **before** their `BEGIN IMMEDIATE`, held until their files have moved), and Phase 7's `finish_folder_rename` (from the disk step through the post-commit journal rename). Lock first, then transaction, everywhere.
     - The concurrent actors in one process are the job worker thread (`register`, `_keep_audio`, `update_journal` → `ensure_folder`), the search backfill thread, and request handling. The web routes are `async def` and run on the event loop.
     - **Long operations leave the event loop:** routes call `move_transcript`, `rename_transcript`, `move_files_home`, `rename_campaign`, `finish_folder_rename`, `delete_campaign`, and `ensure_folder` (uploads) through `starlette.concurrency.run_in_threadpool`, so file retries and `finish_folder_rename`'s sleeps never block SSE streams.
     - Page-load reconcile (`campaign_detail`, `transcripts_list`, `campaign_delete`) takes the lock with `acquire(blocking=False)` and skips the whole reconcile when it's held. The startup and CLI reconciles block.
     - **Another process** (one CLI command) isn't covered by the lock. Inside its write transaction, reconcile re-reads `SELECT id, folder, folder_pending, folder_claimed FROM campaigns` and compares it with what its scan used; a campaign that changed is skipped this pass (its rows are neither matched nor flagged missing). A CLI command in another process isn't covered; that race settles on the next reconcile (the stem follows the file twice) and only marks the journal stale. Document it in `architecture.md`.
   - **Sweep:** stale temp files are swept in every scanned dir.
4. **`file_registry.sync`:**
   - `_scan_output` scans every dir from `transcript_dirs`.
   - The owner of a companion named `<S><suffix>` in dir `D` is the transcript whose current `.md` is `D/<S>.md`. Build that map from the `transcript` rows' `rel_path` (dir + stem), falling back to `expected_dir` for rows without one.
   - The journal file in each campaign folder is found by name (Phase 1) and is never read as a transcript.
   - The "transcript row rel_path matches its stem" consistency check compares the basename only.
5. **Needs attention** (`Attention`) gains five fields (the missing-transcript query already reads `transcripts.campaign_id` from Phase 1):
   - `misplaced: list[Located]`: present transcripts with `loc.misplaced`;
   - `pending_folders: list[tuple[str, str, str]]`: `(display_name, folder, folder_pending)` for campaigns with a pending rename. Phase 7 creates them; listing them belongs here.

   - `folder_taken: list[tuple[int, str, str]]`: `(campaign_id, display_name, folder)` for unclaimed campaigns whose folder exists and isn't empty (ignoring clutter, Phase 1);
   - `legacy_journals: list[str]`: campaigns whose `<data>/campaigns/<slug>/journal.md` couldn't be adopted;
   - `missing_folders: list[tuple[int, str, str]]`: `(campaign_id, display_name, folder)` for claimed campaigns with no `folder_pending` whose folder is gone while the output root exists (a pending rename with both directories gone is listed under `pending_folders`, with Phase 7's Finish without folder).

   `total` counts all five.
   - **Panel, misplaced:** one grouped line, "N sessions aren't in their campaign's folder yet", with each name and campaign. The action text says "Run `wisper storage trim --apply`, or use Move files on the session's page" (that button arrives in Phase 6).
   - **Panel, pending renames:** each one, with its text and a Retry button that Phase 7 adds.
   - **Panel, folder taken:** "A folder named X already exists in your transcripts folder and isn't wisper's. Rename the campaign to use a different folder, or use this folder: every `.md` file in it becomes a session of this campaign." with a **Use this folder** button. It posts `campaign_id` (int) to `POST /transcripts/needs-attention/claim-folder`, which calls `campaign_folders.claim_folder(id)` and redirects to the Transcripts page; the next reconcile registers the folder's files. (Brandon, 2026-10-05: lets an Obsidian user who made the folder first adopt it.)
   - **Panel, legacy journals:** "The journal for X couldn't be moved into its folder yet."
   - **Panel, missing folders:** "The folder X for campaign Y isn't in your transcripts folder. If you renamed it, rename the campaign to match; otherwise Recreate folder." with a **Recreate folder** button posting `campaign_id` (int) to `POST /transcripts/needs-attention/recreate-folder` → `campaign_folders.recreate_folder(id)`.
   - Buttons that later phases add (Move files in Phase 6, Retry in Phase 7) are text only until then.
6. **Delete-file route and `delete_unowned_file`** accept one level:
   - `name` may be `"<file>"` or `"<folder>/<file>"`;
   - `<folder>` must equal a current campaign's `folder`;
   - each component contains no `\`, `:`, or NUL, and passes the basename + abspath guard;
   - the basename must match `_companion_stem`'s patterns (they already include `<stem>.flac` and `.md.bak`).

   `delete_unowned_file(path)` checks that `path`'s parent is a `transcript_dirs` entry.
7. **`storage_trim._find_orphans`** stays root-only: `<recording-id>.wav` hand-off copies only ever landed in the root.

**Existing tests to rewrite:**
- Reconcile and sync tests that assert a full set of scanned files may need the journal-file exclusion. Otherwise none; root behaviour is unchanged.

**New tests** (`tests/test_transcript_store.py`, `tests/test_file_registry.py`):
- (moved from Phase 3, `tests/test_web_routes.py`) the campaign page shows a session whose `.md` and summary are in the campaign folder as present and summarized (seeded by hand).
- A `.md` copied into a campaign folder becomes that campaign's transcript, at the end.
- **Drag between campaigns** (`os.replace` keeps mtime): the transcript moves from A to B with its summary, sidecar, excerpts, and audio. Its journal entry is gone, and A's journal is stale.
- A drag into the root unassigns the transcript.
- Two campaigns with `Session 1.md` each: reconcile keeps both rows apart, and sync assigns each folder's companions to its own transcript.
- `<folder> Journal.md` is never registered as a transcript.
- An unrelated subfolder of the output root is ignored, and so is an **unclaimed** folder with a campaign's name (its `.md` files are never registered).
- A stuck pending rename whose target is a different, foreign directory: that directory isn't scanned.
- **Drag into a misplaced row's campaign:** B has misplaced `Session 1` (`.md` in the root), and `A/Session 1.md` is dragged into `B/`. The file is unclaimed; A's row and its `files` rows are unchanged; B's row is unchanged.
- A row whose registered `.md` basename differs from its stem gets its stem from the file.
- **Drag a misplaced session home:** B's misplaced `Session 1.md` (registered in the root) is moved by hand into `B/`: same row id, same position, no longer misplaced, companions follow.
- **Rename in place:** `B/Session 1.md` renamed to `B/Prologue.md` keeps its position in B's order.
- **Drag with companions:** `.md` and `.flac` both dragged from `A/` to `B/`: the `audio` row is repointed to `B/…flac`, not forgotten.
- **Mid-rename campaign:** a campaign with `folder_pending` set: neither directory is scanned and its rows aren't flagged missing.
- **Use this folder:** posting the claim for a folder-taken campaign sets `folder_claimed`; the next reconcile registers its `.md` files as that campaign's sessions. A non-integer `campaign_id` → 400; an id with no folder-taken state → redirect with `error=not_found`.
- **Lock:** `_LOCATION_LOCK` is held across reconcile's scan and write (a test holds it from a second thread); a page-load reconcile with the lock held returns without scanning.
- **Cross-process rename:** a campaign's `folder` changed between reconcile's scan and its write (patched) → that campaign's rows are untouched this pass.
- **No companion moves on rollback:** a reconcile whose transaction raises after a match moves no file.
- **Registered files aren't sessions:** a campaign-owned file named `Old Journal.md` in a campaign folder isn't registered as a transcript.
- **Pending campaign's root files:** a campaign with `folder_pending` set and a misplaced session in the root: no new root row is created for that `.md`.
- **Missing folder:** a claimed campaign whose folder was deleted is listed; Recreate folder makes it; a non-integer `campaign_id` → 400.
- The misplaced listing for a campaign transcript whose files are in the root.
- **Delete-file route:**
  - a campaign-folder orphan is deleted;
  - `Other/x.summary.md`, with `Other` not a claimed campaign folder → 400;
  - `A/../x`, `A/B/x`, `A\..\x`, and `A:x` → 400.

**Docs:**
- `architecture.md`: Reconcile scans the root and campaign folders, and a cross-folder move changes campaign.
- `docs/scenarios.md`: "I moved a session to another campaign's folder in Obsidian": wisper follows it and its files.
- `docs/web-ui.md`: the Needs attention entries.

**Done when:**
- `pytest tests/test_transcript_store.py tests/test_file_registry.py tests/test_web_routes.py tests/test_path_traversal.py tests/test_storage_trim.py` pass, and the full suite is green.
- **Rehearsal:**
  - on the scratch copy, claim the folder first: with `<copy>/output/Impossible Landscapes` absent, run `ensure_folder` for that campaign (`.venv/bin/python -c` with both env vars set), which creates and claims it;
  - move one session's `.md` and companions into it in the shell;
  - after a page load the session is in place, keeps its position, and is no longer misplaced;
  - the other three are listed as misplaced;
  - separately, on a second scratch copy, `mkdir` the folder with a stray `notes.md` in it: the campaign shows "folder taken", and **Use this folder** claims it and registers `notes.md` as a session.

---

### Phase 5 — Write into campaign folders: uploads, recordings, re-transcribe, CLI

*Done (opencode, one hand-off). Claude's review fixed CLI `--campaign --overwrite`: it found the session by its folder path, so a misplaced session (still in the root) or an unregistered file of that name was refused despite `--overwrite`; it now looks the row up by campaign and name and writes where its `.md` is. Readings opencode flagged: the name check offers overwrite for a session flagged missing; a CLI folder run leaves per-file clashes to `process_file`'s skip/overwrite. Rehearsed the name check on a scratch copy of the Mac data (clash in the campaign, clash with the root copy, new name, journal name reserved). Uploads and recordings weren't run end to end: that needs the ML models; Phase 10 covers it.*

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
1. **Writes go through `campaign_folders.ensure_folder`** (Phase 1). `FolderTakenError` becomes the fixed code `folder_taken`, `FolderPendingError` `rename_pending`, `FolderMissingError` `folder_missing`, and `FileNotFoundError` (root absent) `output_unavailable`; never the path. Checks that must not create anything (name-check, clash pages) use `transcript_store.expected_dir` and `campaign_folders.is_claimed`.
2. **Upload:**
   - `POST /transcribe` resolves the campaign (if any) to its id, and `out_dir = ensure_folder(id)` (else the root), **before** it saves the upload to its temp folder, so an unknown campaign, a taken folder, or an absent output root (`?error=output_unavailable`) refuses the request without leaving a temp file. It passes `output_dir=out_dir`.
   - The form's `overwrite` field becomes `clash` ∈ `{"", "overwrite", "keep_both"}`:
     - `keep_both` → `original_stem = next_free_stem(out_dir, stem, campaign_id)`;
     - `overwrite` → today's overwrite. When the clashing name is an existing session of that campaign (present, missing, or **misplaced**), the job writes to that session's `loc.dir` and `loc.stem`, so its row is reused and no second `.md` appears.
   - **Upload names** (`_upload_stem`, today only splitting separators at `transcribe.py` ~171): NFC; strip; a leading `.` is stripped; a name that is `is_reserved` gets `_` appended (as the folder sanitizer does); cut to 100 characters (Decisions) and trimmed of trailing dots and spaces; empty → `upload`.
   - `transcript_store.next_free_stem(directory, stem, campaign_id, conn=None) -> str`: `stem`, else `stem (2)`, `(3)`, …, the first with no file `<s>.md` or `<s>.flac` in `directory` (casefold when `_fold`), no row `(campaign_id, s)` (casefold), and not the folder's journal name.
3. **`_name_clashes(filename, campaign_slug)` and `GET /transcribe/name-check?filename=…&campaign=…`:**
   - resolve the target with `expected_dir`, with no mkdir; an unclaimed folder that exists and isn't empty is clash `"folder_taken"`, with no overwrite and no keep-both, only Cancel;
   - check the target folder's `.md`/`.flac`;
   - check the target campaign's rows for a casefold match (`"md"` when it's present, `"missing"` when flagged);
   - a stem equal to that folder's journal name casefold is a clash `"reserved"`.
   - **Codes:** the existing `"md"`, `"flac"`, and `"missing"` keep their names and meanings, including the own-audio rule: a `.flac` that is the clashing session's own registered `audio` row isn't a `"flac"` clash. New codes: `"reserved"` and `"folder_taken"`. The script and existing tests keep working on the old three.
   - **Response:** adds `"overwrite_allowed": bool`. It's false for `"reserved"`, for an existing `.md` with no `files` row (a user's file), and for a `"flac"` clash with no `.md` (Keep both or Cancel).
   - **The page's script:**
     - re-checks when the campaign select changes;
     - offers **Overwrite** only when `overwrite_allowed`, plus **Keep both** and **Cancel**;
     - shows the modified time as today.
   - Rendering any text from the response goes through `textContent`.
4. **`process_file`:**
   - after `atomic_write_text`, calls `register(out_path, origin="job")` when `dir_campaign(out_path.parent)[0]`;
   - **remove** the post-write `move_transcript_to_campaign` call (683–695): the folder decides the campaign;
   - `out_dir.mkdir(parents=True, exist_ok=True)` (~419) stays for a CLI `-o` elsewhere (a directory the user named). For the output root or a campaign folder, `out_dir` comes from `ensure_folder`/`get_output_dir()` and already exists, so the mkdir does nothing;
   - `campaign` remains the roster filter only;
   - the "not added to campaign" note becomes "Note: {out_path.parent} isn't the transcripts folder; the transcript isn't tracked".
5. **Recording hand-off** (`_submit_recording_transcription`):
   - **re-transcribe:** with a linked transcript, `loc = locate(recording.transcript_id)`, `output_dir = loc.dir`, `original_stem = loc.stem`;
   - **first run:** `output_dir = ensure_folder(cid)` (or the root), `original_stem = recording.id`. `Recording` has only `campaign_slug`; resolve `cid` with `SELECT id FROM campaigns WHERE slug = ?` (a slug with no campaign → the root, as today's unassigned recording).
   - `FolderTakenError` → a new fixed result code, `"folder_taken"`. The recording page renders it: "The campaign's folder name is taken by another folder; rename the campaign." Extend the documented return to `"not_ready" | "no_audio" | "folder_taken"`.
   - `campaign=` stays, for the roster.
6. **Re-transcribe route:** `output_dir = loc.dir`, `original_stem = loc.stem`, `overwrite=True` (replace in place; the row, campaign, and id are kept).
7. **Jobs:**
   - The "Transcripts folder:" log line prints the job's `output_dir`. `_keep_audio` already writes beside the `.md`.
   - **The target is recorded at submit.** `JobQueue.submit` resolves `locate_path(Path(output_dir) / f"{original_stem}.md")` and stores `job.transcript_id` (or `None` for a new name). `job_history._params` records the job's actual `output_dir` as `output_root` (~60), not `get_output_dir()`, so the missing-file evidence names the campaign folder. `job_history._subject_ids` uses `job.transcript_id` when set, before falling back to `output_path`. A pending or running re-transcribe or overwrite therefore has `jobs.transcript_id`, which Phase 6's busy guard needs.
8. **CLI:**
   - `wisper transcribe --campaign X` without `-o`, or with `-o` resolving (`realpath`) to the output root: `output_dir = ensure_folder(id of X)`.
     - **Name clash check before transcribing** (the web has name-check; the CLI must not take over another session's row): if `(X, stem)` exists as a row or `<stem>.md`/`.flac` is in the folder, refuse with a `ClickException` naming it, unless `--keep-both` or `--overwrite` (write to that session's `loc.dir`/`loc.stem`, as the web overwrite does) is given.
     - **`--keep-both` names the new copy after the run's local start time**, not ` (2)` (Brandon, 2026-10-05): `f"{stem} ({start:%Y-%m-%d %H%M})"`, e.g. `Session 3 (2026-10-05 0142)` (no `:`, which Windows forbids). If that name is also taken, `next_free_stem` on it (`… (2026-10-05 0142) (2)`). It passes the same name checks as any new name (Decisions: 100 characters, so the stem part is cut to leave room for the 18-character suffix). The web's Keep both keeps ` (2)`.
     - An unknown slug or `FolderTakenError` is a `ClickException`.
     - Help text: "Campaign slug: write into its folder and use its roster".
   - With `-o` elsewhere, unchanged: roster only.
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
- **CLI:** `--campaign` writes into the folder; so does `-o <output root> --campaign X`; with `-o` elsewhere, beside the input; a name already in the campaign is refused without `--keep-both`/`--overwrite`; `--keep-both` writes `Name (YYYY-MM-DD HHMM).md` (freeze the clock in the test), and a second clash in the same minute adds ` (2)`.
- **Windows CI:** behaviour that differs on Windows (locks, case, renames) is tested in files already on CI's `windows storage` list (`test_transcript_store.py`, `test_campaign_manager.py`, `test_campaign_folders.py`, `test_file_registry.py`); route and CLI tests check mapping only.
- **Overwrite** of a misplaced session by an upload into its campaign: one row and one `.md`, written where the `.md` was.
- **Job target:** a re-transcribe submitted through `JobQueue.submit` has `jobs.transcript_id` set while pending.
- **name-check** on an unclaimed, non-empty folder returns `folder_taken` and creates nothing.
- **name-check** with only `<stem>.flac` in the target folder returns clash `"flac"` with `overwrite_allowed: false`; `keep_both` skips past it.
- **Upload refused early:** an upload into a folder-taken campaign leaves no `wisper_upload_*` temp folder behind.
- **Upload names:** `.wisper-tmp-x.mp3` → `wisper-tmp-x`; `CON.mp3` → `CON_`; a 150-character name → 100 characters.
- **name-check** keeps `"flac"` for a stray `.flac`, and doesn't report the session's own audio.
- **Missing transcript after write** still fails the job with "Transcript file missing after write" for a campaign-folder target.

**Docs:**
- `docs/web-ui.md`: uploads go into the campaign's folder, and the clash prompt.
- `docs/cli-reference.md`: `transcribe --campaign`.
- `architecture.md`: the job flow's output dir.
- `plan.md` "Open bugs → Missing transcript file": the job's output path can be a campaign folder.
- **`CLAUDE.md` Non-Obvious Gotchas:**
  - "Transcript output dir": web uploads go to the output root, or to the campaign's folder in it;
  - "Each transcript keeps one compact audio copy": `<stem>.flac` sits beside its `.md`.

**Done when:**
- `pytest tests/test_web_routes.py tests/test_web_jobs.py tests/test_pipeline.py tests/test_cli.py tests/test_record_routes.py tests/test_path_traversal.py` pass, and the full suite is green. Use the names that exist; find the hand-off tests with `grep -ln _submit_recording_transcription tests/`.
- **Rehearsal:** upload the 45 s test file into "Impossible Landscapes" (a real transcription on MPS), try again for the clash prompt and choose Keep both, then re-transcribe it.

---

### Phase 6 — Moving a transcript moves its files; clash prompt; Rename

*Done (opencode, three hand-offs). Claude's review: `move_files_home` now holds `_LOCATION_LOCK` from its busy check until the files have moved (it checked busy before taking the lock), and a locked `.md` reports `detail="source_missing"` only when the source really is missing. Rehearsed on a scratch copy of the Mac data through the web routes: Move files (18 files into the campaign folder), a move to a new campaign (17 files), a rename (every companion renamed), and a move back to the root; no companion left behind, every page 200.*

**Goal:**
- Changing a transcript's campaign moves its files.
- A Rename action renames them.
- Both share one clash rule and the busy-job guard.
- Misplaced transcripts can be put in place.

**Read first:**
- `transcript_store` Locations (Phase 2a), `rename_companions`, `delete_transcript`, `next_free_stem` (Phase 5)
- `file_registry.move` (471)
- `campaign_manager.move_transcript_to_campaign`, `remove_transcript_from_campaign`, `set_campaign_transcript_order`
- `web/routes/transcripts.py`: `assign_campaign` (749), `bulk-campaign` (368); `web/routes/campaigns.py` remove (253); `templates/transcript_detail.html` (the campaign select)
- `job_history` (the `jobs` columns; `mark_interrupted`; `record`, which swallows every exception)
- `file_registry._ACTIVE_CAPTURE` (70) and the `recordings.status` values

**Hand-offs:** 6a1 is steps 1, 2 (`check_move` only), and 4 (`active_jobs`, `record_required`, `check_move`, `validate_new_stem`). 6a2 is the rest of step 2, steps 3, 5, 7, and 8 (`move_transcript`, `move_files_home`, `rename_transcript`, `campaign_manager`, CLI). 6b is steps 6 and 9 (routes and templates). Each with its tests; one commit after 6b.

**Steps:**
1. **`job_history.active_jobs(conn, *, transcript_id=None, campaign_ids=(), campaign_slugs=()) -> list[str]`** (the ids of what's busy; empty means free). It counts:
   - `jobs` rows with `status IN ('pending','running')` matching any of:
     - `transcript_id` (set at submit since Phase 5);
     - `campaign_id IN campaign_ids`;
     - a transcript whose `campaign_id IN campaign_ids`;
     - `json_extract(params_json, '$.campaign') IN campaign_slugs`;
   - `recordings` with `capture_status IN file_registry._ACTIVE_CAPTURE` (`db.py` ~443) whose `campaign_id IN campaign_ids` or whose `transcript_id` is `transcript_id` (a Discord or local capture holds its campaign's slug in memory and has no `jobs` row).

   The scope is deliberately the whole source and target campaign, not only that transcript: journal and relabel jobs read every session of their campaign.

   It takes the caller's `conn` and is called **inside** the write transaction (`BEGIN IMMEDIATE`), so a job submitted between check and write is impossible: submit writes its `jobs` row first.
   - **Submit must not lose that row:** `job_history.record` swallows exceptions. Add `job_history.record_required(...)` (same insert, re-raises). `JobQueue._enqueue` (`web/jobs.py` ~559–563) records history **first**, then adds the job to `self._jobs` and the queue (today it tracks first), and calls it via `_record_history(job, required=True)` **only for the job types the busy guard reads: transcription, journal, relabel**; the others (LLM, enroll, live) keep the swallowing `record`. If it raises, `_enqueue` re-raises and the job isn't queued. Callers:
     - upload `POST /transcribe` → deletes the job's `wisper_upload_<id>/` folder, `?error=submit_failed`;
     - recording hand-off → result code `"submit_failed"` (the recording page shows it);
     - journal and relabel routes → `?error=submit_failed`.

     Status updates after that keep the swallowing `record`.
   - **Not covered, accepted:** a CLI `wisper transcribe` run writes no `jobs` row, so it isn't seen. Rows left `pending`/`running` by a crashed server count as busy until the next server start runs `mark_interrupted`; the CLI's busy message names the job ids and says "If no wisper server is running, these are left over from a crash: start the server once to clear them." Document both in `architecture.md`.
2. **`transcript_store.check_move(transcript_id, campaign_slug: Optional[str], *, new_stem=None, conn=None, data_dir=None, output_dir=None) -> MoveCheck`.** It is pure: no mkdir and no writes. It's used by the clash page and by `move_transcript`/`rename_transcript`.
   - **`MoveCheck`:** `status`, `dst_dir`, `stem`, `clash_modified`, `overwrite_allowed`, `existing_id`.
   - **`status`:** `"ok" | "unchanged" | "invalid" | "busy" | "folder_taken" | "clash" | "reserved"`.
   - **`MoveCheck.files_move: bool`:** false when there's nothing to move on disk: the session is flagged missing, or `dst_dir` is the directory its `.md` is already in (by `realpath` + `_key`) and the stem is unchanged. Then the move is a database change only.
   - **Validation first:** with `new_stem`, `check_move` runs `validate_new_stem` and then `safe_path(new_stem, ".md", dst_dir)` before any disk access, returning `status="invalid"` when either refuses. The clash page passes a name from the query string, so this is a path guard.
   - **Rules:**
     - `unchanged`: same campaign and same stem;
     - `busy`: `active_jobs(transcript_id, campaign_ids={source, target}, campaign_slugs={source, target})` is non-empty;
     - `folder_taken`: the target folder is unclaimed and exists non-empty;
     - `clash`: a `<stem>.md` in `dst_dir` (casefold when `_fold`) or a target row with that stem (casefold), **excluding the session's own file and row**: a candidate file that is the session's registered `.md` (`os.path.samefile`, or the same registered path) isn't a clash. Without this, a misplaced session (its `.md` already in the root) removed from its campaign, or a case-only rename on a case-insensitive filesystem, would clash with itself;
     - `reserved`: the folder's journal name.
   - A session flagged missing moves and renames as a database change only (`files_move` false); its rows and assignment change, and reconcile or relink finds the file later. Today a missing session can be removed from its campaign, and that keeps working.
   - `overwrite_allowed` is true only when the clashing file is a registered session (`existing_id` set).

   **`transcript_store.move_transcript(transcript_id, campaign_slug: Optional[str], *, clash: Literal["ask", "overwrite", "keep_both", "skip"] = "ask", data_dir=None, output_dir=None) -> MoveOutcome`.**
   - **`MoveOutcome`:** `status`, `detail: Optional[str]` (`"folder_pending"`/`"folder_missing"` for `folder_taken`; `"source_missing"` for `locked`), `new_stem`, `clash_modified`, `overwrite_allowed`, `kept: list[Path]`, `busy: list[str]`.
   - **`status`:** `"moved" | "unchanged" | "invalid" | "busy" | "folder_taken" | "unavailable" | "clash" | "reserved" | "locked" | "partial"`. `unavailable`: the output root is absent (`ensure_folder` or the root check raised `FileNotFoundError`).

   **One status table** (routes, bulk, the campaign page, and the CLI all use it; `folder_taken` includes `folder_pending`/`folder_missing`, which show their own text):

   | Status | Route redirect | Bulk counter | CLI (exit 1 unless noted) |
   |---|---|---|---|
   | `moved` | 303 to the page | `moved` | prints the new place (exit 0) |
   | `unchanged` | 303 to the page | `moved` | "already there" (exit 0) |
   | `invalid` | `?error=invalid_name` | `skipped` | "invalid name" |
   | `busy` | `?error=busy` | `busy` | names the job ids |
   | `folder_taken` | `?error=folder_taken`, or `rename_pending`/`folder_missing` from `detail` | `skipped` | the folder problem |
   | `unavailable` | `?error=output_unavailable` | `skipped` | "the transcripts folder isn't available" |
   | `clash` | the clash page (`?clash=…`) | `skipped` | existing file's time |
   | `reserved` | `?error=reserved` | `skipped` | "name is the journal's" |
   | `locked` | `?error=locked` | `skipped` | "a file is open in another program" |
   | `partial` | `?notice=partial_move` | `moved` | lists the kept files (exit 0) |

   Steps (`_LOCATION_LOCK` is acquired before step 5's transaction and held through step 6, Phase 4):
   1. `check_move` (read-only). `ask`/`skip` with a clash → return `clash`, with nothing changed. Any other non-`ok` status → return it.
   2. **Target folder:** `ensure_folder(target)`, or the root (only when `files_move`). `FolderTakenError` → `folder_taken`.
   3. **`overwrite`:** only when `overwrite_allowed`. First a short `BEGIN IMMEDIATE` re-running the busy check with `existing_id` added (`active_jobs(transcript_id=existing_id, …)` too); busy → return `busy` with nothing changed. Then `delete_transcript(existing_id)`; `"kept"` → return `locked`, with nothing else changed. If the chosen session's own move then fails, the overwritten one stays deleted (Decisions, Overwrite).
   4. **`keep_both`:** `new_stem = next_free_stem(dst_dir, stem, target_id)`.
   5. **One transaction:** re-run `check_move`'s busy and clash checks with this `conn`; if either now fails, return its status. Then `UPDATE transcripts SET campaign_id = ?, position = <end or NULL>, stem = ? WHERE id = ?`. The triggers un-journal the session, and mark the journal stale on a stem change. When `files_move` is false, return `moved` after commit.
   6. **After commit,** move the `.md` to `dst_dir/<new_stem>.md` with `file_registry.move(row, …)` (with no `transcript` row, `os.replace` then `file_registry.add`):
      - **If that fails** (`error`/`conflict`, or `missing`: the source vanished, and `file_registry.move` forgot the row — re-add it at its old path before reverting): revert the **whole** assignment in a new transaction, compare-and-swapped on what step 5 wrote: `UPDATE transcripts SET campaign_id = :old_cid, stem = :old_stem, position = <:old_pos if that slot is free in :old_cid, else the end; NULL in the root> WHERE id = ? AND campaign_id IS :new_cid AND stem = :new_stem`. Restoring only the stem would break `(campaign_id, stem)` uniqueness when `keep_both` picked a new name because the target already held the old one.
        - Reverted → return `locked` with the `.md` in `kept`: nothing moved, and the session is where it was (its journal entry, deleted by the trigger, stays deleted; the journal is stale).
        - The revert hits an `IntegrityError` (the old name was taken meanwhile) or changes 0 rows → leave the row as step 5 wrote it and return `partial`. The session is misplaced, and reconcile's "registry wins" rule or Needs attention resolves the name.
      - **Otherwise** `rename_companions(tid, old_stem, new_stem, src_dir=old_dir, dst_dir=dst_dir)`. Files not moved go in `kept`, and the status is `partial`; they stay registered at their old paths, and `Located.companion` finds them.
   7. No reindex is needed: the content and mtimes are unchanged, and the title trigger follows a stem change.
3. **`move_files_home(transcript_id, *, data_dir=None, output_dir=None) -> MoveOutcome`:** for a misplaced session.
   - The busy check runs in a short `BEGIN IMMEDIATE` read.
   - Then `ensure_folder`, then move the `.md` (if it isn't in `expected_dir`) and every registered companion not beside it into `expected_dir`, without a database change to `transcripts`.
   - A clash in the expected dir → `clash`; it is never overwritten or renamed automatically.
   - Used by Needs attention's **Move files** and by `storage trim` (Phase 8).
4. **`transcript_store.validate_new_stem(name) -> Optional[str]`:**
   - NFC and strip;
   - non-empty, at most 100 characters (Decisions);
   - no ``/ \ : * ? " < > |`` or control characters;
   - no leading dot (which also refuses `TEMP_PREFIX`), no trailing dot or space;
   - not ending in `.summary` or `.md` (case-insensitive);
   - not `campaign_folders.is_reserved`.

   Returns the cleaned name or None.
5. **`rename_transcript(transcript_id, new_name, *, clash="ask", data_dir=None, output_dir=None) -> MoveOutcome`:**
   - validate;
   - case-only renames are allowed;
   - `check_move(transcript_id, <current campaign>, new_stem=…)`, with the same clash options as moves, and the busy and clash re-checks inside the transaction;
   - in one transaction, update `stem`;
   - after commit (holding `_LOCATION_LOCK`), move the `.md`. If that move returns `error` or `conflict`, restore the old stem with the same compare-and-swap revert as `move_transcript` step 6 and return `locked`, with nothing renamed;
   - then `rename_companions` within `loc.dir`.
6. **Routes** (all `transcript_id: int`, with `Location` built from ints and fixed codes):
   - **`POST /transcripts/{id}/campaign`** (`campaign`, `clash`): on `clash`, redirect to `/transcripts/{id}?clash=move&to=<slug>`. The page recomputes the clash server-side with `check_move` (pure), and shows Overwrite (if allowed), Keep both, and Cancel with the modified time. The slug in the redirect comes from the database row, not the form.
     - `busy` → `?error=busy`; `folder_taken` → `?error=folder_taken`; `locked` → `?error=locked`; `reserved` → `?error=reserved`; `partial` → `?notice=partial_move`.
   - **`POST /transcripts/{id}/rename`** (`new_name`, `clash`): same pattern (`clash=rename`). The confirm text says links typed in your notes don't follow a rename made in wisper.
   - **`POST /transcripts/{id}/move-files`:** `move_files_home`.
   - **`POST /transcripts/bulk-campaign`:** `clash="skip"`, redirecting with `?moved=N&skipped=M&busy=K`, all ints.
   - **The campaign page's remove** → `move_transcript(tid, None, clash="keep_both")`, with the same status mapping on the campaign page.
7. **`campaign_manager`:**
   - `move_transcript_to_campaign` and `remove_transcript_from_campaign` become private helpers (`_assign`) used only by `move_transcript`;
   - `set_campaign_transcript_order` and `_write_order` refuse (`ValueError`) a transcript that isn't already in the campaign, and never unassign one. Moving between campaigns always goes through `move_transcript`. This includes `wisper campaigns reorder --set` (`cli.py` ~1032).
   - `delete_campaign`'s interim unassign (Phase 1) stays until Phase 7.
   - Update their callers. Test seeding assigns through `seed_transcript`/`save_campaigns`' direct `UPDATE` (Phase 1), never through `_write_order`.
8. **CLI** (the existing commands keep their interface):
   - `wisper transcripts move STEM (--campaign <slug> | --no-campaign) [--from <slug>] [--keep-both | --overwrite]` (`cli.py` ~1352). It extends today's command with `--from` and the clash flags;
   - `wisper transcripts rename <name> <new-name> [--campaign <slug>] [--keep-both | --overwrite]`.
   - `<name>` is resolved with `find_by_stem`. When it's ambiguous, `--from`/`--campaign` is required; the error lists the campaigns.
   - Without a flag, a clash prints the existing file's time and exits 1.
9. **Templates and script** (6b):
   - `templates/transcript_detail.html`:
     - the campaign select posts to `/transcripts/{id}/campaign`;
     - a clash panel rendered when the page is opened with `?clash=move&to=<slug>` or `?clash=rename&name=<name>`: the page recomputes with `check_move` and shows the existing file's modified time, **Overwrite** (only when `overwrite_allowed`), **Keep both**, and **Cancel** (a link back to the plain page). Each button is a small form posting `clash=overwrite|keep_both` with the same target;
     - a **Rename** form (one text field, `new_name`), with the note that links typed in notes don't follow;
     - when `loc.misplaced`, a "Files are in the output root, not in <folder>" line with a **Move files** button posting to `/move-files`;
     - the `error`/`notice` codes above shown with fixed messages in the page's existing flash area.
   - `templates/transcripts.html`: the Needs attention misplaced list gets a **Move files** button per session; the bulk-campaign result shows "Moved N, skipped M (name already in the target folder), K busy" from the integer query parameters.
   - `templates/campaigns.html`: the remove result codes shown in the page's flash area.
   - Every query value rendered is escaped by Jinja; nothing from the query string is used to build a path or a link other than through `check_move`'s database result.

**Existing tests to rewrite:**
- `assign_campaign` tests now see files move.
- Bulk-campaign tests assert the counts.
- Tests that moved transcripts through `move_transcript_to_campaign` use `move_transcript`, or `seed_transcript(..., campaign=…)` when files don't matter.

**New tests** (`tests/test_transcript_store.py`, `tests/test_web_routes.py`, `tests/test_cli.py`, `tests/test_path_traversal.py`):
- **Moves:**
  - a move carries every registered file and leaves no row pointing at the old folder;
  - a move to the root;
  - a move into a campaign that has the same name: `ask` → clash (and nothing changed), `keep_both` → `Name (2)`, `overwrite` → the old target is deleted;
  - an `overwrite` against an unregistered `.md` is refused.
- **Locks:** a locked `.md` (patch `os.replace` → `PermissionError` for it) gives `locked` with nothing moved; a locked `.flac` gives `partial`, the session reads misplaced (a companion outside its `.md`'s folder), and `move_files_home` later moves the `.flac`.
- **Busy** (through `JobQueue.submit`, not seeded rows):
  - a queued re-transcribe of the session;
  - a pending journal job on the source or target campaign;
  - a pending upload with `$.campaign`.

  Each gives `busy`; so does `move_files_home` for the first.
- **Locked `.md` with `keep_both`:** the target already holds `S1`, the move picks `S1 (2)`, and the `.md` move fails: the row is back in its source campaign as `S1` (its old position when free), status `locked`, and no companion moved.
- **Revert blocked:** same, but the source campaign gained an `S1` meanwhile: status `partial`, the row stays `S1 (2)` in the target, and no `IntegrityError` escapes.
- **Overwrite** whose target `.md` is locked → `locked`, with nothing changed.
- **Overwrite refused late:** a pending job on the target transcript (`existing_id`) → `busy`, and the target isn't deleted.
- **Missing:** moving or removing a session flagged missing changes only its row (`moved`), and touches no file.
- **Misplaced self-clash:** removing a misplaced session (its `.md` in the root) from its campaign → `moved`, same name, no file moved, no ` (2)`.
- **Case-only rename** on a case-insensitive filesystem isn't a clash with itself.
- **Busy, capture:** a recording of the campaign with status `recording` → `busy`.
- **Submit write:** `job_history.record_required` raising fails `JobQueue.submit`.
- **No writes on a clash render:** rendering the clash page creates no directory (`check_move`).
- **Journal:** a move out of a journaled campaign marks it stale.
- **Rename:** carries companions; case-only works on a case-insensitive filesystem; a locked `.md` restores the old stem; reserved and invalid names are refused.
- **Bulk:** with one clash, it moves the rest.
- **CLI:** an ambiguous name is refused without `--from`.
- **Path traversal:** the rename route's `new_name` payloads (null byte, `../x`, `a/b`, CRLF) are refused with no file touched; so is the GET clash page's `name` query parameter (`?clash=rename&name=../x`), with no disk access before validation.
- **Rename names:** `.hidden`, `.wisper-tmp-1`, `COM1`, and a 101-character name are refused.
- **Submit history:** `record_required` raising makes `submit` raise, queues nothing, and removes the upload folder.
- **Test placement:** the store-level move, lock, and busy tests live in `test_transcript_store.py` and `test_campaign_manager.py` (already in CI's `windows storage` list); `test_web_routes.py` and `test_cli.py` test only the routes and the CLI's status mapping.

**Docs:**
- `docs/web-ui.md`: moving between campaigns moves files, the clash prompt, Rename, and Move files.
- `docs/cli-reference.md`: `transcripts move`, `transcripts rename`.
- `architecture.md`: `move_transcript`, `rename_transcript`, the partial-move state, and the busy guard.
- `docs/scenarios.md`: "A session's files didn't all move" (a file was open; use Move files).

**Done when:**
- `pytest tests/test_transcript_store.py tests/test_campaign_manager.py tests/test_web_routes.py tests/test_cli.py tests/test_path_traversal.py tests/test_job_history.py` pass, and the full suite is green.
- `tests/test_transcript_store.py` is in CI's `windows storage` list (it already is; confirm).
- **Rehearsal:** move a session between two campaigns and back; rename it; try a clash with each choice; run a bulk move with one clash.

---

### Phase 7 — Campaign create, rename, and delete with folders

*Done (opencode 7a/7b; Claude review fixes). Differs from the plan: `_LOCATION_LOCK` now lives in `campaign_folders` and `transcript_store` imports it (one lock per process); `Attention.pending_folders` is a `PendingFolderRename` (slug, campaign, folder, pending, neither) because Retry needs the slug; a keep-files delete also removes a claimed folder left empty, so the name can be reused; claiming a folder registers a journal already in it, and a folder rename registers an unregistered journal under the old name before renaming it; `without_folder` is ignored when either folder exists.*

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

**Hand-offs:** 7a is steps 1, 2, 7, and the delete parts of 8–9 (`check_available`, create, delete, CLI `campaigns delete`); 7b is steps 3–6 and the rename parts of 8–9 (rename, finish, startup, Retry). One commit after 7b.

**Steps:**
1. **`campaign_folders.check_available(name, conn=None, *, output_dir=None, exclude_id=None) -> Optional[str]`:** returns an error code, or None. `output_dir` defaults to `get_output_root()`; when the root doesn't exist, the disk check finds nothing (the folder is claimed later, on first write).
   - `"taken"`: `name.casefold()` equals another campaign's `folder` or `folder_pending` casefold.
   - `"folder_exists"`: an entry in the output root whose NFC casefold equals `name`'s, and that isn't this campaign's current `folder`.
     - Exception: a directory holding only `journal_name(<its name>)` (ignoring `is_clutter` files, Phase 1), left by a keep-files delete, is available and is re-claimed.

   **Codes everywhere** (one table, used by the routes and the CLI):

   | Source | Code | Route `?error=` | Message |
   |---|---|---|---|
   | empty or unusable name | `invalid` | `invalid_name` | "Enter a campaign name." |
   | slug used by another campaign | `slug_taken` | `campaign_exists` | "A campaign with that name already exists." |
   | `check_available` → `taken` | `taken` | `campaign_exists` | same |
   | `check_available` → `folder_exists` | `folder_exists` | `folder_taken` | "A folder with that name already exists in your transcripts folder." |
   | `ensure_folder` → `FolderTakenError` | `folder_taken` | `folder_taken` | same |
   | `active_jobs` non-empty | `busy` | `busy` | "A job is running for this campaign; try again when it finishes." |
   | `folder_pending` already set and still unfinished, or `FolderPendingError` from `ensure_folder` | `pending` | `rename_pending` | "The last rename of this campaign's folder hasn't finished; Retry it under Needs attention first." |
   | `FolderMissingError` from `ensure_folder` | `folder_missing` | `folder_missing` | "The campaign's folder is missing from your transcripts folder; see Needs attention." |
   | a session named like the renamed journal | `reserved` | `reserved` | "A session in this campaign is named like the renamed journal; rename that session first." |
   | an unadoptable legacy journal can't follow the slug | `legacy_journal` | `journal_legacy_pending` | "This campaign's old journal is waiting to be moved; see Needs attention." |

   Database checks (`slug_taken`, and `check_available`'s `taken`) run **inside** the write transaction with its `conn`. An `sqlite3.IntegrityError` from the write is still mapped: a message containing `campaign folder taken` or `campaigns.folder` → `taken`; `campaigns.slug` → `slug_taken`. The `folder_exists` disk check runs before the transaction (it can't be made atomic, and a folder appearing in between is caught by `ensure_folder`).
2. **`create_campaign`:**
   - `folder = folder_name(display_name)`; `check_available`; refuse with a `CampaignError(code)` (a new exception carrying a table code, subclassing `ValueError` so existing callers and tests that catch `ValueError` for empty or duplicate names keep working). No ` (2)` suffix: `unique_folder` is no longer called here;
   - insert the row, commit, then `ensure_folder`, which claims the folder. If it raises (`FileNotFoundError` for an absent root, or a folder that appeared meanwhile), the campaign still exists and the folder is made, and claimed, on first write.
   - `create_campaign(display_name, data_dir=None, *, output_dir=None)` gains `output_dir` for tests.
3. **`campaign_folders.rename_campaign(slug, new_display_name, *, data_dir=None, output_dir=None) -> RenameOutcome`.** `status` is `"renamed"` or one of step 1's codes (`pending`, `busy`, `invalid`, `slug_taken`, `taken`, `folder_exists`, `folder_taken`, `folder_missing`, `reserved`, `legacy_journal`), plus `new_slug`; routes and the CLI map each through the table. Steps:
   1. Strip the display name; `new_slug = _make_slug`; empty → `invalid`. A `new_slug` used by another campaign → `slug_taken`.
   2. `new_folder = folder_name(new_display_name)`; `check_available(new_folder, exclude_id=self)`. An exact match with the current folder means no folder change.
   3. **Legacy journal:** `adopt_legacy_journal(slug)`, then `sync_journal(slug)`. If a legacy journal still exists (`legacy_journal_path(slug)` or its pending file; adoption returned `folder_taken`, `unavailable`, `kept`, or `failed`), the slug change must carry it: before the transaction, `os.replace(<data>/campaigns/<old>, <data>/campaigns/<new>)` (same volume); refuse with `legacy_journal` (→ `?error=journal_legacy_pending`) if `<data>/campaigns/<new>` already exists. The whole directory moves, so a legacy pending file and `journal.md.v11-adopted` go with it. If the transaction then doesn't rename (any non-`renamed`/`pending` status or an exception), move it back. A crash between the two leaves the legacy file under the new slug while the row has the old one; nothing resets (the journal is only reset when its claimed folder exists), and Needs attention lists `<data>/campaigns/*/journal.md` whose slug has no campaign as "orphan legacy journal".
   4. **An earlier rename still pending:** if `folder_pending` is set, call `finish_folder_rename` first; if it's still pending → return `pending` (code `pending`), changing nothing. A second rename must never overwrite or clear a pending one: the disk may already be at the pending name.
   5. **One transaction:**
      - `active_jobs(conn, campaign_ids={id}, campaign_slugs={slug})` non-empty → `busy`;
      - `slug_taken` and `taken` checks (step 1's table, with this `conn`);
      - **reserved:** the campaign holds a session whose stem casefolds equal to `journal_name(new_folder)` minus `.md` → `reserved` (that session would collide with the renamed journal);
      - then one statement per case (rowcount 0 → re-read: a pending rename → `pending`, a changed claim → retry once):
        - **claimed** (`folder_claimed = 1`): `UPDATE campaigns SET display_name = ?, slug = ?, folder_pending = ? WHERE id = ? AND folder_pending IS NULL AND folder_claimed = 1`; `folder_pending` is NULL when `new_folder == folder`;
        - **unclaimed:** `UPDATE campaigns SET display_name = ?, slug = ?, folder = ? WHERE id = ? AND folder_pending IS NULL AND folder_claimed = 0` (nothing of wisper's on disk, so no pending step).
        - An `sqlite3.IntegrityError` from either is mapped per step 1's table, never raised.
   6. If a folder change is pending: `finish_folder_rename(campaign_id)`. Return `renamed`, or `pending` if that left it pending.
4. **`finish_folder_rename(campaign_id, *, without_folder=False, data_dir=None, output_dir=None) -> bool`** (True when done). `without_folder=True` is only for the "neither directory" case below: it runs step 5 database-only and sets `folder_claimed = 0`.
   1. Read `folder` → `:old`, `folder_pending` → `:new`. If `folder_pending` is NULL → True.
   2. **Busy (early exit):** `active_jobs(campaign_ids={id}, campaign_slugs={slug})` non-empty → False, still pending. A queued upload into the old folder must land before the folder moves. Step 5 checks again inside its transaction.
   3. **Output root:** `output = get_output_root()`; if it isn't a directory → False, still pending (an unmounted drive must never commit the swap). Acquire `_LOCATION_LOCK` (held through step 7). `old_dir = output / :old`, `new_dir = output / :new`; `renamed_here = False`.
   4. **Disk** (every case spelled out):
      - **`old_dir` absent, `new_dir` absent:** return False, still pending, unless `without_folder` (then step 5 database-only, `folder_claimed = 0`; the next write claims the new name). Needs attention's pending entry shows a **Finish without folder** button for this case: `POST /campaigns/{slug}/finish-rename` with form field `without_folder=1`.
      - **`old_dir` absent, `new_dir` present:** the main crash-recovery case (the rename happened, the commit didn't). Go to step 5, but only if `new_dir` holds nothing beyond clutter and file names that match registered rows under the `:old/` prefix; otherwise it's a folder the user made, so return False (listed as pending, with its text saying the target folder is taken).
      - **Both present and the same directory** (`os.path.samefile`; a case-only rename on a case-insensitive filesystem): `os.rename(old_dir, new_dir)` (it changes the case), then step 5.
      - **Both present and different:** return False, leaving it pending.
      - **`old_dir` present, `new_dir` absent:** `os.rename(old_dir, new_dir)`, retried 3× with 0.5 s between attempts on `PermissionError`/`OSError`; still failing → False.
      - After any successful `os.rename`, `renamed_here = True`.
   5. **One transaction (compare-and-swap):**
      - `active_jobs(conn, …)` again; non-empty → raise `FolderBusy` (handled in step 6 like any raise);
      - **re-check the disk under the write lock:** `new_dir.is_dir()` and (`not old_dir.exists()` or `os.path.samefile(old_dir, new_dir)`), unless `without_folder`; otherwise roll back and return False (another process renamed it back meanwhile);
      - `cur = conn.execute("UPDATE campaigns SET folder = folder_pending, folder_pending = NULL WHERE id = ? AND folder = :old COLLATE BINARY AND folder_pending = :new COLLATE BINARY RETURNING folder", …)`; `row = cur.fetchone()`. **Never read `cur.rowcount` on a `RETURNING` statement:** Python's sqlite3 reports 0 until the row is fetched. `row is None` → another process finished it: return True, touch nothing. Otherwise the prefix rewrite uses `row[0]`. Both columns are `COLLATE NOCASE`, so without `COLLATE BINARY` a stale caller holding `hanataz → Hanataz` would match a later pending `HANATAZ`;
      - refuse (raise) if another campaign's `folder` or `folder_pending` casefolds equal to `:old` (the database's NOCASE is ASCII-only);
      - rewrite the prefix **in Python, row by row**: `SELECT id, rel_path FROM files WHERE root = 'output' AND instr(rel_path, '/') > 0`; for each row whose first path component matches `:old` under `file_registry._key(nfc(component), fold=True)`, `UPDATE files SET rel_path = :new || '/' || <rest> WHERE id = ?`. Rows carry the disk's spelling of the folder (`db.to_rel` uses `realpath`, which returns the on-disk case on Windows; Finder can store NFD), so a byte-exact SQL prefix match would miss them. Folding is safe: `campaigns.folder` is unique ignoring case, and only claimed folders hold registered rows;
      - then assert, in the same transaction, that every output row whose first component folds to `:old` now has a first component equal to `:new` **byte for byte**. (For a case-only rename `:new` folds to `:old`, so "nothing folds to `:old`" would always fail.) If any row doesn't, raise, which rolls back.
   6. **If step 5 raises and `renamed_here`:** open `BEGIN IMMEDIATE`, re-read the row, and only if it still reads pending `:old → :new`, `os.rename(new_dir, old_dir)` back while holding it; then roll back. `FolderBusy` → return False; any other exception is re-raised.
   7. **After commit:** the journal file `<old folder> Journal.md`, now inside `new`, moves to `journal_name(new folder)` (`file_registry.move`). A conflict or error leaves it under its old name. `journal_path` follows the registry row, so it still works; the next rename or a Needs attention Retry fixes the name.
5. **Startup** (`web/app.py`, after `adopt_legacy_journals`, before `reconcile`): `campaign_folders.finish_pending_renames()` calls `finish_folder_rename` once for every campaign with `folder_pending`. It never loops or sleeps beyond step 4's retries. It catches and logs any exception per campaign (`log.exception`), leaving that rename pending for Needs attention; startup never fails on it.
   - `ensure_folder` (Phase 1) on a campaign with `folder_pending` calls `finish_folder_rename` first. If that returns False, it raises `FolderPendingError` (code `pending`, `?error=rename_pending`, step 1's table). No session is ever written into a folder that's mid-rename.
   - CLI commands that scan (`wisper transcripts list`, `storage trim`) call it too.
6. **Needs attention** pending renames get a **Retry** button: `POST /campaigns/{slug}/finish-rename`, with `slug` validated as today (and a `test_path_traversal.py` case for it). The panel text: "Rename of folder X to Y didn't finish: a file in it is open in another program. Close it, then Retry."
7. **`delete_campaign(slug, *, delete_transcripts=False, …)`.** Phase 1's interim unassign goes; the campaign is deleted only when it's empty.
   - **Both ways start with a busy check:** `active_jobs(campaign_ids={id}, campaign_slugs={slug})` non-empty → `busy`, nothing deleted (a queued upload into the folder would otherwise recreate it after the delete).
   - **Delete everything:**
     - `delete_transcript(id)` for each;
     - if any returns `"kept"` (its `.md` was locked), **stop**: the campaign stays, holding what's left, and the result says how many;
     - otherwise delete the journal file (only a registry path, Phase 1) and the campaign;
     - then, **only when `folder_claimed = 1`**, `os.rmdir(folder)`, ignoring `OSError` (a non-empty folder stays).
   - **Keep the files:**
     - `move_transcript(id, None, clash="keep_both")` for each, one at a time;
     - `moved` and `unchanged` continue (a missing or misplaced session is a database-only move, Phase 6). **Any other status** (`locked`, `partial`, `busy`, and, though moving to the root can't produce them, `folder_taken`, `clash`, `reserved`) **stops** the delete: the campaign stays, and the result names the sessions that didn't move;
     - otherwise delete the campaign. Its journal row cascades, and the journal file and folder stay on disk, untracked.
   - **The final `DELETE`** catches `sqlite3.IntegrityError` (a reconcile in another process registered a new `.md` in the folder) and returns `delete_incomplete`.
   - Returns a `DeleteOutcome(status: Literal["deleted", "busy", "kept", "delete_incomplete"], kept: list[str])`. Routes map it to `?error=busy` or `?error=delete_incomplete` with the count. The CLI `wisper campaigns delete` (find by name in `cli.py`) prints the outcome and the kept names, exits 1 unless `deleted`, and its tests in `test_cli.py` are rewritten for it.
8. **Routes and UI:**
   - `POST /campaigns/{slug}/rename` (`display_name`) → 303 to `/campaigns/<new slug from the database>`, or `?error=<code>`;
   - a Rename form in the campaign page header;
   - the delete dialog's two choices keep their wording, plus "If a session's file is open in another program, the campaign is kept until you retry.";
   - **Delete everything** in the dialog lists the session count and names (from `Campaign.transcripts`), and says "Every `.md` file in the campaign's folder is one of these sessions; all of them, with their audio and summaries, are deleted."
9. **CLI:**
   - `wisper campaigns rename <slug> "<New Name>"` prints the new slug and folder, or the pending notice;
   - `wisper campaigns show` prints `Folder: <folder>` (and `Rename pending → <folder_pending>`).

**Existing tests to rewrite:**
- `create_campaign` tests now see a folder created.
- Delete tests: keep-files moves the files to the root.
- Delete-everything with a locked file keeps the campaign (a deliberate change from the unassign behaviour; see Decisions).

**New tests** (`tests/test_campaign_folders.py`, `tests/test_campaign_manager.py`, `tests/test_web_routes.py`, `tests/test_cli.py`, `tests/test_path_traversal.py`):
- **Create:**
  - create makes and claims the folder;
  - with a pre-existing `hanataz` folder (any case), `Hanataz` is refused (`folder_exists`);
  - a display name sanitizing to an existing campaign's folder is refused (`taken`);
  - after a keep-files delete, re-creating the same campaign re-claims its folder, and its journal is found again.
- **Rename:**
  - renames the folder, rewrites every `files.rel_path` under it (including names with `[`, `*`, `?`), renames the journal file, and changes the slug; the old slug URL gives not_found;
  - a case-only rename works (patch `_fold` True; on a case-sensitive test filesystem, assert the database is right);
  - `os.rename` raising `PermissionError` → `pending`, and nothing under `files` changes; `finish_folder_rename` after "closing" completes it.
- **Crash recovery:**
  - simulate a crash after the rename, before the transaction (rename the dir by hand with `folder_pending` set); `finish_pending_renames` completes it;
  - neither directory present → the database is updated only.
- **Rename refusals:** busy (a pending relabel job, or a pending upload with `$.campaign`), and `slug_taken`.
- **Second rename while pending:** with `folder_pending` set and the old folder locked, a second rename returns `pending` and leaves `folder_pending` unchanged.
- **Prefix spelling:** rows registered as `hanataz/S1.md` and NFD `Été/S2.md` under campaigns `Hanataz` and `Été` are rewritten by a rename (patch `_fold`); the assertion doesn't fire.
- **Case-only rename** `hanataz` → `Hanataz` commits, every row starts `Hanataz/`, and the rename isn't left pending.
- **Stale compare-and-swap:** with the row pending `Alpha → ALPHA2`, a caller holding `Alpha → alpha2` changes nothing.
- **Successful swap:** every `files` row under the folder is rewritten (guards against reading `rowcount` on `RETURNING`).
- **Disk re-check:** the folder renamed back between step 4 and step 5 (patched) → False, nothing committed.
- **Foreign target:** pending `A → B`, `A` gone, and a user-made `B/` holding `notes.md` → False, `B/` untouched.
- **Legacy journal follows a rename:** a folder-taken campaign with an unadopted legacy journal is renamed; the journal is then adopted under the new slug with its entries intact.
- **Reserved:** renaming campaign Y to X while Y holds a session `X Journal` → `reserved`.
- **Finish without folder:** both directories gone; the POST with `without_folder=1` commits the swap with `folder_claimed = 0`.
- **Root unmounted:** `finish_folder_rename` with the output root removed returns False and leaves `folder`, `folder_pending`, and every `files` row unchanged; startup does the same.
- **Neither directory:** returns False and stays pending.
- **Old-named journal after a failed post-commit rename:** the next reconcile registers no transcript for it.
- **Startup:** `finish_pending_renames` with a rename whose step 5 raises logs it, leaves it pending, and returns.
- **Busy inside the swap:** a job submitted between step 2 and step 5 (patch `os.rename` to submit one) → the folder is renamed back and the rename stays pending.
- **Raced create:** two creates of the same name in one test (the second's pre-check patched to pass) → the second is `CampaignError("taken")` or `slug_taken`, never a bare `IntegrityError`.
- **Finishing a rename:**
  - `finish_folder_rename` with a queued upload into the campaign returns False and changes nothing;
  - two callers finishing the same rename (the second after the first commits) leave one consistent result, and the second neither raises nor renames back;
  - `ensure_folder` during a stuck pending rename raises `FolderPendingError`;
  - renaming an unclaimed campaign changes only the database.
- **Delete:**
  - keep-files with a name clash in the root → `Name (2)`;
  - with a locked file, the campaign is kept;
  - delete-everything removes an empty folder and keeps a folder holding a user file;
  - keep-files with a missing session and a misplaced one deletes the campaign, both sessions unassigned with their names unchanged;
  - either way with a pending job on the campaign → `busy`, nothing deleted;
  - delete-everything of an unclaimed campaign leaves a same-named user folder and its `X Journal.md` untouched.
- **Path traversal:** the rename route's `display_name` with `../`, null byte, and CRLF payloads is rejected or sanitized, and never escapes the output root.

**Docs:**
- `docs/web-ui.md`: rename, and the delete outcomes.
- `docs/cli-reference.md`: `campaigns rename`, `campaigns show`.
- `architecture.md`: the folder rename's two steps and crash rule, `check_available`, and never adopting a user folder.
- `docs/scenarios.md`: "Renaming a campaign says the folder is busy", and "I renamed or deleted a campaign folder outside wisper" (Recreate folder, or rename the campaign).

**Done when:**
- The listed test files pass, and the full suite is green.
- `tests/test_campaign_folders.py` is in CI's `windows storage` list.
- **Rehearsal:**
  - rename "Impossible Landscapes" to "Impossible Landscapes: Delta Green" (the colon is sanitized) and back;
  - hold a file open with a background process (`tail -f` on macOS doesn't lock, so patch-only here; the Windows check is in Phase 10);
  - delete a scratch campaign each way.

---

### Phase 8 — `wisper storage trim` organizes folders; prune backups

*Done (opencode; Claude review fixes). Differs from the plan: `TrimPlan.blocked` carries the blocked-campaign lines; `wisper server` reports the upgrade itself, because its pre-start `connect()` migrates before the app's lifespan sees the old version (this hid PR #69's notice too); the dry run's "Needs attention (N)" counts only what it lists below it.*

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
   - Sessions of a campaign whose folder is taken, pending, or missing aren't misplaced (Phase 2a), so they get no action. The dry run and `--apply` instead print one line per such campaign, every run until it's fixed: "Hanataz: 3 sessions can't be organized (folder taken; see Needs attention)".
   - `TrimPlan.total_bytes` and `convert_bytes` exclude it. A new `move_bytes` property sums it.
   - The dry-run summary prints "Move N sessions into their campaign folders (X MB, nothing deleted)".
2. **`plan()` also lists legacy journals** still at `<data>/campaigns/<slug>/journal.md` as `ORGANIZE` actions, with `note="journal"`.
3. **`apply()` runs organize first,** then **plans again**, so the conversion actions name the files' new paths (`apply` computes `current = plan()` once today, `storage_trim.py` ~338):
   - for each, `move_files_home(tid)`;
   - `clash`, `reserved`, or `folder_taken` → a report error ("<name>: a file with that name is already in <folder>", or "<name> is named like the campaign's journal"), skipped;
   - `unavailable` → stop organizing ("the transcripts folder isn't available");
   - `partial` → an error naming the files kept;
   - journals → `journal.adopt_legacy_journal(slug)`;
   - it calls `campaign_folders.finish_pending_renames()` before planning.

   `TrimReport.organized: list[str]`.
4. **Backup pruning** (`db.py`):
   - after `COMMIT` in `migrate()`, when a snapshot was taken, `_prune_snapshots(data_dir, keep=5)` deletes all but the newest 5 files matching `backups/wisper-v*-*.db`;
   - it sorts by the parsed `-<YYYYmmddTHHMMSSZ>.db` stamp, not the whole name (`v10` sorts before `v9` as text);
   - `wisper db status` (`cli.py` `db_status`, ~2041, where it formats `m['backup_dir']`) shows a backup path that no longer exists as `backup <path> (pruned)`;
   - it never touches other files or directories in `backups/` (legacy import dirs, reports);
   - an `OSError` is logged.
5. **`wisper storage trim` output** lists organize actions first. A re-run after `--apply` prints nothing to organize.
6. **Startup upgrade notice** (`web/app.py` `_report_upgrade`, PR #69): it returns early with `if before >= 9: return` (~114). The new check goes **before** that line: when an upgrade crosses v11 (`before < 11 <= db.LATEST_VERSION`) and any session is misplaced (`transcript_store.needs_attention().misplaced` is non-empty), log: "Run `wisper storage trim --apply` (with the server stopped) to move existing sessions into their campaign folders." The existing "Stored audio can be shrunk" message below it counts only non-`ORGANIZE` actions, so it doesn't fire for a database with only sessions to organize.

**Existing tests to rewrite:** `test_storage_trim.py` tests that assert the exact action list or order.

**New tests:**
- **Organize:**
  - a v11 database with assigned transcripts in the root: the dry run lists them and moves nothing;
  - `--apply` moves every file into the folder and leaves none misplaced, and a re-run plans nothing;
  - a clash in the folder is reported and skipped;
  - a legacy journal is moved;
  - organize runs before convert, so the FLAC lands in the folder.
- **Pruning:**
  - 7 snapshots (including `wisper-v9-…` and `wisper-v10-…` with interleaved stamps), plus a legacy backup dir and a report → the newest 5 by stamp remain, and the dir and report are untouched;
  - `db status` marks a pruned backup.
- **Blocked campaign:** a campaign whose folder is taken is reported, not planned, on every run.
- **Notice:** a v10 → v11 upgrade with an assigned session in the root shows the organize line and not the audio line; a fresh install shows neither; a v8 → v11 upgrade with only organize actions shows only the organize line.

**Docs:**
- `docs/cli-reference.md` and `docs/scenarios.md`: `storage trim` also moves sessions into their campaign folders, and run it once after upgrading.
- `docs/configuration.md`: backups keep the newest 5 snapshots.
- `architecture.md`: the storage trim actions.

**Done when:**
- `pytest tests/test_storage_trim.py tests/test_db.py` and the startup-notice tests in `tests/test_web_routes.py` (`test_startup_reports_a_database_upgrade_and_suggests_a_trim`, ~3748) pass, and the full suite is green.
- **Rehearsal:** on a fresh scratch copy (v10), start the server (it migrates to v11), stop it, run `wisper storage trim` (lists 4 sessions), then `--apply`. `Impossible Landscapes/` holds the 4 sessions with their FLACs, summaries, sidecars, and excerpts; pages and playback work; a re-run is clean.

---

### Phase 9 — Holistic docs, comments, and tests review (worker, read and fix; no behaviour changes)

*Done (opencode; Claude review fixes). The worker found no bugs and added tests for Decisions rows 12, 17, 18 and 28. The orchestrator's read-through fixed `architecture.md`'s flat output-tree diagram, the lock's home, and the create/delete folder wording.*

A worker may add tests here (for a Decisions row with none). A test that exposes a bug is reported to the orchestrator, marked in the report, and not fixed in this phase; the orchestrator decides whether a fix phase runs before Phase 10.

**Goal:** the documentation, code comments, docstrings, and tests read as one current, consistent description of the system, under the Documentation and Test standards. Phases 1–8 each kept their own changes clean; this phase checks the whole as one piece.

**Scope:** every file the branch changed (`git diff --name-only main...HEAD`), plus every doc that describes something the branch changed even where the doc wasn't touched:
- `README.md`;
- `architecture.md`;
- every file in `docs/`;
- `CLAUDE.md`;
- `.claude/rules/web-security.md`;
- `docker-compose.yml` comments.

**Steps:**
1. **Inventory.** List every behaviour this branch changed (Decisions table, and each phase's Goal). For each, find every place that describes it: `grep -rn` its key words in `README.md`, `architecture.md`, `docs/`, `CLAUDE.md`, `src/` comments and docstrings, and `tests/` names and docstrings. Key words include:
   - output root, campaign folder, journal, `journal.md`;
   - `/transcripts/`, stem, misplaced, rename, move, slug, organize, `storage trim`, backup.
2. **Facts, not worklog.** Every hit states the current behaviour and its reason. Rewrite any that:
   - narrate history ("now", "no longer", "previously", "changed", "was", "used to", "new:");
   - cite phases, findings, PRs, or dates;
   - name this project ("campaign folders" as a project).
3. **No contradictions.** The same fact is stated the same way everywhere. Where two docs disagree, fix the wrong one. Where a fact is repeated in full, keep it where it belongs (Documentation Rules table in `CLAUDE.md`) and link to it from the others.
4. **No stale statements.** Search for the old model and remove or correct every hit:
   - "flat" output root; `campaigns/<slug>/journal.md` in the data dir;
   - `/transcripts/{name}` and `| urlencode` on transcript links;
   - `campaign_transcripts`;
   - "a transcript's files are named after its stem in the output root";
   - "move only changes the database";
   - unassign-on-delete;
   - the `overwrite=on` form field.
5. **Comments and docstrings:**
   - one line of *why* per comment, none restating the code;
   - docstrings say what and why;
   - no "TODO" without a `plan.md` entry.
6. **Tests:**
   - each test name and docstring states the behaviour it checks (present tense);
   - no test asserts behaviour this branch removed;
   - no skipped or xfail test added by the branch without a reason in its marker;
   - every Decisions row has at least one test (list which in the phase report);
   - every new module has its test file;
   - every storage-touching test file is in CI's `windows storage` list.
7. **Scar-tissue grep** over the whole branch diff (Documentation standard item 6), and over the full text of `architecture.md`, `docs/`, and `CLAUDE.md`. Each hit is rewritten or justified in the report.
8. **README:** only its description, quick start, or docs table changed; it mentions campaign folders in the description if the feature changes what wisper does at a glance.

**Done when:**
- The full suite is green.
- **The phase report lists:**
  - each file reviewed;
  - each change made, as a one-line reason;
  - the Decisions-to-tests mapping;
  - the greps run, with their (empty or justified) output.
- **The orchestrator's check:** it reads `architecture.md`'s storage and campaign sections end to end, and `docs/web-ui.md`'s campaign and transcript sections, and finds no contradiction or worklog phrasing.

---

### Phase 10 — Final review and rehearsals (orchestrator; no new features)

*Carried from the Phase 3 review: `transcript_store.list_transcripts` and the campaign page call `locate` once per session, and each `locate` checks its campaign folder on disk (`_target_blocked`). Fine at today's library size; time the Transcripts page on the rehearsal copy and batch the per-campaign check if it's slow.*

1. **Full suite**, with `--cov`. Add the campaign-folder flow to `tests/test_e2e.py` if Phase 9 didn't:
   - upload into a campaign;
   - move, rename, and rename-campaign;
   - storage trim organize;
   - delete keep-files.

   Coverage: New code (`campaign_folders`, the Locations section, the move and rename functions) has only error branches uncovered.
2. **Greps, each clean or explained:**
   - `campaign_transcripts` (only frozen migrations and `legacy_import`);
   - `_get_safe_content_path`, `for_stem`, `_under_output_root` (none);
   - `glob("*.md")` (only in `transcript_store`'s scan);
   - `get_output_dir() /` (none outside `transcript_store`/`file_registry`/`campaign_folders`/`journal`/`path_utils`);
   - `/transcripts/` in templates and `static/` (every transcript link by id, including `~` concatenation and JavaScript template strings);
   - the scar-tissue grep.
3. **Docs vs code:** spot-check that Phase 9's sweep holds: pick 10 statements at random from the changed docs and check each against the code.
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
   - before migrating, count `SELECT kind, count(*) FROM files WHERE root = 'output' AND rel_path GLOB '*/*' GROUP BY kind` (rows already in a subfolder; expected none) and the transcripts with no `transcript` files row, on both the Mac and Windows copies, and record them here;
   - v10 → v11, `wisper db status` clean;
   - `storage trim` dry run, then `--apply`;
   - rename a campaign while one of its files is open in Obsidian → pending; close it → Retry completes;
   - move a session while its `.md` is open → `locked` (nothing moved); with only its `.flac` open → `partial`, then Move files;
   - change a claimed campaign folder's case in Explorer, then rename the campaign: it completes (the prefix rewrite folds case).
7. **Rollback rehearsal** (Mac copy, once): after step 4, follow `docs/configuration.md`'s "Going back to an older version" with a `main` build; the journal page shows its folded sessions (no reset) and the transcripts list is complete.
8. **Steps that need Brandon:** set `SCHEMA_FROZEN = True` in the PR commit; remove this section from `plan.md`; open the PR (ask first); the Windows rehearsal (step 6) before his next `start.bat`.

---

### Interactions with other plans

- **Campaign-level LLM summaries (DM tools):** their outputs go in the campaign folder beside the journal (`<folder> Combined Summary.md`, `<folder> Recap.md`), registered as `files` rows owned by the campaign. That needs new `files.kind` values and a new migration in that plan. Folder renames carry them, because Phase 7's prefix rewrite covers every row under the folder; the per-name rename after commit in Phase 7 step 4.7 must include them too.
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
