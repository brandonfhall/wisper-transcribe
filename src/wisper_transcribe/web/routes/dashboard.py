"""Dashboard route — job queue overview and system status."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import get_queue, templates
from wisper_transcribe.config import (
    get_device,
    get_llm_api_key,
    load_config,
    resolve_llm_model,
)
from wisper_transcribe.path_utils import get_output_dir

router = APIRouter()

RECENT_JOBS = 20
HISTORY_PAGE_SIZE = 50


def recent_jobs(queue, limit: int = RECENT_JOBS) -> list:
    """The newest jobs: live ones from the queue, the rest from job history
    (which survives restarts and the queue's 50-job cap)."""
    from wisper_transcribe import job_history

    live = queue.list_recent(limit)
    seen = {j.id for j in live}
    try:
        stored, _ = job_history.list_jobs(per_page=limit)
    except Exception:
        stored = []
    merged = live + [r for r in stored if r.id not in seen]
    merged.sort(key=lambda j: j.created_at, reverse=True)
    return merged[:limit]


def job_campaigns(jobs: list) -> dict[str, str]:
    """Job id -> campaign display name, for the job table's Campaign column.

    Same rule as job history: the job's transcript's current campaign, else
    its recording's, else the campaign it was submitted with. History rows
    arrive with it already derived (``campaign_name``).
    """
    from wisper_transcribe.campaign_manager import get_campaign_for_transcript, load_campaigns
    from wisper_transcribe.recording_manager import load_recording

    names = {slug: c.display_name for slug, c in load_campaigns().items()}
    out: dict[str, str] = {}
    for job in jobs:
        name = getattr(job, "campaign_name", None)
        if name is None and hasattr(job, "kwargs"):
            slug = None
            path = (getattr(job, "output_path", None) or getattr(job, "llm_transcript_path", None)
                    or getattr(job, "enroll_md_path", None))
            if path:
                from wisper_transcribe.transcript_store import locate_path
                loc = locate_path(Path(path))
                if loc is not None:
                    slug = get_campaign_for_transcript(loc.id)
            rid = getattr(job, "recording_id", None) or getattr(job, "live_recording_id", None)
            if slug is None and rid:
                rec = load_recording(rid)
                slug = rec.campaign_slug if rec else None
            if slug is None:
                slug = job.kwargs.get("campaign") or job.kwargs.get("slug")
            name = names.get(slug) if slug else None
        if name:
            out[job.id] = name
    return out


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    queue = get_queue(request)
    config = load_config()
    device = get_device()
    hf_token_set = bool(
        config.get("hf_token")
        or os.environ.get("HUGGINGFACE_TOKEN")
        or os.environ.get("HF_TOKEN")
    )

    # Exclude .summary.md sidecars so this matches the Transcripts page.
    output_dir = get_output_dir()
    transcript_count = len(
        [p for p in output_dir.glob("*.md") if not p.stem.endswith(".summary")]
    ) if output_dir.exists() else 0

    # Count enrolled speakers
    from wisper_transcribe.speaker_manager import load_profiles
    speaker_count = len(load_profiles())

    # LLM summary for the System card. Resolve the model so we display the
    # provider's default when llm_model is blank, rather than an empty cell.
    llm_provider = config.get("llm_provider", "ollama")
    try:
        llm_model = resolve_llm_model(llm_provider, config=config)
    except ValueError:
        llm_model = config.get("llm_model", "") or "(unknown provider)"
    # `llm_ready` flips false when a key-requiring provider has no key reachable
    # via env or saved config — surfaces "needs config" without leaking the key.
    if llm_provider in ("ollama", "lmstudio"):
        llm_ready = True
        llm_status_hint = config.get("llm_endpoint") or ""
    else:
        try:
            llm_ready = bool(get_llm_api_key(llm_provider, config=config))
        except ValueError:
            llm_ready = False
        llm_status_hint = "API key" if llm_ready else "API key missing"

    jobs = recent_jobs(queue)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "jobs": jobs,
            "job_campaigns": job_campaigns(jobs),
            "active_count": queue.active_count(),
            "transcript_count": transcript_count,
            "speaker_count": speaker_count,
            "device": device,
            "model": config.get("model", "large-v3-turbo"),
            "hf_token_set": hf_token_set,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "llm_ready": llm_ready,
            "llm_status_hint": llm_status_hint,
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_partial(request: Request) -> HTMLResponse:
    """HTMX partial: job table rows (polled every 2s when jobs are active)."""
    queue = get_queue(request)
    jobs = recent_jobs(queue)
    return templates.TemplateResponse(
        request,
        "partials/job_rows.html",
        {"request": request, "jobs": jobs, "job_campaigns": job_campaigns(jobs)},
    )


@router.get("/api/sidebar-status", response_class=HTMLResponse)
async def sidebar_status(request: Request) -> HTMLResponse:
    """HTMX partial: sidebar system status (device, jobs count). Polled every 5s."""
    queue = get_queue(request)
    device = get_device()
    active = queue.active_count()
    return templates.TemplateResponse(
        request,
        "partials/sidebar_status.html",
        {"request": request, "device": device, "active_jobs": active},
    )


@router.get("/jobs/history", response_class=HTMLResponse)
async def job_history_page(
    request: Request,
    page: int = 1,
    type: str = "",
    status: str = "",
    transcript: str = "",
    campaign: str = "",
) -> HTMLResponse:
    """Every job ever run, newest first, 50 per page, filterable."""
    from wisper_transcribe import job_history
    from wisper_transcribe.web.jobs import (
        JOB_CAMPAIGN_JOURNAL, JOB_ENROLL, JOB_LIVE, JOB_REFINE, JOB_SPEAKER_RELABEL,
        JOB_SUMMARIZE, JOB_TRANSCRIPTION,
    )

    job_types = (JOB_TRANSCRIPTION, JOB_REFINE, JOB_SUMMARIZE, JOB_ENROLL, JOB_LIVE,
                 JOB_CAMPAIGN_JOURNAL, JOB_SPEAKER_RELABEL)
    type_filter = type if type in job_types else ""
    status_filter = status if status in job_history.JOB_STATUSES else ""
    page = max(1, page)
    records, total = job_history.list_jobs(
        page=page, per_page=HISTORY_PAGE_SIZE, job_type=type_filter or None,
        status=status_filter or None, transcript=transcript or None, campaign=campaign or None,
    )
    pages = max(1, -(-total // HISTORY_PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "job_history.html",
        {
            "request": request, "records": records, "total": total, "page": page, "pages": pages,
            "job_types": job_types, "statuses": job_history.JOB_STATUSES,
            "type_filter": type_filter, "status_filter": status_filter,
            "transcript_filter": transcript, "campaign_filter": campaign,
        },
    )
