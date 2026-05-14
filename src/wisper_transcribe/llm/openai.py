"""OpenAI client.

Uses chat.completions with response_format={"type": "json_schema"} for
structured output (OpenAI SDK >=1.50).
"""
from __future__ import annotations

import json

from .base import LLMClient, _strip_json_fence
from .errors import LLMResponseError, LLMUnavailableError


def _load_sdk():
    try:
        import openai
    except ImportError as exc:
        raise LLMUnavailableError(
            "openai SDK not installed. "
            "Run: pip install 'wisper-transcribe[llm-openai]'"
        ) from exc
    return openai


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str, api_key: str, temperature: float = 0.2,
                 max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        sdk = _load_sdk()
        self._client = sdk.OpenAI(api_key=api_key)
        self._errors = (sdk.APIError, sdk.APIConnectionError)

    def _extract_text(self, completion) -> str:
        if not completion.choices:
            raise LLMResponseError("OpenAI response contained no choices")
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise LLMResponseError("OpenAI response content was not a string")
        return content

    def complete(self, system: str, user: str) -> str:
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except self._errors as exc:
            raise LLMUnavailableError(f"OpenAI API error: {exc}") from exc
        return self._extract_text(completion)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": schema,
                "strict": True,
            },
        }
        try:
            completion = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format=response_format,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except self._errors as exc:
            raise LLMUnavailableError(f"OpenAI API error: {exc}") from exc
        text = _strip_json_fence(self._extract_text(completion))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"OpenAI JSON response did not parse: {exc}. Raw: {text[:200]!r}"
            ) from exc
