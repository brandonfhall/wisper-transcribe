"""The ``files`` registry: every file wisper owns is a row.

A row records a file's owner (a transcript, recording, profile, or campaign),
its kind, where it lives, and its size and modified time as last observed.
Renames, moves, deletes, and cleanup act on the rows, so they don't depend
on matching names on disk. This module is the only code that writes ``files``
(apart from the schema's v9 import).

Rules that hold throughout:

- Paths are stored relative to a root: ``output`` for a transcript's files,
  ``data`` for everything else. The default output root sits inside the data
  dir, so the path alone can't say which root a row belongs to.
- ``rel_path`` keeps the on-disk spelling. Lookups compare NFC, and casefolded
  where the filesystem ignores case (the schema has no ``COLLATE NOCASE``: it
  folds ASCII only and is wrong on case-sensitive mounts).
- Functions that touch only rows take ``conn``; with one they run in the
  caller's transaction, without one they open their own. Functions that move
  or delete files (``move``, ``unlink_paths``) take no ``conn``: files change
  only after the owner's transaction commits.
- On Windows a file another program holds open can't be moved or deleted.
  Those failures are returned or reported, and the row is left matching the
  disk.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat as stat_mod
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional

from . import db

log = logging.getLogger(__name__)

KINDS = (
    "transcript", "summary", "sidecar", "excerpt", "excerpt_text", "audio",
    "backup", "combined", "per_user", "live_draft", "reference_clip", "journal",
)
_OUTPUT_KINDS = frozenset(
    {"transcript", "summary", "sidecar", "excerpt", "excerpt_text", "audio", "backup"}
)
ROOT_OF_KIND: dict[str, str] = {
    k: ("output" if k in _OUTPUT_KINDS else "data") for k in KINDS
}
# Kinds with one file per owner; the rest are unique per (owner, label).
SINGLE_KINDS = frozenset(set(KINDS) - {"excerpt", "excerpt_text", "per_user"})

OwnerKind = Literal["transcript", "recording", "profile", "campaign"]
MoveResult = Literal["moved", "conflict", "missing", "error"]

_OWNER_COLUMN: dict[str, str] = {
    "transcript": "transcript_id", "recording": "recording_id",
    "profile": "profile_id", "campaign": "campaign_id",
}
_OWNER_OF_KIND: dict[str, str] = {
    **{k: "transcript" for k in _OUTPUT_KINDS},
    "combined": "recording", "per_user": "recording", "live_draft": "recording",
    "reference_clip": "profile", "journal": "campaign",
}
# Recordings in these states are still being written.
_ACTIVE_CAPTURE = ("recording", "degraded")
SYNC_INTERVAL_S = 30.0
_SYNC_BATCH = 50


class OwnershipConflict(Exception):
    """A path or a (owner, kind, label) slot already belongs to something else."""


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _key(text: str, fold: bool) -> str:
    text = _nfc(text)
    return text.casefold() if fold else text


def _fold(directory: Path) -> bool:
    """Whether names differing only in case collide in ``directory``."""
    from .transcript_store import _is_case_insensitive
    try:
        return _is_case_insensitive(directory)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Connections and directories
# ---------------------------------------------------------------------------

@contextmanager
def _use(conn: Optional[sqlite3.Connection], data_dir: Optional[Path], *,
         write: bool, busy_timeout_ms: Optional[int] = None) -> Iterator[sqlite3.Connection]:
    if conn is not None:
        yield conn
        return
    kwargs = {} if busy_timeout_ms is None else {"busy_timeout_ms": busy_timeout_ms}
    cm = db.transaction(data_dir, **kwargs) if write else db.connection(data_dir, **kwargs)
    with cm as opened:
        yield opened


def _dirs(data_dir: Optional[Path], output_dir: Optional[Path]) -> tuple[Path, Path]:
    data = db._data_dir(data_dir)
    if output_dir is None:
        from .path_utils import get_output_dir
        output = get_output_dir()
    else:
        output = Path(output_dir)
    return data, output


def _root_dir(root: str, data: Path, output: Path) -> Path:
    return output if root == "output" else data


def _abs(root: str, rel: str, data: Path, output: Path) -> Path:
    return db.from_rel(rel, _root_dir(root, data, output))


def _stat(path: Path, kind: str) -> tuple[Optional[int], Optional[int]]:
    """``(size, mtime_ns)``; both None for a directory, a track dir, or a missing file."""
    if kind == "per_user":
        return None, None
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    if stat_mod.S_ISDIR(st.st_mode):
        return None, None
    return st.st_size, st.st_mtime_ns


# ---------------------------------------------------------------------------
# Owners and rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Owner:
    """What a file belongs to. ``id`` is ``transcripts.id``, ``profiles.id``,
    ``campaigns.id`` (int), or ``recordings.id`` (str)."""

    kind: OwnerKind
    id: int | str

    @classmethod
    def for_stem(cls, stem: str, conn: Optional[sqlite3.Connection] = None, *,
                 data_dir: Optional[Path] = None,
                 output_dir: Optional[Path] = None) -> Optional["Owner"]:
        stem = _nfc(stem)
        with _use(conn, data_dir, write=False) as c:
            row = c.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()
            if row is None:
                _, output = _dirs(data_dir, output_dir)
                if _fold(output):
                    want = stem.casefold()
                    for r in c.execute("SELECT id, stem FROM transcripts"):
                        if r["stem"].casefold() == want:
                            row = r
                            break
        return cls("transcript", row["id"]) if row else None

    @classmethod
    def for_profile_key(cls, key: str, conn: Optional[sqlite3.Connection] = None, *,
                        data_dir: Optional[Path] = None) -> Optional["Owner"]:
        with _use(conn, data_dir, write=False) as c:
            row = c.execute("SELECT id FROM profiles WHERE key = ?", (_nfc(key),)).fetchone()
        return cls("profile", row["id"]) if row else None

    @classmethod
    def for_campaign_slug(cls, slug: str, conn: Optional[sqlite3.Connection] = None, *,
                          data_dir: Optional[Path] = None) -> Optional["Owner"]:
        with _use(conn, data_dir, write=False) as c:
            row = c.execute("SELECT id FROM campaigns WHERE slug = ?", (_nfc(slug),)).fetchone()
        return cls("campaign", row["id"]) if row else None

    @classmethod
    def for_recording(cls, rec_id: str, conn: Optional[sqlite3.Connection] = None, *,
                      data_dir: Optional[Path] = None) -> Optional["Owner"]:
        with _use(conn, data_dir, write=False) as c:
            row = c.execute("SELECT id FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        return cls("recording", row["id"]) if row else None


@dataclass(frozen=True)
class FileRow:
    id: int
    kind: str
    root: str
    rel_path: str
    label: Optional[str]
    owner: Owner
    size: Optional[int]
    mtime_ns: Optional[int]
    path: Path


def _owner_of(row: sqlite3.Row) -> Owner:
    for kind, column in _OWNER_COLUMN.items():
        if row[column] is not None:
            return Owner(kind, row[column])  # type: ignore[arg-type]
    raise ValueError("files row has no owner")  # the schema's CHECK forbids it


def _file_row(row: sqlite3.Row, data: Path, output: Path) -> FileRow:
    return FileRow(
        id=row["id"], kind=row["kind"], root=row["root"], rel_path=row["rel_path"],
        label=row["label"], owner=_owner_of(row), size=row["size"],
        mtime_ns=row["mtime_ns"], path=_abs(row["root"], row["rel_path"], data, output),
    )


def _check_kind(kind: str, owner: Owner) -> None:
    if kind not in ROOT_OF_KIND:
        raise ValueError(f"unknown file kind: {kind!r}")
    if _OWNER_OF_KIND[kind] != owner.kind:
        raise ValueError(f"a {kind} file belongs to a {_OWNER_OF_KIND[kind]}, not a {owner.kind}")


def _slot_clause(owner: Owner, kind: str, label: Optional[str]) -> tuple[str, tuple]:
    column = _OWNER_COLUMN[owner.kind]
    return (f"{column} = ? AND kind = ? AND coalesce(label, '') = ?",
            (owner.id, kind, label or ""))


def _find_by_path(conn: sqlite3.Connection, root: str, rel: str,
                  fold: bool) -> Optional[sqlite3.Row]:
    row = conn.execute("SELECT * FROM files WHERE root = ? AND rel_path = ?",
                       (root, rel)).fetchone()
    if row is not None or not (fold or not rel.isascii()):
        return row
    want = _key(rel, fold)
    for candidate in conn.execute("SELECT * FROM files WHERE root = ?", (root,)):
        if _key(candidate["rel_path"], fold) == want:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Register, forget, look up
# ---------------------------------------------------------------------------

def add(path: Path, *, kind: str, owner: Owner, label: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None, data_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None) -> Optional[Path]:
    """Register ``path`` as ``owner``'s file of ``kind``.

    Returns the path this replaced when the owner already had a file in that
    slot, else None. For a ``transcript`` the "replaced" path can be the same
    file spelled differently, so only ``set_audio`` ever unlinks it.

    Raises ``ValueError`` for a path outside the kind's root (or an audio file
    under ``<data>/recordings/``, which belongs to the recording), and
    ``OwnershipConflict`` when the path or slot belongs to something else.
    A file that doesn't exist yet is registered without stats.
    """
    _check_kind(kind, owner)
    data, output = _dirs(data_dir, output_dir)
    root = ROOT_OF_KIND[kind]
    root_dir = _root_dir(root, data, output)
    path = Path(path)
    if kind == "audio":
        recordings = Path(os.path.realpath(data / "recordings"))
        if Path(os.path.realpath(path)).is_relative_to(recordings):
            raise ValueError("a recording's audio belongs to the recording, not a transcript")
    rel = db.to_rel(path, root_dir)
    size, mtime = _stat(path, kind)
    fold = _fold(root_dir)

    with _use(conn, data_dir, write=True) as c:
        same = _find_by_path(c, root, rel, fold)
        if same is not None:
            if (_owner_of(same) != owner or same["kind"] != kind
                    or (same["label"] or "") != (label or "")):
                raise OwnershipConflict(f"{rel} is already registered to another file")
            if size is None:
                c.execute("UPDATE files SET rel_path = ? WHERE id = ?", (rel, same["id"]))
            else:
                c.execute("UPDATE files SET rel_path = ?, size = ?, mtime_ns = ? WHERE id = ?",
                          (rel, size, mtime, same["id"]))
            return None

        clause, params = _slot_clause(owner, kind, label)
        slot = c.execute(f"SELECT * FROM files WHERE {clause}", params).fetchone()
        if slot is not None:
            old = _abs(slot["root"], slot["rel_path"], data, output)
            try:
                c.execute("UPDATE files SET rel_path = ?, size = ?, mtime_ns = ? WHERE id = ?",
                          (rel, size, mtime, slot["id"]))
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc):
                    raise OwnershipConflict(f"{rel} is already registered") from exc
                raise
            return old

        try:
            c.execute(
                f"INSERT INTO files (kind, root, rel_path, label, {_OWNER_COLUMN[owner.kind]}, "
                "size, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, root, rel, label, owner.id, size, mtime),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise OwnershipConflict(f"{rel} is already registered") from exc
            raise
    return None


def add_if_owned(path: Path, *, kind: str, owner: Optional[Owner],
                 label: Optional[str] = None, conn: Optional[sqlite3.Connection] = None,
                 data_dir: Optional[Path] = None,
                 output_dir: Optional[Path] = None) -> Optional[Path]:
    """Best-effort :func:`add` for write sites; never raises.

    Returns None without registering when there is no owner or the path is
    outside the kind's root (a CLI ``--output`` file). A failed add is logged
    as a warning. Catching a statement-level error inside the caller's
    transaction leaves that transaction usable.
    """
    if owner is None:
        log.debug("No owner for %s; not registered", path)
        return None
    try:
        return add(path, kind=kind, owner=owner, label=label, conn=conn,
                   data_dir=data_dir, output_dir=output_dir)
    except ValueError as exc:
        log.debug("Not registered (%s): %s", path, exc)
    except (sqlite3.Error, OwnershipConflict, OSError) as exc:
        log.warning("Could not register %s: %s", path, exc)
    return None


def _delete_rows(c: sqlite3.Connection, rows: list[sqlite3.Row],
                 data: Path, output: Path) -> list[Path]:
    removed = []
    for row in rows:
        c.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        removed.append(_abs(row["root"], row["rel_path"], data, output))
    return removed


def forget(path: Path, conn: Optional[sqlite3.Connection] = None, *,
           data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> list[Path]:
    """Remove the row for ``path``. Returns the absolute paths of the rows removed."""
    data, output = _dirs(data_dir, output_dir)
    with _use(conn, data_dir, write=True) as c:
        found: list[sqlite3.Row] = []
        for root in ("output", "data"):
            root_dir = _root_dir(root, data, output)
            try:
                rel = db.to_rel(Path(path), root_dir)
            except ValueError:
                continue
            row = _find_by_path(c, root, rel, _fold(root_dir))
            if row is not None and row["id"] not in {r["id"] for r in found}:
                found.append(row)
        return _delete_rows(c, found, data, output)


def forget_kind(owner: Owner, kind: str, label: Optional[str] = None,
                conn: Optional[sqlite3.Connection] = None, *,
                data_dir: Optional[Path] = None,
                output_dir: Optional[Path] = None) -> list[Path]:
    """Remove ``owner``'s rows of ``kind``: the one with ``label``, or all when
    ``label`` is None. Returns the absolute paths removed."""
    data, output = _dirs(data_dir, output_dir)
    column = _OWNER_COLUMN[owner.kind]
    with _use(conn, data_dir, write=True) as c:
        sql = f"SELECT * FROM files WHERE {column} = ? AND kind = ?"
        params: list = [owner.id, kind]
        if label is not None:
            sql += " AND label = ?"
            params.append(label)
        return _delete_rows(c, c.execute(sql, params).fetchall(), data, output)


def is_registered(path: Path, conn: Optional[sqlite3.Connection] = None, *,
                  data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> bool:
    """Whether any row names ``path``."""
    data, output = _dirs(data_dir, output_dir)
    with _use(conn, data_dir, write=False) as c:
        for root in ("output", "data"):
            root_dir = _root_dir(root, data, output)
            try:
                rel = db.to_rel(Path(path), root_dir)
            except ValueError:
                continue
            if _find_by_path(c, root, rel, _fold(root_dir)) is not None:
                return True
    return False


def files_for(owner: Owner, conn: Optional[sqlite3.Connection] = None, *,
              data_dir: Optional[Path] = None,
              output_dir: Optional[Path] = None) -> list[FileRow]:
    data, output = _dirs(data_dir, output_dir)
    with _use(conn, data_dir, write=False) as c:
        rows = c.execute(
            f"SELECT * FROM files WHERE {_OWNER_COLUMN[owner.kind]} = ? ORDER BY id",
            (owner.id,),
        ).fetchall()
    return [_file_row(r, data, output) for r in rows]


def file_for(owner: Owner, kind: str, label: Optional[str] = None,
             conn: Optional[sqlite3.Connection] = None, *,
             data_dir: Optional[Path] = None,
             output_dir: Optional[Path] = None) -> Optional[FileRow]:
    data, output = _dirs(data_dir, output_dir)
    clause, params = _slot_clause(owner, kind, label)
    with _use(conn, data_dir, write=False) as c:
        row = c.execute(f"SELECT * FROM files WHERE {clause}", params).fetchone()
    return _file_row(row, data, output) if row else None


def paths_for_delete(owner: Owner, conn: sqlite3.Connection, *,
                     data_dir: Optional[Path] = None,
                     output_dir: Optional[Path] = None) -> list[Path]:
    """Every file ``owner`` has a row for.

    Call inside the delete transaction, before the owner row goes (the
    cascade removes the rows), then :func:`unlink_paths` after the commit.
    """
    return [r.path for r in files_for(owner, conn, data_dir=data_dir, output_dir=output_dir)]


def unlink_paths(paths: Iterable[Path]) -> list[Path]:
    """Delete files (and track directories) after the owner's commit.

    Returns the paths that couldn't be removed (e.g. held open on Windows).
    Their rows are already gone, so :func:`sync` lists them as unclaimed.
    """
    failed: list[Path] = []
    for path in paths:
        path = Path(path)
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not delete %s: %s", path, exc)
            failed.append(path)
    return failed


# ---------------------------------------------------------------------------
# Move, repoint, refresh
# ---------------------------------------------------------------------------

def _same_file(src: Path, dst: Path) -> bool:
    """Whether ``dst`` names the file ``src`` already is (a case-only rename)."""
    try:
        return os.path.samefile(src, dst)
    except OSError:
        return (src.parent == dst.parent
                and _key(src.name, True) == _key(dst.name, True)
                and _fold(dst.parent))


def move(row: FileRow, new_path: Path, *, data_dir: Optional[Path] = None,
         output_dir: Optional[Path] = None) -> MoveResult:
    """Move ``row``'s file to ``new_path`` and re-point its row.

    Call after the owner's transaction commits. Returns ``"missing"`` (and
    forgets the row) when the source is gone, ``"conflict"`` when the target
    is a different existing file, and ``"error"`` when the move fails (e.g. a
    Windows sharing violation), leaving the row unchanged. Raises when the
    row can't be updated after the file moved; the file is moved back first.
    """
    data, output = _dirs(data_dir, output_dir)
    src, dst = row.path, Path(new_path)
    new_rel = db.to_rel(dst, _root_dir(row.root, data, output))  # before touching the disk
    if not os.path.lexists(src):
        forget_id(row.id, data_dir=data_dir)
        return "missing"
    if os.path.lexists(dst) and not _same_file(src, dst):
        return "conflict"
    try:
        from .transcript_store import _replace
        if not _replace(src, dst):
            return "error"
    except OSError as exc:
        log.warning("Could not move %s to %s: %s", src, dst, exc)
        return "error"
    size, mtime = _stat(dst, row.kind)
    try:
        with db.transaction(data_dir) as c:
            c.execute("UPDATE files SET rel_path = ?, size = coalesce(?, size), "
                      "mtime_ns = coalesce(?, mtime_ns) WHERE id = ?",
                      (new_rel, size, mtime, row.id))
    except BaseException:
        try:
            os.replace(dst, src)
        except OSError:
            log.error("Could not move %s back to %s", dst, src)
        raise
    return "moved"


def forget_id(file_id: int, conn: Optional[sqlite3.Connection] = None, *,
              data_dir: Optional[Path] = None) -> None:
    """Remove one row by id."""
    with _use(conn, data_dir, write=True) as c:
        c.execute("DELETE FROM files WHERE id = ?", (file_id,))


def repoint(row: FileRow, new_path: Path, conn: Optional[sqlite3.Connection] = None, *,
            data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> None:
    """Point ``row`` at a file that has already moved (renamed outside wisper)."""
    data, output = _dirs(data_dir, output_dir)
    new_path = Path(new_path)
    rel = db.to_rel(new_path, _root_dir(row.root, data, output))
    size, mtime = _stat(new_path, row.kind)
    with _use(conn, data_dir, write=True) as c:
        c.execute("UPDATE files SET rel_path = ?, size = coalesce(?, size), "
                  "mtime_ns = coalesce(?, mtime_ns) WHERE id = ?",
                  (rel, size, mtime, row.id))


def refresh(row: FileRow, conn: Optional[sqlite3.Connection] = None, *,
            data_dir: Optional[Path] = None) -> None:
    """Re-stat ``row``'s file. A missing file keeps its last observed stats."""
    size, mtime = _stat(row.path, row.kind)
    if size is None:
        return
    with _use(conn, data_dir, write=True) as c:
        c.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE id = ?",
                  (size, mtime, row.id))


# ---------------------------------------------------------------------------
# Sync: the registry vs. the disk
# ---------------------------------------------------------------------------

@dataclass
class SyncReport:
    registered: list[Path] = field(default_factory=list)
    refreshed: list[Path] = field(default_factory=list)
    missing: list[FileRow] = field(default_factory=list)   # rows whose file is gone
    unclaimed: list[Path] = field(default_factory=list)    # pattern files with no owner
    errors: list[str] = field(default_factory=list)


@dataclass
class _Found:
    kind: str
    path: Path
    root: str
    owner: Optional[Owner]
    label: Optional[str] = None
    registrable: bool = True   # False for files that are only ever listed


_reports: dict[tuple[str, str], SyncReport] = {}
_last_run: dict[tuple[str, str], float] = {}
_state_lock = threading.Lock()


def _state_key(data: Path, output: Path) -> tuple[str, str]:
    return os.path.realpath(data), os.path.realpath(output)


def last_report(data_dir: Optional[Path] = None,
                output_dir: Optional[Path] = None) -> Optional[SyncReport]:
    """The most recent :func:`sync` result for these directories, if any."""
    data, output = _dirs(data_dir, output_dir)
    return _reports.get(_state_key(data, output))


def drop_from_report(data_dir: Optional[Path] = None, output_dir: Optional[Path] = None, *,
                     file_ids: Iterable[int] = (), paths: Iterable[Path] = ()) -> None:
    """Remove handled entries (forgotten rows, deleted files) from the cached report."""
    data, output = _dirs(data_dir, output_dir)
    report = _reports.get(_state_key(data, output))
    if report is None:
        return
    ids = set(file_ids)
    gone = {os.path.realpath(p) for p in paths}
    with _state_lock:
        report.missing = [r for r in report.missing if r.id not in ids]
        report.unclaimed = [p for p in report.unclaimed if os.path.realpath(p) not in gone]


def reset_state() -> None:
    """Forget every cached report and throttle timestamp (tests)."""
    with _state_lock:
        _reports.clear()
        _last_run.clear()


def sync_if_due(output_dir: Optional[Path] = None, data_dir: Optional[Path] = None, *,
                interval_s: float = SYNC_INTERVAL_S, busy_timeout_ms: Optional[int] = 500,
                force: bool = False) -> Optional[SyncReport]:
    """:func:`sync`, unless one ran for these directories within ``interval_s``.

    Used by page loads: a short busy timeout keeps a view from waiting on a
    busy database. Returns None when skipped.
    """
    data, output = _dirs(data_dir, output_dir)
    key = _state_key(data, output)
    now = time.monotonic()
    if not force and now - _last_run.get(key, -interval_s - 1.0) < interval_s:
        return None
    _last_run[key] = now
    return sync(output, data, busy_timeout_ms=busy_timeout_ms)


def _output_candidates(name: str) -> list[tuple[str, str, Optional[str]]]:
    """``(kind, stem, label)`` readings of an output-root file name, most likely first."""
    lower = name.lower()
    if lower.endswith(".summary.md"):
        return [("summary", name[:-len(".summary.md")], None)]
    if lower.endswith(".md"):
        return [("transcript", name[:-3], None)]
    if lower.endswith(".md.bak"):
        return [("backup", name[:-len(".md.bak")], None)]
    if name.endswith("_diar.json"):
        return [("sidecar", name[:-len("_diar.json")], None)]
    if name.endswith((".mp3", ".txt")) and "_excerpt_" in name:
        kind = "excerpt" if name.endswith(".mp3") else "excerpt_text"
        found = []
        at = name.rfind("_excerpt_")
        while at != -1:
            label = name[at + len("_excerpt_"):-4]
            if at > 0 and label:
                found.append((kind, name[:at], label))
            at = name.rfind("_excerpt_", 0, at)
        return found
    if lower.endswith(".flac"):
        return [("audio", name[:-5], None)]
    return []


def _scan_output(output: Path, owners: dict[str, Owner], fold: bool) -> list[_Found]:
    from .transcript_store import TEMP_PREFIX
    found = []
    try:
        entries = sorted(os.scandir(output), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries:
        name = entry.name
        if name.startswith(TEMP_PREFIX):
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        candidates = _output_candidates(name)
        if not candidates:
            continue
        owner, picked = None, candidates[0]
        for cand in candidates:
            owner = owners.get(_key(cand[1], fold))
            if owner is not None:
                picked = cand
                break
        kind, _stem, label = picked
        # An unregistered .flac may be the user's own file: it is only ever listed.
        found.append(_Found(kind, Path(entry.path), "output", owner, label,
                            registrable=(kind != "audio")))
    return found


def _scan_data(data: Path, in_output, recordings: dict[str, str], profiles: dict[str, Owner],
               campaigns: dict[str, Owner]) -> list[_Found]:
    found = []

    def listing(directory: Path) -> list[Path]:
        try:
            return sorted(directory.iterdir())
        except OSError:
            return []

    rec_root = data / "recordings"
    if not in_output(rec_root):
        for rec_dir in listing(rec_root):
            if not rec_dir.is_dir() or in_output(rec_dir):
                continue
            status = recordings.get(rec_dir.name)
            if status in _ACTIVE_CAPTURE:
                continue
            owner = Owner("recording", rec_dir.name) if status is not None else None
            for kind, name in (("combined", "combined.wav"), ("live_draft", "live_transcript.md")):
                if (rec_dir / name).is_file():
                    found.append(_Found(kind, rec_dir / name, "data", owner))
            for track in listing(rec_dir / "per-user"):
                if track.is_dir() and (track.name in ("mic", "system") or track.name.isdigit()):
                    found.append(_Found("per_user", track, "data", owner, track.name))
    clips = data / "profiles" / "embeddings"
    if not in_output(clips):
        for clip in listing(clips):
            if clip.suffix == ".mp3" and clip.is_file():
                found.append(_Found("reference_clip", clip, "data", profiles.get(_nfc(clip.stem))))
    camp_root = data / "campaigns"
    if not in_output(camp_root):
        for camp_dir in listing(camp_root):
            journal = camp_dir / "journal.md"
            if camp_dir.is_dir() and not in_output(camp_dir) and journal.is_file():
                found.append(_Found("journal", journal, "data", campaigns.get(_nfc(camp_dir.name))))
    return found


def sync(output_dir: Optional[Path] = None, data_dir: Optional[Path] = None, *,
         busy_timeout_ms: Optional[int] = None, scan_only: bool = False) -> SyncReport:
    """Bring the registry in line with the disk, without deleting anything.

    - A pattern file whose owner exists and has no row of that kind and label
      is registered. An unregistered ``.flac`` is only listed.
    - A row whose stats changed is refreshed.
    - A row whose file is gone is listed as ``missing`` (stats kept; a missing
      ``.md`` is ``transcripts.missing_since``'s business).
    - A pattern file with no owner, or a second file for an owned slot, is
      listed as ``unclaimed``.

    The disk is scanned outside any transaction, then changes are written in
    short batches with compare-and-set, re-statting each new path first.
    Errors are collected per file; this never raises. With ``scan_only`` the
    report is computed the same way but nothing is written: files that
    would be registered or refreshed are not, and the report is not cached.
    """
    report = SyncReport()
    try:
        data, output = _dirs(data_dir, output_dir)
    except Exception as exc:  # unusable directories: nothing to do
        report.errors.append(f"sync could not start: {exc}")
        return report
    try:
        _sync(report, data, output, data_dir, busy_timeout_ms, scan_only)
    except (sqlite3.Error, OSError) as exc:
        report.errors.append(f"sync stopped: {exc}")
        log.warning("File sync stopped: %s", exc)
    except Exception as exc:
        report.errors.append(f"sync failed: {exc}")
        log.warning("File sync failed", exc_info=True)
    if not scan_only:
        with _state_lock:
            _reports[_state_key(data, output)] = report
    return report


def _sync(report: SyncReport, data: Path, output: Path, data_dir_arg: Optional[Path],
          busy_timeout_ms: Optional[int], scan_only: bool = False) -> None:
    fold = _fold(output)
    data_real = Path(os.path.realpath(data))
    out_real = Path(os.path.realpath(output))
    skip_output = out_real == data_real or out_real.is_relative_to(data_real)

    def in_output(p: Path) -> bool:
        return skip_output and Path(os.path.realpath(p)).is_relative_to(out_real)

    with _use(None, data_dir_arg, write=False, busy_timeout_ms=busy_timeout_ms) as c:
        stems = {r["id"]: r["stem"] for r in c.execute("SELECT id, stem FROM transcripts")}
        recordings = {r["id"]: r["capture_status"]
                      for r in c.execute("SELECT id, capture_status FROM recordings")}
        profiles = {_nfc(r["key"]): Owner("profile", r["id"])
                    for r in c.execute("SELECT id, key FROM profiles")}
        campaigns = {_nfc(r["slug"]): Owner("campaign", r["id"])
                     for r in c.execute("SELECT id, slug FROM campaigns")}
        db_rows = c.execute("SELECT * FROM files ORDER BY id").fetchall()

    owners = {_key(stem, fold): Owner("transcript", tid) for tid, stem in stems.items()}
    rows = [_file_row(r, data, output) for r in db_rows]
    by_path = {(r.root, _key(r.rel_path, fold if r.root == "output" else False)): r for r in rows}
    taken = {(r.owner, r.kind, r.label or "") for r in rows}

    # Rows: refresh changed stats, list the missing.
    refresh_rows: list[tuple[FileRow, int, int]] = []
    for r in rows:
        if r.kind == "transcript":
            want = _key(stems.get(r.owner.id, "") + ".md", fold)
            if _key(r.rel_path, fold) != want:
                report.errors.append(
                    f"transcript row {r.rel_path!r} does not match its stem {stems.get(r.owner.id)!r}")
        if not os.path.lexists(r.path):
            if r.kind != "transcript":
                report.missing.append(r)
            continue
        size, mtime = _stat(r.path, r.kind)
        if size is not None and (size, mtime) != (r.size, r.mtime_ns):
            refresh_rows.append((r, size, mtime))

    # Files: register what has an owner and an empty slot, list the rest.
    scanned = _scan_output(output, owners, fold) + _scan_data(
        data, in_output, recordings, profiles, campaigns)
    new_files: list[_Found] = []
    for f in scanned:
        rel_root = _root_dir(f.root, data, output)
        try:
            rel = db.to_rel(f.path, rel_root)
        except ValueError:
            continue
        if (f.root, _key(rel, fold if f.root == "output" else False)) in by_path:
            continue
        slot = (f.owner, f.kind, f.label or "")
        if f.owner is None or not f.registrable or slot in taken:
            report.unclaimed.append(f.path)
            continue
        taken.add(slot)
        new_files.append(f)

    if scan_only:
        return

    # Writes: short transactions, nothing at all when nothing changed.
    for start in range(0, len(refresh_rows), _SYNC_BATCH):
        batch = refresh_rows[start:start + _SYNC_BATCH]
        try:
            with _use(None, data_dir_arg, write=True, busy_timeout_ms=busy_timeout_ms) as c:
                for r, size, mtime in batch:
                    new = _stat(r.path, r.kind)
                    if new[0] is None:
                        continue
                    cur = c.execute(
                        "UPDATE files SET size = ?, mtime_ns = ? WHERE id = ? AND rel_path = ? "
                        "AND size IS ? AND mtime_ns IS ?",
                        (new[0], new[1], r.id, r.rel_path, r.size, r.mtime_ns))
                    if cur.rowcount:
                        report.refreshed.append(r.path)
        except sqlite3.Error as exc:
            report.errors.append(f"could not refresh file records: {exc}")

    for start in range(0, len(new_files), _SYNC_BATCH):
        batch_files = new_files[start:start + _SYNC_BATCH]
        try:
            with _use(None, data_dir_arg, write=True, busy_timeout_ms=busy_timeout_ms) as c:
                for f in batch_files:
                    _insert_found(c, f, data, output, report)
        except sqlite3.Error as exc:
            report.errors.append(f"could not register files: {exc}")


def _insert_found(c: sqlite3.Connection, f: _Found, data: Path, output: Path,
                  report: SyncReport) -> None:
    assert f.owner is not None
    if not os.path.lexists(f.path):  # deleted since the scan
        return
    try:
        rel = db.to_rel(f.path, _root_dir(f.root, data, output))
        size, mtime = _stat(f.path, f.kind)
        c.execute(
            f"INSERT INTO files (kind, root, rel_path, label, {_OWNER_COLUMN[f.owner.kind]}, "
            "size, mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f.kind, f.root, rel, f.label, f.owner.id, size, mtime))
        report.registered.append(f.path)
    except sqlite3.IntegrityError as exc:
        text = str(exc)
        if "UNIQUE" in text or "FOREIGN KEY" in text:
            return  # registered, or its owner deleted, since the scan
        report.errors.append(f"{f.path.name}: {text}")
    except (ValueError, OSError) as exc:
        report.errors.append(f"{f.path.name}: {exc}")
