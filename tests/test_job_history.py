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

    (get_output_dir() / "s1.md").write_text("x", encoding="utf-8")
    register("s1", origin="job")
    job_history.record(_job(status="completed", output_path=str(get_output_dir() / "s1.md"),
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
    from wisper_transcribe import recording_manager
    from wisper_transcribe.speaker_registry import SOURCE_AUTO, SOURCE_MANUAL
    from wisper_transcribe.web import jobs

    ddl = "\n".join(m.ddl for m in db.MIGRATIONS)

    def check_values(column: str) -> set[str]:
        m = re.search(rf"{column}\s+TEXT NOT NULL CHECK \({column} IN \(([^)]*)\)\)", ddl)
        assert m, column
        return set(re.findall(r"'([^']+)'", m.group(1)))

    job_types = {getattr(jobs, n) for n in dir(jobs) if n.startswith("JOB_") and isinstance(getattr(jobs, n), str)}
    assert check_values("type") == job_types
    assert check_values("status") == {jobs.PENDING, jobs.RUNNING, jobs.COMPLETED, jobs.FAILED}
    assert check_values("capture_status") == set(recording_manager.CAPTURE_STATUSES)
    assert check_values("source") >= {SOURCE_AUTO, SOURCE_MANUAL}


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
