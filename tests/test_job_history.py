"""job_history: the jobs table behind the in-memory queue."""
from __future__ import annotations

import json
import re
import sqlite3
import types
from datetime import datetime

import pytest

from wisper_transcribe import db, job_history

JID = "11111111-1111-4111-8111-111111111111"


def _job(**kw):
    base = dict(id=JID, job_type="transcription", status="pending", created_at=datetime.now(),
                started_at=None, finished_at=None, error=None, log_lines=[], output_path=None,
                recording_id=None, kwargs={"model_size": "small", "output_dir": "/tmp/x",
                                           "hotwords": ["Kyra"], "input_path": "/tmp/secret"})
    base.update(kw)
    return types.SimpleNamespace(**base)


def _row():
    with db.connection() as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (JID,)).fetchone())


def test_lifecycle_rows_satisfy_constraints():
    job = _job()
    job_history.record(job)
    assert _row()["status"] == "pending" and _row()["started_at"] is None
    job.status, job.started_at = "running", datetime.now()
    job_history.record(job)
    assert _row()["started_at"] is not None
    job.status, job.error, job.log_lines = "failed", "Transcription failed", [f"l{i}" for i in range(300)]
    job_history.record(job)
    row = _row()
    assert row["error_code"] == "Transcription failed" and row["finished_at"]
    assert row["log_tail"].splitlines()[0] == "l100"      # last 200 lines


def test_params_are_allowlisted_and_record_output_root():
    job_history.record(_job())
    params = json.loads(_row()["params_json"])
    assert params["model_size"] == "small" and params["output_root"]
    assert "output_dir" not in params and "input_path" not in params and "hotwords" not in params


def test_links_only_existing_subjects():
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import register

    md = get_output_dir() / "s1.md"
    md.write_text("x", encoding="utf-8")
    register(md, origin="job")
    job_history.record(_job(status="completed", output_path=str(md),
                            recording_id="22222222-2222-4222-8222-222222222222"))
    row = _row()
    assert row["transcript_id"] is not None and row["recording_id"] is None


def test_record_never_raises():
    job_history.record(_job(id="not-a-uuid"))   # CHECK fails; logged, not raised


def test_mark_interrupted_and_lookup():
    job_history.record(_job(status="running", started_at=datetime.now()))
    assert job_history.mark_interrupted() == 1
    rec = job_history.get_job(JID)
    assert rec.status == "failed" and rec.error == job_history.INTERRUPTED
    records, total = job_history.list_jobs(status="failed")
    assert total == 1 and records[0].id == JID


def test_schema_rejects_inconsistent_rows():
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO jobs (id, type, status, created_at, error_code) "
                         "VALUES (?, 'transcription', 'completed', 'x', 'e')", (JID,))


def test_enum_checks_mirror_python_constants():
    """Adding a job type, status, or source without a migration fails here."""
    from wisper_transcribe import file_registry, recording_manager
    from wisper_transcribe.speaker_registry import SOURCE_AUTO, SOURCE_MANUAL
    from wisper_transcribe.web import jobs

    ddl = "\n".join(m.ddl for m in db.MIGRATIONS)

    def check_values(column: str, source: str = ddl) -> set[str]:
        m = re.search(rf"{column}\s+TEXT NOT NULL CHECK \({column} IN \(([^)]*)\)\)", source)
        assert m, column
        return set(re.findall(r"'([^']+)'", m.group(1)))

    job_types = {getattr(jobs, n) for n in dir(jobs) if n.startswith("JOB_") and isinstance(getattr(jobs, n), str)}
    assert check_values("type") == job_types
    assert check_values("status") == {jobs.PENDING, jobs.RUNNING, jobs.COMPLETED, jobs.FAILED}
    assert check_values("capture_status") == set(recording_manager.CAPTURE_STATUSES)
    assert check_values("source") >= {SOURCE_AUTO, SOURCE_MANUAL}
    # Other tables also have a `kind` column, so read only the files table's DDL.
    files_ddl = next(m.ddl for m in db.MIGRATIONS if m.version == 9)
    assert check_values("kind", files_ddl) == set(file_registry.KINDS)


def test_history_page_and_detail_fallback(tmp_path):
    from fastapi.testclient import TestClient

    from wisper_transcribe.web.app import create_app

    job_history.record(_job(status="failed", started_at=datetime.now(), error="Transcription failed",
                            log_lines=["boom line"]))
    with TestClient(create_app()) as client:
        page = client.get("/jobs/history?status=failed&type=transcription")
        assert 'data-testid="job-history-row"' in page.text
        assert client.get("/jobs/history?type=<script>").status_code == 200
        detail = client.get(f"/transcribe/jobs/{JID}")
        assert 'data-testid="job-history-detail"' in detail.text and "boom line" in detail.text
        assert client.get("/transcribe/jobs/..%2F..%2Fetc").status_code == 404
        # After a restart the dashboard still lists it (memory + history).
        assert JID[:8] in client.get("/").text


def test_transcript_id_filter_lists_only_that_session(tmp_path):
    """`?transcript_id=` narrows job history to one session even when another
    campaign holds a session of the same name."""
    from fastapi.testclient import TestClient

    from wisper_transcribe import db, file_registry
    from wisper_transcribe.config import get_output_root
    from wisper_transcribe.web.app import create_app

    from . import _seed

    out = get_output_root()
    c1 = _seed.seed_campaign("Alpha", slug="alpha", claimed=True)
    c2 = _seed.seed_campaign("Beta", slug="beta", claimed=True)
    ids = {}
    for jid, cid, folder in (
        ("11111111-1111-4111-8111-111111111111", c1, "Alpha"),
        ("22222222-2222-4222-8222-222222222222", c2, "Beta"),
    ):
        md = out / folder / "Same.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("x", encoding="utf-8")
        with db.transaction() as conn:
            tid = conn.execute(
                "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                "VALUES (?, ?, 0, ?)",
                ("Same", cid, db.now_utc()),
            ).lastrowid
        file_registry.add(md, kind="transcript",
                          owner=file_registry.Owner("transcript", tid), output_dir=out)
        job_history.record(_job(id=jid, status="completed", output_path=str(md)))
        ids[cid] = tid

    with TestClient(create_app()) as client:
        filtered = client.get(f"/jobs/history?transcript_id={ids[c1]}")
    assert "11111111" in filtered.text and "22222222" not in filtered.text
    with db.connection() as conn:
        row = conn.execute("SELECT transcript_id FROM jobs WHERE id = ?",
                           ("11111111-1111-4111-8111-111111111111",)).fetchone()
    assert row["transcript_id"] == ids[c1]
    rec, total = job_history.list_jobs(transcript_id=ids[c2])
    assert total == 1 and rec[0].id == "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------------------
# A job's campaign is derived: subject, transcript, recording, submit params
# ---------------------------------------------------------------------------

def _transcript_job(job_id, stem, **kw):
    from wisper_transcribe.path_utils import get_output_dir
    from wisper_transcribe.transcript_store import register

    md = get_output_dir() / f"{stem}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("x", encoding="utf-8")
    register(md, origin="job")
    job_history.record(_job(id=job_id, status="completed", output_path=str(md), **kw))


def test_campaign_comes_from_the_transcripts_current_campaign():
    from wisper_transcribe.campaign_manager import create_campaign, move_transcript_to_campaign

    create_campaign("Curse")
    create_campaign("Other")
    _transcript_job(JID, "s1")
    tid = _tid("s1")
    move_transcript_to_campaign(tid, "curse")
    (rec,) = job_history.list_jobs(campaign="curse")[0]
    assert (rec.id, rec.campaign_slug, rec.campaign_name) == (JID, "curse", "Curse")
    assert job_history.get_job(JID).campaign_name == "Curse"

    move_transcript_to_campaign(tid, "other")  # the job follows its transcript
    assert job_history.list_jobs(campaign="curse")[0] == []
    assert [r.id for r in job_history.list_jobs(campaign="other")[0]] == [JID]


def test_campaign_falls_back_to_recording_then_submit_params():
    from wisper_transcribe.campaign_manager import create_campaign

    from ._seed import seed_recording

    create_campaign("Curse")
    rec = seed_recording(status="completed")
    with db.transaction() as conn:
        conn.execute("UPDATE recordings SET campaign_id = (SELECT id FROM campaigns WHERE slug = 'curse') "
                     "WHERE id = ?", (rec.id,))
    rec_job = "33333333-3333-4333-8333-333333333333"
    job_history.record(_job(id=rec_job, recording_id=rec.id))
    job_history.record(_job(kwargs={"campaign": "curse"}))  # never wrote a transcript
    job_history.record(_job(id="44444444-4444-4444-8444-444444444444", kwargs={"campaign": "gone"}))
    names = {r.id: r.campaign_name for r in job_history.list_jobs()[0]}
    assert names[rec_job] == "Curse" and names[JID] == "Curse"
    assert names["44444444-4444-4444-8444-444444444444"] is None
    assert {r.id for r in job_history.list_jobs(campaign="curse")[0]} == {rec_job, JID}


def test_dashboard_campaign_column(tmp_path):
    from fastapi.testclient import TestClient

    from wisper_transcribe.campaign_manager import create_campaign, move_transcript_to_campaign
    from wisper_transcribe.web.app import create_app
    from wisper_transcribe.web.routes.dashboard import job_campaigns

    create_campaign("Curse")
    _transcript_job(JID, "s1")
    move_transcript_to_campaign(_tid("s1"), "curse")
    with TestClient(create_app()) as client:
        queue = client.app.state.job_queue
        live = queue.submit(str(tmp_path / "wisper_upload_x.mp3"), campaign="curse")
        live.status = "failed"  # keep the worker off it
        rows = client.get("/jobs").text
    assert job_campaigns([live]) == {live.id: "Curse"}
    cells = re.findall(r'data-testid="job-campaign"[^>]*>([^<]*)<', rows)
    assert cells.count("Curse") == 2  # in-memory + history row


def _transcription(job_id, transcript_path, status="completed", job_type="transcription",
                   created=None, **kw):
    kwargs = {"model_size": "large-v3", "device": "cuda", "vad_filter": True,
              "overwrite": True, "campaign": "dnd", "language": "en",
              "num_speakers": 4, "no_diarize": False, "include_timestamps": True}
    kwargs.update(kw.pop("kwargs", {}))
    job = _job(id=job_id, status=status, job_type=job_type, kwargs=kwargs,
               output_path=str(transcript_path), created_at=created or datetime.now(),
               finished_at=datetime.now(), **kw)
    job.llm_transcript_path = str(transcript_path)
    job.post_refine = job.post_summarize = False
    return job


def _transcript_row(tmp_path, monkeypatch, stem="s1"):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    (out / f"{stem}.md").write_text("x", encoding="utf-8")
    from wisper_transcribe import transcript_store as ts
    ts.register(out / f"{stem}.md", origin="job")
    return out / f"{stem}.md"


def _tid(stem="s1"):
    with db.connection() as conn:
        return conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()[0]


def test_last_transcription_params_returns_latest_completed_session_settings(tmp_path, monkeypatch):
    md = _transcript_row(tmp_path, monkeypatch)
    old = "22222222-2222-4222-8222-222222222222"
    new = "33333333-3333-4333-8333-333333333333"
    job_history.record(_transcription(old, md, kwargs={"language": "fr", "num_speakers": 2}))
    newer = _transcription(new, md, created=datetime.now())
    newer.post_refine = True
    job_history.record(newer)
    assert job_history.last_transcription_params(_tid()) == {
        "language": "en", "num_speakers": 4, "no_diarize": False,
        "include_timestamps": True, "post_refine": True,
    }


def test_last_transcription_params_ignores_failed_and_other_jobs(tmp_path, monkeypatch):
    md = _transcript_row(tmp_path, monkeypatch)
    job_history.record(_transcription("22222222-2222-4222-8222-222222222222", md,
                                      kwargs={"language": "de"}))
    job_history.record(_transcription("33333333-3333-4333-8333-333333333333", md,
                                      status="failed", kwargs={"language": "fr"}))
    job_history.record(_transcription("44444444-4444-4444-8444-444444444444", md,
                                      job_type="refine", kwargs={"language": "es"}))
    assert job_history.last_transcription_params(_tid())["language"] == "de"


def test_last_transcription_params_drops_engine_and_submit_keys(tmp_path, monkeypatch):
    md = _transcript_row(tmp_path, monkeypatch)
    job_history.record(_transcription("22222222-2222-4222-8222-222222222222", md))
    got = job_history.last_transcription_params(_tid())
    for key in ("model_size", "device", "compute_type", "vad_filter", "forced_alignment",
                "overwrite", "campaign", "output_root"):
        assert key not in got


def test_last_transcription_params_empty_without_history(tmp_path, monkeypatch):
    _transcript_row(tmp_path, monkeypatch)
    assert job_history.last_transcription_params(_tid()) == {}
    assert job_history.last_transcription_params(99999) == {}
