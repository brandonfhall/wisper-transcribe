"""Anthropic Claude client.

Uses the Messages API. For JSON output, uses tool-use with a forced tool
(strict schema adherence) — the most reliable way to get structured output
from Claude as of the anthropic SDK >=0.39.
"""
from __future__ import annotations

import json

from .base import LLMClient
from .errors import LLMResponseError, LLMUnavailableError


def _load_sdk():
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailableError(
            "anthropic SDK not installed. "
            "Run: pip install 'wisper-transcribe[llm-anthropic]'"
        ) from exc
    return anthropic


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str, api_key: str, temperature: float = 0.2,
                 max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        sdk = _load_sdk()
        self._client = sdk.Anthropic(api_key=api_key)
        self._errors = (sdk.APIError, sdk.APIConnectionError)

    def complete(self, system: str, user: str) -> str:
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except self._errors as exc:
            raise LLMUnavailableError(f"Anthropic API error: {exc}") from exc

        # Text blocks are the default for non-tool-use responses.
        parts = []
        for block in msg.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        tool = {
            "name": "respond",
            "description": "Return the structured response.",
            "input_schema": schema,
        }
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": "respond"},
                messages=[{"role": "user", "content": user}],
            )
        except self._errors as exc:
            raise LLMUnavailableError(f"Anthropic API error: {exc}") from exc

        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "respond":
                data = getattr(block, "input", None)
                if isinstance(data, dict):
                    return data
                if isinstance(data, str):
                    try:
                        return json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise LLMResponseError(
                            f"Anthropic tool_use input did not parse: {exc}"
                        ) from exc
        raise LLMResponseError(
            "Anthropic response did not include the expected tool_use block."
        )
