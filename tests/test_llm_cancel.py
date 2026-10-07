"""Tests for the thread-local LLM cancel scope (llm/cancel.py)."""
from __future__ import annotations

import threading

import pytest


def test_no_scope_has_no_event():
    from wisper_transcribe.llm.cancel import current_cancel_event

    assert current_cancel_event() is None


def test_raise_if_cancelled_is_a_noop_without_a_scope():
    from wisper_transcribe.llm.cancel import raise_if_cancelled

    raise_if_cancelled()  # must not raise


def test_scope_sets_and_restores_the_event():
    from wisper_transcribe.llm.cancel import cancel_scope, current_cancel_event

    event = threading.Event()
    with cancel_scope(event):
        assert current_cancel_event() is event
    assert current_cancel_event() is None


def test_scope_restores_the_previous_event_on_exit():
    from wisper_transcribe.llm.cancel import cancel_scope, current_cancel_event

    outer = threading.Event()
    inner = threading.Event()
    with cancel_scope(outer):
        assert current_cancel_event() is outer
        with cancel_scope(inner):
            assert current_cancel_event() is inner
        assert current_cancel_event() is outer
    assert current_cancel_event() is None


def test_scope_restores_the_previous_event_after_an_exception():
    from wisper_transcribe.llm.cancel import cancel_scope, current_cancel_event

    event = threading.Event()
    with pytest.raises(RuntimeError):
        with cancel_scope(event):
            raise RuntimeError("boom")
    assert current_cancel_event() is None


def test_raise_if_cancelled_raises_only_when_set():
    from wisper_transcribe.llm.cancel import cancel_scope, raise_if_cancelled

    event = threading.Event()
    with cancel_scope(event):
        raise_if_cancelled()  # not set yet — no-op
        event.set()
        with pytest.raises(InterruptedError, match="Job cancelled by user"):
            raise_if_cancelled()


def test_scope_is_thread_local():
    from wisper_transcribe.llm.cancel import cancel_scope, current_cancel_event

    event = threading.Event()
    seen = {}

    def _other() -> None:
        seen["other"] = current_cancel_event()
        from wisper_transcribe.llm.cancel import raise_if_cancelled
        raise_if_cancelled()  # the main thread's event must not apply here
        seen["raised"] = False

    with cancel_scope(event):
        event.set()
        t = threading.Thread(target=_other)
        t.start()
        t.join()

    assert seen["other"] is None
    assert seen["raised"] is False


def test_set_cancel_event_in_one_thread_does_not_affect_another():
    """A cancelled job thread and an unscoped request thread never share state."""
    from wisper_transcribe.llm.cancel import cancel_scope, raise_if_cancelled

    event = threading.Event()
    errors = []

    def _scoped() -> None:
        try:
            with cancel_scope(event):
                event.set()
                raise_if_cancelled()
        except InterruptedError:
            errors.append("scoped")

    with cancel_scope(threading.Event()):  # main thread has its own, unset
        t = threading.Thread(target=_scoped)
        t.start()
        t.join()
        raise_if_cancelled()  # main thread unaffected

    assert errors == ["scoped"]


def test_retry_on_empty_wait_is_interruptible():
    """Stop during the empty-response back-off raises at once, not after the sleep."""
    import time

    from wisper_transcribe.llm.base import LLMClient
    from wisper_transcribe.llm.cancel import cancel_scope

    class _EmptyClient(LLMClient):
        provider = "fake"
        model = "fake-model"

        def complete(self, system, user):
            return ""

        def complete_json(self, system, user, schema):
            return {}

    client = _EmptyClient()
    event = threading.Event()

    def _cancel_later() -> None:
        time.sleep(0.05)
        event.set()

    threading.Thread(target=_cancel_later, daemon=True).start()
    with cancel_scope(event):
        began = time.monotonic()
        with pytest.raises(InterruptedError, match="Job cancelled by user"):
            client._retry_on_empty(lambda: "")
        assert time.monotonic() - began < 1.0
