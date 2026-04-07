"""Transcribe route — file upload and job management."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..jobs import COMPLETED, FAILED, JobQueue
from . import templates

router = APIRouter(prefix="/transcribe")


def _get_queue(request: Request) -> JobQueue:
    return request.app.state.job_queue


def _default_output_dir() -> Path:
    """Return the default output directory for web-submitted transcription jobs.

    Mirrors the logic in transcripts._output_dir so transcripts always land
    where the browser can find them.
    """
    from wisper_transcribe.config import get_data_dir

    out = Path("output")
    if not out.exists():
        out = Path(get_data_dir()) / "output"
    out.mkdir(parents=True, exist_ok=True)
    return out


@router.get("", response_class=HTMLResponse)
async def transcribe_form(request: Request) -> HTMLResponse:
    """Render the upload / options form."""
    return templates.TemplateResponse(
        request,
        "transcribe.html",
        {"request": request},
    )


@router.post("", response_class=HTMLResponse)
async def start_transcribe(
    request: Request,
    file: Annotated[UploadFile, File()],
    model_size: Annotated[str, Form()] = "medium",
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
    output_dir: Annotated[Optional[str], Form()] = None,
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

    # Always write transcripts to a known output dir so the Transcripts page
    # can find them.  User-supplied output_dir overrides the default.
    out_path: Path = Path(output_dir) if output_dir else _default_output_dir()

    # Use the original filename stem as a hint so the output .md has a
    # meaningful name instead of a temp-file UUID.
    original_stem = Path(file.filename or "upload").stem

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
    )

    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> RedirectResponse:
    """Cancel a pending or running job."""
    queue = _get_queue(request)
    queue.cancel(job_id)
    return RedirectResponse(url=f"/transcribe/jobs/{job_id}", status_code=303)


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

            # Send progress update if it has changed
            if job.progress and job.progress != last_progress:
                data = json.dumps({"type": "progress", "message": job.progress})
                yield f"data: {data}\n\n"
                last_progress = job.progress

            # Send status update
            data = json.dumps({"type": "status", "status": job.status})
            yield f"data: {data}\n\n"

            if job.status in (COMPLETED, FAILED):
                final = json.dumps({
                    "type": "done",
                    "status": job.status,
                    "output_path": job.output_path,
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
async def enroll_form(request: Request, job_id: str) -> HTMLResponse:
    """Speaker enrollment wizard for a completed job."""
    queue = _get_queue(request)
    job = queue.get(job_id)
    if job is None:
        return HTMLResponse(content="Job not found", status_code=404)
    if job.status != COMPLETED:
        return RedirectResponse(url=f"/transcribe/jobs/{job_id}", status_code=303)

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

    return templates.TemplateResponse(
        request,
        "speaker_enroll.html",
        {
            "request": request,
            "job": job,
            "detected_speakers": speakers_in_transcript,
            "existing_profiles": profiles,
        },
    )


@router.post("/jobs/{job_id}/enroll", response_class=HTMLResponse)
async def enroll_submit(request: Request, job_id: str) -> RedirectResponse:
    """Apply speaker name assignments and regenerate the transcript."""
    queue = _get_queue(request)
    job = queue.get(job_id)
    if job is None or job.status != COMPLETED or not job.output_path:
        return RedirectResponse(url=f"/transcribe/jobs/{job_id}", status_code=303)

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

    transcript_name = Path(job.output_path).stem
    return RedirectResponse(url=f"/transcripts/{transcript_name}", status_code=303)
