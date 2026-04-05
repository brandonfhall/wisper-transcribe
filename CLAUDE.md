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
