"""Ollama client — local LLM via httpx streaming REST wrapper.

Uses the ``/api/chat`` endpoint with ``stream=True`` to avoid read-timeouts
on long transcripts.  Each chunk is a newline-delimited JSON object; tokens
are accumulated and the full content string is returned once the final chunk
arrives.  A connect/write timeout (``self.timeout``) guards against Ollama
not being reachable, but there is intentionally no per-chunk read timeout —
the model delivers tokens continuously so there is no long idle gap between
bytes.
"""
from __future__ import annotations

import json
import sys

from .base import LLMClient
from .errors import LLMResponseError, LLMUnavailableError

_DOT_INTERVAL = 50   # print a progress dot every N content tokens


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(self, model: str, endpoint: str = "http://localhost:11434",
                 temperature: float = 0.2, timeout: float = 30.0):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout   # connect / write timeout in seconds

    def _post_chat(self, payload: dict) -> str:
        """POST to /api/chat with streaming and return the full content string.

        Prints ``  Asking Ollama (model)… ·····`` to stderr while the model
        generates so the user knows progress is being made.  Uses
        ``connect=self.timeout`` but ``read=None`` so a slow model on a long
        transcript never times out mid-stream.
        """
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — httpx is a core dep
            raise LLMUnavailableError(
                "httpx not installed. Run: pip install httpx"
            ) from exc

        url = f"{self.endpoint}/api/chat"
        stream_payload = dict(payload)
        stream_payload["stream"] = True

        # Short connect/write timeout; no read timeout while streaming.
        timeout = httpx.Timeout(connect=self.timeout, read=None,
                                write=self.timeout, pool=10.0)

        parts: list[str] = []
        token_count = 0
        try:
            sys.stderr.write(f"  Connecting to Ollama ({self.endpoint})...\n")
            sys.stderr.flush()
            with httpx.stream("POST", url, json=stream_payload,
                              timeout=timeout) as resp:
                resp.raise_for_status()
                sys.stderr.write(
                    f"  Waiting for {self.model} to start generating...\n"
                )
                sys.stderr.flush()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    token = (chunk.get("message") or {}).get("content", "")
                    if token:
                        if token_count == 0:
                            sys.stderr.write(f"  Generating ({self.model}): ")
                            sys.stderr.flush()
                        parts.append(token)
                        token_count += 1
                        if token_count % _DOT_INTERVAL == 0:
                            sys.stderr.write("·")
                            sys.stderr.flush()
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as exc:
            if token_count > 0:
                sys.stderr.write("\n")
                sys.stderr.flush()
            raise LLMUnavailableError(
                f"Ollama request failed ({url}): {exc}. "
                f"Is the Ollama daemon running? Try: `ollama serve`"
            ) from exc

        if token_count > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()

        return "".join(parts)

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        return self._post_chat(payload)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        # Ollama supports `format="json"` (free-form JSON) and newer versions
        # accept a JSON schema dict. Pass the schema if available; otherwise
        # rely on the prompt to steer shape.
        payload = {
            "model": self.model,
            "format": schema if schema else "json",
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        text = self._post_chat(payload)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"Ollama JSON response did not parse: {exc}. Raw: {text[:200]!r}"
            ) from exc
