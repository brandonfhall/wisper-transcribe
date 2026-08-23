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
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from wisper_transcribe.campaign_manager import (
    load_campaigns,
    move_transcript_to_campaign,
)
from wisper_transcribe.config import get_data_dir, load_config, save_config
from wisper_transcribe.recording_manager import (
    _validate_recording_id,
    append_marker,
    delete_recording,
    load_recordings,
    save_recording,
)
from wisper_transcribe.web._responses import error_redirect, invalid_input_response
from wisper_transcribe.web.local_capture import (
    ACTIVE_STATUSES,
    enumerate_devices,
    resolve_device_name,
)
from wisper_transcribe.web.routes import get_bot_manager, get_local_capture_manager, get_queue, templates

log = logging.getLogger(__name__)

router = APIRouter()


def _recording_to_dict(rec) -> dict:
    """Serialize a Recording for the JSON API.

    R7: IDs, names, status, timestamps, and derived booleans only -- never a
    raw filesystem path (CLAUDE.md web-route security rules). ``has_audio``
    / ``has_transcript`` let a CLI/API caller know whether ``transcribe`` is
    likely to succeed without exposing where the files actually live.
    """
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
        "unbound_speakers": list(rec.unbound_speakers),
        "job_id": rec.job_id,
        "notes": rec.notes,
        "has_audio": bool(rec.combined_path),
        "has_transcript": bool(rec.transcript_path),
        "source": rec.source,
        "devices": dict(rec.devices),
        "name": rec.name,
        "markers": [{"elapsed_s": m.elapsed_s} for m in rec.markers],
    }


def _current_active_recording(request: Request):
    """Return whichever manager's Recording is currently active, or None.

    "Active" means `.status in ACTIVE_STATUSES` -- never `is not None`.
    Neither manager clears `active_recording` back to None once a session
    finishes (both mirror BotManager's original behaviour here), so a
    status check is required regardless of which manager is asked. Used by
    every place that needs "is a recording live right now" without caring
    which manager owns it (the Record page, the status SSE stream).
    """
    for mgr in (get_bot_manager(request), get_local_capture_manager(request)):
        if mgr is None:
            continue
        rec = mgr.active_recording
        if rec is not None and rec.status in ACTIVE_STATUSES:
            return rec
    return None


def _other_session_active(request: Request, this_manager) -> bool:
    """True if a manager other than `this_manager` has an active session.

    Mutual exclusion: only one capture session (Discord or local) may run
    at a time across both managers.
    """
    for mgr in (get_bot_manager(request), get_local_capture_manager(request)):
        if mgr is None or mgr is this_manager:
            continue
        rec = mgr.active_recording
        if rec is not None and rec.status in ACTIVE_STATUSES:
            return True
    return False


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

    if _other_session_active(request, bm):
        return JSONResponse({"detail": "recording already in progress"}, status_code=409)

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
    """Current active-recording status, any source (Discord or local).

    Powers the global recording-status banner (`base.html` + `app.js`)
    that keeps a live session's Stop control visible while navigating
    away from `/record` -- not just this JSON API's own consumers.
    `{"active": false}` when idle; otherwise `_recording_to_dict()` plus
    `"active": true`.
    """
    rec = _current_active_recording(request)
    if rec is None:
        return JSONResponse({"active": False})
    payload = _recording_to_dict(rec)
    payload["active"] = True
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# JSON API — local capture control
# ---------------------------------------------------------------------------

def _start_live_transcription(
    request: Request, lcm, recording, data_dir: Path, mic_profile_key: str = ""
) -> None:
    """Submit a Phase 2 JOB_LIVE job for a just-started local session and
    wire its ring buffer onto the capture manager's live sink.

    `mic_profile_key` (Phase 3, "this is me") looks up an enrolled
    profile's display_name to use for mic-dominant lines instead of the
    generic "You" -- an unknown/blank key just falls back to "You"; never
    lets a bad profile key fail the live-job submission.

    Best-effort: a failure here must not fail the already-started capture
    session -- logged and swallowed. The recording still records fine
    without a live preview; only the near-real-time transcript is missing.
    """
    try:
        queue = get_queue(request)
        cfg = load_config()

        mic_label = "You"
        if mic_profile_key:
            from wisper_transcribe.speaker_manager import load_profiles

            profile = load_profiles().get(mic_profile_key)
            if profile is not None:
                mic_label = profile.display_name

        live_output_path = data_dir / "recordings" / recording.id / "live_transcript.md"
        job = queue.submit_live(
            recording_id=recording.id,
            output_path=str(live_output_path),
            model_size=cfg.get("model", "large-v3-turbo"),
            device=cfg.get("device", "auto"),
            compute_type=cfg.get("compute_type", "auto"),
            language=cfg.get("language", "en"),
            mic_label=mic_label,
        )
        lcm.set_live_sink(job.live_ring_buffer.push)
    except Exception:
        log.warning(
            "Failed to start live transcription for recording %s", recording.id, exc_info=True
        )


def _stop_live_transcription(request: Request, lcm, recording_id: str) -> None:
    """Detach the live sink and signal the JOB_LIVE job (if any) to end."""
    try:
        lcm.set_live_sink(None)
    except Exception:
        pass
    try:
        queue = get_queue(request)
        job = queue.find_live_job_for_recording(recording_id)
        if job is not None:
            queue.stop_live(job.id)
    except Exception:
        log.warning(
            "Failed to stop live transcription job for recording %s", recording_id, exc_info=True
        )


def _remember_mic_profile_default(mic_profile_key: str) -> None:
    """Persist the most recently selected "this is me" profile so it's
    pre-selected on the next local-capture session.

    The local mic is the same person's voice the large majority of
    sessions, so remembering the last choice (including "— Label my lines
    'You' —", i.e. blank) saves reselecting it every time while still
    letting a one-off different selection stick as the new default.
    """
    cfg = load_config()
    if cfg.get("default_mic_profile_key", "") != mic_profile_key:
        cfg["default_mic_profile_key"] = mic_profile_key
        save_config(cfg)


@router.get("/api/record/devices")
async def record_devices(request: Request):
    """Enumerate local mic + system-audio (loopback) devices for the picker.

    Always 200 -- `available: false` when `soundcard` is not importable or
    enumeration otherwise fails, so the Record page can hide the Local
    section rather than the caller having to handle a 5xx.
    """
    return JSONResponse(enumerate_devices())


# int16 RMS -- generous headroom above real speech levels observed in
# testing (~250-1440), just to keep a fat-fingered slider value sane.
_MAX_NOISE_FLOOR_RMS = 5000.0


@router.post("/api/record/live-noise-floor")
async def record_live_noise_floor(request: Request):
    """Live-update the noise floor of the running local session's JOB_LIVE
    job (Record page slider). No effect on a Discord session -- those
    don't run JOB_LIVE at all.
    """
    lcm = get_local_capture_manager(request)
    if lcm is None or lcm.active_recording is None or lcm.active_recording.status not in ACTIVE_STATUSES:
        return JSONResponse({"detail": "no active local session"}, status_code=400)

    try:
        body = await request.json()
        noise_floor = float(body.get("noise_floor"))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "noise_floor must be a number"}, status_code=400)
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)

    if not (0.0 <= noise_floor <= _MAX_NOISE_FLOOR_RMS):
        return JSONResponse(
            {"detail": f"noise_floor must be between 0 and {_MAX_NOISE_FLOOR_RMS:.0f}"},
            status_code=400,
        )

    queue = get_queue(request)
    job = queue.find_live_job_for_recording(lcm.active_recording.id)
    if job is None:
        return JSONResponse({"detail": "no active live transcription job"}, status_code=404)

    queue.set_live_noise_floor(job.id, noise_floor)
    return JSONResponse({"noise_floor": noise_floor})


@router.post("/record/marker")
async def record_marker(request: Request):
    """Flag the current moment of the active recording (Record page's "Add
    marker" button) -- any source, not local-only. No label, just a
    timestamp: `append_marker()` persists it to `Recording.markers`, and
    the client drops a matching flagged line straight into the live ticker
    on a successful response (see wisperTickerAppendMarker in app.js) --
    no page reload, since that would disrupt mid-session use.
    """
    rec = _current_active_recording(request)
    if rec is None:
        return JSONResponse({"detail": "no active recording"}, status_code=400)

    marker = append_marker(rec.id, get_data_dir())
    return JSONResponse({"elapsed_s": marker.elapsed_s})


_MAX_SESSION_NAME_LEN = 200


def _clean_session_name(raw: str) -> Optional[str]:
    """Normalize a client-supplied session name: trimmed, capped length,
    blank collapses to None. Display-only -- never touches a file path
    (`Recording.id`, a server-generated uuid4, backs the on-disk directory),
    so no path-traversal-style sanitization is needed here."""
    cleaned = raw.strip()[:_MAX_SESSION_NAME_LEN]
    return cleaned or None


@router.post("/api/record/start-local")
async def record_start_local(request: Request):
    """Start a local mic + system-audio capture session."""
    lcm = get_local_capture_manager(request)
    if lcm is None:
        return JSONResponse({"detail": "local capture unavailable"}, status_code=503)

    devices = enumerate_devices()
    if not devices["available"]:
        return JSONResponse({"detail": "local capture unavailable"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)

    mic_id = str(body.get("mic_id", ""))
    system_id = str(body.get("system_id", ""))
    if not mic_id or not system_id:
        return JSONResponse({"detail": "mic_id and system_id are required"}, status_code=400)

    if _other_session_active(request, lcm):
        return JSONResponse({"detail": "recording already in progress"}, status_code=409)

    mic_name = resolve_device_name(devices["microphones"], mic_id)
    system_name = resolve_device_name(devices["loopbacks"], system_id)

    try:
        recording = lcm.start_session(
            body.get("campaign_slug"),
            mic_id,
            system_id,
            mic_name=mic_name,
            system_name=system_name,
            name=_clean_session_name(str(body.get("name", ""))),
        )
    except RuntimeError:
        return JSONResponse({"detail": "recording already in progress"}, status_code=409)

    mic_profile_key = str(body.get("mic_profile_key", ""))
    _remember_mic_profile_default(mic_profile_key)
    _start_live_transcription(request, lcm, recording, get_data_dir(), mic_profile_key=mic_profile_key)
    return JSONResponse(_recording_to_dict(recording), status_code=201)


@router.post("/api/record/stop-local")
async def record_stop_local(request: Request):
    """Stop the active local capture session. Mirrors `/api/record/stop`."""
    lcm = get_local_capture_manager(request)
    if lcm is None:
        return JSONResponse({"detail": "local capture unavailable"}, status_code=503)
    if lcm.active_recording is None or lcm.active_recording.status not in ACTIVE_STATUSES:
        return JSONResponse({"detail": "no active recording"}, status_code=400)

    recording = lcm.active_recording
    # LocalCaptureManager.stop_session() joins threads -- blocking, so it
    # must run off the event loop (see the module docstring).
    await asyncio.to_thread(lcm.stop_session)
    _stop_live_transcription(request, lcm, recording.id)
    return JSONResponse(_recording_to_dict(recording))


_DISCORD_API = "https://discord.com/api/v10"
_VOICE_CHANNEL = 2  # Discord channel type for GUILD_VOICE


@router.get("/api/record/channels")
async def record_channels(request: Request):
    """List guilds and voice channels visible to the bot via Discord REST API."""
    import httpx

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        token = load_config().get("discord_bot_token", "")
    if not token:
        return JSONResponse({"error": "no_token", "guilds": []})

    headers = {"Authorization": f"Bot {token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{_DISCORD_API}/users/@me/guilds", headers=headers)
            if r.status_code == 401:
                return JSONResponse({"error": "invalid_token", "guilds": []})
            r.raise_for_status()
            guilds = r.json()[:20]  # cap to avoid rate-limit issues

            async def _fetch_voice(guild: dict) -> dict:
                try:
                    cr = await client.get(
                        f"{_DISCORD_API}/guilds/{guild['id']}/channels", headers=headers
                    )
                    voice = sorted(
                        [{"id": c["id"], "name": c["name"]}
                         for c in (cr.json() if cr.status_code == 200 else [])
                         if c.get("type") == _VOICE_CHANNEL],
                        key=lambda c: c["name"],
                    )
                except Exception:
                    voice = []
                return {"id": guild["id"], "name": guild["name"], "voice_channels": voice}

            results = await asyncio.gather(*[_fetch_voice(g) for g in guilds])
        return JSONResponse({"guilds": list(results)})
    except Exception:
        log.warning("Failed to fetch Discord channels from API", exc_info=True)
        return JSONResponse({"error": "fetch_failed", "guilds": []})


# ---------------------------------------------------------------------------
# JSON API — recordings CRUD
# ---------------------------------------------------------------------------
#
# R7: these four endpoints back `wisper record list/show/transcribe/delete`
# (cli.py) -- they used to be 501 stubs, so every documented `record`
# subcommand except start/stop always failed. Each delegates to the exact
# same manager functions / job-submission path the working HTML routes
# (`/recordings`, `/recordings/{id}/transcribe`, `/recordings/{id}/delete`)
# already use, via `_submit_recording_transcription()` below for the
# transcribe hand-off. Responses are generic error codes (never `str(exc)`
# or a filesystem path) per CLAUDE.md's web route security rules.

@router.get("/api/recordings")
async def recordings_list(request: Request):
    """List all recordings, optionally filtered by `?campaign=<slug>`."""
    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)

    campaign = request.query_params.get("campaign")
    values = list(recordings.values())
    if campaign:
        values = [r for r in values if r.campaign_slug == campaign]

    sorted_recordings = sorted(
        values,
        key=lambda r: r.started_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return JSONResponse({"recordings": [_recording_to_dict(r) for r in sorted_recordings]})


@router.get("/api/recordings/{recording_id}")
async def recording_detail_api(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    data_dir = get_data_dir()
    recording = load_recordings(data_dir).get(safe_id)
    if recording is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    return JSONResponse(_recording_to_dict(recording))


@router.post("/api/recordings/{recording_id}/transcribe")
async def recording_transcribe(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    data_dir = get_data_dir()
    recording = load_recordings(data_dir).get(safe_id)
    if recording is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    job, error = _submit_recording_transcription(recording, request, data_dir)
    if error is not None:
        # not_ready: recording hasn't finished / already transcribing/failed.
        # no_audio: recording has no combined_path to hand off.
        status_code = 409 if error == "not_ready" else 422
        return JSONResponse({"error": error}, status_code=status_code)

    return JSONResponse({"id": recording.id, "job_id": job.id, "status": "transcribing"}, status_code=202)


@router.post("/api/recordings/{recording_id}/delete")
async def recording_delete_api(recording_id: str, request: Request):
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)

    data_dir = get_data_dir()
    recording = load_recordings(data_dir).get(safe_id)
    if recording is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    # Same removal semantics as the HTML delete route: index entry only,
    # audio/transcript files on disk are left in place.
    delete_recording(recording.id, data_dir)
    return JSONResponse({"id": recording.id, "deleted": True})


# ---------------------------------------------------------------------------
# HTML — control panel
# ---------------------------------------------------------------------------

@router.get("/record", response_class=HTMLResponse)
async def record_page(request: Request) -> HTMLResponse:
    from wisper_transcribe.speaker_manager import load_profiles

    data_dir = get_data_dir()
    campaigns = load_campaigns(data_dir)
    cfg = load_config()
    active_recording = _current_active_recording(request)
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
            "local_devices": enumerate_devices(),
            "speaker_profiles": load_profiles(data_dir),
            "default_mic_profile_key": cfg.get("default_mic_profile_key", ""),
        },
    )


@router.get("/record/sse")
async def record_sse(request: Request) -> StreamingResponse:
    """SSE stream of live recording session status (Pattern 6)."""

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            rec = _current_active_recording(request)
            if rec is None:
                payload = {"type": "status", "status": "idle"}
            else:
                payload = {
                    "type": "status",
                    "status": rec.status,
                    "recording_id": rec.id,
                    "source": rec.source,
                    "segment_count": len(rec.segment_manifest),
                    "speakers": list(rec.discord_speakers.keys()),
                    "started_at": rec.started_at.isoformat() if rec.started_at else None,
                }
                if rec.source == "local":
                    lcm = get_local_capture_manager(request)
                    if lcm is not None:
                        levels = lcm.get_and_reset_levels()
                        payload["mic_rms"] = levels["mic"]
                        payload["system_rms"] = levels["system"]
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
        return error_redirect("/record", "unavailable")
    if not voice_channel_id.strip():
        return error_redirect("/record", "missing_channel")
    if _other_session_active(request, bm):
        return error_redirect("/record", "already_active")

    try:
        await bm.start_session(
            campaign_slug=campaign_slug.strip() or None,
            voice_channel_id=voice_channel_id.strip(),
            guild_id=guild_id.strip(),
        )
    except RuntimeError:
        return error_redirect("/record", "already_active")

    return RedirectResponse(url="/record", status_code=303)


@router.post("/record/stop", response_class=HTMLResponse)
async def record_stop_html(request: Request) -> RedirectResponse:
    """HTML form handler: stop the active session and redirect back to /record."""
    bm = get_bot_manager(request)
    if bm is None or bm.active_recording is None:
        return error_redirect("/record", "no_session")
    await bm.stop_session()
    return RedirectResponse(url="/record", status_code=303)


@router.post("/record/start-local", response_class=HTMLResponse)
async def record_start_local_html(
    request: Request,
    mic_id: Annotated[str, Form()],
    system_id: Annotated[str, Form()],
    campaign_slug: Annotated[str, Form()] = "",
    mic_profile_key: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """HTML form handler: start a local capture session and redirect back to /record."""
    lcm = get_local_capture_manager(request)
    if lcm is None:
        return error_redirect("/record", "unavailable")
    if not mic_id.strip() or not system_id.strip():
        return error_redirect("/record", "missing_device")

    devices = enumerate_devices()
    if not devices["available"]:
        return error_redirect("/record", "unavailable")
    if _other_session_active(request, lcm):
        return error_redirect("/record", "already_active")

    mic_name = resolve_device_name(devices["microphones"], mic_id.strip())
    system_name = resolve_device_name(devices["loopbacks"], system_id.strip())

    try:
        recording = lcm.start_session(
            campaign_slug.strip() or None,
            mic_id.strip(),
            system_id.strip(),
            mic_name=mic_name,
            system_name=system_name,
            name=_clean_session_name(name),
        )
    except RuntimeError:
        return error_redirect("/record", "already_active")

    _remember_mic_profile_default(mic_profile_key.strip())
    _start_live_transcription(
        request, lcm, recording, get_data_dir(), mic_profile_key=mic_profile_key.strip()
    )
    return RedirectResponse(url="/record", status_code=303)


@router.post("/record/stop-local", response_class=HTMLResponse)
async def record_stop_local_html(request: Request) -> RedirectResponse:
    """HTML form handler: stop the active local session and redirect back to /record."""
    lcm = get_local_capture_manager(request)
    if lcm is None or lcm.active_recording is None or lcm.active_recording.status not in ACTIVE_STATUSES:
        return error_redirect("/record", "no_session")
    recording_id = lcm.active_recording.id
    # LocalCaptureManager.stop_session() joins threads -- blocking, so it
    # must run off the event loop (see the module docstring).
    await asyncio.to_thread(lcm.stop_session)
    _stop_live_transcription(request, lcm, recording_id)
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
        key=lambda r: r.started_at or datetime.min.replace(tzinfo=timezone.utc),
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
async def recording_live(recording_id: str, request: Request):
    """SSE stream of committed live-transcript lines (Phase 2).

    Index-based resume, same pattern as the R14 job-log stream
    (`GET /jobs/{id}/stream`): `job.live_lines_dropped` translates the
    absolute count of lines produced so far into a valid slice of whatever
    is still retained after the `_MAX_LIVE_LINES` cap trims the oldest.

    Once no active `JOB_LIVE` job exists for this recording (never
    started, or the session already ended), this sends a `snapshot` signal
    (only if `live_transcript.md` exists on disk -- lets the client
    distinguish "session ended with a draft on disk" from "nothing was ever
    recorded") and closes -- there is nothing further to stream. The
    authoritative transcript is always the post-session full pipeline pass
    via `POST /recordings/{id}/transcribe`; the recording-detail page's own
    server-rendered fallback (once `status` leaves `recording`/`degraded`)
    is what actually shows the draft's content, so this signal doesn't
    carry the markdown itself.
    """
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return JSONResponse({"detail": "invalid recording id"}, status_code=400)

    queue = get_queue(request)
    data_dir = get_data_dir()

    async def event_generator():
        last_idx = 0
        last_job = None  # last job object seen -- see the None-branch below
        while True:
            if await request.is_disconnected():
                break

            job = queue.find_live_job_for_recording(safe_id)
            if job is None:
                # find_live_job_for_recording only matches PENDING/RUNNING
                # (see its docstring), so this fires the instant the job
                # completes -- which can land in the same ~1s window as a
                # final chunk committed right before the user hit Stop.
                # `last_job` is still the same Job object the worker thread
                # was mutating (Job instances aren't replaced on
                # completion), so one more read off it here catches lines
                # that would otherwise never reach an already-open stream.
                if last_job is not None:
                    retained_start = last_job.live_lines_dropped
                    new_lines = last_job.live_lines[max(last_idx, retained_start) - retained_start:]
                    for line in new_lines:
                        yield f"data: {json.dumps({'type': 'line', **line})}\n\n"
                    last_idx = retained_start + len(last_job.live_lines)
                    last_job = None

                if last_idx == 0:
                    live_path = data_dir / "recordings" / safe_id / "live_transcript.md"
                    if live_path.exists():
                        yield f"data: {json.dumps({'type': 'snapshot'})}\n\n"
                yield "event: end\ndata: {}\n\n"
                return

            retained_start = job.live_lines_dropped
            new_lines = job.live_lines[max(last_idx, retained_start) - retained_start:]
            for line in new_lines:
                yield f"data: {json.dumps({'type': 'line', **line})}\n\n"
            last_idx = retained_start + len(job.live_lines)
            last_job = job

            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/recordings/{recording_id}", response_class=HTMLResponse)
async def recording_detail_html(recording_id: str, request: Request) -> HTMLResponse:
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return invalid_input_response("Invalid recording ID")

    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)
    recording = recordings.get(safe_id)
    if recording is None:
        return error_redirect("/recordings", "not_found")

    campaigns = load_campaigns(data_dir)

    # Persisted live-transcript draft (Phase 2 near-real-time preview):
    # shown whenever recordings/<id>/live_transcript.md exists on disk and
    # the recording hasn't been through a full Transcribe yet -- never
    # cleaned up by anything (delete_recording() only pops the index
    # entry), so it survives indefinitely and would otherwise vanish from
    # the page the instant the session stopped, even though the file was
    # still right there. Only overwritten in the user's sense once a real
    # Transcribe job sets transcript_path -- this block never runs then.
    # While still actively recording, the existing SSE-driven pane
    # (below) owns the live view instead of this static one.
    live_draft_blocks = None
    if (
        recording.source == "local"
        and recording.transcript_path is None
        and recording.status not in ("recording", "degraded")
    ):
        live_path = data_dir / "recordings" / safe_id / "live_transcript.md"
        if live_path.exists():
            from wisper_transcribe.formatter import parse_transcript_blocks

            live_draft_blocks = parse_transcript_blocks(live_path.read_text(encoding="utf-8"))

    return templates.TemplateResponse(
        request,
        "recording_detail.html",
        {
            "request": request,
            "recording": recording,
            "campaigns": campaigns,
            "live_draft_blocks": live_draft_blocks,
        },
    )


@router.post("/recordings/{recording_id}/delete", response_class=HTMLResponse)
async def recording_delete_html(recording_id: str, request: Request) -> RedirectResponse:
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return invalid_input_response("Invalid recording ID")

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
        return error_redirect("/recordings", "not_found")

    if safe_uid not in recording.unbound_speakers:
        return JSONResponse({"detail": "speaker not in unbound list"}, status_code=409)

    profile_key = profile_name.strip().lower().replace(" ", "_")
    if not profile_key:
        return RedirectResponse(
            url=f"/recordings/{recording.id}?error=enroll_failed", status_code=303
        )

    per_user_dir = data_dir / "recordings" / recording.id / "per-user" / safe_uid

    # R6: the pydub decode + embedding extraction used to run synchronously
    # here, blocking the event loop and touching the module-level ML caches
    # concurrently with the job worker thread. It now runs as a JOB_ENROLL
    # job (one job at a time); the runner also applies the recording-state
    # updates (unbound list, discord_speakers binding, campaign membership)
    # that used to happen inline after the enroll. Redirect to the job
    # detail page — job.id is server-generated (uuid4), no user-controlled
    # data in the URL.
    queue = request.app.state.job_queue
    job = queue.submit_recording_enroll(
        recording_id=recording.id,
        discord_uid=safe_uid,
        per_user_dir=str(per_user_dir),
        profile_key=profile_key,
        display_name=profile_name.strip(),
    )

    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


# ---------------------------------------------------------------------------
# HTML — transcribe hand-off
# ---------------------------------------------------------------------------


def _submit_recording_transcription(recording, request: Request, data_dir: Path):
    """Validate a recording and submit its transcription job.

    Shared by the HTML hand-off route (``POST /recordings/{id}/transcribe``)
    and the JSON API (``POST /api/recordings/{id}/transcribe``, R7) so both
    entry points run the identical checks and ``JobQueue.submit`` call
    instead of two copies drifting apart.

    Returns ``(job, None)`` on success, or ``(None, error_code)`` where
    ``error_code`` is ``"not_ready"`` or ``"no_audio"`` -- generic codes
    only, never ``str(exc)`` or a filesystem path, per CLAUDE.md's web
    route security rules.
    """
    if recording.status not in ("completed", "transcribed"):
        return None, "not_ready"

    if recording.combined_path is None or not recording.combined_path.exists():
        return None, "no_audio"

    from wisper_transcribe.path_utils import get_output_dir

    # Copy combined.wav to output dir so the transcript lands alongside existing ones
    output_dir = get_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{recording.id}.wav"
    shutil.copy2(str(recording.combined_path), str(dest))

    # Preserved so a failed job can put the recording back where it started
    # (e.g. "transcribed" on a failed re-transcribe keeps the old transcript's
    # View/Re-transcribe actions available, rather than falling back to a
    # bare "completed" that hides them even though the old file is still there).
    previous_status = recording.status

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

    # Symmetric failure callback: without this, a failed job left the
    # recording stuck at "transcribing" forever with no retry path in the
    # UI (recording_detail.html only offers Transcribe/Re-transcribe
    # buttons for "completed"/"transcribed").
    def _on_error(job):
        _recordings = load_recordings(data_dir)
        rec = _recordings.get(recording.id)
        if rec is None:
            return
        rec.status = previous_status
        save_recording(rec, data_dir)

    queue = request.app.state.job_queue
    job = queue.submit(
        str(dest),
        original_stem=recording.id,
        output_dir=str(output_dir),
        campaign=recording.campaign_slug or "",
        title=recording.name,
        on_complete=_on_complete,
        on_error=_on_error,
    )

    recording.job_id = job.id
    recording.status = "transcribing"
    save_recording(recording, data_dir)

    return job, None


@router.post("/recordings/{recording_id}/transcribe", response_class=HTMLResponse)
async def recording_transcribe_html(recording_id: str, request: Request) -> RedirectResponse:
    """Hand off a completed recording to the transcription JobQueue."""
    safe_id = _validate_recording_id(recording_id)
    if safe_id is None:
        return invalid_input_response("Invalid recording ID")

    data_dir = get_data_dir()
    recordings = load_recordings(data_dir)
    recording = recordings.get(safe_id)
    if recording is None:
        return error_redirect("/recordings", "not_found")

    _job, error = _submit_recording_transcription(recording, request, data_dir)
    if error is not None:
        return error_redirect(f"/recordings/{recording.id}", error)

    return RedirectResponse(url=f"/recordings/{recording.id}", status_code=303)
