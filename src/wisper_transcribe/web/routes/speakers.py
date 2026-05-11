"""Speakers route — manage enrolled speaker profiles."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import templates
from wisper_transcribe.path_utils import validate_path_component
from wisper_transcribe.speaker_manager import load_profiles, save_profiles
from wisper_transcribe.web._responses import error_redirect, invalid_input_response

router = APIRouter(prefix="/speakers")


def _clip_path(key: str) -> "Path":
    import os
    from wisper_transcribe.speaker_manager import _get_embeddings_dir
    return _get_embeddings_dir() / f"{os.path.basename(key)}.mp3"


@router.get("", response_class=HTMLResponse)
async def speakers_list(request: Request) -> HTMLResponse:
    profiles = load_profiles()
    # Pass which profiles have a reference clip available
    has_clip = {key: _clip_path(key).exists() for key in profiles}
    return templates.TemplateResponse(
        request,
        "speakers.html",
        {"request": request, "profiles": profiles, "has_clip": has_clip},
    )


@router.get("/{key}/clip")
async def speaker_clip(request: Request, key: str) -> Response:
    """Serve the reference audio clip for a speaker profile."""
    safe_key = validate_path_component(key, "_speakers_key_guard")
    if safe_key is None:
        return invalid_input_response("Invalid key")

    from wisper_transcribe.speaker_manager import _get_embeddings_dir
    embeddings_dir = _get_embeddings_dir().resolve()
    base_dir = os.path.abspath(str(embeddings_dir))
    if not base_dir.endswith(os.sep):
        base_dir += os.sep
    target_path = os.path.abspath(os.path.join(str(embeddings_dir), f"{safe_key}.mp3"))
    if not target_path.startswith(base_dir):
        return invalid_input_response("Invalid key")
    clean_clip = Path(target_path)

    if not clean_clip.exists() or not clean_clip.is_file():
        return HTMLResponse(content="No clip available", status_code=404)
    return FileResponse(path=str(clean_clip), media_type="audio/mpeg")


@router.get("/enroll", response_class=HTMLResponse)
async def enroll_form(request: Request) -> HTMLResponse:
    """Standalone speaker enrollment form (not tied to a transcription job)."""
    return templates.TemplateResponse(
        request,
        "speaker_enroll_standalone.html",
        {"request": request},
    )


@router.post("/enroll", response_class=HTMLResponse)
async def enroll_submit(
    request: Request,
    name: Annotated[str, Form()],
    role: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    audio: Annotated[UploadFile, File()] = None,
    segment: Annotated[Optional[str], Form()] = None,
    update: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    """Enroll a new speaker or update an existing one from an uploaded audio file."""
    import tempfile

    if not name or "\x00" in name:
        return error_redirect("/speakers/enroll", "invalid_name")
    safe_name = os.path.basename(name)
    if safe_name != name or safe_name in {".", ".."}:
        return error_redirect("/speakers/enroll", "invalid_name")

    profile_key = validate_path_component(safe_name.lower().replace(" ", "_"), "_speakers_enroll_guard")
    if profile_key is None:
        return error_redirect("/speakers/enroll", "invalid_name")

    if audio is None:
        return error_redirect("/speakers/enroll", "no_audio")

    suffix = Path(audio.filename or "audio.mp3").suffix or ".mp3"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="wisper_enroll_")
    try:
        content = await audio.read()
        tmp.write(content)
    finally:
        tmp.close()

    try:
        from wisper_transcribe.audio_utils import convert_to_wav
        from wisper_transcribe.config import get_device, get_hf_token, load_config
        from wisper_transcribe.diarizer import diarize
        from wisper_transcribe.speaker_manager import enroll_speaker, extract_embedding, update_embedding

        config = load_config()
        device = get_device()
        hf_token = get_hf_token(config)
        wav_path = convert_to_wav(Path(tmp.name))

        # Diarize to get segment boundaries
        diarization = diarize(wav_path, hf_token=hf_token, device=device)

        # Find the primary speaker label (most total speech time)
        from collections import defaultdict
        speaker_time: dict[str, float] = defaultdict(float)
        for seg in diarization:
            speaker_time[seg.speaker] += seg.end - seg.start
        if not speaker_time:
            return error_redirect("/speakers/enroll", "no_speech")
        primary_label = max(speaker_time, key=lambda k: speaker_time[k])

        if update and profile_key in load_profiles():
            new_emb = extract_embedding(wav_path, diarization, primary_label, device)
            update_embedding(profile_key, new_emb)
        else:
            enroll_speaker(
                name=profile_key,
                display_name=safe_name,
                role=role,
                audio_path=wav_path,
                segments=diarization,
                speaker_label=primary_label,
                device=device,
                notes=notes,
            )
    except Exception:
        return error_redirect("/speakers/enroll", "enroll_failed")

    return RedirectResponse(url="/speakers", status_code=303)


@router.post("/{name}/remove", response_class=HTMLResponse)
async def remove_speaker(request: Request, name: str) -> RedirectResponse:
    profiles = load_profiles()
    if name in profiles:
        profile = profiles.pop(name)
        # Remove embedding file
        if profile.embedding_path.exists():
            profile.embedding_path.unlink()
        save_profiles(profiles)
    return RedirectResponse(url="/speakers", status_code=303)


@router.post("/{name}/rename", response_class=HTMLResponse)
async def rename_speaker(request: Request, name: str) -> RedirectResponse:
    form = await request.form()
    new_display = str(form.get("new_name", "")).strip()
    if not new_display:
        return RedirectResponse(url="/speakers", status_code=303)

    profiles = load_profiles()
    if name in profiles:
        profiles[name].display_name = new_display
        save_profiles(profiles)
    return RedirectResponse(url="/speakers", status_code=303)
