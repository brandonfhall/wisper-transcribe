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
    "similarity_threshold": 0.55,
    # Off so no test loads the real aligner on a GPU machine; forced-alignment
    # tests set it explicitly.
    "forced_alignment": "false",
    # Off so no test reaches a real spawned ML child; ml_worker/delegation
    # tests turn it on explicitly.
    "ml_worker": False,
}


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    """Point WISPER_DATA_DIR at a fresh temp dir so no test reads or writes
    the developer's real data (wisper.db, config, transcripts). Tests that
    need a specific dir still set their own (inner setenv/patch wins)."""
    data_dir = tmp_path_factory.mktemp("wisper_data")
    monkeypatch.setenv("WISPER_DATA_DIR", str(data_dir))
    # The root get_output_root() resolves to while WISPER_OUTPUT_DIR is unset;
    # wisper never creates it itself.
    (data_dir / "output").mkdir()
    # A dev shell or CI with WISPER_OUTPUT_DIR set must not leak into tests.
    monkeypatch.delenv("WISPER_OUTPUT_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_output_env_guard(monkeypatch):
    """The unmerged-build guard needs WISPER_OUTPUT_DIR, which the tests
    above deliberately leave unset. Guard tests turn it back on."""
    monkeypatch.setattr("wisper_transcribe.db.REQUIRE_OUTPUT_ENV", False)


@pytest.fixture(autouse=True)
def _reset_file_registry_state():
    """The cached sync report and its 30 s throttle are per process; a test
    must not inherit another's."""
    from wisper_transcribe import file_registry

    file_registry.reset_state()
    yield
    file_registry.reset_state()


@pytest.fixture(autouse=True)
def _no_search_backfill_thread(monkeypatch):
    """The web lifespan would start the search backfill thread, racing tests'
    DB assertions. Search tests call search_index.run_backfill() directly."""
    monkeypatch.setattr("wisper_transcribe.search_index.AUTOSTART_WORKER", False)


@pytest.fixture(autouse=True)
def _isolated_pipeline_config():
    """Patch pipeline.load_config so tests never read the real user config."""
    with patch(
        "wisper_transcribe.pipeline.load_config",
        return_value=dict(_BASE_CONFIG),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_ml_worker():
    """Keep the whole suite in-process: no test spawns a real ML child.

    ``jobs.load_config`` reads the developer's real config, whose default for
    ``ml_worker`` is now True, so patching the module switch (not just the
    config) is what actually guarantees no delegation. Tests that exercise
    delegation patch ``_ml_worker_enabled``/``_delegation`` themselves."""
    with patch("wisper_transcribe.web.jobs._ml_worker_enabled", return_value=False):
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


@pytest.fixture(autouse=True)
def _block_discord_network():
    """Prevent tests from opening real Discord connections or sockets.

    Replaces _unix_socket_source with a no-op generator so any BotManager
    created without an explicit audio_source_factory does nothing instead of
    trying to launch the JDA subprocess or bind a Unix socket.
    Tests that need scripted audio inject their own factory via BotManager().
    """
    async def _null_source(*_a, **_kw):
        return
        yield  # makes this an async generator

    with patch(
        "wisper_transcribe.web.discord_bot._unix_socket_source", _null_source
    ):
        yield
