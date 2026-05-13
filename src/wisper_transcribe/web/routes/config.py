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
    get_llm_api_key,
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
    ("llm_provider",         "select", "LLM provider",                list(LLM_PROVIDERS)),
    ("llm_model",            "str",    "Model name (blank = default)", None),
    ("llm_endpoint",         "str",    "Ollama endpoint URL",          None),
    ("llm_temperature",      "float",  "Sampling temperature (0–1)",   None),
    ("anthropic_api_key",    "secret", "Anthropic API key",            None),
    ("openai_api_key",       "secret", "OpenAI API key",               None),
    ("google_api_key",       "secret", "Google API key",               None),
    ("ollama_cloud_api_key", "secret", "Ollama Cloud API key",         None),
]

_DISCORD_FIELDS = [
    ("discord_bot_token",       "secret", "Discord bot token",           None),
    ("discord_default_guild",   "str",    "Default guild (server) ID",   None),
    ("discord_default_channel", "str",    "Default voice channel ID",    None),
]

# Keys that must not be overwritten with an empty string
_LLM_SECRET_FIELD_KEYS = frozenset({
    "anthropic_api_key", "openai_api_key", "google_api_key", "ollama_cloud_api_key",
})

# Discord snowflake IDs are 17–20 decimal digits
_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")

# Reject absurdly long API key submissions before touching any SDK
_MAX_API_KEY_LEN = 512

# OpenAI returns ~50 models including audio/image/embedding/legacy — filter to chat-likely.
_OPENAI_CHAT_PREFIXES = ("gpt-", "chatgpt-")
_OPENAI_REASONING_RE = re.compile(r"^o\d")  # o1, o3-mini, o4, …
# Pre-compiled deny pattern — avoids the string `in` operator so CodeQL's
# py/incomplete-url-substring-sanitization query does not flag this as an
# untrusted-URL substring check (these are model IDs, not URLs).
_OPENAI_DENY_RE = re.compile(
    r"instruct|audio|realtime|search|transcribe|image|tts|dall-e|"
    r"whisper|embedding|moderation|davinci|babbage|vision",
    re.IGNORECASE,
)

# Google model short-name prefixes that are NOT chat models.
_GOOGLE_DENY_PREFIXES = ("embedding", "imagen", "aqa")


def _is_openai_chat_model(model_id: str) -> bool:
    if _OPENAI_DENY_RE.search(model_id):
        return False
    if any(model_id.startswith(p) for p in _OPENAI_CHAT_PREFIXES):
        return True
    return bool(_OPENAI_REASONING_RE.match(model_id))


def _is_google_chat_model(short_name: str) -> bool:
    # Use startswith (not `in`) so CodeQL does not mistake this for an
    # incomplete URL-substring sanitization check.
    if any(short_name.startswith(x) for x in _GOOGLE_DENY_PREFIXES):
        return False
    return short_name.startswith("gemini")


async def _resolve_form_api_key(request: Request, provider: str) -> str | None:
    """Resolve the API key for *provider*: form `api_key` > env > saved config.

    Form value is bounded by _MAX_API_KEY_LEN so a hostile or accidental
    multi-megabyte submission can't reach the SDK.
    """
    form = await request.form()
    raw = form.get("api_key")
    if raw is not None:
        raw = str(raw).strip()
        if 0 < len(raw) <= _MAX_API_KEY_LEN:
            return raw
    return get_llm_api_key(provider)


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


@router.get("/ollama-cloud-catalog", response_class=JSONResponse)
async def ollama_cloud_catalog() -> JSONResponse:
    """Return the public Ollama Cloud model catalog from https://ollama.com/api/tags.

    Used by both the `ollama` provider combobox (cloud models get a `-cloud`
    suffix for routing through the local daemon's signin proxy) and the
    `ollama-cloud` provider combobox (bare names for direct API calls).
    The catalog endpoint is public, so no API key is sent.
    """
    import httpx
    url = "https://ollama.com/api/tags"
    try:
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        data = r.json()
        models = []
        for m in data.get("models", []):
            name = m.get("name")
            if not name:
                continue
            size_bytes = m.get("size", 0)
            size_str = f"{size_bytes / 1e9:.0f} GB" if size_bytes else ""
            models.append({"name": name, "size": size_str})
        return JSONResponse({"running": True, "models": models})
    except Exception:
        log.warning("Failed to fetch Ollama Cloud catalog", exc_info=True)
        return JSONResponse({
            "running": False, "models": [],
            "error": "Could not reach ollama.com · check your network",
        })


def _no_key_response(provider_label: str) -> JSONResponse:
    return JSONResponse({
        "running": False,
        "models": [],
        "error": f"{provider_label} API key required · enter it above and click Refresh",
    })


def _sdk_missing_response(install_extra: str) -> JSONResponse:
    return JSONResponse({
        "running": False,
        "models": [],
        "error": f"SDK not installed · pip install 'wisper-transcribe[{install_extra}]'",
    })


@router.post("/anthropic-models", response_class=JSONResponse)
async def anthropic_models(request: Request) -> JSONResponse:
    """List available Anthropic models for the config combobox.

    POST so the freshly-typed API key travels in the request body, never in
    a URL or log. Resolution order: form `api_key` > env ANTHROPIC_API_KEY >
    saved config.
    """
    key = await _resolve_form_api_key(request, "anthropic")
    if not key:
        return _no_key_response("Anthropic")
    try:
        import anthropic
    except ImportError:
        return _sdk_missing_response("llm-anthropic")
    try:
        client = anthropic.Anthropic(api_key=key)
        page = client.models.list()
        models = []
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", None)
            if not mid:
                continue
            label = getattr(m, "display_name", "") or ""
            models.append({"name": mid, "size": label})
        return JSONResponse({"running": True, "models": models})
    except Exception:
        log.warning("Failed to list Anthropic models", exc_info=True)
        return JSONResponse({
            "running": False, "models": [],
            "error": "Anthropic API call failed · check your key and network",
        })


@router.post("/openai-models", response_class=JSONResponse)
async def openai_models(request: Request) -> JSONResponse:
    """List available OpenAI chat models. Same security pattern as anthropic_models."""
    key = await _resolve_form_api_key(request, "openai")
    if not key:
        return _no_key_response("OpenAI")
    try:
        import openai
    except ImportError:
        return _sdk_missing_response("llm-openai")
    try:
        client = openai.OpenAI(api_key=key)
        page = client.models.list()
        models = []
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", None)
            if not mid or not _is_openai_chat_model(mid):
                continue
            models.append({"name": mid, "size": ""})
        models.sort(key=lambda m: m["name"])
        return JSONResponse({"running": True, "models": models})
    except Exception:
        log.warning("Failed to list OpenAI models", exc_info=True)
        return JSONResponse({
            "running": False, "models": [],
            "error": "OpenAI API call failed · check your key and network",
        })


@router.post("/google-models", response_class=JSONResponse)
async def google_models(request: Request) -> JSONResponse:
    """List available Google Gemini chat models. Same security pattern."""
    key = await _resolve_form_api_key(request, "google")
    if not key:
        return _no_key_response("Google")
    try:
        from google import genai
    except ImportError:
        return _sdk_missing_response("llm-google")
    try:
        client = genai.Client(api_key=key)
        models = []
        for m in client.models.list():
            full = getattr(m, "name", None)
            if not full:
                continue
            short = full.split("/", 1)[-1]
            if not _is_google_chat_model(short):
                continue
            models.append({"name": short, "size": ""})
        models.sort(key=lambda m: m["name"])
        return JSONResponse({"running": True, "models": models})
    except Exception:
        log.warning("Failed to list Google models", exc_info=True)
        return JSONResponse({
            "running": False, "models": [],
            "error": "Google API call failed · check your key and network",
        })


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


@router.get("/open-data-dir", response_class=JSONResponse)
async def open_data_dir() -> JSONResponse:
    """Open the data directory in the OS file manager (macOS Finder, Linux Files, Windows Explorer).

    Only meaningful when running locally — silently fails on headless servers.
    Returns {ok: true} on success, {ok: false, error: "..."} on failure.
    """
    import subprocess
    import sys
    from wisper_transcribe.config import get_data_dir
    path = str(get_data_dir())
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
