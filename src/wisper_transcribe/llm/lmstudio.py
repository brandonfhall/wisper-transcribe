"""LM Studio client — local LLM via OpenAI-compatible streaming REST API.

LM Studio exposes an OpenAI-compatible server (default http://localhost:1234).
Uses ``POST /v1/chat/completions`` with ``stream=True`` and SSE parsing so
long transcripts never hit a read timeout.  A short connect/write timeout
guards against the server not being reachable.
"""
from __future__ import annotations

import json
import sys

from .base import LLMClient
from .errors import LLMResponseError, LLMUnavailableError

_DOT_INTERVAL = 50   # print a progress dot every N content tokens


class LMStudioClient(LLMClient):
    provider = "lmstudio"

    def __init__(self, model: str, endpoint: str = "http://localhost:1234",
                 temperature: float = 0.2, timeout: float = 30.0):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout   # connect / write timeout in seconds

    def _post_chat(self, payload: dict) -> str:
        """POST to /v1/chat/completions with SSE streaming; return full content.

        Prints progress dots to stderr while the model generates.  Uses
        ``connect=self.timeout`` but ``read=None`` so generation on slow
        hardware never times out mid-stream.
        """
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — httpx is a core dep
            raise LLMUnavailableError(
                "httpx not installed. Run: pip install httpx"
            ) from exc

        url = f"{self.endpoint}/v1/chat/completions"
        stream_payload = dict(payload)
        stream_payload["stream"] = True

        timeout = httpx.Timeout(connect=self.timeout, read=None,
                                write=self.timeout, pool=10.0)

        parts: list[str] = []
        token_count = 0
        try:
            sys.stderr.write(f"  Connecting to LM Studio ({self.endpoint})...\n")
            sys.stderr.flush()
            with httpx.stream("POST", url, json=stream_payload,
                              timeout=timeout) as resp:
                resp.raise_for_status()
                sys.stderr.write(
                    f"  Waiting for {self.model} to start generating...\n"
                )
                sys.stderr.flush()
                for line in resp.iter_lines():
                    if not line or line == "data: [DONE]":
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    token = (choices[0].get("delta") or {}).get("content", "") if choices else ""
                    if token:
                        if token_count == 0:
                            sys.stderr.write(f"  Generating ({self.model}): ")
                            sys.stderr.flush()
                        parts.append(token)
                        token_count += 1
                        if token_count % _DOT_INTERVAL == 0:
                            sys.stderr.write("·")
                            sys.stderr.flush()
                    if choices and choices[0].get("finish_reason") == "stop":
                        break
        except httpx.HTTPStatusError as exc:
            if token_count > 0:
                sys.stderr.write("\n")
                sys.stderr.flush()
            if exc.response.status_code == 404:
                raise LLMUnavailableError(
                    f"Model {self.model!r} not found in LM Studio. "
                    f"Load a model in the LM Studio UI first, then pick it on "
                    f"the Config page or run `wisper config llm`."
                ) from exc
            raise LLMUnavailableError(
                f"LM Studio request failed ({url}): {exc}"
            ) from exc
        except httpx.ConnectError as exc:
            if token_count > 0:
                sys.stderr.write("\n")
                sys.stderr.flush()
            raise LLMUnavailableError(
                f"Cannot connect to LM Studio at {self.endpoint}. "
                f"Is the local server running? Enable it in LM Studio → "
                f"Developer → Local Server."
            ) from exc
        except httpx.HTTPError as exc:
            if token_count > 0:
                sys.stderr.write("\n")
                sys.stderr.flush()
            raise LLMUnavailableError(
                f"LM Studio request failed ({url}): {exc}"
            ) from exc

        if token_count > 0:
            sys.stderr.write("\n")
            sys.stderr.flush()

        return "".join(parts)

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        return self._post_chat(payload)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
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
                f"LM Studio JSON response did not parse: {exc}. Raw: {text[:200]!r}"
            ) from exc
