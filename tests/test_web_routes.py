"""Tests for web route handlers using FastAPI TestClient."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def app():
    """Fresh FastAPI app per test — no lifespan (background worker not started)."""
    from wisper_transcribe.web.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    # Don't start the lifespan (avoids background asyncio worker in tests)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_returns_200(client, tmp_path):
    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.get_device", return_value="cpu"), \
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)):
        resp = client.get("/")
    assert resp.status_code == 200
    assert b"wisper" in resp.content


def test_dashboard_jobs_partial_returns_200(client):
    resp = client.get("/jobs")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Transcribe form
# ---------------------------------------------------------------------------


def test_transcribe_get_returns_200(client):
    resp = client.get("/transcribe")
    assert resp.status_code == 200
    assert b"audio" in resp.content.lower()


def test_transcribe_post_queues_job_and_redirects(client, tmp_path):
    """Posting a file to /transcribe creates a job and redirects to job detail."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake mp3")

    with open(audio_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={"model_size": "tiny", "language": "en", "device": "cpu"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")


def test_job_detail_returns_200(client, tmp_path):
    """Job detail page renders correctly for an existing job."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake")
    with open(audio_file, "rb") as f:
        post_resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={},
            follow_redirects=False,
        )
    job_url = post_resp.headers["location"]
    resp = client.get(job_url)
    assert resp.status_code == 200


def test_job_detail_unknown_returns_404(client):
    resp = client.get("/transcribe/jobs/nonexistent-id-xyz")
    assert resp.status_code == 404


def test_cancel_job_redirects(client, tmp_path):
    """POST /transcribe/jobs/<id>/cancel marks job cancelled and redirects."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake")
    with open(audio_file, "rb") as f:
        post_resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={},
            follow_redirects=False,
        )
    job_url = post_resp.headers["location"]
    job_id = job_url.split("/")[-1]

    cancel_resp = client.post(f"/transcribe/jobs/{job_id}/cancel", follow_redirects=False)
    assert cancel_resp.status_code == 303
    assert cancel_resp.headers["location"] == job_url


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def test_transcripts_list_empty(client, tmp_path):
    with patch("wisper_transcribe.web.routes.transcripts._output_dir", return_value=tmp_path):
        resp = client.get("/transcripts")
    assert resp.status_code == 200


def test_transcripts_list_shows_files(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text("---\ntitle: Session 01\n---\n\n**Alice**: Hello.")
    with patch("wisper_transcribe.web.routes.transcripts._output_dir", return_value=tmp_path):
        resp = client.get("/transcripts")
    assert resp.status_code == 200
    assert b"session01" in resp.content


def test_transcript_detail_returns_200(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\ndate_processed: '2026-04-07'\nduration: '1:00:00'\n"
        "speakers:\n  - name: Alice\n    role: DM\n---\n\n**Alice** *(00:00)*: Hello."
    )
    with patch("wisper_transcribe.web.routes.transcripts._output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01")
    assert resp.status_code == 200
    assert b"Session 01" in resp.content


def test_transcript_detail_not_found(client, tmp_path):
    with patch("wisper_transcribe.web.routes.transcripts._output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/nonexistent")
    assert resp.status_code == 404


def test_transcript_detail_invalid_name_rejected(client):
    # The route regex allows only word chars and hyphens; dots are rejected
    resp = client.get("/transcripts/../../etc")
    # FastAPI normalizes the path — will result in 400 or 404
    assert resp.status_code in (400, 404, 422)


def test_transcript_download(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text("# Session 01\n\n**Alice**: Hello.")
    with patch("wisper_transcribe.web.routes.transcripts._output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01/download")
    assert resp.status_code == 200
    assert b"Session 01" in resp.content


def test_fix_speaker_renames_in_transcript(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n    role: ''\n---\n"
        "\n**SPEAKER_00** *(00:00)*: Hello."
    )
    with patch("wisper_transcribe.web.routes.transcripts._output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/session01/fix-speaker",
            data={"old_name": "SPEAKER_00", "new_name": "Alice"},
            follow_redirects=False,
        )
    assert resp.status_code in (303, 200)
    content = md.read_text()
    assert "Alice" in content
    assert "SPEAKER_00" not in content


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_get_returns_200(client):
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={"model": "medium"}), \
         patch("wisper_transcribe.web.routes.config.get_config_path", return_value=Path("/tmp/config.toml")):
        resp = client.get("/config")
    assert resp.status_code == 200
    assert b"model" in resp.content


def test_config_post_saves_and_redirects(client):
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.save_config") as mock_save:
        resp = client.post(
            "/config",
            data={"model": "large-v3", "device": "cpu"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "saved=1" in resp.headers["location"]
    mock_save.assert_called_once()


def test_config_post_bool_field_unchecked(client):
    """Unchecked checkbox should save as False, not missing key."""
    captured = {}

    def fake_save(cfg):
        captured.update(cfg)

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={"vad_filter": True}), \
         patch("wisper_transcribe.web.routes.config.save_config", side_effect=fake_save):
        # Submit form WITHOUT the vad_filter checkbox
        client.post("/config", data={"model": "medium"}, follow_redirects=False)

    assert captured.get("vad_filter") is False


# ---------------------------------------------------------------------------
# Speakers
# ---------------------------------------------------------------------------


def test_speakers_list_returns_200(client):
    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value={}):
        resp = client.get("/speakers")
    assert resp.status_code == 200


def test_speakers_enroll_form_returns_200(client):
    resp = client.get("/speakers/enroll")
    assert resp.status_code == 200


def test_speakers_remove_redirects(client, tmp_path):
    from wisper_transcribe.models import SpeakerProfile
    fake_emb = tmp_path / "alice.npy"
    fake_emb.write_bytes(b"")
    fake_profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=fake_emb,
        enrolled_date="2026-04-07",
        enrollment_source="test.mp3",
    )
    profiles = {"alice": fake_profile}

    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value=profiles), \
         patch("wisper_transcribe.web.routes.speakers.save_profiles") as mock_save:
        resp = client.post("/speakers/alice/remove", follow_redirects=False)

    assert resp.status_code == 303
    mock_save.assert_called_once_with({})  # alice removed, empty dict saved


def test_speakers_rename_updates_display_name(client):
    from wisper_transcribe.models import SpeakerProfile
    fake_profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=Path("/tmp/alice.npy"),
        enrolled_date="2026-04-07",
        enrollment_source="test.mp3",
    )
    profiles = {"alice": fake_profile}

    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value=profiles), \
         patch("wisper_transcribe.web.routes.speakers.save_profiles"):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "Alice (DM)"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert profiles["alice"].display_name == "Alice (DM)"


def test_speakers_rename_empty_name_no_change(client):
    from wisper_transcribe.models import SpeakerProfile
    fake_profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=Path("/tmp/alice.npy"),
        enrolled_date="2026-04-07",
        enrollment_source="test.mp3",
    )
    profiles = {"alice": fake_profile}

    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value=profiles), \
         patch("wisper_transcribe.web.routes.speakers.save_profiles") as mock_save:
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": ""},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_save.assert_not_called()  # empty name: no save
    assert profiles["alice"].display_name == "Alice"  # unchanged
