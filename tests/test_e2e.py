"""End-to-end flows through the web app against a real temp database.

Only the ML and LLM boundaries are mocked (Whisper, pyannote, embedding
extraction, the LLM client, ffmpeg checks). Everything else is real: the job
queue, pipeline, aligner, formatter, transcript store, campaign and journal
code, the search index, and SQLite. Each step checks database rows, files on
disk, and the search index.
"""
from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from wisper_transcribe import db, search_index
from wisper_transcribe.models import DiarizationSegment, TranscriptionSegment
from wisper_transcribe.path_utils import get_output_dir

from ._legacy_store import write_campaigns, write_speakers
from ._seed import seed_recording, unit

SEGMENTS = [
    TranscriptionSegment(start=0.0, end=4.0, text="Welcome back, the party stands before Castle Ravenloft."),
    TranscriptionSegment(start=5.0, end=9.0, text="I knock on the gate and call for Strahd."),
]
DIARIZATION = [
    DiarizationSegment(start=0.0, end=4.5, speaker="SPEAKER_00"),
    DiarizationSegment(start=4.5, end=9.5, speaker="SPEAKER_01"),
]
SUMMARY = {"summary": "The party reached Castle Ravenloft and summoned Strahd.",
           "session_title": "At the Gate", "loot": [], "npcs": [{"name": "Strahd"}]}


def _embedding(*args, **kwargs):
    label = kwargs.get("speaker_label") or args[2]
    return unit(np.eye(256)[0 if label == "SPEAKER_00" else 1])


@pytest.fixture
def ml():
    """Mock only the ML/LLM boundaries."""
    llm = MagicMock()
    llm.complete_json.return_value = SUMMARY
    llm.complete.return_value = "# Journal\n\nSession 1: the party reached the castle gate."
    llm.provider, llm.model = "fake", "fake-model"
    with ExitStack() as stack:
        for target, kw in [
            ("wisper_transcribe.pipeline.check_ffmpeg", {}),
            ("wisper_transcribe.pipeline.validate_audio", {}),
            ("wisper_transcribe.pipeline.convert_to_wav", {"side_effect": lambda p, *a, **k: Path(p)}),
            ("wisper_transcribe.audio_utils.convert_to_wav", {"side_effect": lambda p, *a, **k: Path(p)}),
            ("wisper_transcribe.pipeline.get_duration", {"return_value": 10.0}),
            ("wisper_transcribe.pipeline.transcribe", {"return_value": SEGMENTS}),
            ("wisper_transcribe.pipeline.get_hf_token", {"return_value": "hf_fake"}),
            ("wisper_transcribe.diarizer.diarize", {"return_value": DIARIZATION}),
            ("wisper_transcribe.speaker_manager.extract_embedding", {"side_effect": _embedding}),
            ("wisper_transcribe.llm.get_client", {"return_value": llm}),
        ]:
            stack.enter_context(patch(target, **kw))
        yield llm


@pytest.fixture
def client():
    from wisper_transcribe.web.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _wait(client, job_id: str, timeout: float = 20.0):
    queue = client.app.state.job_queue
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job is not None and job.status in ("completed", "failed"):
            assert job.status == "completed", (job.error, job.log_lines[-10:])
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _job_from(resp) -> str:
    assert resp.status_code == 303, resp.text[:300]
    return resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]


def _row(sql: str, *params):
    with db.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return tuple(row) if row is not None else None


def _count(sql: str, *params) -> int:
    return _row(sql, *params)[0]


def _upload(client, stem: str, campaign: str = "") -> str:
    data = {"campaign": campaign} if campaign else {}
    resp = client.post("/transcribe", files={"file": (f"{stem}.mp3", b"ID3fake-audio", "audio/mpeg")},
                       data=data, follow_redirects=False)
    return _job_from(resp)


# ---------------------------------------------------------------------------
# 1. Upload → transcribe → campaign → summarize → journal → rename → search → delete
# ---------------------------------------------------------------------------

def test_full_session_lifecycle(client, ml):
    out = get_output_dir()
    assert client.post("/campaigns", data={"display_name": "Curse of Strahd"},
                       follow_redirects=False).status_code == 303

    # Transcribe into the campaign.
    _wait(client, _upload(client, "Session 1", "curse-of-strahd"))
    md = out / "Session 1.md"
    assert md.is_file() and "Castle Ravenloft" in md.read_text(encoding="utf-8")
    tid, audio_rel = _row("SELECT id, audio_rel_path FROM transcripts WHERE stem = 'Session 1'")
    assert audio_rel and (out / audio_rel).is_file()  # durable audio copy next to it
    assert _count("SELECT count(*) FROM campaign_transcripts ct JOIN campaigns c ON c.id = ct.campaign_id "
                  "WHERE ct.transcript_id = ? AND c.slug = 'curse-of-strahd'", tid) == 1
    assert _count("SELECT count(*) FROM transcript_speakers WHERE transcript_id = ?", tid) == 2
    assert (out / "Session 1_diar.json").is_file()
    assert [g.stem for g in search_index.search("ravenloft").groups] == ["Session 1"]

    # Summarize.
    _wait(client, _job_from(client.post("/transcripts/Session%201/summarize", follow_redirects=False)))
    assert (out / "Session 1.summary.md").is_file()
    hits = search_index.search("summoned", kind="summary").groups
    assert [g.stem for g in hits] == ["Session 1"]

    # Fold into the journal.
    _wait(client, _job_from(client.post("/campaigns/curse-of-strahd/journal", data={"mode": "next"},
                                        follow_redirects=False)))
    assert _count("SELECT count(*) FROM journal_entries WHERE transcript_id = ?", tid) == 1
    assert _row("SELECT journal_sha256 FROM campaigns WHERE slug = 'curse-of-strahd'")[0]

    # Rename a speaker in the wizard.
    resp = client.post("/transcripts/Session%201/enroll", data={"speaker_SPEAKER_01": "Ezmerelda"},
                       follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    if "/transcribe/jobs/" in location:
        _wait(client, location.rsplit("/", 1)[-1])
    assert "**Ezmerelda**" in md.read_text(encoding="utf-8")
    assert _row("SELECT display_name, source FROM transcript_speakers "
                "WHERE transcript_id = ? AND label = 'SPEAKER_01'", tid) == ("Ezmerelda", "manual")
    assert "Ezmerelda" in search_index.speakers()
    page = client.get("/search", params={"q": "strahd", "speaker": "Ezmerelda"}).text
    assert 'data-testid="search-hit"' in page and "#b-1" in page

    # Delete: file, row, links, index, companions; the journal goes stale.
    assert client.post("/transcripts/Session%201/delete", follow_redirects=False).status_code == 303
    assert not md.exists() and not (out / "Session 1.summary.md").exists()
    assert not (out / "Session 1_diar.json").exists() and not (out / audio_rel).exists()
    for table in ("transcripts", "campaign_transcripts", "journal_entries", "transcript_speakers",
                  "search_index_state", "search_blocks"):
        col = "id" if table == "transcripts" else "transcript_id"
        assert _count(f"SELECT count(*) FROM {table} WHERE {col} = ?", tid) == 0, table
    assert search_index.search("ravenloft").groups == []
    assert _row("SELECT journal_stale_since FROM campaigns WHERE slug = 'curse-of-strahd'")[0]


# ---------------------------------------------------------------------------
# 2. A JSON-era data dir → import on first start → the same flow
# ---------------------------------------------------------------------------

def test_legacy_install_imports_then_works(ml, tmp_path, monkeypatch):
    data = tmp_path / "legacy-data"
    monkeypatch.setenv("WISPER_DATA_DIR", str(data))
    write_speakers(data, {"alice": {"display_name": "Alice"}}, embeddings={"alice": unit(np.eye(256)[0])})
    write_campaigns(data, {"cos": {"display_name": "Curse", "members": {"alice": {"role": "DM"}}}})
    (data / "config.toml").write_text("", encoding="utf-8")

    from wisper_transcribe.web.app import create_app
    with TestClient(create_app()) as client:  # startup migrates and imports
        assert not (data / "profiles" / "speakers.json").exists()
        assert list((data / "backups").glob("pre-sqlite-v2-*/profiles/speakers.json"))
        assert _count("SELECT count(*) FROM campaign_members m JOIN profiles p ON p.id = m.profile_id "
                      "WHERE p.key = 'alice'") == 1

        _wait(client, _upload(client, "Session 2", "cos"))
        md = get_output_dir() / "Session 2.md"
        # The imported profile matched SPEAKER_00 by its embedding.
        assert "**Alice**" in md.read_text(encoding="utf-8")
        assert [g.stem for g in search_index.search("gate", speaker="Alice").groups] == []
        assert [g.stem for g in search_index.search("ravenloft", speaker="Alice").groups] == ["Session 2"]
        assert client.post("/transcripts/Session%202/delete", follow_redirects=False).status_code == 303
        assert _count("SELECT count(*) FROM transcripts") == 0


# ---------------------------------------------------------------------------
# 3. Discord recording → transcribe → delete transcript → transcribable again
# ---------------------------------------------------------------------------

def test_recording_transcript_delete_reopens_recording(client, ml):
    from wisper_transcribe.recording_manager import load_recording

    rec = seed_recording(status="completed")
    assert rec.status == "completed"
    resp = client.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)
    assert resp.status_code == 303
    # Job history is written at submit, with the recording as its subject.
    (job_id,) = _row("SELECT id FROM jobs WHERE recording_id = ? ORDER BY created_at DESC", rec.id)
    _wait(client, job_id)

    done = load_recording(rec.id)
    assert done.status == "transcribed" and done.transcript_path is not None
    stem = Path(done.transcript_path).stem
    assert search_index.search("ravenloft").groups[0].stem == stem

    assert client.post(f"/transcripts/{stem}/delete", follow_redirects=False).status_code == 303
    again = load_recording(rec.id)
    assert again.status == "completed" and again.transcript_path is None
    assert "Transcribe" in client.get(f"/recordings/{rec.id}").text
