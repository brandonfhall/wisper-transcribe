"""Warm subprocess for the four GPU functions (job cancellation support).

Stop only sets ``job._cancel_event``; a job thread notices it on its next
``tqdm.write``, so a long model call keeps the GPU busy until the stage ends.
Proxying the four GPU functions — ``transcriber.transcribe``,
``diarizer.diarize``, ``word_alignment.align_words``, and
``speaker_manager.extract_embedding`` — into one spawned child lets Stop
``terminate()`` the process and free the GPU immediately.

The child is spawned once, lazily, and kept warm across jobs so the
module-level model caches live there.  Delegation is thread-local:
``delegating()`` is set by the job runner and cleared in ``finally``; the CLI,
live recording, request threads, and the worker child itself never delegate.

Importing this module is cheap: no ML library is touched here.
"""
from __future__ import annotations

import multiprocessing
import queue as _queue
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

# The only functions a delegated call may run in the child.  Keys are the
# names used on the wire; values are "module:function" strings, imported
# lazily inside the child so importing this module never pulls in torch.
DEFAULT_ALLOW_LIST = {
    "transcribe": "wisper_transcribe.transcriber:transcribe",
    "diarize": "wisper_transcribe.diarizer:diarize",
    "align_words": "wisper_transcribe.word_alignment:align_words",
    "extract_embedding": "wisper_transcribe.speaker_manager:extract_embedding",
}

# Re-raised in the parent as the matching built-in type.  Anything else
# becomes a plain RuntimeError: torch/pyannote exceptions often don't
# unpickle across processes.
_KNOWN_ERRORS = {
    exc.__name__: exc
    for exc in (RuntimeError, ValueError, FileNotFoundError, MemoryError, ImportError)
}

_CRASH_MESSAGE = "ML worker exited unexpectedly"

# The wait loop polls the result queue this often so it can drain logs/bars
# and notice a cancel promptly without busy-spinning.
_POLL_SECONDS = 0.1
_JOIN_SECONDS = 2.0


def _child_loop(allow_list: dict, spec_q, log_q, result_q) -> None:
    """Child entry point.  Spawned by ``MLWorker``; never called directly.

    Third-party noise suppression runs first, before any ML import, then
    ``pipeline._patch_tqdm_for_queue`` routes ``tqdm.write`` logs and progress
    bars across ``log_q``.  The child never delegates: it is the worker.
    """
    from ._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    from .pipeline import _patch_tqdm_for_queue
    _patch_tqdm_for_queue(log_q, "")

    import importlib

    while True:
        spec = spec_q.get()
        if spec is None:
            return
        name, args, kwargs = spec
        try:
            module_name, func_name = allow_list[name].split(":", 1)
            func = getattr(importlib.import_module(module_name), func_name)
        except BaseException as exc:
            result_q.put(("err", type(exc).__name__, str(exc)))
            continue
        try:
            result_q.put(("ok", func(*args, **kwargs)))
        except BaseException as exc:
            result_q.put(("err", type(exc).__name__, str(exc)))


class _Delegation:
    """Per-thread marker set by ``delegating()`` and read by ``active()``."""

    __slots__ = ("worker", "cancel_event", "on_log", "on_bar")

    def __init__(self, worker: "MLWorker", cancel_event, on_log, on_bar) -> None:
        self.worker = worker
        self.cancel_event = cancel_event
        self.on_log = on_log
        self.on_bar = on_bar


_thread_state = threading.local()


def active() -> Optional["_Delegation"]:
    """Return the calling thread's delegation, or ``None``.

    Only the job runner sets this; every other thread — request handlers,
    the CLI, live recording, and the worker child — sees ``None``.
    """
    return getattr(_thread_state, "value", None)


@contextmanager
def delegating(worker: "MLWorker", cancel_event=None, on_log=None,
               on_bar=None) -> Iterator["_Delegation"]:
    """Make GPU calls in this thread run through *worker* until the block ends.

    Always cleared in ``finally``, including on exception, and restored to
    whatever the thread had before (normally ``None``).

    Cancel is sticky: a block that ends normally while ``cancel_event`` is set
    raises ``InterruptedError``. Several callers catch ``Exception`` around a
    GPU call and carry on (per-speaker loops), which would otherwise turn a
    Stop into a "completed" job.
    """
    previous = getattr(_thread_state, "value", None)
    _thread_state.value = _Delegation(worker, cancel_event, on_log, on_bar)
    try:
        yield _thread_state.value
    finally:
        _thread_state.value = previous
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Job cancelled by user")


def delegated_call(name: str, args: tuple, kwargs: dict) -> tuple[bool, Any]:
    """Route a GPU call through the active delegation.

    Returns ``(True, result)`` when this thread is delegating, else
    ``(False, None)`` so the caller runs in-process.  Kept here so the four
    guarded functions stay one line each.
    """
    token = getattr(_thread_state, "value", None)
    if token is None:
        return False, None
    return True, token.worker.call(
        name, *args, cancel_event=token.cancel_event,
        on_log=token.on_log, on_bar=token.on_bar, **kwargs,
    )


class MLWorker:
    """One warm spawned child that runs delegated GPU calls in a loop.

    ``call()`` runs in the job thread — never a helper/drain thread — so the
    existing cancellation semantics (``tqdm.write`` raising ``InterruptedError``
    in the job thread) hold and Stop can ``terminate()`` the child.
    """

    def __init__(self, allow_list: Optional[dict] = None) -> None:
        self._allow_list = dict(allow_list) if allow_list else dict(DEFAULT_ALLOW_LIST)
        self._lock = threading.Lock()
        self._proc = None
        self._spec_q = None
        self._log_q = None
        self._result_q = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.is_alive()

    @property
    def pid(self) -> Optional[int]:
        proc = self._proc
        return proc.pid if proc is not None else None

    def _spawn(self) -> None:
        """Start a fresh child and its queues.  Must hold ``self._lock``."""
        ctx = multiprocessing.get_context("spawn")
        # Fresh queues each spawn: nothing from a killed child is ever read.
        self._spec_q = ctx.Queue()
        self._log_q = ctx.Queue()
        self._result_q = ctx.Queue()
        self._proc = ctx.Process(
            target=_child_loop,
            args=(self._allow_list, self._spec_q, self._log_q, self._result_q),
            daemon=True,
        )
        self._proc.start()

    def stop(self) -> None:
        """Stop the child, if any.  Safe to call more than once.

        Asks the loop to exit, then terminates and kills a child that won't.
        Called from ``JobQueue.stop()`` and the ``CancelledError`` path, so the
        worker never outlives the server holding the GPU.
        """
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        try:
            if proc.is_alive():
                self._spec_q.put_nowait(None)
                proc.join(timeout=_JOIN_SECONDS)
        except Exception:
            pass
        self._kill(proc)

    def _kill(self, proc) -> None:
        """Terminate and, if needed, kill *proc*.  Idempotent."""
        if proc is None:
            return
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=_JOIN_SECONDS)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=_JOIN_SECONDS)
        except Exception:
            pass
        with self._lock:
            if self._proc is proc:
                self._proc = None

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def _drain(self, log_q, on_log, on_bar) -> None:
        """Forward every queued log/bar message to its callback."""
        while True:
            try:
                _channel, msg_type, message = log_q.get_nowait()
            except _queue.Empty:
                return
            if msg_type == "bar":
                if on_bar is not None:
                    on_bar(message)
            elif on_log is not None:
                on_log(message)

    def call(self, name: str, *args: Any, cancel_event=None,
             on_log: Optional[Callable[[str], None]] = None,
             on_bar: Optional[Callable[[str], None]] = None, **kwargs: Any) -> Any:
        """Run *name* in the child and return its result.

        Waits in the calling (job) thread: polls the result with a short
        timeout, drains logs/bars each pass, and checks ``cancel_event`` and
        the child's liveness.  Cancel terminates the child and raises
        ``InterruptedError``; a crash raises ``RuntimeError`` and the next
        call respawns.
        """
        # Checked before spawning: after a Stop, a caller that swallowed the
        # first InterruptedError must not start (and load models in) a new child.
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Job cancelled by user")
        with self._lock:
            if not self.alive:
                self._spawn()
            proc = self._proc
            spec_q, log_q, result_q = self._spec_q, self._log_q, self._result_q

        replied = False
        try:
            spec_q.put((name, args, kwargs))
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Job cancelled by user")
                try:
                    msg = result_q.get(timeout=_POLL_SECONDS)
                except _queue.Empty:
                    self._drain(log_q, on_log, on_bar)
                    if not proc.is_alive():
                        self._kill(proc)
                        raise RuntimeError(_CRASH_MESSAGE)
                    continue
                replied = True
                self._drain(log_q, on_log, on_bar)
                kind = msg[0]
                if kind == "ok":
                    return msg[1]
                _, err_name, err_msg = msg
                raise _KNOWN_ERRORS.get(err_name, RuntimeError)(err_msg)
        except BaseException:
            # Leaving before the reply (cancel, a callback such as
            # capturing_write raising, Ctrl+C) leaves the child mid-call; its
            # late reply would answer the next call. Kill it. An error the
            # child reported is a reply: the child stays warm.
            if not replied:
                self._kill(proc)
            raise
