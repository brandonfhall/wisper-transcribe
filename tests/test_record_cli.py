"""Tests for wisper record CLI — Phase 2."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from wisper_transcribe.cli import main


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def server_json(tmp_path):
    """Write a fake server.json and return the data_dir."""
    sj = tmp_path / "server.json"
    sj.write_text(json.dumps({"url": "http://127.0.0.1:8080"}), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Server not running
# ---------------------------------------------------------------------------

def test_record_start_errors_when_server_not_running(runner, tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        result = runner.invoke(main, ["record", "start", "--voice-channel", "123"])
    assert result.exit_code != 0
    assert "not running" in result.output.lower() or "not running" in str(result.exception).lower()


# ---------------------------------------------------------------------------
# Server running — HTTP calls mocked
# ---------------------------------------------------------------------------

def test_record_start_reads_server_json_and_posts(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"detail": "not implemented"}
    mock_resp.raise_for_status = MagicMock()

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp) as mock_req:
        result = runner.invoke(main, [
            "record", "start", "--voice-channel", "456", "--campaign", "dnd-mondays"
        ])

    assert result.exit_code == 0
    mock_req.assert_called_once()
    call_kwargs = mock_req.call_args
    assert call_kwargs[0][0] == "POST"
    assert "/api/record/start" in call_kwargs[0][1]
    assert call_kwargs[1]["json"]["voice_channel_id"] == "456"
    assert call_kwargs[1]["json"]["campaign_slug"] == "dnd-mondays"


def test_wisper_server_url_env_var_overrides_server_json(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "ok"}
    mock_resp.raise_for_status = MagicMock()

    env = {**os.environ, "WISPER_SERVER_URL": "http://192.168.1.50:8080"}
    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp) as mock_req, \
         patch.dict(os.environ, {"WISPER_SERVER_URL": "http://192.168.1.50:8080"}):
        result = runner.invoke(main, ["record", "stop"])

    assert result.exit_code == 0
    called_url = mock_req.call_args[0][1]
    assert "192.168.1.50" in called_url


def test_record_list_formats_output(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status = MagicMock()

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp):
        result = runner.invoke(main, ["record", "list"])

    assert result.exit_code == 0


def test_record_show_validates_recording_id(runner, server_json):
    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json):
        result = runner.invoke(main, ["record", "show", "../traversal"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_record_stop_posts_to_server(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "completed"}
    mock_resp.raise_for_status = MagicMock()

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp) as mock_req:
        result = runner.invoke(main, ["record", "stop"])

    assert result.exit_code == 0
    mock_req.assert_called_once()
    assert mock_req.call_args[0][0] == "POST"
    assert "/api/record/stop" in mock_req.call_args[0][1]


def test_record_transcribe_validates_path_traversal(runner, server_json):
    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json):
        result = runner.invoke(main, ["record", "transcribe", "../evil"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_record_delete_validates_path_traversal(runner, server_json):
    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json):
        result = runner.invoke(main, ["record", "delete", "invalid*name", "--yes"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_record_start_missing_voice_channel_errors(runner, server_json):
    """record start without --voice-channel should fail (Click requires it)."""
    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json):
        result = runner.invoke(main, ["record", "start"])
    assert result.exit_code != 0


def test_record_show_valid_id_requests_server(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": "abc-123", "status": "recording"}
    mock_resp.raise_for_status = MagicMock()

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp) as mock_req:
        result = runner.invoke(main, ["record", "show", "abc-123"])

    assert result.exit_code == 0
    mock_req.assert_called_once()
    assert "/api/recordings/abc-123" in mock_req.call_args[0][1]


def test_record_transcribe_valid_id_posts_to_server(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "transcribing"}
    mock_resp.raise_for_status = MagicMock()

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp) as mock_req:
        result = runner.invoke(main, ["record", "transcribe", "abc-123"])

    assert result.exit_code == 0
    mock_req.assert_called_once()
    assert mock_req.call_args[0][0] == "POST"
    assert "/api/recordings/abc-123/transcribe" in mock_req.call_args[0][1]


# ---------------------------------------------------------------------------
# Phase 9 — hardening
# ---------------------------------------------------------------------------

def test_discord_token_masked_in_config_show(runner, tmp_path):
    """wisper config show masks discord_bot_token with ***."""
    cfg_path = tmp_path / "config.toml"
    import tomli_w
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"discord_bot_token": "super-secret-token", "model": "large-v3"}, f)

    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        result = runner.invoke(main, ["config", "show"])

    assert result.exit_code == 0
    assert "***" in result.output
    assert "super-secret-token" not in result.output


def test_config_discord_wizard_prompts_for_token(runner, tmp_path):
    """wisper config discord prompts for a token with input hidden."""
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        result = runner.invoke(main, ["config", "discord"], input="test-token-123\n\n\n")
    assert result.exit_code == 0
    assert "OK" in result.output or "saved" in result.output


def test_config_discord_wizard_empty_input_preserves_existing(runner, tmp_path):
    """wisper config discord keeps existing token when user enters nothing."""
    cfg_path = tmp_path / "config.toml"
    import tomli_w
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"discord_bot_token": "existing-token"}, f)

    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        result = runner.invoke(main, ["config", "discord"], input="\n\n\n")
    assert result.exit_code == 0

    import tomllib
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    assert cfg.get("discord_bot_token") == "existing-token"
