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
  companion paths (including the ``files`` rows), unlink the ``.md``, delete
  the row (cascades), then unlink the companions best-effort.

Every transcript, summary, sidecar, and journal write goes through
:func:`atomic_write_text`, so a crash leaves the old file or the new one,
never a truncated one. Transcript and summary rewrites use
:func:`save_transcript`/:func:`save_summary`, which also update the search
index (a test enforces it).
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Optional

from . import db, file_registry
from .config import get_output_root
from .search_index import check_freshness, mark_stale, request_backfill

log = logging.getLogger(__name__)

# ``find_by_stem(campaign_id=...)`` sentinel: any campaign, including the root.
ANY: Final = object()

# Prefix of atomic-write temp files, so reconcile can sweep crash leftovers.
TEMP_PREFIX = ".wisper-tmp-"
# Diarization segments for the enrollment wizard; speakers live in the DB.
SIDECAR_SUFFIX = "_diar.json"

# One writer at a time in this process: reconcile's scan and write, a
# transcript move or rename, and finishing a folder rename all take it before
# their transaction. A page-load reconcile takes it without blocking and skips
# when it's held. A second process isn't covered (see architecture.md).
# campaign_folders owns it: it imports nothing that imports this module.
from .campaign_folders import _LOCATION_LOCK  # noqa: E402

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


def save_transcript(md_path: Path, text: str) -> None:
    """Rewrite an existing transcript ``.md`` and reindex it for search.

    For edits (renames, refine). A new transcript is written by the pipeline
    and indexed by :func:`register`. Files outside the output root are
    written but not indexed.
    """
    from .search_index import reindex_path
    atomic_write_text(md_path, text)
    _refresh_transcript_row(Path(md_path))
    reindex_path(md_path)


def save_summary(summary_path: Path, text: str) -> None:
    """Write a ``<stem>.summary.md`` and reindex its transcript for search."""
    from .search_index import SUMMARY_SUFFIX, reindex_path
    atomic_write_text(summary_path, text)
    if Path(summary_path).name.endswith(SUMMARY_SUFFIX):  # not a custom --output name
        _register_summary(Path(summary_path), SUMMARY_SUFFIX)
        reindex_path(summary_path)


def _refresh_transcript_row(md_path: Path) -> None:
    """Re-stat the ``transcript`` file row after a rewrite; best-effort."""
    try:
        loc = locate_path(Path(md_path))
        if loc is None:
            return
        row = file_registry.file_for(file_registry.Owner("transcript", loc.id), "transcript")
        if row is not None:
            file_registry.refresh(row)
    except Exception:
        log.debug("Could not refresh the file record for %s", Path(md_path).name, exc_info=True)


def _register_summary(summary_path: Path, suffix: str) -> None:
    """Register ``<stem>.summary.md`` for a registered transcript; best-effort.

    A file outside the output root (a CLI ``--output``) isn't tracked.
    """
    try:
        summary_path = Path(summary_path)
        md = summary_path.with_name(f"{summary_path.name[: -len(suffix)]}.md")
        loc = locate_path(md, output_dir=get_output_root())
        owner = file_registry.Owner("transcript", loc.id) if loc else None
        file_registry.add_if_owned(summary_path, kind="summary", owner=owner,
                                   output_dir=get_output_root())
    except Exception:
        log.debug("Could not register %s", Path(summary_path).name, exc_info=True)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def nfc(stem: str) -> str:
    return unicodedata.normalize("NFC", stem)


def existing_form(target: str) -> str:
    """``target``, or its NFD spelling when only that exists on disk.

    Stems are stored NFC. On a normalization-sensitive filesystem (ext4, e.g.
    Docker) a file synced from a Mac can carry the NFD bytes, so the NFC path
    misses it. New files are still written NFC.
    """
    if target.isascii() or os.path.exists(target):
        return target
    head, tail = os.path.split(target)
    alt = os.path.join(head, unicodedata.normalize("NFD", tail))
    return alt if alt != target and os.path.exists(alt) else target


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
        output_dir = get_output_root()
    base = os.path.abspath(str(output_dir))
    if not base.endswith(os.sep):
        base += os.sep
    target = os.path.abspath(os.path.join(base, f"{safe}{suffix}"))
    if not target.startswith(base):
        return None
    # Only a path already inside the base is probed on disk; re-checked after.
    target = existing_form(target)
    if not target.startswith(base):
        return None
    return Path(target)


def _file_timestamp(path: Path) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _modified_local(path: Optional[Path]) -> Optional[str]:
    """A file's last-modified time as ``%Y-%m-%d %H:%M`` local, or None."""
    if path is None:
        return None
    from datetime import datetime
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return None


def validate_new_stem(name: str) -> Optional[str]:
    """``name`` as a new session stem, or None when it is refused.

    NFC-normalized and stripped. Non-empty and at most 100 characters; no
    path separators (``/ \\ : * ? " < > |``) or control characters or DEL; no
    leading dot (which also refuses an atomic-write temp name); no trailing dot
    or space; not ending in ``.summary`` or ``.md`` (case-insensitive); and not
    a Windows device name.
    """
    from .campaign_folders import is_reserved

    if not name:
        return None
    name = nfc(name).strip()
    if not name or len(name) > 100:
        return None
    if any(c in '/\\:*?"<>|' or ord(c) < 32 or ord(c) == 127 for c in name):
        return None
    if name.startswith(".") or name.endswith((".", " ")):
        return None
    low = name.casefold()
    if low.endswith((".summary", ".md")):
        return None
    if is_reserved(name):
        return None
    return name


# ---------------------------------------------------------------------------
# Locations: where a transcript's files are, and where they should be
# ---------------------------------------------------------------------------

def _resolved_dir(path: Path) -> Path:
    """A directory identity for comparison: ``realpath``, so symlinked roots match."""
    return Path(os.path.realpath(path))


def _dirs_equal(a: Path, b: Path, fold: bool) -> bool:
    """Whether two directories name the same place, comparing NFC and case."""
    ar, br = _resolved_dir(a), _resolved_dir(b)
    try:
        if os.path.samefile(ar, br):
            return True
    except OSError:
        pass
    return file_registry._key(str(ar), fold) == file_registry._key(str(br), fold)


@dataclass(frozen=True)
class Located:
    """A transcript's identity and where each of its files is.

    Where a transcript *is* comes from its ``transcript`` row and the other
    ``files`` rows; where it *should be* comes from ``transcripts.campaign_id``
    (``expected_dir``). ``locate`` fills the derived facts (``target_blocked``,
    ``fold``) so the properties on this dataclass stay pure.
    """

    id: int
    stem: str                    # transcripts.stem (NFC)
    campaign_id: Optional[int]
    expected_dir: Path           # output root, or output root / campaigns.folder
    md: Path                     # its `transcript` files row; else expected_dir / f"{stem}.md"
    missing: bool                # missing_since IS NOT NULL
    companions: dict             # (kind, label or "") -> Path, from its files rows
    target_blocked: bool         # campaign folder is taken, pending, or claimed-and-missing
    fold: bool                   # the output root's file_registry._fold

    @property
    def dir(self) -> Path:
        """The directory the ``.md`` is in."""
        return self.md.parent

    @property
    def misplaced(self) -> bool:
        """Whether the files are not where the campaign assignment says.

        False when the target folder can't be written to right now (taken,
        pending rename, or claimed-and-missing): those have their own Needs
        attention entries. Otherwise true when the ``.md`` is elsewhere, or a
        registered companion stayed behind in a different directory.
        """
        if self.target_blocked:
            return False
        if not _dirs_equal(self.dir, self.expected_dir, self.fold):
            return True
        for path in self.companions.values():
            if not _dirs_equal(path.parent, self.dir, self.fold):
                return True
        return False

    def companion(self, suffix: str) -> Path:
        """The path of a companion with ``suffix`` (``.summary.md``, ``_diar.json``,
        ``.flac``, ``.md.bak``), preferring its registered ``files`` row.

        Registered rows win, so a partial rename or move still finds its files.
        Falls back to ``<stem><suffix>`` in :attr:`dir`; raises ``ValueError``
        when that name could escape the directory.
        """
        kind = {
            ".summary.md": "summary",
            SIDECAR_SUFFIX: "sidecar",
            ".flac": "audio",
            ".md.bak": "backup",
        }.get(suffix)
        if kind is not None:
            row = self.companions.get((kind, ""))
            if row is not None:
                return row
        path = safe_path(self.stem, suffix, self.dir)
        if path is None:
            raise ValueError(f"invalid companion name: {self.stem!r}{suffix}")
        return path


def expected_dir(campaign_id: Optional[int], conn: Optional[sqlite3.Connection] = None, *,
                 output_dir: Optional[Path] = None) -> Path:
    """Where a transcript assigned to ``campaign_id`` belongs. Never creates it."""
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if campaign_id is None:
        return output
    if conn is not None:
        row = conn.execute(
            "SELECT folder FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
    else:
        with db.connection() as c:
            row = c.execute(
                "SELECT folder FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
    if row is None:
        return output
    return output / row[0]


def _campaign_folder(conn: sqlite3.Connection, campaign_id: Optional[int]) -> Optional[tuple]:
    """``(folder, folder_pending, folder_claimed)`` for a campaign, else None."""
    if campaign_id is None:
        return None
    row = conn.execute(
        "SELECT folder, folder_pending, folder_claimed FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()
    return None if row is None else (row[0], row[1], bool(row[2]))


def dir_campaign(directory: Path, conn: Optional[sqlite3.Connection] = None, *,
                 data_dir: Optional[Path] = None,
                 output_dir: Optional[Path] = None) -> tuple[bool, Optional[int]]:
    """The campaign a directory belongs to: ``(True, id)`` or ``(False, None)``.

    ``(True, None)`` is the output root. A claimed campaign folder is ``(True,
    id)``. A folder rename's ``folder_pending`` is accepted only while the old
    folder is gone or the two are the same directory, so a stale pending name
    doesn't capture the wrong directory.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    fold = file_registry._fold(output)
    if _dirs_equal(Path(directory), output, fold):
        return True, None
    if conn is not None:
        return _dir_campaign_in(conn, Path(directory), output, fold)
    with db.connection(data_dir) as c:
        return _dir_campaign_in(c, Path(directory), output, fold)


def _dir_campaign_in(conn: sqlite3.Connection, directory: Path,
                 output: Path, fold: bool) -> tuple[bool, Optional[int]]:
    """Whether ``directory`` is the output root or a claimed campaign folder."""
    if _dirs_equal(directory, output, fold):
        return True, None
    rows = conn.execute(
        "SELECT id, folder, folder_pending, folder_claimed FROM campaigns"
    ).fetchall()
    for row in rows:
        cid, folder, pending, claimed = row[0], row[1], row[2], bool(row[3])
        if not claimed:
            continue
        if _dirs_equal(directory, output / folder, fold):
            return True, cid
        if pending is not None and _dirs_equal(directory, output / pending, fold):
            old = output / folder
            if not old.is_dir() or _dirs_equal(old, output / pending, fold):
                return True, cid
    return False, None


def locate(transcript_id: int, conn: Optional[sqlite3.Connection] = None, *,
           data_dir: Optional[Path] = None,
           output_dir: Optional[Path] = None) -> Optional[Located]:
    """The :class:`Located` for transcript ``transcript_id``, or None.

    Reads the ``transcripts`` row with its campaign, then the transcript's
    ``files`` rows.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if conn is not None:
        return _locate_in(conn, transcript_id, output)
    with db.connection(data_dir) as c:
        return _locate_in(c, transcript_id, output)


def _locate_in(conn: sqlite3.Connection, transcript_id: int, output: Path) -> Optional[Located]:
    row = conn.execute(
        "SELECT t.id, t.stem, t.campaign_id, t.missing_since, c.folder "
        "FROM transcripts t LEFT JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.id = ?",
        (transcript_id,),
    ).fetchone()
    if row is None:
        return None
    tid, stem, campaign_id, missing_since = row[0], row[1], row[2], row[3]
    folder = row[4]
    expected = output / folder if campaign_id is not None and folder is not None else output
    files = conn.execute(
        "SELECT kind, label, root, rel_path FROM files WHERE transcript_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    md = None
    companions: dict = {}
    for kind, label, root, rel_path in files:
        path = db.from_rel(rel_path, output)
        if kind == "transcript":
            if md is None:
                md = path
        else:
            companions.setdefault((kind, label or ""), path)
    if md is None:
        md = expected / f"{stem}.md"
    fold = file_registry._fold(output)
    return Located(tid, stem, campaign_id, expected, md, missing_since is not None,
                   companions, _target_blocked(conn, campaign_id, output), fold)


def _target_blocked(conn: sqlite3.Connection, campaign_id: Optional[int], output: Path) -> bool:
    """Whether the campaign's target folder can't be written to right now.

    True for an unclaimed folder holding files wisper doesn't own (taken), a
    folder rename in progress (pending), or a claimed folder gone from disk
    (missing). A plain unassigned transcript (campaign_id None) is never blocked.
    """
    from .campaign_folders import holds_only_wisper

    info = _campaign_folder(conn, campaign_id)
    if info is None:
        return False
    folder, pending, claimed = info
    if pending is not None:
        return True
    path = output / folder
    if claimed or not path.is_dir():
        return claimed and not path.is_dir()
    return not holds_only_wisper(path, folder)


def locate_path(md_path: Path, conn: Optional[sqlite3.Connection] = None, *,
                data_dir: Optional[Path] = None,
                output_dir: Optional[Path] = None) -> Optional[Located]:
    """The :class:`Located` for the transcript ``.md`` at ``md_path``, or None.

    1. the ``transcript`` ``files`` row whose path matches (NFC, and casefolded
       when the filesystem ignores case);
    2. else, when ``md_path.parent`` is a transcript folder, the row with that
       campaign (``None`` for the root) and stem, but only if that row has no
       ``transcript`` files row of its own (a row already registered at another
       file isn't this file's session: this is a newcomer);
    3. else None.
    """
    md_path = Path(md_path)
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if conn is not None:
        found = _locate_path_in(conn, md_path, output)
    else:
        with db.connection(data_dir) as c:
            found = _locate_path_in(c, md_path, output)
    if found is not None:
        loc = locate(found, conn=conn, data_dir=data_dir, output_dir=output)
        if loc is not None:
            return loc
    return None


def _locate_path_in(conn: sqlite3.Connection, md_path: Path, output: Path) -> Optional[int]:
    fold = file_registry._fold(output)
    try:
        rel = nfc(db.to_rel(md_path, output))
    except ValueError:
        return None  # outside the output root
    hit = file_registry._find_by_path(conn, "output", rel, fold)
    if hit is not None:
        return hit["transcript_id"] if hit["kind"] == "transcript" else None

    has_dir, campaign_id = _dir_campaign_in(conn, md_path.parent, output, fold)
    if not has_dir:
        return None
    stem = _path_stem(md_path)
    row = _stem_row(conn, campaign_id, stem)
    if row is None:
        return None
    tid = row[0]
    registered = conn.execute(
        "SELECT 1 FROM files WHERE transcript_id = ? AND kind = 'transcript' LIMIT 1", (tid,)
    ).fetchone()
    if registered is not None:
        return None
    return tid


def _stem_row(conn: sqlite3.Connection, campaign_id: Optional[int], stem: str):
    """The row named ``stem`` in a campaign, or in the root for ``None``.

    Two spellings, so each uses its partial unique index (``campaign_id IS ?``
    can use neither).
    """
    if campaign_id is None:
        return conn.execute(
            "SELECT id FROM transcripts WHERE campaign_id IS NULL AND stem = ?", (stem,)
        ).fetchone()
    return conn.execute(
        "SELECT id FROM transcripts WHERE campaign_id = ? AND stem = ?", (campaign_id, stem)
    ).fetchone()


def _path_stem(md_path: Path) -> str:
    return nfc(md_path.stem)


def find_by_stem(stem: str, conn: Optional[sqlite3.Connection] = None, *,
                 campaign_id=ANY, present_only: bool = False,
                 data_dir: Optional[Path] = None,
                 output_dir: Optional[Path] = None) -> list[Located]:
    """Every transcript named ``stem`` (NFC, then casefold where the filesystem
    ignores case), optionally narrowed to one campaign.

    ``campaign_id`` defaults to :data:`ANY`; ``None`` means the root only.
    ``present_only`` drops rows flagged missing (no disk check).
    """
    stem = nfc(stem)
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if conn is not None:
        ids = _find_ids(conn, stem, campaign_id, present_only, output)
    else:
        with db.connection(data_dir) as c:
            ids = _find_ids(c, stem, campaign_id, present_only, output)
    found = []
    for tid in ids:
        loc = locate(tid, conn=conn, data_dir=data_dir, output_dir=output)
        if loc is not None:
            found.append(loc)
    return found


def _find_ids(conn: sqlite3.Connection, stem: str, campaign_id, present_only: bool,
              output: Path) -> list[int]:
    fold = file_registry._fold(output)
    if campaign_id is ANY:
        ids = [r[0] for r in conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,))]
    else:
        row = _stem_row(conn, campaign_id, stem)
        ids = [] if row is None else [row[0]]
    if fold:
        # Other case spellings of the name; the exact match above used an index.
        want = file_registry._key(stem, fold)
        if campaign_id is ANY:
            rows = conn.execute("SELECT id, stem FROM transcripts")
        elif campaign_id is None:
            rows = conn.execute("SELECT id, stem FROM transcripts WHERE campaign_id IS NULL")
        else:
            rows = conn.execute(
                "SELECT id, stem FROM transcripts WHERE campaign_id = ?", (campaign_id,)
            )
        ids += [r[0] for r in rows if r[0] not in ids and file_registry._key(r[1], fold) == want]
    if present_only and ids:
        placeholders = ",".join("?" * len(ids))
        present = {r[0] for r in conn.execute(
            f"SELECT id FROM transcripts WHERE id IN ({placeholders}) AND missing_since IS NULL",
            ids,
        )}
        ids = [i for i in ids if i in present]
    return sorted(ids)


def next_free_stem(directory: Path, stem: str, campaign_id: Optional[int],
                   conn: Optional[sqlite3.Connection] = None) -> str:
    """A free session name in ``directory`` for campaign ``campaign_id``.

    ``stem``, else ``stem (2)``, ``stem (3)``, …: the first with no
    ``<name>.md`` or ``<name>.flac`` in ``directory``, no ``(campaign_id, name)``
    row (casefolded where the filesystem ignores case), and not the folder's
    journal name. ``campaign_id`` ``None`` means the output root.
    """
    from .campaign_folders import journal_name

    directory = Path(directory)
    fold = file_registry._fold(directory)
    journal = journal_name(directory.name) if campaign_id is not None else None

    def taken(name: str) -> bool:
        for suffix in (".md", ".flac"):
            path = safe_path(name, suffix, directory)
            if path is not None and path.is_file():
                return True
        if journal is not None and file_registry._key(name, fold) == file_registry._key(journal, fold):
            return True
        if conn is not None:
            return _stem_taken(conn, campaign_id, name, fold)
        with db.connection() as c:
            return _stem_taken(c, campaign_id, name, fold)

    candidate = nfc(stem)
    if not taken(candidate):
        return candidate
    n = 2
    while True:
        candidate = f"{nfc(stem)} ({n})"
        if not taken(candidate):
            return candidate
        n += 1


def _stem_taken(conn: sqlite3.Connection, campaign_id: Optional[int], stem: str,
                fold: bool) -> bool:
    if _stem_row(conn, campaign_id, stem) is not None:
        return True
    if not fold:
        return False
    if campaign_id is None:
        rows = conn.execute("SELECT stem FROM transcripts WHERE campaign_id IS NULL")
    else:
        rows = conn.execute("SELECT stem FROM transcripts WHERE campaign_id = ?", (campaign_id,))
    want = file_registry._key(stem, fold)
    return any(file_registry._key(r[0], fold) == want for r in rows)


@dataclass(frozen=True)
class MoveCheck:
    """Whether a transcript can move or rename, and where it would land.

    Pure: :func:`check_move` computes it with reads only, never a ``mkdir`` or a
    write, so the clash page can render it. ``clash_modified`` is the clashing
    file's last-modified time for the prompt. ``overwrite_allowed`` is true only
    when the clashing file is a registered session (``existing_id`` set).
    """

    status: str
    dst_dir: Path
    stem: str
    clash_modified: Optional[str]
    overwrite_allowed: bool
    existing_id: Optional[int]
    _files_move: bool = True

    @property
    def files_move(self) -> bool:
        """False when there is nothing to move on disk: the session is flagged
        missing, or the ``.md`` is already in ``dst_dir`` under ``stem``."""
        return self._files_move


def _stem_row_folded(conn: sqlite3.Connection, campaign_id: Optional[int], stem: str, fold: bool):
    """The row named ``stem`` in a campaign (or the root), ignoring case when
    the filesystem does. Uses the partial unique index for the exact match."""
    row = _stem_row(conn, campaign_id, stem)
    if row is not None or not fold:
        return row
    if campaign_id is None:
        rows = conn.execute("SELECT id, stem FROM transcripts WHERE campaign_id IS NULL")
    else:
        rows = conn.execute(
            "SELECT id, stem FROM transcripts WHERE campaign_id = ?", (campaign_id,))
    want = file_registry._key(stem, fold)
    for candidate in rows:
        if file_registry._key(candidate[1], fold) == want:
            return candidate
    return None


def _clash_path(directory: Path, stem: str, fold: bool) -> Optional[Path]:
    """An existing ``<stem>.md`` in ``directory``, ignoring case when ``fold``."""
    target = safe_path(stem, ".md", directory)
    if target is not None and target.is_file():
        return target
    if not fold:
        return None
    want = file_registry._key(f"{nfc(stem)}.md", fold)
    try:
        entries = os.scandir(directory)
    except OSError:
        return None
    with entries:
        for entry in entries:
            try:
                if entry.is_file() and file_registry._key(entry.name, fold) == want:
                    return Path(entry.path)
            except OSError:
                continue
    return None


def check_move(transcript_id: int, campaign_slug: Optional[str], *, new_stem: Optional[str] = None,
               conn: Optional[sqlite3.Connection] = None, data_dir: Optional[Path] = None,
               output_dir: Optional[Path] = None) -> MoveCheck:
    """Whether transcript ``transcript_id`` can move to ``campaign_slug`` (or the
    root for None), optionally renamed to ``new_stem``.

    Pure: no ``mkdir`` and no writes. Validation runs before any disk access on
    the name, so a query-string name is a path guard too. Status is ``ok`` when
    the move may proceed, else ``unchanged``, ``invalid``, ``busy``,
    ``folder_taken``, ``reserved``, or ``clash``.
    """
    if conn is not None:
        return _check_move_in(conn, transcript_id, campaign_slug, new_stem, output_dir)
    with db.connection(data_dir) as c:
        return _check_move_in(c, transcript_id, campaign_slug, new_stem, output_dir)


def _check_move_in(conn: sqlite3.Connection, transcript_id: int, campaign_slug: Optional[str],
                   new_stem: Optional[str], output_dir: Optional[Path]) -> MoveCheck:
    from .campaign_folders import _fold, holds_only_wisper, journal_name

    output = Path(output_dir) if output_dir is not None else get_output_root()
    target_cid: Optional[int] = None
    target_folder: Optional[str] = None
    target_pending: Optional[str] = None
    target_claimed = False
    if campaign_slug is None:
        target_dir = output
    else:
        row = conn.execute(
            "SELECT id, folder, folder_pending, folder_claimed FROM campaigns WHERE slug = ?",
            (campaign_slug,),
        ).fetchone()
        if row is None:
            return MoveCheck("invalid", output, nfc(new_stem or ""), None, False, None)
        target_cid, target_folder, target_pending, target_claimed = (
            row[0], row[1], row[2], bool(row[3]))
        target_dir = output / target_folder

    stem = ""
    if new_stem is not None:
        cleaned = validate_new_stem(new_stem)
        if cleaned is None or safe_path(cleaned, ".md", target_dir) is None:
            return MoveCheck("invalid", target_dir, cleaned or nfc(new_stem.strip()), None, False, None)
        stem = cleaned

    loc = _locate_in(conn, transcript_id, output)
    if loc is None:
        return MoveCheck("invalid", target_dir, stem, None, False, None)
    if new_stem is None:
        stem = loc.stem

    fold = loc.fold
    same_dir = _dirs_equal(loc.dir, target_dir, loc.fold)
    files_move = not loc.missing and not (same_dir and nfc(stem) == nfc(loc.stem))

    def unchanged() -> MoveCheck:
        return MoveCheck("unchanged", target_dir, stem, None, False, None, _files_move=files_move)

    if loc.campaign_id == target_cid and nfc(stem) == nfc(loc.stem):
        return unchanged()

    source_slug = None
    if loc.campaign_id is not None:
        r = conn.execute("SELECT slug FROM campaigns WHERE id = ?", (loc.campaign_id,)).fetchone()
        source_slug = r[0] if r is not None else None

    from .job_history import active_jobs
    if active_jobs(conn, transcript_id=transcript_id,
                   campaign_ids={loc.campaign_id, target_cid} - {None},
                   campaign_slugs={source_slug, campaign_slug} - {None}):
        return MoveCheck("busy", target_dir, stem, None, False, None, _files_move=files_move)

    if target_cid is not None:
        if target_pending is not None:
            return MoveCheck("folder_taken", target_dir, stem, None, False, None)
        if target_claimed and not target_dir.is_dir():
            return MoveCheck("folder_taken", target_dir, stem, None, False, None)
        if (not target_claimed and target_dir.is_dir()
                and not holds_only_wisper(target_dir, target_folder)):
            return MoveCheck("folder_taken", target_dir, stem, None, False, None)
        if _fold(stem) == _fold(Path(journal_name(target_folder)).stem):
            return MoveCheck("reserved", target_dir, stem, None, False, None)

    clash_file = _clash_path(target_dir, stem, fold)
    if clash_file is not None and _same_file(clash_file, loc.md):
        clash_file = None
    existing = _stem_row_folded(conn, target_cid, stem, fold)
    existing_id = existing[0] if existing is not None and existing[0] != transcript_id else None
    if clash_file is None and existing_id is None:
        return MoveCheck("ok", target_dir, stem, None, False, None, _files_move=files_move)

    if existing_id is None and clash_file is not None:
        hit = _locate_path_in(conn, clash_file, output)
        existing_id = None if hit == transcript_id else hit
    if clash_file is not None:
        modified = _modified_local(clash_file)
    elif existing_id is not None:
        other = _locate_in(conn, existing_id, output)
        modified = _modified_local(other.md) if other is not None else None
    else:
        modified = None
    return MoveCheck("clash", target_dir, stem, modified, existing_id is not None, existing_id,
                     _files_move=files_move)


@dataclass(frozen=True)
class MoveOutcome:
    """What a move, rename, or move-home did.

    ``status`` is ``moved``, ``unchanged``, ``invalid``, ``busy``,
    ``folder_taken``, ``unavailable``, ``clash``, ``reserved``, ``locked``, or
    ``partial``. ``detail`` carries ``folder_pending``/``folder_missing`` for
    ``folder_taken`` and ``source_missing`` for ``locked``. ``kept`` names the
    files left behind (a ``partial``); ``busy`` the job ids that refused it.
    """

    status: str
    detail: Optional[str] = None
    new_stem: Optional[str] = None
    clash_modified: Optional[str] = None
    overwrite_allowed: bool = False
    kept: list[Path] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)


def _slug_of(conn: sqlite3.Connection, campaign_id: Optional[int]) -> Optional[str]:
    if campaign_id is None:
        return None
    row = conn.execute("SELECT slug FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    return row[0] if row is not None else None


def _target_cid(conn: sqlite3.Connection, campaign_slug: Optional[str]) -> Optional[int]:
    if campaign_slug is None:
        return None
    row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (campaign_slug,)).fetchone()
    return row[0] if row is not None else None


def _move_busy_ids(conn: sqlite3.Connection, transcript_id: int, campaign_slug: Optional[str],
                   output: Path, extra_transcript_id: Optional[int] = None) -> list:
    """The active job ids that make a move of ``transcript_id`` unsafe.

    The whole source and target campaign is in scope, because journal and
    relabel jobs read every session of their campaign. ``extra_transcript_id``
    adds the transcript an Overwrite would delete.
    """
    from .job_history import active_jobs

    loc = _locate_in(conn, transcript_id, output)
    if loc is None:
        return []
    campaign_ids = {loc.campaign_id, _target_cid(conn, campaign_slug)} - {None}
    campaign_slugs = {_slug_of(conn, loc.campaign_id), campaign_slug} - {None}
    ids: set = set()
    for tid in (transcript_id, extra_transcript_id):
        if tid is None:
            continue
        ids.update(active_jobs(conn, transcript_id=tid,
                               campaign_ids=campaign_ids, campaign_slugs=campaign_slugs))
    return sorted(ids)


def _folder_taken_detail(campaign_slug: Optional[str], data_dir: Optional[Path],
                         output: Path) -> Optional[str]:
    if campaign_slug is None:
        return None
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT folder, folder_pending, folder_claimed FROM campaigns WHERE slug = ?",
            (campaign_slug,)).fetchone()
    if row is None:
        return None
    folder, pending, claimed = row[0], row[1], bool(row[2])
    if pending is not None:
        return "folder_pending"
    if claimed and not (output / folder).is_dir():
        return "folder_missing"
    return None


def _outcome_from_check(check: MoveCheck, transcript_id: int, campaign_slug: Optional[str],
                        data_dir: Optional[Path], output: Path) -> MoveOutcome:
    """Map a non-``ok`` :class:`MoveCheck` to a :class:`MoveOutcome`."""
    if check.status == "busy":
        with db.connection(data_dir) as conn:
            return MoveOutcome("busy", busy=_move_busy_ids(conn, transcript_id, campaign_slug, output))
    if check.status == "folder_taken":
        return MoveOutcome("folder_taken", detail=_folder_taken_detail(campaign_slug, data_dir, output))
    return MoveOutcome(check.status, new_stem=check.stem, clash_modified=check.clash_modified,
                       overwrite_allowed=check.overwrite_allowed)


def _move_md(transcript_id: int, old_md: Path, target_md: Path, data_dir: Optional[Path],
             output: Path) -> tuple:
    """Move a transcript's ``.md`` through its registry row (``files follow rows``).

    Returns ``(result, had_row)`` with the :func:`file_registry.move` result, or
    ``os.replace`` + :func:`file_registry.add` when the row isn't registered yet.
    """
    owner = file_registry.Owner("transcript", transcript_id)
    row = file_registry.file_for(owner, "transcript", data_dir=data_dir, output_dir=output)
    if row is not None:
        return file_registry.move(row, target_md, data_dir=data_dir, output_dir=output), True
    try:
        os.replace(old_md, target_md)
    except OSError as exc:
        log.warning("Could not move %s to %s: %s", old_md, target_md, exc)
        return "error", False
    file_registry.add(target_md, kind="transcript", owner=owner, data_dir=data_dir, output_dir=output)
    return "moved", False


def _revert_move(transcript_id: int, *, old_cid: Optional[int], old_stem: str,
                 old_pos: Optional[int], new_cid: Optional[int], new_stem: str,
                 data_dir: Optional[Path]) -> int:
    """Undo step 5's assignment after a failed file move, compare-and-swapped.

    Only a row still at ``(new_cid, new_stem)`` is reverted. The old position is
    kept when its slot is free, else the row is appended; the root has none.
    Returns the rows changed (0 when the swap lost a race or the old name was
    taken, or an ``IntegrityError``).
    """
    with db.transaction(data_dir) as conn:
        pos = old_pos
        if old_cid is None:
            pos = None
        elif old_pos is None or conn.execute(
                "SELECT 1 FROM transcripts WHERE campaign_id = ? AND position = ? AND id <> ?",
                (old_cid, old_pos, transcript_id)).fetchone():
            pos = conn.execute(
                "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
                (old_cid,)).fetchone()[0]
        try:
            cur = conn.execute(
                "UPDATE transcripts SET campaign_id = :oc, stem = :os, position = :op "
                "WHERE id = :id AND campaign_id IS :nc AND stem = :ns",
                {"oc": old_cid, "os": old_stem, "op": pos, "id": transcript_id,
                 "nc": new_cid, "ns": new_stem})
        except sqlite3.IntegrityError:
            return 0
        return cur.rowcount


def _revert_stem(transcript_id: int, *, old_stem: str, new_stem: str,
                 data_dir: Optional[Path]) -> int:
    """Undo a rename after a failed ``.md`` move, compare-and-swapped on the stem."""
    with db.transaction(data_dir) as conn:
        try:
            cur = conn.execute("UPDATE transcripts SET stem = ? WHERE id = ? AND stem = ?",
                               (old_stem, transcript_id, new_stem))
        except sqlite3.IntegrityError:
            return 0
        return cur.rowcount


def _recheck_clash(conn: sqlite3.Connection, transcript_id: int, target_cid: Optional[int],
                   directory: Path, stem: str, loc: Located, output: Path):
    """Re-run a move's clash check inside the write transaction.

    Returns ``(clash_file, existing_id)``; both None means free. The session's
    own file and row are excluded, so a misplaced move-home or a case-only
    rename isn't a clash with itself.
    """
    fold = file_registry._fold(output)
    clash_file = _clash_path(directory, stem, fold)
    if clash_file is not None and _same_file(clash_file, loc.md):
        clash_file = None
    existing = _stem_row_folded(conn, target_cid, stem, fold)
    existing_id = existing[0] if existing is not None and existing[0] != transcript_id else None
    return clash_file, existing_id


def move_transcript(transcript_id: int, campaign_slug: Optional[str], *,
                    clash: Literal["ask", "overwrite", "keep_both", "skip"] = "ask",
                    data_dir: Optional[Path] = None,
                    output_dir: Optional[Path] = None) -> MoveOutcome:
    """Move a transcript to a campaign (or the root) and move its files.

    Rows change first; the files follow after the commit (``files follow rows``).
    ``_LOCATION_LOCK`` is held from before the first write transaction until the
    files have moved, so no reconcile races the swap. A failed ``.md`` move is
    reverted with a compare-and-swap on the whole assignment; a companion that
    can't move stays registered, and the result is ``partial``. ``clash``
    chooses the clash behaviour: ``ask``/``skip`` return ``clash`` unchanged,
    ``keep_both`` appends `` (2)``, ``overwrite`` deletes the clashing session
    first (only when it is a registered transcript).
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    check = check_move(transcript_id, campaign_slug, data_dir=data_dir, output_dir=output)

    if check.status == "clash":
        if clash == "overwrite" and not check.overwrite_allowed:
            return MoveOutcome("clash", new_stem=check.stem,
                               clash_modified=check.clash_modified, overwrite_allowed=False)
        if clash in ("ask", "skip"):
            return MoveOutcome("clash", new_stem=check.stem,
                               clash_modified=check.clash_modified,
                               overwrite_allowed=check.overwrite_allowed)
    elif check.status != "ok":
        return _outcome_from_check(check, transcript_id, campaign_slug, data_dir, output)

    files_move = check.files_move
    dst_dir = check.dst_dir

    with _LOCATION_LOCK:
        with db.connection(data_dir) as conn:
            target_cid = _target_cid(conn, campaign_slug)
        if campaign_slug is not None and target_cid is None:
            return MoveOutcome("invalid")

        # Step 2: the target folder (or the root) when there is a file to move.
        if files_move:
            if target_cid is not None:
                from .campaign_folders import (FolderMissingError, FolderPendingError,
                                               FolderTakenError, ensure_folder)
                try:
                    dst_dir = ensure_folder(target_cid, data_dir=data_dir, output_dir=output)
                except FolderPendingError:
                    return MoveOutcome("folder_taken", detail="folder_pending")
                except FolderMissingError:
                    return MoveOutcome("folder_taken", detail="folder_missing")
                except FolderTakenError:
                    return MoveOutcome("folder_taken")
                except FileNotFoundError:
                    return MoveOutcome("unavailable")
            else:
                dst_dir = output
                if not dst_dir.is_dir():
                    return MoveOutcome("unavailable")

        # Step 3: overwrite deletes the clashing session first.
        if check.status == "clash" and clash == "overwrite" and check.existing_id is not None:
            with db.transaction(data_dir) as conn:
                ids = _move_busy_ids(conn, transcript_id, campaign_slug, output,
                                     extra_transcript_id=check.existing_id)
                if ids:
                    return MoveOutcome("busy", busy=ids)
            if delete_transcript(check.existing_id, data_dir=data_dir, output_dir=output) == "kept":
                return MoveOutcome("locked")

        # Step 4: keep both picks a free name in the target folder.
        new_stem = check.stem
        if check.status == "clash" and clash == "keep_both":
            with db.connection(data_dir) as conn:
                new_stem = next_free_stem(dst_dir, check.stem, target_cid, conn=conn)

        # Step 5: one transaction, re-checking busy and clash under the write lock.
        with db.transaction(data_dir) as conn:
            ids = _move_busy_ids(conn, transcript_id, campaign_slug, output)
            if ids:
                return MoveOutcome("busy", busy=ids)
            loc = _locate_in(conn, transcript_id, output)
            if loc is None:
                return MoveOutcome("invalid")
            clash_file, existing_id = _recheck_clash(
                conn, transcript_id, target_cid, dst_dir, new_stem, loc, output)
            if clash_file is not None or existing_id is not None:
                modified = _modified_local(clash_file) if clash_file is not None else None
                return MoveOutcome("clash", new_stem=new_stem, clash_modified=modified,
                                   overwrite_allowed=existing_id is not None)
            old = conn.execute(
                "SELECT campaign_id, stem, position FROM transcripts WHERE id = ?",
                (transcript_id,)).fetchone()
            old_cid, old_stem, old_pos = old[0], old[1], old[2]
            old_dir, old_md = loc.dir, loc.md
            if target_cid is None:
                new_pos = None
            else:
                new_pos = conn.execute(
                    "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
                    (target_cid,)).fetchone()[0]
            from .campaign_manager import _assign
            _assign(conn, transcript_id, target_cid, new_pos, stem=new_stem)

        # Step 6: the files follow the rows.
        if not files_move:
            return MoveOutcome("moved", new_stem=new_stem)
        target_md = dst_dir / f"{new_stem}.md"
        result, had_row = _move_md(transcript_id, old_md, target_md, data_dir, output)
        if result in ("error", "conflict", "missing"):
            if result == "missing" and had_row:
                file_registry.add(old_md, kind="transcript",
                                  owner=file_registry.Owner("transcript", transcript_id),
                                  data_dir=data_dir, output_dir=output)
            changed = _revert_move(transcript_id, old_cid=old_cid, old_stem=old_stem,
                                   old_pos=old_pos, new_cid=target_cid, new_stem=new_stem,
                                   data_dir=data_dir)
            if changed:
                detail = "source_missing" if result == "missing" else None
                return MoveOutcome("locked", detail=detail, kept=[old_md])
            return MoveOutcome("partial", new_stem=new_stem)
        kept = rename_companions(transcript_id, old_stem, new_stem, src_dir=old_dir,
                                 dst_dir=dst_dir, output_dir=output, data_dir=data_dir)
        if kept:
            return MoveOutcome("partial", new_stem=new_stem, kept=kept)
        return MoveOutcome("moved", new_stem=new_stem)


def move_files_home(transcript_id: int, *, data_dir: Optional[Path] = None,
                     output_dir: Optional[Path] = None) -> MoveOutcome:
    """Move a misplaced session's files into the folder its campaign names.

    The assignment doesn't change, so no ``transcripts`` row is written; every
    registered file moves. A name already in the target folder is a ``clash``:
    it is never overwritten or renamed automatically. A missing session changes
    nothing (``unchanged``). Used by Needs attention and ``storage trim``.

    ``_LOCATION_LOCK`` is held from the busy check until the files have moved,
    so neither a reconcile nor another move runs in between.
    """
    with _LOCATION_LOCK:
        return _move_files_home_locked(transcript_id, data_dir=data_dir, output_dir=output_dir)


def _move_files_home_locked(transcript_id: int, *, data_dir: Optional[Path],
                            output_dir: Optional[Path]) -> MoveOutcome:
    output = Path(output_dir) if output_dir is not None else get_output_root()
    with db.transaction(data_dir) as conn:
        loc = _locate_in(conn, transcript_id, output)
        if loc is None:
            return MoveOutcome("invalid")
        campaign_slug = _slug_of(conn, loc.campaign_id)
        ids = _move_busy_ids(conn, transcript_id, campaign_slug, output)
        if ids:
            return MoveOutcome("busy", busy=ids)
        target_cid = loc.campaign_id
        stem = loc.stem
        old_dir = loc.dir
        md = loc.md
        companions = dict(loc.companions)
        missing = loc.missing
        fold = loc.fold

    if missing:
        return MoveOutcome("unchanged", new_stem=stem)
    if target_cid is not None:
        from .campaign_folders import (FolderMissingError, FolderPendingError,
                                       FolderTakenError, ensure_folder)
        try:
            expected = ensure_folder(target_cid, data_dir=data_dir, output_dir=output)
        except FolderPendingError:
            return MoveOutcome("folder_taken", detail="folder_pending")
        except FolderMissingError:
            return MoveOutcome("folder_taken", detail="folder_missing")
        except FolderTakenError:
            return MoveOutcome("folder_taken")
        except FileNotFoundError:
            return MoveOutcome("unavailable")
    else:
        expected = output
        if not expected.is_dir():
            return MoveOutcome("unavailable")

    md_moves = not _dirs_equal(old_dir, expected, fold)
    if not md_moves and all(_dirs_equal(p.parent, expected, fold) for p in companions.values()):
        return MoveOutcome("unchanged", new_stem=stem)

    targets = [(path, expected / path.name) for path in companions.values()
               if os.path.lexists(path) and not _dirs_equal(path.parent, expected, fold)]
    if md_moves:
        targets.insert(0, (md, expected / f"{stem}.md"))
    for src, dst in targets:
        if os.path.lexists(dst) and not _same_file(src, dst):
            return MoveOutcome("clash", new_stem=stem, clash_modified=_modified_local(dst))

    owner = file_registry.Owner("transcript", transcript_id)
    rows = {(r.kind, r.label or ""): r for r in
            file_registry.files_for(owner, data_dir=data_dir, output_dir=output)}
    kept: list = []
    if md_moves:
        result, had_row = _move_md(transcript_id, md, expected / f"{stem}.md", data_dir, output)
        if result in ("error", "conflict", "missing"):
            if result == "missing" and had_row:
                file_registry.add(md, kind="transcript", owner=owner,
                                  data_dir=data_dir, output_dir=output)
            return MoveOutcome("locked", kept=[md])
    for key, path in companions.items():
        if _dirs_equal(path.parent, expected, fold):
            continue
        row = rows.get(key)
        if row is None:
            continue
        if file_registry.move(row, expected / path.name,
                              data_dir=data_dir, output_dir=output) in ("conflict", "error"):
            kept.append(path)
    if kept:
        return MoveOutcome("partial", new_stem=stem, kept=kept)
    return MoveOutcome("moved", new_stem=stem)


def rename_transcript(transcript_id: int, new_name: str, *,
                      clash: Literal["ask", "overwrite", "keep_both", "skip"] = "ask",
                      data_dir: Optional[Path] = None,
                      output_dir: Optional[Path] = None) -> MoveOutcome:
    """Rename a transcript's ``.md`` and every companion together.

    The stem changes in one transaction; the files follow after the commit,
    holding ``_LOCATION_LOCK``. The same clash rule as a move applies. A failed
    ``.md`` move restores the old stem (compare-and-swapped) and returns
    ``locked``; a companion that can't move is a ``partial``. Case-only renames
    are allowed.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    cleaned = validate_new_stem(new_name)
    if cleaned is None:
        return MoveOutcome("invalid")
    with db.connection(data_dir) as conn:
        loc = _locate_in(conn, transcript_id, output)
        if loc is None:
            return MoveOutcome("invalid")
        campaign_slug = _slug_of(conn, loc.campaign_id)
        current_cid = loc.campaign_id
        old_dir = loc.dir
        old_md = loc.md
        missing = loc.missing

    check = check_move(transcript_id, campaign_slug, new_stem=cleaned,
                       data_dir=data_dir, output_dir=output)
    if check.status == "clash":
        if clash == "overwrite" and not check.overwrite_allowed:
            return MoveOutcome("clash", new_stem=check.stem,
                               clash_modified=check.clash_modified, overwrite_allowed=False)
        if clash in ("ask", "skip"):
            return MoveOutcome("clash", new_stem=check.stem,
                               clash_modified=check.clash_modified,
                               overwrite_allowed=check.overwrite_allowed)
    elif check.status != "ok":
        return _outcome_from_check(check, transcript_id, campaign_slug, data_dir, output)

    with _LOCATION_LOCK:
        if check.status == "clash" and clash == "overwrite" and check.existing_id is not None:
            with db.transaction(data_dir) as conn:
                ids = _move_busy_ids(conn, transcript_id, campaign_slug, output,
                                     extra_transcript_id=check.existing_id)
                if ids:
                    return MoveOutcome("busy", busy=ids)
            if delete_transcript(check.existing_id, data_dir=data_dir, output_dir=output) == "kept":
                return MoveOutcome("locked")

        new_stem = cleaned
        if check.status == "clash" and clash == "keep_both":
            with db.connection(data_dir) as conn:
                new_stem = next_free_stem(old_dir, cleaned, current_cid, conn=conn)

        with db.transaction(data_dir) as conn:
            ids = _move_busy_ids(conn, transcript_id, campaign_slug, output)
            if ids:
                return MoveOutcome("busy", busy=ids)
            loc = _locate_in(conn, transcript_id, output)
            if loc is None:
                return MoveOutcome("invalid")
            old = conn.execute("SELECT stem FROM transcripts WHERE id = ?",
                               (transcript_id,)).fetchone()
            old_stem = old[0]
            old_dir, old_md = loc.dir, loc.md
            clash_file, existing_id = _recheck_clash(
                conn, transcript_id, current_cid, old_dir, new_stem, loc, output)
            if clash_file is not None or existing_id is not None:
                modified = _modified_local(clash_file) if clash_file is not None else None
                return MoveOutcome("clash", new_stem=new_stem, clash_modified=modified,
                                   overwrite_allowed=existing_id is not None)
            conn.execute("UPDATE transcripts SET stem = ? WHERE id = ?", (new_stem, transcript_id))

        if missing:
            return MoveOutcome("moved", new_stem=new_stem)
        target_md = old_dir / f"{new_stem}.md"
        result, had_row = _move_md(transcript_id, old_md, target_md, data_dir, output)
        if result in ("error", "conflict", "missing"):
            if result == "missing" and had_row:
                file_registry.add(old_md, kind="transcript",
                                  owner=file_registry.Owner("transcript", transcript_id),
                                  data_dir=data_dir, output_dir=output)
            changed = _revert_stem(transcript_id, old_stem=old_stem, new_stem=new_stem,
                                   data_dir=data_dir)
            if changed:
                return MoveOutcome("locked", kept=[old_md])
            return MoveOutcome("partial", new_stem=new_stem)
        kept = rename_companions(transcript_id, old_stem, new_stem, src_dir=old_dir,
                                 output_dir=output, data_dir=data_dir)
        if kept:
            return MoveOutcome("partial", new_stem=new_stem, kept=kept)
        return MoveOutcome("moved", new_stem=new_stem)


def list_transcripts(conn: Optional[sqlite3.Connection] = None, *,
                     campaign_id: object = ANY,
                     data_dir: Optional[Path] = None,
                     output_dir: Optional[Path] = None) -> list["Located"]:
    """Every **present** transcript, newest file first.

    ``campaign_id`` defaults to :data:`ANY`; ``None`` means the root only, an
    int one campaign. Ordered by the ``transcript`` row's ``mtime_ns``
    descending (rows without a fingerprint last), then ``created_at``
    descending, then ``id``. Rows flagged missing are left out; they belong to
    Needs attention and their campaign page.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if conn is not None:
        ids = _list_ids(conn, campaign_id)
    else:
        with db.connection(data_dir) as c:
            ids = _list_ids(c, campaign_id)
    found = []
    for tid in ids:
        loc = locate(tid, conn=conn, data_dir=data_dir, output_dir=output)
        if loc is not None:
            found.append(loc)
    return found


def _list_ids(conn: sqlite3.Connection, campaign_id: object) -> list[int]:
    """Ids of present transcripts in the required order (see list_transcripts)."""
    if campaign_id is ANY:
        where, params = "t.missing_since IS NULL", []
    elif campaign_id is None:
        where, params = "t.missing_since IS NULL AND t.campaign_id IS NULL", []
    else:
        where, params = "t.missing_since IS NULL AND t.campaign_id = ?", [campaign_id]
    rows = conn.execute(
        "SELECT t.id FROM transcripts t "
        "LEFT JOIN files f ON f.transcript_id = t.id AND f.kind = 'transcript' "
        f"WHERE {where} "
        "ORDER BY (f.mtime_ns IS NULL), f.mtime_ns DESC, t.created_at DESC, t.id",
        params,
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def ensure_row(conn: sqlite3.Connection, stem: str, output_dir: Optional[Path] = None,
               *, campaign_id: Optional[int] = None) -> int:
    """The registry row for ``stem`` in a campaign, created if absent (in ``conn``).

    ``campaign_id`` ``None`` means the root. A new campaign row is appended to
    the campaign's order; a root row has no position. A new row whose ``.md``
    doesn't exist is flagged ``missing_since`` (e.g. a campaign association
    made before the transcript was written).
    """
    stem = nfc(stem)
    row = _stem_row(conn, campaign_id, stem)
    if row is not None:
        return row[0]
    output = Path(output_dir) if output_dir is not None else get_output_root()
    expected = output if campaign_id is None else expected_dir(campaign_id, conn, output_dir=output)
    md = safe_path(stem, ".md", expected)
    exists = md is not None and md.is_file()
    now = db.now_utc()
    position = None
    if campaign_id is not None:
        position = conn.execute(
            "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()[0]
    return conn.execute(
        "INSERT INTO transcripts (stem, campaign_id, position, created_at, missing_since) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (stem, campaign_id, position, _file_timestamp(md) if exists else now,
         None if exists else now),
    ).fetchone()[0]


def _row_for_path(conn: sqlite3.Connection, md_path: Path, *,
                  data_dir: Optional[Path] = None,
                  output_dir: Optional[Path] = None) -> Optional[int]:
    """The transcript row the ``.md`` at ``md_path`` belongs to, else None.

    Runs inside the caller's ``conn`` and touches no files or speakers; it may
    create the row (:func:`ensure_row`) for a new ``.md`` in a transcript folder. A file already registered as
    some transcript's ``.md`` is that transcript, whatever its campaign (a
    misplaced session re-transcribed in place keeps its row). Otherwise, in a
    transcript folder, a row of that campaign and stem is the file's session,
    unless that row already has a different, present ``.md`` (a misplaced
    session: the newcomer must not take over the row).
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    md_path = Path(md_path)
    fold = file_registry._fold(output)
    found = _locate_path_in(conn, md_path, output)
    if found is not None:
        return found
    has_dir, campaign_id = dir_campaign(md_path.parent, conn, output_dir=output)
    if not has_dir:
        return None
    stem = _path_stem(md_path)
    row = _stem_row(conn, campaign_id, stem)
    if row is None:
        return ensure_row(conn, stem, output, campaign_id=campaign_id)
    registered = conn.execute(
        "SELECT rel_path FROM files WHERE transcript_id = ? AND kind = 'transcript' LIMIT 1",
        (row[0],),
    ).fetchone()
    if registered is not None:
        other = db.from_rel(registered[0], output)
        if _same_file(other, md_path) or not other.is_file():
            return row[0]
        return None  # a different, present .md already owns that row
    return row[0]


def register(md_path: Path, *, origin: Literal["job", "reconcile"],
             data_dir: Optional[Path] = None) -> Optional[int]:
    """Record that the ``.md`` at ``md_path`` exists. Returns the row id, or None.

    Call after the ``.md`` is written. The row is found (or created) by
    :func:`_row_for_path`, so a file in a campaign folder joins that campaign and
    a misplaced session keeps its row. An existing row keeps its id, campaign
    position, and journal entries, so an overwrite or re-transcribe keeps its
    links; a row flagged missing is un-flagged. ``origin`` is ``"job"`` when the
    app just wrote the file (an existing, journaled transcript then marks that
    campaign's journal stale), ``"reconcile"`` when it was found on disk. None
    means nothing was registered (the file is in no transcript folder).
    """
    if origin not in ("job", "reconcile"):
        raise ValueError(f"invalid origin: {origin!r}")
    md_path = Path(md_path)
    output = get_output_root()
    stale_sidecar = None
    with db.transaction(data_dir) as conn:
        tid = _row_for_path(conn, md_path, data_dir=data_dir, output_dir=output)
        if tid is None:
            log.warning("Not registering %s: it is not in a transcript folder", md_path.name)
            return None
        stem = conn.execute("SELECT stem FROM transcripts WHERE id = ?", (tid,)).fetchone()[0]
        row = conn.execute("SELECT missing_since FROM transcripts WHERE id = ?", (tid,)).fetchone()
        if row["missing_since"] is not None:
            conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (tid,))
            if origin == "job":
                log.info("Reused the name of a previously missing transcript: %s", stem)
        if origin == "job":
            # Overwritten or re-transcribed: a journal that folded the old text
            # describes something else. Flag it; never un-fold automatically.
            conn.execute(
                "UPDATE campaigns SET journal_stale_since = coalesce(journal_stale_since, ?) "
                "WHERE id IN (SELECT campaign_id FROM journal_entries WHERE transcript_id = ?)",
                (db.now_utc(), tid),
            )
            # The old run's speakers describe the old text. A web job writes
            # fresh ones (write_sidecar) right after; a CLI overwrite has none.
            conn.execute("DELETE FROM transcript_speakers WHERE transcript_id = ?", (tid,))
            removed = file_registry.forget_kind(file_registry.Owner("transcript", tid), "sidecar",
                                                conn=conn, data_dir=data_dir, output_dir=output)
            # Beside this .md: the output root may hold another session of the same name.
            stale_sidecar = removed[0] if removed else safe_path(stem, SIDECAR_SUFFIX, md_path.parent)
        file_registry.add_if_owned(md_path, kind="transcript",
                                   owner=file_registry.Owner("transcript", tid),
                                   conn=conn, data_dir=data_dir, output_dir=output)
    if stale_sidecar is not None:
        stale_sidecar.unlink(missing_ok=True)
    # The app just wrote this file: index it now. Files found on disk
    # (reconcile) are left to the backfill, so no request parses them.
    if origin == "job":
        _reindex(tid, data_dir)
    return tid


def _reindex(transcript_id: int, data_dir: Optional[Path]) -> None:
    """Index a transcript the app just wrote or relinked; never fails the write."""
    from .search_index import reindex
    try:
        reindex(transcript_id, data_dir=data_dir)
    except Exception:
        log.warning("Search reindex failed for transcript %s", transcript_id, exc_info=True)


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _companion_paths(loc: "Located", conn: Optional[sqlite3.Connection] = None) -> list[Path]:
    """Every file that belongs to ``loc``'s transcript except its ``.md``.

    The union of three sources, so unregistered files are still found:
    - the transcript's ``files`` rows (summary, sidecar, audio, excerpts, backup);
    - the names derived from the stem, in :attr:`Located.dir` (summary,
      sidecar, ``glob.escape``d excerpt clips);
    - an old sidecar's ``input_path``, only inside the output root (old
      sidecars point at temp dirs or user files) and never under
      ``<data>/recordings/``.

    Read before anything is deleted: an audio copy's name can't be derived
    from the stem.
    """
    output_dir = get_output_root()
    stem = loc.stem
    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            paths.append(path)

    recordings = Path(os.path.realpath(db._data_dir(None) / "recordings"))

    def in_recordings(path: Path) -> bool:
        return Path(os.path.realpath(path)).is_relative_to(recordings)

    for path in loc.companions.values():
        if not in_recordings(path):
            add(path)
    summary = loc.companion(".summary.md")
    add(summary)
    sidecar = loc.companion(SIDECAR_SUFFIX)
    add(sidecar)
    try:
        stored = json.loads(sidecar.read_text(encoding="utf-8")).get("input_path")
    except (OSError, ValueError, AttributeError):
        stored = None
    if stored:
        base = os.path.abspath(str(loc.dir))
        if not base.endswith(os.sep):
            base += os.sep
        candidate = os.path.abspath(stored)
        if (candidate.startswith(base) and not candidate.endswith(".md")
                and not in_recordings(Path(candidate))):
            add(Path(candidate))
    if loc.md is not None:
        # glob.escape: a stem like "mix*" must not match other transcripts' clips.
        for clip in loc.dir.glob(f"{glob.escape(stem)}_excerpt_*"):
            add(clip)
    return paths


def delete_transcript(transcript_id: int, data_dir: Optional[Path] = None,
                      output_dir: Optional[Path] = None) -> Literal["deleted", "kept", "absent"]:
    """Delete a transcript, its links, and its companion files.

    The only way a transcript is deleted. Follows the ordering rule: read the
    companion paths (registered and name-derived) before any write lock,
    unlink the ``.md``, delete the row in one transaction (cascading to its
    ``files``, campaign, and journal rows), then unlink the companions. A
    crash between the last two steps leaves companions with no ``.md`` and
    no row; Needs attention lists them.

    Returns ``"absent"`` when there is no such row, ``"kept"`` when the ``.md``
    couldn't be unlinked (the row stays too), else ``"deleted"``.
    """
    with db.connection(data_dir) as conn:
        loc = locate(transcript_id, conn=conn, data_dir=data_dir, output_dir=output_dir)
        if loc is None:
            return "absent"
        companions = _companion_paths(loc, conn)
    md = loc.md
    try:
        md.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not delete %s: %s", md.name, exc)
        return "kept"  # the file is still there, so the row stays too

    with db.transaction(data_dir) as conn:
        # Cascades to files/campaign/journal/speaker rows; a recording that
        # produced it goes back to "completed" (recordings.transcript_id SET NULL).
        conn.execute("DELETE FROM transcripts WHERE id = ?", (transcript_id,))

    for path in companions:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return "deleted"


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


def transcript_dirs(conn: Optional[sqlite3.Connection] = None, *,
                    data_dir: Optional[Path] = None,
                    output_dir: Optional[Path] = None) -> list[tuple[Path, Optional[int]]]:
    """The directories reconcile and sync scan: the output root, then each
    claimed campaign folder that exists.

    ``(dir, None)`` is the output root; ``(folder, campaign_id)`` is a campaign
    folder. An unclaimed folder, a campaign mid-rename, and every other
    subfolder are never scanned.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    dirs: list[tuple[Path, Optional[int]]] = [(output, None)]

    def collect(c: sqlite3.Connection) -> None:
        for r in c.execute(
            "SELECT id, folder FROM campaigns "
            "WHERE folder_claimed = 1 AND folder_pending IS NULL ORDER BY id"
        ):
            path = output / r["folder"]
            if path.is_dir():
                dirs.append((path, r["id"]))

    if conn is not None:
        collect(conn)
    else:
        with db.connection(data_dir) as c:
            collect(c)
    return dirs


def _registered_campaign_file(md: Path, output: Path, fold: bool,
                              campaign_paths: set[tuple[str, str]]) -> bool:
    """Whether ``md`` has a ``files`` row owned by a campaign (never a session)."""
    try:
        rel = nfc(db.to_rel(md, output))
    except ValueError:
        return False
    return ("output", file_registry._key(rel, fold)) in campaign_paths


def _transcript_files(dirs: list[tuple[Path, Optional[int]]], output: Path, fold: bool,
                      campaign_paths: set[tuple[str, str]]) -> dict[tuple[str, str], Path]:
    """``(dir key, NFC stem) -> path`` for every transcript ``.md`` on disk.

    Skips summaries, atomic-write temp files, a campaign folder's own journal,
    and any file registered to a campaign (a journal under its old name, or a
    campaign-level output). Keyed by resolved directory and NFC stem, not
    ``os.path.normcase`` (a no-op on macOS).
    """
    from .campaign_folders import combined_summary_name, journal_name

    found: dict[tuple[str, str], Path] = {}
    for directory, cid in dirs:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        journal_key = (file_registry._key(journal_name(directory.name), fold)
                       if cid is not None else None)
        digest_key = (file_registry._key(combined_summary_name(directory.name), fold)
                      if cid is not None else None)
        recap_prefix = f"{directory.name} Recap \u2014 " if cid is not None else None
        dir_key = file_registry._key(str(_resolved_dir(directory)), fold)
        for entry in entries:
            name = entry.name
            if name.startswith(TEMP_PREFIX) or not name.endswith(".md"):
                continue
            if name.endswith(".summary.md"):
                continue
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if journal_key is not None and file_registry._key(name, fold) == journal_key:
                continue
            # A campaign's combined summary and recaps are not sessions, even
            # before they are registered (a crash between write and register).
            if digest_key is not None and file_registry._key(name, fold) == digest_key:
                continue
            if recap_prefix is not None and name.startswith(recap_prefix):
                continue
            path = Path(entry.path)
            if _registered_campaign_file(path, output, fold, campaign_paths):
                continue
            found.setdefault((dir_key, nfc(path.stem)), path)
    return found


def _companion_stem(name: str) -> Optional[str]:
    """The transcript stem a pattern-identifiable output-root file belongs to.

    Covers summaries, sidecars, excerpt clips, ``.md.bak`` backups, and
    ``.flac`` audio.
    """
    if name.endswith(".summary.md"):
        return name[: -len(".summary.md")]
    if name.endswith("_diar.json"):
        return name[: -len("_diar.json")]
    if "_excerpt_" in name and name.endswith((".mp3", ".txt")):
        return name.rsplit("_excerpt_", 1)[0]
    if name.endswith(".md.bak"):
        return name[: -len(".md.bak")]
    if name.lower().endswith(".flac"):
        return name[: -len(".flac")]
    return None


def _dir_key(path: Path, fold: bool) -> str:
    return file_registry._key(str(_resolved_dir(path)), fold)


def reconcile(output_dir: Optional[Path] = None, data_dir: Optional[Path] = None,
              *, sweep: bool = False,
              sync: Literal["always", "throttled", "never"] = "always",
              blocking: bool = True) -> dict[str, int]:
    """Bring the registry in line with the ``.md`` files in the output root and
    each claimed campaign folder.

    - An unregistered ``.md`` gets a row in the folder's campaign.
    - A row whose ``.md`` is gone is flagged ``missing_since`` but kept, with
      its campaign position and companions: the file may come back (a sync,
      an unmounted drive). A reappearing file clears the flag.
    - On a case-insensitive filesystem, a ``.md`` whose name differs from a
      missing row's only in case renames that row, keeping its links.
    - An unregistered ``.md`` whose size and modified time equal those the
      registry last saw for exactly one row with no ``.md`` on disk is that
      transcript moved or renamed: the row takes the new stem and campaign.
    - A file whose target ``(campaign, stem)`` is already taken by a different
      present row, or a second file for a row whose own ``.md`` is registered
      elsewhere, is left unclaimed.

    Rows are never deleted here. A moved or renamed transcript's companion
    files follow it after the transaction commits. With ``sweep`` (server
    startup), also delete stale atomic-write temp files from every scanned
    directory; files with no transcript are never deleted, only listed
    (:func:`needs_attention`). Only cheap work: no file is parsed.

    ``_LOCATION_LOCK`` covers the scan and the write. With ``blocking=False``
    (page loads) the whole reconcile is skipped when it's held; startup and
    the CLI block. After the transaction closes, it moves companions, syncs
    the file registry (:func:`file_registry.sync`), then checks search
    freshness. A sync runs on every call with ``"always"``, at most every 30 s
    per directory pair with ``"throttled"``, never with ``"never"``.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    counts = {"added": 0, "missing": 0, "restored": 0, "renamed": 0, "swept": 0}
    if not output.is_dir():
        return counts  # output root absent: unavailable, never "deleted"
    if not _LOCATION_LOCK.acquire(blocking=blocking):
        return counts
    try:
        fold = _is_case_insensitive(output)
        dirs = transcript_dirs(data_dir=data_dir, output_dir=output)
        with db.connection(data_dir) as conn:
            campaigns = {
                r["id"]: (r["folder"], r["folder_pending"], bool(r["folder_claimed"]))
                for r in conn.execute(
                    "SELECT id, folder, folder_pending, folder_claimed FROM campaigns")
            }
            campaign_paths = {
                ("output", file_registry._key(r["rel_path"], fold))
                for r in conn.execute(
                    "SELECT rel_path FROM files WHERE root = 'output' AND campaign_id IS NOT NULL")
            }
        # The scanned directory named by a campaign, resolved; a campaign
        # whose folder isn't scanned (unclaimed or missing) gets its plain
        # folder path, which never matches a scanned file.
        dir_paths = {_dir_key(path, fold): path for path, _cid in dirs}
        pending_cids = {cid for cid, (_f, pending, _c) in campaigns.items() if pending is not None}
        files = _transcript_files(dirs, output, fold, campaign_paths)
        now = db.now_utc()

        def identity_dir(cid: Optional[int]) -> Path:
            if cid is None:
                return output
            folder = campaigns.get(cid, ("", None, False))[0]
            scanned = dir_paths.get(_dir_key(output / folder, fold))
            return scanned if scanned is not None else output / folder

        with db.connection(data_dir) as conn:
            transcripts = [dict(r) for r in conn.execute(
                "SELECT id, stem, campaign_id, missing_since FROM transcripts")]
            registered = {
                r["transcript_id"]: db.from_rel(r["rel_path"], output)
                for r in conn.execute(
                    "SELECT transcript_id, rel_path FROM files WHERE kind = 'transcript'")
            }
        # A row's identity is its registered `.md` when it has one (a
        # misplaced session keeps the location it actually has), else the
        # location its campaign assignment expects. Keyed by resolved dir
        # and NFC stem.
        rows: dict[tuple[str, str], dict] = {}
        for t in transcripts:
            if t["campaign_id"] in pending_cids:
                continue  # mid-rename: neither directory is scanned
            path = registered.get(t["id"])
            if path is not None:
                key = (_dir_key(path.parent, fold), nfc(path.stem))
            else:
                key = (_dir_key(identity_dir(t["campaign_id"]), fold), nfc(t["stem"]))
            rows.setdefault(key, t)

        # Stat unregistered files before the write transaction.
        new_by_stat: dict[tuple[int, int], list[tuple[str, str]]] = {}
        stat_of: dict[tuple[str, str], tuple[int, int]] = {}
        matched_tids: set[int] = set()
        for key, md in files.items():
            if key in rows:
                continue
            try:
                st = md.stat()
            except OSError:
                continue
            stat_of[key] = (st.st_size, st.st_mtime_ns)
            new_by_stat.setdefault(stat_of[key], []).append(key)

        def campaign_of_dir(key: tuple[str, str]) -> Optional[int]:
            for path, cid in dirs:
                if _dir_key(path, fold) == key[0]:
                    return cid
            return None

        pending_moves: list[tuple[int, str, str, Path, Path]] = []
        with db.transaction(data_dir) as conn:
            # Another process may have renamed a folder between our scan and
            # now; skip this pass so no row is matched or flagged against it.
            state_now = {
                r["id"]: (r["folder"], r["folder_pending"], bool(r["folder_claimed"]))
                for r in conn.execute(
                    "SELECT id, folder, folder_pending, folder_claimed FROM campaigns")
            }
            if state_now != campaigns:
                return counts

            missing_by_case = {
                (k[0], file_registry._key(k[1], fold)): k
                for k in rows if k not in files
            } if fold else {}

            # A registered `.md` is never a new session, whatever its row's
            # campaign (a mid-rename campaign's misplaced sessions sit in the
            # root). The path map covers rows whose identity dir we skipped.
            registered_path_keys = {
                (_dir_key(p.parent, fold), nfc(p.stem)) for p in registered.values()
            }

            def registered_md(tid: int) -> Optional[Path]:
                return registered.get(tid)

            for key, md in files.items():
                # A row whose registered .md is this file is that row: a
                # misplaced session, or a session whose campaign is unclaimed.
                existing = rows.get(key)
                if existing is not None:
                    matched_tids.add(existing["id"])
                    # The registry wins on a name/file disagreement: the file's
                    # basename is the stem, unless that name is taken.
                    if key[1] != nfc(existing["stem"]):
                        try:
                            conn.execute("SAVEPOINT rename")
                            conn.execute("UPDATE transcripts SET stem = ?, missing_since = NULL "
                                         "WHERE id = ?", (key[1], existing["id"]))
                            mark_stale(conn, [existing["id"]])
                            conn.execute("RELEASE rename")
                            counts["renamed"] += 1
                        except sqlite3.IntegrityError:
                            conn.execute("ROLLBACK TO rename")
                            conn.execute("RELEASE rename")
                    elif existing["missing_since"] is not None:
                        conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?",
                                     (existing["id"],))
                        counts["restored"] += 1
                    continue

                claimed_key = key in registered_path_keys
                twin_key = missing_by_case.pop((key[0], file_registry._key(key[1], fold)), None) if fold else None
                if twin_key is not None:
                    twin = rows[twin_key]
                    old_path = registered_md(twin["id"]) or (md.parent / f"{twin['stem']}.md")
                    try:
                        conn.execute("SAVEPOINT m")
                        conn.execute(
                            "UPDATE transcripts SET stem = ?, missing_since = NULL WHERE id = ?",
                            (key[1], twin["id"]))
                        mark_stale(conn, [twin["id"]])
                        _point_transcript_row(conn, twin["id"], md, data_dir, output)
                        conn.execute("RELEASE m")
                    except sqlite3.IntegrityError:
                        conn.execute("ROLLBACK TO m")
                        conn.execute("RELEASE m")
                        continue
                    pending_moves.append((twin["id"], twin["stem"], key[1],
                                          old_path.parent, md.parent))
                    matched_tids.add(twin["id"])
                    counts["renamed"] += 1
                    continue

                if claimed_key:
                    continue  # already some row's `.md`: never a new session
                cid = campaign_of_dir(key)
                matched = _match_renamed(conn, md, key[1], stat_of.get(key),
                                         new_by_stat, registered, files, dirs, output, fold, data_dir)
                if matched is not None:
                    tid, old_stem, old_dir = matched
                    pending_moves.append((tid, old_stem, key[1], old_dir, md.parent))
                    matched_tids.add(tid)
                    counts["renamed"] += 1
                    continue
                same = _stem_row(conn, cid, key[1])
                if same is not None:
                    other = registered_md(same[0])
                    if other is not None and other.is_file():
                        continue  # a misplaced session's newcomer: unclaimed
                    # The row's .md is gone from where it was: this file is it.
                    try:
                        conn.execute("SAVEPOINT claim")
                        conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?",
                                     (same[0],))
                        _point_transcript_row(conn, same[0], md, data_dir, output)
                        conn.execute("RELEASE claim")
                    except sqlite3.IntegrityError:
                        conn.execute("ROLLBACK TO claim")
                        conn.execute("RELEASE claim")
                        continue
                    if other is not None and _dir_key(other.parent, fold) != key[0]:
                        pending_moves.append((same[0], key[1], key[1], other.parent, md.parent))
                    matched_tids.add(same[0])
                    counts["restored"] += 1
                    continue
                try:
                    conn.execute("SAVEPOINT ins")
                    ensure_row(conn, key[1], output, campaign_id=cid)
                    conn.execute("RELEASE ins")
                    counts["added"] += 1
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK TO ins")
                    conn.execute("RELEASE ins")
                    # A row of this campaign already holds the name (a missing
                    # session, or one this pass didn't match): the file is
                    # unclaimed rather than a second row.
                    continue

            for key, row in rows.items():
                if row["id"] in matched_tids or key in files:
                    continue
                if row["campaign_id"] in pending_cids:
                    continue
                if row["missing_since"] is None:
                    conn.execute("UPDATE transcripts SET missing_since = ? WHERE id = ?",
                                 (now, row["id"]))
                    counts["missing"] += 1
    finally:
        _LOCATION_LOCK.release()

    if sweep:
        cutoff = time.time() - _TEMP_MAX_AGE_S
        for directory, _cid in dirs:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.name.startswith(TEMP_PREFIX) and entry.stat().st_mtime < cutoff:
                        Path(entry.path).unlink()
                        counts["swept"] += 1
                except OSError:
                    pass
    for tid, old_stem, new_stem, old_dir, new_dir in pending_moves:
        dst = new_dir if _dir_key(old_dir, fold) != _dir_key(new_dir, fold) else None
        kept = rename_companions(tid, old_stem, new_stem, src_dir=old_dir, dst_dir=dst,
                                 output_dir=output, data_dir=data_dir)
        if kept:
            log.warning("%d file(s) of %r kept their old names: a file with the new name exists",
                        len(kept), new_stem)
    if sync == "always":
        file_registry.sync(output, data_dir)
    elif sync == "throttled":
        file_registry.sync_if_due(output, data_dir, force=bool(pending_moves))
    if any(counts.values()):
        log.info("Transcript reconcile: %s", counts)
    # Files edited outside wisper get reindexed; new ones get their first index.
    try:
        check_freshness(output, data_dir)
    except Exception:
        log.warning("Search freshness check failed", exc_info=True)
    if counts["added"] or counts["renamed"] or counts["restored"]:
        request_backfill()
    return counts


def _match_renamed(conn: sqlite3.Connection, md: Path, stem: str,
                   key: Optional[tuple[int, int]],
                   new_by_stat: dict[tuple[int, int], list[tuple[str, str]]],
                   registered: dict[int, Path],
                   files: dict[tuple[str, str], Path],
                   dirs: list[tuple[Path, Optional[int]]],
                   output: Path, fold: bool, data_dir: Optional[Path],
                   ) -> Optional[tuple[int, str, Path]]:
    """The transcript ``md`` is the moved/renamed file of, if provably one.

    Provable means: no other unregistered file has the same size and modified
    time, and exactly one row with no ``.md`` on disk last saw those stats. On
    a match the row takes the stem and the scanned directory's campaign, its
    ``transcript`` file row is re-pointed, and ``(id, old_stem, old_dir)`` is
    returned; a target name another present row already holds, or any
    integrity failure, leaves the file unclaimed (returns None).
    """
    if key is None or len(new_by_stat.get(key, ())) != 1:
        return None
    new_dir_key = _dir_key(md.parent, fold)
    new_cid = next((cid for path, cid in dirs if _dir_key(path, fold) == new_dir_key), None)
    candidates: list[sqlite3.Row] = []
    for r in conn.execute(
        "SELECT t.id, t.stem, t.campaign_id, f.size, f.mtime_ns FROM transcripts t "
        "JOIN files f ON f.transcript_id = t.id AND f.kind = 'transcript' "
        "WHERE f.size IS NOT NULL AND f.mtime_ns IS NOT NULL"
    ):
        if (r["size"], r["mtime_ns"]) != key:
            continue
        old_md = registered.get(r["id"])
        if old_md is not None and (_dir_key(old_md.parent, fold), nfc(old_md.stem)) in files:
            continue  # it still has an .md on disk
        candidates.append(r)
    if len(candidates) != 1:
        return None
    cand = candidates[0]
    tid = cand["id"]

    # A rename within one directory keeps the transcript's campaign; only a
    # different directory moves it.
    old_md = registered.get(tid)
    old_dir = old_md.parent if old_md is not None else output
    same_dir = _dir_key(old_dir, fold) == new_dir_key
    effective_cid = cand["campaign_id"] if same_dir else new_cid

    # Target taken: another row of the target campaign holds the name
    # (casefolded where the filesystem ignores case), present or not.
    want = file_registry._key(stem, fold)
    if effective_cid is None:
        others = conn.execute(
            "SELECT id, stem FROM transcripts WHERE id <> ? AND campaign_id IS NULL", (tid,))
    else:
        others = conn.execute(
            "SELECT id, stem FROM transcripts WHERE id <> ? AND campaign_id = ?",
            (tid, effective_cid))
    if any(file_registry._key(nfc(o["stem"]), fold) == want for o in others):
        return None
    owner = file_registry.Owner("transcript", tid)
    file_row = file_registry.file_for(owner, "transcript", conn=conn,
                                      data_dir=data_dir, output_dir=output)
    try:  # SAVEPOINT so a failure undoes the re-point too
        conn.execute("SAVEPOINT move")
        if file_row is not None:
            file_registry.repoint(file_row, md, conn=conn, data_dir=data_dir, output_dir=output)
        conn.execute(
            "UPDATE transcripts SET stem = ?, campaign_id = ?, missing_since = NULL, "
            "position = CASE WHEN ? IS NULL THEN NULL "
            "WHEN campaign_id = ? THEN position "
            "ELSE (SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?) END "
            "WHERE id = ?",
            (stem, effective_cid, effective_cid, effective_cid, effective_cid, tid),
        )
        conn.execute("RELEASE move")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO move")
        conn.execute("RELEASE move")
        log.warning("Could not match %s to a missing transcript", md.name, exc_info=True)
        return None
    return tid, cand["stem"], old_dir


def _point_transcript_row(conn: sqlite3.Connection, tid: int, md: Path,
                          data_dir: Optional[Path], output_dir: Path) -> None:
    """Make the ``transcript`` file row of transcript ``tid`` name ``md``.

    A transcript that went missing before its first sync has no row yet, so
    it is registered instead.
    """
    owner = file_registry.Owner("transcript", tid)
    row = file_registry.file_for(owner, "transcript", conn=conn,
                                 data_dir=data_dir, output_dir=output_dir)
    try:
        if row is not None:
            file_registry.repoint(row, md, conn=conn, data_dir=data_dir, output_dir=output_dir)
            return
    except (ValueError, sqlite3.Error, OSError):
        log.warning("Could not re-point the file record to %s", md.name, exc_info=True)
        return
    file_registry.add_if_owned(md, kind="transcript", owner=owner, conn=conn,
                               data_dir=data_dir, output_dir=output_dir)


def rename_companions(transcript_id: int, old_stem: str, new_stem: str, *,
                      src_dir: Path, dst_dir: Optional[Path] = None,
                      output_dir: Optional[Path] = None,
                      data_dir: Optional[Path] = None) -> list[Path]:
    """Rename or move a transcript's other files from ``old_stem`` to ``new_stem``.

    Call after the ``stem`` change has committed (companion files follow the
    row), with the stem captured before it. ``src_dir`` is where the old files
    are; with ``dst_dir`` they also move there (a campaign change), otherwise
    they are renamed in place. The files are the transcript's ``files`` rows
    except the ``.md`` itself, plus any summary, sidecar, excerpt, or backup of
    ``old_stem`` found in ``src_dir`` that isn't registered yet (registered
    first). An unregistered ``.flac`` is never claimed: it may be the user's
    own. With a ``dst_dir``, every other registered row of the transcript also
    moves, keeping its file name (a partial rename's leftovers). ``output_dir``
    is the output root, for ``file_registry``.

    A file's new name replaces the ``old_stem`` prefix and keeps the rest
    (``_excerpt_SPEAKER_00.mp3``, ``.summary.md``, ``_1.flac``). Returns the
    files that kept their old name because the new name is taken or the move
    failed (e.g. held open on Windows); their rows are unchanged.
    """
    output_dir = Path(output_dir) if output_dir is not None else get_output_root()
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir) if dst_dir is not None else None
    old_stem, new_stem = nfc(old_stem), nfc(new_stem)
    if old_stem == new_stem and dst_dir is None:
        return []
    fold = file_registry._fold(output_dir)
    owner = file_registry.Owner("transcript", transcript_id)
    target_dir = dst_dir or src_dir

    def is_old(name: str) -> bool:
        return nfc(name)[:len(old_stem)].casefold() == old_stem.casefold() if fold \
            else nfc(name).startswith(old_stem)

    rows = file_registry.files_for(owner, data_dir=data_dir, output_dir=output_dir)
    taken = {(r.kind, r.label or "") for r in rows}
    try:
        names = sorted(e.name for e in os.scandir(src_dir) if e.is_file())
    except OSError:
        names = []
    registered = False
    for name in names:
        if name.startswith(TEMP_PREFIX) or not is_old(name):
            continue
        for kind, stem, label in file_registry._output_candidates(name):
            if kind in ("transcript", "audio") or file_registry._key(stem, fold) != \
                    file_registry._key(old_stem, fold):
                continue
            if (kind, label or "") not in taken:
                taken.add((kind, label or ""))
                file_registry.add_if_owned(src_dir / name, kind=kind, owner=owner, label=label,
                                           data_dir=data_dir, output_dir=output_dir)
                registered = True
            break
    if registered:
        rows = file_registry.files_for(owner, data_dir=data_dir, output_dir=output_dir)

    kept: list[Path] = []
    for row in rows:
        if row.kind == "transcript" or row.root != "output":
            continue
        if is_old(row.path.name):
            tail = nfc(row.path.name)[len(old_stem):]
            target = target_dir / (new_stem + tail)
        elif dst_dir is not None:
            target = dst_dir / row.path.name
        else:
            continue
        # A companion the user already moved along with the .md is re-pointed,
        # not moved (its source is gone and its target is already there).
        if dst_dir is not None and not os.path.lexists(row.path) and os.path.lexists(target):
            try:
                file_registry.repoint(row, target, data_dir=data_dir, output_dir=output_dir)
            except (ValueError, sqlite3.Error, OSError):
                pass
            continue
        result = file_registry.move(row, target, data_dir=data_dir, output_dir=output_dir)
        if result in ("conflict", "error"):
            kept.append(row.path)
    return kept


def relink(old_id: int, new_md: Path, data_dir: Optional[Path] = None) -> list[Path]:
    """Give a missing transcript's identity to a file under a new name.

    ``old_id`` must be flagged missing; ``new_md`` must exist and its stem must
    not be linked to anything yet (no campaign, no journal entry). The old row
    takes the new name, so its campaign position, journal entry, and speakers
    are kept; the new name's own row (reconcile may have created one) is
    removed. The old name's companion files are renamed to match
    (:func:`rename_companions`); returns the ones that couldn't be.

    Raises ValueError (not missing, or target already linked) or KeyError (no
    such file or transcript).
    """
    new_md = Path(new_md)
    output = get_output_root()
    if not new_md.is_file():
        raise KeyError(f"No transcript file named {new_md.stem!r}")
    new_stem = nfc(new_md.stem)
    new_dir = new_md.parent
    old_loc = locate(old_id, data_dir=data_dir)
    if old_loc is None:
        raise KeyError(f"No transcript with id {old_id}")
    if not old_loc.missing:
        raise ValueError(f"{old_loc.stem!r} is not missing")
    with db.transaction(data_dir) as conn:
        old = conn.execute("SELECT stem FROM transcripts WHERE id = ?", (old_id,)).fetchone()
        rows = conn.execute(
            "SELECT id FROM transcripts WHERE stem = ? LIMIT 2", (new_stem,)
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"more than one session is named {new_stem!r}")
        if rows:
            linked = conn.execute(
                "SELECT 1 FROM transcripts WHERE id = ? AND campaign_id IS NOT NULL "
                "UNION SELECT 1 FROM journal_entries WHERE transcript_id = ?",
                (rows[0][0], rows[0][0]),
            ).fetchone()
            if linked:
                raise ValueError(f"{new_stem!r} already belongs to a campaign")
            conn.execute("DELETE FROM transcripts WHERE id = ?", (rows[0][0],))
        conn.execute("UPDATE transcripts SET stem = ?, missing_since = NULL WHERE id = ?",
                     (new_stem, old_id))
        _point_transcript_row(conn, old_id, new_md, data_dir, output)
    kept = rename_companions(old_id, old["stem"], new_stem, src_dir=old_loc.dir,
                             dst_dir=new_md.parent, output_dir=output, data_dir=data_dir)
    _reindex(old_id, data_dir)  # the old identity's index described the old file
    return kept


def relink_candidates(data_dir: Optional[Path] = None,
                      output_dir: Optional[Path] = None) -> list["Located"]:
    """Present transcripts not linked to any campaign, newest first — the
    files a missing campaign entry can be relinked to."""
    with db.connection(data_dir) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT t.id FROM transcripts t WHERE t.missing_since IS NULL "
            "AND t.campaign_id IS NULL "
            "ORDER BY t.created_at DESC, t.stem"
        )]
        return [loc for loc in
                (locate(tid, conn=conn, data_dir=data_dir, output_dir=output_dir) for tid in ids)
                if loc is not None]


# ---------------------------------------------------------------------------
# Needs attention: what wisper can't resolve on its own
# ---------------------------------------------------------------------------

@dataclass
class MissingTranscript:
    id: int
    stem: str
    campaign: Optional[str]   # the campaign's display name


@dataclass
class PendingFolderRename:
    """A campaign folder rename that didn't finish (a file open, or a crash)."""

    slug: str
    campaign: str        # the campaign's display name
    folder: str          # the current folder
    pending: str         # the target folder
    neither: bool = False  # both directories are gone: Finish without folder


@dataclass
class Attention:
    """Everything the user has to resolve; nothing here is deleted automatically."""

    missing_transcripts: list[MissingTranscript] = field(default_factory=list)
    missing_files: list[file_registry.FileRow] = field(default_factory=list)
    unclaimed: list[Path] = field(default_factory=list)
    misplaced: list["Located"] = field(default_factory=list)
    pending_folders: list[PendingFolderRename] = field(default_factory=list)
    folder_taken: list[tuple[int, str, str]] = field(default_factory=list)
    legacy_journals: list[str] = field(default_factory=list)
    missing_folders: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (len(self.missing_transcripts) + len(self.missing_files) + len(self.unclaimed)
                + len(self.misplaced) + len(self.pending_folders) + len(self.folder_taken)
                + len(self.legacy_journals) + len(self.missing_folders))


def needs_attention(output_dir: Optional[Path] = None,
                    data_dir: Optional[Path] = None,
                    report: Optional[file_registry.SyncReport] = None) -> Attention:
    """The transcripts and files that need the user's decision.

    - missing transcripts: rows flagged ``missing_since``;
    - missing files: registered companions, clips, or journals whose file is gone;
    - unclaimed files: pattern-matching files with no owner;
    - misplaced: present transcripts not in the folder their campaign names;
    - pending folder renames, taken folders, unadoptable legacy journals, and
      claimed folders gone from disk.

    The files come from ``report`` if given, else the latest
    :func:`file_registry.sync` report (taken on demand if there is none), and
    are re-checked against the disk.
    """
    from . import campaign_folders
    from .journal import legacy_journal_path

    if output_dir is None:
        output_dir = get_output_root()
    if report is None:
        report = file_registry.last_report(data_dir, output_dir)
    if report is None:
        report = file_registry.sync(output_dir, data_dir)
    root_present = output_dir.is_dir()
    with db.connection(data_dir) as conn:
        missing = [MissingTranscript(r["id"], r["stem"], r["display_name"]) for r in conn.execute(
            "SELECT t.id, t.stem, c.display_name FROM transcripts t "
            "LEFT JOIN campaigns c ON c.id = t.campaign_id "
            "WHERE t.missing_since IS NOT NULL ORDER BY t.stem")]
        campaigns = [dict(r) for r in conn.execute(
            "SELECT id, slug, display_name, folder, folder_pending, folder_claimed "
            "FROM campaigns ORDER BY id")]
        present_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transcripts WHERE missing_since IS NULL ORDER BY id")]

    misplaced = [loc for loc in (locate(tid, data_dir=data_dir, output_dir=output_dir)
                                 for tid in present_ids)
                 if loc is not None and loc.misplaced]

    pending_folders: list[PendingFolderRename] = []
    folder_taken: list[tuple[int, str, str]] = []
    legacy_journals: list[str] = []
    missing_folders: list[tuple[int, str, str]] = []
    for c in campaigns:
        folder = c["folder"]
        pending = c["folder_pending"]
        claimed = bool(c["folder_claimed"])
        path = output_dir / folder
        if pending is not None:
            pending_folders.append(PendingFolderRename(
                slug=c["slug"], campaign=c["display_name"], folder=folder, pending=pending,
                neither=root_present and not path.is_dir()
                and not (output_dir / pending).is_dir(),
            ))
            continue
        if claimed:
            if root_present and not path.is_dir():
                missing_folders.append((c["id"], c["display_name"], folder))
        elif path.is_dir() and not campaign_folders.holds_only_wisper(path, folder):
            folder_taken.append((c["id"], c["display_name"], folder))
        if legacy_journal_path(c["slug"], data_dir).is_file():
            legacy_journals.append(c["display_name"])

    return Attention(
        missing_transcripts=missing,
        missing_files=[r for r in report.missing if not os.path.lexists(r.path)],
        unclaimed=[p for p in report.unclaimed if os.path.isfile(p)],
        misplaced=misplaced,
        pending_folders=pending_folders,
        folder_taken=folder_taken,
        legacy_journals=legacy_journals,
        missing_folders=missing_folders,
    )


def delete_unowned_file(path: Path, output_dir: Optional[Path] = None,
                        data_dir: Optional[Path] = None) -> bool:
    """Delete a regular file in a scanned transcript directory that no ``files``
    row names.

    Returns False, deleting nothing, for anything else: a path outside the
    output root or below one folder depth, a directory or symlink, a registered
    file, or a file that couldn't be removed. Callers decide which names are
    eligible.
    """
    if output_dir is None:
        output_dir = get_output_root()
    base = os.path.abspath(str(output_dir))
    if not base.endswith(os.sep):
        base += os.sep
    target = os.path.abspath(str(path))
    if not target.startswith(base):
        return False
    parts = target[len(base):].split(os.sep)
    if len(parts) not in (1, 2) or any(not p for p in parts):
        return False  # the root or exactly one folder, never deeper
    if os.path.islink(target) or not os.path.isfile(target):
        return False
    parent = Path(target).parent
    if not any(_dirs_equal(parent, directory, file_registry._fold(output_dir))
               for directory, _cid in transcript_dirs(data_dir=data_dir, output_dir=output_dir)):
        return False
    if file_registry.is_registered(Path(target), data_dir=data_dir, output_dir=output_dir):
        return False
    if file_registry.unlink_paths([Path(target)]):
        return False
    file_registry.drop_from_report(data_dir, output_dir, paths=[Path(target)])
    return True


# ---------------------------------------------------------------------------
# Diarization sidecar: speakers in the DB, segments in <stem>_diar.json
# ---------------------------------------------------------------------------

_SPEAKER_FIELDS = ("speaker_map", "speaker_map_source", "speaker_embeddings",
                   "embedding_space", "input_path", "campaign")


def derived_source(name: str) -> str:
    """Provenance for a label with none recorded (sidecars before
    ``speaker_map_source``): pipeline-shaped names were automatic, anything
    else may have been typed by the user."""
    from .web.enroll_shared import AUTO_NAME_RE
    return "auto" if AUTO_NAME_RE.match(str(name)) else "manual"


def read_sidecar(md_path: Path, data_dir: Optional[Path] = None) -> Optional[dict]:
    """The transcript's diarization data as one dict (the sidecar shape), or None.

    ``diarization_segments`` comes from ``<stem>_diar.json`` (its only
    content); ``speaker_map``, ``speaker_map_source``, ``speaker_embeddings`` +
    ``embedding_space``, ``input_path`` (:func:`audio_path`), and ``campaign``
    come from the database. A sidecar still carrying
    the old fields (e.g. synced from an older install) is used as a fallback
    only when the database has nothing for the transcript.
    """
    import numpy as np

    md_path = Path(md_path)
    path = md_path.with_name(md_path.stem + SIDECAR_SUFFIX)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    diar: dict = {"diarization_segments": raw.get("diarization_segments") or [], "input_path": ""}
    for key in _SPEAKER_FIELDS:
        if key in raw:
            diar[key] = raw[key]

    with db.connection(data_dir) as conn:
        loc = locate_path(md_path, conn=conn, data_dir=data_dir)
        if loc is None:
            return diar
        speakers = conn.execute(
            "SELECT label, display_name, source, embedding, embedding_space "
            "FROM transcript_speakers WHERE transcript_id = ? ORDER BY label",
            (loc.id,),
        ).fetchall()
        audio = audio_path(md_path, conn=conn, data_dir=data_dir)
        owner = file_registry.Owner("transcript", loc.id)
        has_audio_row = file_registry.file_for(
            owner, "audio", conn=conn, data_dir=data_dir) is not None
        slug = conn.execute("SELECT slug FROM campaigns WHERE id = ?",
                            (loc.campaign_id,)).fetchone() if loc.campaign_id is not None else None
    diar["campaign"] = slug[0] if slug is not None else None
    if audio is not None:
        diar["input_path"] = str(audio)
    if not speakers and not has_audio_row:
        return diar  # nothing stored yet: keep any legacy file fields
    diar["speaker_map"] = {s["label"]: s["display_name"] for s in speakers}
    diar["speaker_map_source"] = {s["label"]: s["source"] for s in speakers}
    embedded = [s for s in speakers if s["embedding"] is not None]
    diar.pop("speaker_embeddings", None)
    diar.pop("embedding_space", None)
    if embedded:
        diar["embedding_space"] = embedded[0]["embedding_space"]
        diar["speaker_embeddings"] = {
            s["label"]: np.frombuffer(s["embedding"], dtype=np.float32).tolist()
            for s in embedded if s["embedding_space"] == diar["embedding_space"]
        }
    diar["input_path"] = str(audio) if audio is not None else ""
    return diar


def audio_path(md_path: Path, conn: Optional[sqlite3.Connection] = None,
               data_dir: Optional[Path] = None,
               output_dir: Optional[Path] = None) -> Optional[Path]:
    """Where a transcript's audio is, or None.

    The transcript's ``audio`` file if it exists; else, for a transcript made
    from a recording, that recording's combined track (``combined.flac``, or a
    legacy ``combined.wav``) if it exists. The recording's file is never a
    transcript's own, so deleting the transcript leaves it.
    """
    md_path = Path(md_path)
    if conn is None:
        with db.connection(data_dir) as opened:
            return _audio_for(md_path, opened, data_dir, output_dir)
    return _audio_for(md_path, conn, data_dir, output_dir)


def _audio_for(md_path: Path, conn: sqlite3.Connection, data_dir: Optional[Path],
               output_dir: Optional[Path]) -> Optional[Path]:
    loc = locate_path(md_path, conn=conn, data_dir=data_dir, output_dir=output_dir)
    if loc is None:
        return None
    audio = loc.companions.get(("audio", ""))
    if audio is not None and audio.is_file():
        return audio
    rec = conn.execute("SELECT id FROM recordings WHERE transcript_id = ?",
                       (loc.id,)).fetchone()
    if rec is not None:
        from .recording_manager import existing_combined_path
        combined = existing_combined_path(rec["id"], data_dir)
        if combined is not None:
            return combined
    return None


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def set_audio(md_path: Path, path: Optional[Path], *, data_dir: Optional[Path] = None,
              output_dir: Optional[Path] = None) -> None:
    """Record ``path`` as the transcript's ``audio`` file, or clear it (None).

    A different file replaced by this call is deleted after the commit, so
    call it after your own transaction. Raises ``ValueError`` for a path
    outside the output root or under ``<data>/recordings/``: a recording's
    audio belongs to the recording. Does nothing when the ``.md`` isn't in a
    transcript folder.
    """
    md_path = Path(md_path)
    output = Path(output_dir) if output_dir is not None else get_output_root()
    with db.transaction(data_dir) as conn:
        tid = _row_for_path(conn, md_path, data_dir=data_dir, output_dir=output)
        if tid is None:
            log.warning("Not recording audio for %s: it is not in a transcript folder", md_path.name)
            return
        owner = file_registry.Owner("transcript", tid)
        if path is None:
            replaced = file_registry.forget_kind(owner, "audio", conn=conn,
                                                 data_dir=data_dir, output_dir=output)
        else:
            old = file_registry.add(Path(path), kind="audio", owner=owner, conn=conn,
                                    data_dir=data_dir, output_dir=output)
            replaced = [old] if old is not None else []
    for old in replaced:
        if path is not None and _same_file(old, Path(path)):
            continue
        try:
            if old.is_file() and not old.name.endswith(".md"):
                old.unlink()
        except OSError:
            pass


def write_sidecar(md_path: Path, diar: dict, data_dir: Optional[Path] = None) -> None:
    """Store a transcript's diarization data (the sidecar-shaped dict).

    Speakers (name, provenance, embedding) go to the database in one
    transaction; the segments go to ``<stem>_diar.json`` after it (companion
    files follow the row), and the sidecar is registered. The ``campaign``
    key is ignored: the campaign is the transcript's own
    ``campaign_id``. ``input_path`` becomes the transcript's ``audio`` file when it lies
    inside the ``.md``'s folder (:func:`set_audio`); any other value clears
    it, and an audio copy replaced by a different one (re-transcribe) is
    deleted. Does nothing when the ``.md`` isn't in a transcript folder.
    """
    import numpy as np

    md_path = Path(md_path)
    output = get_output_root()
    speaker_map = {str(k): str(v) for k, v in (diar.get("speaker_map") or {}).items()}
    sources = dict(diar.get("speaker_map_source") or {})
    space = diar.get("embedding_space")
    embeddings = {}
    if space and isinstance(diar.get("speaker_embeddings"), dict):
        for label, vec in diar["speaker_embeddings"].items():
            arr = np.asarray(vec, dtype=np.float32).reshape(-1)
            if arr.size:
                embeddings[str(label)] = arr
    audio: Optional[Path] = None
    if diar.get("input_path"):
        # The audio copy must sit in the .md's own folder; the registry root is separate.
        loc_for_audio = locate_path(md_path, data_dir=data_dir, output_dir=output)
        guard = loc_for_audio.dir if loc_for_audio is not None else md_path.parent
        try:
            db.to_rel(Path(diar["input_path"]), guard)
            audio = Path(diar["input_path"])
        except ValueError:
            audio = None  # outside the .md's folder: not ours to track

    with db.transaction(data_dir) as conn:
        tid = _row_for_path(conn, md_path, data_dir=data_dir, output_dir=output)
        if tid is None:
            log.warning("Not storing speaker data for %s: it is not in a transcript folder",
                        md_path.name)
            return
        conn.execute("DELETE FROM transcript_speakers WHERE transcript_id = ?", (tid,))
        for label in sorted(set(speaker_map) | set(embeddings)):
            name = speaker_map.get(label, label)
            source = sources.get(label)
            if source not in ("auto", "manual"):
                source = derived_source(name)
            emb = embeddings.get(label)
            conn.execute(
                "INSERT INTO transcript_speakers (transcript_id, label, display_name, source, "
                "embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?)",
                (tid, label, name, source,
                 None if emb is None else emb.tobytes(), None if emb is None else space),
            )

    sidecar = md_path.with_name(md_path.stem + SIDECAR_SUFFIX)
    atomic_write_text(
        sidecar,
        json.dumps({"diarization_segments": diar.get("diarization_segments") or []}, indent=2),
    )
    file_registry.add_if_owned(sidecar, kind="sidecar", owner=file_registry.Owner("transcript", tid),
                               data_dir=data_dir, output_dir=output)
    try:
        set_audio(md_path, audio, data_dir=data_dir, output_dir=output)
    except (ValueError, sqlite3.Error, file_registry.OwnershipConflict, OSError) as exc:
        # An input that can't be registered (a recording's audio, a name the
        # schema rejects) is left untracked rather than failing the write.
        log.warning("Audio for %s not recorded: %s", md_path.name, exc)


def set_speaker_names(md_path: Path, names: dict[str, str], sources: dict[str, str],
                      data_dir: Optional[Path] = None) -> None:
    """Record each label's current display name and provenance (wizard renames).

    Only the given labels' names and sources change; embeddings, the audio
    path, and the segments file are left alone. A label with no row yet gets
    one without an embedding. A missing or invalid source is derived from the
    name (see :func:`derived_source`). Does nothing when the ``.md`` isn't in
    a transcript folder.
    """
    md_path = Path(md_path)
    with db.transaction(data_dir) as conn:
        tid = _row_for_path(conn, md_path)
        if tid is None:
            log.warning("Not recording speaker names for %s: it is not in a transcript folder",
                        md_path.name)
            return
        for label, name in names.items():
            source = sources.get(label)
            if source not in ("auto", "manual"):
                source = derived_source(name)
            conn.execute(
                "INSERT INTO transcript_speakers (transcript_id, label, display_name, source) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (transcript_id, label) DO UPDATE SET "
                "display_name = excluded.display_name, source = excluded.source",
                (tid, str(label), str(name), source),
            )


def set_speaker_embeddings(md_path: Path, embeddings: dict, space: str,
                           data_dir: Optional[Path] = None) -> None:
    """Store per-label voice embeddings (the campaign relabel backfill).

    Names are kept; a label with no row yet is named after itself. Does
    nothing when the ``.md`` isn't in a transcript folder.
    """
    import numpy as np

    md_path = Path(md_path)
    with db.transaction(data_dir) as conn:
        tid = _row_for_path(conn, md_path)
        if tid is None:
            log.warning("Not storing embeddings for %s: it is not in a transcript folder",
                        md_path.name)
            return
        for label, vec in embeddings.items():
            blob = np.asarray(vec, dtype=np.float32).reshape(-1).tobytes()
            conn.execute(
                "INSERT INTO transcript_speakers (transcript_id, label, display_name, source, "
                "embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (transcript_id, label) DO UPDATE SET "
                "embedding = excluded.embedding, embedding_space = excluded.embedding_space",
                (tid, str(label), str(label), derived_source(str(label)), blob, space),
            )
