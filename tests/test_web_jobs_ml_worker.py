"""JobQueue delegation tests for the warm ML worker (Phase 2).

Uses a fake MLWorker object, never a real child, so the suite stays
GPU-free and fast.  The autouse ``_no_ml_worker`` fixture turns delegation
off; each test here patches ``jobs._ml_worker_enabled`` True and swaps in
the fake.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest


class FakeMLWorker:
    """Stand-in for MLWorker; records calls and can block until cancel.

    ``block`` makes every ``call`` wait for ``cancel_event`` and then raise
    ``InterruptedError``, mimicking a long GPU call interrupted by Stop.
    """

    def __init__(self, block: bool = False) -> None:
        self.calls: list[tuple] = []
        self.stopped = False
        self.block = block
        self.started = False

    def call(self, name, *args, cancel_event=None, on_log=None, on_bar=None, **kwargs):
        self.started = True
        self.calls.append((name, args, kwargs))
        if on_bar is not None:
            on_bar("fake bar")
        if self.block:
            while not (cancel_event is not None and cancel_event.is_set()):
                time.sleep(0.01)
            raise InterruptedError("Job cancelled by user")
        if name == "transcribe":
            return []
        if name == "diarize":
            return []
        return None

    def stop(self) -> None:
        self.stopped = True


def _make_queue_with(fake: FakeMLWorker):
    from wisper_transcribe.web.jobs import JobQueue
    q = JobQueue()
    q._ml_worker = fake
    return q


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


@pytest.fixture()
def _enabled():
    with patch("wisper_transcribe.web.jobs._ml_worker_enabled", return_value=True):
        yield


def test_transcription_job_delegates_when_on(tmp_path, _enabled):
    """With ml_worker on, process_file runs in an active delegation."""
    from wisper_transcribe import ml_worker

    out_md = tmp_path / "out.md"
    out_md.write_text("# x", encoding="utf-8")
    seen = {}

    def _process(path, _result_store=None, job_id=None, **kwargs):
        seen["active"] = ml_worker.active()
        return out_md

    fake = FakeMLWorker()
    q = _make_queue_with(fake)
    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process):
        job = q.submit("/tmp/a.mp3", no_diarize=True, output_dir=str(tmp_path))
        q._run_job(job)

    assert seen["active"] is not None
    assert seen["active"].worker is fake
    assert job.status == "completed"


def test_transcription_job_does_not_delegate_when_off(tmp_path):
    """With ml_worker off, process_file sees no active delegation."""
    from wisper_transcribe import ml_worker

    out_md = tmp_path / "out.md"
    out_md.write_text("# x", encoding="utf-8")
    seen = {}

    def _process(path, _result_store=None, job_id=None, **kwargs):
        seen["active"] = ml_worker.active()
        return out_md

    with patch("wisper_transcribe.web.jobs._ml_worker_enabled", return_value=False):
        q = _make_queue_with(FakeMLWorker())
        with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process):
            job = q.submit("/tmp/a.mp3", no_diarize=True, output_dir=str(tmp_path))
            q._run_job(job)

    assert seen["active"] is None
    assert job.status == "completed"


@pytest.mark.anyio
async def test_cancel_mid_call_ends_job_cancelled_then_queue_usable(tmp_path, _enabled):
    """A GPU call blocked in the worker is killed on cancel: the job fails as
    Cancelled, its upload is deleted, and the queue runs the next job."""
    from wisper_transcribe.web.jobs import COMPLETED, FAILED

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    upload = tmp_dir / "wisper_upload_x.mp3"
    upload.write_bytes(b"audio")

    fake = FakeMLWorker(block=True)

    def _blocking_process(path, _result_store=None, job_id=None, **kwargs):
        from wisper_transcribe import ml_worker
        token = ml_worker.active()
        token.worker.call("transcribe", path, cancel_event=token.cancel_event)
        return tmp_path / "never.md"

    q = _make_queue_with(fake)
    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_blocking_process), \
            patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=_fake_convert):
        job = q.submit(str(upload), no_diarize=True, output_dir=str(tmp_path))
        q.start()
        while not fake.started:
            await asyncio.sleep(0.01)
        assert q.cancel(job.id) is True
        # Let the worker loop observe the cancelled job.
        while job.status not in (FAILED, COMPLETED):
            await asyncio.sleep(0.01)

        assert job.status == FAILED and job.error == "Cancelled"
        assert job.upload_dir and not Path(job.upload_dir).exists()

        # The queue still works for the next job.
        good_md = tmp_path / "good.md"
        good_md.write_text("# good", encoding="utf-8")

        def _ok(path, _result_store=None, job_id=None, **kwargs):
            return good_md

        with patch("wisper_transcribe.web.jobs.process_file", side_effect=_ok):
            job2 = q.submit("/tmp/b.mp3", no_diarize=True, output_dir=str(tmp_path))
            while job2.status not in (FAILED, COMPLETED):
                await asyncio.sleep(0.01)
        assert job2.status == COMPLETED
        await q.stop()


@pytest.mark.anyio
async def test_job_queue_stop_stops_the_ml_worker():
    """JobQueue.stop() terminates the warm child so it never outlives the server."""
    fake = FakeMLWorker()
    q = _make_queue_with(fake)
    await q.stop()
    assert fake.stopped is True


# ---------------------------------------------------------------------------
# Phase 3: enroll and relabel jobs
# ---------------------------------------------------------------------------

def _seed_wizard(tmp_path):
    """A transcript + sidecar so a wizard enroll job has something to run."""
    from wisper_transcribe import transcript_store
    from ._seed import seed_sidecar

    md = tmp_path / "session01.md"
    md.write_text("# Session 01", encoding="utf-8")
    transcript_store.register(md, origin="reconcile")
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    seed_sidecar(md, {
        "input_path": str(audio),
        "diarization_segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
    })
    return md


@pytest.fixture()
def out_root(tmp_path, monkeypatch):
    """Make tmp_path the transcript output root so the sidecar resolves."""
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(tmp_path))
    return tmp_path


def test_enroll_job_delegates_when_on(tmp_path, out_root, _enabled):
    from wisper_transcribe import ml_worker
    from wisper_transcribe.web.jobs import JobQueue

    md = _seed_wizard(tmp_path)
    fake = FakeMLWorker()
    q = _make_queue_with(fake)
    job = q.submit_enroll(str(md), "session01", {"Alice": ["SPEAKER_00"]}, device="cpu")

    seen = {}
    with patch("wisper_transcribe.web.enroll_shared.enroll_profiles",
               side_effect=lambda **kw: seen.setdefault("active", ml_worker.active())):
        q._run_enroll_job(job)

    assert seen["active"] is not None and seen["active"].worker is fake
    assert job.status == "completed"


def test_enroll_job_does_not_delegate_when_off(tmp_path, out_root):
    from wisper_transcribe import ml_worker
    from wisper_transcribe.web.jobs import JobQueue

    md = _seed_wizard(tmp_path)
    with patch("wisper_transcribe.web.jobs._ml_worker_enabled", return_value=False):
        q = _make_queue_with(FakeMLWorker())
        job = q.submit_enroll(str(md), "session01", {"Alice": ["SPEAKER_00"]}, device="cpu")
        seen = {}
        with patch("wisper_transcribe.web.enroll_shared.enroll_profiles",
                   side_effect=lambda **kw: seen.setdefault("active", ml_worker.active())):
            q._run_enroll_job(job)

    assert seen["active"] is None
    assert job.status == "completed"


def test_enroll_job_cancel_ends_cancelled(tmp_path, out_root, _enabled):
    from wisper_transcribe import ml_worker
    from wisper_transcribe.web.jobs import FAILED

    md = _seed_wizard(tmp_path)

    def _blocked(**kw):
        # Stop killed the worker mid-call, so the delegated call raises.
        assert ml_worker.active() is not None
        raise InterruptedError("Job cancelled by user")

    q = _make_queue_with(FakeMLWorker())
    job = q.submit_enroll(str(md), "session01", {"Alice": ["SPEAKER_00"]}, device="cpu")
    with patch("wisper_transcribe.web.enroll_shared.enroll_profiles", side_effect=_blocked):
        q._run_enroll_job(job)

    assert job.status == FAILED and job.error == "Cancelled"


def test_standalone_enroll_delegates_and_cancels(tmp_path, out_root, _enabled):
    from wisper_transcribe import ml_worker
    from wisper_transcribe.web.jobs import FAILED

    src = tmp_path / "clip.wav"
    src.write_bytes(b"fake")
    fake = FakeMLWorker()
    q = _make_queue_with(fake)
    job = q.submit_standalone_enroll(str(src), profile_key="alice",
                                     display_name="Alice")
    seen = {}

    def _diarize(*a, **k):
        seen["active"] = ml_worker.active()
        raise InterruptedError("Job cancelled by user")

    with patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p), \
            patch("wisper_transcribe.config.get_hf_token", return_value="tok"), \
            patch("wisper_transcribe.diarizer.diarize", side_effect=_diarize):
        q._run_enroll_job(job)

    assert seen["active"] is not None and seen["active"].worker is fake
    assert job.status == FAILED and job.error == "Cancelled"


def test_recording_enroll_cancel_ends_cancelled(tmp_path, _enabled):
    from wisper_transcribe.web.jobs import FAILED

    fake = FakeMLWorker()
    q = _make_queue_with(fake)
    job = q.submit_recording_enroll(
        recording_id="rec-1", discord_uid="42", per_user_dir=str(tmp_path),
        profile_key="alice", display_name="Alice",
    )
    with patch("wisper_transcribe.speaker_manager.enroll_speaker_from_audio_dir",
               side_effect=InterruptedError("Job cancelled by user")):
        q._run_enroll_job(job)

    assert job.status == FAILED and job.error == "Cancelled"


def test_relabel_job_delegates_when_on(_enabled):
    from wisper_transcribe import ml_worker
    from types import SimpleNamespace
    fake = FakeMLWorker()
    q = _make_queue_with(fake)
    job = q.submit_relabel("game")
    report = SimpleNamespace(transcripts=[], recurring=[])
    holder = {}

    def _inner(slug, **kwargs):
        holder["active"] = ml_worker.active()
        return report

    with patch("wisper_transcribe.speaker_registry.relabel_campaign", side_effect=_inner):
        q._run_relabel_job(job)

    assert holder["active"] is not None and holder["active"].worker is fake
    assert job.status == "completed"


def test_relabel_job_does_not_delegate_when_off():
    from wisper_transcribe import ml_worker
    from types import SimpleNamespace

    with patch("wisper_transcribe.web.jobs._ml_worker_enabled", return_value=False):
        q = _make_queue_with(FakeMLWorker())
        job = q.submit_relabel("game")
        holder = {}
        report = SimpleNamespace(transcripts=[], recurring=[])

        def _inner(slug, **kwargs):
            holder["active"] = ml_worker.active()
            return report

        with patch("wisper_transcribe.speaker_registry.relabel_campaign", side_effect=_inner):
            q._run_relabel_job(job)

    assert holder["active"] is None
    assert job.status == "completed"


def test_relabel_job_cancel_ends_cancelled(_enabled):
    from wisper_transcribe import ml_worker
    from wisper_transcribe.web.jobs import FAILED

    fake = FakeMLWorker(block=True)
    q = _make_queue_with(fake)
    job = q.submit_relabel("game")

    def _blocked(slug, **kwargs):
        # Stop killed the worker mid-call, so the delegated call raises.
        assert ml_worker.active() is not None
        raise InterruptedError("Job cancelled by user")

    with patch("wisper_transcribe.speaker_registry.relabel_campaign", side_effect=_blocked):
        q._run_relabel_job(job)

    assert job.status == FAILED and job.error == "Cancelled"


def test_llm_journal_and_live_jobs_never_delegate(_enabled, tmp_path):
    """Only transcription/enroll/relabel delegate; these three leave active() None."""
    from wisper_transcribe import ml_worker
    from types import SimpleNamespace

    q = _make_queue_with(FakeMLWorker())
    seen = {}

    # LLM job: _do_llm_work is the seam.
    llm_job = q.submit_llm("/tmp/x.md", "refine")
    with patch.object(q, "_do_llm_work",
                      side_effect=lambda **kw: seen.setdefault("llm", ml_worker.active())):
        q._run_llm_job(llm_job)
    assert seen["llm"] is None

    # Journal job: capture at the first LLM seam.
    journal_job = q.submit_journal("game")
    with patch("wisper_transcribe.llm.get_client", return_value=SimpleNamespace(
            provider="ollama", model="m")), \
            patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
            patch("wisper_transcribe.journal.update_journal",
                  side_effect=lambda *a, **k: seen.setdefault("journal", ml_worker.active())), \
            patch("wisper_transcribe.journal.unjournalled_sessions", return_value=["s1"]):
        q._run_journal_job(journal_job)
    assert seen["journal"] is None

    # Live job: the live loop is the seam.
    live_job = q.submit_live("rec-1", str(tmp_path / "live.md"),
                             model_size="tiny", device="cpu")
    with patch("wisper_transcribe.web.live_transcribe.run_live_loop",
               side_effect=lambda *a, **k: seen.setdefault("live", ml_worker.active())):
        q._run_live_job(live_job)
    assert seen["live"] is None
