"""Packaging guards: runtime imports must be core dependencies, not dev extras."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirement_names(reqs: list[str]) -> set[str]:
    return {re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0].lower() for r in reqs}


def test_httpx_is_a_core_dependency():
    """src/ imports httpx at runtime (LLM clients, config and record routes).
    Listed only under the dev extra, a plain install failed every Ollama call
    with "httpx not installed"."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert any("import httpx" in p.read_text(encoding="utf-8")
               for p in (ROOT / "src").rglob("*.py"))
    assert "httpx" in _requirement_names(project["dependencies"])
