"""LLM client package for wisper-transcribe post-processing.

Public surface:
    get_client(provider, config) -> LLMClient

Provider SDKs (anthropic, openai, google-genai) are imported lazily inside each
client class so a user with only Ollama installed never sees an ImportError for
a provider they don't use. Missing SDK → LLMUnavailableError with install hint.
"""
from __future__ import annotations

from typing import Optional

from .base import LLMClient
from .errors import LLMUnavailableError, LLMResponseError


def get_client(provider: str, config: Optional[dict] = None) -> LLMClient:
    """Instantiate an LLMClient for the given provider.

    Raises:
        LLMUnavailableError: provider SDK not installed, or required API key missing.
        ValueError: unknown provider string.
    """
    from ..config import LLM_PROVIDERS, get_llm_api_key, load_config, resolve_llm_model

    if provider not in LLM_PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider: {provider!r}. Choose from: {', '.join(LLM_PROVIDERS)}"
        )

    if config is None:
        config = load_config()
    model = resolve_llm_model(provider, override=None, config=config)
    temperature = float(config.get("llm_temperature", 0.2) or 0.2)

    if provider == "ollama":
        from .ollama import OllamaClient
        endpoint = config.get("llm_endpoint", "http://localhost:11434") or "http://localhost:11434"
        return OllamaClient(model=model, endpoint=endpoint, temperature=temperature)

    api_key = get_llm_api_key(provider, config=config)
    if not api_key:
        env_name = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
        }[provider]
        config_key = {
            "anthropic": "anthropic_api_key",
            "openai": "openai_api_key",
            "google": "google_api_key",
        }[provider]
        raise LLMUnavailableError(
            f"No API key for provider {provider!r}. "
            f"Set env var {env_name} or run `wisper config set {config_key} <key>`."
        )

    if provider == "anthropic":
        from .anthropic import AnthropicClient
        return AnthropicClient(model=model, api_key=api_key, temperature=temperature)
    if provider == "openai":
        from .openai import OpenAIClient
        return OpenAIClient(model=model, api_key=api_key, temperature=temperature)
    if provider == "google":
        from .google import GoogleClient
        return GoogleClient(model=model, api_key=api_key, temperature=temperature)

    # Should never reach here given the LLM_PROVIDERS check above.
    raise ValueError(f"Unknown LLM provider: {provider!r}")


__all__ = ["LLMClient", "LLMUnavailableError", "LLMResponseError", "get_client"]
