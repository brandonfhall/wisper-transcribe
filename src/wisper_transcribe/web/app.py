"""FastAPI application factory for the wisper-transcribe web UI."""
from __future__ import annotations

import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import tqdm as _tqdm_module
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .jobs import JobQueue

# Disable TMonitor globally — it spawns a daemon thread with an atexit join()
# that hangs on Python 3.14's stricter thread cleanup, requiring multiple Ctrl+C.
# TMonitor only helps detect stalled bars in interactive terminals; it's useless
# in a web server context.
_tqdm_module.tqdm.monitor_interval = 0

_STATIC_DIR = Path(__file__).parent.parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_INPUT_CSS = _STATIC_DIR / "input.css"
_OUTPUT_CSS = _STATIC_DIR / "tailwind.min.css"


def _build_tailwind() -> None:
    """Rebuild tailwind.min.css from input.css if the source is newer.

    Runs the pytailwindcss standalone binary (bundled with the package —
    no Node.js required).  Safe to call on every startup; skips the build
    if output is already up-to-date.
    """
    if (
        _OUTPUT_CSS.exists()
        and _INPUT_CSS.stat().st_mtime <= _OUTPUT_CSS.stat().st_mtime
    ):
        return  # already up-to-date

    try:
        subprocess.run(
            [
                sys.executable, "-m", "pytailwindcss",
                "-i", str(_INPUT_CSS),
                "-o", str(_OUTPUT_CSS),
                "--minify",
            ],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # Non-fatal: serve the existing CSS if the build fails
        import warnings
        warnings.warn(f"Tailwind CSS build failed: {exc}. Using existing tailwind.min.css.")

try:
    from wisper_transcribe import __version__
except Exception:
    __version__ = "unknown"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    job_queue = JobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[misc]
        _build_tailwind()
        job_queue.start()
        yield
        await job_queue.stop()

    app = FastAPI(
        title="wisper-transcribe",
        description="Podcast transcription with speaker diarization",
        version=__version__,
        lifespan=lifespan,
    )

    # Store shared state
    app.state.job_queue = job_queue

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Register routers
    from .routes import config as config_router
    from .routes import dashboard as dashboard_router
    from .routes import speakers as speakers_router
    from .routes import transcribe as transcribe_router
    from .routes import transcripts as transcripts_router

    app.include_router(dashboard_router.router)
    app.include_router(transcribe_router.router)
    app.include_router(transcripts_router.router)
    app.include_router(speakers_router.router)
    app.include_router(config_router.router)

    return app


# Module-level app instance (for uvicorn)
app = create_app()
