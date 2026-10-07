"""LLMClient abstract base class.

Hides provider differences behind two methods:
    complete(system, user) -> str            # free-text generation (summarize)
    complete_json(system, user, schema) -> dict  # structured output (refine)

Every concrete client must:
- Lazy-import its provider SDK inside __init__ (or the method) and raise
  LLMUnavailableError with a pip install hint if the import fails.
- Soft-fail with LLMUnavailableError on network / endpoint errors.
- Raise LLMResponseError when the model returns unparseable or non-conforming
  output.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Callable

from tqdm import tqdm

from .cancel import current_cancel_event, raise_if_cancelled
from .errors import LLMResponseError

# Retry policy when a streaming call comes back with empty content: two more
# attempts (three calls total), sleeping this long before each.
_EMPTY_RETRY_DELAYS = (5.0, 10.0)

# Idle read timeout for streaming clients. httpx applies it to each socket
# read, so it bounds the gap between chunks — not total generation time — and
# a stalled stream fails instead of blocking forever.
_STREAM_IDLE_TIMEOUT = 600.0


def _strip_json_fence(text: str) -> str:
    """Strip a markdown code fence if the model wrapped its JSON output in one.

    Some models ignore ``format: json`` / ``response_format`` and emit:
        ```json
        { ... }
        ```
    This strips the fence so json.loads() can parse the content cleanly.
    """
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    return m.group(1).strip() if m else text


class LLMClient(ABC):
    """Abstract base class for all LLM provider clients."""

    provider: str = ""       # filled in by subclasses
    model: str = ""
    temperature: float = 0.2

    def _retry_on_empty(self, call: Callable[[], str]) -> str:
        """Run ``call()``, retrying when it returns empty (whitespace-only) text.

        Reasoning models (Ollama, LM Studio) sometimes stream a ``thinking``
        field and no ``content``, which is indistinguishable from a model that
        returned nothing. Retry a couple of times — logging one line per retry
        via ``tqdm.write`` so CLI and web job logs show it — then raise
        ``LLMResponseError`` if it stays empty. Only empty content retries;
        a non-empty response is returned as-is (its caller parses JSON).
        """
        attempts = len(_EMPTY_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            raise_if_cancelled()
            text = call()
            if text.strip():
                return text
            if attempt == attempts:
                break
            tqdm.write(f"  LLM returned an empty response; retrying ({attempt}/{attempts - 1})…")
            delay = _EMPTY_RETRY_DELAYS[attempt - 1]
            event = current_cancel_event()
            if event is not None:
                # Interruptible wait, so Stop ends the retry instead of sleeping it out.
                if event.wait(delay):
                    raise_if_cancelled()
            else:
                time.sleep(delay)
        raise LLMResponseError(
            f"The {self.provider} model {self.model!r} returned an empty response "
            f"after {attempts} attempts."
        )

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return free-text completion. System prompt is provider-native;
        user prompt is a single message."""

    @abstractmethod
    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """Return structured JSON matching `schema` (JSON-Schema subset).

        `schema` is expected to describe a top-level object with typed
        properties; each concrete client maps this to its native JSON mode.
        Callers must treat the result as untrusted and validate field shapes
        before use.
        """
