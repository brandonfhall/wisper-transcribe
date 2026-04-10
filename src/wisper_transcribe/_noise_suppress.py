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


def suppress_third_party_noise() -> None:
    """Filter warnings and logging noise from Lightning / pyannote / speechbrain.

    Safe to call multiple times (filterwarnings is additive; setLevel is
    idempotent).  In subprocess workers, call this before any heavy ML import
    so there is no window where warnings can leak.
    """
    if os.environ.get("WISPER_DEBUG"):
        return

    # ── warnings.warn() filters ───────────────────────────────────────────────
    _f = warnings.filterwarnings

    # speechbrain module-redirect deprecations (inspect.getmembers during load)
    _f("ignore", message=r"Module 'speechbrain\..+' was deprecated", category=UserWarning)
    # pyannote TF32 reproducibility advisory (not relevant for inference)
    _f("ignore", module=r"pyannote\.audio\.utils\.reproducibility")
    # pyannote pooling std() on short/silent segments
    _f("ignore", message=r"std\(\): degrees of freedom is <= 0", category=UserWarning)
    # Lightning migration shim: "Redirecting import of pytorch_lightning..."
    _f("ignore", message=r"Redirecting import of pytorch_lightning")
    # Lightning checkpoint auto-upgrade notification (v1.x → v2.x)
    _f("ignore", message=r"Lightning automatically upgraded your loaded checkpoint")
    # Lightning: multiple ModelCheckpoint states in old checkpoint
    _f("ignore", message=r"You have multiple `ModelCheckpoint` callback states")
    # pyannote: task-dependent loss in checkpoint, unused at inference
    _f("ignore", message=r"Model has been trained with a task-dependent loss function", category=UserWarning)
    # Lightning: extra checkpoint keys not in inference model (harmless)
    _f("ignore", message=r"Found keys that are not in the model state dict but in the checkpoint")

    # HuggingFace Hub symlink warning on Windows (informational, not actionable)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    # ── logging level suppression ─────────────────────────────────────────────
    # Some Lightning messages go through rank_zero_info() → logging.info()
    # rather than warnings.warn(), so filterwarnings() alone does not catch them.
    for _name in ("lightning", "lightning.pytorch", "pytorch_lightning"):
        logging.getLogger(_name).setLevel(logging.ERROR)

    # torch.utils.flop_counter logs "triton not found" via standard Python logging
    logging.getLogger("torch").setLevel(logging.ERROR)

    # absl "triton not found" flop-counter log (absl has its own logging hierarchy)
    try:
        import absl.logging as _absl
        _absl.set_verbosity(_absl.ERROR)
    except ImportError:
        pass
