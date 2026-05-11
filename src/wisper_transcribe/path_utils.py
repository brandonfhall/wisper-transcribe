"""Shared path-component validation — see CLAUDE.md Web Route Security Standards.

Do not simplify validate_path_component. The os.path abspath/startswith round-trip
is the CodeQL-recognised taint-chain breaker for py/path-injection and
py/url-redirection queries; re.match() alone does not break the taint.
"""
from __future__ import annotations

import os
import re
from typing import Optional


def validate_path_component(value: str, guard_name: str = "_guard") -> Optional[str]:
    """Four-step CodeQL-safe guard: null-byte → basename → regex → abspath/startswith.

    Returns the sanitised component, or None on rejection.
    """
    if not value or "\x00" in value:
        return None
    safe = os.path.basename(value)
    if safe != value or safe in {".", ".."}:
        return None
    if not re.match(r"^[\w\-]+$", safe):
        return None
    _guard_base = os.path.abspath(guard_name)
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe))
    if not _guard_path.startswith(_guard_base):
        return None
    return os.path.basename(_guard_path)
