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

from . import _seed
# Import these before any autouse patch replaces them in the module namespace.
from wisper_transcribe.cli import _get_ollama_models as _real_get_ollama_models
from wisper_transcribe.cli import _get_lmstudio_models as _real_get_lmstudio_models


# ---------------------------------------------------------------------------
# Safety: prevent any test from accidentally launching Ollama or LM Studio.
# Both _get_ollama_models (subprocess.run ["ollama", "list"]) and
# _get_lmstudio_models (httpx.get to localhost:1234) hit real processes.
# Tests that need specific model lists patch these further inside their scope.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_local_llm_queries():
    """Block all real Ollama/LM Studio queries for every test in this module."""
    with patch("wisper_transcribe.cli._get_ollama_models", return_value=[]), \
         patch("wisper_transcribe.cli._get_lmstudio_models", return_value=[]):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_profile(tmp_path: Path, name: str, display_name: str = "", role: str = "Player") -> str:
    """Insert a fake speaker profile into tmp_path's wisper.db and return the key."""
    from ._seed import seed_profile

    key = name.lower().replace(" ", "_")
    seed_profile(key, display_name or name, role=role, data_dir=tmp_path,
                 enrolled_date="2026-04-06", enrollment_source="test")
    return key


# ---------------------------------------------------------------------------
# UTF-8 stdio (crashed transcription jobs writing Unicode via tqdm.write()
# on a legacy-codepage Windows console -- see pipeline.py's box-drawing
# separator line)
# ---------------------------------------------------------------------------

def test_ensure_utf8_stdio_reconfigures_both_streams():
    from wisper_transcribe.cli import _ensure_utf8_stdio

    fake_out, fake_err = MagicMock(), MagicMock()
    with patch("sys.stdout", fake_out), patch("sys.stderr", fake_err):
        _ensure_utf8_stdio()

    fake_out.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
    fake_err.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


def test_ensure_utf8_stdio_swallows_missing_reconfigure():
    """A stream without .reconfigure() (e.g. a test harness's capture
    object) must not crash startup."""
    from wisper_transcribe.cli import _ensure_utf8_stdio

    class _NoReconfigure:
        pass

    with patch("sys.stdout", _NoReconfigure()), patch("sys.stderr", _NoReconfigure()):
        _ensure_utf8_stdio()  # must not raise


def test_ensure_utf8_stdio_handles_none_streams():
    from wisper_transcribe.cli import _ensure_utf8_stdio

    with patch("sys.stdout", None), patch("sys.stderr", None):
        _ensure_utf8_stdio()  # must not raise


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


def test_speakers_remove_deletes_reference_clip(tmp_path, monkeypatch):
    """Removing a profile also deletes its .mp3 reference clip, not
    just the profile row, so the Speakers-page play button doesn't
    dangle after a CLI removal."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    emb_dir = tmp_path / "profiles" / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
    clip_path = emb_dir / "alice.mp3"
    clip_path.write_bytes(b"fake mp3")

    result = CliRunner().invoke(main, ["speakers", "remove", "Alice"])
    assert result.exit_code == 0
    assert not clip_path.exists()


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


def test_speakers_rename_moves_reference_clip(tmp_path, monkeypatch):
    """Renaming a profile moves its .mp3 reference clip alongside the
    embedding, so playback keeps working under the new key."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    emb_dir = tmp_path / "profiles" / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
    old_clip = emb_dir / "alice.mp3"
    old_clip.write_bytes(b"fake mp3")

    result = CliRunner().invoke(main, ["speakers", "rename", "Alice", "Alicia"])
    assert result.exit_code == 0

    new_clip = emb_dir / "alicia.mp3"
    assert not old_clip.exists()
    assert new_clip.exists()
    assert new_clip.read_bytes() == b"fake mp3"


def test_speakers_rename_updates_campaign_membership(tmp_path, monkeypatch):
    """A CLI rename rekeys campaign rosters (including the Discord ID
    binding) so membership follows the profile instead of dangling."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")

    from wisper_transcribe.campaign_manager import (
        add_member, bind_discord_id, create_campaign, load_campaigns,
    )
    campaign = create_campaign("Test Campaign", data_dir=tmp_path)
    add_member(campaign.slug, "alice", role="player", data_dir=tmp_path)
    bind_discord_id(campaign.slug, "alice", "123456789012345678", data_dir=tmp_path)

    result = CliRunner().invoke(main, ["speakers", "rename", "Alice", "Alicia"])
    assert result.exit_code == 0

    members = load_campaigns(data_dir=tmp_path)[campaign.slug].members
    assert "alice" not in members
    assert "alicia" in members
    assert members["alicia"].role == "player"
    assert members["alicia"].discord_user_id == "123456789012345678"


def test_speakers_rename_rejects_unsafe_new_name(tmp_path, monkeypatch):
    """The shared rename_profile refuses a new name whose derived key
    fails the path-component guard (the key becomes a filename/URL slug)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")

    result = CliRunner().invoke(main, ["speakers", "rename", "Alice", "../escape"])
    assert result.exit_code != 0
    assert "Invalid speaker name" in result.output

    from wisper_transcribe.speaker_manager import load_profiles
    assert set(load_profiles(data_dir=tmp_path)) == {"alice"}


def test_speakers_rename_success_without_reference_clip(tmp_path, monkeypatch):
    """A profile enrolled before reference clips existed (no .mp3 on disk)
    must still rename cleanly -- the clip move is best-effort."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")

    result = CliRunner().invoke(main, ["speakers", "rename", "Alice", "Alicia"])
    assert result.exit_code == 0
    assert not (tmp_path / "profiles" / "embeddings" / "alicia.mp3").exists()


def test_speakers_rename_refuses_to_overwrite_existing_profile(tmp_path, monkeypatch):
    """Renaming onto an existing speaker key fails loudly and leaves both
    profiles and their embeddings untouched."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice", display_name="Alice")
    _make_fake_profile(tmp_path, "Bob", display_name="Bob")

    result = CliRunner().invoke(main, ["speakers", "rename", "Alice", "Bob"])
    assert result.exit_code != 0
    assert "already exists" in result.output.lower()

    from wisper_transcribe.speaker_manager import load_profiles
    profiles = load_profiles(data_dir=tmp_path)
    assert "alice" in profiles
    assert "bob" in profiles
    assert profiles["alice"].display_name == "Alice"
    assert profiles["bob"].display_name == "Bob"
    assert profiles["alice"].embedding is not None
    assert profiles["bob"].embedding is not None


def test_speakers_test_deletes_converted_wav(tmp_path, monkeypatch):
    """`wisper speakers test` deletes the WAV produced by
    convert_to_wav() once matching is done, mirroring the `enroll` command."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio")

    converted_wav = tmp_path / "converted.wav"
    converted_wav.write_bytes(b"RIFF" + b"\x00" * 36)

    with patch("wisper_transcribe.audio_utils.convert_to_wav", return_value=converted_wav), \
         patch("wisper_transcribe.config.get_hf_token", return_value="fake-token"), \
         patch("wisper_transcribe.diarizer.diarize", return_value=[]), \
         patch("wisper_transcribe.speaker_manager.match_speakers", return_value={}):
        result = CliRunner().invoke(main, ["speakers", "test", str(audio)])

    assert result.exit_code == 0
    assert not converted_wav.exists()
    assert audio.exists()


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


def test_transcribe_cli_language_auto_passes_through(tmp_path):
    """--language auto is forwarded as the literal string "auto" — process_file
    (not the CLI) is responsible for turning it into None for auto-detection,
    since None is the CLI's own "unset, use config" sentinel."""
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")

    with patch("wisper_transcribe.pipeline.process_file", return_value=tmp_path / "test.md") as mock_pf:
        (tmp_path / "test.md").write_text("# test", encoding="utf-8")
        CliRunner().invoke(main, ["transcribe", str(audio), "--language", "auto"])

    call_kwargs = mock_pf.call_args.kwargs
    assert call_kwargs["language"] == "auto"


def test_transcribe_cli_language_unset_passes_none(tmp_path):
    """When --language is not passed at all, process_file receives None
    (the config-fallback sentinel), not any hardcoded default."""
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")

    with patch("wisper_transcribe.pipeline.process_file", return_value=tmp_path / "test.md") as mock_pf:
        (tmp_path / "test.md").write_text("# test", encoding="utf-8")
        CliRunner().invoke(main, ["transcribe", str(audio)])

    call_kwargs = mock_pf.call_args.kwargs
    assert call_kwargs["language"] is None
    assert call_kwargs["model_size"] is None
    assert call_kwargs["include_timestamps"] is None


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


def test_config_set_unknown_key_rejected(tmp_path, monkeypatch):
    """Unknown config keys are rejected instead of being silently written."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "not_a_real_key", "value"])
    assert result.exit_code != 0
    assert "Unknown config key" in result.output
    assert "config show" in result.output

    from wisper_transcribe.config import load_config
    assert "not_a_real_key" not in load_config()


def test_config_set_int_coercion(tmp_path, monkeypatch):
    """min_speakers (an int-typed default) is coerced to int, not left
    as a string — bool must be checked before int since bool is an int
    subclass, so this also guards that ordering."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "min_speakers", "3"])
    assert result.exit_code == 0, result.output

    from wisper_transcribe.config import load_config
    value = load_config()["min_speakers"]
    assert value == 3
    assert isinstance(value, int) and not isinstance(value, bool)


def test_config_set_int_coercion_recovers_from_bad_stored_type(tmp_path, monkeypatch):
    """Coercion must key off DEFAULTS[key]'s schema type, not
    cfg[key]'s current runtime type. If config.toml holds min_speakers as a
    string (e.g. hand-edited), isinstance(cfg[key], int) would miss and silently re-store it as a string forever. Simulate that
    by pre-seeding a string value, then confirm `config set` self-heals it
    to int."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.config import load_config, save_config

    cfg = load_config()
    cfg["min_speakers"] = "5"  # simulate a pre-existing bad-typed value
    save_config(cfg)

    result = CliRunner().invoke(main, ["config", "set", "min_speakers", "3"])
    assert result.exit_code == 0, result.output

    value = load_config()["min_speakers"]
    assert value == 3
    assert isinstance(value, int) and not isinstance(value, bool)


def test_config_set_hotwords_list(tmp_path, monkeypatch):
    """wisper config set hotwords accepts comma-separated input and stores as list."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra, Golarion, Zeldris"])
    assert result.exit_code == 0

    from wisper_transcribe.config import load_config
    assert load_config()["hotwords"] == ["Kyra", "Golarion", "Zeldris"]


# ---------------------------------------------------------------------------
# wisper setup
# ---------------------------------------------------------------------------

def test_setup_detects_ffmpeg(monkeypatch):
    """Setup wizard detects ffmpeg and reports OK."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(Path(__file__).parent / "tmp_setup"))
    with patch("wisper_transcribe.config.check_ffmpeg"):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends.mps.is_available.return_value = False
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = CliRunner().invoke(main, ["setup"], input="\n")
    # Should at least get past the ffmpeg check
    assert "ffmpeg found" in result.output or "OK" in result.output


def test_setup_ffmpeg_missing_exits(monkeypatch):
    """Setup wizard exits when ffmpeg is missing."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(Path(__file__).parent / "tmp_setup"))
    with patch("wisper_transcribe.config.check_ffmpeg", side_effect=RuntimeError("ffmpeg not found")):
        result = CliRunner().invoke(main, ["setup"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# wisper server
# ---------------------------------------------------------------------------

def test_server_missing_uvicorn():
    """Server command shows error when uvicorn is not installed."""
    with patch.dict("sys.modules", {"uvicorn": None}):
        with patch("builtins.__import__", side_effect=ImportError("No module named 'uvicorn'")):
            # The click exception should mention uvicorn
            result = CliRunner().invoke(main, ["server"])
            # Either exits non-zero or mentions uvicorn in output
            assert result.exit_code != 0 or "uvicorn" in result.output.lower()


def test_server_defaults_to_loopback():
    """`wisper server` binds 127.0.0.1 by default — the UI has no auth,
    so all-interfaces exposure must be an explicit opt-in."""
    mock_uvicorn = MagicMock()
    with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
        result = CliRunner().invoke(main, ["server"])
    assert result.exit_code == 0
    _, kwargs = mock_uvicorn.run.call_args
    assert kwargs["host"] == "127.0.0.1"


def test_server_explicit_host_still_works():
    """--host 0.0.0.0 remains available for trusted networks/Docker."""
    mock_uvicorn = MagicMock()
    with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
        result = CliRunner().invoke(main, ["server", "--host", "0.0.0.0"])
    assert result.exit_code == 0
    _, kwargs = mock_uvicorn.run.call_args
    assert kwargs["host"] == "0.0.0.0"


# ---------------------------------------------------------------------------
# wisper enroll
# ---------------------------------------------------------------------------

def test_enroll_cli_creates_profile(tmp_path, monkeypatch):
    """wisper enroll <name> --audio <file> enrolls a speaker."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio")

    with patch("wisper_transcribe.audio_utils.convert_to_wav", return_value=audio), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=np.ones(512)), \
         patch("wisper_transcribe.speaker_manager._save_reference_clip"), \
         patch("wisper_transcribe.audio_utils.get_duration", return_value=30.0):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends.mps.is_available.return_value = False
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = CliRunner().invoke(
                main, ["enroll", "TestSpeaker", "--audio", str(audio)]
            )

    assert result.exit_code == 0
    assert "Enrolled" in result.output

    from wisper_transcribe.speaker_manager import load_profiles
    profiles = load_profiles(data_dir=tmp_path)
    assert "testspeaker" in profiles


def test_enroll_cli_with_update_flag(tmp_path, monkeypatch):
    """wisper enroll --update averages with existing embedding."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio")

    # Create an existing profile first
    _make_fake_profile(tmp_path, "alice")

    with patch("wisper_transcribe.audio_utils.convert_to_wav", return_value=audio), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=np.ones(512)), \
         patch("wisper_transcribe.audio_utils.get_duration", return_value=30.0):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends.mps.is_available.return_value = False
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = CliRunner().invoke(
                main, ["enroll", "alice", "--audio", str(audio), "--update"]
            )

    assert result.exit_code == 0
    assert "Updated" in result.output


def test_enroll_cli_deletes_converted_wav(tmp_path, monkeypatch):
    """When convert_to_wav() produces a separate temp WAV (input isn't
    already a correct WAV), `wisper enroll` deletes it afterwards instead of
    leaking it in the OS tempdir."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake audio")

    converted_wav = tmp_path / "converted.wav"
    converted_wav.write_bytes(b"RIFF" + b"\x00" * 36)

    with patch("wisper_transcribe.audio_utils.convert_to_wav", return_value=converted_wav), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=np.ones(512)), \
         patch("wisper_transcribe.speaker_manager._save_reference_clip"), \
         patch("wisper_transcribe.audio_utils.get_duration", return_value=30.0):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        mock_torch.backends.mps.is_available.return_value = False
        with patch.dict("sys.modules", {"torch": mock_torch}):
            result = CliRunner().invoke(
                main, ["enroll", "TestSpeaker", "--audio", str(audio)]
            )

    assert result.exit_code == 0
    assert not converted_wav.exists()
    assert audio.exists()  # original input untouched


# ---------------------------------------------------------------------------
# wisper transcribe folder
# ---------------------------------------------------------------------------

def test_transcribe_folder_reports_summary(tmp_path):
    """Transcribing a folder prints a summary with counts."""
    audio1 = tmp_path / "s01.mp3"
    audio1.write_bytes(b"fake")
    out_md = tmp_path / "s01.md"
    out_md.write_text("# test")

    with patch("wisper_transcribe.pipeline.process_file", return_value=out_md), \
         patch("wisper_transcribe.pipeline.process_folder", return_value=([out_md], [], [])):
        result = CliRunner().invoke(main, ["transcribe", str(tmp_path)])

    assert result.exit_code == 0
    assert "Done" in result.output


def test_transcribe_folder_includes_video_files(tmp_path):
    """Video files in a folder are picked up alongside audio files."""
    from wisper_transcribe.audio_utils import VIDEO_EXTENSIONS

    (tmp_path / "session.mp3").write_bytes(b"fake audio")
    (tmp_path / "session.mp4").write_bytes(b"fake video")
    (tmp_path / "session.mkv").write_bytes(b"fake video")
    (tmp_path / "notes.txt").write_bytes(b"not media")

    out_md = tmp_path / "session.md"
    out_md.write_text("# test")

    processed: list[str] = []

    def fake_process(path, **kw):
        processed.append(path.suffix.lower())
        return out_md

    with patch("wisper_transcribe.pipeline.process_file", side_effect=fake_process), \
         patch("wisper_transcribe.pipeline.process_folder") as mock_folder:
        mock_folder.side_effect = None
        # Drive folder logic directly to avoid process_folder mock swallowing it
        from wisper_transcribe.cli import _audio_extensions
        found = {f.suffix.lower() for f in tmp_path.iterdir()
                 if f.suffix.lower() in _audio_extensions()}

    assert ".mp3" in found
    assert ".mp4" in found
    assert ".mkv" in found
    assert ".txt" not in found


# ---------------------------------------------------------------------------
# wisper config llm (interactive wizard)
# ---------------------------------------------------------------------------

def test_config_llm_ollama_wizard(tmp_path, monkeypatch):
    """Walk the wizard choosing ollama; writes provider/endpoint/model."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    # New order: provider → endpoint → model
    user_input = "ollama\nhttp://localhost:11434\nllama3.1:8b\n"
    result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0

    from wisper_transcribe.config import load_config
    cfg = load_config()
    assert cfg["llm_provider"] == "ollama"
    assert cfg["llm_model"] == "llama3.1:8b"
    assert cfg["llm_endpoint"] == "http://localhost:11434"


def test_config_llm_anthropic_wizard_saves_key(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    # provider=anthropic, model=(default), key=sk-xxx
    user_input = "anthropic\nclaude-sonnet-4-6\nsk-secret\n"
    result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0

    from wisper_transcribe.config import load_config
    cfg = load_config()
    assert cfg["llm_provider"] == "anthropic"
    assert cfg["anthropic_api_key"] == "sk-secret"


def test_config_llm_ollama_pick_by_number(tmp_path, monkeypatch):
    """When ollama models are listed, user can pick by number."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    fake_models = [("gemma4:e4b", "9.6 GB"), ("mistral-nemo:latest", "7.1 GB")]
    # New order: provider → endpoint → model-number
    user_input = "ollama\nhttp://localhost:11434\n1\n"
    with patch("wisper_transcribe.cli._get_ollama_models", return_value=fake_models):
        result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0, result.output
    assert "gemma4:e4b" in result.output

    from wisper_transcribe.config import load_config
    assert load_config()["llm_model"] == "gemma4:e4b"


def test_config_llm_ollama_pick_by_name(tmp_path, monkeypatch):
    """When ollama models are listed, user can still type a name instead of a number."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    fake_models = [("gemma4:e4b", "9.6 GB"), ("mistral-nemo:latest", "7.1 GB")]
    # New order: provider → endpoint → model-name
    user_input = "ollama\nhttp://localhost:11434\nmistral-nemo:latest\n"
    with patch("wisper_transcribe.cli._get_ollama_models", return_value=fake_models):
        result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0, result.output

    from wisper_transcribe.config import load_config
    assert load_config()["llm_model"] == "mistral-nemo:latest"


def test_config_llm_ollama_no_models_falls_back_to_text(tmp_path, monkeypatch):
    """When _get_ollama_models returns [], falls back to plain text prompt."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    # New order: provider → endpoint → model
    user_input = "ollama\nhttp://localhost:11434\nllama3.1:8b\n"
    with patch("wisper_transcribe.cli._get_ollama_models", return_value=[]):
        result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0, result.output

    from wisper_transcribe.config import load_config
    assert load_config()["llm_model"] == "llama3.1:8b"


def test_config_llm_lmstudio_wizard(tmp_path, monkeypatch):
    """Walk the wizard choosing lmstudio; writes provider/endpoint/model."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    fake_models = [("phi-3", "")]
    user_input = "lmstudio\nhttp://localhost:1234\n1\n"
    with patch("wisper_transcribe.cli._get_lmstudio_models", return_value=fake_models):
        result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0, result.output

    from wisper_transcribe.config import load_config
    cfg = load_config()
    assert cfg["llm_provider"] == "lmstudio"
    assert cfg["llm_model"] == "phi-3"
    assert cfg["llm_endpoint"] == "http://localhost:1234"


def test_config_llm_lmstudio_no_models_falls_back_to_text(tmp_path, monkeypatch):
    """When _get_lmstudio_models returns [], falls back to plain text prompt."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    user_input = "lmstudio\nhttp://localhost:1234\nmy-model\n"
    with patch("wisper_transcribe.cli._get_lmstudio_models", return_value=[]):
        result = CliRunner().invoke(main, ["config", "llm"], input=user_input)
    assert result.exit_code == 0, result.output

    from wisper_transcribe.config import load_config
    assert load_config()["llm_model"] == "my-model"


def test_get_ollama_models_parses_list_output():
    """_get_ollama_models parses `ollama list` stdout into (name, size) pairs."""
    fake_stdout = (
        "NAME                    ID              SIZE      MODIFIED\n"
        "gemma4:e4b              c6eb396dbd59    9.6 GB    13 days ago\n"
        "mistral-nemo:latest     e7e06d107c6c    7.1 GB    6 days ago\n"
    )
    # subprocess is imported lazily inside the function, so patch at the module level
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_stdout)
        models = _real_get_ollama_models()
    assert models == [("gemma4:e4b", "9.6 GB"), ("mistral-nemo:latest", "7.1 GB")]


def test_get_ollama_models_returns_empty_on_failure():
    """_get_ollama_models returns [] when ollama is missing or exits non-zero."""
    with patch("subprocess.run", side_effect=FileNotFoundError("ollama not found")):
        assert _real_get_ollama_models() == []
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _real_get_ollama_models() == []


def test_get_lmstudio_models_parses_response():
    """_get_lmstudio_models parses the /v1/models JSON into (id, "") pairs."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "data": [
            {"id": "lmstudio-community/gemma-3-12b"},
            {"id": "mistral-7b-instruct"},
        ]
    }
    # httpx is imported lazily inside the function, so patch at the httpx module level
    with patch("httpx.get", return_value=fake_response):
        models = _real_get_lmstudio_models()
    assert models == [("lmstudio-community/gemma-3-12b", ""), ("mistral-7b-instruct", "")]


def test_record_delete_passes_purge_true(monkeypatch):
    """`wisper record delete` promises to delete files, and the API only
    purges with ?purge=true, so the CLI must pass it."""
    import uuid

    monkeypatch.setenv("WISPER_SERVER_URL", "http://127.0.0.1:8080")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"id": "x", "deleted": True, "purged": True}
    rec_id = str(uuid.uuid4())

    with patch("httpx.request", return_value=fake_response) as mock_request:
        result = CliRunner().invoke(main, ["record", "delete", rec_id, "--yes"])

    assert result.exit_code == 0
    mock_request.assert_called_once()
    called_url = mock_request.call_args.args[1]
    assert called_url.endswith(f"/api/recordings/{rec_id}/delete?purge=true")


def test_get_lmstudio_models_returns_empty_on_failure():
    """_get_lmstudio_models returns [] when LM Studio is unreachable."""
    with patch("httpx.get", side_effect=Exception("connection refused")):
        assert _real_get_lmstudio_models() == []
    with patch("httpx.get", side_effect=Exception("timeout")):
        assert _real_get_lmstudio_models() == []


def test_config_llm_rejects_bad_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "llm"], input="bogus\n")
    assert result.exit_code != 0
    assert "Unknown provider" in result.output


def test_config_show_masks_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    # Stash a fake key in config.
    CliRunner().invoke(main, ["config", "set", "anthropic_api_key", "sk-secret-xxx"])

    mock_torch = MagicMock()
    mock_torch.cuda.is_available.return_value = False
    mock_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": mock_torch}):
        result = CliRunner().invoke(main, ["config", "show"])
    assert result.exit_code == 0
    assert "sk-secret-xxx" not in result.output
    assert "***" in result.output


# ---------------------------------------------------------------------------
# wisper refine
# ---------------------------------------------------------------------------

def _write_transcript(tmp_path: Path, name: str = "ep.md") -> Path:
    md = (
        "---\n"
        "title: Session 01\n"
        "---\n"
        "**Alice** *(00:01)*: I met Kira in Golarian.\n"
        "**Unknown Speaker 1** *(00:05)*: Good to see you!\n"
    )
    path = tmp_path / name
    path.write_text(md, encoding="utf-8")
    return path


def test_refine_dry_run_does_not_modify_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)
    original = transcript.read_text(encoding="utf-8")

    fake_client = MagicMock()
    fake_client.provider = "mock"
    fake_client.model = "m1"
    fake_client.complete_json.return_value = {"changes": [
        {"original": "Kira", "corrected": "Kyra"},
        {"original": "Golarian", "corrected": "Golarion"},
    ]}
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        # Ensure the CLI sees hotwords from config.
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra, Golarion"])
        result = CliRunner().invoke(main, ["refine", str(transcript), "--no-color"])

    assert result.exit_code == 0, result.output
    assert "Vocabulary edits: 2" in result.output
    # File unchanged (dry-run is the default).
    assert transcript.read_text(encoding="utf-8") == original
    # No backup written.
    assert not (tmp_path / "ep.md.bak").exists()


def test_refine_apply_writes_backup_and_updates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)
    original = transcript.read_text(encoding="utf-8")

    fake_client = MagicMock()
    fake_client.provider = "mock"
    fake_client.model = "m1"
    fake_client.complete_json.return_value = {"changes": [
        {"original": "Kira", "corrected": "Kyra"},
    ]}
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra"])
        result = CliRunner().invoke(main, ["refine", str(transcript), "--apply", "--no-color"])

    assert result.exit_code == 0, result.output
    refined = transcript.read_text(encoding="utf-8")
    assert "Kyra" in refined and "Kira" not in refined
    # YAML frontmatter preserved byte-for-byte.
    assert refined.startswith("---\ntitle: Session 01\n---\n")
    backup = tmp_path / "ep.md.bak"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original


def test_refine_unknown_task_surfaces_suggestions(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice", role="DM")
    _make_fake_profile(tmp_path, "Bob", role="Player")
    transcript = _write_transcript(tmp_path)

    fake_client = MagicMock()
    fake_client.provider = "mock"
    fake_client.model = "m1"
    fake_client.complete_json.side_effect = [
        {"changes": []},
        {"suggestions": [{
            "line_number": 5, "current_label": "Unknown Speaker 1",
            "suggested_name": "Bob", "confidence": 0.9, "reason": "greeting",
        }]},
    ]
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra"])
        result = CliRunner().invoke(
            main, ["refine", str(transcript), "--tasks", "vocabulary,unknown", "--no-color"]
        )

    assert result.exit_code == 0, result.output
    assert "Unknown-speaker suggestions: 1" in result.output
    assert "Bob" in result.output


def test_refine_provider_lmstudio_accepted(tmp_path, monkeypatch):
    """--provider lmstudio is accepted (choices derive from config.LLM_PROVIDERS)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)

    fake_client = MagicMock()
    fake_client.provider = "lmstudio"
    fake_client.model = "m1"
    fake_client.complete_json.return_value = {"changes": []}
    with patch("wisper_transcribe.llm.get_client", return_value=fake_client):
        result = CliRunner().invoke(
            main, ["refine", str(transcript), "--provider", "lmstudio", "--no-color"]
        )

    assert result.exit_code == 0, result.output
    assert "lmstudio" in result.output


def test_refine_rejects_unknown_task():
    result = CliRunner().invoke(main, ["refine", "nope.md", "--tasks", "bogus"])
    # Missing file raises first — use --help to hit the task validator.
    result2 = CliRunner().invoke(
        main, ["refine", "--help"]
    )
    assert result2.exit_code == 0
    assert "vocabulary" in result2.output


# ---------------------------------------------------------------------------
# wisper summarize
# ---------------------------------------------------------------------------

_SUMMARY_PAYLOAD = {
    "summary": "The party entered the crypt.",
    "session_title": "Into the Crypt",
    "loot": [{"item": "Wand", "quantity": "1", "recipient": "Alice"}],
    "npcs": [{"name": "Aziel", "role": "dragon", "first_mentioned_at": "14:22"}],
    "followups": ["Who sent the letter?"],
}


def test_summarize_writes_sidecar_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    transcript = _write_transcript(tmp_path)

    fake_client = MagicMock()
    fake_client.provider = "anthropic"
    fake_client.model = "claude-sonnet-4-6"
    fake_client.complete_json.return_value = _SUMMARY_PAYLOAD
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        result = CliRunner().invoke(main, ["summarize", str(transcript)])

    assert result.exit_code == 0, result.output
    summary = tmp_path / "ep.summary.md"
    assert summary.exists()
    content = summary.read_text(encoding="utf-8")
    assert "# Into the Crypt" in content
    assert "## Summary" in content
    assert "Aziel" in content
    # Refine was NOT triggered.
    assert "refined: false" in content


def test_summarize_refuses_to_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)
    existing = tmp_path / "ep.summary.md"
    existing.write_text("already here", encoding="utf-8")

    result = CliRunner().invoke(main, ["summarize", str(transcript)])
    assert result.exit_code != 0
    assert "--overwrite" in result.output
    assert existing.read_text(encoding="utf-8") == "already here"


def test_summarize_with_refine_applies_and_summarizes(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)
    original = transcript.read_text(encoding="utf-8")

    fake_client = MagicMock()
    fake_client.provider = "ollama"
    fake_client.model = "llama3.1:8b"
    # Calls: (1) refine vocabulary, (2) summarize JSON
    fake_client.complete_json.side_effect = [
        {"changes": [{"original": "Kira", "corrected": "Kyra"}]},
        _SUMMARY_PAYLOAD,
    ]
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra"])
        result = CliRunner().invoke(
            main, ["summarize", str(transcript), "--refine"]
        )

    assert result.exit_code == 0, result.output
    # Refine ran: transcript was updated + backup created.
    refined = transcript.read_text(encoding="utf-8")
    assert "Kyra" in refined and "Kira" not in refined
    backup = tmp_path / "ep.md.bak"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    # Summary was written with refined: true.
    summary = tmp_path / "ep.summary.md"
    assert summary.exists()
    assert "refined: true" in summary.read_text(encoding="utf-8")


def test_summarize_refine_failure_falls_through(tmp_path, monkeypatch):
    """If the refine LLM call fails, summarize should still succeed."""
    from wisper_transcribe.llm.errors import LLMUnavailableError

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)

    fake_client = MagicMock()
    fake_client.provider = "ollama"
    fake_client.model = "llama3.1:8b"
    # First call (refine) fails; second call (summarize) succeeds.
    fake_client.complete_json.side_effect = [
        LLMUnavailableError("ollama unreachable"),
        _SUMMARY_PAYLOAD,
    ]
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra"])
        result = CliRunner().invoke(
            main, ["summarize", str(transcript), "--refine"]
        )

    assert result.exit_code == 0, result.output
    summary = tmp_path / "ep.summary.md"
    assert summary.exists()
    # Refine failed → refined flag is false, no backup written.
    assert "refined: false" in summary.read_text(encoding="utf-8")
    assert not (tmp_path / "ep.md.bak").exists()


def test_summarize_custom_output_path(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript = _write_transcript(tmp_path)
    out = tmp_path / "notes" / "custom.md"
    out.parent.mkdir()

    fake_client = MagicMock()
    fake_client.provider = "mock"
    fake_client.model = "m1"
    fake_client.complete_json.return_value = _SUMMARY_PAYLOAD
    with patch("wisper_transcribe.cli._get_llm_client", return_value=fake_client):
        result = CliRunner().invoke(
            main, ["summarize", str(transcript), "--output", str(out)]
        )

    assert result.exit_code == 0, result.output
    assert out.exists()
    # Default sidecar should NOT have been written.
    assert not (tmp_path / "ep.summary.md").exists()


# ---------------------------------------------------------------------------
# wisper campaigns
# ---------------------------------------------------------------------------

def test_campaigns_create_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(main, ["campaigns", "create", "D&D Mondays"])
    assert result.exit_code == 0, result.output
    assert "d-d-mondays" in result.output

    result = runner.invoke(main, ["campaigns", "list"])
    assert result.exit_code == 0, result.output
    assert "d-d-mondays" in result.output
    assert "D&D Mondays" in result.output


def test_campaigns_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["campaigns", "list"])
    assert result.exit_code == 0
    assert "No campaigns" in result.output


def test_campaigns_reorder_up(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    for stem in ("s1", "s2", "s3"):
        _seed.move_to_campaign(stem, "test-campaign", data_dir=tmp_path)

    result = runner.invoke(main, ["campaigns", "reorder", "test-campaign", "s3", "--up"])
    assert result.exit_code == 0, result.output
    assert "1. s1" in result.output
    assert "2. s3 <-" in result.output
    assert "3. s2" in result.output


def test_campaigns_reorder_set(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import get_transcripts_for_campaign
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    for stem in ("s1", "s2", "s3"):
        _seed.move_to_campaign(stem, "test-campaign", data_dir=tmp_path)

    result = runner.invoke(main, ["campaigns", "reorder", "test-campaign", "--set", "s3,s1,s2"])
    assert result.exit_code == 0, result.output
    assert get_transcripts_for_campaign("test-campaign", data_dir=tmp_path) == ["s3", "s1", "s2"]


def test_campaigns_reorder_set_rejects_non_permutation(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    _seed.move_to_campaign("s1", "test-campaign", data_dir=tmp_path)

    result = runner.invoke(main, ["campaigns", "reorder", "test-campaign", "--set", "s1,ghost"])
    assert result.exit_code != 0


def test_campaigns_reorder_requires_up_or_down(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    _seed.move_to_campaign("s1", "test-campaign", data_dir=tmp_path)

    result = runner.invoke(main, ["campaigns", "reorder", "test-campaign", "s1"])
    assert result.exit_code != 0
    assert "exactly one" in result.output

    result = runner.invoke(main, ["campaigns", "reorder", "test-campaign", "s1", "--up", "--down"])
    assert result.exit_code != 0


def test_campaigns_reorder_unknown_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["campaigns", "reorder", "ghost", "s1", "--up"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_campaigns_delete_requires_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])

    result = runner.invoke(main, ["campaigns", "delete", "test-campaign"], input="n\n")
    assert result.exit_code != 0


def test_campaigns_delete_with_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])

    result = runner.invoke(main, ["campaigns", "delete", "test-campaign", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output


def _campaign_with_transcript(out):
    from wisper_transcribe import transcript_store

    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    md = out / "s01.md"
    md.write_text("x", encoding="utf-8")
    transcript_store.register(md, origin="job")
    (out / "s01.summary.md").write_text("sum", encoding="utf-8")
    _seed.move_to_campaign("s01", "test-campaign")
    return runner


def test_campaigns_delete_keeps_transcripts_by_default(tmp_path, monkeypatch):
    from wisper_transcribe.path_utils import get_output_dir

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = get_output_dir()
    runner = _campaign_with_transcript(out)

    result = runner.invoke(main, ["campaigns", "delete", "test-campaign"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "move its transcripts to the output root" in result.output
    assert "Deleted campaign" in result.output
    assert (out / "s01.md").exists() and (out / "s01.summary.md").exists()


def test_campaigns_delete_transcripts_flag_deletes_them(tmp_path, monkeypatch):
    from wisper_transcribe.path_utils import get_output_dir

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = get_output_dir()
    runner = _campaign_with_transcript(out)

    result = runner.invoke(main, ["campaigns", "delete", "test-campaign", "--delete-transcripts"],
                           input="y\n")
    assert result.exit_code == 0, result.output
    assert "delete its transcripts, their files, and its journal" in result.output
    assert not list(out.iterdir())


def test_campaigns_delete_reports_a_kept_campaign_and_exits_nonzero(tmp_path, monkeypatch):
    from wisper_transcribe import transcript_store as ts
    from wisper_transcribe.path_utils import get_output_dir
    from unittest.mock import patch

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = get_output_dir()
    runner = _campaign_with_transcript(out)

    with patch("wisper_transcribe.transcript_store.delete_transcript", return_value="kept"):
        result = runner.invoke(main, ["campaigns", "delete", "test-campaign",
                                      "--delete-transcripts"], input="y\n")

    assert result.exit_code != 0
    assert "was kept" in result.output and "s01" in result.output
    from wisper_transcribe.campaign_manager import load_campaigns
    assert "test-campaign" in load_campaigns(tmp_path)


def test_campaigns_delete_busy_exits_nonzero(tmp_path, monkeypatch):
    from wisper_transcribe import db
    from wisper_transcribe.campaign_manager import load_campaigns

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    with db.connection(tmp_path) as conn:
        cid = conn.execute("SELECT id FROM campaigns WHERE slug = 'test-campaign'").fetchone()[0]
    with db.transaction(tmp_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, status, created_at, started_at, campaign_id, params_json) "
            "VALUES ('00000000-0000-0000-0000-000000000002', 'campaign_journal', 'running', "
            "'now', 'now', ?, '{}')", (cid,))

    result = runner.invoke(main, ["campaigns", "delete", "test-campaign", "--yes"])
    assert result.exit_code != 0
    assert "try again when it finishes" in result.output
    assert "test-campaign" in load_campaigns(tmp_path)


def test_campaigns_rename_prints_the_new_slug_and_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Hanataz"])

    result = runner.invoke(main, ["campaigns", "rename", "hanataz", "Hanataz: Act I?"])
    assert result.exit_code == 0, result.output
    assert "Hanataz Act I" in result.output
    assert "hanataz-act-i" in result.output


def test_campaigns_rename_missing_slug_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["campaigns", "rename", "ghost", "X"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_campaigns_show_prints_the_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Hanataz"])

    result = runner.invoke(main, ["campaigns", "show", "hanataz"])
    assert result.exit_code == 0, result.output
    assert "Folder:" in result.output and "Hanataz" in result.output


def test_campaigns_show_reports_a_pending_rename(tmp_path, monkeypatch):
    from wisper_transcribe import db

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Hanataz"])
    with db.transaction(tmp_path) as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Next' WHERE slug = 'hanataz'")

    result = runner.invoke(main, ["campaigns", "show", "hanataz"])
    assert "Rename pending → Next" in result.output


def test_transcripts_list_points_at_the_attention_page(tmp_path, monkeypatch):
    from wisper_transcribe.path_utils import get_output_dir

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = get_output_dir()
    (out / "s01.md").write_text("x", encoding="utf-8")
    result = CliRunner().invoke(main, ["transcripts", "list"])
    assert "need attention" not in result.output and "needs attention" not in result.output

    (out / "ghost.summary.md").write_text("orphan", encoding="utf-8")
    (out / "ghost_diar.json").write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(main, ["transcripts", "list"])
    assert "2 items need attention; see the Transcripts page" in result.output
    assert (out / "ghost.summary.md").exists()


def test_campaigns_add_unknown_profile_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    runner.invoke(main, ["campaigns", "create", "Test Campaign"])

    result = runner.invoke(main, ["campaigns", "add-member", "test-campaign", "nobody"])
    assert result.exit_code != 0
    assert "not enrolled" in result.output


def test_campaigns_add_then_show(tmp_path, monkeypatch):
    """add-member then show displays the member with correct role."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    runner = CliRunner()

    # Create a fake enrolled speaker
    from tests._seed import save_profiles
    from wisper_transcribe.models import SpeakerProfile
    save_profiles(
        {"alice": SpeakerProfile(
            name="alice", display_name="Alice", role="",
            embedding=None, enrolled_date="2026-04-28",
            enrollment_source="test.mp3",
        )},
        data_dir=tmp_path,
    )

    runner.invoke(main, ["campaigns", "create", "Test Campaign"])
    result = runner.invoke(main, ["campaigns", "add-member", "test-campaign", "alice", "--role", "DM"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["campaigns", "show", "test-campaign"])
    assert result.exit_code == 0, result.output
    assert "Alice" in result.output
    assert "DM" in result.output


def test_campaigns_invalid_slug_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["campaigns", "show", "../etc/passwd"])
    assert result.exit_code != 0
    assert "Invalid" in result.output


def test_transcribe_passes_campaign_to_process_file(tmp_path, monkeypatch):
    """--campaign is forwarded to process_file as the campaign kwarg."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    from wisper_transcribe.campaign_manager import create_campaign
    create_campaign("D&D Mondays", data_dir=tmp_path)

    captured = {}

    def fake_process_file(path, **kwargs):
        captured.update(kwargs)
        return tmp_path / "session.md"

    with patch("wisper_transcribe.pipeline.process_file", side_effect=fake_process_file):
        result = CliRunner().invoke(
            main, ["transcribe", str(audio), "--campaign", "d-d-mondays"]
        )

    assert captured.get("campaign") == "d-d-mondays", result.output


def test_cli_campaign_writes_into_the_folder(tmp_path, monkeypatch):
    """--campaign without -o writes into the campaign's folder."""
    from unittest.mock import patch
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.campaign_manager import create_campaign

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    campaign = create_campaign("D&D Mondays", data_dir=tmp_path)
    folder = campaign_folders.ensure_folder(campaign.id, data_dir=tmp_path)

    captured = {}
    with patch("wisper_transcribe.pipeline.process_file",
               side_effect=lambda path, **kw: (captured.update(kw), tmp_path / "session.md")[1]):
        result = CliRunner().invoke(main, ["transcribe", str(audio), "--campaign", "d-d-mondays"])
    assert result.exit_code == 0, result.output
    assert Path(captured["output_dir"]) == folder


def test_cli_campaign_with_output_root_writes_into_the_folder(tmp_path, monkeypatch):
    """-o naming the output root writes into the campaign's folder."""
    from unittest.mock import patch
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.config import get_output_root

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    campaign = create_campaign("D&D Mondays", data_dir=tmp_path)
    folder = campaign_folders.ensure_folder(campaign.id, data_dir=tmp_path)

    captured = {}
    with patch("wisper_transcribe.pipeline.process_file",
               side_effect=lambda path, **kw: (captured.update(kw), tmp_path / "s.md")[1]):
        result = CliRunner().invoke(
            main, ["transcribe", str(audio), "--campaign", "d-d-mondays",
                   "-o", str(get_output_root())])
    assert result.exit_code == 0, result.output
    assert Path(captured["output_dir"]) == folder


def test_cli_campaign_with_output_elsewhere_is_roster_only(tmp_path, monkeypatch):
    """-o elsewhere leaves the output beside the input (roster-only)."""
    from unittest.mock import patch
    from wisper_transcribe.campaign_manager import create_campaign

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    create_campaign("D&D Mondays", data_dir=tmp_path)
    elsewhere = tmp_path / "elsewhere"

    captured = {}
    with patch("wisper_transcribe.pipeline.process_file",
               side_effect=lambda path, **kw: (captured.update(kw), tmp_path / "s.md")[1]):
        result = CliRunner().invoke(
            main, ["transcribe", str(audio), "--campaign", "d-d-mondays", "-o", str(elsewhere)])
    assert result.exit_code == 0, result.output
    assert Path(captured["output_dir"]) == elsewhere


def test_cli_campaign_name_clash_is_refused(tmp_path, monkeypatch):
    from unittest.mock import patch
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.campaign_manager import create_campaign

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    campaign = create_campaign("Game", data_dir=tmp_path)
    folder = campaign_folders.ensure_folder(campaign.id, data_dir=tmp_path)
    (folder / "session.md").write_text("old")

    with patch("wisper_transcribe.pipeline.process_file") as mock_pf:
        result = CliRunner().invoke(main, ["transcribe", str(audio), "--campaign", "game"])
    assert result.exit_code != 0
    assert "already in the campaign" in result.output
    mock_pf.assert_not_called()
    assert (folder / "session.md").read_text() == "old"


def test_cli_campaign_keep_both_uses_the_timestamp(tmp_path, monkeypatch):
    from unittest.mock import patch
    from datetime import datetime
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.campaign_manager import create_campaign

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    campaign = create_campaign("Game", data_dir=tmp_path)
    folder = campaign_folders.ensure_folder(campaign.id, data_dir=tmp_path)
    (folder / "session.md").write_text("old")

    captured = {}
    with patch("wisper_transcribe.pipeline.process_file",
               side_effect=lambda path, **kw: (captured.update(kw), tmp_path / "s.md")[1]), \
         patch("wisper_transcribe.cli._keep_both_suffix", return_value="2026-10-05 0142"):
        result = CliRunner().invoke(
            main, ["transcribe", str(audio), "--campaign", "game", "--keep-both"])
    assert result.exit_code == 0, result.output
    assert captured["output_stem"] == "session (2026-10-05 0142)"

    # A second clash in the same minute gets "(2)" appended.
    (folder / "session (2026-10-05 0142).md").write_text("prior")
    with patch("wisper_transcribe.pipeline.process_file",
               side_effect=lambda path, **kw: (captured.update(kw), tmp_path / "s.md")[1]), \
         patch("wisper_transcribe.cli._keep_both_suffix", return_value="2026-10-05 0142"):
        result = CliRunner().invoke(
            main, ["transcribe", str(audio), "--campaign", "game", "--keep-both"])
    assert result.exit_code == 0, result.output
    assert captured["output_stem"] == "session (2026-10-05 0142) (2)"


def test_cli_campaign_overwrite_writes_over_a_misplaced_session_where_it_is(tmp_path, monkeypatch):
    """--overwrite keeps the row: a session still in the root is overwritten there."""
    from unittest.mock import patch
    from wisper_transcribe import campaign_folders, transcript_store
    from wisper_transcribe.campaign_manager import create_campaign

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    root = tmp_path / "output"
    root.mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    campaign = create_campaign("Game", data_dir=tmp_path)
    campaign_folders.ensure_folder(campaign.id, data_dir=tmp_path)
    (root / "session.md").write_text("old")
    tid = transcript_store.register(root / "session.md", origin="reconcile")
    _seed.move_to_campaign("session", "game", data_dir=tmp_path)  # misplaced: stays in the root

    captured = {}
    with patch("wisper_transcribe.pipeline.process_file",
               side_effect=lambda path, **kw: (captured.update(kw), tmp_path / "s.md")[1]):
        result = CliRunner().invoke(
            main, ["transcribe", str(audio), "--campaign", "game", "--overwrite"])
    assert result.exit_code == 0, result.output
    assert Path(captured["output_dir"]) == root
    assert captured["output_stem"] == "session"


def test_cli_campaign_unknown_slug_is_refused(tmp_path, monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "output").mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")

    with patch("wisper_transcribe.pipeline.process_file") as mock_pf:
        result = CliRunner().invoke(main, ["transcribe", str(audio), "--campaign", "nope"])
    assert result.exit_code != 0
    assert "No campaign" in result.output
    mock_pf.assert_not_called()


# ---------------------------------------------------------------------------
# wisper transcripts
# ---------------------------------------------------------------------------


def test_transcripts_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    # No output directory — should not crash
    result = CliRunner().invoke(main, ["transcripts", "list"])
    assert result.exit_code == 0


def test_transcripts_list_shows_ungrouped_stems(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = tmp_path / "output"
    out.mkdir()
    (out / "session01.md").write_text("hello")
    result = CliRunner().invoke(main, ["transcripts", "list"])
    assert result.exit_code == 0
    assert "session01" in result.output


def test_transcripts_list_grouped_by_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = tmp_path / "output"
    out.mkdir()
    (out / "session01.md").write_text("hello")
    from wisper_transcribe.campaign_manager import create_campaign
    create_campaign("D&D Mondays", data_dir=tmp_path)
    _seed.move_to_campaign("session01", "d-d-mondays", data_dir=tmp_path)
    result = CliRunner().invoke(main, ["transcripts", "list"])
    assert result.exit_code == 0
    assert "D&D Mondays" in result.output
    assert "session01" in result.output


def test_transcripts_list_campaign_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    out = tmp_path / "output"
    out.mkdir()
    (out / "session01.md").write_text("hello")
    (out / "session02.md").write_text("hello")
    from wisper_transcribe.campaign_manager import create_campaign
    create_campaign("Alpha", data_dir=tmp_path)
    _seed.move_to_campaign("session01", "alpha", data_dir=tmp_path)
    result = CliRunner().invoke(main, ["transcripts", "list", "--campaign", "alpha"])
    assert result.exit_code == 0
    assert "session01" in result.output
    assert "session02" not in result.output


def test_transcripts_move_assigns_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign, get_campaign_for_transcript
    create_campaign("Alpha", data_dir=tmp_path)
    _seed.seed_transcript("session01", data_dir=tmp_path)
    result = CliRunner().invoke(main, ["transcripts", "move", "session01", "--campaign", "alpha"])
    assert result.exit_code == 0
    assert get_campaign_for_transcript(_seed.transcript_id("session01", data_dir=tmp_path),
                                       data_dir=tmp_path) == "alpha"


def test_transcripts_move_unlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import (
        create_campaign, get_campaign_for_transcript
    )
    create_campaign("Alpha", data_dir=tmp_path)
    _seed.move_to_campaign("session01", "alpha", data_dir=tmp_path)
    result = CliRunner().invoke(main, ["transcripts", "move", "session01", "--no-campaign"])
    assert result.exit_code == 0
    assert get_campaign_for_transcript(_seed.transcript_id("session01", data_dir=tmp_path),
                                       data_dir=tmp_path) is None


def test_transcripts_move_invalid_slug_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["transcripts", "move", "session01", "--campaign", "../evil"])
    assert result.exit_code != 0


def test_transcripts_move_ambiguous_name_needs_from(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe import db
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    with db.transaction(tmp_path) as conn:
        for slug in ("alpha", "beta"):
            conn.execute(
                "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                "SELECT 's1', id, 0, 'now' FROM campaigns WHERE slug = ?", (slug,))

    result = CliRunner().invoke(main, ["transcripts", "move", "s1", "--no-campaign"])
    assert result.exit_code != 0
    assert "several campaigns" in result.output and "--from" in result.output


def test_transcripts_move_clash_without_a_flag_lists_the_time(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe import transcript_store
    from wisper_transcribe.path_utils import get_output_dir

    from ._moves import claimed_campaign, placed_session

    out = get_output_dir()
    cid, slug, folder = claimed_campaign("Alpha")
    placed_session("s1", campaign=slug, directory=out / folder)  # the claimed target
    source, _md = placed_session("s1")                            # a fresh root session

    result = CliRunner().invoke(main, ["transcripts", "move", "s1", "--campaign", slug])
    assert result.exit_code == 1
    assert "already there" in result.output and "--keep-both" in result.output


def test_transcripts_move_keep_both(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.path_utils import get_output_dir

    from ._moves import claimed_campaign, placed_session

    out = get_output_dir()
    cid, slug, folder = claimed_campaign("Alpha")
    placed_session("s1", campaign=slug, directory=out / folder)
    placed_session("s1")

    result = CliRunner().invoke(
        main, ["transcripts", "move", "s1", "--campaign", slug, "--keep-both"])
    assert result.exit_code == 0, result.output
    assert (out / folder / "s1 (2).md").exists()


def test_transcripts_rename_carries_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe import transcript_store
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    (out / "old.md").write_text("x", encoding="utf-8")
    transcript_store.register(out / "old.md", origin="job")

    result = CliRunner().invoke(main, ["transcripts", "rename", "old", "new"])
    assert result.exit_code == 0, result.output
    assert (out / "new.md").exists() and not (out / "old.md").exists()


def test_transcripts_rename_busy_names_the_job(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    import tempfile
    from wisper_transcribe import transcript_store
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.web.jobs import JobQueue

    out = get_output_dir()
    (out / "old.md").write_text("x", encoding="utf-8")
    tid = transcript_store.register(out / "old.md", origin="job")
    JobQueue().submit(str(tmp_path / "wisper_upload_x.mp3"), original_stem="old",
                      output_dir=str(out))
    # The uploaded temp lives under WISPER_OUTPUT_DIR in this CLI test; just assert the guard.
    result = CliRunner().invoke(main, ["transcripts", "rename", "old", "new"])
    assert result.exit_code == 1
    assert "job is running" in result.output.lower()


def test_campaigns_relabel_reports_changes(tmp_path, monkeypatch):
    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.speaker_registry import RelabelReport, TranscriptRelabel

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)
    report = RelabelReport(
        transcripts=[TranscriptRelabel("s1", renamed={"SPEAKER_00": ("Unknown Speaker 1", "Alice")}),
                     TranscriptRelabel("s2", skipped="no speaker data")],
        recurring=2,
    )
    with patch("wisper_transcribe.speaker_registry.relabel_campaign", return_value=report) as mock_relabel:
        result = CliRunner().invoke(main, ["campaigns", "relabel", "my-game", "--dry-run", "--device", "cpu"])

    assert result.exit_code == 0, result.output
    assert mock_relabel.call_args.kwargs["dry_run"] is True
    assert mock_relabel.call_args.kwargs["backfill"] is True
    assert "Would rename 1 speaker label(s) across 2 session(s)." in result.output
    assert "Unknown voices heard in more than one session: 2" in result.output
    assert "Skipped s2: no speaker data" in result.output


def test_campaigns_relabel_unknown_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["campaigns", "relabel", "ghost"])
    assert result.exit_code != 0
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# Forced word alignment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag,expected", [
    ([], None), (["--forced-align"], "true"), (["--no-forced-align"], "false"),
])
def test_transcribe_cli_forced_align_flag(tmp_path, flag, expected):
    audio = tmp_path / "test.mp3"
    audio.write_bytes(b"fake")
    with patch("wisper_transcribe.pipeline.process_file", return_value=tmp_path / "test.md") as mock_pf:
        CliRunner().invoke(main, ["transcribe", str(audio), *flag])
    assert mock_pf.call_args.kwargs["forced_alignment"] == expected


def test_config_set_forced_alignment(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "forced_alignment", "false"])
    assert result.exit_code == 0
    from wisper_transcribe.config import load_config
    assert load_config()["forced_alignment"] == "false"


def test_config_set_rejects_value_outside_choices(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "set", "forced_alignment", "maybe"])
    assert result.exit_code != 0
    assert "choose from: auto, true, false" in result.output
    result = CliRunner().invoke(main, ["config", "set", "device", "tpu"])
    assert result.exit_code != 0


def _run_setup_with_device(tmp_path, monkeypatch, device, forced_alignment="auto"):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_fake")
    from wisper_transcribe.config import load_config, save_config
    cfg = load_config()
    cfg["forced_alignment"] = forced_alignment
    save_config(cfg)
    with patch("wisper_transcribe.config.check_ffmpeg"), \
         patch("wisper_transcribe.config.get_device", return_value=device), \
         patch("pyannote.audio.Pipeline.from_pretrained"), \
         patch("huggingface_hub.snapshot_download") as mock_dl:
        result = CliRunner().invoke(main, ["setup"], input="\n\n\n\n")
    return result, mock_dl


def test_setup_predownloads_aligner_on_gpu(tmp_path, monkeypatch):
    from wisper_transcribe.config import FORCED_ALIGNMENT_MODEL
    result, mock_dl = _run_setup_with_device(tmp_path, monkeypatch, "cuda")
    mock_dl.assert_called_once_with(FORCED_ALIGNMENT_MODEL)
    assert "alignment model cached" in result.output


def test_setup_skips_aligner_when_disabled(tmp_path, monkeypatch):
    _, mock_dl = _run_setup_with_device(tmp_path, monkeypatch, "cpu")
    mock_dl.assert_not_called()
    _, mock_dl = _run_setup_with_device(tmp_path, monkeypatch, "cuda", forced_alignment="false")
    mock_dl.assert_not_called()


def test_setup_aligner_download_failure_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_fake")
    with patch("wisper_transcribe.config.check_ffmpeg"), \
         patch("wisper_transcribe.config.get_device", return_value="cuda"), \
         patch("pyannote.audio.Pipeline.from_pretrained"), \
         patch("huggingface_hub.snapshot_download", side_effect=OSError("offline")):
        result = CliRunner().invoke(main, ["setup"], input="\n\n\n\n")
    assert "alignment model download failed" in result.output


@pytest.mark.parametrize("mlx,expected", [(True, "transcription uses MLX"), (False, "install the [macos] extra")])
def test_setup_mps_note_reflects_mlx(tmp_path, monkeypatch, mlx, expected):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_fake")
    with patch("wisper_transcribe.config.check_ffmpeg"), \
         patch("wisper_transcribe.config.get_device", return_value="mps"), \
         patch("wisper_transcribe.transcriber._is_mlx_available", return_value=mlx), \
         patch("pyannote.audio.Pipeline.from_pretrained"), \
         patch("huggingface_hub.snapshot_download"):
        result = CliRunner().invoke(main, ["setup"], input="\n\n\n\n")
    assert expected in result.output


# ---------------------------------------------------------------------------
# wisper speakers doctor
# ---------------------------------------------------------------------------

def test_speakers_doctor_reports_duplicates_stale_and_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from ._seed import seed_profile

    base = np.zeros(256, dtype=np.float32)
    base[0] = 1.0
    near = base.copy()
    near[1] = 0.1  # cosine ≈ 0.995 with base
    far = np.zeros(256, dtype=np.float32)
    far[2] = 1.0
    seed_profile("alice", "Alice", embedding=base)
    seed_profile("alice_2", "Alice 2", embedding=near)
    seed_profile("bob", "Bob", embedding=far)
    seed_profile("old", "Old", embedding=np.ones(512), embedding_space="")
    seed_profile("speaker_03", "SPEAKER_03", embedding=-base)

    result = CliRunner().invoke(main, ["speakers", "doctor"])
    assert result.exit_code == 0, result.output
    assert "Alice (alice) ≈ Alice 2 (alice_2)" in result.output
    assert "Bob (bob) ≈" not in result.output
    assert "Old (old)" in result.output
    assert "SPEAKER_03 (speaker_03)" in result.output

    from wisper_transcribe.speaker_manager import load_profiles
    assert len(load_profiles()) == 5  # report only


def test_speakers_doctor_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _make_fake_profile(tmp_path, "Alice")
    result = CliRunner().invoke(main, ["speakers", "doctor"])
    assert result.exit_code == 0
    assert "No problems found in 1 profile(s)." in result.output


def test_transcripts_list_marks_missing_entries_in_campaign_order(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    create_campaign("Game")
    for stem in ("s02", "s01"):
        (out / f"{stem}.md").write_text("x", encoding="utf-8")
        _seed.move_to_campaign(stem, "game")
    (out / "s02.md").unlink()

    result = CliRunner().invoke(main, ["transcripts", "list", "--campaign", "game"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:2] == ["s02  (missing — file not found)", "s01"]
    # s02 is missing, s01 is in the root though it belongs to Game.
    assert "2 items need attention; see the Transcripts page or `wisper storage trim`" in lines[2:]


# ---------------------------------------------------------------------------
# wisper storage trim / server lock
# ---------------------------------------------------------------------------

def _trim_world(tmp_path, monkeypatch):
    from tests._seed import seed_sidecar
    out = tmp_path / "trim_out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    md = out / "Ep.md"
    md.write_text("# t\n", encoding="utf-8")
    (out / "Ep.mp4").write_bytes(b"v" * 2048)
    seed_sidecar(md, {
        "diarization_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
        "speaker_map": {"SPEAKER_00": "A"},
        "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0]},
        "embedding_space": __import__("wisper_transcribe.config", fromlist=["x"]).EMBEDDING_SPACE,
        "input_path": str(out / "Ep.mp4"),
    })
    return out


def _fake_flac(src, dst):
    Path(dst).write_bytes(b"flac")


def test_storage_trim_dry_run_output_and_no_changes(tmp_path, monkeypatch):
    out = _trim_world(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["storage", "trim"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("convert to FLAC") and lines[0].endswith(str(out / "Ep.mp4"))
    assert "2.0 KB" in lines[0]
    assert any(line.startswith("Converted") and "2.0 KB" in line for line in lines)
    assert "Dry run: nothing was changed. Run again with --apply to do this." in result.output
    assert (out / "Ep.mp4").is_file() and not (out / "Ep.flac").exists()


def test_storage_trim_apply_after_dry_run_is_allowed(tmp_path, monkeypatch):
    out = _trim_world(tmp_path, monkeypatch)
    assert CliRunner().invoke(main, ["storage", "trim"]).exit_code == 0
    with patch("wisper_transcribe.audio_utils.encode_flac", side_effect=_fake_flac), \
         patch("wisper_transcribe.audio_utils.probe_format", return_value=(16000, 1)):
        result = CliRunner().invoke(main, ["storage", "trim", "--apply"])
    assert result.exit_code == 0, result.output
    assert (out / "Ep.flac").is_file() and not (out / "Ep.mp4").exists()
    with patch("wisper_transcribe.audio_utils.encode_flac", side_effect=_fake_flac), \
         patch("wisper_transcribe.audio_utils.probe_format", return_value=(16000, 1)):
        again = CliRunner().invoke(main, ["storage", "trim", "--apply"])
    assert again.exit_code == 0 and "Nothing to trim." in again.output


def test_storage_trim_apply_refused_while_server_lock_held(tmp_path, monkeypatch):
    from wisper_transcribe import db
    out = _trim_world(tmp_path, monkeypatch)
    held = db.ServerLock().acquire()
    try:
        result = CliRunner().invoke(main, ["storage", "trim", "--apply"])
    finally:
        held.release()
    assert result.exit_code != 0
    assert "Stop the wisper server first, then run this again." in result.output
    assert (out / "Ep.mp4").is_file()


def test_server_refuses_while_trim_holds_the_lock(tmp_path):
    from wisper_transcribe import db
    mock_uvicorn = MagicMock()
    held = db.ServerLock().acquire()
    try:
        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
            result = CliRunner().invoke(main, ["server"])
    finally:
        held.release()
    assert result.exit_code != 0
    assert "wisper storage trim is running; try again when it finishes" in result.output
    mock_uvicorn.run.assert_not_called()
    assert not (db.db_path()).exists()


def test_server_releases_its_lock_on_exit():
    from wisper_transcribe import db
    with patch.dict("sys.modules", {"uvicorn": MagicMock()}):
        assert CliRunner().invoke(main, ["server"]).exit_code == 0
    db.ServerLock().acquire().release()


def test_storage_trim_organizes_sessions_into_campaign_folders(tmp_path, monkeypatch):
    out = tmp_path / "trim_out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    from tests._moves import claimed_campaign, placed_session

    _cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("Stray", campaign=slug)

    dry = CliRunner().invoke(main, ["storage", "trim"])
    assert dry.exit_code == 0, dry.output
    lines = dry.output.splitlines()
    assert lines[0].startswith("move into campaign folder") and "Stray.md" in lines[0]
    assert any(line.startswith("Move 1 session") and "nothing deleted" in line
               for line in lines)
    assert md.is_file()

    with patch("wisper_transcribe.audio_utils.encode_flac", side_effect=_fake_flac), \
         patch("wisper_transcribe.audio_utils.probe_format", return_value=(16000, 1)):
        applied = CliRunner().invoke(main, ["storage", "trim", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "Moved 1 into campaign folders" in applied.output
    assert (out / folder / "Stray.md").is_file() and not md.exists()


def test_storage_trim_move_count_names_journals_apart():
    from wisper_transcribe.cli import _move_count
    from wisper_transcribe.storage_trim import ORGANIZE, Action

    def act(note=""):
        return Action(ORGANIZE, Path("x.md"), 1, note=note)

    assert _move_count([act()]) == "1 session"
    assert _move_count([act(), act(), act("journal")]) == "2 sessions and 1 journal"
    assert _move_count([act("journal"), act("journal")]) == "2 journals"

    again = CliRunner().invoke(main, ["storage", "trim"])
    assert again.exit_code == 0 and "Nothing to trim." in again.output


def test_storage_trim_reports_a_blocked_campaign(tmp_path, monkeypatch):
    out = tmp_path / "trim_out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    _seed.seed_campaign("Game", slug="game")   # unclaimed
    (out / "Game").mkdir()
    (out / "Game" / "notes.md").write_text("# mine\n", encoding="utf-8")
    _seed.seed_transcript("Stray", campaign="game", write_md=True)

    result = CliRunner().invoke(main, ["storage", "trim"])
    assert result.exit_code == 0, result.output
    assert "Game: 1 session can't be organized (folder taken; see Needs attention)" \
        in result.output


def test_storage_trim_container_refused_on_fresh_host_lease(tmp_path, monkeypatch):
    import sqlite3
    from datetime import UTC, datetime

    from wisper_transcribe import db
    _trim_world(tmp_path, monkeypatch)
    db.connect().close()
    with sqlite3.connect(db.db_path()) as conn:
        conn.execute("INSERT OR REPLACE INTO runtime_leases VALUES ('host', 'h:1', 0, ?)",
                     (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("container", True))
    result = CliRunner().invoke(main, ["storage", "trim", "--apply"])
    assert result.exit_code != 0 and "host process" in result.output
    with sqlite3.connect(db.db_path()) as conn:
        conn.execute("DELETE FROM runtime_leases WHERE runtime = 'host'")
    with patch("wisper_transcribe.audio_utils.encode_flac", side_effect=_fake_flac), \
         patch("wisper_transcribe.audio_utils.probe_format", return_value=(16000, 1)):
        ok = CliRunner().invoke(main, ["storage", "trim", "--apply"])
    assert ok.exit_code == 0, ok.output
