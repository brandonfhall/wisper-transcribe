"""Record API route stubs — Phase 2.

All endpoints return 501 until Phase 3 wires in BotManager.
Path-traversal guards on recording_id follow the CodeQL four-step pattern.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from wisper_transcribe.recording_manager import _validate_recording_id

router = APIRouter()

_NOT_IMPLEMENTED = JSONResponse(
    {"detail": "not implemented — bot core lands in Phase 3"},
    status_code=501,
)


# ---------------------------------------------------------------------------
# Bot control
# ---------------------------------------------------------------------------

@router.post("/api/record/start")
async def record_start(request: Request):
    """Start a recording session. (stub)"""
    return _NOT_IMPLEMENTED


@router.post("/api/record/stop")
async def record_stop(request: Request):
    """Stop the active recording session. (stub)"""
    return _NOT_IMPLEMENTED


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
