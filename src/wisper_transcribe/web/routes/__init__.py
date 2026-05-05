"""Web UI route handlers."""
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..jobs import JobQueue

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Custom Jinja2 filters
templates.env.filters["basename"] = lambda p: Path(str(p)).name
templates.env.filters["stem"] = lambda p: Path(str(p)).stem
templates.env.filters["urlencode"] = lambda s: quote(str(s))


def get_queue(request: Request) -> JobQueue:
    """Retrieve the shared JobQueue from the app state."""
    return request.app.state.job_queue


def get_bot_manager(request: Request):
    """Retrieve BotManager from app state. Returns None until Phase 3 wires it in."""
    return getattr(request.app.state, "bot_manager", None)
