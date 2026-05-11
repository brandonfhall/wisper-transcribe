"""FastAPI application factory for the wisper-transcribe web UI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import tqdm as _tqdm_module
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

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


# Content-Security-Policy: script-src allows 'unsafe-inline' because several
# templates still use inline <script> blocks and onclick handlers.  Moving those
# to app.js and switching to a nonce-based policy would eliminate this exception
# (tracked as a future hardening task).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add defensive HTTP headers to every response (A05 Security Misconfiguration)."""

    async def dispatch(
        self, request: StarletteRequest, call_next  # type: ignore[override]
    ) -> StarletteResponse:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = _CSP
        return response


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    job_queue = JobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[misc]
        _build_tailwind()
        job_queue.start()

        # Write server.json so CLI can discover the running server.
        # WISPER_BIND is set by the `wisper server` CLI command; falls back to
        # 0.0.0.0:8080. 0.0.0.0 is normalised to 127.0.0.1 for CLI use.
        raw_bind = os.environ.get("WISPER_BIND", "0.0.0.0:8080")
        host, _, port = raw_bind.rpartition(":")
        cli_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        server_url = f"http://{cli_host}:{port}"
        from wisper_transcribe.config import get_data_dir
        data_dir = get_data_dir()
        _sj = data_dir / "server.json"
        _sj.parent.mkdir(parents=True, exist_ok=True)
        _sj.write_text(json.dumps({"url": server_url}), encoding="utf-8")

        from wisper_transcribe.recording_manager import reconcile_on_startup
        reconcile_on_startup(data_dir)

        from .discord_bot import BotManager
        bot_manager = BotManager(data_dir=data_dir)
        bot_manager.start()
        app.state.bot_manager = bot_manager

        try:
            yield
        finally:
            await bot_manager.stop()
            await job_queue.stop()
            try:
                _sj.unlink(missing_ok=True)
            except OSError:
                pass

    app = FastAPI(
        title="wisper-transcribe",
        description="Podcast transcription with speaker diarization",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(_SecurityHeadersMiddleware)

    # Store shared state
    app.state.job_queue = job_queue

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Register routers
    from .routes import campaigns as campaigns_router
    from .routes import config as config_router
    from .routes import dashboard as dashboard_router
    from .routes import record as record_router
    from .routes import speakers as speakers_router
    from .routes import transcribe as transcribe_router
    from .routes import transcripts as transcripts_router

    app.include_router(dashboard_router.router)
    app.include_router(transcribe_router.router)
    app.include_router(transcripts_router.router)
    app.include_router(speakers_router.router)
    app.include_router(config_router.router)
    app.include_router(campaigns_router.router)
    app.include_router(record_router.router)

    return app


# Module-level app instance (for uvicorn)
app = create_app()
