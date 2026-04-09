"""Optional file-based debug logging.

Activated by --debug on the CLI or server command.
Creates ./logs/wisper_YYYYMMDD_HHmmss.log in the current working directory.
All tqdm.write() output (which captures the full pipeline log) is tee'd to the
file alongside Python logging output at DEBUG level.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

_debug_fh: Optional["_DebugFile"] = None


class _DebugFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._f = open(path, "w", encoding="utf-8", buffering=1)

    def write(self, s: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        for line in s.splitlines():
            if line.strip():
                self._f.write(f"[{ts}] {line}\n")

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def setup_debug_logging() -> Path:
    """Enable file-based debug logging.

    - Sets WISPER_DEBUG=1 so warning suppression is disabled.
    - Creates ./logs/wisper_<timestamp>.log in the calling process's CWD.
    - Patches tqdm.write to tee output to the file.
    - Attaches a FileHandler to the root Python logger at DEBUG level.

    Returns the path to the log file.
    """
    global _debug_fh

    # Disable warning suppression so everything is visible in the log
    os.environ["WISPER_DEBUG"] = "1"

    logs_dir = Path.cwd() / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"wisper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    _debug_fh = _DebugFile(log_path)

    # Tee tqdm.write() to the debug file (captures pipeline status messages and
    # forwarded subprocess progress via the drain-thread write calls)
    try:
        import tqdm as _tqdm
        _orig_write = _tqdm.tqdm.write

        def _tee_write(msg: str, *a, **kw) -> None:
            _orig_write(msg, *a, **kw)
            if _debug_fh:
                _debug_fh.write(msg)

        _tqdm.tqdm.write = _tee_write  # type: ignore[method-assign]
    except ImportError:
        pass

    # Attach Python logging FileHandler (captures ML library log output)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(fh)
    if root.level == logging.WARNING:
        root.setLevel(logging.DEBUG)

    return log_path
