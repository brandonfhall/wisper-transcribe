"""Tests for Phase 2 (live local recording) route wiring: JOB_LIVE
submission/teardown on local session start/stop, and the
GET /recordings/{id}/live SSE stream.

`_start_live_transcription`/`_stop_live_transcription` are tested directly
against a never-started `JobQueue()` (its background worker is only
spawned by `.start()`, which nothing here calls) -- no job this file
creates is ever picked up and run for real, so there's no risk of a test
trying to load an actual Whisper model. The SSE route tests inject a
`Job` directly into the real app's `job_queue._jobs` dict (bypassing
`submit_live`/the asyncio queue entirely) for the same reason.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from wisper_transcribe.web.jobs import JOB_LIVE, RUNNING, Job, JobQueue
from wisper_transcribe.web.live_transcribe import LiveRingBuffer
from wisper_transcribe.web.routes.record import _start_live_transcription, _stop_live_transcription


# ---------------------------------------------------------------------------
# _start_live_transcription / _stop_live_transcription (direct, no HTTP)
# ---------------------------------------------------------------------------

class _FakeAppState:
    def __init__(self, job_queue):
        self.job_queue = job_queue


class _FakeApp:
    def __init__(self, job_queue):
        self.state = _FakeAppState(job_queue)


class _FakeRequest:
    def __init__(self, job_queue):
        self.app = _FakeApp(job_queue)


class _FakeLocalCaptureManager:
    def __init__(self):
        self.live_sink = None

    def set_live_sink(self, sink):
        self.live_sink = sink


def _fake_recording(id_="rec-123"):
    rec = MagicMock()
    rec.id = id_
    return rec


def test_start_live_transcription_submits_job_and_wires_sink(tmp_path):
    queue = JobQueue()  # never .start()ed -- nothing drains its asyncio.Queue
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    recording = _fake_recording()

    fake_cfg = {"model": "tiny", "device": "cpu", "compute_type": "int8", "language": "en"}
    with patch("wisper_transcribe.web.routes.record.load_config", return_value=fake_cfg):
        _start_live_transcription(request, lcm, recording, tmp_path)

    job = queue.find_live_job_for_recording("rec-123")
    assert job is not None
    assert job.kwargs == {
        "model_size": "tiny", "device": "cpu", "compute_type": "int8", "language": "en",
    }
    assert job.live_output_path == str(tmp_path / "recordings" / "rec-123" / "live_transcript.md")
    assert lcm.live_sink == job.live_ring_buffer.push


def test_start_live_transcription_failure_is_swallowed(tmp_path):
    """Best-effort -- must never fail the already-started capture session."""
    queue = JobQueue()
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    recording = _fake_recording()

    with patch("wisper_transcribe.web.routes.record.load_config", side_effect=RuntimeError("boom")):
        _start_live_transcription(request, lcm, recording, tmp_path)  # must not raise

    assert lcm.live_sink is None
    assert queue.find_live_job_for_recording("rec-123") is None


def test_stop_live_transcription_clears_sink_and_signals_job(tmp_path):
    queue = JobQueue()
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    recording = _fake_recording()

    with patch("wisper_transcribe.web.routes.record.load_config", return_value={}):
        _start_live_transcription(request, lcm, recording, tmp_path)
    job = queue.find_live_job_for_recording("rec-123")
    assert not job.live_stop_event.is_set()

    _stop_live_transcription(request, lcm, "rec-123")

    assert lcm.live_sink is None
    assert job.live_stop_event.is_set()


def test_stop_live_transcription_noop_when_no_job(tmp_path):
    queue = JobQueue()
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    _stop_live_transcription(request, lcm, "no-such-recording")  # must not raise
    assert lcm.live_sink is None


# ---------------------------------------------------------------------------
# GET /recordings/{id}/live -- SSE
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.record.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app) as c:
            yield c, tmp_path


def _make_live_job(recording_id: str, lines: list) -> Job:
    job = Job(
        id="job-1", status=RUNNING, created_at=datetime.now(), input_path="", kwargs={},
        job_type=JOB_LIVE, live_recording_id=recording_id, live_ring_buffer=LiveRingBuffer(),
    )
    job.live_lines = lines
    return job


def test_recording_live_streams_committed_lines_then_ends(client):
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("", "", data_dir=tmp_path, source="local")
    job = _make_live_job(rec.id, [{"speaker": "You", "text": "hi there", "start_s": 0.0, "end_s": 1.0}])

    queue = c.app.state.job_queue
    with patch.object(queue, "find_live_job_for_recording", side_effect=[job, None]):
        with c.stream("GET", f"/recordings/{rec.id}/live") as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

    assert "hi there" in body
    assert '"speaker": "You"' in body
    assert "event: end" in body


def test_recording_live_translates_dropped_line_index(client):
    """job.live_lines_dropped (trimmed by append_live_line's _MAX_LIVE_LINES
    cap) doesn't break the slice math -- mirrors R14's log-line coverage."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("", "", data_dir=tmp_path, source="local")
    job = _make_live_job(rec.id, [{"speaker": "Other", "text": "later line", "start_s": 5.0, "end_s": 6.0}])
    job.live_lines_dropped = 3

    queue = c.app.state.job_queue
    with patch.object(queue, "find_live_job_for_recording", side_effect=[job, None]):
        with c.stream("GET", f"/recordings/{rec.id}/live") as resp:
            body = "".join(resp.iter_text())

    assert "later line" in body
    assert "event: end" in body


def test_recording_live_snapshot_from_disk_when_no_active_job(client):
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("", "", data_dir=tmp_path, source="local")
    live_dir = tmp_path / "recordings" / rec.id
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "live_transcript.md").write_text(
        "# Live transcript\n\n**You** *(1.0s)*: hello from disk\n\n", encoding="utf-8"
    )

    with c.stream("GET", f"/recordings/{rec.id}/live") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())

    assert "hello from disk" in body
    assert "event: end" in body


def test_recording_live_no_job_no_file_ends_with_no_snapshot(client):
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("", "", data_dir=tmp_path, source="local")

    with c.stream("GET", f"/recordings/{rec.id}/live") as resp:
        body = "".join(resp.iter_text())

    assert "event: end" in body
    assert '"type": "snapshot"' not in body
