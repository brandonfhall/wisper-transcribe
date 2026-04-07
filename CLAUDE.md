# wisper-transcribe — Claude Instructions

> **Full references:** [README.md](README.md) (user docs) · [architecture.md](architecture.md) (technical deep-dive)

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
- **Tests must pass locally before pushing.** CI blocks merges on failure.
- **Commit at least once per phase.** Pause for user review after each phase commit before starting the next.
- **Branch naming:** `feat/...` or `fix/...`
- **CI matrix:** Python 3.10–3.13 are blocking; 3.14 is `continue-on-error: true` (non-blocking).
- **Update `architecture.md`** in the same commit whenever you add a module, change the pipeline, or introduce a non-obvious design decision.

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

---

## Key Conventions

| Rule | Why |
|------|-----|
| Always `pathlib.Path`, never string paths | Cross-platform (Windows backslash) |
| Always `get_data_dir()` from `config.py` for user data | Respects `WISPER_DATA_DIR` env var (Docker) |
| URL-encode transcript stems in templates with `\| urlencode` filter | Filenames may contain em-dashes, spaces, `!`, `()` |
| Use path-traversal check (`..`, `/`, `\`, null bytes) not allowlist regex | Allowlist blocked valid unicode filenames |
| Redirect `Location` headers use `urllib.parse.quote(name)` | latin-1 codec rejects non-ASCII characters |

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
