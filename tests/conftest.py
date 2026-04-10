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
