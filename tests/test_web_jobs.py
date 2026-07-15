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
