"""Tests for the LLM client layer.

The SDKs (anthropic, openai, google-genai) are not required by the core
install, so tests mock them out via sys.modules injection. The Ollama client
mocks httpx directly.
"""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from wisper_transcribe.llm.errors import LLMResponseError, LLMUnavailableError


# ---------------------------------------------------------------------------
# get_client factory
# ---------------------------------------------------------------------------

def test_get_client_unknown_provider():
    from wisper_transcribe.llm import get_client

    with pytest.raises(ValueError, match="Unknown LLM provider"):
        get_client("nope", config={})


def test_get_client_ollama_no_key_required(tmp_path, monkeypatch):
    from wisper_transcribe.llm import get_client

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    client = get_client("ollama", config={
        "llm_provider": "ollama",
        "llm_model": "llama3.1:8b",
        "llm_endpoint": "http://localhost:11434",
        "llm_temperature": 0.3,
    })
    assert client.provider == "ollama"
    assert client.model == "llama3.1:8b"
    assert client.temperature == 0.3


def test_get_client_anthropic_missing_key(tmp_path, monkeypatch):
    from wisper_transcribe.llm import get_client

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMUnavailableError, match="No API key"):
        get_client("anthropic", config={
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "anthropic_api_key": "",
        })


# ---------------------------------------------------------------------------
# OllamaClient
# ---------------------------------------------------------------------------

def _fake_stream_context(chunks: list[dict], raise_on_enter: Exception | None = None):
    """Build a mock ``httpx.stream()`` context manager.

    ``chunks`` is a list of dicts that will be JSON-serialised and yielded as
    lines by ``resp.iter_lines()``.  Set ``raise_on_enter`` to an
    ``httpx.HTTPError`` subclass instance to simulate a connection failure.
    """
    import httpx as _httpx

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines.return_value = iter(json.dumps(c) for c in chunks)

    cm = MagicMock()
    if raise_on_enter is not None:
        cm.__enter__ = MagicMock(side_effect=raise_on_enter)
    else:
        cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _ollama_chunks(content: str) -> list[dict]:
    """Turn a content string into a minimal list of streaming chunks."""
    return [
        {"message": {"role": "assistant", "content": content}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True},
    ]


def test_ollama_complete_ok():
    from wisper_transcribe.llm.ollama import OllamaClient

    client = OllamaClient(model="llama3.1:8b")
    fake_cm = _fake_stream_context(_ollama_chunks("hi there"))
    with patch("httpx.stream", return_value=fake_cm) as mock_stream:
        result = client.complete("sys", "user msg")
    assert result == "hi there"
    _, kwargs = mock_stream.call_args
    assert kwargs["json"]["model"] == "llama3.1:8b"
    assert kwargs["json"]["messages"][0]["role"] == "system"
    assert kwargs["json"]["stream"] is True


def test_ollama_complete_json_ok():
    from wisper_transcribe.llm.ollama import OllamaClient

    client = OllamaClient(model="llama3.1:8b")
    payload = json.dumps({"changes": [{"original": "a", "corrected": "b"}]})
    fake_cm = _fake_stream_context(_ollama_chunks(payload))
    with patch("httpx.stream", return_value=fake_cm):
        data = client.complete_json("sys", "user",
                                    {"type": "object", "properties": {"changes": {"type": "array"}}})
    assert data == {"changes": [{"original": "a", "corrected": "b"}]}


def test_ollama_connect_error_mentions_daemon():
    from wisper_transcribe.llm.ollama import OllamaClient
    import httpx

    client = OllamaClient(model="llama3.1:8b")
    fake_cm = _fake_stream_context([], raise_on_enter=httpx.ConnectError("refused"))
    with patch("httpx.stream", return_value=fake_cm):
        with pytest.raises(LLMUnavailableError, match="Cannot connect to Ollama"):
            client.complete("sys", "user")


def test_ollama_404_raises_model_not_found():
    from wisper_transcribe.llm.ollama import OllamaClient
    import httpx

    client = OllamaClient(model="nosuchmodel:7b")
    req = httpx.Request("POST", "http://localhost:11434/api/chat")
    fake_resp = httpx.Response(404, request=req)
    status_exc = httpx.HTTPStatusError("404 Not Found", request=req, response=fake_resp)

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=status_exc)
    resp.iter_lines.return_value = iter([])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=cm):
        with pytest.raises(LLMUnavailableError, match="not found in Ollama"):
            client.complete("sys", "user")


def test_ollama_non404_http_status_raises_generic():
    """A non-404 HTTP error (e.g. 500) raises LLMUnavailableError with a generic message."""
    from wisper_transcribe.llm.ollama import OllamaClient
    import httpx

    client = OllamaClient(model="llama3.1:8b")
    req = httpx.Request("POST", "http://localhost:11434/api/chat")
    fake_resp = httpx.Response(500, request=req)
    status_exc = httpx.HTTPStatusError("500 Internal Server Error", request=req, response=fake_resp)

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=status_exc)
    resp.iter_lines.return_value = iter([])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=cm):
        with pytest.raises(LLMUnavailableError, match="Ollama request failed"):
            client.complete("sys", "user")


def test_ollama_bad_json_raises_response_error():
    from wisper_transcribe.llm.ollama import OllamaClient

    client = OllamaClient(model="llama3.1:8b")
    fake_cm = _fake_stream_context(_ollama_chunks("not valid json"))
    with patch("httpx.stream", return_value=fake_cm):
        with pytest.raises(LLMResponseError, match="did not parse"):
            client.complete_json("sys", "user", {"type": "object"})


# ---------------------------------------------------------------------------
# AnthropicClient (mocked SDK)
# ---------------------------------------------------------------------------

def _install_fake_anthropic(monkeypatch):
    """Install a fake `anthropic` module into sys.modules for lazy imports."""
    fake = types.ModuleType("anthropic")

    class _APIError(Exception):
        pass

    class _APIConnectionError(_APIError):
        pass

    class _Anthropic:
        def __init__(self, api_key):
            self.api_key = api_key
            self.messages = MagicMock()

    fake.Anthropic = _Anthropic
    fake.APIError = _APIError
    fake.APIConnectionError = _APIConnectionError
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return fake


def _fake_text_msg(text: str):
    block = MagicMock()
    block.text = text
    block.type = "text"
    msg = MagicMock()
    msg.content = [block]
    return msg


def _fake_tool_use_msg(name: str, payload: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = payload
    msg = MagicMock()
    msg.content = [block]
    return msg


def test_anthropic_complete_joins_text_blocks(monkeypatch):
    _install_fake_anthropic(monkeypatch)
    from wisper_transcribe.llm.anthropic import AnthropicClient

    client = AnthropicClient(model="claude-sonnet-4-6", api_key="sk-test")
    client._client.messages.create.return_value = _fake_text_msg("hello world")
    out = client.complete("sys", "user")
    assert out == "hello world"


def test_anthropic_complete_json_extracts_tool_use(monkeypatch):
    _install_fake_anthropic(monkeypatch)
    from wisper_transcribe.llm.anthropic import AnthropicClient

    client = AnthropicClient(model="claude-sonnet-4-6", api_key="sk-test")
    client._client.messages.create.return_value = _fake_tool_use_msg(
        "respond", {"changes": [{"original": "Kira", "corrected": "Kyra"}]}
    )
    out = client.complete_json("sys", "user", {"type": "object"})
    assert out == {"changes": [{"original": "Kira", "corrected": "Kyra"}]}


def test_anthropic_api_error_raises_unavailable(monkeypatch):
    fake = _install_fake_anthropic(monkeypatch)
    from wisper_transcribe.llm.anthropic import AnthropicClient

    client = AnthropicClient(model="claude-sonnet-4-6", api_key="sk-test")
    client._client.messages.create.side_effect = fake.APIError("500")
    with pytest.raises(LLMUnavailableError, match="Anthropic API error"):
        client.complete("sys", "user")


def test_anthropic_missing_sdk_raises_unavailable(monkeypatch):
    # Force ImportError at the lazy SDK loader
    monkeypatch.setitem(sys.modules, "anthropic", None)
    from wisper_transcribe.llm.anthropic import AnthropicClient

    with pytest.raises(LLMUnavailableError, match="anthropic SDK not installed"):
        AnthropicClient(model="x", api_key="y")


# ---------------------------------------------------------------------------
# OpenAIClient (mocked SDK)
# ---------------------------------------------------------------------------

def _install_fake_openai(monkeypatch):
    fake = types.ModuleType("openai")

    class _APIError(Exception):
        pass

    class _APIConnectionError(_APIError):
        pass

    class _OpenAI:
        def __init__(self, api_key):
            self.api_key = api_key
            self.chat = MagicMock()
            self.chat.completions = MagicMock()

    fake.OpenAI = _OpenAI
    fake.APIError = _APIError
    fake.APIConnectionError = _APIConnectionError
    monkeypatch.setitem(sys.modules, "openai", fake)
    return fake


def _fake_chat_completion(content: str):
    choice = MagicMock()
    choice.message.content = content
    completion = MagicMock()
    completion.choices = [choice]
    return completion


def test_openai_complete_ok(monkeypatch):
    _install_fake_openai(monkeypatch)
    from wisper_transcribe.llm.openai import OpenAIClient

    client = OpenAIClient(model="gpt-4o-mini", api_key="sk-test")
    client._client.chat.completions.create.return_value = _fake_chat_completion("hello")
    assert client.complete("sys", "user") == "hello"


def test_openai_complete_json_parses(monkeypatch):
    _install_fake_openai(monkeypatch)
    from wisper_transcribe.llm.openai import OpenAIClient

    client = OpenAIClient(model="gpt-4o-mini", api_key="sk-test")
    client._client.chat.completions.create.return_value = _fake_chat_completion(
        json.dumps({"changes": []})
    )
    assert client.complete_json("sys", "user", {"type": "object"}) == {"changes": []}


def test_openai_bad_json_raises(monkeypatch):
    _install_fake_openai(monkeypatch)
    from wisper_transcribe.llm.openai import OpenAIClient

    client = OpenAIClient(model="gpt-4o-mini", api_key="sk-test")
    client._client.chat.completions.create.return_value = _fake_chat_completion("not json")
    with pytest.raises(LLMResponseError):
        client.complete_json("sys", "user", {"type": "object"})


# ---------------------------------------------------------------------------
# GoogleClient (mocked SDK)
# ---------------------------------------------------------------------------

def _install_fake_google(monkeypatch):
    # google.genai namespace
    google_pkg = types.ModuleType("google")
    google_pkg.__path__ = []  # make it a package
    genai = types.ModuleType("google.genai")
    types_mod = types.ModuleType("google.genai.types")

    class _GenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    types_mod.GenerateContentConfig = _GenerateContentConfig

    class _Client:
        def __init__(self, api_key):
            self.api_key = api_key
            self.models = MagicMock()

    genai.Client = _Client
    genai.types = types_mod
    google_pkg.genai = genai
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    return genai


def test_google_complete_ok(monkeypatch):
    _install_fake_google(monkeypatch)
    from wisper_transcribe.llm.google import GoogleClient

    client = GoogleClient(model="gemini-1.5-flash", api_key="k")
    resp = MagicMock()
    resp.text = "session recap"
    client._client.models.generate_content.return_value = resp
    assert client.complete("sys", "user") == "session recap"


def test_google_complete_json_parses(monkeypatch):
    _install_fake_google(monkeypatch)
    from wisper_transcribe.llm.google import GoogleClient

    client = GoogleClient(model="gemini-1.5-flash", api_key="k")
    resp = MagicMock()
    resp.text = json.dumps({"summary": "ok"})
    client._client.models.generate_content.return_value = resp
    out = client.complete_json("sys", "user", {"type": "object"})
    assert out == {"summary": "ok"}


def test_google_generate_error_raises_unavailable(monkeypatch):
    _install_fake_google(monkeypatch)
    from wisper_transcribe.llm.google import GoogleClient

    client = GoogleClient(model="gemini-1.5-flash", api_key="k")
    client._client.models.generate_content.side_effect = RuntimeError("boom")
    with pytest.raises(LLMUnavailableError, match="Google API error"):
        client.complete("sys", "user")
