"""Ollama client — local LLM via httpx REST wrapper.

Uses the `/api/chat` endpoint with `format="json"` for structured output.
httpx is already a dev/runtime dependency; no new package required.
"""
from __future__ import annotations

import json
from typing import Optional

from .base import LLMClient
from .errors import LLMResponseError, LLMUnavailableError


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(self, model: str, endpoint: str = "http://localhost:11434",
                 temperature: float = 0.2, timeout: float = 120.0):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    def _post_chat(self, payload: dict) -> dict:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — httpx is a core dep
            raise LLMUnavailableError(
                "httpx not installed. Run: pip install httpx"
            ) from exc

        url = f"{self.endpoint}/api/chat"
        try:
            resp = httpx.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"Ollama request failed ({url}): {exc}. "
                f"Is the Ollama daemon running? Try: `ollama serve`"
            ) from exc

        try:
            return resp.json()
        except ValueError as exc:
            raise LLMResponseError(f"Ollama returned non-JSON response: {exc}") from exc

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post_chat(payload)
        msg = data.get("message") or {}
        text = msg.get("content", "")
        if not isinstance(text, str):
            raise LLMResponseError("Ollama response `message.content` was not a string")
        return text

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        # Ollama supports `format="json"` (free-form JSON) and newer versions
        # accept a JSON schema dict. Pass the schema if available; otherwise
        # rely on the prompt to steer shape.
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema if schema else "json",
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post_chat(payload)
        msg = data.get("message") or {}
        text = msg.get("content", "")
        if not isinstance(text, str):
            raise LLMResponseError("Ollama response `message.content` was not a string")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"Ollama JSON response did not parse: {exc}. Raw: {text[:200]!r}"
            ) from exc
