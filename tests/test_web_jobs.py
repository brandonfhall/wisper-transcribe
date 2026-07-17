"""Tests for the web job queue (wisper_transcribe.web.jobs)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest


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


def test_list_all_ties_break_by_submission_order():
    """R32-9: two jobs submitted within the same clock tick share an equal
    `created_at` -- the tie must break by insertion order, most-recently
    -submitted first (same guarantee the old reverse-then-stable-sort trick
    gave, now via an explicit (created_at, insertion_index) sort key)."""
    q = _make_queue()
    j1 = q.submit("/tmp/a.mp3")
    j2 = q.submit("/tmp/b.mp3")
    j3 = q.submit("/tmp/c.mp3")
    # Force an exact tie between all three.
    same_time = j2.created_at
    j1.created_at = same_time
    j3.created_at = same_time

    jobs = q.list_all()
    assert [j.id for j in jobs] == [j3.id, j2.id, j1.id]


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
    # R13: raw exception text must never reach job.error (it renders into
    # the job-detail page and SSE stream) — a generic message is used and
    # the real exception goes to the server log.
    assert "boom" not in job.error
    assert job.error == "Transcription failed — see server logs"


def test_cancel_pending_job_marks_failed():
    from wisper_transcribe.web.jobs import FAILED
    q = _make_queue()
    job = q.submit("/tmp/a.mp3")
    assert q.cancel(job.id) is True
    assert job.status == FAILED
    assert job.error == "Cancelled"
    assert job.finished_at is not None


@pytest.mark.anyio
async def test_worker_does_not_revive_cancelled_pending_job():
    """R3 regression: cancel() marks a PENDING job FAILED, but its id stays
    in the asyncio queue. _worker() must skip it instead of dequeuing it and
    unconditionally running it as if it were still pending.
    """
    from wisper_transcribe.web.jobs import FAILED

    q = _make_queue()
    with patch("wisper_transcribe.web.jobs.process_file") as mock_process:
        job = q.submit("/tmp/a.mp3", model_size="tiny", no_diarize=True)
        assert q.cancel(job.id) is True
        assert job.status == FAILED
        assert job.error == "Cancelled"

        # Drive the worker loop just long enough to dequeue the cancelled
        # job's id; it blocks forever afterwards waiting on an empty queue,
        # so bound it with a timeout instead of awaiting it directly.
        try:
            await asyncio.wait_for(q._worker(), timeout=0.2)
        except asyncio.TimeoutError:
            pass

    assert job.status == FAILED
    assert job.error == "Cancelled"
    mock_process.assert_not_called()


def test_cancel_unknown_job_returns_false():
    q = _make_queue()
    assert q.cancel("nonexistent") is False


def test_cancel_completed_job_returns_false():
    from wisper_transcribe.web.jobs import COMPLETED
    q = _make_queue()
    job = q.submit("/tmp/a.mp3")
    job.status = COMPLETED
    assert q.cancel(job.id) is False


def test_run_job_completed_after_post_process(tmp_path):
    """COMPLETED status must not be set until _run_post_process finishes.

    Regression test: previously job.status = COMPLETED was set before
    _run_post_process() was called, causing the SSE stream to fire 'done'
    while Ollama was still generating the campaign summary.
    """
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED, RUNNING
    from datetime import datetime

    out_md = tmp_path / "out.md"
    out_md.write_text("---\nspeakers: []\n---\n# Session\n")

    status_during_post_process: list[str] = []

    def fake_post_process(job: Job, transcript_path) -> None:
        # Record job status at the moment post-processing runs
        status_during_post_process.append(job.status)

    q = JobQueue()
    job = Job(
        id="pp-test",
        status=RUNNING,
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={"no_diarize": True, "device": "cpu"},
        post_summarize=True,
    )

    with patch("wisper_transcribe.web.jobs.process_file", return_value=out_md):
        with patch.object(q, "_run_post_process", side_effect=fake_post_process):
            q._run_job(job)

    # Post-process must have been called while job was still RUNNING
    assert status_during_post_process == [RUNNING], (
        f"Expected RUNNING during post-process, got {status_during_post_process}"
    )
    # After _run_job returns, job must be COMPLETED
    assert job.status == COMPLETED


# ---------------------------------------------------------------------------
# R14 -- unbounded memory growth guards
# ---------------------------------------------------------------------------


def test_append_log_caps_log_lines_and_tracks_dropped():
    """R14: Job.append_log trims the oldest lines once _MAX_LOG_LINES is
    exceeded and counts them in log_lines_dropped, rather than growing
    log_lines without bound."""
    from wisper_transcribe.web.jobs import Job, _MAX_LOG_LINES
    from datetime import datetime

    job = Job(id="log-cap-test", status="running", created_at=datetime.now(), input_path="/tmp/a.mp3", kwargs={})

    total = _MAX_LOG_LINES + 250
    for i in range(total):
        job.append_log(f"line {i}")

    assert len(job.log_lines) == _MAX_LOG_LINES
    assert job.log_lines_dropped == total - _MAX_LOG_LINES
    # Oldest lines are the ones dropped; the retained tail is contiguous
    # and ends with the most recent line appended.
    assert job.log_lines[0] == f"line {job.log_lines_dropped}"
    assert job.log_lines[-1] == f"line {total - 1}"


def test_append_log_under_cap_does_not_trim():
    from wisper_transcribe.web.jobs import Job
    from datetime import datetime

    job = Job(id="log-nocap-test", status="running", created_at=datetime.now(), input_path="/tmp/a.mp3", kwargs={})
    for i in range(10):
        job.append_log(f"line {i}")

    assert len(job.log_lines) == 10
    assert job.log_lines_dropped == 0


def test_prune_finished_jobs_caps_terminal_jobs_only(monkeypatch):
    """R14: only COMPLETED/FAILED jobs count against _MAX_RETAINED_JOBS, and
    the OLDEST terminal jobs are dropped first. PENDING/RUNNING jobs are
    never pruned, even when the terminal-job count alone exceeds the cap."""
    from wisper_transcribe.web.jobs import COMPLETED, FAILED, PENDING, RUNNING
    import wisper_transcribe.web.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "_MAX_RETAINED_JOBS", 3)

    q = _make_queue()
    terminal_jobs = [q.submit(f"/tmp/t{i}.mp3") for i in range(5)]
    for i, j in enumerate(terminal_jobs):
        j.status = COMPLETED if i % 2 == 0 else FAILED

    pending_job = q.submit("/tmp/pending.mp3")
    running_job = q.submit("/tmp/running.mp3")
    running_job.status = RUNNING

    q._prune_finished_jobs()

    remaining_ids = {j.id for j in q._jobs.values()}
    # 5 terminal jobs pruned down to the cap of 3 -- the 2 oldest gone.
    assert len(remaining_ids & {j.id for j in terminal_jobs}) == 3
    for j in terminal_jobs[:2]:
        assert j.id not in remaining_ids
    for j in terminal_jobs[2:]:
        assert j.id in remaining_ids
    # PENDING/RUNNING jobs are always retained, regardless of the cap.
    assert pending_job.id in remaining_ids
    assert running_job.id in remaining_ids


def test_prune_finished_jobs_noop_under_cap():
    q = _make_queue()
    from wisper_transcribe.web.jobs import COMPLETED
    job = q.submit("/tmp/a.mp3")
    job.status = COMPLETED
    q._prune_finished_jobs()
    assert q.get(job.id) is job


def test_cancel_pending_job_triggers_prune(monkeypatch):
    """R14: cancelling a PENDING job (which sets it straight to FAILED
    without ever passing through the worker's finally block) still gets
    swept by the retention cap."""
    from wisper_transcribe.web.jobs import COMPLETED
    import wisper_transcribe.web.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "_MAX_RETAINED_JOBS", 1)

    q = _make_queue()
    old_job = q.submit("/tmp/old.mp3")
    old_job.status = COMPLETED

    new_job = q.submit("/tmp/new.mp3")
    assert q.cancel(new_job.id) is True

    assert q.get(old_job.id) is None  # pruned
    assert q.get(new_job.id) is new_job  # the newly-cancelled job survives


def test_list_recent_respects_limit():
    q = _make_queue()
    for i in range(5):
        q.submit(f"/tmp/a{i}.mp3")
    assert len(q.list_recent(limit=3)) == 3


def test_on_complete_callback_invoked_after_completion(tmp_path):
    """on_complete fires exactly once, after status transitions to COMPLETED."""
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED
    from datetime import datetime

    out_md = tmp_path / "out.md"
    out_md.write_text("# Session")

    calls: list[Job] = []

    q = JobQueue()
    job = Job(
        id="cb-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={},
    )
    q._on_complete_callbacks[job.id] = lambda j: calls.append(j)

    with patch("wisper_transcribe.web.jobs.process_file", return_value=out_md):
        q._run_job(job)

    assert len(calls) == 1
    assert calls[0].status == COMPLETED
    # Callback is consumed — not called a second time
    assert job.id not in q._on_complete_callbacks


def test_on_complete_callback_not_invoked_on_failure(tmp_path):
    """on_complete must not fire when the job fails."""
    from wisper_transcribe.web.jobs import Job, JobQueue, FAILED
    from datetime import datetime

    calls: list[Job] = []

    q = JobQueue()
    job = Job(
        id="cb-fail",
        status="running",
        created_at=datetime.now(),
        input_path="/tmp/bad.mp3",
        kwargs={},
    )
    q._on_complete_callbacks[job.id] = lambda j: calls.append(j)

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=RuntimeError("oops")):
        try:
            q._run_job(job)
        except RuntimeError:
            pass

    assert calls == []
    assert job.status == FAILED


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


# ---------------------------------------------------------------------------
# F5 -- durable audio: move wisper_upload_* temp files to the output dir
# ---------------------------------------------------------------------------

def _fake_process_file_with_segments(out_md, segments):
    """Build a process_file stand-in that populates _result_store like the
    real pipeline does, so job.diarization_segments is non-empty afterwards."""
    def _fake(path, _result_store=None, job_id=None, **kwargs):
        if _result_store is not None:
            _result_store["diarization_segments"] = segments
        return out_md
    return _fake


def test_submit_detects_web_upload_prefix(tmp_path):
    """JobQueue.submit must flag is_web_upload from the *original* basename,
    before the friendly-name rename strips the wisper_upload_ prefix."""
    q = _make_queue()
    upload = tmp_path / "wisper_upload_abc123.mp3"
    upload.write_bytes(b"fake-audio")

    job = q.submit(str(upload), original_stem="My Session")

    assert job.is_web_upload is True
    # The rename already happened inside submit() -- the prefix is gone from
    # the current path, but the flag must still be True.
    assert not Path(job.input_path).name.startswith("wisper_upload_")
    assert Path(job.input_path).name == "My Session.mp3"


def test_submit_non_upload_path_not_flagged(tmp_path):
    """A durable, non-temp input (e.g. a recording) must never be flagged."""
    q = _make_queue()
    rec = tmp_path / "recording123.wav"
    rec.write_bytes(b"fake-audio")

    job = q.submit(str(rec))

    assert job.is_web_upload is False


def test_completed_job_moves_upload_to_output_dir(tmp_path):
    """(a) A completed job moves the wisper_upload_* temp file next to the
    transcript, and the enrollment sidecar records the durable path."""
    import json
    from datetime import datetime
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    upload = tmp_dir / "wisper_upload_abc123.mp3"
    upload.write_bytes(b"fake-audio")
    out_md = out_dir / "Session 12.md"
    out_md.write_text("# Session 12", encoding="utf-8")

    seg = DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")

    q = JobQueue()
    job = Job(
        id="move-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(upload),
        kwargs={},
        is_web_upload=True,
    )

    fake_pf = _fake_process_file_with_segments(out_md, [seg])
    with patch("wisper_transcribe.web.jobs.process_file", side_effect=fake_pf):
        q._run_job(job)

    assert job.status == COMPLETED
    durable = out_dir / "Session 12.mp3"
    assert Path(job.input_path) == durable
    assert durable.exists()
    assert not upload.exists()

    sidecar = out_dir / "Session 12_diar.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["input_path"] == str(durable)


def test_non_temp_input_not_moved(tmp_path):
    """(b) A non-temp (e.g. recording-sourced) input must never be moved,
    even though it lives next to (or anywhere relative to) the output dir."""
    from datetime import datetime
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()

    recording = rec_dir / "rec-abc123.wav"
    recording.write_bytes(b"fake-audio")
    out_md = out_dir / "Session 12.md"
    out_md.write_text("# Session 12", encoding="utf-8")

    seg = DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")

    q = JobQueue()
    job = Job(
        id="no-move-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(recording),
        kwargs={},
        is_web_upload=False,
    )

    fake_pf = _fake_process_file_with_segments(out_md, [seg])
    with patch("wisper_transcribe.web.jobs.process_file", side_effect=fake_pf):
        q._run_job(job)

    assert job.status == COMPLETED
    # Untouched -- still at its original recordings path
    assert job.input_path == str(recording)
    assert recording.exists()
    assert not (out_dir / "Session 12.wav").exists()


def test_failed_job_deletes_temp_upload(tmp_path):
    """(c) A failed job deletes its wisper_upload_* temp file -- it's useless
    once the job won't complete, so it must not leak until next restart."""
    from datetime import datetime
    from wisper_transcribe.web.jobs import Job, JobQueue, FAILED

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    upload = tmp_dir / "wisper_upload_fail123.mp3"
    upload.write_bytes(b"fake-audio")

    q = JobQueue()
    job = Job(
        id="fail-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(upload),
        kwargs={},
        is_web_upload=True,
    )

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=RuntimeError("boom")):
        try:
            q._run_job(job)
        except RuntimeError:
            pass

    assert job.status == FAILED
    assert not upload.exists()


def test_cancelled_job_deletes_temp_upload(tmp_path):
    """(c) Cancellation (InterruptedError) must also delete the temp upload."""
    from datetime import datetime
    from wisper_transcribe.web.jobs import Job, JobQueue, FAILED

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    upload = tmp_dir / "wisper_upload_cancel123.mp3"
    upload.write_bytes(b"fake-audio")

    q = JobQueue()
    job = Job(
        id="cancel-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(upload),
        kwargs={},
        is_web_upload=True,
    )

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=InterruptedError("cancelled")):
        q._run_job(job)

    assert job.status == FAILED
    assert job.error == "Cancelled"
    assert not upload.exists()


def test_move_upload_collision_gets_counter_suffix(tmp_path):
    """(d) A name collision with an existing file in the output dir must not
    clobber it -- the moved upload gets a counter suffix instead."""
    from datetime import datetime
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    # Pre-existing file at the exact destination the mover would pick.
    collision = out_dir / "Session 12.mp3"
    collision.write_bytes(b"pre-existing-file")

    upload = tmp_dir / "wisper_upload_dup123.mp3"
    upload.write_bytes(b"new-upload-audio")
    out_md = out_dir / "Session 12.md"
    out_md.write_text("# Session 12", encoding="utf-8")

    seg = DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")

    q = JobQueue()
    job = Job(
        id="collision-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(upload),
        kwargs={},
        is_web_upload=True,
    )

    fake_pf = _fake_process_file_with_segments(out_md, [seg])
    with patch("wisper_transcribe.web.jobs.process_file", side_effect=fake_pf):
        q._run_job(job)

    assert job.status == COMPLETED
    # Original collision file must be untouched
    assert collision.read_bytes() == b"pre-existing-file"
    # New file lands with a counter suffix
    counted = out_dir / "Session 12_1.mp3"
    assert counted.exists()
    assert counted.read_bytes() == b"new-upload-audio"
    assert Path(job.input_path) == counted


def test_completed_job_no_diarization_deletes_upload_not_moves(tmp_path):
    """When a job completes with no diarization data, there will never be a
    _diar.json sidecar to record an audio path -- moving the file would leak
    it in the output dir forever. It must be deleted instead, same as the
    failure path."""
    from datetime import datetime
    from wisper_transcribe.web.jobs import Job, JobQueue, COMPLETED

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    upload = tmp_dir / "wisper_upload_nodiar123.mp3"
    upload.write_bytes(b"fake-audio")
    out_md = out_dir / "Session 12.md"
    out_md.write_text("# Session 12", encoding="utf-8")

    q = JobQueue()
    job = Job(
        id="no-diar-test",
        status="running",
        created_at=datetime.now(),
        input_path=str(upload),
        kwargs={},
        is_web_upload=True,
    )

    # process_file returns no diarization_segments (e.g. --no-diarize)
    with patch("wisper_transcribe.web.jobs.process_file", return_value=out_md):
        q._run_job(job)

    assert job.status == COMPLETED
    assert not upload.exists()
    assert not (out_dir / "Session 12.mp3").exists()
    assert not (out_dir / "Session 12_diar.json").exists()


# ---------------------------------------------------------------------------
# JOB_ENROLL — Phase 2.5 (speaker-enrollment wizard's slow half as a job)
# ---------------------------------------------------------------------------

def test_submit_enroll_creates_pending_job_with_groups():
    q = _make_queue()
    job = q.submit_enroll(
        md_path="/tmp/session01.md",
        transcript_name="session01",
        groups={"Alice": ["SPEAKER_00"]},
        device="cpu",
    )
    assert job.status == "pending"
    assert job.job_type == "enroll"
    assert job.enroll_groups == {"Alice": ["SPEAKER_00"]}
    assert job.enroll_md_path == "/tmp/session01.md"
    assert job.enroll_device == "cpu"
    # (4) output_path is set immediately -- not just on completion -- so the
    # job detail page's "View transcript" link works while it's still running.
    assert job.output_path == "/tmp/session01.md"


def _write_sidecar(tmp_path, md_path, input_path, campaign=None):
    import json
    diar = {
        "input_path": str(input_path),
        "campaign": campaign,
        "diarization_segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
    }
    md_path.with_name(md_path.stem + "_diar.json").write_text(
        json.dumps(diar), encoding="utf-8"
    )


def test_run_enroll_job_success_calls_enroll_profiles_and_completes(tmp_path):
    """(d) The success path calls enroll_profiles() and sets COMPLETED +
    output_path."""
    from wisper_transcribe.web.jobs import JobQueue, COMPLETED

    md_path = tmp_path / "session01.md"
    md_path.write_text("# Session 01", encoding="utf-8")
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    _write_sidecar(tmp_path, md_path, audio)

    q = JobQueue()
    job = q.submit_enroll(
        md_path=str(md_path),
        transcript_name="session01",
        groups={"Alice": ["SPEAKER_00"]},
        device="cpu",
    )

    with patch("wisper_transcribe.web.enroll_shared.enroll_profiles") as mock_enroll_profiles:
        q._run_enroll_job(job)

    mock_enroll_profiles.assert_called_once()
    kw = mock_enroll_profiles.call_args.kwargs
    assert kw["input_path"] == audio
    assert kw["groups"] == {"Alice": ["SPEAKER_00"]}
    assert kw["device"] == "cpu"
    assert job.status == COMPLETED
    assert job.output_path == str(md_path)
    assert job.finished_at is not None


def test_run_enroll_job_progress_lines_land_in_log(tmp_path):
    """(f) Progress lines the runner passes to enroll_profiles() land in
    job.log_lines (so the SSE stream picks them up)."""
    from wisper_transcribe.web.jobs import JobQueue, COMPLETED

    md_path = tmp_path / "session01.md"
    md_path.write_text("# Session 01", encoding="utf-8")
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    _write_sidecar(tmp_path, md_path, audio)

    q = JobQueue()
    job = q.submit_enroll(
        md_path=str(md_path),
        transcript_name="session01",
        groups={"Alice": ["SPEAKER_00"]},
        device="cpu",
    )

    def fake_enroll_profiles(*, input_path, segments, groups, campaign_slug,
                              device, data_dir=None, progress=None):
        if progress is not None:
            progress("Converting audio…")
            progress("Extracting embedding for Alice (1/1)…")

    with patch("wisper_transcribe.web.enroll_shared.enroll_profiles", side_effect=fake_enroll_profiles):
        q._run_enroll_job(job)

    assert job.status == COMPLETED
    assert "Converting audio…" in job.log_lines
    assert any("Alice" in line for line in job.log_lines)


def test_run_enroll_job_missing_audio_sets_generic_error(tmp_path):
    """(e) Missing source audio fails the job with a generic message --
    never the path."""
    from wisper_transcribe.web.jobs import JobQueue, FAILED

    md_path = tmp_path / "session01.md"
    md_path.write_text("# Session 01", encoding="utf-8")
    missing_audio = tmp_path / "nonexistent.mp3"
    _write_sidecar(tmp_path, md_path, missing_audio)

    q = JobQueue()
    job = q.submit_enroll(
        md_path=str(md_path),
        transcript_name="session01",
        groups={"Alice": ["SPEAKER_00"]},
        device="cpu",
    )

    q._run_enroll_job(job)

    assert job.status == FAILED
    assert job.error == "Source audio not available"
    assert str(tmp_path) not in job.error
    assert job.finished_at is not None


def test_run_enroll_job_missing_sidecar_sets_generic_error(tmp_path):
    """No _diar.json at all (e.g. deleted between wizard submit and job run)
    also fails generically rather than raising."""
    from wisper_transcribe.web.jobs import JobQueue, FAILED

    md_path = tmp_path / "session01.md"
    md_path.write_text("# Session 01", encoding="utf-8")
    # No sidecar written.

    q = JobQueue()
    job = q.submit_enroll(
        md_path=str(md_path),
        transcript_name="session01",
        groups={"Alice": ["SPEAKER_00"]},
        device="cpu",
    )

    q._run_enroll_job(job)

    assert job.status == FAILED
    assert job.error == "Source audio not available"


def test_run_enroll_job_exception_sets_generic_error_not_path(tmp_path):
    """(e) An unexpected exception from enroll_profiles() (e.g. a WAV
    conversion failure whose message contains a path) must never leak that
    path into job.error -- the job detail page renders it directly into
    HTML."""
    from wisper_transcribe.web.jobs import JobQueue, FAILED

    md_path = tmp_path / "session01.md"
    md_path.write_text("# Session 01", encoding="utf-8")
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    _write_sidecar(tmp_path, md_path, audio)

    q = JobQueue()
    job = q.submit_enroll(
        md_path=str(md_path),
        transcript_name="session01",
        groups={"Alice": ["SPEAKER_00"]},
        device="cpu",
    )

    boom = RuntimeError(f"couldn't decode {tmp_path / 'session01.mp3'}")
    with patch("wisper_transcribe.web.enroll_shared.enroll_profiles", side_effect=boom):
        q._run_enroll_job(job)

    assert job.status == FAILED
    assert job.error == "Enrollment failed"
    assert str(tmp_path) not in job.error


# ---------------------------------------------------------------------------
# R13: generic job error messages — no raw exception text in job.error
# ---------------------------------------------------------------------------

def _make_failed_job(job_type, exc, **fields):
    from datetime import datetime as _dt
    from wisper_transcribe.web.jobs import Job
    return Job(
        id="r13-id",
        status="running",
        created_at=_dt.now(),
        input_path=str(fields.pop("input_path", "/tmp/whatever.mp3")),
        kwargs={},
        job_type=job_type,
        **fields,
    )


def test_transcription_error_does_not_leak_paths(tmp_path):
    """R13: an exception message carrying a filesystem path must not land in
    job.error for a transcription job."""
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    secret = tmp_path / "private" / "session.mp3"
    job = _make_failed_job("transcription", None, input_path=secret)

    with patch(
        "wisper_transcribe.web.jobs.process_file",
        side_effect=RuntimeError(f"ffmpeg failed on {secret}"),
    ):
        try:
            q._run_job(job)
        except RuntimeError:
            pass

    assert job.status == FAILED
    assert str(tmp_path) not in job.error
    assert job.error == "Transcription failed — see server logs"


def test_llm_job_error_is_generic(tmp_path):
    """R13: standalone refine/summarize job failures use a generic message."""
    from wisper_transcribe.web.jobs import FAILED, JOB_REFINE, JobQueue

    q = JobQueue()
    md = tmp_path / "t.md"
    md.write_text("body", encoding="utf-8")
    job = _make_failed_job(JOB_REFINE, None, llm_transcript_path=str(md))

    with patch.object(
        JobQueue, "_do_llm_work",
        side_effect=RuntimeError(f"cannot open {tmp_path}/secret.bin"),
    ):
        try:
            q._run_job(job)
        except RuntimeError:
            pass

    assert job.status == FAILED
    assert str(tmp_path) not in job.error
    assert job.error == "Post-processing failed — see server logs"


def test_file_not_found_maps_to_short_safe_message():
    """R13: known input errors get short safe text, still with no path."""
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    job = _make_failed_job("transcription", None)

    with patch(
        "wisper_transcribe.web.jobs.process_file",
        side_effect=FileNotFoundError("/tmp/gone/file.mp3"),
    ):
        try:
            q._run_job(job)
        except FileNotFoundError:
            pass

    assert job.status == FAILED
    assert job.error == "Input file not found"
    assert "/tmp/gone" not in job.error


def test_cancelled_error_string_is_preserved():
    """R13: the literal "Cancelled" string survives the generic-error policy
    (other code and the job template check for it)."""
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    job = _make_failed_job("transcription", None)

    with patch(
        "wisper_transcribe.web.jobs.process_file",
        side_effect=InterruptedError("Job cancelled by user"),
    ):
        q._run_job(job)

    assert job.status == FAILED
    assert job.error == "Cancelled"


def test_post_process_log_line_is_generic(tmp_path):
    """R13: the post-processing failure line appended to job.log_lines (also
    rendered in the UI) never carries raw exception text."""
    from wisper_transcribe.web.jobs import JobQueue

    q = JobQueue()
    job = _make_failed_job("transcription", None, post_refine=True)

    with patch.object(
        JobQueue, "_do_llm_work",
        side_effect=RuntimeError(f"boom at {tmp_path}/x"),
    ):
        q._run_post_process(job, tmp_path / "t.md")

    assert any("Post-processing failed — see server logs" == l for l in job.log_lines)
    assert not any(str(tmp_path) in l for l in job.log_lines)


# ---------------------------------------------------------------------------
# R6: standalone / recording enroll job runners
# ---------------------------------------------------------------------------

def _make_standalone_enroll_job(tmp_path, **param_overrides):
    from datetime import datetime as _dt
    from wisper_transcribe.web.jobs import JOB_ENROLL, Job

    upload = tmp_path / "wisper_enrollsrc_test.mp3"
    upload.write_bytes(b"fake audio")
    params = {
        "profile_key": "alice",
        "display_name": "Alice",
        "role": "DM",
        "notes": "",
        "update": False,
    }
    params.update(param_overrides)
    return Job(
        id="standalone-test",
        status="running",
        created_at=_dt.now(),
        input_path=str(upload),
        kwargs={},
        job_type=JOB_ENROLL,
        enroll_mode="standalone",
        enroll_params=params,
    ), upload


def test_standalone_enroll_job_success_cleans_temp_files(tmp_path):
    """R6/R9-1: the standalone enroll runner enrolls the primary speaker and
    deletes both the temp upload and the converted WAV — the cleanup that
    lived in the route before the hand-off."""
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.jobs import COMPLETED, JobQueue

    q = JobQueue()
    job, upload = _make_standalone_enroll_job(tmp_path)

    converted = tmp_path / "converted.wav"

    def _fake_convert(path):
        converted.write_bytes(b"RIFF" + b"\x00" * 36)
        return converted

    diarization = [DiarizationSegment(start=0.0, end=2.0, speaker="SPEAKER_00")]

    with patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=_fake_convert), \
         patch("wisper_transcribe.config.get_device", return_value="cpu"), \
         patch("wisper_transcribe.config.get_hf_token", return_value="fake-token"), \
         patch("wisper_transcribe.config.load_config", return_value={}), \
         patch("wisper_transcribe.diarizer.diarize", return_value=diarization), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        q._run_job(job)

    assert job.status == COMPLETED
    mock_enroll.assert_called_once()
    kwargs = mock_enroll.call_args.kwargs
    assert kwargs["name"] == "alice"
    assert kwargs["display_name"] == "Alice"
    assert kwargs["speaker_label"] == "SPEAKER_00"
    assert not upload.exists()
    assert not converted.exists()


def test_standalone_enroll_job_update_merges_embedding(tmp_path):
    """update=True with an existing profile goes through update_embedding
    (EMA merge) instead of enroll_speaker."""
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.jobs import COMPLETED, JobQueue

    q = JobQueue()
    job, upload = _make_standalone_enroll_job(tmp_path, update=True)

    diarization = [DiarizationSegment(start=0.0, end=2.0, speaker="SPEAKER_00")]

    with patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p), \
         patch("wisper_transcribe.config.get_device", return_value="cpu"), \
         patch("wisper_transcribe.config.get_hf_token", return_value="fake-token"), \
         patch("wisper_transcribe.config.load_config", return_value={}), \
         patch("wisper_transcribe.diarizer.diarize", return_value=diarization), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={"alice": object()}), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=[0.0]) as mock_extract, \
         patch("wisper_transcribe.speaker_manager.update_embedding") as mock_update, \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll:
        q._run_job(job)

    assert job.status == COMPLETED
    mock_extract.assert_called_once()
    mock_update.assert_called_once()
    mock_enroll.assert_not_called()
    assert not upload.exists()


def test_standalone_enroll_job_failure_is_generic_and_cleans_up(tmp_path):
    """R6/R13: a failure mid-enroll sets a generic error (no exception text,
    no paths) and still deletes the temp upload."""
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    job, upload = _make_standalone_enroll_job(tmp_path)

    with patch("wisper_transcribe.audio_utils.convert_to_wav",
               side_effect=RuntimeError(f"ffmpeg failed on {upload}")):
        q._run_job(job)  # must not raise — enroll jobs swallow locally

    assert job.status == FAILED
    assert job.error == "Enrollment failed"
    assert str(tmp_path) not in job.error
    assert not upload.exists()


def test_standalone_enroll_job_no_speech_sets_safe_error(tmp_path):
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    job, upload = _make_standalone_enroll_job(tmp_path)

    with patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p), \
         patch("wisper_transcribe.config.get_device", return_value="cpu"), \
         patch("wisper_transcribe.config.get_hf_token", return_value="fake-token"), \
         patch("wisper_transcribe.config.load_config", return_value={}), \
         patch("wisper_transcribe.diarizer.diarize", return_value=[]):
        q._run_job(job)

    assert job.status == FAILED
    assert job.error == "No speech detected in the uploaded audio"
    assert not upload.exists()


def test_standalone_enroll_job_missing_upload_fails_safely(tmp_path):
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    job, upload = _make_standalone_enroll_job(tmp_path)
    upload.unlink()

    q._run_job(job)

    assert job.status == FAILED
    assert job.error == "Source audio not available"


def _make_recording_enroll_job(recording_id, uid="999999999999999999"):
    from datetime import datetime as _dt
    from wisper_transcribe.web.jobs import JOB_ENROLL, Job

    return Job(
        id="recording-enroll-test",
        status="running",
        created_at=_dt.now(),
        input_path="/tmp/recordings/whatever",
        kwargs={},
        job_type=JOB_ENROLL,
        enroll_mode="recording",
        enroll_params={
            "recording_id": recording_id,
            "discord_uid": uid,
            "per_user_dir": f"/tmp/recordings/{recording_id}/per-user/{uid}",
            "profile_key": "bob",
            "display_name": "Bob",
        },
    )


def test_recording_enroll_job_updates_recording_state(tmp_path):
    """R6: the recording-state updates (unbound list, discord binding,
    campaign membership) moved from the route into the job runner."""
    from wisper_transcribe.campaign_manager import create_campaign, load_campaigns
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording
    from wisper_transcribe.web.jobs import COMPLETED, JobQueue

    campaign = create_campaign("Test Campaign", data_dir=tmp_path)
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.unbound_speakers = ["999999999999999999"]
    rec.discord_speakers["999999999999999999"] = ""
    rec.campaign_slug = campaign.slug
    save_recording(rec, tmp_path)

    q = JobQueue()
    job = _make_recording_enroll_job(rec.id)

    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.campaign_manager.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker_from_audio_dir") as mock_enroll:
        q._run_job(job)

    assert job.status == COMPLETED
    mock_enroll.assert_called_once()
    assert mock_enroll.call_args.kwargs["name"] == "bob"

    loaded = load_recordings(tmp_path)[rec.id]
    assert "999999999999999999" not in loaded.unbound_speakers
    assert loaded.discord_speakers["999999999999999999"] == "bob"

    members = load_campaigns(data_dir=tmp_path)[campaign.slug].members
    assert "bob" in members
    assert members["bob"].discord_user_id == "999999999999999999"


def test_recording_enroll_job_failure_is_generic(tmp_path):
    """R6/R13: an enroll failure sets a generic error and leaves the
    recording's speaker state untouched."""
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.unbound_speakers = ["999999999999999999"]
    save_recording(rec, tmp_path)

    q = JobQueue()
    job = _make_recording_enroll_job(rec.id)

    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker_from_audio_dir",
               side_effect=RuntimeError(f"no opus files in {tmp_path}")):
        q._run_job(job)  # must not raise

    assert job.status == FAILED
    assert job.error == "Enrollment failed"
    assert str(tmp_path) not in job.error

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.unbound_speakers == ["999999999999999999"]
