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

import asyncio
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from wisper_transcribe.web.jobs import JOB_LIVE, RUNNING, Job, JobQueue
from wisper_transcribe.web.live_transcribe import NOISE_FLOOR_RMS, LiveRingBuffer
from wisper_transcribe.web.routes.record import (
    _start_live_transcription,
    _stop_live_transcription,
    record_sse,
)


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
        "mic_label": "You", "noise_floor": NOISE_FLOOR_RMS,
    }
    assert job.live_output_path == str(tmp_path / "recordings" / "rec-123" / "live_transcript.md")
    assert lcm.live_sink == job.live_ring_buffer.push


def test_start_live_transcription_resolves_profile_to_display_name(tmp_path):
    """Phase 3 'this is me': a valid mic_profile_key resolves to the
    enrolled profile's display_name for mic-dominant live lines."""
    from wisper_transcribe.models import SpeakerProfile

    queue = JobQueue()
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    recording = _fake_recording()

    fake_profile = SpeakerProfile(
        name="brandon", display_name="Brandon", role="player",
        embedding_path=tmp_path / "brandon.npy", enrolled_date="2026-01-01",
        enrollment_source="test",
    )
    with patch("wisper_transcribe.web.routes.record.load_config", return_value={}), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={"brandon": fake_profile}):
        _start_live_transcription(request, lcm, recording, tmp_path, mic_profile_key="brandon")

    job = queue.find_live_job_for_recording("rec-123")
    assert job.kwargs["mic_label"] == "Brandon"


def test_start_live_transcription_unknown_profile_key_falls_back_to_you(tmp_path):
    queue = JobQueue()
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    recording = _fake_recording()

    with patch("wisper_transcribe.web.routes.record.load_config", return_value={}), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        _start_live_transcription(request, lcm, recording, tmp_path, mic_profile_key="no-such-profile")

    job = queue.find_live_job_for_recording("rec-123")
    assert job.kwargs["mic_label"] == "You"


def test_start_live_transcription_blank_profile_key_skips_lookup(tmp_path):
    queue = JobQueue()
    request = _FakeRequest(queue)
    lcm = _FakeLocalCaptureManager()
    recording = _fake_recording()

    with patch("wisper_transcribe.web.routes.record.load_config", return_value={}), \
         patch("wisper_transcribe.speaker_manager.load_profiles") as mock_load:
        _start_live_transcription(request, lcm, recording, tmp_path, mic_profile_key="")

    mock_load.assert_not_called()
    job = queue.find_live_job_for_recording("rec-123")
    assert job.kwargs["mic_label"] == "You"


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


def test_recording_live_catches_final_lines_committed_right_before_completion(client):
    """A line committed in the same ~1s window as the job completing must
    not be silently dropped -- find_live_job_for_recording only matches
    PENDING/RUNNING, so it flips to None the instant the job finishes."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("", "", data_dir=tmp_path, source="local")
    job = _make_live_job(rec.id, [{"speaker": "You", "text": "first line", "start_s": 0.0, "end_s": 1.0}])

    call_count = {"n": 0}

    def fake_find(recording_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return job
        # Simulate the job committing one more line and completing in the
        # gap between this poll and the previous one.
        job.live_lines.append({"speaker": "You", "text": "final line", "start_s": 1.0, "end_s": 2.0})
        return None

    queue = c.app.state.job_queue
    with patch.object(queue, "find_live_job_for_recording", side_effect=fake_find):
        with c.stream("GET", f"/recordings/{rec.id}/live") as resp:
            body = "".join(resp.iter_text())

    assert "first line" in body
    assert "final line" in body
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


# ---------------------------------------------------------------------------
# App lifespan shutdown must stop a live job's thread (not just cancel the
# asyncio task awaiting it) -- see JobQueue.stop_all_live()'s docstring.
# ---------------------------------------------------------------------------

def test_lifespan_shutdown_signals_active_live_job(tmp_path):
    """Owns its own TestClient context (rather than the shared `client`
    fixture) so the test controls exactly when lifespan shutdown runs."""
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.record.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.web.app import create_app
        test_app = create_app()

        job = _make_live_job("rec-shutdown-test", [])
        job.status = RUNNING

        with TestClient(test_app) as c:
            c.app.state.job_queue._jobs[job.id] = job
            assert not job.live_stop_event.is_set()
        # Exiting the `with` block above runs lifespan shutdown.

    assert job.live_stop_event.is_set()


# ---------------------------------------------------------------------------
# The generic dashboard "Stop job" button (POST /transcribe/jobs/{id}/cancel)
# must properly tear down a JOB_LIVE session, not just no-op via the normal
# _cancel_event mechanism (which run_live_loop never checks).
# ---------------------------------------------------------------------------

def test_generic_cancel_route_stops_live_job_and_clears_sink(client):
    c, data_dir = client
    job = _make_live_job("rec-cancel-test", [])
    job.status = RUNNING
    c.app.state.job_queue._jobs[job.id] = job
    c.app.state.local_capture_manager.set_live_sink(job.live_ring_buffer.push)

    resp = c.post(f"/transcribe/jobs/{job.id}/cancel", follow_redirects=False)

    assert resp.status_code == 303
    assert job.live_stop_event.is_set()
    assert c.app.state.local_capture_manager._live_sink is None
    # Not routed through the generic cancel path -- status stays whatever
    # run_live_loop itself would set (it isn't running here), not FAILED.
    assert job.status == RUNNING


def test_generic_cancel_route_stops_pending_live_job(client):
    """A still-queued (PENDING) live job also gets torn down -- run_live_loop
    checks live_stop_event as its very first loop condition, so setting it
    ahead of time makes the job complete immediately once it's dequeued."""
    c, data_dir = client
    job = _make_live_job("rec-cancel-pending", [])
    job.status = "pending"
    c.app.state.job_queue._jobs[job.id] = job

    resp = c.post(f"/transcribe/jobs/{job.id}/cancel", follow_redirects=False)

    assert resp.status_code == 303
    assert job.live_stop_event.is_set()


def test_generic_cancel_route_unaffected_for_non_live_jobs(client):
    """Non-JOB_LIVE cancellation is untouched by this change."""
    from datetime import datetime

    from wisper_transcribe.web.jobs import PENDING, Job

    c, data_dir = client
    job = Job(id="txn-job", status=PENDING, created_at=datetime.now(), input_path="/tmp/a.mp3", kwargs={})
    c.app.state.job_queue._jobs[job.id] = job

    resp = c.post(f"/transcribe/jobs/{job.id}/cancel", follow_redirects=False)

    assert resp.status_code == 303
    assert job.status == "failed"
    assert job.error == "Cancelled"


# ---------------------------------------------------------------------------
# POST /api/record/live-noise-floor -- live slider updates a running
# JOB_LIVE job's noise floor without restarting the session.
# ---------------------------------------------------------------------------

class _FakeLCMWithActiveRecording:
    def __init__(self, recording):
        self.active_recording = recording


def test_live_noise_floor_no_active_session_returns_400(client):
    c, _ = client
    c.app.state.local_capture_manager = _FakeLCMWithActiveRecording(None)

    resp = c.post("/api/record/live-noise-floor", json={"noise_floor": 200})
    assert resp.status_code == 400


def test_live_noise_floor_invalid_value_returns_400(client):
    c, data_dir = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("", "", data_dir=data_dir, source="local")
    c.app.state.local_capture_manager = _FakeLCMWithActiveRecording(rec)

    resp = c.post("/api/record/live-noise-floor", json={"noise_floor": "not-a-number"})
    assert resp.status_code == 400

    resp = c.post("/api/record/live-noise-floor", json={"noise_floor": -5})
    assert resp.status_code == 400

    resp = c.post("/api/record/live-noise-floor", json={"noise_floor": 999999})
    assert resp.status_code == 400


def test_live_noise_floor_no_active_job_returns_404(client):
    c, data_dir = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("", "", data_dir=data_dir, source="local")
    c.app.state.local_capture_manager = _FakeLCMWithActiveRecording(rec)

    resp = c.post("/api/record/live-noise-floor", json={"noise_floor": 200})
    assert resp.status_code == 404


def test_live_noise_floor_updates_running_job(client):
    c, data_dir = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("", "", data_dir=data_dir, source="local")
    c.app.state.local_capture_manager = _FakeLCMWithActiveRecording(rec)

    job = _make_live_job(rec.id, [])
    c.app.state.job_queue._jobs[job.id] = job

    resp = c.post("/api/record/live-noise-floor", json={"noise_floor": 275})
    assert resp.status_code == 200
    assert resp.json()["noise_floor"] == 275
    assert job.kwargs["noise_floor"] == 275


# ---------------------------------------------------------------------------
# GET /record/sse -- level-gauge fields for a local session.
#
# `/record/sse` polls forever (`while True: ... await asyncio.sleep(1.0)`),
# so it's exercised by pulling exactly one chunk off its StreamingResponse's
# body_iterator directly (asyncio.run) rather than over a live TestClient
# HTTP stream, which has no clean way to read "just the first event" without
# risking a hang (see test_current_active_recording_reports_active_local_session
# in test_record_routes.py for the same convention).
# ---------------------------------------------------------------------------

class _FakeSSERequest:
    def __init__(self, app):
        self.app = app

    async def is_disconnected(self):
        return False


def _first_sse_payload(app) -> dict:
    async def _get():
        resp = await record_sse(_FakeSSERequest(app))
        chunk = await resp.body_iterator.__anext__()
        return chunk

    chunk = asyncio.run(_get())
    return json.loads(chunk.split("data: ", 1)[1].strip())


def test_record_sse_includes_level_gauge_for_local_session(client):
    c, data_dir = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("", "", data_dir=data_dir, source="local")
    lcm = _FakeLCMWithActiveRecording(rec)
    lcm.get_and_reset_levels = lambda: {"mic": 42.0, "system": 7.0}
    c.app.state.local_capture_manager = lcm

    payload = _first_sse_payload(c.app)
    assert payload["mic_rms"] == 42.0
    assert payload["system_rms"] == 7.0


def test_record_sse_idle_status_omits_level_gauge_fields(client):
    c, _ = client
    c.app.state.local_capture_manager = _FakeLCMWithActiveRecording(None)

    payload = _first_sse_payload(c.app)
    assert payload["status"] == "idle"
    assert "mic_rms" not in payload
