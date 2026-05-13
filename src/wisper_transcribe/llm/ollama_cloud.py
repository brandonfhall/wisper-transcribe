"""Ollama Cloud client — direct calls to ollama.com without the local daemon.

Inherits OllamaClient's streaming /api/chat logic; the only difference is the
default endpoint (https://ollama.com) and a required Bearer token. Used when
the user picks `llm_provider = "ollama-cloud"` and supplies an OLLAMA_API_KEY
(env or `ollama_cloud_api_key` in config).

Alternative path: the user keeps `llm_provider = "ollama"` and picks a model
with `-cloud` suffix (e.g. `gpt-oss:120b-cloud`). In that case the local
daemon proxies to ollama.com using `ollama signin` credentials, and this
class is not involved.
"""
from __future__ import annotations

from .ollama import OllamaClient


class OllamaCloudClient(OllamaClient):
    provider = "ollama-cloud"

    def __init__(self, model: str, api_key: str,
                 endpoint: str = "https://ollama.com",
                 temperature: float = 0.2, timeout: float = 30.0):
        if not api_key:
            from .errors import LLMUnavailableError
            raise LLMUnavailableError(
                "Ollama Cloud requires an API key. "
                "Set OLLAMA_API_KEY or `wisper config set ollama_cloud_api_key <key>`."
            )
        super().__init__(model=model, endpoint=endpoint, temperature=temperature,
                         timeout=timeout, api_key=api_key)
