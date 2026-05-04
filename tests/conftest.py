"""Shared pytest fixtures for wisper-transcribe tests.

Key concern: several pipeline tests don't explicitly patch load_config(), so
they read the developer's real config file.  If parallel_stages=True is set
there, tests that mock wisper_transcribe.pipeline.transcribe will fail because
the mock doesn't carry through into spawned subprocesses.

The _isolated_pipeline_config autouse fixture prevents this by patching
load_config at the pipeline module with a safe baseline.  Tests that need
specific config values (e.g. the parallel_stages tests) override this with an
explicit `with patch(...)` block inside the test body — inner patches take
precedence over the fixture's outer patch.
"""
from unittest.mock import patch

import pytest


_BASE_CONFIG = {
    "model": "medium",
    "language": "en",
    "compute_type": "auto",
    "vad_filter": True,
    "hotwords": [],
    "use_mlx": "false",
    "parallel_stages": False,
    "similarity_threshold": 0.65,
}


@pytest.fixture(autouse=True)
def _isolated_pipeline_config():
    """Patch pipeline.load_config so tests never read the real user config."""
    with patch(
        "wisper_transcribe.pipeline.load_config",
        return_value=dict(_BASE_CONFIG),
    ):
        yield


@pytest.fixture(autouse=True)
def _block_real_llm_calls():
    """Block real HTTP calls to local LLM providers during tests.

    ``httpx.stream`` is the single function used by OllamaClient and
    LMStudioClient to make HTTP requests.  Patching it here ensures no test
    can accidentally launch a model or hit a running Ollama/LM Studio
    instance.  Tests that need to exercise the client HTTP layer (e.g.
    ``test_llm_clients.py``, ``test_lmstudio_client.py``) override this with
    their own ``patch("httpx.stream", ...)`` — inner patches take precedence.

    To write a test that exercises real Ollama HTTP interaction::

        import httpx
        from unittest.mock import MagicMock, patch

        def test_ollama_some_new_scenario():
            fake_cm = _fake_stream_context(_ollama_chunks("response"))
            # Inner patch overrides the conftest block.
            with patch("httpx.stream", return_value=fake_cm):
                client = OllamaClient(model="llama3.1:8b")
                result = client.complete("system", "user prompt")
            assert result == "response"
    """
    def _blocked(*a, **kw):
        raise RuntimeError(
            "Real LLM HTTP call blocked by conftest.py. "
            "Patch httpx.stream explicitly in your test."
        )

    with patch("httpx.stream", _blocked):
        yield
