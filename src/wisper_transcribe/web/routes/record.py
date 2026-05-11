"""Record routes — Phase 3 implements JSON start/stop API;
Phase 5 adds HTML control panel + recordings list/detail pages.

Path-traversal guards on recording_id follow the CodeQL four-step pattern.

Security: recording control endpoints assume local/trusted network access — see
architecture.md Known Constraints for details.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from wisper_transcribe.campaign_manager import (
    add_member,
    bind_discord_id,
    load_campaigns,
    move_transcript_to_campaign,
)
from wisper_transcribe.config import get_data_dir, load_config
from wisper_transcribe.recording_manager import (
    _validate_recording_id,
    delete_recording,
    load_recordings,
    save_recording,
)
from wisper_transcribe.speaker_manager import enroll_speaker_from_audio_dir
from wisper_transcribe.web.routes import get_bot_manager, templates

log = logging.getLogger(__name__)

router = APIRouter()

_NOT_IMPLEMENTED = JSONResponse(
    {"detail": "not implemented"},
    status_code=501,
)


def _recording_to_dict(rec) -> dict:
    return {
        "id": rec.id,
        "status": rec.status,
        "campaign_slug": rec.campaign_slug,
        "voice_channel_id": rec.voice_channel_id,
        "guild_id": rec.guild_id,
        "started_at": rec.started_at.isoformat() if rec.started_at else None,
        "ended_at": rec.ended_at.isoformat() if rec.ended_at else None,
        "discord_speakers": rec.discord_speakers,
        "segment_count": len(rec.segment_manifest),
    }


# ---------------------------------------------------------------------------
# JSON API — bot control
# ---------------------------------------------------------------------------

@router.post("/api/record/start")
async def record_start(request: Request):
    """Start a recording session."""
    bm = get_bot_manager(request)
    if bm is None:
        return JSONResponse({"detail": "bot manager not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)

    voice_channel_id = body.get("voice_channel_id", "")
    if not voice_channel_id:
        return JSONResponse({"detail": "voice_channel_id is required"}, status_code=400)

    try:
        recording = await bm.start_session(
            campaign_slug=body.get("campaign_slug"),
            voice_channel_id=str(voice_channel_id),
            guild_id=str(body.get("guild_id", "")),
        )
    except RuntimeError:
        return JSONResponse({"detail": "recording already in progress"}, status_code=409)

    return JSONResponse(_recording_to_dict(recording), status_code=201)


@router.post("/api/record/stop")
async def record_stop(request: Request):
    """Stop the active recording session."""
    bm = get_bot_manager(request)
    if bm is None:
        return JSONResponse({"detail": "bot manager not available"}, status_code=503)
    if bm.active_recording is None:
        return JSONResponse({"detail": "no active recording"}, status_code=400)

    recording = bm.active_recording
    await bm.stop_session()
    return JSONResponse(_recording_to_dict(recording))


@router.get("/api/record/status")
async def record_status(request: Request):
    """Return current bot + recording status. (stub)"""
    return _NOT_IMPLEMENTED


@router.get("/api/record/channels")
async def record_channels(request: Request):
    """List guilds and voice channels visible to the bot. (stub)"""
    return _NOT_IMPLEMENTED


# ---------------------------------------------------------------------------
# JSON API — recordings CRUD
# ---------------------------------------------------------------------------

@router.get("/api/recordings")
async def recordings_list(request: Request):
    """List all recordings, optionally filtered by campaign. (stub)"""
    return _NOT_IMPLEMENTED


@router.get("/api/recordings/{recording_id}")
async def recording_detail_api(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)
    return _NOT_IMPLEMENTED


@router.post("/api/recordings/{recording_id}/transcribe")
async def recording_transcribe(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)
    return _NOT_IMPLEMENTED


@router.post("/api/recordings/{recording_id}/delete")
async def recording_delete_api(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)
    return _NOT_IMPLEMENTED


# ---------------------------------------------------------------------------
# HTML — control panel
# ---------------------------------------------------------------------------

@router.get("/record", response_class=HTMLResponse)
async def record_page(request: Request) -> HTMLResponse:
    bm = get_bot_manager(request)
    data_dir = get_data_dir()
    campaigns = load_campaigns(data_dir)
    cfg = load_config()
    active_recording = bm.active_recording if bm else None
    return templates.TemplateResponse(
        request,
        "record.html",
        {
            "request": request,
            "campaigns": campaigns,
            "active_recording": active_recording,
            "discord_presets": cfg.get("discord_presets", []),
            "default_guild": cfg.get("discord_default_guild", ""),
            "default_channel": cfg.get("discord_default_channel", ""),
        },
    )


@router.get("/record/sse")
async def record_sse(request: Request) -> StreamingResponse:
    """SSE stream of live recording session status (Pattern 6)."""
    bm = get_bot_manager(request)

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            if bm is None or bm.active_recording is None:
                payload = {"type": "status", "status": "idle"}
            else:
                rec = bm.active_recording
                payload = {
                    "type": "status",
                    "status": rec.status,
                    "recording_id": rec.id,
                    "segment_count": len(rec.segment_manifest),
                    "speakers": list(rec.discord_speakers.keys()),
                    "started_at": rec.started_at.isoformat() if rec.started_at else None,
                }
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/record/start", response_class=HTMLResponse)
async def record_start_html(
    request: Request,
    voice_channel_id: Annotated[str, Form()],
    guild_id: Annotated[str, Form()] = "",
    campaign_slug: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """HTML form handler: start a session and redirect back to /record."""
    bm = get_bot_manager(request)
    if bm is None:
        return RedirectResponse(url="/record?error=unavailable", status_code=303)
    if not voice_channel_id.strip():
        return RedirectResponse(url="/record?error=missing_channel", status_code=303)

    try:
        await bm.start_session(
            campaign_slug=campaign_slug.strip() or None,
            voice_channel_id=voice_channel_id.strip(),
            guild_id=guild_id.strip(),
        )
    except RuntimeError:
        return RedirectResponse(url="/record?error=already_active", status_code=303)

    return RedirectResponse(url="/record", status_code=303)


@router.post("/record/stop", response_class=HTMLResponse)
async def record_stop_html(request: Request) -> RedirectResponse:
    """HTML form handler: stop the active session and redirect back to /record."""
    bm = get_bot_manager(request)
    if bm is None or bm.active_recording is None:
        return RedirectResponse(url="/record?error=no_session", status_code=303)
    await bm.stop_session()
    return RedirectResponse(url="/record", status_code=303)


# ---------------------------------------------------------------------------
# HTML — recordings list + detail
# ---------------------------------------------------------------------------

@router.get("/recordings", response_class=HTMLResponse)
async def recordings_list_html(request: Request) -> HTMLResponse:
    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)
    campaigns = load_campaigns(data_dir)
    sorted_recordings = sorted(
        recordings.values(),
        key=lambda r: r.started_at or r.started_at,
        reverse=True,
    )
    return templates.TemplateResponse(
        request,
        "recordings.html",
        {
            "request": request,
            "recordings": sorted_recordings,
            "campaigns": campaigns,
        },
    )


@router.get("/recordings/{recording_id}/live")
async def recording_live(recording_id: str, request: Request) -> JSONResponse:
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)
    return JSONResponse({"detail": "not implemented in v1"}, status_code=501)


@router.get("/recordings/{recording_id}", response_class=HTMLResponse)
async def recording_detail_html(recording_id: str, request: Request) -> HTMLResponse:
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return HTMLResponse(content="Invalid recording ID", status_code=400)

    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)
    recording = recordings.get(safe_id)
    if recording is None:
        return RedirectResponse(url="/recordings?error=not_found", status_code=303)

    campaigns = load_campaigns(data_dir)
    return templates.TemplateResponse(
        request,
        "recording_detail.html",
        {
            "request": request,
            "recording": recording,
            "campaigns": campaigns,
        },
    )


@router.post("/recordings/{recording_id}/delete", response_class=HTMLResponse)
async def recording_delete_html(recording_id: str, request: Request) -> RedirectResponse:
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return HTMLResponse(content="Invalid recording ID", status_code=400)

    delete_recording(safe_id, get_data_dir())
    return RedirectResponse(url="/recordings", status_code=303)


@router.post("/recordings/{recording_id}/enroll")
async def recording_enroll_html(
    recording_id: str,
    request: Request,
    discord_user_id: Annotated[str, Form()] = "",
    profile_name: Annotated[str, Form()] = "",
):
    """Enroll an unbound speaker from a recording into a wisper profile."""
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)

    # Validate discord_user_id: numeric snowflake 15-20 digits
    stripped_uid = discord_user_id.strip()
    if not stripped_uid or not re.match(r"^\d{15,20}$", stripped_uid):
        return JSONResponse({"detail": "invalid discord_user_id"}, status_code=400)

    # CodeQL path guard on stripped_uid before it enters any path construction
    _uid_guard_base = os.path.abspath("_uid_guard") + os.sep
    safe_uid = os.path.basename(os.path.abspath(os.path.join(_uid_guard_base, stripped_uid)))

    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)
    recording = recordings.get(safe_id)
    if recording is None:
        return RedirectResponse(url="/recordings?error=not_found", status_code=303)

    if safe_uid not in recording.unbound_speakers:
        return JSONResponse({"detail": "speaker not in unbound list"}, status_code=409)

    profile_key = profile_name.strip().lower().replace(" ", "_")
    if not profile_key:
        return RedirectResponse(
            url=f"/recordings/{recording.id}?error=enroll_failed", status_code=303
        )

    per_user_dir = data_dir / "recordings" / recording.id / "per-user" / safe_uid

    try:
        enroll_speaker_from_audio_dir(
            name=profile_key,
            display_name=profile_name.strip(),
            role="player",
            per_user_dir=per_user_dir,
            data_dir=data_dir,
        )
    except Exception:
        log.warning("Speaker enrollment from audio dir failed", exc_info=True)
        return RedirectResponse(
            url=f"/recordings/{recording.id}?error=enroll_failed", status_code=303
        )

    recording.unbound_speakers = [
        uid for uid in recording.unbound_speakers if uid != safe_uid
    ]
    recording.discord_speakers[safe_uid] = profile_key
    save_recording(recording, data_dir)

    if recording.campaign_slug:
        try:
            campaigns = load_campaigns(data_dir)
            if recording.campaign_slug in campaigns:
                campaign = campaigns[recording.campaign_slug]
                if profile_key not in campaign.members:
                    add_member(recording.campaign_slug, profile_key, data_dir=data_dir)
                bind_discord_id(
                    recording.campaign_slug, profile_key, safe_uid, data_dir=data_dir
                )
        except Exception:
            log.warning("Failed to auto-bind discord ID to campaign", exc_info=True)

    return RedirectResponse(url=f"/recordings/{recording.id}", status_code=303)


# ---------------------------------------------------------------------------
# HTML — transcribe hand-off
# ---------------------------------------------------------------------------


@router.post("/recordings/{recording_id}/transcribe", response_class=HTMLResponse)
async def recording_transcribe_html(recording_id: str, request: Request) -> RedirectResponse:
    """Hand off a completed recording to the transcription JobQueue."""
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return HTMLResponse(content="Invalid recording ID", status_code=400)

    from wisper_transcribe.web.routes.transcribe import _default_output_dir

    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)
    recording = recordings.get(safe_id)
    if recording is None:
        return RedirectResponse(url="/recordings?error=not_found", status_code=303)

    if recording.status not in ("completed", "transcribed"):
        return RedirectResponse(url=f"/recordings/{recording.id}?error=not_ready", status_code=303)

    if recording.combined_path is None or not recording.combined_path.exists():
        return RedirectResponse(url=f"/recordings/{recording.id}?error=no_audio", status_code=303)

    # Copy combined.wav to output dir so the transcript lands alongside existing ones
    output_dir = _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{recording.id}.wav"
    shutil.copy2(str(recording.combined_path), str(dest))

    # Build the post-completion callback: auto-associate transcript with campaign
    def _on_complete(job):
        _recordings = load_recordings(data_dir)
        rec = _recordings.get(recording.id)
        if rec is None:
            return
        rec.status = "transcribed"
        if job.output_path:
            rec.transcript_path = Path(job.output_path)
            stem = Path(job.output_path).stem
            if rec.campaign_slug:
                try:
                    move_transcript_to_campaign(stem, rec.campaign_slug, data_dir)
                except Exception:
                    log.warning("Failed to move transcript to campaign in on_complete", exc_info=True)
        save_recording(rec, data_dir)

    queue = request.app.state.job_queue
    job = queue.submit(
        str(dest),
        original_stem=recording.id,
        output_dir=str(output_dir),
        campaign=recording.campaign_slug or "",
        on_complete=_on_complete,
    )

    recording.job_id = job.id
    recording.status = "transcribing"
    save_recording(recording, data_dir)

    return RedirectResponse(url=f"/recordings/{recording.id}", status_code=303)
