"""Thread-local cancel scope for LLM calls.

The web job runners set a job's ``_cancel_event`` on Stop; a streaming LLM
client polls this scope so an in-flight stream ends promptly without ever
reading the job directly.  Code outside a scope (the CLI, request threads)
sees no event and behaves as before.

Modelled on ``ml_worker.delegating()``: the scope is thread-local, nests, and
restores the previous value on exit.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional


_state = threading.local()


def current_cancel_event() -> Optional[threading.Event]:
    """Return the calling thread's cancel event, or ``None`` when unscoped."""
    return getattr(_state, "event", None)


@contextmanager
def cancel_scope(event: threading.Event) -> Iterator[None]:
    """Make *event* the calling thread's cancel event until the block ends.

    Restores whatever the thread had before (normally ``None``) and nests.
    """
    previous = getattr(_state, "event", None)
    _state.event = event
    try:
        yield
    finally:
        _state.event = previous


def raise_if_cancelled() -> None:
    """Raise ``InterruptedError`` when the current thread's event is set.

    A no-op with no scope, so process-wide ``sys.stderr`` writers on other
    threads never raise for a job's cancel.
    """
    event = getattr(_state, "event", None)
    if event is not None and event.is_set():
        raise InterruptedError("Job cancelled by user")
