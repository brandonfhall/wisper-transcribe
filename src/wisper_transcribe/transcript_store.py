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


class TranscriptExistsError(FileExistsError):
    """A transcript with this name already exists and overwrite wasn't chosen."""

    def __init__(self, stem: str) -> None:
        super().__init__(f"A transcript named {stem!r} already exists")
        self.stem = stem


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


# ---------------------------------------------------------------------------
# Reconcile: the registry vs. what's on disk
# ---------------------------------------------------------------------------

# Leftover atomic-write temp files older than this are crash debris.
_TEMP_MAX_AGE_S = 600
_case_insensitive: dict[str, bool] = {}


def _is_case_insensitive(directory: Path) -> bool:
    """Probe (once per directory) whether names differing only in case collide."""
    key = os.path.realpath(directory)
    if key not in _case_insensitive:
        import tempfile

        fd, probe = tempfile.mkstemp(prefix=f"{TEMP_PREFIX}case-probe-", dir=directory)
        os.close(fd)
        try:
            head, tail = os.path.split(probe)
            _case_insensitive[key] = os.path.exists(os.path.join(head, tail.upper()))
        finally:
            os.unlink(probe)
    return _case_insensitive[key]


def _transcript_files(output_dir: Path) -> dict[str, Path]:
    """NFC stem → path for every transcript ``.md`` in the output root."""
    found: dict[str, Path] = {}
    for md in output_dir.glob("*.md"):
        if md.name.endswith(".summary.md") or md.name.startswith(TEMP_PREFIX):
            continue
        found[nfc(md.stem)] = md
    return found


def _companion_stem(name: str) -> Optional[str]:
    """The transcript stem a pattern-identifiable companion file belongs to."""
    if name.endswith(".summary.md"):
        return name[: -len(".summary.md")]
    if name.endswith("_diar.json"):
        return name[: -len("_diar.json")]
    if "_excerpt_" in name and name.endswith((".mp3", ".txt")):
        return name.rsplit("_excerpt_", 1)[0]
    return None


def reconcile(output_dir: Optional[Path] = None, data_dir: Optional[Path] = None,
              *, sweep: bool = False) -> dict[str, int]:
    """Bring the registry in line with the ``.md`` files on disk.

    - An unregistered ``.md`` gets a row.
    - A row whose ``.md`` is gone is flagged ``missing_since`` but kept, with
      its campaign position and companions: the file may come back (a sync,
      an unmounted drive). A reappearing file clears the flag.
    - On a case-insensitive filesystem, a ``.md`` whose name differs from a
      missing row's only in case renames that row, keeping its links.

    Rows are never deleted here. With ``sweep`` (server startup), also delete
    stale atomic-write temp files and pattern-identifiable companions
    (summary, sidecar, excerpt clips) that have neither a ``.md`` nor a row.
    Only cheap work: no file is parsed. Returns counts for logging.
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    files = _transcript_files(output_dir)
    fold = _is_case_insensitive(output_dir)
    counts = {"added": 0, "missing": 0, "restored": 0, "renamed": 0, "swept": 0}
    now = db.now_utc()

    with db.transaction(data_dir) as conn:
        rows = {r["stem"]: r for r in conn.execute("SELECT id, stem, missing_since FROM transcripts")}
        missing_by_fold = {
            stem.casefold(): r for stem, r in rows.items() if stem not in files
        } if fold else {}
        for stem, md in files.items():
            row = rows.get(stem)
            if row is None:
                twin = missing_by_fold.pop(stem.casefold(), None)
                if twin is not None:
                    conn.execute("UPDATE transcripts SET stem = ?, missing_since = NULL WHERE id = ?",
                                 (stem, twin["id"]))
                    rows.pop(twin["stem"], None)
                    counts["renamed"] += 1
                else:
                    ensure_row(conn, stem, output_dir)
                    counts["added"] += 1
            elif row["missing_since"] is not None:
                conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (row["id"],))
                counts["restored"] += 1
        for stem, row in rows.items():
            if stem not in files and row["missing_since"] is None:
                conn.execute("UPDATE transcripts SET missing_since = ? WHERE id = ?", (now, row["id"]))
                counts["missing"] += 1
        registered = {r[0] for r in conn.execute("SELECT stem FROM transcripts")}

    if sweep:
        cutoff = time.time() - _TEMP_MAX_AGE_S
        for entry in output_dir.iterdir():
            try:
                if entry.name.startswith(TEMP_PREFIX):
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        counts["swept"] += 1
                    continue
                owner = _companion_stem(entry.name)
                if owner is None or not entry.is_file():
                    continue
                owner = nfc(owner)
                if owner not in files and owner not in registered:
                    entry.unlink()
                    counts["swept"] += 1
            except OSError:
                pass
    if any(counts.values()):
        log.info("Transcript reconcile: %s", counts)
    return counts


def relink(old_stem: str, new_stem: str, data_dir: Optional[Path] = None,
           output_dir: Optional[Path] = None) -> None:
    """Give a missing transcript's identity to a file under a new name.

    ``old_stem`` must be flagged missing; ``<new_stem>.md`` must exist and
    must not be linked to anything yet (no campaign, no journal entry). The
    old row takes the new name, so its campaign position, journal entry, and
    speakers are kept; the new name's own row (reconcile may have created one)
    is removed.

    Raises ValueError (unsafe name, not missing, or target already linked)
    or KeyError (no such transcript).
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    old_md = safe_path(old_stem, ".md", output_dir)
    new_md = safe_path(new_stem, ".md", output_dir)
    if old_md is None or new_md is None:
        raise ValueError("invalid transcript name")
    old_stem, new_stem = nfc(old_md.stem), nfc(new_md.stem)
    if not new_md.is_file():
        raise KeyError(f"No transcript file named {new_stem!r}")
    with db.transaction(data_dir) as conn:
        old = conn.execute(
            "SELECT id, missing_since FROM transcripts WHERE stem = ?", (old_stem,)
        ).fetchone()
        if old is None:
            raise KeyError(f"No transcript named {old_stem!r}")
        if old["missing_since"] is None:
            raise ValueError(f"{old_stem!r} is not missing")
        new = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (new_stem,)).fetchone()
        if new is not None:
            linked = conn.execute(
                "SELECT 1 FROM campaign_transcripts WHERE transcript_id = ? "
                "UNION SELECT 1 FROM journal_entries WHERE transcript_id = ?",
                (new["id"], new["id"]),
            ).fetchone()
            if linked:
                raise ValueError(f"{new_stem!r} already belongs to a campaign")
            conn.execute("DELETE FROM transcripts WHERE id = ?", (new["id"],))
        conn.execute("UPDATE transcripts SET stem = ?, missing_since = NULL WHERE id = ?",
                     (new_stem, old["id"]))


def relink_candidates(data_dir: Optional[Path] = None) -> list[str]:
    """Present transcripts not linked to any campaign, newest first — the
    files a missing campaign entry can be relinked to."""
    with db.connection(data_dir) as conn:
        return [r[0] for r in conn.execute(
            "SELECT t.stem FROM transcripts t WHERE t.missing_since IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM campaign_transcripts ct WHERE ct.transcript_id = t.id) "
            "ORDER BY t.created_at DESC, t.stem"
        )]
