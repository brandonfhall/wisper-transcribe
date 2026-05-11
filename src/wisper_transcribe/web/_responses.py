"""Shared HTTP response helpers for route handlers."""
from __future__ import annotations

from fastapi.responses import HTMLResponse, RedirectResponse


def invalid_input_response(msg: str) -> HTMLResponse:
    return HTMLResponse(content=msg, status_code=400)


def error_redirect(base: str, code: str) -> RedirectResponse:
    return RedirectResponse(url=f"{base}?error={code}", status_code=303)
