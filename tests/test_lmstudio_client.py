"""Tests for the LM Studio LLM client."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from wisper_transcribe.llm.errors import LLMResponseError, LLMUnavailableError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse_lines(content: str) -> list[str]:
    """Minimal SSE stream for a complete response."""
    return [
        f'data: {json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": None}]})}',
        f'data: {json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})}',
        "data: [DONE]",
    ]


def _fake_stream_ctx(lines: list[str], raise_on_enter: Exception | None = None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    cm = MagicMock()
    if raise_on_enter is not None:
        cm.__enter__ = MagicMock(side_effect=raise_on_enter)
    else:
        cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# get_client factory
# ---------------------------------------------------------------------------

def test_get_client_lmstudio_no_key_required(tmp_path, monkeypatch):
    from wisper_transcribe.llm import get_client

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    client = get_client("lmstudio", config={
        "llm_provider": "lmstudio",
        "llm_model": "phi-3",
        "llm_endpoint": "http://localhost:1234",
        "llm_temperature": 0.3,
    })
    assert client.provider == "lmstudio"
    assert client.model == "phi-3"
    assert client.temperature == 0.3


def test_get_client_lmstudio_default_endpoint(tmp_path, monkeypatch):
    from wisper_transcribe.llm import get_client

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    client = get_client("lmstudio", config={"llm_provider": "lmstudio"})
    assert client.endpoint == "http://localhost:1234"


# ---------------------------------------------------------------------------
# LMStudioClient happy paths
# ---------------------------------------------------------------------------

def test_lmstudio_complete_ok():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(_sse_lines("hello world"))
    with patch("httpx.stream", return_value=fake_cm) as mock_stream:
        result = client.complete("sys", "user msg")

    assert result == "hello world"
    _, kwargs = mock_stream.call_args
    assert kwargs["json"]["model"] == "phi-3"
    assert kwargs["json"]["stream"] is True
    assert kwargs["json"]["messages"][0]["role"] == "system"


def test_lmstudio_complete_json_ok():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    payload = json.dumps({"changes": [{"original": "a", "corrected": "b"}]})
    fake_cm = _fake_stream_ctx(_sse_lines(payload))
    with patch("httpx.stream", return_value=fake_cm):
        data = client.complete_json("sys", "user", {"type": "object"})

    assert data == {"changes": [{"original": "a", "corrected": "b"}]}


def test_lmstudio_complete_json_uses_json_object_format():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(_sse_lines('{"x": 1}'))
    with patch("httpx.stream", return_value=fake_cm) as mock_stream:
        client.complete_json("sys", "user", {"type": "object"})

    assert mock_stream.call_args[1]["json"]["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# LMStudioClient error paths
# ---------------------------------------------------------------------------

def test_lmstudio_connect_error_mentions_local_server():
    import httpx
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx([], raise_on_enter=httpx.ConnectError("refused"))
    with patch("httpx.stream", return_value=fake_cm):
        with pytest.raises(LLMUnavailableError, match="Cannot connect to LM Studio"):
            client.complete("sys", "user")


def test_lmstudio_404_raises_model_not_loaded():
    import httpx
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="no-such-model")
    req = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    fake_resp = httpx.Response(404, request=req)
    exc = httpx.HTTPStatusError("404", request=req, response=fake_resp)

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=exc)
    resp.iter_lines.return_value = iter([])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=cm):
        with pytest.raises(LLMUnavailableError, match="not found in LM Studio"):
            client.complete("sys", "user")


def test_lmstudio_non404_http_status_raises_generic():
    import httpx
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    req = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    fake_resp = httpx.Response(500, request=req)
    exc = httpx.HTTPStatusError("500", request=req, response=fake_resp)

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=exc)
    resp.iter_lines.return_value = iter([])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=cm):
        with pytest.raises(LLMUnavailableError, match="LM Studio request failed"):
            client.complete("sys", "user")


def test_lmstudio_bad_json_raises_response_error():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(_sse_lines("not valid json"))
    with patch("httpx.stream", return_value=fake_cm):
        with pytest.raises(LLMResponseError, match="did not parse"):
            client.complete_json("sys", "user", {"type": "object"})


def test_lmstudio_ignores_non_data_lines():
    """Non-SSE lines (empty, comment) are skipped without error."""
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    lines = ["", ": keep-alive"] + _sse_lines("hi")
    fake_cm = _fake_stream_ctx(lines)
    with patch("httpx.stream", return_value=fake_cm):
        assert client.complete("sys", "user") == "hi"
