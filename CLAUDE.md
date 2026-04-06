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
.venv/Scripts/pytest tests/ -v
```

Tests must not require a GPU, network access, or real ML models. Mock all ML model calls (faster-whisper, pyannote) using `unittest.mock`.

## Security — Public Repo Rules

This repo is public on GitHub. Before every commit:

- **Never commit secrets.** HuggingFace tokens, API keys, and passwords must never appear in source files, test fixtures, or commit messages.
- **HF token storage:** The token lives in `platformdirs.user_data_dir("wisper-transcribe")/config.toml`, which is outside the repo by design. Never move it into the repo.
- **No real audio files.** Do not commit actual podcast recordings or any audio that could contain people's voices/personal data. The `example-file/` directory is gitignored for local testing only.
- **No personal data in tests.** All test fixtures must use synthetic/fake data. Real names, voices, or session recordings must not appear in `tests/`.
- **Check before staging.** Run `git diff` and `git status` before `git add` to verify nothing sensitive is accidentally included.
- **Environment variables are fine.** `HUGGINGFACE_TOKEN` as an env var is an acceptable alternative to the config file — just never hardcode the value in source.

If a secret is accidentally committed, it must be treated as compromised immediately (revoke and regenerate the token).

## Development Rules

- **Write tests alongside each feature.** Every new module gets a corresponding `tests/test_<module>.py`. Do not defer tests to a later phase.
- **Commit at least once per phase.** Pause for user review after each phase commit before starting the next.
- **Never commit to main without tests passing.**
- **Cross-platform paths:** Always use `pathlib.Path`, never string concatenation for file paths.
- **Config/data storage:** Use `platformdirs.user_data_dir("wisper-transcribe")` — never hardcode `%APPDATA%` or `~`.

## Project Structure

```
wisper-transcribe/
├── src/wisper_transcribe/   # all source code
├── tests/                   # mirrors src structure
├── .venv/                   # local venv (gitignored)
├── pyproject.toml
└── CLAUDE.md                # this file
```

## Build Phases

- Phase 1: Project skeleton + basic transcription ✓
- Phase 2: Speaker diarization (pyannote)
- Phase 3: Speaker profiles + cross-file voice matching
- Phase 4: Batch processing + CLI polish
- Phase 5: Tests + README
- Phase 6: GUI (future, not in MVP)
