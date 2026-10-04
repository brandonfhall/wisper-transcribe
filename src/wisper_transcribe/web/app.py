"""FastAPI application factory for the wisper-transcribe web UI."""
from __future__ import annotations

import asyncio
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

from ..tailwind import build_css
from .jobs import JobQueue

# Disable tqdm's TMonitor thread: its atexit join hangs Ctrl+C on Python 3.14,
# and stall detection is useless in a server.
_tqdm_module.tqdm.monitor_interval = 0

# UTF-8 stdio, as in cli._ensure_utf8_stdio(): `uvicorn --reload` imports this
# module in a fresh subprocess that never runs cli.py, and legacy code pages
# can't encode the characters pipeline.py writes via tqdm.write().
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

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
    # A partial install without input.css warns and continues below.
    try:
        up_to_date = (
            _OUTPUT_CSS.exists()
            and _INPUT_CSS.stat().st_mtime <= _OUTPUT_CSS.stat().st_mtime
        )
    except OSError:
        up_to_date = False

    if up_to_date:
        return  # already up-to-date

    try:
        build_css(_INPUT_CSS, _OUTPUT_CSS).check_returncode()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # Non-fatal: serve the existing CSS if the build fails
        import warnings
        warnings.warn(f"Tailwind CSS build failed: {exc}. Using existing tailwind.min.css.")

def _cleanup_recording_trash() -> None:
    """Delete ``recordings/*/.wisper-trash-*`` directories at startup.

    A trim renames what it deletes to a trash directory before removing it, so
    these exist only if the server stopped mid-delete.
    """
    import logging
    import shutil

    from wisper_transcribe.config import get_data_dir
    from wisper_transcribe.recording_manager import TRASH_PREFIX

    log = logging.getLogger(__name__)
    for path in (get_data_dir() / "recordings").glob(f"*/{TRASH_PREFIX}*"):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)


def _cleanup_orphaned_uploads() -> None:
    """Delete wisper_upload_* folders and files and wisper_enroll_*/wisper_enrollsrc_* files at startup.

    A transcription upload lives in ``wisper_upload_<job-id>/`` until its job
    ends, and the job deletes the folder on success, failure, or cancel; an
    enroll upload is renamed to ``wisper_enrollsrc_<job-id>`` and deleted the
    same way. This only covers a crash, and sweeping is safe because the
    in-memory queue is empty at startup.
    """
    import glob
    import logging
    import shutil
    import tempfile

    tmp_dir = tempfile.gettempdir()
    patterns = ("wisper_upload_*", "wisper_enroll_*", "wisper_enrollsrc_*")
    orphans = [p for pattern in patterns for p in glob.glob(str(Path(tmp_dir) / pattern))]
    if not orphans:
        return
    log = logging.getLogger(__name__)
    for path in orphans:
        try:
            if Path(path).is_dir() and not Path(path).is_symlink():
                shutil.rmtree(path)
            else:
                Path(path).unlink(missing_ok=True)
            log.debug("Removed orphaned upload: %s", path)
        except OSError as exc:
            log.warning("Could not remove orphaned upload %s: %s", path, exc)
    log.info("Cleaned up %d orphaned upload(s) from previous session", len(orphans))


try:
    from wisper_transcribe import __version__
except Exception:
    __version__ = "unknown"


# script-src allows 'unsafe-inline' because templates still contain inline
# <script> blocks and onclick handlers; moving them to app.js would allow a
# nonce-based policy.
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
        # DENY matches the CSP's frame-ancestors 'none'.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = _CSP
        return response


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    job_queue = JobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[misc]
        # Migrate (or refuse) before serving anything.
        from wisper_transcribe import db
        try:
            db.connect().close()
        except db.DatabaseError as exc:
            import logging
            logging.getLogger(__name__).error("%s", exc)
            raise
        heartbeat = db.Heartbeat().start()

        # Jobs left pending/running died with the last process.
        try:
            from wisper_transcribe import job_history
            job_history.mark_interrupted()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Could not mark interrupted jobs", exc_info=True)

        # Register transcripts added while the server was down, flag deleted
        # ones, match renames, and sweep crash leftover temp files.
        try:
            from wisper_transcribe import transcript_store
            transcript_store.reconcile(sweep=True)
            attention = transcript_store.needs_attention()
            if attention.total:
                import logging
                logging.getLogger(__name__).warning(
                    "Needs attention: %d missing transcript(s), %d missing file(s), "
                    "%d file(s) with no transcript; see the Transcripts page",
                    len(attention.missing_transcripts), len(attention.missing_files),
                    len(attention.unclaimed))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Transcript reconcile failed", exc_info=True)

        # Index transcripts not yet in the search index (an existing archive,
        # files added while stopped), in the background.
        from wisper_transcribe import search_index
        search_index.start_worker()

        _build_tailwind()
        job_queue.start()

        # Write server.json for CLI discovery. WISPER_BIND comes from
        # `wisper server`; 0.0.0.0 is normalised to 127.0.0.1 for the CLI.
        raw_bind = os.environ.get("WISPER_BIND", "127.0.0.1:8080")
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

        _cleanup_orphaned_uploads()
        _cleanup_recording_trash()

        from .discord_bot import BotManager
        bot_manager = BotManager(data_dir=data_dir)
        bot_manager.start()
        app.state.bot_manager = bot_manager

        from .local_capture import LocalCaptureManager
        local_capture_manager = LocalCaptureManager(data_dir=data_dir)
        local_capture_manager.start()
        app.state.local_capture_manager = local_capture_manager

        try:
            yield
        finally:
            # Must precede job_queue.stop(); see stop_all_live().
            job_queue.stop_all_live()
            # stop() joins threads, so run it off the event loop.
            await asyncio.to_thread(local_capture_manager.stop)
            await bot_manager.stop()
            await job_queue.stop()
            await asyncio.to_thread(search_index.stop_worker)
            await asyncio.to_thread(heartbeat.stop)
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
    from .routes import search as search_router
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
    app.include_router(search_router.router)

    return app


# Module-level app instance (for uvicorn)
app = create_app()
