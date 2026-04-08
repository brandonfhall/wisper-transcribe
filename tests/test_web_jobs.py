"""Tests for the web job queue (wisper_transcribe.web.jobs)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch


def _make_queue():
    from wisper_transcribe.web.jobs import JobQueue
    return JobQueue()


def test_submit_returns_job_with_pending_status():
    q = _make_queue()
    job = q.submit("/tmp/test.mp3", model_size="tiny", no_diarize=True)
    assert job.id
    assert job.status == "pending"
    assert job.input_path == "/tmp/test.mp3"
    assert job.output_path is None
    assert job.error is None


def test_get_returns_job():
    q = _make_queue()
    job = q.submit("/tmp/test.mp3")
    assert q.get(job.id) is job


def test_get_unknown_returns_none():
    q = _make_queue()
    assert q.get("nonexistent-id") is None


def test_list_all_empty():
    q = _make_queue()
    assert q.list_all() == []


def test_list_all_sorted_by_created_at():
    q = _make_queue()
    j1 = q.submit("/tmp/a.mp3")
    j2 = q.submit("/tmp/b.mp3")
    jobs = q.list_all()
    # Most recent first
    assert jobs[0].id == j2.id
    assert jobs[1].id == j1.id


def test_active_count():
    q = _make_queue()
    assert q.active_count() == 0
    q.submit("/tmp/a.mp3")
    q.submit("/tmp/b.mp3")
    assert q.active_count() == 2


def test_active_count_excludes_terminal_states():
    from wisper_transcribe.web.jobs import COMPLETED, FAILED
    q = _make_queue()
    j1 = q.submit("/tmp/a.mp3")
    j2 = q.submit("/tmp/b.mp3")
    j1.status = COMPLETED
    j2.status = FAILED
    assert q.active_count() == 0


def test_run_job_captures_log_lines(tmp_path):
    """_run_job patches tqdm.write and appends to job.log_lines."""
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED
    from datetime import datetime
    from pathlib import Path

    out_md = tmp_path / "out.md"
    out_md.write_text("# Test")

    q = JobQueue()
    job = Job(
        id="test-id",
        status="running",
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={"no_diarize": True, "device": "cpu"},
    )

    with patch("wisper_transcribe.web.jobs.process_file", return_value=out_md):
        q._run_job(job)

    assert job.status == COMPLETED
    assert job.output_path == str(out_md)
    assert job.finished_at is not None


def test_run_job_records_error_on_failure(tmp_path):
    from wisper_transcribe.web.jobs import Job, JobQueue, FAILED
    from datetime import datetime

    q = JobQueue()
    job = Job(
        id="err-id",
        status="running",
        created_at=datetime.now(),
        input_path="/tmp/bad.mp3",
        kwargs={},
    )

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=RuntimeError("boom")):
        try:
            q._run_job(job)
        except RuntimeError:
            pass

    assert job.status == FAILED
    assert "boom" in job.error


def test_cancel_pending_job_marks_failed():
    from wisper_transcribe.web.jobs import FAILED
    q = _make_queue()
    job = q.submit("/tmp/a.mp3")
    assert q.cancel(job.id) is True
    assert job.status == FAILED
    assert job.error == "Cancelled"
    assert job.finished_at is not None


def test_cancel_unknown_job_returns_false():
    q = _make_queue()
    assert q.cancel("nonexistent") is False


def test_cancel_completed_job_returns_false():
    from wisper_transcribe.web.jobs import COMPLETED
    q = _make_queue()
    job = q.submit("/tmp/a.mp3")
    job.status = COMPLETED
    assert q.cancel(job.id) is False


def test_run_job_tqdm_patch_restores_original(tmp_path):
    """tqdm.write should be restored to its original after job completes."""
    import tqdm as _tqdm
    from wisper_transcribe.web.jobs import Job, JobQueue
    from datetime import datetime

    original_write = _tqdm.tqdm.write
    out_md = tmp_path / "out.md"
    out_md.write_text("# Test")

    q = JobQueue()
    job = Job(
        id="patch-id",
        status="running",
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={},
    )

    with patch("wisper_transcribe.web.jobs.process_file", return_value=out_md):
        q._run_job(job)

    # tqdm.write should be the original after job finishes
    assert _tqdm.tqdm.write is original_write
