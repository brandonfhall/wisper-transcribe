"""Tests for /record and /recordings HTML routes + /api/record JSON API."""
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


# ---------------------------------------------------------------------------
# Phase 5 — HTML routes
# ---------------------------------------------------------------------------

def test_record_page_returns_200(client):
    c, _ = client
    resp = c.get("/record")
    assert resp.status_code == 200
    assert "Record" in resp.text


def test_recordings_list_returns_200_empty(client):
    c, _ = client
    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert "Recordings" in resp.text
    assert "No recordings yet" in resp.text


def test_recordings_list_groups_by_campaign(client):
    c, tmp_path = client
    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.recording_manager import create_recording, update_recording_status
    create_campaign("Test Campaign", data_dir=tmp_path)
    rec = create_recording("VC1", "G1", campaign_slug="test-campaign", data_dir=tmp_path)
    update_recording_status(rec.id, "completed", data_dir=tmp_path)
    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert "Test Campaign" in resp.text
    assert rec.id[:8] in resp.text


def test_recording_detail_returns_200(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert rec.id in resp.text


def test_recording_detail_unknown_id_redirects(client):
    c, _ = client
    import uuid
    unknown_id = str(uuid.uuid4())
    resp = c.get(f"/recordings/{unknown_id}", follow_redirects=False)
    assert resp.status_code == 303
    assert "/recordings" in resp.headers["location"]


def test_recording_delete_removes_entry(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, load_recordings
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.post(f"/recordings/{rec.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert load_recordings(tmp_path).get(rec.id) is None


def test_recording_live_returns_501(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.get(f"/recordings/{rec.id}/live")
    assert resp.status_code == 501
    assert resp.json().get("detail") == "not implemented in v1"


# ---------------------------------------------------------------------------
# Phase 6 — enrollment routes
# ---------------------------------------------------------------------------

def test_enroll_unknown_speaker_creates_profile(client):
    """POST /recordings/{id}/enroll with a valid unbound speaker creates a profile
    and removes the user from unbound_speakers."""
    from pathlib import Path
    from unittest.mock import patch

    from wisper_transcribe.models import SpeakerProfile
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.unbound_speakers = ["999999999999999999"]
    rec.discord_speakers["999999999999999999"] = ""
    save_recording(rec, tmp_path)

    dummy_profile = SpeakerProfile(
        name="bob",
        display_name="Bob",
        role="player",
        embedding_path=Path("/fake/bob.npy"),
        enrolled_date="2025-01-01",
        enrollment_source="test.opus",
    )

    with patch("wisper_transcribe.speaker_manager.enroll_speaker_from_audio_dir", return_value=dummy_profile):
        resp = c.post(
            f"/recordings/{rec.id}/enroll",
            data={"discord_user_id": "999999999999999999", "profile_name": "Bob"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    loaded = load_recordings(tmp_path)[rec.id]
    assert "999999999999999999" not in loaded.unbound_speakers
    assert loaded.discord_speakers.get("999999999999999999") == "bob"


def test_enroll_unknown_speaker_invalid_id_returns_400(client):
    """POST /recordings/{id}/enroll with a non-numeric discord_user_id returns 400."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.post(
        f"/recordings/{rec.id}/enroll",
        data={"discord_user_id": "not-a-snowflake", "profile_name": "Bob"},
    )
    assert resp.status_code == 400


def test_enroll_already_bound_speaker_returns_409(client):
    """POST /recordings/{id}/enroll for a user not in unbound_speakers returns 409."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    # User never added to unbound_speakers
    resp = c.post(
        f"/recordings/{rec.id}/enroll",
        data={"discord_user_id": "999999999999999999", "profile_name": "Bob"},
    )
    assert resp.status_code == 409
