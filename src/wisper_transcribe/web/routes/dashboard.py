"""Dashboard route — job queue overview and system status."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import get_queue, templates
from wisper_transcribe.config import get_data_dir, get_device, load_config  # noqa: F401 (get_data_dir used in template context and tests)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    queue = get_queue(request)
    config = load_config()
    device = get_device()
    hf_token_set = bool(config.get("hf_token") or __import__("os").environ.get("HUGGINGFACE_TOKEN"))

    # Count transcripts in output dirs
    output_dir = Path(get_data_dir()) / "output" if not (Path.cwd() / "output").exists() else Path.cwd() / "output"
    transcript_count = len(list(output_dir.glob("*.md"))) if output_dir.exists() else 0

    # Count enrolled speakers
    from wisper_transcribe.speaker_manager import load_profiles
    speaker_count = len(load_profiles())

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "jobs": queue.list_recent(),
            "active_count": queue.active_count(),
            "transcript_count": transcript_count,
            "speaker_count": speaker_count,
            "device": device,
            "model": config.get("model", "large-v3-turbo"),
            "hf_token_set": hf_token_set,
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_partial(request: Request) -> HTMLResponse:
    """HTMX partial: job table rows (polled every 2s when jobs are active)."""
    queue = get_queue(request)
    return templates.TemplateResponse(
        request,
        "partials/job_rows.html",
        {"request": request, "jobs": queue.list_recent()},
    )
