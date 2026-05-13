#!/usr/bin/env python3
"""
vendor.py — re-download all vendored web assets for wisper-transcribe.

Run:  python scripts/vendor.py
      python scripts/vendor.py --check   # show versions without downloading
      python scripts/vendor.py --dry-run # same as --check

Assets managed:
  static/htmx.min.js          HTMX (MIT/ISC)
  static/fonts/*.woff2         Geist, Newsreader, JetBrains Mono, Instrument Serif  (SIL OFL)
  static/tailwind.min.css      Rebuilt from input.css (not downloaded, generated locally)

After running, commit the changed files:
  git add src/wisper_transcribe/static/
  git commit -m "chore(vendor): update vendored assets"
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "src" / "wisper_transcribe" / "static"
FONTS = STATIC / "fonts"

# ── Asset registry ─────────────────────────────────────────────────────────────

HTMX_VERSION = "1.9.12"

GOOGLE_FONTS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Geist:wght@300;400;500;600;700"
    "&family=Newsreader:ital,opsz,wght@0,6..72,300;0,6..72,400;0,6..72,500;"
    "1,6..72,400;1,6..72,500"
    "&family=JetBrains+Mono:wght@400;500;600"
    "&family=Instrument+Serif:ital@0;1"
    "&display=swap"
)
GOOGLE_FONTS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ASSETS: list[dict] = [
    {
        "name": f"htmx.min.js ({HTMX_VERSION})",
        "url": f"https://unpkg.com/htmx.org@{HTMX_VERSION}/dist/htmx.min.js",
        "dest": STATIC / "htmx.min.js",
        "min_size": 10_000,
        "license": "ISC",
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch(url: str, dest: Path, *, ua: str | None = None) -> int:
    """Download url → dest. Returns bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": ua} if ua else {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


def _size_str(path: Path) -> str:
    if not path.exists():
        return "missing"
    sz = path.stat().st_size
    return f"{sz / 1024:.1f} KB" if sz >= 1024 else f"{sz} B"


def _fetch_fonts(dry_run: bool) -> None:
    """Download Google Fonts CSS, extract woff2 URLs, download latin+latin-ext subsets."""
    import re

    print("\nFonts (SIL OFL — Geist, Newsreader, JetBrains Mono, Instrument Serif)")
    req = urllib.request.Request(GOOGLE_FONTS_URL, headers={"User-Agent": GOOGLE_FONTS_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        css = resp.read().decode()

    # Parse @font-face blocks — keep latin and latin-ext subsets only
    block_re = re.compile(
        r'/\*\s*(.*?)\s*\*/\s*@font-face\s*\{([^}]+)\}', re.DOTALL
    )
    url_re    = re.compile(r'url\((https://[^)]+\.woff2)\)')
    family_re = re.compile(r"font-family:\s*'([^']+)'")
    weight_re = re.compile(r'font-weight:\s*([\w ]+?)\s*;')
    style_re  = re.compile(r'font-style:\s*(\w+)')
    range_re  = re.compile(r'unicode-range:\s*([^;]+);')

    downloaded = 0
    skipped = 0

    for comment, block in block_re.findall(css):
        subset = comment.strip().replace(' ', '-').lower()
        # Keep only latin and latin-ext
        if subset not in ('latin', 'latin-ext'):
            skipped += 1
            continue

        url_m    = url_re.search(block)
        family_m = family_re.search(block)
        weight_m = weight_re.search(block)
        style_m  = style_re.search(block)
        if not (url_m and family_m):
            continue

        family = family_m.group(1).replace(' ', '-').lower()
        weight = weight_m.group(1).strip().split()[0] if weight_m else '400'
        style  = style_m.group(1) if style_m else 'normal'
        url    = url_m.group(1)
        fname  = f"{family}-{weight}-{style}-{subset}.woff2"
        dest   = FONTS / fname

        current = _size_str(dest)
        if dry_run:
            print(f"  {'✓' if dest.exists() else '?'} {fname:<60} {current}")
        else:
            sz = _fetch(url, dest)
            print(f"  ↓ {fname:<60} {sz/1024:.1f} KB")
            downloaded += 1

    if not dry_run:
        print(f"  {downloaded} font files downloaded ({skipped} non-latin subsets skipped)")


def _rebuild_tailwind() -> None:
    print("\nTailwind CSS")
    input_css  = STATIC / "input.css"
    output_css = STATIC / "tailwind.min.css"
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytailwindcss",
         "-i", str(input_css), "-o", str(output_css), "--minify"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  ✓ tailwind.min.css rebuilt → {_size_str(output_css)}")
    else:
        print(f"  ✗ Tailwind build failed: {result.stderr.strip()}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", "--dry-run", action="store_true",
                        help="Show current state without downloading")
    args = parser.parse_args()

    dry = args.check
    action = "Checking" if dry else "Downloading"
    print(f"wisper-transcribe vendor assets — {action}")
    print("=" * 55)

    # JS assets
    print("\nJavaScript")
    for asset in ASSETS:
        dest = asset["dest"]
        current = _size_str(dest)
        ok = dest.exists() and dest.stat().st_size >= asset["min_size"]
        if dry:
            status = "✓ OK" if ok else "✗ MISSING/STALE"
            print(f"  {status:12}  {asset['name']:<40} {current}")
        else:
            sz = _fetch(asset["url"], dest)
            print(f"  ↓ {asset['name']:<40} {sz/1024:.1f} KB")

    # Fonts
    _fetch_fonts(dry_run=dry)

    # Tailwind
    if not dry:
        _rebuild_tailwind()
    else:
        css = STATIC / "tailwind.min.css"
        print(f"\nTailwind CSS")
        print(f"  {'✓' if css.exists() else '?'} tailwind.min.css  {_size_str(css)}")

    print("\nDone." if not dry else "\nRun without --check to update.")
    if not dry:
        print("\nNext: commit the changed files in static/")
        print("  git add src/wisper_transcribe/static/")
        print('  git commit -m "chore(vendor): update vendored assets"')


if __name__ == "__main__":
    main()
