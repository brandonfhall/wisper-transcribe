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


def _fake_stream_ctx(lines: list[str], raise_on_enter: Exception | None = None,
                     raise_on_read: Exception | None = None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if raise_on_read is not None:
        def _lines():
            raise raise_on_read
            yield  # pragma: no cover — makes this a generator
        resp.iter_lines.side_effect = _lines
    else:
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

def test_lmstudio_stream_uses_idle_read_timeout():
    """The stream timeout must set read to the shared idle constant, not None."""
    from wisper_transcribe.llm.base import _STREAM_IDLE_TIMEOUT
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(_sse_lines("hi"))
    with patch("httpx.stream", return_value=fake_cm) as mock_stream:
        client.complete("sys", "user")

    timeout = mock_stream.call_args.kwargs["timeout"]
    assert timeout.read == _STREAM_IDLE_TIMEOUT
    assert timeout.connect == client.timeout


def test_lmstudio_read_timeout_raises_unavailable():
    """A stalled stream (httpx.ReadTimeout) becomes LLMUnavailableError naming the model."""
    import httpx
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx([], raise_on_read=httpx.ReadTimeout("timed out"))
    with patch("httpx.stream", return_value=fake_cm):
        with pytest.raises(LLMUnavailableError, match="phi-3"):
            client.complete("sys", "user")


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


def test_lmstudio_complete_json_strips_code_fence():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(_sse_lines('```json\n{"changes": []}\n```'))
    with patch("httpx.stream", return_value=fake_cm):
        data = client.complete_json("sys", "user", {"type": "object"})
    assert data == {"changes": []}


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


# ---------------------------------------------------------------------------
# LMStudioClient retry on empty content
# ---------------------------------------------------------------------------

def test_lmstudio_complete_retries_an_empty_response():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    empty = _fake_stream_ctx(_sse_lines(""))
    content = _fake_stream_ctx(_sse_lines("finally"))
    with patch("httpx.stream", side_effect=[empty, content]) as mock_stream, \
         patch("time.sleep"):
        result = client.complete("sys", "user")

    assert result == "finally"
    assert mock_stream.call_count == 2


def test_lmstudio_complete_empty_exhausts_retries():
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    empties = [_fake_stream_ctx(_sse_lines("")) for _ in range(3)]
    with patch("httpx.stream", side_effect=empties) as mock_stream, \
         patch("time.sleep"):
        with pytest.raises(LLMResponseError, match="empty response"):
            client.complete("sys", "user")

    assert mock_stream.call_count == 3


def test_lmstudio_stream_error_field_raises_unavailable():
    """A streamed `error` field fails immediately, with no retry."""
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(
        [f'data: {json.dumps({"error": "model failed to load"})}']
    )
    with patch("httpx.stream", return_value=fake_cm) as mock_stream:
        with pytest.raises(LLMUnavailableError, match="model failed to load"):
            client.complete("sys", "user")

    assert mock_stream.call_count == 1


def test_lmstudio_complete_json_bad_json_does_not_retry():
    """Non-empty unparseable JSON raises at once (empty-only retry)."""
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    fake_cm = _fake_stream_ctx(_sse_lines("not valid json"))
    with patch("httpx.stream", return_value=fake_cm) as mock_stream:
        with pytest.raises(LLMResponseError, match="did not parse"):
            client.complete_json("sys", "user", {"type": "object"})

    assert mock_stream.call_count == 1


# ---------------------------------------------------------------------------
# LMStudioClient cancellation via the thread-local cancel scope
# ---------------------------------------------------------------------------

def _blocking_stream_ctx(started, release):
    """A stream whose iteration blocks until ``release`` is set."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()

    def _lines():
        started.set()
        release.wait(5)
        yield 'data: {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}'

    resp.iter_lines.side_effect = _lines
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_lmstudio_complete_cancels_a_blocked_stream():
    """Inside a cancel_scope, Stop ends a blocked stream within ~1 s."""
    import threading
    import time

    from wisper_transcribe.llm.cancel import cancel_scope
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    started, release = threading.Event(), threading.Event()
    fake_cm = _blocking_stream_ctx(started, release)
    cancel = threading.Event()

    def _cancel_later() -> None:
        started.wait(2)
        cancel.set()

    threading.Thread(target=_cancel_later, daemon=True).start()
    try:
        with patch("httpx.stream", return_value=fake_cm):
            with cancel_scope(cancel):
                began = time.monotonic()
                with pytest.raises(InterruptedError, match="Job cancelled by user"):
                    client.complete("sys", "user")
                assert time.monotonic() - began < 1.0
    finally:
        release.set()


def test_lmstudio_complete_outside_scope_waits_for_the_stream():
    """With no scope the caller simply waits for the reader thread to finish."""
    import threading

    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")
    started, release = threading.Event(), threading.Event()
    fake_cm = _blocking_stream_ctx(started, release)

    def _release_later() -> None:
        started.wait(2)
        release.set()

    threading.Thread(target=_release_later, daemon=True).start()
    with patch("httpx.stream", return_value=fake_cm):
        assert client.complete("sys", "user") == "late"


def test_lmstudio_reader_thread_error_propagates_unchanged():
    """An error raised on the reader thread re-raises in the caller as-is."""
    from wisper_transcribe.llm.lmstudio import LMStudioClient

    client = LMStudioClient(model="phi-3")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines.side_effect = LLMUnavailableError("reader blew up")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=resp)
    cm.__exit__ = MagicMock(return_value=False)

    with patch("httpx.stream", return_value=cm):
        with pytest.raises(LLMUnavailableError, match="reader blew up"):
            client.complete("sys", "user")
