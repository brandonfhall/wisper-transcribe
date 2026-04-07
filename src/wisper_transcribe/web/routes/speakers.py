"""Speakers route — manage enrolled speaker profiles."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import templates

router = APIRouter(prefix="/speakers")


@router.get("", response_class=HTMLResponse)
async def speakers_list(request: Request) -> HTMLResponse:
    from wisper_transcribe.speaker_manager import load_profiles
    profiles = load_profiles()
    return templates.TemplateResponse(
        "speakers.html",
        {"request": request, "profiles": profiles},
    )


@router.get("/enroll", response_class=HTMLResponse)
async def enroll_form(request: Request) -> HTMLResponse:
    """Standalone speaker enrollment form (not tied to a transcription job)."""
    return templates.TemplateResponse(
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

    if audio is None:
        return RedirectResponse(url="/speakers/enroll?error=no_audio", status_code=303)

    suffix = Path(audio.filename or "audio.mp3").suffix or ".mp3"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="wisper_enroll_")
    try:
        content = await audio.read()
        tmp.write(content)
    finally:
        tmp.close()

    try:
        from wisper_transcribe.audio_utils import convert_to_wav
        from wisper_transcribe.config import get_device
        from wisper_transcribe.diarizer import diarize
        from wisper_transcribe.config import load_config, get_hf_token
        from wisper_transcribe.speaker_manager import enroll_speaker, update_embedding, extract_embedding, load_profiles

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
            return RedirectResponse(url="/speakers/enroll?error=no_speech", status_code=303)
        primary_label = max(speaker_time, key=lambda k: speaker_time[k])

        profile_key = name.lower().replace(" ", "_")

        if update:
            profiles = load_profiles()
            if profile_key in profiles:
                new_emb = extract_embedding(wav_path, diarization, primary_label, device)
                update_embedding(profile_key, new_emb)
            else:
                enroll_speaker(
                    name=profile_key,
                    display_name=name,
                    role=role,
                    audio_path=wav_path,
                    segments=diarization,
                    speaker_label=primary_label,
                    device=device,
                    notes=notes,
                )
        else:
            enroll_speaker(
                name=profile_key,
                display_name=name,
                role=role,
                audio_path=wav_path,
                segments=diarization,
                speaker_label=primary_label,
                device=device,
                notes=notes,
            )
    except Exception as exc:
        return RedirectResponse(
            url=f"/speakers/enroll?error={str(exc)[:100]}", status_code=303
        )

    return RedirectResponse(url="/speakers", status_code=303)


@router.post("/{name}/remove", response_class=HTMLResponse)
async def remove_speaker(request: Request, name: str) -> RedirectResponse:
    from wisper_transcribe.speaker_manager import load_profiles, save_profiles

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
    from wisper_transcribe.speaker_manager import load_profiles, save_profiles

    form = await request.form()
    new_display = str(form.get("new_name", "")).strip()
    if not new_display:
        return RedirectResponse(url="/speakers", status_code=303)

    profiles = load_profiles()
    if name in profiles:
        profiles[name].display_name = new_display
        save_profiles(profiles)
    return RedirectResponse(url="/speakers", status_code=303)
