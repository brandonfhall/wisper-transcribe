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

    # Use the platform-native string form of the path so the assertion
    # matches the sidecar value on both POSIX and Windows.
    input_path_str = str(Path("/tmp/session01.mp3"))

    seg = DiarizationSegment(start=1.0, end=5.0, speaker="SPEAKER_00")
    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=input_path_str,
        kwargs={"campaign": "my-campaign"},
        output_path=str(out_md),
        diarization_segments=[seg],
    )

    _write_enrollment_sidecar(job, out_md)

    sidecar = tmp_path / "session01_diar.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["input_path"] == input_path_str
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


def test_enroll_form_prefills_previously_applied_names(client: TestClient, tmp_path: Path):
    """After a first rename pass, the wizard must show the applied display
    names in the input fields so the user can come back and fix a typo
    without losing what they typed."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Brandon\n- name: Sam\n---\n\n"
        "**Brandon** *(00:00)*: Hello everyone\n"
        "**Sam** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    body = resp.content.decode()
    assert 'name="speaker_SPEAKER_00"' in body
    assert 'value="Brandon"' in body
    assert 'name="speaker_SPEAKER_01"' in body
    assert 'value="Sam"' in body


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


def test_enroll_submit_second_pass_corrects_typo(client: TestClient, tmp_path: Path):
    """A second pass through the wizard must actually update the transcript.
    The form key is still the raw label, but the markdown body now contains
    the previously-applied display name — the submit handler has to translate
    raw_label -> current_display before calling update_speaker_names."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Bradnon\n- name: Sam\n---\n\n"
        "**Bradnon** *(00:00)*: Hello everyone\n"
        "**Sam** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Brandon", "speaker_SPEAKER_01": "Sam"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    content = md.read_text(encoding="utf-8")
    assert "**Brandon**" in content
    assert "**Bradnon**" not in content
    assert "**Sam**" in content


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


def test_excerpt_falls_back_to_legacy_display_name(client: TestClient, tmp_path: Path):
    """Pre-fix transcripts saved excerpt files keyed by display name (e.g.
    'Unknown_Speaker_1') instead of the raw pyannote label. The wizard +
    excerpt route must locate them via timestamp-matched markdown parsing
    so users don't have to re-transcribe."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\n---\n\n"
        "**Unknown Speaker 1** *(00:00)*: Hello everyone\n"
        "**Unknown Speaker 2** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    # Legacy file name: keyed by display name, not raw label
    legacy_clip = tmp_path / "session01_excerpt_Unknown_Speaker_1.mp3"
    legacy_clip.write_bytes(b"fake-mp3-data")

    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_00")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"


def test_enroll_form_finds_legacy_display_name_excerpts(client: TestClient, tmp_path: Path):
    """The wizard page must surface excerpts even when files are keyed by
    the old display-name convention."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\n---\n\n"
        "**Unknown Speaker 1** *(00:00)*: Hello everyone\n"
        "**Unknown Speaker 2** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    (tmp_path / "session01_excerpt_Unknown_Speaker_1.mp3").write_bytes(b"audio")
    (tmp_path / "session01_excerpt_Unknown_Speaker_1.txt").write_text(
        "Hello everyone", encoding="utf-8",
    )

    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 200
    # Sample button is rendered when speaker_excerpts contains the raw label
    assert b"Sample" in resp.content
    # Italic snippet is rendered when speaker_excerpt_texts contains the raw label
    assert b"Hello everyone" in resp.content


def test_legacy_backfill_uses_interval_match_not_exact_timestamp(client: TestClient, tmp_path: Path):
    """The legacy backfill must match by *interval containment*, not exact
    start-time string match. In real transcripts the markdown's first-block
    timestamp is the first whisper segment's start, which usually differs
    from pyannote's first-segment-of-SPEAKER_XX start (whisper skips silence,
    aligner may assign nearby segments to UNKNOWN, etc.)."""
    md = tmp_path / "session01.md"
    # Pyannote says SPEAKER_00 spans 0.0-30.0, but the first whisper line
    # assigned to SPEAKER_00 starts at 02:15 (135s) — well inside the turn
    # but NOT equal to the pyannote start of 0.0.
    md.write_text(
        "---\ntitle: Session 01\n---\n\n"
        "**Unknown Speaker 1** *(02:15)*: Mid-turn whisper segment\n",
        encoding="utf-8",
    )
    diar = {
        "input_path": "/tmp/session01.mp3",
        "campaign": None,
        "diarization_segments": [
            # Single long pyannote turn covering the markdown timestamp
            {"start": 0.0, "end": 300.0, "speaker": "SPEAKER_00"},
        ],
    }
    (tmp_path / "session01_diar.json").write_text(json.dumps(diar), encoding="utf-8")
    (tmp_path / "session01_excerpt_Unknown_Speaker_1.mp3").write_bytes(b"audio")

    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_00")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"


def test_extract_speaker_excerpts_uses_raw_labels(tmp_path: Path):
    """`_extract_speaker_excerpts` must key files by raw pyannote label
    (`SPEAKER_00`), not the rendered display name from the markdown."""
    from wisper_transcribe.web.jobs import _extract_speaker_excerpts, Job, COMPLETED
    from wisper_transcribe.models import AlignedSegment
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text(
        "**Unknown Speaker 1** *(00:00)*: Hello\n", encoding="utf-8",
    )
    fake_audio = tmp_path / "audio.mp3"
    fake_audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(fake_audio),
        kwargs={},
        output_path=str(out_md),
    )
    aligned = [
        AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00"),
        AlignedSegment(start=10.0, end=15.0, text="Hi", speaker="SPEAKER_00"),
        AlignedSegment(start=20.0, end=25.0, text="Goodbye", speaker="UNKNOWN"),
    ]

    with patch("wisper_transcribe.web.jobs.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        _extract_speaker_excerpts(job, out_md, aligned_segments=aligned)

    # The text file is written even if ffmpeg is mocked
    txt_path = tmp_path / "session01_excerpt_SPEAKER_00.txt"
    assert txt_path.exists()
    assert txt_path.read_text(encoding="utf-8") == "Hello"

    # UNKNOWN segments are skipped
    assert not (tmp_path / "session01_excerpt_UNKNOWN.txt").exists()

    # ffmpeg was invoked with the raw-label output path
    cmd = mock_run.call_args[0][0]
    assert any("SPEAKER_00" in str(p) for p in cmd)


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
