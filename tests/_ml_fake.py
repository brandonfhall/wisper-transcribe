"""Fake GPU functions for ml_worker tests.

Runs in a real spawned child, so it must not import torch, pyannote,
faster_whisper, or transformers.  Only tqdm/os/time are used.
"""
from __future__ import annotations

import os
import time


def echo(value):
    """Return *value* unchanged (picklable round-trip)."""
    return value


def boom() -> None:
    """Raise a known-type error (ValueError)."""
    raise ValueError("known boom")


def weird() -> None:
    """Raise an error type the parent doesn't re-raise (KeyError)."""
    raise KeyError("unknown nope")


def slow(seconds: float = 5.0) -> str:
    """Sleep for *seconds*; lets the parent check cancel mid-call."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.02)
    return "finished"


def crash() -> None:
    """Exit the child immediately without replying (a hard crash)."""
    os._exit(1)


def emit() -> str:
    """Write a log line and render a bar, then linger so the parent drains."""
    from tqdm import tqdm

    tqdm.write("hello from the child")
    for _ in tqdm(range(3), desc="fake", dynamic_ncols=False, ncols=60):
        pass
    # Stay alive briefly so the parent's poll loop runs its drain before the
    # result arrives (mirrors the real case: output during a long call).
    time.sleep(0.5)
    return "emitted"
