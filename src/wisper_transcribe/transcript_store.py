"""Transcript registry and file lifecycle.

A transcript is ``<output root>/<stem>.md`` plus a row in ``transcripts``
(``wisper.db``). The ``.md`` is the product (edited in Obsidian, synced); the
row is the identity that campaigns, journals, recordings, and jobs link to.
A write that touches both a file and a row can't be atomic, so one ordering
rule applies everywhere:

    the ``.md`` is the existence marker, the row follows it,
    and companion files follow the row.

- Creating: write the ``.md`` (atomically), then :func:`register` the row.
- Deleting (:func:`delete_transcript`, the only delete path): read the
  companion paths, unlink the ``.md``, delete the row (cascades), then unlink
  the companions best-effort.

Every transcript, summary, sidecar, and journal write goes through
:func:`atomic_write_text`, so a crash leaves the old file or the new one,
never a truncated one.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Literal, Optional

from . import db

log = logging.getLogger(__name__)

# Prefix of atomic-write temp files, so reconcile can sweep crash leftovers.
TEMP_PREFIX = ".wisper-tmp-"

# os.replace() onto a file another process holds open without
# FILE_SHARE_DELETE (Obsidian, antivirus, the search indexer) fails on
# Windows with these codes. Retried with backoff, then written in place.
_WINDOWS_SHARING_ERRORS = {5, 32, 33}
_REPLACE_ATTEMPTS = 8
_REPLACE_FIRST_DELAY_S = 0.01
_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def _replace(src: Path, dst: Path) -> bool:
    """``os.replace`` with the Windows sharing-violation retry.

    Returns False when every attempt hit a sharing violation (the caller
    falls back to an in-place write).
    """
    if not _IS_WINDOWS:
        os.replace(src, dst)
        return True
    delay = _REPLACE_FIRST_DELAY_S
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return True
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in _WINDOWS_SHARING_ERRORS:
                raise
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(delay)
                delay *= 2
    return False


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via a temp file in the same dir + ``os.replace``.

    A crash mid-write leaves either the old file or the new one. On Windows,
    if another process keeps the target locked through every retry (~1.3 s),
    the text is written in place instead and a warning is logged.
    """
    path = Path(path)
    tmp = path.with_name(f"{TEMP_PREFIX}{path.name}.{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if not _replace(tmp, path):
            log.warning("%s is locked by another program; writing it in place", path.name)
            with open(path, "w", encoding=encoding) as fh:
                fh.write(text)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def nfc(stem: str) -> str:
    return unicodedata.normalize("NFC", stem)


def safe_path(stem: str, suffix: str, output_dir: Optional[Path] = None) -> Optional[Path]:
    """``<output root>/<stem><suffix>``, or None if ``stem`` could escape it.

    Same two-layer guard as the routes (basename, then abspath/startswith):
    CodeQL recognises only ``os.path`` as a sanitiser.
    """
    if not stem or "\x00" in stem:
        return None
    safe = os.path.basename(stem)
    if safe != stem or safe in {".", ".."}:
        return None
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    base = os.path.abspath(str(output_dir))
    if not base.endswith(os.sep):
        base += os.sep
    target = os.path.abspath(os.path.join(base, f"{safe}{suffix}"))
    if not target.startswith(base):
        return None
    return Path(target)


def _file_timestamp(path: Path) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def ensure_row(conn: sqlite3.Connection, stem: str, output_dir: Optional[Path] = None) -> int:
    """The registry row for ``stem``, created if absent (inside ``conn``'s txn).

    A new row whose ``.md`` doesn't exist is flagged ``missing_since`` (e.g.
    a campaign association made before the transcript was written).
    """
    stem = nfc(stem)
    row = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()
    if row is not None:
        return row[0]
    md = safe_path(stem, ".md", output_dir)
    exists = md is not None and md.is_file()
    now = db.now_utc()
    return conn.execute(
        "INSERT INTO transcripts (stem, created_at, missing_since) VALUES (?, ?, ?) RETURNING id",
        (stem, _file_timestamp(md) if exists else now, None if exists else now),
    ).fetchone()[0]


def register(stem: str, *, origin: Literal["job", "reconcile"],
             data_dir: Optional[Path] = None) -> int:
    """Record that ``<stem>.md`` exists under the output root. Returns the row id.

    Call after the ``.md`` is written. An existing row keeps its id, campaign
    position, and journal entries, so an overwrite or re-transcribe keeps its
    links; a row flagged missing is un-flagged. ``origin`` is ``"job"`` when
    the app just wrote the file (an existing, journaled transcript then marks
    that campaign's journal stale), ``"reconcile"`` when it was found on disk.
    """
    if origin not in ("job", "reconcile"):
        raise ValueError(f"invalid origin: {origin!r}")
    stem = nfc(stem)
    with db.transaction(data_dir) as conn:
        row = conn.execute(
            "SELECT id, missing_since FROM transcripts WHERE stem = ?", (stem,)
        ).fetchone()
        if row is None:
            return ensure_row(conn, stem)
        if row["missing_since"] is not None:
            conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (row["id"],))
            if origin == "job":
                log.info("Reused the name of a previously missing transcript: %s", stem)
        if origin == "job":
            # Overwritten or re-transcribed: a journal that folded the old text
            # now describes something else. Flag it; never un-fold automatically.
            conn.execute(
                "UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, ?) "
                "WHERE id IN (SELECT campaign_id FROM journal_entries WHERE transcript_id = ?)",
                (db.now_utc(), row["id"]),
            )
        return row["id"]


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _companion_paths(stem: str, output_dir: Path) -> list[Path]:
    """Every file that belongs to a transcript except its ``.md``.

    The summary, the enrollment sidecar and the audio copy it references
    (only inside the output root: old sidecars point at temp dirs or user
    files), and excerpt clips. Read before anything is deleted, because the
    audio copy's name can't be derived from the stem (collision suffixes).
    """
    paths: list[Path] = []
    summary = safe_path(stem, ".summary.md", output_dir)
    if summary is not None:
        paths.append(summary)
    sidecar = safe_path(stem, "_diar.json", output_dir)
    if sidecar is not None:
        paths.append(sidecar)
        try:
            stored = json.loads(sidecar.read_text(encoding="utf-8")).get("input_path")
        except (OSError, ValueError, AttributeError):
            stored = None
        if stored:
            base = os.path.abspath(str(output_dir))
            if not base.endswith(os.sep):
                base += os.sep
            candidate = os.path.abspath(stored)
            if candidate.startswith(base) and not candidate.endswith(".md"):
                paths.append(Path(candidate))
    md = safe_path(stem, ".md", output_dir)
    if md is not None:
        # glob.escape: a stem like "mix*" must not match other transcripts' clips.
        paths.extend(output_dir.glob(f"{glob.escape(md.stem)}_excerpt_*"))
    return paths


def delete_transcript(stem: str, data_dir: Optional[Path] = None,
                      output_dir: Optional[Path] = None) -> bool:
    """Delete a transcript, its links, and its companion files.

    The only way a transcript is deleted. Follows the ordering rule: read the
    companion paths, unlink the ``.md``, delete the row in one transaction
    (cascading to campaign and journal rows), then unlink the companions. A
    crash between the last two steps leaves companions with no ``.md`` and
    no row, which reconcile sweeps.

    ``output_dir`` defaults to the output root; routes pass the directory they
    already resolved. Returns False if ``stem`` isn't a safe name; True
    otherwise (deleting a transcript that doesn't exist is not an error).
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    md = safe_path(stem, ".md", output_dir)
    if md is None:
        return False
    stem = nfc(md.stem)

    companions = _companion_paths(stem, output_dir)
    try:
        md.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not delete %s: %s", md.name, exc)
        return True  # the file is still there, so the row stays too

    with db.transaction(data_dir) as conn:
        conn.execute("DELETE FROM transcripts WHERE stem = ?", (stem,))
    # Recordings are still JSON until the SQLite migration's Phase 4.
    from .recording_manager import clear_transcript_link
    clear_transcript_link(stem, data_dir)

    for path in companions:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return True
