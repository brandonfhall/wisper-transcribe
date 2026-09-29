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

from ..jobs import COMPLETED, FAILED, JOB_LIVE, resume_slice
from . import get_local_capture_manager, get_queue as _get_queue, templates
from wisper_transcribe.campaign_manager import _validate_campaign_slug as _validate_campaign_slug_cm, load_campaigns
from wisper_transcribe.path_utils import get_output_dir, validate_path_component
from wisper_transcribe.web._responses import error_redirect, invalid_input_response

router = APIRouter(prefix="/transcribe")


def _validate_job_id(job_id: str) -> str | None:
    return validate_path_component(job_id, "_guard")



#: Model sizes exposed as radio options in transcribe.html. A curated subset
#: of config.MODEL_SIZES — the template does not surface every Whisper size.
_FORM_MODEL_CHOICES = ("small", "medium", "large-v3-turbo")


@router.get("", response_class=HTMLResponse)
async def transcribe_form(request: Request) -> HTMLResponse:
    """Render the upload / options form."""
    from wisper_transcribe.config import load_config

    campaigns = load_campaigns()
    config = load_config()
    # Preselect the configured model, or the form default if the template
    # doesn't offer it.
    selected_model = config.get("model", "large-v3-turbo")
    if selected_model not in _FORM_MODEL_CHOICES:
        selected_model = "large-v3-turbo"
    return templates.TemplateResponse(
        request,
        "transcribe.html",
        {"request": request, "campaigns": campaigns, "selected_model": selected_model},
    )


@router.post("", response_class=HTMLResponse)
async def start_transcribe(
    request: Request,
    file: Annotated[UploadFile, File()],
    model_size: Annotated[str, Form()] = "large-v3-turbo",
    language: Annotated[Optional[str], Form()] = None,
    device: Annotated[str, Form()] = "auto",
    num_speakers: Annotated[Optional[str], Form()] = None,
    min_speakers: Annotated[Optional[str], Form()] = None,
    max_speakers: Annotated[Optional[str], Form()] = None,
    no_diarize: Annotated[bool, Form()] = False,
    compute_type: Annotated[str, Form()] = "auto",
    vad: Annotated[Optional[str], Form()] = None,
    include_timestamps: Annotated[Optional[bool], Form()] = None,
    initial_prompt: Annotated[Optional[str], Form()] = None,
    post_refine: Annotated[Optional[str], Form()] = None,
    post_summarize: Annotated[Optional[str], Form()] = None,
    campaign: Annotated[Optional[str], Form()] = None,
    vocab_file: Annotated[Optional[UploadFile], File()] = None,
) -> RedirectResponse:
    """Accept an uploaded audio file, save it to a temp location, enqueue job."""
    # Validate enums before any file I/O so a bad value never orphans a temp
    # upload. Never echo the value back.
    from wisper_transcribe.config import COMPUTE_TYPES, DEVICES, MODEL_SIZES
    if model_size not in MODEL_SIZES or device not in DEVICES or compute_type not in COMPUTE_TYPES:
        return error_redirect("/transcribe", "invalid_option")

    # Save uploaded file to a persistent temp location (job must outlive request)
    suffix = Path(file.filename or "audio.mp3").suffix or ".mp3"
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, prefix="wisper_upload_"
    )
    try:
        # Stream to disk in 1 MiB chunks; uploads can be multi-GB.
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
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

    # Always use the default output dir; never accept a path from form data.
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
        # Pass "auto" through: process_file treats None as "use config".
        language=language,
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
        post_refine=bool(post_refine),
        post_summarize=bool(post_summarize),
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
    job = queue.get(safe_id)
    if job is not None and job.job_type == JOB_LIVE:
        # JOB_LIVE ignores _cancel_event; stop it the same way the Record
        # page does (clear the live sink, then stop_live). The recording
        # itself keeps running.
        from wisper_transcribe.web.routes.record import _stop_live_transcription

        lcm = get_local_capture_manager(request)
        if lcm is not None:
            _stop_live_transcription(request, lcm, job.live_recording_id or "")
    else:
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

            # Send new log lines; resume_slice maps the absolute index onto
            # the capped list.
            new_lines, last_line_idx = resume_slice(job.log_lines, job.log_lines_dropped, last_line_idx)
            for line in new_lines:
                data = json.dumps({"type": "log", "message": line})
                yield f"data: {data}\n\n"

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
                    # For campaign-journal jobs there is no transcript — the
                    # completion action links to the campaign journal instead.
                    "journal_slug": job.kwargs.get("slug"),
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
    from wisper_transcribe.web.enroll_shared import (
        _load_diar_sidecar,
        resolve_current_names,
        template_current_names,
    )

    # Prefer the job's raw diarization labels (unchanged by renames); fall
    # back to frontmatter when diarization was skipped.
    speakers_in_transcript: list[str] = []
    current_names: dict[str, str] = {}
    if job.diarization_segments:
        seen_order: dict[str, float] = {}
        for seg in job.diarization_segments:
            if seg.speaker not in seen_order:
                seen_order[seg.speaker] = seg.start
        speakers_in_transcript = sorted(seen_order.keys(), key=lambda s: seen_order[s])
        # Current display name per raw label (sidecar speaker_map when
        # present), filtered so untouched inputs start empty.
        if job.output_path:
            out_path = Path(job.output_path)
            diar = _load_diar_sidecar(out_path)
            current_names = template_current_names(
                resolve_current_names(out_path, diar, job.diarization_segments)
            )
    elif job.output_path:
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

    # Warn before submit when the source audio is gone.
    audio_missing = not (job.input_path and Path(job.input_path).exists())

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
            "form_action": f"/transcribe/jobs/{job.id}/enroll",
            "back_url": f"/transcribe/jobs/{job.id}",
            "excerpt_base_url": f"/transcribe/jobs/{job.id}/excerpt",
            "display_name": job.name,
            "detected_speakers": speakers_in_transcript,
            "existing_profiles": profiles,
            "speaker_excerpts": job.speaker_excerpts,
            "speaker_excerpt_texts": speaker_excerpt_texts,
            "current_names": current_names,
            "audio_missing": audio_missing,
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

    # The in-memory clip path may be missing or stale. Fall back to disk, but
    # only within this job's own transcript stem: every transcript has a
    # SPEAKER_00, so a wider glob could serve another transcript's voice. If
    # the job is gone there is no stem to scope to, so 404; the
    # transcript-centric excerpt route serves clips after a restart.
    if job is not None and job.output_path and (not clip_path or not Path(clip_path).exists()):
        # Shared lookup + CodeQL guard (also used by the transcript wizard).
        from wisper_transcribe.web.enroll_shared import find_excerpt_clip

        found = find_excerpt_clip(
            Path(job.output_path).parent, Path(job.output_path).stem, [speaker_name]
        )
        if found is not None:
            clip_path = str(found)

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

    transcript_name = Path(job.output_path).stem
    url = f"/transcripts/{quote(transcript_name, safe='')}"

    if renames:
        # Rename now; embedding extraction runs as a JOB_ENROLL job.
        from wisper_transcribe.web.enroll_shared import apply_renames

        md_path = Path(job.output_path)
        rename_result = apply_renames(md_path, job.diarization_segments, renames)

        if rename_result.groups:
            input_path = Path(job.input_path)
            if not input_path.exists():
                url += "?notice=enroll_audio_missing"
            else:
                device = job.kwargs.get("device", "cpu")
                if device == "auto":
                    from wisper_transcribe.config import get_device
                    device = get_device()

                enroll_job = queue.submit_enroll(
                    md_path=str(md_path),
                    transcript_name=transcript_name,
                    groups=rename_result.groups,
                    device=device,
                )
                return RedirectResponse(url=f"/transcribe/jobs/{enroll_job.id}", status_code=303)

    return RedirectResponse(url=url, status_code=303)
