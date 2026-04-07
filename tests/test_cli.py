"""CLI layer tests using Click's CliRunner.

These tests exercise the Click command wrappers — argument parsing, error
handling, output formatting — without running the full ML pipeline.
All external I/O (pipeline, diarizer, audio conversion) is mocked.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

from wisper_transcribe.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_profile(tmp_path: Path, name: str, display_name: str = "", role: str = "Player") -> str:
    """Write a fake speaker profile to tmp_path/profiles/ and return the key."""
    key = name.lower().replace(" ", "_")
    display = display_name or name
    profiles_dir = tmp_path / "profiles"
    emb_dir = profiles_dir / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)

    npy_path = emb_dir / f"{key}.npy"
    np.save(str(npy_path), np.zeros(512, dtype=np.float32))

    speakers_json = profiles_dir / "speakers.json"
    profiles: dict = {}
    if speakers_json.exists():
        with open(speakers_json, encoding="utf-8") as f:
            profiles = json.load(f)
    profiles[key] = {
        "display_name": display,
        "role": role,
        "embedding_file": f"embeddings/{key}.npy",
        "enrolled_date": "2026-04-06",
        "enrollment_source": "test",
        "notes": "",
    }
    with open(speakers_json, "w", encoding="utf-8") as f:
        json.dump(profiles, f)
    return key


# ---------------------------------------------------------------------------
# wisper config
# ---------------------------------------------------------------------------

def test_config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "path"])
    assert result.exit_code == 0
    assert str(tmp_path) in result.output


def test_config_set_string(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "model", "large-v3"])
    assert result.exit_code == 0
    assert "large-v3" in result.output

    # Verify persisted
    from wisper_transcribe.config import load_config
    cfg = load_config()
    assert cfg["model"] == "large-v3"


def test_config_set_bool_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "vad_filter", "false"])
    assert result.exit_code == 0

    from wisper_transcribe.config import load_config
    assert load_config()["vad_filter"] is False


def test_config_set_float_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "similarity_threshold", "0.80"])
    assert result.exit_code == 0

    from wisper_transcribe.config import load_config
    assert load_config()["similarity_threshold"] == pytest.approx(0.80)


def test_config_show(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    mock_torch = MagicMock()
    mock_torch.cuda.is_available.return_value = False
    mock_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": mock_torch}):
        result = CliRunner().invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "Paths" in result.output
    assert "Settings" in result.output
    assert "Models" in result.output


# ---------------------------------------------------------------------------
# wisper speakers
# ---------------------------------------------------------------------------

def test_speakers_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["speakers", "list"])
    assert result.exit_code == 0
    assert "No speakers enrolled" in result.output


def test_speakers_list_with_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice", role="DM")
    _make_fake_profile(tmp_path, "Bob", role="Player")
    result = CliRunner().invoke(main, ["speakers", "list"])
    assert result.exit_code == 0
    assert "Alice" in result.output
    assert "Bob" in result.output


def test_speakers_remove_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["speakers", "remove", "Ghost"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_speakers_remove_success(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    result = CliRunner().invoke(main, ["speakers", "remove", "Alice"])
    assert result.exit_code == 0
    assert "Removed" in result.output

    from wisper_transcribe.speaker_manager import load_profiles
    assert "alice" not in load_profiles(data_dir=tmp_path)


def test_speakers_rename_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["speakers", "rename", "Ghost", "Spectre"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_speakers_rename_success(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    result = CliRunner().invoke(main, ["speakers", "rename", "Alice", "Alicia"])
    assert result.exit_code == 0
    assert "Alicia" in result.output

    from wisper_transcribe.speaker_manager import load_profiles
    profiles = load_profiles(data_dir=tmp_path)
    assert "alicia" in profiles
    assert "alice" not in profiles
    assert profiles["alicia"].display_name == "Alicia"


def test_speakers_reset_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["speakers", "reset", "--yes"])
    assert result.exit_code == 0
    assert "nothing to reset" in result.output.lower()


def test_speakers_reset_yes_flag_skips_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    _make_fake_profile(tmp_path, "Bob")
    result = CliRunner().invoke(main, ["speakers", "reset", "--yes"])
    assert result.exit_code == 0
    assert "Removed 2" in result.output

    from wisper_transcribe.speaker_manager import load_profiles
    assert load_profiles(data_dir=tmp_path) == {}


def test_speakers_reset_prompt_abort(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    # Provide "n" to the confirmation prompt
    result = CliRunner().invoke(main, ["speakers", "reset"], input="n\n")
    assert result.exit_code != 0

    from wisper_transcribe.speaker_manager import load_profiles
    assert "alice" in load_profiles(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# wisper fix
# ---------------------------------------------------------------------------

def test_fix_replaces_speaker_name(tmp_path):
    transcript = tmp_path / "session.md"
    transcript.write_text("**Alice**: Hello\n**Bob**: World\n**Alice**: Goodbye\n", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["fix", str(transcript), "--speaker", "Alice", "--name", "Diana"]
    )
    assert result.exit_code == 0
    assert "Diana" in result.output

    updated = transcript.read_text(encoding="utf-8")
    assert "**Diana**" in updated
    assert "**Alice**" not in updated
    assert "**Bob**" in updated  # other speakers untouched


# ---------------------------------------------------------------------------
# wisper transcribe (CLI layer)
# ---------------------------------------------------------------------------

def test_transcribe_cli_raises_click_exception_on_error(tmp_path):
    """RuntimeError from process_file surfaces as a ClickException (non-zero exit)."""
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")

    with patch("wisper_transcribe.pipeline.process_file", side_effect=RuntimeError("GPU unavailable")):
        result = CliRunner().invoke(main, ["transcribe", str(audio)])

    assert result.exit_code != 0
    assert "GPU unavailable" in result.output


def test_transcribe_cli_language_auto_passes_none(tmp_path):
    """--language auto passes None to process_file (triggers auto-detection)."""
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")

    with patch("wisper_transcribe.pipeline.process_file", return_value=tmp_path / "test.md") as mock_pf:
        (tmp_path / "test.md").write_text("# test", encoding="utf-8")
        CliRunner().invoke(main, ["transcribe", str(audio), "--language", "auto"])

    call_kwargs = mock_pf.call_args.kwargs
    assert call_kwargs["language"] is None


def test_transcribe_cli_vocab_file_passes_hotwords(tmp_path):
    """--vocab-file reads lines and passes them as hotwords to process_file."""
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")
    vocab = tmp_path / "words.txt"
    vocab.write_text("Kyra\nGolarion\n# comment\n\nZeldris\n", encoding="utf-8")

    with patch("wisper_transcribe.pipeline.process_file", return_value=tmp_path / "test.md") as mock_pf:
        (tmp_path / "test.md").write_text("# test", encoding="utf-8")
        CliRunner().invoke(main, ["transcribe", str(audio), "--vocab-file", str(vocab)])

    call_kwargs = mock_pf.call_args.kwargs
    assert call_kwargs["hotwords"] == ["Kyra", "Golarion", "Zeldris"]


def test_transcribe_cli_initial_prompt_passes_through(tmp_path):
    """--initial-prompt passes the string to process_file."""
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")

    with patch("wisper_transcribe.pipeline.process_file", return_value=tmp_path / "test.md") as mock_pf:
        (tmp_path / "test.md").write_text("# test", encoding="utf-8")
        CliRunner().invoke(main, ["transcribe", str(audio), "--initial-prompt", "Kyra Golarion"])

    call_kwargs = mock_pf.call_args.kwargs
    assert call_kwargs["initial_prompt"] == "Kyra Golarion"


def test_config_set_hotwords_list(tmp_path, monkeypatch):
    """wisper config set hotwords accepts comma-separated input and stores as list."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra, Golarion, Zeldris"])
    assert result.exit_code == 0

    from wisper_transcribe.config import load_config
    assert load_config()["hotwords"] == ["Kyra", "Golarion", "Zeldris"]
