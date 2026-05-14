"""Transcribe route — file upload and job management."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse

from ..jobs import COMPLETED, FAILED
from . import get_queue as _get_queue, templates
from wisper_transcribe.campaign_manager import _validate_campaign_slug as _validate_campaign_slug_cm, load_campaigns
from wisper_transcribe.path_utils import get_output_dir, validate_path_component
from wisper_transcribe.web._responses import error_redirect, invalid_input_response

router = APIRouter(prefix="/transcribe")


def _validate_job_id(job_id: str) -> str | None:
    return validate_path_component(job_id, "_guard")



@router.get("", response_class=HTMLResponse)
async def transcribe_form(request: Request) -> HTMLResponse:
    """Render the upload / options form."""
    campaigns = load_campaigns()
    return templates.TemplateResponse(
        request,
        "transcribe.html",
        {"request": request, "campaigns": campaigns},
    )


@router.post("", response_class=HTMLResponse)
async def start_transcribe(
    request: Request,
    file: Annotated[UploadFile, File()],
    model_size: Annotated[str, Form()] = "large-v3-turbo",
    language: Annotated[str, Form()] = "en",
    device: Annotated[str, Form()] = "auto",
    num_speakers: Annotated[Optional[str], Form()] = None,
    min_speakers: Annotated[Optional[str], Form()] = None,
    max_speakers: Annotated[Optional[str], Form()] = None,
    no_diarize: Annotated[bool, Form()] = False,
    compute_type: Annotated[str, Form()] = "auto",
    vad: Annotated[Optional[str], Form()] = None,
    include_timestamps: Annotated[bool, Form()] = True,
    initial_prompt: Annotated[Optional[str], Form()] = None,
    post_refine: Annotated[Optional[str], Form()] = None,
    post_summarize: Annotated[Optional[str], Form()] = None,
    campaign: Annotated[Optional[str], Form()] = None,
    vocab_file: Annotated[Optional[UploadFile], File()] = None,
) -> RedirectResponse:
    """Accept an uploaded audio file, save it to a temp location, enqueue job."""
    # Save uploaded file to a persistent temp location (job must outlive request)
    suffix = Path(file.filename or "audio.mp3").suffix or ".mp3"
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, prefix="wisper_upload_"
    )
    try:
        content = await file.read()
        tmp.write(content)
    finally:
        tmp.close()

    # Parse optional vocab file into hotwords list (same logic as CLI --vocab-file)
    hotwords: Optional[list[str]] = None
    if vocab_file and vocab_file.filename:
        raw = await vocab_file.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        parsed = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        if parsed:
            hotwords = parsed

    # Parse optional integer fields
    def _int_or_none(val: Optional[str]) -> Optional[int]:
        try:
            return int(val) if val else None
        except ValueError:
            return None

    vad_filter: Optional[bool] = None
    if vad == "on":
        vad_filter = True
    elif vad == "off":
        vad_filter = False

    # Always write transcripts to the default output dir so the Transcripts
    # page can find them.  A user-supplied path is not accepted — accepting
    # arbitrary paths from form data would allow writing outside the configured
    # data directory.
    out_path: Path = get_output_dir()

    # Use the original filename stem as a hint so the output .md has a
    # meaningful name instead of a temp-file UUID.
    original_stem = Path(file.filename or "upload").stem

    # Validate campaign slug if provided — use server-side object for redirect URL.
    safe_campaign: Optional[str] = None
    if campaign and campaign.strip():
        safe_campaign = _validate_campaign_slug_cm(campaign.strip())
        if safe_campaign is None:
            return error_redirect("/transcribe", "invalid_campaign")

    queue = _get_queue(request)
    job = queue.submit(
        input_path=tmp.name,
        original_stem=original_stem,
        model_size=model_size,
        language=None if language == "auto" else language,
        device=device,
        num_speakers=_int_or_none(num_speakers),
        min_speakers=_int_or_none(min_speakers),
        max_speakers=_int_or_none(max_speakers),
        no_diarize=no_diarize,
        compute_type=compute_type,
        vad_filter=vad_filter,
        include_timestamps=include_timestamps,
        initial_prompt=initial_prompt or None,
        output_dir=out_path,
        enroll_speakers=False,  # Web enrollment is post-job wizard
        post_refine=(post_refine == "1"),
        post_summarize=(post_summarize == "1"),
        campaign=safe_campaign,
        hotwords=hotwords,
    )

    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> Response:
    """Cancel a pending or running job."""
    safe_id = _validate_job_id(job_id)
    if safe_id is None:
        return invalid_input_response("Invalid job ID")

    queue = _get_queue(request)
    queue.cancel(safe_id)
    # Use server-generated job.id (UUID) instead of safe_id so CodeQL's
    # py/url-redirection taint tracker sees no user-controlled data in the URL.
    job = queue.get(safe_id)
    if job is None:
        return RedirectResponse(url="/transcribe", status_code=303)
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str) -> HTMLResponse:
    """Job status page with SSE log streaming."""
    queue = _get_queue(request)
    job = queue.get(job_id)
    if job is None:
        return HTMLResponse(content="Job not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"request": request, "job": job},
    )


@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: str) -> StreamingResponse:
    """Server-Sent Events stream: streams log lines and final status."""
    queue = _get_queue(request)

    async def event_generator():
        last_line_idx = 0
        last_progress = None
        last_channel_progress: dict[str, str] = {}
        while True:
            if await request.is_disconnected():
                break
            job = queue.get(job_id)
            if job is None:
                yield "event: error\ndata: Job not found\n\n"
                return

            # Send any new log lines
            new_lines = job.log_lines[last_line_idx:]
            for line in new_lines:
                data = json.dumps({"type": "log", "message": line})
                yield f"data: {data}\n\n"
            last_line_idx += len(new_lines)

            # Send overall progress update (sequential mode)
            if job.progress and job.progress != last_progress:
                data = json.dumps({"type": "progress", "message": job.progress})
                yield f"data: {data}\n\n"
                last_progress = job.progress

            # Send per-channel progress updates (parallel mode)
            for channel, msg in job.progress_channels.items():
                if last_channel_progress.get(channel) != msg:
                    data = json.dumps({"type": "channel_progress", "channel": channel, "message": msg})
                    yield f"data: {data}\n\n"
                    last_channel_progress[channel] = msg

            # Send status update
            data = json.dumps({"type": "status", "status": job.status})
            yield f"data: {data}\n\n"

            if job.status in (COMPLETED, FAILED):
                final = json.dumps({
                    "type": "done",
                    "status": job.status,
                    "output_path": job.output_path,
                    "summary_path": job.summary_path,
                    "job_type": job.job_type,
                    "error": job.error,
                })
                yield f"data: {final}\n\n"
                return

            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/jobs/{job_id}/enroll", response_class=HTMLResponse)
async def enroll_form(request: Request, job_id: str) -> Response:
    """Speaker enrollment wizard for a completed job."""
    safe_id = _validate_job_id(job_id)
    if safe_id is None:
        return invalid_input_response("Invalid job ID")

    queue = _get_queue(request)
    job = queue.get(safe_id)
    if job is None:
        return HTMLResponse(content="Job not found", status_code=404)
    if job.status != COMPLETED:
        return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)

    from wisper_transcribe.speaker_manager import load_profiles

    # Parse YAML frontmatter from the output to get detected speakers
    speakers_in_transcript: list[str] = []
    if job.output_path:
        try:
            import yaml
            content = Path(job.output_path).read_text(encoding="utf-8")
            parts = content.split("---")
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1])
                speakers_in_transcript = [
                    s.get("name", "") for s in (fm.get("speakers") or [])
                ]
        except Exception:
            pass

    profiles = load_profiles()

    # Load persisted transcript text snippets for each speaker (written as
    # <stem>_excerpt_<speaker>.txt alongside the clip files).
    import re as _re
    speaker_excerpt_texts: dict[str, str] = {}
    if job.output_path:
        out_dir = Path(job.output_path).parent
        stem = Path(job.output_path).stem
        for speaker_name in speakers_in_transcript:
            safe_label = _re.sub(r"[^\w\-]", "_", speaker_name)
            txt_path = out_dir / f"{stem}_excerpt_{safe_label}.txt"
            if txt_path.exists():
                try:
                    speaker_excerpt_texts[speaker_name] = txt_path.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

    return templates.TemplateResponse(
        request,
        "speaker_enroll.html",
        {
            "request": request,
            "job": job,
            "detected_speakers": speakers_in_transcript,
            "existing_profiles": profiles,
            "speaker_excerpts": job.speaker_excerpts,
            "speaker_excerpt_texts": speaker_excerpt_texts,
        },
    )


@router.get("/jobs/{job_id}/excerpt/{speaker_name}")
async def speaker_excerpt(request: Request, job_id: str, speaker_name: str) -> Response:
    """Serve a short audio clip for a detected speaker (used in enrollment wizard)."""
    if not speaker_name or "\x00" in speaker_name:
        return invalid_input_response("Invalid speaker name")
        
    safe_name = os.path.basename(speaker_name)
    if safe_name != speaker_name or safe_name in {".", ".."}:
        return invalid_input_response("Invalid speaker name")
    queue = _get_queue(request)
    job = queue.get(job_id)

    clip_path: Optional[str] = None
    if job is not None:
        clip_path = job.speaker_excerpts.get(speaker_name)

    # Fallback: scan the output directory for an on-disk excerpt clip so the
    # wizard still works after a server restart (the in-memory job is gone but
    # the files remain alongside the transcript).
    if not clip_path or not Path(clip_path).exists():
        from wisper_transcribe.path_utils import get_output_dir
        import re as _re
        safe_label = _re.sub(r"[^\w\-]", "_", speaker_name)
        out_dir = get_output_dir()
        # Excerpts are named <transcript_stem>_excerpt_<speaker_label>.mp3
        candidates = list(out_dir.glob(f"*_excerpt_{safe_label}.mp3"))
        if candidates:
            clip_path = str(candidates[0])

    if not clip_path or not Path(clip_path).exists():
        return HTMLResponse(content="Excerpt not available", status_code=404)
    return FileResponse(path=clip_path, media_type="audio/mpeg")


@router.post("/jobs/{job_id}/enroll", response_class=HTMLResponse)
async def enroll_submit(request: Request, job_id: str) -> Response:
    """Apply speaker name assignments and regenerate the transcript."""
    safe_id = _validate_job_id(job_id)
    if safe_id is None:
        return invalid_input_response("Invalid job ID")

    queue = _get_queue(request)
    job = queue.get(safe_id)
    if job is None:
        return HTMLResponse(content="Job not found", status_code=404)
    if job.status != COMPLETED or not job.output_path:
        return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)

    form_data = await request.form()
    # Form fields: speaker_<label> = display_name
    renames: dict[str, str] = {}
    for key, value in form_data.items():
        if key.startswith("speaker_") and str(value).strip():
            old_name = key[len("speaker_"):]
            renames[old_name] = str(value).strip()

    if renames:
        from wisper_transcribe.formatter import update_speaker_names
        out_path = Path(job.output_path)
        content = out_path.read_text(encoding="utf-8")
        for old_name, new_name in renames.items():
            content = update_speaker_names(content, old_name, new_name)
        out_path.write_text(content, encoding="utf-8")

        # Enroll each labelled speaker so voice profiles are persisted.
        # old_name is the raw diarization label (e.g. "SPEAKER_00"); new_name is
        # the display name typed in the wizard.  Failures are logged but never
        # surfaced as HTTP errors — the transcript rename already happened.
        if job.diarization_segments:
            import logging
            from wisper_transcribe.speaker_manager import enroll_speaker
            device = job.kwargs.get("device", "cpu")
            if device == "auto":
                from wisper_transcribe.config import get_device
                device = get_device()
            campaign_slug = job.kwargs.get("campaign")
            log = logging.getLogger(__name__)
            for old_label, display_name in renames.items():
                profile_key = display_name.lower().replace(" ", "_")
                try:
                    enroll_speaker(
                        name=profile_key,
                        display_name=display_name,
                        role="",
                        audio_path=Path(job.input_path),
                        segments=job.diarization_segments,
                        speaker_label=old_label,
                        device=device,
                    )
                except Exception as exc:
                    log.warning("enroll_speaker failed for %s: %s", display_name, exc)
                    continue
                # Add the newly-enrolled profile to the job's campaign, if any.
                # Mirrors record.py: only add when the slug exists and the profile
                # isn't already a member, so existing role/character entries are
                # never clobbered. Campaign failures never break enrollment.
                if campaign_slug:
                    try:
                        from wisper_transcribe.campaign_manager import (
                            add_member, load_campaigns,
                        )
                        campaigns = load_campaigns()
                        if (campaign_slug in campaigns
                                and profile_key not in campaigns[campaign_slug].members):
                            add_member(campaign_slug, profile_key)
                    except Exception as exc:
                        log.warning(
                            "add_member failed for %s in campaign %s: %s",
                            profile_key, campaign_slug, exc,
                        )

    transcript_name = Path(job.output_path).stem
    return RedirectResponse(url=f"/transcripts/{quote(transcript_name, safe='')}", status_code=303)
