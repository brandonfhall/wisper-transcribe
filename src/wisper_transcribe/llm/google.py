"""Google Gemini client (google-genai SDK).

Uses `client.models.generate_content` with `response_schema` for structured
output. SDK import path: `from google import genai`.
"""
from __future__ import annotations

import json

from .base import LLMClient
from .errors import LLMResponseError, LLMUnavailableError


def _load_sdk():
    try:
        from google import genai  # noqa: F401
    except ImportError as exc:
        raise LLMUnavailableError(
            "google-genai SDK not installed. "
            "Run: pip install 'wisper-transcribe[llm-google]'"
        ) from exc
    from google import genai
    return genai


class GoogleClient(LLMClient):
    provider = "google"

    def __init__(self, model: str, api_key: str, temperature: float = 0.2,
                 max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        genai = _load_sdk()
        self._genai = genai
        self._client = genai.Client(api_key=api_key)

    def _build_config(self, *, system: str, schema: dict = None):
        # google.genai.types is the namespaced config type container.
        from google.genai import types
        kwargs = {
            "temperature": self.temperature,
            "max_output_tokens": self.max_tokens,
            "system_instruction": system,
        }
        if schema:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = schema
        return types.GenerateContentConfig(**kwargs)

    def _safe_generate(self, *, system: str, user: str, schema: dict = None):
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=user,
                config=self._build_config(system=system, schema=schema),
            )
        except Exception as exc:  # google-genai raises its own errors; be broad
            raise LLMUnavailableError(f"Google API error: {exc}") from exc
        text = getattr(resp, "text", None)
        if not isinstance(text, str):
            raise LLMResponseError("Google response had no `text` attribute")
        return text

    def complete(self, system: str, user: str) -> str:
        return self._safe_generate(system=system, user=user)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        text = self._safe_generate(system=system, user=user, schema=schema)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"Google JSON response did not parse: {exc}. Raw: {text[:200]!r}"
            ) from exc
