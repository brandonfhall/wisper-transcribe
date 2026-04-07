"""FastAPI application factory for the wisper-transcribe web UI."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .jobs import JobQueue

_STATIC_DIR = Path(__file__).parent.parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

try:
    from wisper_transcribe import __version__
except Exception:
    __version__ = "unknown"


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    job_queue = JobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[misc]
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
