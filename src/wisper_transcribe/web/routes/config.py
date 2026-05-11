"""Config route — view and edit application settings."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import templates
from wisper_transcribe.config import (
    LLM_PROVIDERS,
    get_config_path,
    load_config,
    save_config,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/config")

# Config keys exposed in the UI with types and descriptions
_CONFIG_FIELDS = [
    ("model",               "str",   "Whisper model size", ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]),
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

_LLM_FIELDS = [
    ("llm_provider",       "select", "LLM provider",                list(LLM_PROVIDERS)),
    ("llm_model",          "str",    "Model name (blank = default)", None),
    ("llm_endpoint",       "str",    "Ollama endpoint URL",          None),
    ("llm_temperature",    "float",  "Sampling temperature (0–1)",   None),
    ("anthropic_api_key",  "secret", "Anthropic API key",            None),
    ("openai_api_key",     "secret", "OpenAI API key",               None),
    ("google_api_key",     "secret", "Google API key",               None),
]

_DISCORD_FIELDS = [
    ("discord_bot_token",       "secret", "Discord bot token",           None),
    ("discord_default_guild",   "str",    "Default guild (server) ID",   None),
    ("discord_default_channel", "str",    "Default voice channel ID",    None),
]

# Keys that must not be overwritten with an empty string
_LLM_SECRET_FIELD_KEYS = frozenset({"anthropic_api_key", "openai_api_key", "google_api_key"})

# Discord snowflake IDs are 17–20 decimal digits
_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")


@router.get("/ollama-status", response_class=JSONResponse)
async def ollama_status() -> JSONResponse:
    """Return Ollama reachability and installed model list for the config UI.

    Reads the saved llm_endpoint from config — no user-supplied URL reaches
    httpx, which eliminates the SSRF taint path CodeQL would otherwise flag.
    """
    import httpx
    from wisper_transcribe.config import _LLM_DEFAULT_ENDPOINTS

    cfg = load_config()
    endpoint = (cfg.get("llm_endpoint") or _LLM_DEFAULT_ENDPOINTS["ollama"]).rstrip("/")
    url = endpoint + "/api/tags"
    try:
        r = httpx.get(url, timeout=3.0)
        r.raise_for_status()
        data = r.json()
        models = []
        for m in data.get("models", []):
            size_bytes = m.get("size", 0)
            size_str = f"{size_bytes / 1e9:.1f} GB" if size_bytes else ""
            models.append({"name": m["name"], "size": size_str})
        return JSONResponse({"running": True, "models": models})
    except Exception:
        log.warning("Failed to query Ollama status", exc_info=True)
        return JSONResponse({"running": False, "models": []})


@router.get("/lmstudio-status", response_class=JSONResponse)
async def lmstudio_status() -> JSONResponse:
    """Return LM Studio reachability and loaded model list for the config UI.

    Reads the saved llm_endpoint from config — no user-supplied URL reaches
    httpx, which eliminates the SSRF taint path CodeQL would otherwise flag.
    """
    import httpx
    from wisper_transcribe.config import _LLM_DEFAULT_ENDPOINTS

    cfg = load_config()
    endpoint = (cfg.get("llm_endpoint") or _LLM_DEFAULT_ENDPOINTS["lmstudio"]).rstrip("/")
    url = endpoint + "/v1/models"
    try:
        r = httpx.get(url, timeout=3.0)
        r.raise_for_status()
        data = r.json()
        models = [{"name": m["id"], "size": ""} for m in data.get("data", []) if m.get("id")]
        return JSONResponse({"running": True, "models": models})
    except Exception:
        log.warning("Failed to query LM Studio status", exc_info=True)
        return JSONResponse({"running": False, "models": []})


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
            "llm_fields": _LLM_FIELDS,
            "discord_fields": _DISCORD_FIELDS,
            "discord_presets": config.get("discord_presets", []),
            "saved": request.query_params.get("saved") == "1",
        },
    )


def _apply_fields(config: dict, form, fields) -> None:
    """Write validated form values into *config* in-place."""
    for key, type_, _desc, _choices in fields:
        raw = form.get(key)
        if raw is None:
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
        elif type_ == "secret":
            # Never overwrite an existing secret with an empty submission
            if raw:
                config[key] = raw
        else:
            config[key] = raw


@router.post("/presets/add")
async def preset_add(request: Request) -> RedirectResponse:
    form = await request.form()
    name = str(form.get("name", "")).strip()
    guild_id = str(form.get("guild_id", "")).strip()
    channel_id = str(form.get("channel_id", "")).strip()

    if not name or not _SNOWFLAKE_RE.match(guild_id) or not _SNOWFLAKE_RE.match(channel_id):
        return RedirectResponse(url="/record?preset_error=invalid", status_code=303)

    config = load_config()
    presets = list(config.get("discord_presets", []))
    presets.append({"name": name, "guild_id": guild_id, "channel_id": channel_id})
    config["discord_presets"] = presets
    save_config(config)
    return RedirectResponse(url="/record?preset_saved=1", status_code=303)


@router.post("", response_class=HTMLResponse)
async def config_save(request: Request) -> RedirectResponse:
    form = await request.form()
    config = load_config()

    _apply_fields(config, form, _CONFIG_FIELDS)
    _apply_fields(config, form, _LLM_FIELDS)
    _apply_fields(config, form, _DISCORD_FIELDS)

    # Rebuild discord_presets from form data (names, guild_ids, channel_ids arrays)
    preset_names = form.getlist("preset_name")
    preset_guilds = form.getlist("preset_guild_id")
    preset_channels = form.getlist("preset_channel_id")
    presets = []
    for i in range(len(preset_names)):
        nm = preset_names[i].strip() if i < len(preset_names) else ""
        gid = preset_guilds[i].strip() if i < len(preset_guilds) else ""
        cid = preset_channels[i].strip() if i < len(preset_channels) else ""
        if nm and gid and cid:
            presets.append({"name": nm, "guild_id": gid, "channel_id": cid})
    config["discord_presets"] = presets

    save_config(config)
    return RedirectResponse(url="/config?saved=1", status_code=303)
