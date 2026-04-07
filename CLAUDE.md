# wisper-transcribe — Developer Instructions

## Environment

All commands (installs, tests, running the CLI) must be run inside the project's Python virtual environment:

```bash
# Create venv (one-time)
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Install package in editable mode
pip install -e .
```

Never run `pip install` or `pytest` against the system Python — always use `.venv`.

## Running Tests

```bash
# Without coverage
.venv/Scripts/pytest tests/ -v

# With coverage report (matches what CI runs)
.venv/Scripts/pytest tests/ -v --cov --cov-report=term-missing
```

Tests must not require a GPU, network access, or real ML models. Mock all ML model calls (faster-whisper, pyannote) using `unittest.mock`.

## Branch Protection & CI

The `main` branch is protected on GitHub. All changes must go through a pull request:

- **Never push directly to `main`.** Create a feature branch and open a PR.
- **CI must pass before merging.** The `CI` workflow (`.github/workflows/ci.yml`) runs the full test suite on every push and PR. A failing CI check blocks the merge.
- **Tests must pass locally before pushing.** Run `pytest tests/ -v` and confirm all tests pass — do not push a branch knowing tests are red.
- **Branch naming:** Use descriptive names like `feat/setup-scripts` or `fix/cuda-detection`.

The CI workflow runs on `ubuntu-latest` with CPU-only PyTorch (no GPU available on GitHub runners). Tests are all mocked so this is fine.

**CI matrix:** The `test` job runs against Python 3.10, 3.11, 3.12, 3.13, and 3.14 in parallel. 3.10–3.13 are blocking (a failure prevents merge). 3.14 is non-blocking (`continue-on-error: true`) — failures are visible but do not block the PR. A weekly cron job also runs the full matrix plus a `latest-deps` job (installs with `--upgrade`) to catch forward-compatibility issues early.

## Security — Public Repo Rules

This repo is public on GitHub. Before every commit:

- **Never commit secrets.** HuggingFace tokens, API keys, and passwords must never appear in source files, test fixtures, or commit messages.
- **HF token storage:** The token lives in `platformdirs.user_data_dir("wisper-transcribe")/config.toml`, which is outside the repo by design. Never move it into the repo.
- **No real audio files.** Do not commit actual podcast recordings or any audio that could contain people's voices/personal data. The `example-file/` directory is gitignored for local testing only.
- **No personal data in tests.** All test fixtures must use synthetic/fake data. Real names, voices, or session recordings must not appear in `tests/`.
- **Check before staging.** Run `git diff` and `git status` before `git add` to verify nothing sensitive is accidentally included.
- **Environment variables are fine.** `HUGGINGFACE_TOKEN` as an env var is an acceptable alternative to the config file — just never hardcode the value in source.

If a secret is accidentally committed, it must be treated as compromised immediately (revoke and regenerate the token).

## Architecture Documentation

Detailed technical reference lives in [`architecture.md`](architecture.md). It covers the processing pipeline, module responsibilities, key design decisions, speaker identification, data storage, and known constraints.

**Keep `architecture.md` current.** After any PR that:
- Adds or renames a module
- Changes the processing pipeline
- Introduces a new key design decision or workaround
- Updates dependencies in a way that affects runtime behavior

...update `architecture.md` as part of the same commit. Do not defer docs to a follow-up.

## Development Rules

- **Write tests alongside each feature.** Every new module gets a corresponding `tests/test_<module>.py`. Do not defer tests to a later phase.
- **Commit at least once per phase.** Pause for user review after each phase commit before starting the next.
- **Never commit to main without tests passing.**
- **Cross-platform paths:** Always use `pathlib.Path`, never string concatenation for file paths.
- **Config/data storage:** Use `get_data_dir()` from `config.py` — never hardcode `%APPDATA%`, `~`, or call `platformdirs` directly. `get_data_dir()` checks `WISPER_DATA_DIR` env var first (used in Docker) before falling back to `platformdirs`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `WISPER_DATA_DIR` | Override config/profile storage path (used in Docker; maps to a bind-mounted volume) |
| `WISPER_DEBUG` | Set to `1` to disable third-party warning suppression and see raw output |
| `HUGGINGFACE_TOKEN` | HF token as an alternative to storing it in `config.toml` |

## Project Structure

```
wisper-transcribe/
├── src/wisper_transcribe/   # all source code
│   ├── web/                 # Phase 11: FastAPI web UI
│   │   ├── app.py
│   │   ├── jobs.py
│   │   ├── routes/
│   │   └── templates/
│   └── static/              # Phase 11: vendored assets (htmx.min.js, tailwind.min.css, wisp.svg)
├── tests/                   # mirrors src structure
├── .venv/                   # local venv (gitignored)
├── Dockerfile               # gpu and cpu build targets
├── docker-compose.yml       # wisper (GPU), wisper-cpu, wisper-web, wisper-cpu-web services
├── .dockerignore
├── tailwind.config.js       # Phase 11: Tailwind CSS config (content paths)
├── pyproject.toml
└── CLAUDE.md                # this file
```

## Running the Web UI (Phase 11)

```bash
# Start the server (works immediately — Tailwind CSS is pre-built and committed)
wisper server                          # defaults: host=0.0.0.0, port=8080
wisper server --port 9000              # custom port
wisper server --reload                 # dev mode auto-reload

# Docker
docker compose up wisper-web           # GPU
docker compose up wisper-cpu-web       # CPU-only
# → Open http://localhost:8080
```

All web assets (HTMX, Tailwind CSS) are served locally — no CDN or internet required at runtime.

## Web UI Development (Phase 11)

The compiled `tailwind.min.css` is committed to the repo. `pip install -e .` → `wisper server` works with no extra steps.

If you **modify HTML templates** and add/remove Tailwind utility classes, regenerate the CSS:

```bash
pip install -e .[dev]       # installs pytailwindcss (wraps Tailwind standalone binary, no Node.js)
python -m pytailwindcss -i src/wisper_transcribe/static/input.css \
    -o src/wisper_transcribe/static/tailwind.min.css --minify
# commit tailwind.min.css along with your template changes
```

HTMX placeholder is at `src/wisper_transcribe/static/htmx.min.js`. The Dockerfile downloads the real file automatically during `docker build`. For local use, download it once:
```bash
curl -sL "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js" \
     -o src/wisper_transcribe/static/htmx.min.js
```

Web route tests: `pytest tests/test_web_*.py -v`

## Build Phases

- Phase 1: Project skeleton + basic transcription ✓
- Phase 2: Speaker diarization (pyannote) ✓
- Phase 3: Speaker profiles + cross-file voice matching ✓
- Phase 4: Batch processing + CLI polish ✓
- Phase 5: Tests + README ✓
- Phase 6: `wisper setup` wizard ✓
- Phase 7: Docker containerization ✓
- Phase 8: VAD filter (`--vad/--no-vad`) ✓
- Phase 9: Compute type / quantization (`--compute-type`) ✓
- Phase 10: Parallel folder processing (CPU-only, `--workers N`) ✓
- Phase 11: Browser-based web UI (`wisper server`) ✓
