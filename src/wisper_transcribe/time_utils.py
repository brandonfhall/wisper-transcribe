"""Shared time formatting helpers.

Centralises the two flavours of seconds→string conversion that were
previously duplicated in formatter.py and pipeline.py.
"""
from __future__ import annotations


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``mm:ss`` or ``hh:mm:ss`` (for inline timestamps)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_duration(seconds: float) -> str:
    """Format seconds as ``h:mm:ss`` (for total-duration display)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"
