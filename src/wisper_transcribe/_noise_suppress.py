"""Centralised suppression of third-party log/warning spam.

Call suppress_third_party_noise() as the first thing in any process or
subprocess that will load pyannote-audio, Lightning, or speechbrain.
The filters must be in place *before* those packages are imported so that
the Lightning compat-shim redirect warnings are already filtered when the
import triggers them.

Set WISPER_DEBUG=1 to disable suppression and see the raw output.
"""
from __future__ import annotations

import logging
import os
import warnings


class _SilenceFilter(logging.Filter):
    """Blocks all log records on a logger — immune to setLevel() resets.

    Lightning resets its own loggers' levels to INFO during import, which
    undoes a plain setLevel(ERROR) call.  A Filter persists independently
    of the level, so it cannot be overridden by downstream package init code.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: ARG002
        return False


def _silence_logger(name: str) -> None:
    """Apply a permanent silence filter to a named logger.

    Sets setLevel(ERROR) so child loggers inherit a high effective level and
    drop messages before any handler or filter runs.  Also attaches a
    _SilenceFilter for defense-in-depth (persists even if downstream package
    init code resets the level), and sets propagate=False so records do not
    leak to root-logger handlers.  A NullHandler is added to prevent the
    'no handlers found' warning.
    """
    logger = logging.getLogger(name)
    # Idempotent: skip if already silenced.
    if any(isinstance(f, _SilenceFilter) for f in logger.filters):
        return
    logger.setLevel(logging.ERROR)
    logger.addFilter(_SilenceFilter())
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())


def suppress_third_party_noise() -> None:
    """Filter warnings and logging noise from Lightning / pyannote / speechbrain.

    Safe to call multiple times — filterwarnings is additive and _silence_logger
    is idempotent.  In subprocess workers, call this before any heavy ML import
    so there is no window where warnings can leak.
    """
    if os.environ.get("WISPER_DEBUG"):
        return

    # ── warnings.warn() filters ───────────────────────────────────────────────
    _f = warnings.filterwarnings

    # speechbrain module-redirect deprecations (inspect.getmembers during load)
    _f("ignore", message=r"Module 'speechbrain\..+' was deprecated")
    # pyannote TF32 reproducibility advisory (not relevant for inference)
    _f("ignore", module=r"pyannote\.audio\.utils\.reproducibility")
    # pyannote pooling std() on short/silent segments
    _f("ignore", message=r"std\(\): degrees of freedom is <= 0")
    # Lightning migration shim: "Redirecting import of pytorch_lightning..."
    _f("ignore", message=r"Redirecting import of pytorch_lightning")
    # Lightning checkpoint auto-upgrade notification (v1.x → v2.x)
    _f("ignore", message=r"Lightning automatically upgraded your loaded checkpoint")
    # Lightning: multiple ModelCheckpoint states in old checkpoint
    _f("ignore", message=r"You have multiple `ModelCheckpoint` callback states")
    # pyannote: task-dependent loss in checkpoint, unused at inference
    _f("ignore", message=r"Model has been trained with a task-dependent loss function")
    # Lightning: extra checkpoint keys not in inference model (harmless)
    _f("ignore", message=r"Found keys that are not in the model state dict but in the checkpoint")

    # HuggingFace Hub symlink warning on Windows (informational, not actionable)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    # ── logging suppression ───────────────────────────────────────────────────
    # Lightning resets its own loggers to INFO during import, so setLevel(ERROR)
    # alone is not reliable.  _silence_logger() attaches a persistent Filter and
    # sets propagate=False — both survive Lightning's own logging setup.
    for _name in (
        "lightning",
        "lightning.pytorch",
        "lightning.pytorch.utilities",
        "lightning.pytorch.utilities.migration",
        "pytorch_lightning",
    ):
        _silence_logger(_name)

    # torch.utils.flop_counter logs "triton not found" via standard Python logging
    _silence_logger("torch")

    # absl "triton not found" flop-counter log (absl has its own logging hierarchy
    # separate from Python's — logging.getLogger("absl") has no effect on it)
    try:
        import absl.logging as _absl
        _absl.set_verbosity(_absl.ERROR)
    except ImportError:
        pass
