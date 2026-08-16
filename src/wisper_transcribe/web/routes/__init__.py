"""Web UI route handlers."""
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..jobs import JobQueue

try:
    from wisper_transcribe import __version__ as _app_version
except Exception:
    _app_version = "dev"

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_STATIC_DIR = Path(__file__).parent.parent.parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Custom Jinja2 filters
templates.env.filters["basename"] = lambda p: Path(str(p)).name
templates.env.filters["stem"] = lambda p: Path(str(p)).stem
templates.env.filters["urlencode"] = lambda s: quote(str(s))


def _static_mtime(filename: str) -> str:
    """Cache-busting query value for a static asset, keyed to the file's own
    mtime rather than the package version. `app_version` is static across a
    `--reload` dev session (the process never restarts for a static-file
    edit, so `app_version` doesn't either), which let the browser serve a
    stale cached `app.js`/`tailwind.min.css` across every edit in a session
    -- confirmed 2026-08-16 (a live JS fix silently never reached the
    browser this way). Called fresh per template render, not cached at
    import time, since static edits don't restart the process.
    """
    try:
        return str(int((_STATIC_DIR / filename).stat().st_mtime))
    except OSError:
        return _app_version


# Globals available in every template
templates.env.globals["app_version"] = _app_version
templates.env.globals["static_mtime"] = _static_mtime


def get_queue(request: Request) -> JobQueue:
    """Retrieve the shared JobQueue from the app state."""
    return request.app.state.job_queue


def get_bot_manager(request: Request):
    """Retrieve BotManager from app state. Returns None until Phase 3 wires it in."""
    return getattr(request.app.state, "bot_manager", None)


def get_local_capture_manager(request: Request):
    """Retrieve LocalCaptureManager from app state. Returns None before lifespan wiring."""
    return getattr(request.app.state, "local_capture_manager", None)
