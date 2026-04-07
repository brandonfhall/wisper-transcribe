"""Config route — view and edit application settings."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import templates
from wisper_transcribe.config import get_config_path, load_config, save_config

router = APIRouter(prefix="/config")

# Config keys exposed in the UI with types and descriptions
_CONFIG_FIELDS = [
    ("model",               "str",   "Whisper model size", ["tiny", "base", "small", "medium", "large-v3"]),
    ("language",            "str",   "Default language code (e.g. en, fr) or auto", None),
    ("device",              "str",   "Compute device", ["auto", "cpu", "cuda", "mps"]),
    ("compute_type",        "str",   "CTranslate2 quantization", ["auto", "float16", "int8_float16", "int8", "float32"]),
    ("vad_filter",          "bool",  "Voice activity detection (skip silence)", None),
    ("timestamps",          "bool",  "Include timestamps in output", None),
    ("similarity_threshold","float", "Speaker matching similarity threshold (0–1)", None),
    ("min_speakers",        "int",   "Minimum number of speakers for diarization", None),
    ("max_speakers",        "int",   "Maximum number of speakers for diarization", None),
    ("hf_token",            "secret","HuggingFace access token", None),
]


@router.get("", response_class=HTMLResponse)
async def config_show(request: Request) -> HTMLResponse:
    config = load_config()
    config_path = get_config_path()
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "request": request,
            "config": config,
            "config_path": str(config_path),
            "fields": _CONFIG_FIELDS,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("", response_class=HTMLResponse)
async def config_save(request: Request) -> RedirectResponse:
    form = await request.form()
    config = load_config()

    for key, type_, _desc, _choices in _CONFIG_FIELDS:
        raw = form.get(key)
        if raw is None:
            # Unchecked checkboxes are absent from form data
            if type_ == "bool":
                config[key] = False
            continue
        raw = str(raw).strip()
        if type_ == "bool":
            config[key] = raw.lower() in ("1", "true", "on", "yes")
        elif type_ == "int":
            try:
                config[key] = int(raw)
            except ValueError:
                pass
        elif type_ == "float":
            try:
                config[key] = float(raw)
            except ValueError:
                pass
        else:
            config[key] = raw

    save_config(config)
    return RedirectResponse(url="/config?saved=1", status_code=303)
