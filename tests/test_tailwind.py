"""Tests for the pinned Tailwind build helper."""
from pathlib import Path
from unittest.mock import patch

from wisper_transcribe import tailwind

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_an_exact_release_tag():
    assert tailwind.TAILWIND_VERSION.startswith("v")
    assert tailwind.TAILWIND_VERSION != "latest"


def test_build_css_pins_version_and_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TAILWINDCSS_VERSION", "latest")
    src, out = tmp_path / "in.css", tmp_path / "out.css"
    with patch("wisper_transcribe.tailwind.subprocess.run") as run:
        tailwind.build_css(src, out)
    args, kwargs = run.call_args
    cmd = args[0]
    assert cmd[1:3] == ["-m", "pytailwindcss"]
    assert cmd[cmd.index("-i") + 1] == str(src)
    assert cmd[cmd.index("-o") + 1] == str(out)
    assert kwargs["env"]["TAILWINDCSS_VERSION"] == tailwind.TAILWIND_VERSION


def test_main_returns_build_exit_code(capsys):
    with patch.object(tailwind, "build_css") as build:
        build.return_value.returncode = 1
        build.return_value.stderr = "boom"
        assert tailwind.main() == 1
    assert "boom" in capsys.readouterr().err


def test_no_build_bypasses_the_pinned_module():
    """Direct pytailwindcss calls would use an unpinned binary and produce different CSS."""
    checked = [".github/workflows/ci.yml", "Dockerfile", "Makefile", "scripts/vendor.py",
               "src/wisper_transcribe/web/app.py", ".claude/hooks/pre_commit.py"]
    for rel in checked:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "-m pytailwindcss" not in text and '"pytailwindcss"' not in text, rel
        assert "TAILWINDCSS_VERSION" not in text, rel
