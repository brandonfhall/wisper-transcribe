"""Tests for the transcript-centric enrollment wizard and _diar.json sidecar."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    from wisper_transcribe.web.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


_SAMPLE_MD = """\
---
title: Session 01
date_processed: '2026-05-14'
speakers:
- name: SPEAKER_00
- name: SPEAKER_01
---

# Session 01

**SPEAKER_00** *(00:00)*: Hello everyone
**SPEAKER_01** *(00:12)*: Thanks for having me
"""

_SAMPLE_DIAR = {
    "input_path": "/tmp/session01.mp3",
    "campaign": "d-d-mondays",
    "diarization_segments": [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 12.0, "end": 18.0, "speaker": "SPEAKER_01"},
    ],
}


def _write_transcript(tmp_path: Path, diar: dict | None = _SAMPLE_DIAR) -> Path:
    md = tmp_path / "session01.md"
    md.write_text(_SAMPLE_MD, encoding="utf-8")
    if diar is not None:
        (tmp_path / "session01_diar.json").write_text(
            json.dumps(diar), encoding="utf-8"
        )
    return md


def _patch_output(tmp_path: Path):
    return patch(
        "wisper_transcribe.web.routes.transcripts.get_output_dir",
        return_value=tmp_path,
    )


# ---------------------------------------------------------------------------
# Sidecar writer (_write_enrollment_sidecar in jobs.py)
# ---------------------------------------------------------------------------

def test_sidecar_written_after_job_completes(tmp_path: Path):
    """_write_enrollment_sidecar writes a _diar.json alongside the transcript."""
    from wisper_transcribe.web.jobs import _write_enrollment_sidecar, Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text("# Session 01", encoding="utf-8")

    seg = DiarizationSegment(start=1.0, end=5.0, speaker="SPEAKER_00")
    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path="/tmp/session01.mp3",
        kwargs={"campaign": "my-campaign"},
        output_path=str(out_md),
        diarization_segments=[seg],
    )

    _write_enrollment_sidecar(job, out_md)

    sidecar = tmp_path / "session01_diar.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["input_path"] == "/tmp/session01.mp3"
    assert data["campaign"] == "my-campaign"
    assert len(data["diarization_segments"]) == 1
    assert data["diarization_segments"][0] == {"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"}


def test_sidecar_not_written_when_no_segments(tmp_path: Path):
    """_write_enrollment_sidecar is a no-op when diarization_segments is empty."""
    from wisper_transcribe.web.jobs import _write_enrollment_sidecar, Job, COMPLETED
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text("# Session 01", encoding="utf-8")
    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path="/tmp/session01.mp3",
        kwargs={},
        output_path=str(out_md),
        diarization_segments=[],
    )

    _write_enrollment_sidecar(job, out_md)

    assert not (tmp_path / "session01_diar.json").exists()


# ---------------------------------------------------------------------------
# GET /transcripts/{name}/enroll
# ---------------------------------------------------------------------------

def test_enroll_form_renders_with_sidecar(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "SPEAKER_00" in body
    assert "SPEAKER_01" in body
    assert 'name="speaker_SPEAKER_00"' in body


def test_enroll_form_404_when_no_sidecar(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path, diar=None)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 404


def test_enroll_form_404_when_no_transcript(client: TestClient, tmp_path: Path):
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/nonexistent/enroll")
    assert resp.status_code == 404


def test_enroll_form_orders_speakers_by_first_appearance(client: TestClient, tmp_path: Path):
    """Speaker list must be ordered by first segment start time, not dict order."""
    diar = {
        "input_path": "/tmp/a.mp3",
        "campaign": None,
        "diarization_segments": [
            {"start": 10.0, "end": 15.0, "speaker": "SPEAKER_01"},
            {"start": 0.5, "end": 4.0, "speaker": "SPEAKER_00"},
        ],
    }
    _write_transcript(tmp_path, diar=diar)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    body = resp.content.decode()
    assert body.index("SPEAKER_00") < body.index("SPEAKER_01")


# ---------------------------------------------------------------------------
# POST /transcripts/{name}/enroll
# ---------------------------------------------------------------------------

def test_enroll_submit_renames_transcript(client: TestClient, tmp_path: Path):
    # The sidecar points to /tmp/session01.mp3 which won't exist in CI —
    # enrollment is silently skipped, but the rename always happens first.
    md = _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice", "speaker_SPEAKER_01": "Bob"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    content = md.read_text(encoding="utf-8")
    assert "**Alice**" in content
    assert "**Bob**" in content
    assert "**SPEAKER_00**" not in content


def test_enroll_submit_calls_enroll_speaker_when_audio_exists(
    client: TestClient, tmp_path: Path
):
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    diar = {**_SAMPLE_DIAR, "input_path": str(audio)}
    _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    mock_enroll.assert_called_once()
    kw = mock_enroll.call_args.kwargs
    assert kw["display_name"] == "Alice"
    assert kw["speaker_label"] == "SPEAKER_00"
    assert kw["audio_path"] == audio


def test_enroll_submit_skips_enroll_when_audio_missing(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    diar = {**_SAMPLE_DIAR, "input_path": "/nonexistent/audio.mp3"}
    _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll:
        client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    mock_enroll.assert_not_called()


def test_enroll_submit_adds_to_campaign(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign, get_campaign_profile_keys
    create_campaign("D&D Mondays")

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    diar = {**_SAMPLE_DIAR, "input_path": str(audio), "campaign": "d-d-mondays"}
    _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert "alice" in get_campaign_profile_keys("d-d-mondays")


def test_enroll_submit_404_when_no_sidecar(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path, diar=None)
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )
    assert resp.status_code == 404


def test_enroll_submit_no_renames_redirects_without_write(
    client: TestClient, tmp_path: Path
):
    md = _write_transcript(tmp_path)
    original = md.read_text(encoding="utf-8")
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert md.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# GET /transcripts/{name}/excerpt/{speaker_name}
# ---------------------------------------------------------------------------

def test_excerpt_serves_clip(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    clip = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    clip.write_bytes(b"fake-mp3-data")
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_00")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"


def test_excerpt_404_when_no_clip(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_99")
    assert resp.status_code == 404


def test_excerpt_rejects_null_byte(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER%00_00")
    assert resp.status_code == 400
