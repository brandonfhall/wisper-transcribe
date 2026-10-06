"""Tests for the warm ML worker subprocess (wisper_transcribe.ml_worker).

Real spawned children, but the functions run are the fakes in
``tests/_ml_fake.py`` — never a real ML function.  Keep this file's
real-spawn tests few: each spawn costs 1-2 s.
"""
from __future__ import annotations

import threading

import pytest

from . import _ml_fake

_ALLOW = {
    "echo": "tests._ml_fake:echo",
    "boom": "tests._ml_fake:boom",
    "weird": "tests._ml_fake:weird",
    "slow": "tests._ml_fake:slow",
    "crash": "tests._ml_fake:crash",
    "emit": "tests._ml_fake:emit",
}


def _worker():
    from wisper_transcribe.ml_worker import MLWorker
    return MLWorker(allow_list=_ALLOW)


@pytest.fixture()
def worker():
    w = _worker()
    try:
        yield w
    finally:
        w.stop()


def test_import_is_cheap():
    """Importing ml_worker must not pull in any ML library."""
    import subprocess
    import sys

    code = (
        "import sys; import wisper_transcribe.ml_worker; "
        "bad=[m for m in ('torch','faster_whisper','transformers') if m in sys.modules]"
        "; sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_call_round_trips_a_result(worker):
    assert worker.call("echo", {"a": 1, "b": [1, 2, 3]}) == {"a": 1, "b": [1, 2, 3]}


def test_known_error_is_rer_aised(worker):
    with pytest.raises(ValueError, match="known boom"):
        worker.call("boom")


def test_unknown_error_becomes_runtime_error(worker):
    with pytest.raises(RuntimeError, match="unknown nope"):
        worker.call("weird")


def test_logs_and_bars_reach_callbacks(worker):
    logs: list[str] = []
    bars: list[str] = []
    assert worker.call("emit", on_log=logs.append, on_bar=bars.append) == "emitted"
    assert any("hello from the child" in line for line in logs)
    assert any("fake" in bar for bar in bars)


def test_cancel_kills_the_child_and_raises(worker):
    cancel = threading.Event()

    def _cancel_soon():
        cancel.set()

    timer = threading.Timer(0.3, _cancel_soon)
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            worker.call("slow", 10.0, cancel_event=cancel)
    finally:
        timer.cancel()

    assert not worker.alive


def test_next_call_respawns_after_cancel(worker):
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        with pytest.raises(InterruptedError):
            worker.call("slow", 10.0, cancel_event=cancel)
    finally:
        timer.cancel()
    first_pid = worker.pid

    assert worker.call("echo", "back") == "back"
    assert worker.alive
    assert worker.pid is not None and worker.pid != first_pid


def test_crash_raises_then_respawns(worker):
    with pytest.raises(RuntimeError, match="ML worker exited unexpectedly"):
        worker.call("crash")
    assert worker.call("echo", "recovered") == "recovered"


def test_stop_leaves_no_live_child(worker):
    worker.call("echo", "x")
    assert worker.alive
    worker.stop()
    assert not worker.alive
    worker.stop()  # idempotent


def test_delegating_is_thread_local_and_cleared(worker):
    from wisper_transcribe import ml_worker

    assert ml_worker.active() is None

    seen = {}

    def other_thread():
        seen["active"] = ml_worker.active()

    with ml_worker.delegating(worker):
        assert ml_worker.active() is not None
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()

    assert seen["active"] is None
    assert ml_worker.active() is None


def test_delegating_cleared_on_exception(worker):
    from wisper_transcribe import ml_worker

    with pytest.raises(RuntimeError):
        with ml_worker.delegating(worker):
            raise RuntimeError("boom")
    assert ml_worker.active() is None


def test_four_gpu_functions_delegate_when_active():
    """Each guarded function routes through the worker without touching its
    real implementation (a sentinel return proves the guard fired first)."""
    from wisper_transcribe import (
        diarizer, ml_worker, speaker_manager, transcriber, word_alignment,
    )

    calls: list[str] = []

    class FakeWorker:
        def call(self, name, *args, **kwargs):
            calls.append(name)
            return "SENTINEL"

    with ml_worker.delegating(FakeWorker()):
        assert transcriber.transcribe("audio.wav") == "SENTINEL"
        assert diarizer.diarize("audio.wav", "token", "cpu") == "SENTINEL"
        assert word_alignment.align_words("audio.wav", [], "cpu") == "SENTINEL"
        assert speaker_manager.extract_embedding("audio.wav", [], "SPEAKER_00") == "SENTINEL"

    assert calls == ["transcribe", "diarize", "align_words", "extract_embedding"]


def test_delegated_call_inactive_returns_false():
    from wisper_transcribe import ml_worker

    assert ml_worker.delegated_call("transcribe", ("x",), {}) == (False, None)
