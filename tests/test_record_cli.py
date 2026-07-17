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
        result = runner.invoke(main, ["record", "start", "--voice-channel", "123", "--guild", "456"])
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
            "record", "start", "--voice-channel", "456", "--guild", "789", "--campaign", "dnd-mondays"
        ])

    assert result.exit_code == 0
    mock_req.assert_called_once()
    call_kwargs = mock_req.call_args
    assert call_kwargs[0][0] == "POST"
    assert "/api/record/start" in call_kwargs[0][1]
    assert call_kwargs[1]["json"]["voice_channel_id"] == "456"
    assert call_kwargs[1]["json"]["guild_id"] == "789"
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


def test_record_list_calls_api_and_prints_response(runner, server_json):
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status = MagicMock()

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("httpx.request", return_value=mock_resp) as mock_req:
        result = runner.invoke(main, ["record", "list"])

    assert result.exit_code == 0
    mock_req.assert_called_once()
    assert mock_req.call_args[0][0] == "GET"
    assert "/api/recordings" in mock_req.call_args[0][1]
    assert "[]" in result.output


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


def test_record_start_falls_back_to_config_defaults(runner, server_json):
    """R30: with no --guild/--voice-channel and no --preset, `record start`
    falls back to discord_default_guild/discord_default_channel from config
    (the same keys `wisper config discord` and the web form already use)
    before erroring."""
    from wisper_transcribe.config import load_config, save_config

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json):
        cfg = load_config()
        cfg["discord_default_guild"] = "789"
        cfg["discord_default_channel"] = "456"
        save_config(cfg)

        with patch("wisper_transcribe.cli._record_request", return_value={"status": "ok"}) as mock_req:
            result = runner.invoke(main, ["record", "start"])

    assert result.exit_code == 0, result.output
    mock_req.assert_called_once()
    payload = mock_req.call_args.kwargs["json"]
    assert payload["voice_channel_id"] == "456"
    assert payload["guild_id"] == "789"


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
# R7 — end-to-end against the real FastAPI app (not just a mocked httpx.request)
#
# Every other test in this file mocks `httpx.request` directly, which can't
# catch a mismatch between what the CLI expects and what the server route
# actually does (that's exactly how /api/recordings shipped as a 501 stub
# behind passing CLI tests). This drives `_record_request` through the real
# `create_app()` via TestClient instead.
# ---------------------------------------------------------------------------

def test_record_list_show_transcribe_delete_end_to_end_against_real_app(runner, server_json):
    from fastapi.testclient import TestClient
    from urllib.parse import urlsplit

    from wisper_transcribe.recording_manager import create_recording
    from wisper_transcribe.web.app import create_app

    with patch("wisper_transcribe.config.get_data_dir", return_value=server_json), \
         patch("wisper_transcribe.web.routes.record.get_data_dir", return_value=server_json):
        rec = create_recording("VC1", "G1", data_dir=server_json)
        app = create_app()
        with TestClient(app) as test_client:

            def _fake_request(method, url, **kwargs):
                parsed = urlsplit(url)
                path = parsed.path
                if parsed.query:
                    path += f"?{parsed.query}"
                kwargs.pop("timeout", None)  # TestClient doesn't accept it
                return test_client.request(method, path, **kwargs)

            with patch("httpx.request", side_effect=_fake_request):
                list_result = runner.invoke(main, ["record", "list"])
                assert list_result.exit_code == 0, list_result.output
                assert rec.id in list_result.output

                show_result = runner.invoke(main, ["record", "show", rec.id])
                assert show_result.exit_code == 0, show_result.output
                assert rec.id in show_result.output

                # Not ready to transcribe yet ("recording" status) -> the
                # server returns 409, which the CLI surfaces as a non-zero
                # exit with the server's response body, not a crash.
                transcribe_result = runner.invoke(main, ["record", "transcribe", rec.id])
                assert transcribe_result.exit_code != 0
                assert "409" in transcribe_result.output

                delete_result = runner.invoke(main, ["record", "delete", rec.id, "--yes"])
                assert delete_result.exit_code == 0, delete_result.output

        from wisper_transcribe.recording_manager import load_recordings
        assert load_recordings(server_json).get(rec.id) is None


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
