"""Ollama client — local LLM via httpx streaming REST wrapper.

Uses the ``/api/chat`` endpoint with ``stream=True``.  Each chunk is a
newline-delimited JSON object; tokens are accumulated and the full content
string is returned once the final chunk arrives.  A connect/write timeout
(``self.timeout``) guards against Ollama not being reachable, and the read
timeout bounds the idle gap between chunks, so a stalled stream fails rather
than blocking forever.
"""
from __future__ import annotations

import json
import queue as _queue
import sys
import threading

from .base import LLMClient, _STREAM_IDLE_TIMEOUT, _strip_json_fence
from .cancel import current_cancel_event
from .errors import LLMResponseError, LLMUnavailableError

_DOT_INTERVAL = 50   # print a progress dot every N content tokens
_CANCEL_POLL_SECONDS = 0.25  # how often the caller checks the cancel event


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(self, model: str, endpoint: str = "http://localhost:11434",
                 temperature: float = 0.2, timeout: float = 30.0,
                 api_key: str | None = None):
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout   # connect / write timeout in seconds
        self.api_key = api_key   # set for ollama-cloud direct calls; None for local daemon

    def _post_chat(self, payload: dict) -> str:
        """POST to /api/chat with streaming and return the full content string.

        Prints ``  Asking Ollama (model)… ·····`` to stderr while the model
        generates so the user knows progress is being made.  Uses
        ``connect=self.timeout`` and ``read=_STREAM_IDLE_TIMEOUT``; the read
        timeout is per chunk, so a model that stalls without sending bytes
        fails instead of blocking forever.
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

        # Short connect/write timeout; read timeout bounds the gap between chunks.
        timeout = httpx.Timeout(connect=self.timeout, read=_STREAM_IDLE_TIMEOUT,
                                write=self.timeout, pool=10.0)

        headers = None
        if self.api_key:
            headers = {"Authorization": f"Bearer {self.api_key}"}

        parts: list[str] = []
        messages: "_queue.Queue" = _queue.Queue()
        held: dict = {}

        def _read() -> None:
            """Read the stream in a daemon thread; the caller stays pollable."""
            token_count = 0
            try:
                with httpx.stream("POST", url, json=stream_payload,
                                  headers=headers, timeout=timeout) as resp:
                    held["resp"] = resp
                    resp.raise_for_status()
                    # All progress writes happen on the caller's side, so an
                    # abandoned reader can never write into a later job's log.
                    messages.put(("waiting", None))
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise LLMUnavailableError(
                                f"Ollama request failed ({url}): {chunk['error']}"
                            )
                        token = (chunk.get("message") or {}).get("content", "")
                        if token:
                            if token_count == 0:
                                messages.put(("start", None))
                            parts.append(token)
                            token_count += 1
                            if token_count % _DOT_INTERVAL == 0:
                                messages.put(("dot", None))
                        if chunk.get("done"):
                            break
                if token_count > 0:
                    messages.put(("end", None))
                messages.put(("done", None))
            except BaseException as exc:
                messages.put(("error", exc))

        generating = False
        try:
            sys.stderr.write(f"  Connecting to Ollama ({self.endpoint})...\n")
            sys.stderr.flush()
            reader = threading.Thread(target=_read, daemon=True)
            reader.start()
            event = current_cancel_event()
            while True:
                # Closing the response won't reliably wake a blocked read,
                # so abandon the reader and fail at once; the close is
                # best-effort only.
                if event is not None and event.is_set():
                    resp = held.get("resp")
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    raise InterruptedError("Job cancelled by user")
                try:
                    kind, payload = messages.get(timeout=_CANCEL_POLL_SECONDS)
                except _queue.Empty:
                    continue
                if kind == "done":
                    break
                if kind == "error":
                    raise payload
                if kind == "waiting":
                    sys.stderr.write(
                        f"  Waiting for {self.model} to start generating...\n"
                    )
                elif kind == "start":
                    generating = True
                    sys.stderr.write(f"  Generating ({self.model}): ")
                elif kind == "dot":
                    sys.stderr.write("·")
                else:  # "end"
                    sys.stderr.write("\n")
                sys.stderr.flush()
        except httpx.HTTPStatusError as exc:
            if generating:
                sys.stderr.write("\n")
                sys.stderr.flush()
            if exc.response.status_code == 404:
                raise LLMUnavailableError(
                    f"Model {self.model!r} not found in Ollama. "
                    f"Run: `ollama pull {self.model}` — or pick an installed model "
                    f"with `wisper config llm` / the web Config page."
                ) from exc
            raise LLMUnavailableError(
                f"Ollama request failed ({url}): {exc}"
            ) from exc
        except httpx.ConnectError as exc:
            if generating:
                sys.stderr.write("\n")
                sys.stderr.flush()
            raise LLMUnavailableError(
                f"Cannot connect to Ollama at {self.endpoint}. "
                f"Is the daemon running? Try: `ollama serve`"
            ) from exc
        except httpx.ReadTimeout as exc:
            if generating:
                sys.stderr.write("\n")
                sys.stderr.flush()
            minutes = int(_STREAM_IDLE_TIMEOUT // 60)
            raise LLMUnavailableError(
                f"No response from {self.model} for {minutes} minutes; "
                f"the provider may be overloaded. Try again or pick another model."
            ) from exc
        except httpx.HTTPError as exc:
            if generating:
                sys.stderr.write("\n")
                sys.stderr.flush()
            raise LLMUnavailableError(
                f"Ollama request failed ({url}): {exc}"
            ) from exc

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
        return self._retry_on_empty(lambda: self._post_chat(payload))

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
        text = _strip_json_fence(self._retry_on_empty(lambda: self._post_chat(payload)))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"Ollama JSON response did not parse: {exc}. Raw: {text[:200]!r}"
            ) from exc
