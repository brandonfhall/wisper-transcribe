import pytest
from wisper_transcribe.path_utils import validate_path_component

_VALID = [
    ("550e8400-e29b-41d4-a716-446655440000", "550e8400-e29b-41d4-a716-446655440000"),
    ("abc-123", "abc-123"),
    ("job_id_with_underscores", "job_id_with_underscores"),
    ("a1B2c3", "a1B2c3"),
]

@pytest.mark.parametrize("value,expected", _VALID)
def test_valid_components_pass(value: str, expected: str):
    assert validate_path_component(value) == expected


_INVALID = [
    "",                          # empty
    "\x00",                      # null byte
    "some\x00name",              # embedded null byte
    "../../etc/passwd",          # path traversal
    "../relative",               # relative path component
    "id with spaces",            # spaces
    "id/with/slashes",           # path separators
    "id\\backslash",             # backslash
    "evil\r\nLocation: x",       # CRLF injection
    "javascript:alert(1)",       # JS URI
    "\\\\evil.com",              # UNC path
    "name!@#",                   # special chars
    ".",                         # dot
    "..",                        # double-dot
]

@pytest.mark.parametrize("bad", _INVALID)
def test_invalid_components_rejected(bad: str):
    assert validate_path_component(bad) is None


def test_custom_guard_name_does_not_affect_output():
    """guard_name is a dummy dir; the returned value is the same regardless."""
    assert validate_path_component("abc", "_guard_a") == validate_path_component("abc", "_guard_b")


def test_returns_basename_not_full_path():
    result = validate_path_component("simple-id")
    assert result == "simple-id"
    assert "/" not in (result or "")
    assert "\\" not in (result or "")


# ---------------------------------------------------------------------------
# get_output_dir(): WISPER_OUTPUT_DIR → output_dir setting → <data dir>/output
# ---------------------------------------------------------------------------

from pathlib import Path

from wisper_transcribe.config import get_data_dir, load_config, save_config
from wisper_transcribe.path_utils import get_output_dir


def test_output_dir_defaults_to_data_dir():
    assert get_output_dir() == get_data_dir() / "output"
    assert get_output_dir().is_dir()


def test_output_dir_ignores_cwd_output(tmp_path, monkeypatch):
    (tmp_path / "output").mkdir()
    monkeypatch.chdir(tmp_path)
    assert get_output_dir() == get_data_dir() / "output"


def test_output_dir_from_config(tmp_path):
    cfg = load_config()
    cfg["output_dir"] = str(tmp_path / "transcripts")
    save_config(cfg)
    assert get_output_dir() == tmp_path / "transcripts"


def test_relative_output_dir_setting_is_relative_to_data_dir():
    cfg = load_config()
    cfg["output_dir"] = "mine"
    save_config(cfg)
    assert get_output_dir() == get_data_dir() / "mine"


def test_env_overrides_config(tmp_path, monkeypatch):
    cfg = load_config()
    cfg["output_dir"] = str(tmp_path / "from-config")
    save_config(cfg)
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(tmp_path / "from-env"))
    assert get_output_dir() == tmp_path / "from-env"


def test_config_set_output_dir_stores_absolute(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from wisper_transcribe.cli import main

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["config", "set", "output_dir", "rel/out"])
    assert result.exit_code == 0, result.output
    stored = load_config()["output_dir"]
    assert Path(stored).is_absolute()
    assert Path(stored) == (tmp_path / "rel" / "out").resolve()
