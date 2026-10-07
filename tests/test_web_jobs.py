"""Tests for the web job queue (wisper_transcribe.web.jobs)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from ._seed import seed_profile, sidecar_data


@pytest.fixture
def llm_out_dir(tmp_path, monkeypatch):
    """A tmp output root, with WISPER_DATA_DIR pointed at the same tree."""
    d = tmp_path / "output"
    d.mkdir()
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(d))
    return d


def _make_queue():
    from wisper_transcribe.web.jobs import JobQueue
    return JobQueue()


def _write_wav(path: Path) -> Path:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)
    return path


def _fake_convert(path, out_path=None):
    """Stand-in for audio_utils.convert_to_wav: writes the WAV it was asked for."""
    if out_path is None:
        return Path(path)
    return _write_wav(Path(out_path))


def _fake_encode_flac(src, dst):
    """Stand-in for audio_utils.encode_flac: creates ``dst``."""
    Path(dst).write_bytes(b"flac-from-" + Path(src).name.encode())


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
    """Two jobs submitted within the same clock tick share an equal
    `created_at` -- the tie must break by insertion order, most-recently
    -submitted first."""
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
    # Raw exception text must never reach job.error (it renders into
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
    """cancel() marks a PENDING job FAILED, but its id stays
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


@pytest.mark.anyio
async def test_server_shutdown_mid_job_records_it_interrupted_not_completed(tmp_path):
    """stop() cancels the worker while a job thread runs: the job is recorded
    failed (interrupted) and its temp upload removed."""
    import threading

    from wisper_transcribe import db
    from wisper_transcribe.job_history import INTERRUPTED
    from wisper_transcribe.web.jobs import FAILED, RUNNING

    upload = tmp_path / "wisper_upload_abc.mp3"
    upload.write_bytes(b"audio")
    started, release = threading.Event(), threading.Event()

    def _slow(*args, **kwargs):
        started.set()
        release.wait(5)
        raise RuntimeError("thread outlived the server")

    q = _make_queue()
    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_slow), \
            patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=_fake_convert):
        job = q.submit(str(upload), model_size="tiny", no_diarize=True)
        q.start()
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert job.status == RUNNING
        await q.stop()
        release.set()

    assert job.status == FAILED and job.error == INTERRUPTED
    assert job.upload_dir and not Path(job.upload_dir).exists()
    with db.connection() as conn:
        row = conn.execute("SELECT status, error_code FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert tuple(row) == ("failed", INTERRUPTED)


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

    Otherwise the SSE stream fires 'done' while the LLM is still generating
    the campaign summary.
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
# Unbounded memory growth guards
# ---------------------------------------------------------------------------


def test_append_log_caps_log_lines_and_tracks_dropped():
    """job.append_log trims the oldest lines once _MAX_LOG_LINES is
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


def test_resume_slice_normal_case_no_drops():
    """No trimming has happened yet (dropped=0) -- behaves like a plain slice."""
    from wisper_transcribe.web.jobs import resume_slice

    items = ["a", "b", "c"]
    new_items, last_idx = resume_slice(items, dropped=0, last_idx=0)
    assert new_items == ["a", "b", "c"]
    assert last_idx == 3

    new_items, last_idx = resume_slice(items, dropped=0, last_idx=last_idx)
    assert new_items == []
    assert last_idx == 3


def test_resume_slice_translates_past_dropped_prefix():
    """Once _append_capped has trimmed a prefix, an absolute last_idx from
    before the trim must be translated into the retained list's own
    indexing rather than sliced directly."""
    from wisper_transcribe.web.jobs import resume_slice

    # Absolute count says 5 produced so far; only the last 2 are retained
    # (3 were dropped from the front).
    retained = ["item3", "item4"]
    new_items, last_idx = resume_slice(retained, dropped=3, last_idx=5)
    assert new_items == []
    assert last_idx == 5


def test_resume_slice_client_fell_behind_the_cap_resumes_from_retained():
    """A client that fell more than the cap behind (last_idx below the
    dropped count) resumes from whatever's still retained instead of
    crashing or re-slicing negative indices."""
    from wisper_transcribe.web.jobs import resume_slice

    retained = ["item3", "item4"]
    new_items, last_idx = resume_slice(retained, dropped=3, last_idx=1)
    assert new_items == ["item3", "item4"]
    assert last_idx == 5


def test_prune_finished_jobs_caps_terminal_jobs_only(monkeypatch):
    """Only COMPLETED/FAILED jobs count against _MAX_RETAINED_JOBS, and
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
    """Cancelling a PENDING job (which sets it straight to FAILED
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


def test_on_error_callback_invoked_after_failure(tmp_path):
    """on_error fires exactly once when the job fails, and on_complete
    (never going to fire now) is discarded too rather than leaking."""
    from wisper_transcribe.web.jobs import Job, JobQueue, FAILED
    from datetime import datetime

    calls: list[Job] = []
    complete_calls: list[Job] = []

    q = JobQueue()
    job = Job(
        id="err-test",
        status="running",
        created_at=datetime.now(),
        input_path="/tmp/bad.mp3",
        kwargs={},
    )
    q._on_error_callbacks[job.id] = lambda j: calls.append(j)
    q._on_complete_callbacks[job.id] = lambda j: complete_calls.append(j)

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=RuntimeError("oops")):
        try:
            q._run_job(job)
        except RuntimeError:
            pass

    assert len(calls) == 1
    assert calls[0].status == FAILED
    assert complete_calls == []
    # Both callbacks are consumed -- neither leaks or fires a second time
    assert job.id not in q._on_error_callbacks
    assert job.id not in q._on_complete_callbacks


def test_on_error_callback_invoked_on_cancellation(tmp_path):
    """Cancellation (InterruptedError) is also a failure path for on_error."""
    from wisper_transcribe.web.jobs import Job, JobQueue, FAILED
    from datetime import datetime

    calls: list[Job] = []

    q = JobQueue()
    job = Job(
        id="err-cancel-test",
        status="running",
        created_at=datetime.now(),
        input_path="/tmp/bad.mp3",
        kwargs={},
    )
    job._cancel_event.set()
    q._on_error_callbacks[job.id] = lambda j: calls.append(j)

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=InterruptedError("Job cancelled by user")):
        q._run_job(job)

    assert len(calls) == 1
    assert calls[0].status == FAILED
    assert calls[0].error == "Cancelled"


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
# Durable audio: move wisper_upload_* temp files to the output dir
# ---------------------------------------------------------------------------

def _fake_process_file_with_segments(out_md, segments):
    """Build a process_file stand-in that populates _result_store like the
    real pipeline does, so job.diarization_segments is non-empty afterwards."""
    def _fake(path, _result_store=None, job_id=None, **kwargs):
        if _result_store is not None:
            _result_store["diarization_segments"] = segments
        return out_md
    return _fake


def test_submit_moves_upload_into_its_own_folder(tmp_path):
    """An upload goes into wisper_upload_<job-id>/ under its original name; the
    job keeps the web-upload flag and names the transcript after original_stem."""
    q = _make_queue()
    upload = tmp_path / "wisper_upload_abc123.mp3"
    upload.write_bytes(b"fake-audio")

    job = q.submit(str(upload), original_stem="My Session", source_name="My Session.mp3")

    assert job.is_web_upload is True
    assert job.upload_dir == str(tmp_path / f"wisper_upload_{job.id}")
    assert Path(job.input_path) == Path(job.upload_dir) / "My Session.mp3"
    assert Path(job.input_path).read_bytes() == b"fake-audio"
    assert not upload.exists()
    assert job.kwargs["output_stem"] == "My Session"
    assert job.kwargs["source_name"] == "My Session.mp3"
    assert job.name == "My Session"


def test_submit_upload_with_unusable_name_falls_back_to_upload(tmp_path):
    """If the filesystem refuses the original name, the file is stored as
    upload<suffix>; the transcript is still named after original_stem."""
    import shutil as _shutil

    q = _make_queue()
    upload = tmp_path / "wisper_upload_abc123.mp3"
    upload.write_bytes(b"fake-audio")
    real_move = _shutil.move

    def _move(src, dst):
        if Path(dst).name == "CON.mp3":
            raise OSError("reserved name")
        return real_move(src, dst)

    with patch("shutil.move", side_effect=_move):
        job = q.submit(str(upload), original_stem="CON")

    assert Path(job.input_path) == Path(job.upload_dir) / "upload.mp3"
    assert Path(job.input_path).read_bytes() == b"fake-audio"
    assert job.kwargs["output_stem"] == "CON"


def test_submit_strips_separators_from_original_stem(tmp_path):
    q = _make_queue()
    upload = tmp_path / "wisper_upload_abc123.mp3"
    upload.write_bytes(b"x")

    job = q.submit(str(upload), original_stem="..\\evil/name")

    assert job.kwargs["output_stem"] == "name"
    assert Path(job.input_path).parent == Path(job.upload_dir)


def test_submit_nonexistent_upload_gets_no_folder(tmp_path):
    q = _make_queue()
    job = q.submit(str(tmp_path / "wisper_upload_x.mp3"), original_stem="Gone")

    assert job.is_web_upload is True
    assert job.upload_dir == ""
    assert not any(p.name.startswith("wisper_upload_") for p in tmp_path.iterdir())


def test_submit_non_upload_path_not_flagged(tmp_path):
    """A durable, non-temp input (e.g. a recording) must never be flagged."""
    q = _make_queue()
    rec = tmp_path / "recording123.wav"
    rec.write_bytes(b"fake-audio")

    job = q.submit(str(rec))

    assert job.is_web_upload is False
    assert job.upload_dir == ""


def test_submit_fails_and_removes_the_upload_when_history_cannot_be_written(tmp_path):
    """A job type the busy guard reads must not be queued when its history row
    can't be written: submit raises, queues nothing, and drops the upload."""
    from wisper_transcribe import job_history

    q = _make_queue()
    upload = tmp_path / "wisper_upload_abc123.mp3"
    upload.write_bytes(b"fake-audio")
    calls = []

    def boom(job, data_dir=None):
        calls.append(job)
        raise RuntimeError("database busy")

    with patch.object(job_history, "record_required", boom):
        with pytest.raises(RuntimeError):
            q.submit(str(upload), original_stem="My Session")

    assert q.list_all() == []
    assert q.active_count() == 0
    assert not any(p.name.startswith("wisper_upload_") for p in tmp_path.iterdir())


def test_submit_llm_keeps_swallowing_a_history_failure(tmp_path):
    """A job type the busy guard ignores (an LLM rerun) still queues when its
    history write fails; only the guard's job types are required."""
    from wisper_transcribe import job_history
    from wisper_transcribe.web.jobs import JOB_REFINE

    q = _make_queue()

    def boom(job, data_dir=None):
        raise RuntimeError("database busy")

    with patch.object(job_history, "record_required", boom), \
            patch.object(job_history, "record", lambda job, data_dir=None: None):
        job = q.submit_llm(str(tmp_path / "s1.md"), JOB_REFINE)

    assert q.get(job.id) is job and q.active_count() == 1


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



# ---- upload jobs: extract, delete the upload, keep a FLAC ------------------

def _upload_env(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    out_dir = tmp_path / "output"
    tmp_dir.mkdir()
    out_dir.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out_dir))
    return tmp_dir, out_dir


def _audio_row(out_dir, stem):
    from wisper_transcribe import file_registry, transcript_store

    loc = transcript_store.locate_path(out_dir / f"{stem}.md")
    if loc is None:
        return None
    return file_registry.file_for(file_registry.Owner("transcript", loc.id), "audio",
                                  output_dir=out_dir)


def _run_upload_job(tmp_dir, out_dir, *, suffix=".mp4", stem="Session 12", diarize=True,
                    overwrite=False, process_raises=None, encode=_fake_encode_flac,
                    convert=_fake_convert, cancel_in_convert=False, wav_upload=False):
    """Submit a temp upload and run it with every ML/ffmpeg call mocked.

    Returns ``(job, seen)``; ``seen`` records what the mocks observed.
    """
    from wisper_transcribe import transcript_store
    from wisper_transcribe.models import DiarizationSegment

    upload = tmp_dir / f"wisper_upload_{stem.replace(' ', '')}{suffix}"
    upload.write_bytes(b"fake-upload")
    q = _make_queue()
    job = q.submit(str(upload), original_stem=stem, source_name=f"{stem}{suffix}",
                   output_dir=out_dir, overwrite=overwrite, no_diarize=not diarize)
    upload_path = Path(job.input_path)
    seen: dict = {"upload": upload_path}

    def _convert(path, out_path=None):
        if cancel_in_convert:
            import tqdm
            job._cancel_event.set()
            tqdm.tqdm.write("converting")
        if wav_upload and out_path is not None:
            return Path(path)  # already 16 kHz mono: used in place
        return convert(path, out_path)

    def _process(path, _result_store=None, job_id=None, **kwargs):
        seen["process_input"] = Path(path)
        seen["upload_existed"] = upload_path.exists()
        seen["kwargs"] = kwargs
        if process_raises is not None:
            raise process_raises
        md = out_dir / (kwargs["output_stem"] + ".md")
        md.write_text("# " + kwargs["output_stem"], encoding="utf-8")
        transcript_store.register(md.stem, origin="job")
        if diarize:
            _result_store["diarization_segments"] = [
                DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")]
            _result_store["speaker_map"] = {"SPEAKER_00": "Speaker 1"}
        return md

    def _excerpts(job_, output_path, **kw):
        seen["excerpt_input"] = Path(job_.input_path)
        (Path(output_path).parent / f"{Path(output_path).stem}_excerpt_SPEAKER_00.mp3").write_bytes(b"m")

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process), \
            patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=_convert), \
            patch("wisper_transcribe.audio_utils.encode_flac", side_effect=encode), \
            patch("wisper_transcribe.web.jobs._extract_speaker_excerpts", side_effect=_excerpts):
        try:
            q._run_job(job)
        except Exception:
            pass
    return job, seen


def test_mp4_upload_is_extracted_deleted_and_kept_as_flac(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    job, seen = _run_upload_job(tmp_dir, out_dir)

    assert job.status == "completed"
    assert seen["upload_existed"] is False  # gone before the pipeline runs
    assert seen["process_input"].name == "audio.wav"
    assert seen["excerpt_input"].name == "audio.wav"  # excerpts cut from the WAV
    assert seen["kwargs"]["output_stem"] == "Session 12"
    assert seen["kwargs"]["source_name"] == "Session 12.mp4"
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "Session 12.flac", "Session 12.md", "Session 12_diar.json",
        "Session 12_excerpt_SPEAKER_00.mp3",
    ]
    assert _audio_row(out_dir, "Session 12").path == out_dir / "Session 12.flac"
    assert Path(job.input_path) == out_dir / "Session 12.flac"
    assert not Path(job.upload_dir).exists()
    assert "Extracted audio; deleted the uploaded file" in "\n".join(job.log_lines)


def test_upload_without_diarization_keeps_flac(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    job, _ = _run_upload_job(tmp_dir, out_dir, diarize=False)

    assert job.status == "completed"
    assert (out_dir / "Session 12.flac").exists()
    assert _audio_row(out_dir, "Session 12").path == out_dir / "Session 12.flac"
    assert not (out_dir / "Session 12_diar.json").exists()
    assert not Path(job.upload_dir).exists()


@pytest.mark.parametrize("diarize", [True, False])
def test_encode_failure_completes_without_audio(tmp_path, monkeypatch, diarize):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    def _boom(src, dst):
        raise RuntimeError("ffmpeg exploded")

    job, _ = _run_upload_job(tmp_dir, out_dir, diarize=diarize, encode=_boom)

    assert job.status == "completed"
    assert _audio_row(out_dir, "Session 12") is None
    assert not (out_dir / "Session 12.flac").exists()
    assert any("could not save the audio copy" in line for line in job.log_lines)
    assert not Path(job.upload_dir).exists()


def test_unregistered_flac_is_untouched_without_overwrite(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    (out_dir / "Session 12.flac").write_bytes(b"users-own-file")

    job, _ = _run_upload_job(tmp_dir, out_dir)

    assert job.status == "completed"
    assert (out_dir / "Session 12.flac").read_bytes() == b"users-own-file"
    assert _audio_row(out_dir, "Session 12") is None
    assert any("already exists and belongs to something else" in line for line in job.log_lines)


def test_unregistered_flac_is_replaced_with_overwrite(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    (out_dir / "Session 12.flac").write_bytes(b"users-own-file")

    job, _ = _run_upload_job(tmp_dir, out_dir, overwrite=True)

    assert (out_dir / "Session 12.flac").read_bytes().startswith(b"flac-from-")
    assert _audio_row(out_dir, "Session 12").path == out_dir / "Session 12.flac"


def test_flac_registered_to_another_transcript_is_never_overwritten(tmp_path, monkeypatch):
    from wisper_transcribe import transcript_store

    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    other = out_dir / "Other.md"
    other.write_text("# Other", encoding="utf-8")
    transcript_store.register("Other", origin="job")
    theirs = out_dir / "Session 12.flac"
    theirs.write_bytes(b"belongs-to-other")
    transcript_store.set_audio(other, theirs)

    job, _ = _run_upload_job(tmp_dir, out_dir, overwrite=True)

    assert job.status == "completed"
    assert theirs.read_bytes() == b"belongs-to-other"
    assert _audio_row(out_dir, "Other").path == theirs
    assert _audio_row(out_dir, "Session 12") is None
    assert any("belongs to another transcript" in line for line in job.log_lines)


def test_process_file_failure_leaves_nothing(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    job, _ = _run_upload_job(tmp_dir, out_dir, process_raises=RuntimeError("boom"))

    assert job.status == "failed"
    assert not Path(job.upload_dir).exists()
    assert list(out_dir.iterdir()) == []


def test_cancel_while_running_deletes_the_upload_folder(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    job, _ = _run_upload_job(tmp_dir, out_dir, cancel_in_convert=True)

    assert job.status == "failed"
    assert job.error == "Cancelled"
    assert not Path(job.upload_dir).exists()
    assert list(out_dir.iterdir()) == []


def test_cancel_while_pending_deletes_the_upload_folder(tmp_path):
    q = _make_queue()
    upload = tmp_path / "wisper_upload_pending.mp3"
    upload.write_bytes(b"x")
    job = q.submit(str(upload), original_stem="Waiting")
    assert Path(job.upload_dir).is_dir()

    assert q.cancel(job.id) is True

    assert job.status == "failed"
    assert not Path(job.upload_dir).exists()


def test_16khz_wav_upload_is_used_in_place_and_kept_as_flac(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    job, seen = _run_upload_job(tmp_dir, out_dir, suffix=".wav", wav_upload=True)

    assert job.status == "completed"
    assert seen["upload_existed"] is True  # not deleted before the pipeline
    assert seen["process_input"].name == "Session 12.wav"
    assert (out_dir / "Session 12.flac").exists()
    assert not Path(job.upload_dir).exists()


def test_rerun_replaces_a_legacy_audio_file_with_the_flac(tmp_path, monkeypatch):
    from wisper_transcribe import transcript_store

    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    md = out_dir / "Session 12.md"
    md.write_text("# old", encoding="utf-8")
    transcript_store.register("Session 12", origin="job")
    legacy = out_dir / "Session 12.mp4"
    legacy.write_bytes(b"whole-video")
    transcript_store.set_audio(md, legacy)

    job, _ = _run_upload_job(tmp_dir, out_dir, overwrite=True)

    assert job.status == "completed"
    assert not legacy.exists()
    assert _audio_row(out_dir, "Session 12").path == out_dir / "Session 12.flac"


@pytest.mark.parametrize("diarize", [True, False])
def test_failed_encode_on_overwrite_keeps_the_old_flac(tmp_path, monkeypatch, diarize):
    from wisper_transcribe import transcript_store

    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    md = out_dir / "Session 12.md"
    md.write_text("# old", encoding="utf-8")
    transcript_store.register("Session 12", origin="job")
    old = out_dir / "Session 12.flac"
    old.write_bytes(b"old-audio")
    transcript_store.set_audio(md, old)

    def _boom(src, dst):
        raise OSError("player holds the file")

    job, _ = _run_upload_job(tmp_dir, out_dir, overwrite=True, diarize=diarize, encode=_boom)

    assert job.status == "completed"
    assert old.read_bytes() == b"old-audio"
    assert _audio_row(out_dir, "Session 12").path == old
    assert "Kept the previous audio" in "\n".join(job.log_lines)


@pytest.mark.parametrize("diarize", [True, False])
def test_failed_encode_keeps_a_legacy_mp4(tmp_path, monkeypatch, diarize):
    from wisper_transcribe import transcript_store

    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    md = out_dir / "Session 12.md"
    md.write_text("# old", encoding="utf-8")
    transcript_store.register("Session 12", origin="job")
    legacy = out_dir / "Session 12.mp4"
    legacy.write_bytes(b"whole-video")
    transcript_store.set_audio(md, legacy)

    def _boom(src, dst):
        raise OSError("player holds the file")

    job, _ = _run_upload_job(tmp_dir, out_dir, overwrite=True, diarize=diarize, encode=_boom)

    assert legacy.read_bytes() == b"whole-video"
    assert _audio_row(out_dir, "Session 12").path == legacy
    assert not (out_dir / "Session 12.flac").exists()


def test_sidecar_failure_after_encode_still_registers_the_flac(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)

    with patch("wisper_transcribe.transcript_store.write_sidecar", side_effect=RuntimeError("db")):
        job, _ = _run_upload_job(tmp_dir, out_dir)

    assert job.status == "completed"
    assert _audio_row(out_dir, "Session 12").path == out_dir / "Session 12.flac"


def test_recording_input_is_never_moved_deleted_or_registered(tmp_path, monkeypatch):
    """A recording's combined.wav is not an upload: it stays put, no FLAC is
    written, and the transcript gets no audio row of its own."""
    from wisper_transcribe import transcript_store
    from wisper_transcribe.models import DiarizationSegment

    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    combined = _write_wav(tmp_path / "recordings" / "rec1" / "combined.wav")
    q = _make_queue()
    job = q.submit(str(combined), original_stem="rec1", recording_id="rec1",
                   output_dir=out_dir, overwrite=True)
    assert Path(job.input_path) == combined and job.upload_dir == ""

    def _process(path, _result_store=None, job_id=None, **kwargs):
        md = out_dir / (kwargs["output_stem"] + ".md")
        md.write_text("# rec1", encoding="utf-8")
        transcript_store.register(md.stem, origin="job")
        _result_store["diarization_segments"] = [
            DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")]
        return md

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process), \
            patch("wisper_transcribe.audio_utils.encode_flac", side_effect=_fake_encode_flac) as enc, \
            patch("wisper_transcribe.web.jobs._extract_speaker_excerpts"):
        q._run_job(job)

    assert job.status == "completed"
    assert combined.exists() and Path(job.input_path) == combined
    enc.assert_not_called()
    assert not list(out_dir.glob("*.flac"))
    assert _audio_row(out_dir, "rec1") is None


def test_needs_extraction_is_frozen_at_submit(tmp_path, monkeypatch):
    tmp_dir, out_dir = _upload_env(tmp_path, monkeypatch)
    upload = tmp_dir / "wisper_upload_frozen.mp4"
    upload.write_bytes(b"x")
    q = _make_queue()
    job = q.submit(str(upload), original_stem="Frozen")
    assert job.needs_extraction is True  # before

    seen = []

    def _process(path, _result_store=None, job_id=None, **kwargs):
        seen.append(job.needs_extraction)  # during: input_path is a .wav
        md = out_dir / "Frozen.md"
        md.write_text("# Frozen", encoding="utf-8")
        return md

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process), \
            patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=_fake_convert), \
            patch("wisper_transcribe.audio_utils.encode_flac", side_effect=_fake_encode_flac):
        q._run_job(job)

    assert Path(job.input_path).suffix != ".mp4"
    assert seen == [True]
    assert job.needs_extraction is True  # after


def test_delete_temp_upload_only_touches_wisper_upload_folders(tmp_path):
    from wisper_transcribe.web.jobs import Job, _delete_temp_upload

    safe = tmp_path / "keepme"
    safe.mkdir()
    (safe / "f.txt").write_text("x")
    job = Job(id="j", status="running", created_at=None, input_path=str(safe / "f.txt"),
              kwargs={}, is_web_upload=True, upload_dir=str(safe))

    _delete_temp_upload(job)

    assert (safe / "f.txt").exists()


# ---------------------------------------------------------------------------
# JOB_ENROLL — Speaker-enrollment wizard's slow half as a job
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
    from ._seed import seed_sidecar
    seed_sidecar(md_path, diar)


@pytest.fixture
def out_env(tmp_path, monkeypatch):
    """Output root == tmp_path, so ``session01.md`` written there is a transcript."""
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(tmp_path))
    return tmp_path


def test_run_enroll_job_success_calls_enroll_profiles_and_completes(tmp_path, out_env):
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


def test_run_enroll_job_progress_lines_land_in_log(tmp_path, out_env):
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
                              device, data_dir=None, progress=None, **kw):
        if progress is not None:
            progress("Converting audio…")
            progress("Extracting embedding for Alice (1/1)…")

    with patch("wisper_transcribe.web.enroll_shared.enroll_profiles", side_effect=fake_enroll_profiles):
        q._run_enroll_job(job)

    assert job.status == COMPLETED
    assert "Converting audio…" in job.log_lines
    assert any("Alice" in line for line in job.log_lines)


def test_run_enroll_job_missing_audio_sets_generic_error(tmp_path, out_env):
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


def test_run_enroll_job_missing_sidecar_sets_generic_error(tmp_path, out_env):
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


def test_run_enroll_job_exception_sets_generic_error_not_path(tmp_path, out_env):
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
# Generic job error messages — no raw exception text in job.error
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
    """An exception message carrying a filesystem path must not land in
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
    """Standalone refine/summarize job failures use a generic message."""
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
    """Known input errors get short safe text, still with no path."""
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
    """The literal "Cancelled" string survives the generic-error policy
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
    """The post-processing failure line appended to job.log_lines (also
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
# Standalone / recording enroll job runners
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
    """The standalone enroll runner enrolls the primary speaker and
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
    """A failure mid-enroll sets a generic error (no exception text,
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
    """The recording-state updates (unbound list, discord binding,
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
         patch.dict("os.environ", {"WISPER_DATA_DIR": str(tmp_path)}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker_from_audio_dir",
               side_effect=lambda **kw: seed_profile(kw["name"], data_dir=tmp_path)) as mock_enroll:
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


def _run_recording_enroll_with_trim(tmp_path, trim):
    from wisper_transcribe.recording_manager import create_recording, save_recording
    from wisper_transcribe.web.jobs import JobQueue

    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.unbound_speakers = ["999999999999999999"]
    rec.discord_speakers["999999999999999999"] = ""
    save_recording(rec, tmp_path)
    job = _make_recording_enroll_job(rec.id)
    order = []
    import wisper_transcribe.recording_manager as rm
    real_bind = rm.bind_recording_speaker

    def bind(*a, **k):
        order.append("bind")
        return real_bind(*a, **k)

    def trim_spy(*a, **k):
        order.append("trim")
        return trim(*a, **k)

    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch.dict("os.environ", {"WISPER_DATA_DIR": str(tmp_path)}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker_from_audio_dir",
               side_effect=lambda **kw: seed_profile(kw["name"], data_dir=tmp_path)), \
         patch.object(rm, "bind_recording_speaker", bind), \
         patch.object(rm, "trim_recording_audio", trim_spy):
        JobQueue()._run_job(job)
    return job, rec, order


def test_recording_enroll_job_trims_after_binding(tmp_path):
    from wisper_transcribe.web.jobs import COMPLETED

    calls = []
    job, rec, order = _run_recording_enroll_with_trim(
        tmp_path, lambda rid, data_dir=None: calls.append((rid, data_dir)) or 0)

    assert job.status == COMPLETED
    assert calls == [(rec.id, tmp_path)]
    assert order == ["bind", "trim"]


def test_recording_enroll_job_survives_a_failing_trim(tmp_path):
    from wisper_transcribe.web.jobs import COMPLETED

    def boom(*a, **k):
        raise OSError("disk")

    job, _rec, order = _run_recording_enroll_with_trim(tmp_path, boom)

    assert job.status == COMPLETED
    assert order == ["bind", "trim"]


def test_recording_enroll_job_failure_is_generic(tmp_path):
    """An enroll failure sets a generic error and leaves the
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


# ---------------------------------------------------------------------------
# Live local recording: JOB_LIVE
# ---------------------------------------------------------------------------

def test_submit_live_creates_pending_job_with_ring_buffer(tmp_path):
    from wisper_transcribe.web.jobs import JOB_LIVE, JobQueue
    from wisper_transcribe.web.live_transcribe import NOISE_FLOOR_RMS, LiveRingBuffer

    q = _make_queue()
    out = tmp_path / "live_transcript.md"
    job = q.submit_live(
        recording_id="rec-123", output_path=str(out),
        model_size="tiny", device="cpu", compute_type="int8", language="en",
    )

    assert job.status == "pending"
    assert job.job_type == JOB_LIVE
    assert job.live_recording_id == "rec-123"
    assert isinstance(job.live_ring_buffer, LiveRingBuffer)
    assert job.live_output_path == str(out)
    assert job.kwargs == {
        "model_size": "tiny", "device": "cpu", "compute_type": "int8", "language": "en",
        "mic_label": "You", "noise_floor": NOISE_FLOOR_RMS,
    }
    assert job.live_lines == []


def test_submit_live_stores_custom_mic_label(tmp_path):
    q = _make_queue()
    job = q.submit_live("rec-123", str(tmp_path / "live.md"), mic_label="Brandon")
    assert job.kwargs["mic_label"] == "Brandon"


def test_find_live_job_for_recording_returns_running_job(tmp_path):
    q = _make_queue()
    job = q.submit_live("rec-123", str(tmp_path / "live.md"))
    job.status = "running"
    assert q.find_live_job_for_recording("rec-123") is job


def test_find_live_job_for_recording_ignores_other_recordings(tmp_path):
    q = _make_queue()
    job = q.submit_live("rec-123", str(tmp_path / "live.md"))
    job.status = "running"
    assert q.find_live_job_for_recording("rec-999") is None


def test_find_live_job_for_recording_ignores_terminal_jobs(tmp_path):
    from wisper_transcribe.web.jobs import COMPLETED

    q = _make_queue()
    job = q.submit_live("rec-123", str(tmp_path / "live.md"))
    job.status = COMPLETED
    assert q.find_live_job_for_recording("rec-123") is None


def test_find_live_job_for_recording_none_when_no_jobs():
    q = _make_queue()
    assert q.find_live_job_for_recording("rec-123") is None


def test_stop_live_sets_stop_event(tmp_path):
    q = _make_queue()
    job = q.submit_live("rec-123", str(tmp_path / "live.md"))
    assert not job.live_stop_event.is_set()
    q.stop_live(job.id)
    assert job.live_stop_event.is_set()


def test_stop_live_unknown_job_id_is_noop():
    q = _make_queue()
    q.stop_live("no-such-job")  # must not raise


def test_stop_all_live_sets_stop_event_on_running_and_pending_jobs(tmp_path):
    q = _make_queue()
    running = q.submit_live("rec-1", str(tmp_path / "a.md"))
    running.status = "running"
    pending = q.submit_live("rec-2", str(tmp_path / "b.md"))  # stays pending

    q.stop_all_live()

    assert running.live_stop_event.is_set()
    assert pending.live_stop_event.is_set()


def test_stop_all_live_ignores_terminal_jobs(tmp_path):
    from wisper_transcribe.web.jobs import COMPLETED

    q = _make_queue()
    job = q.submit_live("rec-1", str(tmp_path / "a.md"))
    job.status = COMPLETED

    q.stop_all_live()  # must not raise
    assert not job.live_stop_event.is_set()  # already finished -- nothing to signal


def test_stop_all_live_ignores_non_live_jobs(tmp_path):
    q = _make_queue()
    other = q.submit("/tmp/test.mp3")
    q.stop_all_live()  # must not raise or touch unrelated job types
    assert other.status == "pending"


def test_stop_all_live_noop_with_no_jobs():
    q = _make_queue()
    q.stop_all_live()  # must not raise


def test_run_live_job_calls_run_live_loop_and_completes(tmp_path):
    from wisper_transcribe.web.jobs import COMPLETED, JobQueue

    q = JobQueue()
    out = tmp_path / "live_transcript.md"
    job = q.submit_live("rec-123", str(out), model_size="tiny", device="cpu", mic_label="Brandon")

    with patch("wisper_transcribe.web.live_transcribe.run_live_loop") as mock_loop:
        q._run_live_job(job)

    mock_loop.assert_called_once()
    call_kwargs = mock_loop.call_args
    assert call_kwargs.kwargs["model_size"] == "tiny"
    assert call_kwargs.kwargs["device"] == "cpu"
    assert call_kwargs.kwargs["mic_label"] == "Brandon"
    assert job.status == COMPLETED
    assert job.finished_at is not None
    assert out.exists()  # header written up front


def test_submit_live_seeds_noise_floor_kwarg(tmp_path):
    from wisper_transcribe.web.jobs import JobQueue
    from wisper_transcribe.web.live_transcribe import NOISE_FLOOR_RMS

    q = JobQueue()
    default_job = q.submit_live("rec-1", str(tmp_path / "a.md"))
    assert default_job.kwargs["noise_floor"] == NOISE_FLOOR_RMS

    custom_job = q.submit_live("rec-2", str(tmp_path / "b.md"), noise_floor=42.0)
    assert custom_job.kwargs["noise_floor"] == 42.0


def test_set_live_noise_floor_updates_running_job_kwargs(tmp_path):
    from wisper_transcribe.web.jobs import JobQueue

    q = JobQueue()
    job = q.submit_live("rec-1", str(tmp_path / "a.md"))

    assert q.set_live_noise_floor(job.id, 300.0) is True
    assert job.kwargs["noise_floor"] == 300.0
    assert q.set_live_noise_floor("no-such-job", 999.0) is False


def test_run_live_job_wires_get_noise_floor_reading_job_kwargs_live(tmp_path):
    """The getter passed into run_live_loop must read job.kwargs fresh on
    each call -- not capture the value once at job start -- so a live
    slider update (set_live_noise_floor) takes effect on a job already
    running."""
    from wisper_transcribe.web.jobs import JobQueue

    q = JobQueue()
    job = q.submit_live("rec-123", str(tmp_path / "live_transcript.md"), noise_floor=150.0)
    captured = {}

    def fake_run_live_loop(ring_buffer, stop_event, on_line, **kwargs):
        captured["get_noise_floor"] = kwargs["get_noise_floor"]

    with patch("wisper_transcribe.web.live_transcribe.run_live_loop", side_effect=fake_run_live_loop):
        q._run_live_job(job)

    assert captured["get_noise_floor"]() == 150.0
    q.set_live_noise_floor(job.id, 500.0)
    assert captured["get_noise_floor"]() == 500.0


def test_run_live_job_on_line_appends_job_live_lines_and_markdown(tmp_path):
    from wisper_transcribe.web.jobs import JobQueue
    from wisper_transcribe.web.live_transcribe import LiveLine

    q = JobQueue()
    out = tmp_path / "live_transcript.md"
    job = q.submit_live("rec-123", str(out))

    def fake_run_live_loop(ring_buffer, stop_event, on_line, **kwargs):
        on_line(LiveLine(speaker="You", text="hello world", start_s=1.0, end_s=2.5))

    with patch("wisper_transcribe.web.live_transcribe.run_live_loop", side_effect=fake_run_live_loop):
        q._run_live_job(job)

    assert len(job.live_lines) == 1
    assert job.live_lines[0]["speaker"] == "You"
    assert job.live_lines[0]["text"] == "hello world"
    body = out.read_text(encoding="utf-8")
    assert "hello world" in body
    assert "You" in body


def test_run_live_job_exception_still_completes_not_failed(tmp_path):
    """A live session ending abnormally reads as 'session ended', not a
    raw failure -- see _run_live_job's docstring."""
    from wisper_transcribe.web.jobs import COMPLETED, JobQueue

    q = JobQueue()
    out = tmp_path / "live_transcript.md"
    job = q.submit_live("rec-123", str(out))

    with patch("wisper_transcribe.web.live_transcribe.run_live_loop", side_effect=RuntimeError("boom")):
        q._run_live_job(job)  # must not raise

    assert job.status == COMPLETED
    assert job.finished_at is not None


@pytest.mark.anyio
async def test_worker_fails_live_job_stopped_before_it_reached_front_of_queue(tmp_path):
    """JOB_LIVE shares the single-worker queue with every other job type. If a
    long-running job (e.g. a campaign journal rebuild) is already running
    when a local session starts and finishes, `stop_live()` sets
    `live_stop_event` on a JOB_LIVE job that is still PENDING -- and
    `run_live_loop`'s `while not stop_event.is_set()` would then exit on its
    first check, "completing" with zero lines processed and no error shown
    anywhere. `_worker` must instead recognize this case before ever
    calling `run_live_loop` and fail the job with an explicit reason.
    """
    from wisper_transcribe.web.jobs import FAILED, JobQueue

    q = JobQueue()
    with patch("wisper_transcribe.web.live_transcribe.run_live_loop") as mock_loop:
        job = q.submit_live("rec-123", str(tmp_path / "live.md"))
        q.stop_live(job.id)  # session ended while job still sat PENDING
        assert job.status == "pending"

        try:
            await asyncio.wait_for(q._worker(), timeout=0.2)
        except asyncio.TimeoutError:
            pass

    assert job.status == FAILED
    assert job.error
    assert "queue was busy" in job.error
    mock_loop.assert_not_called()


def test_worker_runs_live_job_normally_when_stop_event_not_yet_set(tmp_path):
    """Counterpart of the test above: a JOB_LIVE job that reaches the front
    of the queue before its session is stopped must still run normally."""
    from wisper_transcribe.web.jobs import COMPLETED, JobQueue

    q = JobQueue()
    job = q.submit_live("rec-123", str(tmp_path / "live.md"))
    assert not job.live_stop_event.is_set()

    with patch("wisper_transcribe.web.live_transcribe.run_live_loop") as mock_loop:
        q._run_job(job)

    mock_loop.assert_called_once()
    assert job.status == COMPLETED


def test_append_live_line_caps_and_tracks_dropped():
    from datetime import datetime

    from wisper_transcribe.web.jobs import Job, _MAX_LIVE_LINES

    job = Job(id="live-cap-test", status="running", created_at=datetime.now(), input_path="", kwargs={})
    total = _MAX_LIVE_LINES + 100
    for i in range(total):
        job.append_live_line({"speaker": "You", "text": f"line {i}", "start_s": i, "end_s": i + 1})

    assert len(job.live_lines) == _MAX_LIVE_LINES
    assert job.live_lines_dropped == total - _MAX_LIVE_LINES
    assert job.live_lines[0]["text"] == f"line {job.live_lines_dropped}"
    assert job.live_lines[-1]["text"] == f"line {total - 1}"


def test_append_live_line_under_cap_does_not_trim():
    from datetime import datetime

    from wisper_transcribe.web.jobs import Job

    job = Job(id="live-nocap-test", status="running", created_at=datetime.now(), input_path="", kwargs={})
    job.append_live_line({"speaker": "You", "text": "hi", "start_s": 0, "end_s": 1})
    assert len(job.live_lines) == 1
    assert job.live_lines_dropped == 0


# ---------------------------------------------------------------------------
# Forced alignment: frozen setting and the job page's Align step
# ---------------------------------------------------------------------------


def _config(**kw):
    base = {"forced_alignment": "auto", "hf_token": "hf_fake"}
    base.update(kw)
    return base


def test_submit_freezes_forced_alignment_setting():
    q = _make_queue()
    with patch("wisper_transcribe.config.load_config", return_value=_config(forced_alignment="false")):
        job = q.submit("/tmp/test.mp3")
    assert job.kwargs["forced_alignment"] == "false"


def test_submit_keeps_explicit_forced_alignment():
    q = _make_queue()
    with patch("wisper_transcribe.config.load_config", return_value=_config(forced_alignment="false")):
        job = q.submit("/tmp/test.mp3", forced_alignment="true")
    assert job.kwargs["forced_alignment"] == "true"


@pytest.mark.parametrize("kwargs,device,config,expected", [
    ({"device": "mps"}, "mps", _config(), True),
    ({"device": "cpu"}, "cpu", _config(), False),                       # auto is GPU-only
    ({"device": "auto"}, "cuda", _config(), True),                      # auto device resolved
    ({"device": "cpu", "forced_alignment": "true"}, "cpu", _config(), True),
    ({"device": "mps", "forced_alignment": "false"}, "mps", _config(), False),
    ({"device": "mps", "no_diarize": True}, "mps", _config(), False),   # no diarization
    ({"device": "mps"}, "mps", _config(hf_token=""), False),            # no token, no diarization
])
def test_will_align(monkeypatch, kwargs, device, config, expected):
    from wisper_transcribe.web.jobs import JOB_TRANSCRIPTION, Job
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    job = Job(id="j", status="pending", created_at=None, input_path="/tmp/a.mp3",
              kwargs=kwargs, name="a", job_type=JOB_TRANSCRIPTION)
    with patch("wisper_transcribe.config.load_config", return_value=config), \
         patch("wisper_transcribe.config.get_device", return_value=device):
        assert job.will_align is expected


def test_will_align_false_for_other_job_types():
    from wisper_transcribe.web.jobs import Job
    job = Job(id="j", status="pending", created_at=None, input_path="/tmp/a.md",
              kwargs={}, name="a", job_type="refine")
    assert job.will_align is False


# ---------------------------------------------------------------------------
# Transcription jobs never report success on a transcript they didn't write
# ---------------------------------------------------------------------------

def _transcription_job(tmp_path):
    from wisper_transcribe.web.jobs import JobQueue

    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    q = JobQueue()
    return q, q.submit(str(audio), output_dir=str(tmp_path))


def test_job_fails_when_transcript_already_exists(tmp_path):
    from wisper_transcribe.transcript_store import TranscriptExistsError
    from wisper_transcribe.web.jobs import FAILED

    q, job = _transcription_job(tmp_path)
    with patch("wisper_transcribe.web.jobs.process_file",
               side_effect=TranscriptExistsError("session")) as mock_pf:
        with pytest.raises(TranscriptExistsError):
            q._run_transcription_job(job)
    assert mock_pf.call_args.kwargs["skip_existing"] is False
    assert job.status == FAILED
    assert job.error == "Transcript already exists"


def test_job_fails_when_reported_transcript_is_missing(tmp_path):
    from wisper_transcribe.web.jobs import FAILED, TranscriptMissingError

    q, job = _transcription_job(tmp_path)
    with patch("wisper_transcribe.web.jobs.process_file", return_value=tmp_path / "nowhere.md"):
        with pytest.raises(TranscriptMissingError):
            q._run_transcription_job(job)
    assert job.status == FAILED
    assert job.error == "Transcript file missing after write"
    assert any("Transcripts folder:" in line for line in job.log_lines)


def test_job_target_is_recorded_at_submit(tmp_path):
    """A re-transcribe submitted through JobQueue.submit carries its
    transcript_id while still pending, so the busy guard can see it."""
    from wisper_transcribe import db, transcript_store
    from wisper_transcribe.web.jobs import JobQueue

    out = tmp_path / "out"
    out.mkdir()
    md = out / "Session 01.md"
    md.write_text("# t", encoding="utf-8")
    tid = transcript_store.register(md, origin="reconcile")

    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    q = JobQueue()
    job = q.submit(str(audio), original_stem="Session 01", output_dir=str(out), overwrite=True)
    assert job.status == "pending"
    assert job.transcript_id == tid
    # History recorded it too, so the busy guard sees a queued re-transcribe.
    with db.connection() as conn:
        row = conn.execute("SELECT transcript_id FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert row["transcript_id"] == tid


def test_job_target_is_none_for_a_new_name(tmp_path):
    from wisper_transcribe.web.jobs import JobQueue

    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    q = JobQueue()
    job = q.submit(str(audio), original_stem="brand-new", output_dir=str(tmp_path))
    assert job.transcript_id is None


def test_missing_transcript_after_write_names_a_campaign_folder(tmp_path):
    from wisper_transcribe.web.jobs import FAILED, JobQueue, TranscriptMissingError

    folder = tmp_path / "Game"
    folder.mkdir()
    audio = tmp_path / "session.mp3"
    audio.write_bytes(b"fake")
    q = JobQueue()
    job = q.submit(str(audio), original_stem="session", output_dir=str(folder))
    with patch("wisper_transcribe.web.jobs.process_file", return_value=folder / "nowhere.md"):
        with pytest.raises(TranscriptMissingError):
            q._run_transcription_job(job)
    assert job.status == FAILED
    assert job.error == "Transcript file missing after write"
    assert any(str(folder) in line for line in job.log_lines)


def test_recording_is_transcribing_only_while_its_job_is_pending_or_running(tmp_path):
    """A cancelled pending job never runs its callbacks; the recording must
    still come back as transcribable (status is derived from the queue)."""
    from wisper_transcribe.recording_manager import load_recordings
    from wisper_transcribe.web.jobs import JobQueue

    from ._seed import seed_recording

    rec = seed_recording()
    q = JobQueue()
    job = q.submit(str(rec.combined_path), original_stem=rec.id, recording_id=rec.id,
                   output_dir=str(tmp_path))
    loaded = load_recordings()[rec.id]
    assert loaded.status == "transcribing" and loaded.job_id == job.id

    q.cancel(job.id)
    loaded = load_recordings()[rec.id]
    assert loaded.status == "completed"
    assert loaded.job_id == job.id


def _seed_stored(tmp_path, vectors, *, audio=False, space=None):
    from wisper_transcribe.config import EMBEDDING_SPACE
    from ._seed import seed_sidecar

    md_path = tmp_path / "session01.md"
    md_path.write_text("# Session 01", encoding="utf-8")
    audio_path = tmp_path / "gone" / "session01.mp3"
    if audio:
        audio_path.parent.mkdir()
        audio_path.write_bytes(b"fake")
    seed_sidecar(md_path, {
        "input_path": str(audio_path),
        "diarization_segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 6.0, "end": 9.0, "speaker": "SPEAKER_01"},
        ],
        "speaker_map": {},
        "speaker_embeddings": {k: list(v) for k, v in vectors.items()},
        "embedding_space": space or EMBEDDING_SPACE,
    })
    return md_path


def test_run_enroll_job_stored_embeddings_without_audio_completes(tmp_path, out_env):
    from wisper_transcribe.web.jobs import JobQueue, COMPLETED

    md_path = _seed_stored(tmp_path, {"SPEAKER_00": [1.0, 0.0, 0.0]})
    q = JobQueue()
    job = q.submit_enroll(md_path=str(md_path), transcript_name="session01",
                          groups={"Alice": ["SPEAKER_00"]}, device="cpu")
    with patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=AssertionError):
        q._run_enroll_job(job)

    assert job.status == COMPLETED
    from wisper_transcribe.speaker_manager import load_profiles
    assert "alice" in load_profiles()


def test_run_enroll_job_partial_logs_skipped_name_and_completes(tmp_path, out_env):
    from wisper_transcribe.web.jobs import JobQueue, COMPLETED

    md_path = _seed_stored(tmp_path, {"SPEAKER_00": [1.0, 0.0, 0.0]})
    q = JobQueue()
    job = q.submit_enroll(md_path=str(md_path), transcript_name="session01",
                          groups={"Alice": ["SPEAKER_00"], "Brad": ["SPEAKER_01"]}, device="cpu")
    q._run_enroll_job(job)

    assert job.status == COMPLETED
    assert any("Skipped Brad" in line for line in job.log_lines)
    from wisper_transcribe.speaker_manager import load_profiles
    profiles = load_profiles()
    assert "alice" in profiles and "brad" not in profiles


def test_run_enroll_job_old_embedding_space_without_audio_fails(tmp_path, out_env):
    from wisper_transcribe.web.jobs import JobQueue, FAILED

    md_path = _seed_stored(tmp_path, {"SPEAKER_00": [1.0, 0.0, 0.0]}, space="old-model")
    q = JobQueue()
    job = q.submit_enroll(md_path=str(md_path), transcript_name="session01",
                          groups={"Alice": ["SPEAKER_00"]}, device="cpu")
    q._run_enroll_job(job)

    assert job.status == FAILED
    assert job.error == "Source audio not available"


def test_run_enroll_job_old_embedding_space_with_audio_extracts(tmp_path, out_env):
    import numpy as np
    from wisper_transcribe.web.jobs import JobQueue, COMPLETED

    md_path = _seed_stored(tmp_path, {"SPEAKER_00": [1.0, 0.0, 0.0]}, audio=True, space="old-model")
    q = JobQueue()
    job = q.submit_enroll(md_path=str(md_path), transcript_name="session01",
                          groups={"Alice": ["SPEAKER_00"]}, device="cpu")
    with patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p), \
         patch("wisper_transcribe.speaker_manager.extract_embedding",
               return_value=np.array([0.0, 1.0, 0.0])) as ext, \
         patch("wisper_transcribe.speaker_manager._save_reference_clip"):
        q._run_enroll_job(job)

    assert job.status == COMPLETED
    ext.assert_called_once()


def test_rerun_from_the_kept_flac_leaves_the_audio_alone(tmp_path, monkeypatch):
    from wisper_transcribe import transcript_store
    from wisper_transcribe.models import DiarizationSegment

    _, out_dir = _upload_env(tmp_path, monkeypatch)
    md = out_dir / "s1.md"
    md.write_text("# old", encoding="utf-8")
    transcript_store.register("s1", origin="job")
    flac = out_dir / "s1.flac"
    flac.write_bytes(b"original-flac")
    transcript_store.set_audio(md, flac)

    q = _make_queue()
    job = q.submit(str(flac), original_stem="s1", output_dir=str(out_dir), overwrite=True,
                   source_name="Session 1.mp4")
    assert job.is_web_upload is False and job.upload_dir == ""

    def _process(path, _result_store=None, job_id=None, **kwargs):
        md.write_text("# new", encoding="utf-8")
        transcript_store.register("s1", origin="job")
        _result_store["diarization_segments"] = [
            DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")]
        _result_store["speaker_map"] = {"SPEAKER_00": "Speaker 1"}
        return md

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process), \
            patch("wisper_transcribe.audio_utils.encode_flac") as encode, \
            patch("wisper_transcribe.web.jobs._extract_speaker_excerpts"):
        q._run_job(job)

    assert job.status == "completed"
    encode.assert_not_called()
    assert flac.read_bytes() == b"original-flac"
    assert _audio_row(out_dir, "s1").path == flac
    assert sorted(p.name for p in out_dir.glob("*.flac")) == ["s1.flac"]
    assert md.read_text(encoding="utf-8") == "# new"


# ---------------------------------------------------------------------------
# LLM job cancellation (Stop) and generic journal errors
# ---------------------------------------------------------------------------

class _BlockingClient:
    """Stand-in LLM client that blocks until the scoped cancel event is set.

    Mimics a streaming client that only notices Stop through the thread-local
    cancel scope, then raises ``InterruptedError`` like the real one.
    """

    provider = "fake"
    model = "fake-model"

    def __init__(self, started: "threading.Event | None" = None) -> None:
        import threading as _threading
        self._started = started if started is not None else _threading.Event()

    def _block(self):
        from wisper_transcribe.llm.cancel import current_cancel_event
        event = current_cancel_event()
        assert event is not None, "LLM call must run inside a cancel_scope"
        self._started.set()
        while not event.is_set():
            import time
            time.sleep(0.01)
        raise InterruptedError("Job cancelled by user")

    def complete(self, system, user):
        return self._block()

    def complete_json(self, system, user, schema):
        return self._block()


def _journal_env(tmp_path, out_dir, monkeypatch):
    """A campaign with one summary ready to fold in."""
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("My Game", data_dir=tmp_path)
    from . import _seed
    _seed.seed_transcript("s1", campaign="my-game", write_md=True, data_dir=tmp_path)
    (out_dir / "s1.summary.md").write_text("A session happened.", encoding="utf-8")


async def _run_until_cancelled(q, job):
    """Start the queue, wait for the job to run, cancel it, await its finish."""
    q.start()
    while job.status != "running":
        await asyncio.sleep(0.01)
    assert q.cancel(job.id) is True
    while job.finished_at is None:
        await asyncio.sleep(0.01)
    await q.stop()


@pytest.mark.anyio
async def test_cancel_running_journal_job_ends_cancelled(tmp_path, llm_out_dir, monkeypatch):
    import wisper_transcribe.llm as llm_mod
    from wisper_transcribe.web.jobs import FAILED

    _journal_env(tmp_path, llm_out_dir, monkeypatch)
    client = _BlockingClient()
    q = _make_queue()
    job = q.submit_journal("my-game")
    with patch.object(llm_mod, "get_client", lambda *a, **k: client):
        await _run_until_cancelled(q, job)

    assert client._started.is_set()
    assert job.status == FAILED
    assert job.error == "Cancelled"


@pytest.mark.anyio
async def test_cancel_running_digest_job_ends_cancelled(tmp_path, llm_out_dir, monkeypatch):
    import wisper_transcribe.llm as llm_mod
    from wisper_transcribe.web.jobs import FAILED

    _journal_env(tmp_path, llm_out_dir, monkeypatch)
    client = _BlockingClient()
    q = _make_queue()
    job = q.submit_campaign_summary("my-game")
    with patch.object(llm_mod, "get_client", lambda *a, **k: client):
        await _run_until_cancelled(q, job)

    assert client._started.is_set()
    assert job.status == FAILED
    assert job.error == "Cancelled"


@pytest.mark.anyio
async def test_cancel_running_summarize_job_ends_cancelled(tmp_path, llm_out_dir, monkeypatch):
    import wisper_transcribe.llm as llm_mod
    from wisper_transcribe.web.jobs import FAILED, JOB_SUMMARIZE

    md = tmp_path / "s1.md"
    md.write_text("# Session\n\nBody.", encoding="utf-8")
    client = _BlockingClient()
    q = _make_queue()
    job = q.submit_llm(str(md), JOB_SUMMARIZE)
    with patch.object(llm_mod, "get_client", lambda *a, **k: client):
        await _run_until_cancelled(q, job)

    assert client._started.is_set()
    assert job.status == FAILED
    assert job.error == "Cancelled"


@pytest.mark.anyio
async def test_stop_with_running_journal_job_ends_it_within_two_seconds(
        tmp_path, llm_out_dir, monkeypatch):
    """Ctrl+C (stop()) with a journal job blocked in an LLM call: the
    CancelledError branch sets the job's cancel event, so its worker thread
    returns on its own instead of leaving the interpreter to join a thread
    stuck on an open stream."""
    import threading
    import time

    import wisper_transcribe.llm as llm_mod
    from wisper_transcribe.job_history import INTERRUPTED
    from wisper_transcribe.web.jobs import FAILED

    _journal_env(tmp_path, llm_out_dir, monkeypatch)
    started = threading.Event()
    client = _BlockingClient(started)
    q = _make_queue()
    job = q.submit_journal("my-game")
    with patch.object(llm_mod, "get_client", lambda *a, **k: client), \
            patch.object(q, "_ml_worker") as ml_worker:
        q.start()
        deadline = time.monotonic() + 2
        while not started.is_set():
            assert time.monotonic() < deadline, "job never reached the blocking LLM call"
            await asyncio.sleep(0.01)

        t0 = time.monotonic()
        await asyncio.wait_for(q.stop(), timeout=2.0)
        await asyncio.wait_for(
            _wait_finished(job), timeout=max(0.0, 2.0 - (time.monotonic() - t0))
        )

    assert time.monotonic() - t0 < 2.0
    assert job.status == FAILED
    # Shutdown sets INTERRUPTED before cancelling; the runner keeps it.
    assert job.error == INTERRUPTED
    assert ml_worker.stop.called


async def _wait_finished(job):
    while job.finished_at is None:
        await asyncio.sleep(0.01)


def test_journal_job_error_is_generic(tmp_path, llm_out_dir, monkeypatch):
    """A journal runner failure shows the generic message, never exception text."""
    from wisper_transcribe.web.jobs import FAILED

    _journal_env(tmp_path, llm_out_dir, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError(f"cannot open {tmp_path}/secret.bin")

    q = _make_queue()
    job = q.submit_journal("my-game")
    import wisper_transcribe.llm as llm_mod
    with patch.object(llm_mod, "get_client", _boom):
        with pytest.raises(RuntimeError):
            q._run_journal_job(job)

    assert job.status == FAILED
    assert job.error == "Journal update failed — see server logs"
    assert str(tmp_path) not in job.error


def test_cancel_during_post_process_keeps_the_transcription_job(tmp_path):
    """A Stop during chained refine/summarize ends only the LLM step: the
    transcript is already saved, so the job completes and its on-complete
    callback (the recording hand-off) still runs."""
    from datetime import datetime

    from wisper_transcribe.web.jobs import COMPLETED, Job, JobQueue

    out_md = tmp_path / "out.md"
    out_md.write_text("# Session", encoding="utf-8")

    q = JobQueue()
    job = Job(
        id="pp-cancel",
        status="running",
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={"no_diarize": True, "device": "cpu"},
        post_summarize=True,
    )
    completed: list[str] = []
    q._on_complete_callbacks[job.id] = lambda j: completed.append(j.id)

    with patch("wisper_transcribe.web.jobs.process_file", return_value=out_md), \
            patch.object(JobQueue, "_do_llm_work",
                         side_effect=InterruptedError("Job cancelled by user")):
        q._run_job(job)

    assert job.status == COMPLETED
    assert not job.error
    assert "Post-processing cancelled" in job.log_lines
    assert completed == ["pp-cancel"]


