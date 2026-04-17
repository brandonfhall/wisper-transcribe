import subprocess
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


def test_get_data_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    import importlib
    import wisper_transcribe.config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.get_data_dir() == tmp_path


def test_check_ffmpeg_called_process_error():
    """CalledProcessError (ffmpeg found but exits non-zero) is treated same as missing."""
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
        from wisper_transcribe.config import check_ffmpeg
        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            check_ffmpeg()


def test_get_hf_token_env_var_wins(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_from_env")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    from wisper_transcribe.config import get_hf_token
    assert get_hf_token({"hf_token": "hf_from_config"}) == "hf_from_env"


def test_get_hf_token_hf_token_alias_accepted(monkeypatch):
    """HF_TOKEN (huggingface_hub's canonical name) should be accepted as an alias."""
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", "hf_alias_token")
    from wisper_transcribe.config import get_hf_token
    assert get_hf_token({}) == "hf_alias_token"


def test_get_hf_token_propagates_to_hf_token_env(monkeypatch):
    """When resolved from HUGGINGFACE_TOKEN, HF_TOKEN must be set so third-party libs see it."""
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_propagate")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    import os
    from wisper_transcribe.config import get_hf_token
    get_hf_token({})
    assert os.environ.get("HF_TOKEN") == "hf_propagate"


def test_get_hf_token_propagates_from_config(monkeypatch):
    """When resolved from config, both env vars must be set."""
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    import os
    from wisper_transcribe.config import get_hf_token
    get_hf_token({"hf_token": "hf_from_cfg"})
    assert os.environ.get("HUGGINGFACE_TOKEN") == "hf_from_cfg"
    assert os.environ.get("HF_TOKEN") == "hf_from_cfg"


def test_get_hf_token_config_fallback(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    from wisper_transcribe.config import get_hf_token
    assert get_hf_token({"hf_token": "hf_from_config"}) == "hf_from_config"


def test_get_hf_token_non_tty_raises(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with patch("sys.stdin.isatty", return_value=False):
        from wisper_transcribe.config import get_hf_token
        with pytest.raises(RuntimeError, match="HuggingFace token required"):
            get_hf_token({})


def test_resolve_compute_type_auto_cuda():
    from wisper_transcribe.config import resolve_compute_type
    assert resolve_compute_type("auto", "cuda") == "float16"


def test_resolve_compute_type_auto_cpu():
    from wisper_transcribe.config import resolve_compute_type
    assert resolve_compute_type("auto", "cpu") == "int8"


def test_resolve_compute_type_explicit():
    from wisper_transcribe.config import resolve_compute_type
    assert resolve_compute_type("int8_float16", "cuda") == "int8_float16"
    assert resolve_compute_type("float32", "cpu") == "float32"
