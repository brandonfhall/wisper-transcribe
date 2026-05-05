"""Tests for /api/record route stubs — Phase 2."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    """TestClient with server.json writing patched to tmp_path."""
    import wisper_transcribe.web.app as app_module
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app) as c:
            yield c, tmp_path


def test_record_start_creates_recording(client):
    c, _ = client
    resp = c.post("/api/record/start", json={"voice_channel_id": "123", "guild_id": "G1"})
    assert resp.status_code == 201
    data = resp.json()
    assert "recording_id" in data or "id" in data
    assert data.get("status") == "recording"


def test_record_start_missing_voice_channel_returns_400(client):
    c, _ = client
    resp = c.post("/api/record/start", json={})
    assert resp.status_code == 400


def test_record_stop_with_no_active_session_returns_400(client):
    c, _ = client
    resp = c.post("/api/record/stop")
    assert resp.status_code == 400


def test_record_status_returns_501(client):
    c, _ = client
    resp = c.get("/api/record/status")
    assert resp.status_code == 501


def test_recording_detail_invalid_id_returns_400(client):
    c, _ = client
    resp = c.get("/api/recordings/../evil")
    assert resp.status_code in (400, 404)


def test_recording_detail_null_byte_returns_400(client):
    from urllib.parse import quote
    c, _ = client
    resp = c.get(f"/api/recordings/{quote('some%00name')}")
    assert resp.status_code in (400, 404)


def test_server_json_written_on_lifespan_startup(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        import os
        os.environ["WISPER_BIND"] = "127.0.0.1:9999"
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app):
            sj = tmp_path / "server.json"
            assert sj.exists(), "server.json should be written during lifespan startup"
            data = json.loads(sj.read_text())
            assert data["url"] == "http://127.0.0.1:9999"
    # cleanup env
    os.environ.pop("WISPER_BIND", None)


def test_server_json_deleted_on_lifespan_shutdown(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app):
            pass  # exits context manager = shutdown
        sj = tmp_path / "server.json"
        assert not sj.exists(), "server.json should be deleted after lifespan shutdown"
