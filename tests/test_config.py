import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_load_config_returns_defaults(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.config import DEFAULTS, load_config

        cfg = load_config()
        for key, val in DEFAULTS.items():
            assert cfg[key] == val


def test_save_and_load_config(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.config import load_config, save_config

        cfg = load_config()
        cfg["model"] = "large-v3"
        cfg["hf_token"] = "hf_test123"
        save_config(cfg)

        loaded = load_config()
        assert loaded["model"] == "large-v3"
        assert loaded["hf_token"] == "hf_test123"


def test_check_ffmpeg_success():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        from wisper_transcribe.config import check_ffmpeg

        check_ffmpeg()  # should not raise


def test_check_ffmpeg_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        from wisper_transcribe.config import check_ffmpeg

        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            check_ffmpeg()


def test_get_device_cuda_available():
    mock_torch = MagicMock()
    mock_torch.cuda.is_available.return_value = True
    with patch.dict("sys.modules", {"torch": mock_torch}):
        from wisper_transcribe import config
        import importlib
        importlib.reload(config)
        result = config.get_device()
        assert result == "cuda"


def test_get_device_cpu_fallback():
    mock_torch = MagicMock()
    mock_torch.cuda.is_available.return_value = False
    mock_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": mock_torch}):
        from wisper_transcribe import config
        import importlib
        importlib.reload(config)
        result = config.get_device()
        assert result == "cpu"
