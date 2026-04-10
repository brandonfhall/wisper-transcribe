"""Centralized logging for wisper-transcribe.

Two independent modes, both off by default:

  verbose=True  — attach a console StreamHandler at DEBUG level so ML library
                  output (pyannote, faster-whisper, Lightning …) is visible on
                  the terminal alongside normal tqdm.write messages.

  debug=True    — tee every tqdm.write() call to a timestamped log file under
                  ./logs/ and attach a _LoggingBridge handler to the root
                  Python logger so ML library output is also captured there.

Both flags may be combined.

Usage (from CLI entry points):

    from .debug_log import setup_logging
    log_path = setup_logging(debug=True, verbose=False)
    if log_path:
        click.echo(f"  Debug log: {log_path}")
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import IO, Optional


# Module-level singleton — created once by setup_logging() at CLI startup.
_logger: Optional["Logger"] = None


class _LoggingBridge(logging.Handler):
    """Routes Python logging records through Logger._write_to_file().

    Using a separate logging.FileHandler would open a second file descriptor
    to the same log file, causing interleaved / corrupt output when both fds
    write concurrently (e.g. a long pydub debug line split around a tqdm.write
    call).  This handler funnels everything through the single fd owned by
    Logger, eliminating the race.
    """

    def __init__(self, logger_instance: "Logger") -> None:
        super().__init__(level=logging.DEBUG)
        self._logger_inst = logger_instance
        self.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._logger_inst._write_to_file(self.format(record))
        except Exception:
            self.handleError(record)


class Logger:
    """Manages file and console log output for a single wisper run."""

    def __init__(self, *, verbose: bool = False, debug: bool = False) -> None:
        self.verbose = verbose
        self.debug = debug
        self.log_path: Optional[Path] = None
        self._file: Optional[IO[str]] = None

        if debug:
            # Disable warning suppression so everything is visible.
            os.environ["WISPER_DEBUG"] = "1"
            self._open_log_file()

        self._patch_tqdm()
        self._configure_python_logging()

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _open_log_file(self) -> None:
        logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = logs_dir / f"wisper_{ts}.log"
        self._file = open(self.log_path, "w", encoding="utf-8", buffering=1)

    def _patch_tqdm(self) -> None:
        """Tee tqdm.write() through this logger so file output is captured."""
        try:
            import tqdm as _tqdm_mod
        except ImportError:
            return

        _orig = _tqdm_mod.tqdm.write
        _self = self

        def _tee(msg: str, *a, **kw) -> None:
            _orig(msg, *a, **kw)   # always write to the normal tqdm destination
            _self._write_to_file(msg)

        _tqdm_mod.tqdm.write = _tee  # type: ignore[method-assign]

    def _configure_python_logging(self) -> None:
        """Wire up Python logging handlers for verbose and/or debug modes."""
        root = logging.getLogger()

        if self.verbose:
            # Surface ML library log output on the console.
            sh = logging.StreamHandler()
            sh.setLevel(logging.DEBUG)
            sh.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
            root.addHandler(sh)
            if root.level > logging.DEBUG:
                root.setLevel(logging.DEBUG)

        if self._file is not None:
            # Use _LoggingBridge (not FileHandler) so all file output goes
            # through a single fd — prevents interleaved writes.
            bridge = _LoggingBridge(self)
            root.addHandler(bridge)
            if root.level > logging.DEBUG:
                root.setLevel(logging.DEBUG)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _write_to_file(self, msg: str) -> None:
        """Append a timestamped message to the log file (no-op if not open)."""
        if self._file is None:
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        for line in msg.splitlines():
            if line.strip():
                self._file.write(f"[{ts}] {line}\n")

    def close(self) -> None:
        """Flush and close the log file."""
        if self._file:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def setup_logging(*, verbose: bool = False, debug: bool = False) -> Optional[Path]:
    """Create the global Logger and return the log file path (or None).

    Call once at CLI startup after parsing --verbose / --debug flags.
    Subsequent tqdm.write() calls and Python logging output are automatically
    routed according to the active modes.
    """
    global _logger
    _logger = Logger(verbose=verbose, debug=debug)
    return _logger.log_path


def get_logger() -> Optional[Logger]:
    """Return the active Logger, or None if setup_logging() was not called."""
    return _logger
