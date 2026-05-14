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
from abc import ABC, abstractmethod


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
