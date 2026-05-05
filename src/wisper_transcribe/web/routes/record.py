"""Record API routes — Phase 3 implements start/stop; remaining stubs are 501.

Path-traversal guards on recording_id follow the CodeQL four-step pattern.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from wisper_transcribe.recording_manager import _validate_recording_id
from wisper_transcribe.web.routes import get_bot_manager

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
# Bot control
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
    except RuntimeError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)

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
# Recordings CRUD
# ---------------------------------------------------------------------------

@router.get("/api/recordings")
async def recordings_list(request: Request):
    """List all recordings, optionally filtered by campaign. (stub)"""
    return _NOT_IMPLEMENTED


@router.get("/api/recordings/{recording_id}")
async def recording_detail(recording_id: str, request: Request):
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
async def recording_delete(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)
    return _NOT_IMPLEMENTED
