"""Tests for wisper_transcribe._noise_suppress."""
from __future__ import annotations

import logging
import warnings
from unittest.mock import patch

import pytest


def test_suppress_filters_lightning_warnings(monkeypatch):
    """suppress_third_party_noise installs warning filters for Lightning messages."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    # Clear existing filters so we can check ours were added
    with warnings.catch_warnings(record=True):
        warnings.resetwarnings()
        from wisper_transcribe._noise_suppress import suppress_third_party_noise
        suppress_third_party_noise()

        # Lightning redirect warning should be suppressed
        with warnings.catch_warnings(record=True) as w:
            warnings.warn("Redirecting import of pytorch_lightning", UserWarning)
            lightning_warnings = [x for x in w if "Redirecting" in str(x.message)]
            assert len(lightning_warnings) == 0


def test_suppress_sets_lightning_loggers_to_error(monkeypatch):
    """suppress_third_party_noise sets Lightning loggers to ERROR level."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    for name in ("lightning", "lightning.pytorch", "pytorch_lightning"):
        assert logging.getLogger(name).level >= logging.ERROR


def test_suppress_noop_when_wisper_debug_set(monkeypatch):
    """When WISPER_DEBUG=1, suppression is skipped entirely."""
    monkeypatch.setenv("WISPER_DEBUG", "1")

    # Reset lightning loggers to a known level
    for name in ("lightning", "lightning.pytorch", "pytorch_lightning"):
        logging.getLogger(name).setLevel(logging.WARNING)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    # Loggers should NOT have been set to ERROR (suppression was skipped)
    assert logging.getLogger("lightning").level == logging.WARNING


def test_suppress_handles_missing_absl(monkeypatch):
    """suppress_third_party_noise doesn't crash when absl is not installed."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    with patch.dict("sys.modules", {"absl": None, "absl.logging": None}):
        from wisper_transcribe._noise_suppress import suppress_third_party_noise
        suppress_third_party_noise()  # must not raise


def test_suppress_filters_speechbrain_deprecation(monkeypatch):
    """speechbrain module-redirect deprecations are filtered."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    with warnings.catch_warnings(record=True) as w:
        warnings.warn(
            "Module 'speechbrain.foo.bar' was deprecated", UserWarning
        )
        sb_warnings = [x for x in w if "speechbrain" in str(x.message)]
        assert len(sb_warnings) == 0


def test_suppress_filters_checkpoint_upgrade(monkeypatch):
    """Lightning checkpoint auto-upgrade warnings are filtered."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    with warnings.catch_warnings(record=True) as w:
        warnings.warn(
            "Lightning automatically upgraded your loaded checkpoint from v1 to v2",
            UserWarning,
        )
        upgrade_warnings = [x for x in w if "upgraded" in str(x.message)]
        assert len(upgrade_warnings) == 0


def test_suppress_sets_hf_hub_symlink_env_var(monkeypatch):
    """suppress_third_party_noise sets HF_HUB_DISABLE_SYMLINKS_WARNING=1."""
    import os
    monkeypatch.delenv("WISPER_DEBUG", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    assert os.environ.get("HF_HUB_DISABLE_SYMLINKS_WARNING") == "1"


def test_suppress_sets_torch_logger_to_error(monkeypatch):
    """suppress_third_party_noise sets torch logger to ERROR to silence flop_counter."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    assert logging.getLogger("torch").level >= logging.ERROR
