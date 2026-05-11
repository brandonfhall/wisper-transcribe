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
