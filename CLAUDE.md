# wisper-transcribe — Claude Instructions

> **Full references:** [README.md](README.md) (overview + quickstart) · [docs/](docs/) (full user docs) · [architecture.md](architecture.md) (technical deep-dive)

---

## Documentation Rules (always apply)

Keep docs in sync with code as part of every task; it's step 2 of the Definition of Done.

| Doc | Update when |
|-----|-------------|
| `architecture.md` | Any new module, pipeline change, design decision, or config key change. Update: module map entry, relevant design-decision section, config key list, Known Constraints table. |
| `README.md` | Only when the **one-paragraph description** or **Option A quickstart** changes, or the docs table of contents needs a new entry. README is a ~60-line landing page: what it does, Option A quick start, and links to `docs/`. Do not add detail here — put it in the right `docs/` file instead. |
| `docs/setup.md` | New install path, changed requirements, HF token flow, model size guide, or anything about first-time setup. |
| `docs/cli-reference.md` | New or changed CLI command, flag, or output format. |
| `docs/web-ui.md` | New web UI page, changed UI behaviour, or new job management feature. |
| `docs/docker.md` | Docker, Makefile targets, volume layout, or Discord bot setup changes. |
| `docs/configuration.md` | New or changed env var, data storage location, or debugging flag. |
| `docs/scenarios.md` | New common scenario, known limitation added or resolved. |
| `plan.md` | All active plans, research findings, and open design decisions live here. When work is completed, remove it from `plan.md` — unless the context directly informs a remaining action item, in which case keep only the relevant excerpt. |

Doc updates go **in the same commit** as the code change, not as a follow-up.

---

## Definition of Done

A task is not complete until all four are true — in this order:

1. **Tests pass** — the full suite is green (see Commands for the per-OS invocation)
2. **Docs updated** — every doc the Documentation Rules table above calls for
3. **Tailwind rebuilt** — `tailwind.min.css` is rebuilt and committed if it changed. Any text-file change can alter it, because Tailwind v4 scans the whole repo, including Markdown and docstrings.
4. **Committed** — all changed files in a single `git commit`

A Claude Code pre-commit hook (`.claude/hooks/pre_commit.py`) runs steps 1 and 3 on every `git commit`. It blocks the commit if tests fail (~30 s) or if the rebuilt `tailwind.min.css` differs and isn't staged. The hook runs before the command, so rebuild and `git add` the CSS in one step and commit in the next. It warns when `src/` changes have no doc changes. While iterating, run only the tests you need; the hook runs the full suite at commit. A blocked commit means fix and retry — never bypass it.

When a todo list reaches 100% completed, do steps 2–4 immediately without waiting to be asked.

Commits (and pushing the feature branch) are authorized as part of completing any task per the Definition of Done — no separate permission required. Opening or merging a PR is not: ask the user first.

---

## Commands

```bash
# Install / editable mode (always use .venv)
.venv/bin/pip install -e .            # Mac/Linux
.venv\Scripts\pip install -e .        # Windows

# Run tests
.venv/bin/pytest tests/ -v            # Mac/Linux
.venv\Scripts\pytest tests/ -v        # Windows

# With coverage (matches CI)
.venv/bin/pytest tests/ -v --cov --cov-report=term-missing

# Run web server
wisper server --reload                # dev mode; http://localhost:8080

# Rebuild Tailwind CSS (the pre-commit hook also does this)
.venv/bin/python -m wisper_transcribe.tailwind        # Mac/Linux
.venv\Scripts\python -m wisper_transcribe.tailwind    # Windows

# Manage vendored web assets (htmx, fonts, Tailwind)
python scripts/vendor.py --check    # audit current state
python scripts/vendor.py            # re-download + rebuild all assets
# Run when bumping HTMX version or changing font subsets, then commit static/

# Measure forced word alignment on real audio (output: alignment-eval/, gitignored)
python scripts/alignment_eval.py run <audio> --start <s> --duration 180 --out alignment-eval/<name>
python scripts/alignment_eval.py audit|sheet alignment-eval/<name>
python scripts/alignment_eval.py score alignment-eval/*/
# Re-run before changing the aligner, smoothing thresholds, or the forced_alignment default
```

---

## Git / CI Rules

- **Never push to `main` directly.** All changes go through a PR.
- **Push frequently when running in the Claude Code app** — after each commit, push the branch so work is available to pick up from another device. Use `git push -u origin <branch>`.
- **After committing a phase, pause for user review before starting the next.**
- **Branch naming:** `feat/...` or `fix/...`
- **CI matrix:** Python 3.13 and 3.14 — the versions the project ships on (Docker `python:3.14-slim`; local-`.venv` floor 3.13). Both are blocking. We deliberately do not test versions we don't ship; `requires-python = ">=3.13"` and the `setup.sh`/`setup.ps1` floor checks must stay in sync with the lowest matrix entry.
- **CI Tailwind staleness check:** CI rebuilds `tailwind.min.css` and fails on `git diff --exit-code` if the committed CSS is stale.
- **Tailwind version is pinned** in `wisper_transcribe/tailwind.py` (`TAILWIND_VERSION`). Different releases emit different CSS, so always build through `python -m wisper_transcribe.tailwind`, never `python -m pytailwindcss` directly. To upgrade, bump the constant and commit the rebuilt CSS.

---

## Testing Rules

- No GPU, no network, no real audio in tests — mock everything ML-related.
- Mock targets: `faster_whisper.WhisperModel` (imported lazily inside `transcriber`, so patching `wisper_transcribe.transcriber.WhisperModel` needs `create=True`), `wisper_transcribe.diarizer.Pipeline`, `wisper_transcribe.speaker_manager.load_profiles`.
- Web tests use `fastapi.testclient.TestClient` with all ML calls mocked.
- Seed data the way the app stores it — `tests/_seed.py` (`seed_profile`, `seed_recording`, `seed_job`, `seed_sidecar`) — rather than writing JSON-era files; `tests/_legacy_store.py` is for importer tests only. Schema rules belong in `test_schema.py`, whole-flow checks in `test_e2e.py`.
- Every new module needs a `tests/test_<module>.py`.

---

## Security (public repo)

- **No secrets in source.** HF token lives in `platformdirs` user data dir or `HUGGINGFACE_TOKEN` env var — never in code.
- **No real audio files committed.** `example-file/` is gitignored.
- **No personal data in tests.** Synthetic/fake data only.
- If a secret is accidentally committed, treat it as compromised immediately.
- **Web route security** (path traversal, open redirect, CodeQL taint rules) lives in `.claude/rules/web-security.md` and loads when you work under `src/wisper_transcribe/web/`. Read it before adding a route.

---

## Key Conventions

| Rule | Why |
|------|-----|
| Always `pathlib.Path`, never string paths | Cross-platform (Windows backslash) |
| Always `get_data_dir()` from `config.py` for user data | Respects `WISPER_DATA_DIR` env var (Docker) |
| URL-encode transcript stems in templates with `\| urlencode` filter | Filenames may contain em-dashes, spaces, `!`, `()` |
| Use `os.path.basename` + `abspath/startswith` for path guards, not `Path.resolve()` | CodeQL only recognises `os.path` as a path sanitiser |
| Use `_validate_job_id()` then redirect via `job.id` (UUID) | `_validate_job_id` gates access; `job.id` (uuid4, untainted) breaks CodeQL taint chain in redirect URL |
| Redirect `Location` headers use `urllib.parse.quote(name)` | latin-1 codec rejects non-ASCII characters |
| Never put `str(exc)` in a redirect URL or error response | Information disclosure; use generic error codes |

---

## Environment Variables

Full list in `docs/configuration.md`. The ones that affect code: `WISPER_DATA_DIR` overrides the data dir (always go through `get_data_dir()`), and `get_hf_token()` accepts `HF_TOKEN` or `HUGGINGFACE_TOKEN` and propagates the value to both in `os.environ`.

---

## Non-Obvious Gotchas

- **All web assets are fully committed** — `static/htmx.min.js` (HTMX 1.9.12), `static/fonts/*.woff2` (Newsreader, Geist, JetBrains Mono, Instrument Serif), and `static/tailwind.min.css`. No download step needed. Use `python scripts/vendor.py` to refresh them when upgrading.
- **Tailwind auto-rebuilds on startup** only when `input.css` is newer than the output (mtime check in `app.py`). Tailwind v4 scans every tracked text file — docs, docstrings, and tests included — so even a prose change can add or drop a class (e.g. the word "invisible" in a docstring). Rebuild and commit `tailwind.min.css` whenever it changes.
- **Startup cleanup** — `app._cleanup_orphaned_uploads()` runs on every startup and deletes `wisper_upload_*`, `wisper_enroll_*`, and `wisper_enrollsrc_*` temp files. It is only the crash-window safety net: `JobQueue.submit()` renames the transcribe upload to a friendly name immediately (so running jobs never match the glob), `submit_standalone_enroll()` renames enroll uploads to `wisper_enrollsrc_<job-id>` at submit time and the job deletes them in a `finally`, and completed transcription jobs either move the audio next to the transcript (durable, backs the enrollment wizard) or delete it. Never point anything long-lived at any of these temp paths.
- **Web-upload audio lives next to its transcript** — `<stem><suffix>` in the output dir, recorded as `transcripts.audio_rel_path`. Deleted together with the transcript. The authoritative `speaker_map` (raw label → display name) is the `transcript_speakers` table, updated on every wizard rename — never reconstruct that mapping from the rendered markdown when the table has it. Read diarization data through `transcript_store.read_sidecar()`; write it with `write_sidecar()` (a job's first write), `set_speaker_names()` (renames), or `set_speaker_embeddings()` (relabel backfill). `<stem>_diar.json` holds only the segments.
- **`tqdm.monitor_interval = 0`** is set globally at app startup (`app.py`) and per-job (`jobs.py`) to prevent `TMonitor` from spawning a daemon thread that hangs `Ctrl+C` on Python 3.14.
- **`tqdm.write`/`tqdm.__init__` are patched in three unrelated layers** — `debug_log.Logger` (permanent tee), `jobs._run_transcription_job` (per-job capture for the SSE log stream — also where job cancellation is checked, so cancellation only fires when tqdm writes), and `pipeline._patch_tqdm_for_queue` (per-subprocess, `parallel_stages=True`). They only coexist safely because of the one-job-at-a-time invariant below. See architecture.md's "tqdm patching is load-bearing in three layers" before touching any of the three.
- **One job at a time.** `_model`, `_pipeline`, `_embedding_model`, and `word_alignment._fa_model`/`_fa_processor` are module-level globals — not thread-safe. `JobQueue` runs one job at a time intentionally; this covers every job type, including `JOB_LIVE`, which holds the slot for a whole live-recording session.
- **Transcript output dir:** Web uploads go to the output root — `WISPER_OUTPUT_DIR`, else the `output_dir` setting, else `data_dir/output/` — never `input_path.parent`, and never a CWD `./output` (that check is gone). Always resolve it with `path_utils.get_output_dir()`.
- **Database access only through `db.py`.** `db.connect()` / `db.transaction()` set `foreign_keys=ON` (off by default in SQLite) and the other pragmas; a test fails on any other `sqlite3.connect`. Every load-modify-save uses `db.transaction()` (`BEGIN IMMEDIATE`), and never holds it across ML or LLM work. Stored paths go through `db.to_rel()`/`from_rel()`.
- **Rewrite a transcript or summary only with `transcript_store.save_transcript()`/`save_summary()`.** They write atomically and reindex it for search; `test_transcript_writes_reindex` lists every other `atomic_write_text()` target. Indexing is otherwise the backfill worker's job (it's disabled in tests; call `search_index.run_backfill()`).
- **Delete transcripts only with `transcript_store.delete_transcript()`, and write transcript/summary/sidecar/journal files only with `transcript_store.atomic_write_text()`.** A bare `unlink()` leaves the registry row and campaign links behind (the #64 bug class); a bare `write_text()` can leave a truncated `.md`. Guard tests in `test_transcript_store.py` fail on new direct calls.
- **Migrations are frozen once merged.** Never edit a shipped migration in `db.py`; add a new version. A branch that must reshape unreleased migrations sets `db.SCHEMA_FROZEN = False`, which makes unmerged builds refuse the default data dir (work with `WISPER_DATA_DIR=<copy>`); `test_schema_frozen_on_main` fails if that reaches `main`.
- **Capture code writes recordings with targeted writers** (`update_recording_status`, `bind_recording_speaker`, `append_segment`/`append_marker`/`append_rejoin`), never `save_recording()` on its long-lived `Recording`, which would revert name or notes edited mid-session. A test enforces it.
- **Speaker profile keys** are `name.lower().replace(" ", "_")` — used as both filesystem filename and URL slug.
