"""Tests for wisper_transcribe._noise_suppress."""
from __future__ import annotations

import logging
import os
import warnings
from unittest.mock import patch


def test_suppress_filters_lightning_warnings(monkeypatch):
    """suppress_third_party_noise installs warning filters for Lightning messages."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    with warnings.catch_warnings(record=True):
        warnings.resetwarnings()
        from wisper_transcribe._noise_suppress import suppress_third_party_noise
        suppress_third_party_noise()

        with warnings.catch_warnings(record=True) as w:
            warnings.warn("Redirecting import of pytorch_lightning", UserWarning)
            assert not [x for x in w if "Redirecting" in str(x.message)]


def test_suppress_silences_lightning_loggers(monkeypatch):
    """Lightning loggers are silenced via _SilenceFilter, not just setLevel."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import _SilenceFilter, suppress_third_party_noise
    suppress_third_party_noise()

    for name in ("lightning", "lightning.pytorch", "pytorch_lightning"):
        logger = logging.getLogger(name)
        assert any(isinstance(f, _SilenceFilter) for f in logger.filters), (
            f"Logger '{name}' is missing _SilenceFilter"
        )
        assert logger.propagate is False, (
            f"Logger '{name}' should have propagate=False"
        )


def test_silence_filter_persists_after_setlevel_reset(monkeypatch):
    """_SilenceFilter blocks records even after Lightning resets the log level."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import _SilenceFilter, suppress_third_party_noise
    suppress_third_party_noise()

    logger = logging.getLogger("lightning.pytorch")

    # Simulate Lightning resetting the level back to INFO on import
    logger.setLevel(logging.INFO)

    # Records should still be blocked by the filter
    record = logger.makeRecord(
        "lightning.pytorch", logging.INFO, "", 0,
        "Lightning automatically upgraded your loaded checkpoint", (), None
    )
    for f in logger.filters:
        if isinstance(f, _SilenceFilter):
            assert f.filter(record) is False
            break
    else:
        raise AssertionError("_SilenceFilter not found on lightning.pytorch logger")


def test_suppress_is_idempotent(monkeypatch):
    """Calling suppress_third_party_noise() twice does not add duplicate filters."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import _SilenceFilter, suppress_third_party_noise
    suppress_third_party_noise()
    suppress_third_party_noise()

    logger = logging.getLogger("lightning.pytorch")
    silence_count = sum(1 for f in logger.filters if isinstance(f, _SilenceFilter))
    assert silence_count == 1, "duplicate _SilenceFilter added on second call"


def test_suppress_noop_when_wisper_debug_set(monkeypatch):
    """When WISPER_DEBUG=1, suppression is skipped entirely."""
    monkeypatch.setenv("WISPER_DEBUG", "1")

    # Ensure the lightning logger is at WARNING before the call
    logging.getLogger("lightning").setLevel(logging.WARNING)
    # Remove any existing _SilenceFilters so we get a clean test
    from wisper_transcribe._noise_suppress import _SilenceFilter
    logger = logging.getLogger("lightning")
    logger.filters = [f for f in logger.filters if not isinstance(f, _SilenceFilter)]

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    # No _SilenceFilter should have been added
    assert not any(isinstance(f, _SilenceFilter) for f in logger.filters)


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
        warnings.warn("Module 'speechbrain.foo.bar' was deprecated", UserWarning)
        assert not [x for x in w if "speechbrain" in str(x.message)]


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
        assert not [x for x in w if "upgraded" in str(x.message)]


def test_suppress_sets_hf_hub_symlink_env_var(monkeypatch):
    """suppress_third_party_noise sets HF_HUB_DISABLE_SYMLINKS_WARNING=1."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    assert os.environ.get("HF_HUB_DISABLE_SYMLINKS_WARNING") == "1"


def test_suppress_sets_torch_logger_to_error(monkeypatch):
    """suppress_third_party_noise silences torch logger to suppress flop_counter."""
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import _SilenceFilter, suppress_third_party_noise
    suppress_third_party_noise()

    logger = logging.getLogger("torch")
    assert any(isinstance(f, _SilenceFilter) for f in logger.filters)


def test_torch_child_logger_blocked_after_suppress(monkeypatch):
    """torch.utils.flop_counter WARNING is blocked after suppress.

    Regression test: _silence_logger must call setLevel(ERROR) so child
    loggers inherit a high effective level.  A _SilenceFilter on the parent
    alone does NOT block records from child loggers — Python's callHandlers()
    bypasses parent logger filters for propagated records.
    """
    monkeypatch.delenv("WISPER_DEBUG", raising=False)

    from wisper_transcribe._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()

    child_logger = logging.getLogger("torch.utils.flop_counter")
    # isEnabledFor(WARNING) must be False — the effective level is ERROR,
    # inherited from the silenced "torch" parent.
    assert not child_logger.isEnabledFor(logging.WARNING), (
        "torch.utils.flop_counter should not process WARNING messages after suppress"
    )


def test_suppress_called_before_speechbrain_import_in_diarizer():
    """_suppress() must run before the speechbrain shim in diarizer.py.

    Regression test for the timing bug: speechbrain imports torch, which
    imports torch.utils.flop_counter, which fires a WARNING.  If suppress
    runs after that import, the warning leaks.  Verify that after importing
    diarizer, the torch logger is already silenced (meaning suppress ran
    before speechbrain was imported).
    """
    from wisper_transcribe._noise_suppress import _SilenceFilter

    # Importing diarizer triggers the full module-level setup.
    # We can't easily test import ORDER directly, but we can assert the
    # end state: torch logger must have _SilenceFilter after diarizer loads.
    import wisper_transcribe.diarizer  # noqa: F401

    logger = logging.getLogger("torch")
    assert any(isinstance(f, _SilenceFilter) for f in logger.filters), (
        "torch logger not silenced — suppress() may be running after speechbrain import"
    )


def test_suppress_called_at_module_level_in_speaker_manager():
    """_suppress() must run at module level in speaker_manager.py.

    Regression test: wisper enroll bypasses diarizer.py entirely and loads
    pyannote.audio (embedding model + Lightning) directly through
    speaker_manager._load_embedding_model().  If suppress() is not called
    before that import path, Lightning checkpoint-upgrade, migration-shim,
    TF32, and torch flop_counter warnings all leak to the terminal.

    Verify that importing speaker_manager leaves the torch logger silenced,
    proving suppress() ran before any ML package could fire.
    """
    from wisper_transcribe._noise_suppress import _SilenceFilter

    import wisper_transcribe.speaker_manager  # noqa: F401

    logger = logging.getLogger("torch")
    assert any(isinstance(f, _SilenceFilter) for f in logger.filters), (
        "torch logger not silenced after importing speaker_manager — "
        "_suppress() call is missing or mis-ordered in speaker_manager.py"
    )
