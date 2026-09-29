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
    """Cache-busting value for a static asset: its mtime, read on every render.

    The package version can't be used: static edits don't restart a
    ``--reload`` server, so browsers would keep a stale cached copy.
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
    """Retrieve BotManager from app state, or None if not configured."""
    return getattr(request.app.state, "bot_manager", None)


def get_local_capture_manager(request: Request):
    """Retrieve LocalCaptureManager from app state. Returns None before lifespan wiring."""
    return getattr(request.app.state, "local_capture_manager", None)
