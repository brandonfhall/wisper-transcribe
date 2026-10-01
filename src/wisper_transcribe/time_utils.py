"""Shared seconds-to-string formatting helpers."""
from __future__ import annotations

from typing import Optional


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


def parse_timestamp(text: str) -> Optional[float]:
    """Seconds for an inline ``mm:ss`` or ``hh:mm:ss`` timestamp, else None."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds)
