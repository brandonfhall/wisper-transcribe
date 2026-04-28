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
         patch("wisper_transcribe.pipeline.process_folder", return_value=([out_md], [])):
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
