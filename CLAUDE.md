# wisper-transcribe — Claude Instructions

> **Full references:** [README.md](README.md) (user docs) · [architecture.md](architecture.md) (technical deep-dive)

---

## Documentation Rules (always apply)

Keeping docs in sync with code is **non-optional** — treat it as part of the definition of done for every task.

| Doc | Update when |
|-----|-------------|
| `architecture.md` | Any new module, pipeline change, design decision, config key, or test count change. Update: module map entry, relevant design-decision section, config key list, Known Constraints table, test count. |
| `README.md` | Any user-facing change: new CLI flag, changed flag behaviour, new command, changed env variable, Docker change, or anything that affects how a user runs or configures wisper. Do **not** put test counts in README — they belong in `architecture.md` only. |

Both files must be updated **in the same commit** as the code change, not as a follow-up.

---

## Definition of Done

A task is not complete until all three are true — in this order:

1. **Tests pass** — run `.venv/bin/pytest tests/ -v` and confirm green
2. **Docs updated** — `architecture.md` updated; `README.md` updated if user-facing (per Documentation Rules above)
3. **Committed** — all changed files in a single `git commit`

When a todo list reaches 100% completed, execute steps 1–3 immediately without waiting to be asked.

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

# Rebuild Tailwind CSS (required after any template class changes)
.venv/bin/python -m pytailwindcss -i src/wisper_transcribe/static/input.css \
    -o src/wisper_transcribe/static/tailwind.min.css --minify
# Commit tailwind.min.css alongside template changes
```

---

## Git / CI Rules

- **Never push to `main` directly.** All changes go through a PR.
- **After committing a phase, pause for user review before starting the next.**
- **Branch naming:** `feat/...` or `fix/...`
- **CI matrix:** Python 3.10–3.13 are blocking; 3.14 is `continue-on-error: true` (non-blocking).

---

## Testing Rules

- No GPU, no network, no real audio in tests — mock everything ML-related.
- Mock targets: `wisper_transcribe.transcriber.WhisperModel`, `wisper_transcribe.diarizer.Pipeline`, `wisper_transcribe.speaker_manager.load_profiles`.
- Web tests use `fastapi.testclient.TestClient` with all ML calls mocked.
- Every new module needs a `tests/test_<module>.py`.

---

## Security (public repo)

- **No secrets in source.** HF token lives in `platformdirs` user data dir or `HUGGINGFACE_TOKEN` env var — never in code.
- **No real audio files committed.** `example-file/` is gitignored.
- **No personal data in tests.** Synthetic/fake data only.
- If a secret is accidentally committed, treat it as compromised immediately.

### Web Route Security Standards

These rules apply to every web route handler. CodeQL scans all PRs — violations block merge.

#### User input in file paths (CWE-22 Path Traversal)
Use the two-layer pattern for any URL parameter or form field used in a file path:
1. `os.path.basename()` strips leading path components.
2. `os.path.abspath(os.path.join(base, safe_name)).startswith(base + os.sep)` confirms the result stays inside the intended directory.

`Path.resolve()` on tainted input is **not** sufficient — CodeQL does not recognise it as a sanitiser. Use `os.path.abspath` + `startswith`.

#### User input in redirect URLs (CWE-601 Open Redirect)
Use `_validate_job_id()` (defined in `transcribe.py`) for every job ID that appears in a `RedirectResponse` or `Location` header. For other ID types, apply the same two-layer pattern:
1. Strict regex guard `re.match(r"^[\w\-]+$", value)` — rejects everything except alphanumerics and hyphens.
2. `os.path` dummy-guard round-trip — `os.path.basename(os.path.abspath(os.path.join(base, value)))` — to produce a string that CodeQL's taint tracker recognises as clean.

`re.match().group(1)` is **still considered tainted** by CodeQL even after a format check. The `os.path` round-trip is required to break the taint chain.

#### Never reflect user input into error messages or redirect parameters
Exception messages, file paths, and internal state must not appear in redirect `Location` headers or in HTML error responses. Use a generic error code (e.g. `?error=enroll_failed`) instead of `?error={str(exc)}`.

#### Never accept arbitrary file paths from form data
Do not accept `output_dir`, `base_path`, or similar path parameters from form POST data. Always use the internally-resolved default (e.g. `_default_output_dir()`).

#### Test coverage requirement
Every security control must have a corresponding test in `tests/test_path_traversal.py` covering:
- Null-byte payloads (`\x00`)
- Regex-busting payloads (`invalid*name`, `id/with/slashes`)
- Open-redirect / CRLF payloads for any endpoint that redirects

---

## Key Conventions

| Rule | Why |
|------|-----|
| Always `pathlib.Path`, never string paths | Cross-platform (Windows backslash) |
| Always `get_data_dir()` from `config.py` for user data | Respects `WISPER_DATA_DIR` env var (Docker) |
| URL-encode transcript stems in templates with `\| urlencode` filter | Filenames may contain em-dashes, spaces, `!`, `()` |
| Use `os.path.basename` + `abspath/startswith` for path guards, not `Path.resolve()` | CodeQL only recognises `os.path` as a path sanitiser |
| Use `_validate_job_id()` for job IDs in redirects | Breaks CodeQL taint chain; regex alone is insufficient |
| Redirect `Location` headers use `urllib.parse.quote(name)` | latin-1 codec rejects non-ASCII characters |
| Never put `str(exc)` in a redirect URL or error response | Information disclosure; use generic error codes |

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `WISPER_DATA_DIR` | Override data dir (Docker bind mount) |
| `WISPER_DEBUG` | Set `1` to disable warning suppression |
| `HUGGINGFACE_TOKEN` | HF token alternative to `config.toml` |

---

## Non-Obvious Gotchas

- **`static/htmx.min.js` is a placeholder.** The real file is downloaded by `docker build`. For local dev: `curl -sL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" -o src/wisper_transcribe/static/htmx.min.js`
- **Tailwind auto-rebuilds on startup** (mtime check in `app.py`), but you still need to rebuild manually and commit `tailwind.min.css` when changing template classes.
- **`tqdm.monitor_interval = 0`** is set globally at app startup (`app.py`) and per-job (`jobs.py`) to prevent `TMonitor` from spawning a daemon thread that hangs `Ctrl+C` on Python 3.14.
- **One job at a time.** `_model` and `_pipeline` are module-level globals — not thread-safe. `JobQueue` runs one job at a time intentionally.
- **Transcript output dir:** Web uploads go to `./output/` (or `data_dir/output/`) — not `input_path.parent`. This is enforced in `transcribe.py`'s `_default_output_dir()`.
- **Speaker profile keys** are `name.lower().replace(" ", "_")` — used as both filesystem filename and URL slug.
