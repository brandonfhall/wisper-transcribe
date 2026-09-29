"""Pinned Tailwind CSS build, shared by the web app, CI, Docker, scripts, and the Claude pre-commit hook.

Run ``python -m wisper_transcribe.tailwind`` to rebuild ``static/tailwind.min.css``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Every build must use this version: different Tailwind releases emit different
# CSS for the same source, which makes the committed file look stale elsewhere.
TAILWIND_VERSION = "v4.3.2"

STATIC_DIR = Path(__file__).parent / "static"
INPUT_CSS = STATIC_DIR / "input.css"
OUTPUT_CSS = STATIC_DIR / "tailwind.min.css"


def build_css(input_css: Path = INPUT_CSS, output_css: Path = OUTPUT_CSS) -> subprocess.CompletedProcess[str]:
    """Build ``output_css`` from ``input_css`` with the pinned Tailwind binary."""
    return subprocess.run(
        [sys.executable, "-m", "pytailwindcss", "-i", str(input_css), "-o", str(output_css), "--minify"],
        env={**os.environ, "TAILWINDCSS_VERSION": TAILWIND_VERSION},
        capture_output=True,
        text=True,
    )


def main() -> int:
    result = build_css()
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
