"""Tests for wisper_transcribe.debug_log — Logger class and setup_logging()."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import tqdm as _tqdm_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_tqdm_write(original_write):
    """Restore tqdm.write to its original implementation after a test."""
    _tqdm_mod.tqdm.write = original_write


@pytest.fixture(autouse=True)
def _restore_tqdm_write():
    """Ensure tqdm.write is restored to its original after every test."""
    original = _tqdm_mod.tqdm.write
    yield
    _tqdm_mod.tqdm.write = original


@pytest.fixture(autouse=True)
def _reset_logger_singleton():
    """Reset the module-level _logger singleton between tests."""
    import wisper_transcribe.debug_log as dl
    dl._logger = None
    yield
    dl._logger = None


# ---------------------------------------------------------------------------
# Logger — file output (debug=True)
# ---------------------------------------------------------------------------

class TestLoggerFileMode:
    def test_creates_log_file(self, tmp_path):
        from wisper_transcribe.debug_log import Logger

        with patch("wisper_transcribe.debug_log.Path") as mock_path_cls:
            # Redirect CWD/logs to tmp_path/logs
            logs_dir = tmp_path / "logs"
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.return_value = logs_dir
            # Use real Path for everything except cwd()
            import wisper_transcribe.debug_log as dl
            real_path = Path
            mock_path_cls.side_effect = real_path
            mock_path_cls.cwd.return_value = tmp_path

            logger = Logger(debug=True)
            assert logger.log_path is not None
            assert logger.log_path.exists()
            logger.close()

    def test_write_to_file_adds_timestamp(self, tmp_path):
        from wisper_transcribe.debug_log import Logger
        import wisper_transcribe.debug_log as dl

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(debug=True)
            logger._write_to_file("hello world")
            logger.close()

        content = logger.log_path.read_text(encoding="utf-8")
        assert "hello world" in content
        # Timestamp format [HH:MM:SS.mmm]
        assert content.startswith("[")

    def test_write_to_file_skips_blank_lines(self, tmp_path):
        from wisper_transcribe.debug_log import Logger
        import wisper_transcribe.debug_log as dl

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(debug=True)
            logger._write_to_file("   ")  # whitespace-only — should be skipped
            logger._write_to_file("")
            logger._write_to_file("real line")
            logger.close()

        content = logger.log_path.read_text(encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) == 1
        assert "real line" in lines[0]

    def test_tqdm_write_teed_to_file(self, tmp_path):
        from wisper_transcribe.debug_log import Logger
        import wisper_transcribe.debug_log as dl

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(debug=True)
            _tqdm_mod.tqdm.write("tqdm message via patched write")
            logger.close()

        content = logger.log_path.read_text(encoding="utf-8")
        assert "tqdm message via patched write" in content

    def test_python_logging_teed_to_file_via_bridge(self, tmp_path):
        """Python logging records reach the file through _LoggingBridge (single fd)."""
        from wisper_transcribe.debug_log import Logger, _LoggingBridge
        import wisper_transcribe.debug_log as dl

        root = logging.getLogger()
        handlers_before = list(root.handlers)

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(debug=True)
            # Emit a record through the root logger — bridge should capture it.
            logging.getLogger("test.bridge").debug("bridge test record")
            logger.close()

        # Cleanup handlers added by Logger
        for h in list(root.handlers):
            if h not in handlers_before:
                root.removeHandler(h)

        content = logger.log_path.read_text(encoding="utf-8")
        assert "bridge test record" in content

    def test_bridge_handler_not_filehandler(self, tmp_path):
        """_LoggingBridge is NOT a FileHandler — no second fd opened to the log file."""
        from wisper_transcribe.debug_log import Logger, _LoggingBridge
        import wisper_transcribe.debug_log as dl

        root = logging.getLogger()
        handlers_before = list(root.handlers)

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(debug=True)

        new_handlers = [h for h in root.handlers if h not in handlers_before]
        file_handlers = [h for h in new_handlers if isinstance(h, logging.FileHandler)]
        bridges = [h for h in new_handlers if isinstance(h, _LoggingBridge)]

        assert not file_handlers, "No FileHandler should be added — would open a second fd"
        assert bridges, "_LoggingBridge handler should be present"

        logger.close()
        for h in new_handlers:
            root.removeHandler(h)

    def test_no_log_file_when_debug_false(self):
        from wisper_transcribe.debug_log import Logger

        logger = Logger(debug=False)
        assert logger.log_path is None
        assert logger._file is None

    def test_write_to_file_noop_when_no_file(self):
        from wisper_transcribe.debug_log import Logger

        logger = Logger(debug=False)
        # Must not raise
        logger._write_to_file("this should silently do nothing")

    def test_close_is_idempotent(self, tmp_path):
        from wisper_transcribe.debug_log import Logger
        import wisper_transcribe.debug_log as dl

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(debug=True)
            logger.close()
            logger.close()  # second close must not raise


# ---------------------------------------------------------------------------
# Logger — verbose mode (verbose=True)
# ---------------------------------------------------------------------------

class TestLoggerVerboseMode:
    def test_verbose_attaches_stream_handler(self):
        from wisper_transcribe.debug_log import Logger

        root = logging.getLogger()
        handlers_before = list(root.handlers)
        logger = Logger(verbose=True)

        new_handlers = [h for h in root.handlers if h not in handlers_before]
        stream_handlers = [h for h in new_handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.FileHandler)]
        assert stream_handlers, "verbose=True should attach a StreamHandler to root logger"

        # Cleanup
        for h in new_handlers:
            root.removeHandler(h)

    def test_non_verbose_does_not_attach_stream_handler(self):
        from wisper_transcribe.debug_log import Logger

        root = logging.getLogger()
        handlers_before = list(root.handlers)
        Logger(verbose=False, debug=False)

        new_stream = [h for h in root.handlers
                      if h not in handlers_before
                      and isinstance(h, logging.StreamHandler)
                      and not isinstance(h, logging.FileHandler)]
        assert not new_stream


# ---------------------------------------------------------------------------
# Logger — combined mode (verbose=True, debug=True)
# ---------------------------------------------------------------------------

class TestLoggerCombinedMode:
    def test_both_file_and_stream_handlers_attached(self, tmp_path):
        from wisper_transcribe.debug_log import Logger
        import wisper_transcribe.debug_log as dl

        root = logging.getLogger()
        handlers_before = list(root.handlers)

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            logger = Logger(verbose=True, debug=True)

        from wisper_transcribe.debug_log import _LoggingBridge

        new_handlers = [h for h in root.handlers if h not in handlers_before]
        has_bridge = any(isinstance(h, _LoggingBridge) for h in new_handlers)
        has_stream = any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in new_handlers
        )
        assert has_bridge, "debug=True should attach a _LoggingBridge handler"
        assert has_stream, "verbose=True should attach a StreamHandler"

        logger.close()
        for h in new_handlers:
            root.removeHandler(h)


# ---------------------------------------------------------------------------
# setup_logging() — public factory
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def test_returns_none_when_both_false(self):
        from wisper_transcribe.debug_log import setup_logging

        result = setup_logging(verbose=False, debug=False)
        assert result is None

    def test_returns_path_when_debug_true(self, tmp_path):
        from wisper_transcribe.debug_log import setup_logging
        import wisper_transcribe.debug_log as dl

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            result = setup_logging(debug=True)

        assert result is not None
        assert isinstance(result, Path)
        assert result.exists()
        if dl._logger:
            dl._logger.close()

    def test_sets_singleton(self):
        from wisper_transcribe.debug_log import get_logger, setup_logging

        setup_logging(verbose=False, debug=False)
        assert get_logger() is not None

    def test_get_logger_returns_none_before_setup(self):
        from wisper_transcribe.debug_log import get_logger

        # _reset_logger_singleton fixture has already set it to None
        assert get_logger() is None

    def test_sets_wisper_debug_env_when_debug_true(self, tmp_path, monkeypatch):
        from wisper_transcribe.debug_log import setup_logging
        import wisper_transcribe.debug_log as dl

        monkeypatch.delenv("WISPER_DEBUG", raising=False)

        with patch.object(dl, "Path") as mock_path_cls:
            real_path = Path
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.side_effect = real_path

            setup_logging(debug=True)

        import os
        assert os.environ.get("WISPER_DEBUG") == "1"
        if dl._logger:
            dl._logger.close()

    def test_does_not_set_wisper_debug_when_verbose_only(self, monkeypatch):
        from wisper_transcribe.debug_log import setup_logging
        import os

        monkeypatch.delenv("WISPER_DEBUG", raising=False)
        setup_logging(verbose=True, debug=False)
        assert os.environ.get("WISPER_DEBUG") is None
