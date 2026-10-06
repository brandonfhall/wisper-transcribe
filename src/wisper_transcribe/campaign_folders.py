"""Campaign folders: the directory each campaign owns under the output root.

A campaign's folder name is stored in ``campaigns.folder``. wisper owns a
folder only once it has claimed it (``campaigns.folder_claimed``): it created
the folder, or found it absent or empty. An unclaimed folder is never scanned,
written to, or deleted from, because the output root can be the user's vault.
The output root itself is never created here; an absent root means
"unavailable".
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import db
from .config import get_output_root

log = logging.getLogger(__name__)

# Serializes filesystem location changes (folder renames here; moves and
# renames in transcript_store) with reconcile. transcript_store re-exports
# this object, so there is one lock per process.
_LOCATION_LOCK = threading.RLock()

# Microsoft "Naming Files, Paths, and Namespaces": COM0-9, LPT0-9, and the superscript forms.
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"{p}{d}" for p in ("COM", "LPT") for d in "0123456789¹²³")}

_CLUTTER = {".ds_store", "thumbs.db", "desktop.ini"}


class FolderTakenError(Exception):
    """The campaign's folder holds files wisper doesn't own, or can't be made."""


class FolderPendingError(FolderTakenError):
    """A folder rename is in progress for the campaign."""


class FolderMissingError(FolderTakenError):
    """The campaign's claimed folder is gone from the output root."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _trim(s: str) -> str:
    while True:
        t = s.strip().lstrip(".").rstrip(". ")
        if t == s:
            return s
        s = t


def _cut(s: str, room: int, byte_room: int) -> str:
    # ext4 and APFS cap one name at 255 bytes; the journal name adds to the folder's.
    s = s[:room]
    while len(s.encode("utf-8")) > byte_room:
        s = s[:-1]
    return s


def is_reserved(name: str) -> bool:
    """Whether Windows treats ``name`` as a device name (``con.txt`` and ``nul .md`` too)."""
    return name.partition(".")[0].rstrip().upper() in _RESERVED


def folder_name(display_name: str, room: int = 80, byte_room: int = 200) -> str:
    """A display name made safe as one folder name on Windows, macOS, and Linux."""
    s = unicodedata.normalize("NFC", display_name)
    s = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', " ", s)
    s = re.sub(r"\s+", " ", s)
    s = _trim(_cut(_trim(s), room, byte_room))
    head, dot, tail = s.partition(".")
    core = head.rstrip()  # Windows ignores spaces before the extension: "nul .txt" is NUL
    if core.upper() in _RESERVED:
        s = _trim(_cut(core + "_" + head[len(core):] + dot + tail, room, byte_room))
    return s or "Campaign"


def journal_name(folder: str) -> str:
    """The campaign journal's file name; per campaign so Obsidian links stay unambiguous."""
    return f"{folder} Journal.md"


def combined_summary_name(folder: str) -> str:
    """The campaign's combined summary file name (one per campaign)."""
    return f"{folder} Combined Summary.md"


def recap_name(folder: str, stem: str) -> str:
    """A recap's file name: one per newest session stem, so several coexist."""
    return f"{folder} Recap \u2014 {stem}.md"


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def inside(root: Path, name: str) -> Optional[Path]:
    """``root/name`` when it stays inside ``root``, else None (CWE-22 path guard).

    The two layers CodeQL recognises: strip leading components with
    ``os.path.basename``, then confirm with ``abspath`` + ``startswith``.
    """
    base = os.path.abspath(root)
    safe = os.path.basename(name)
    path = os.path.abspath(os.path.join(base, safe))
    if path == base or not path.startswith(base + os.sep):
        return None
    return Path(path)


def unique_folder(display_name: str, conn: Optional[sqlite3.Connection] = None, *,
                  exclude_id: Optional[int] = None, data_dir: Optional[Path] = None) -> str:
    """``folder_name`` with a `` (2)``, `` (3)`` suffix until no other campaign's
    ``folder`` or ``folder_pending`` matches it, ignoring case."""
    def taken_set(c: sqlite3.Connection) -> set[str]:
        rows = c.execute(
            "SELECT folder, folder_pending FROM campaigns WHERE id IS NOT ?", (exclude_id,)
        ).fetchall()
        return {_fold(n) for r in rows for n in (r[0], r[1]) if n is not None}

    if conn is not None:
        taken = taken_set(conn)
    else:
        with db.connection(data_dir) as c:
            taken = taken_set(c)
    folder = folder_name(display_name)
    n = 2
    while _fold(folder) in taken:
        sfx = f" ({n})"
        folder = folder_name(display_name, room=80 - len(sfx),
                             byte_room=200 - len(sfx.encode())) + sfx
        n += 1
    return folder


def taken_by_campaign(conn: sqlite3.Connection, name: str, *,
                      exclude_id: Optional[int] = None) -> bool:
    """Whether another campaign's ``folder`` or ``folder_pending`` folds to ``name``.

    The database's NOCASE folds ASCII only, so the comparison casefolds in Python.
    The lookup goes through the unique index on ``folder`` and the scan is of the
    campaign table only, which is tiny.
    """
    want = _fold(name)
    rows = conn.execute(
        "SELECT folder, folder_pending FROM campaigns WHERE id IS NOT ?", (exclude_id,)
    )
    return any(other is not None and _fold(other) == want for row in rows for other in row)


def holds_only_journal(path: Path, folder: str) -> bool:
    """True when the existing folder holds only wisper's journal (a keep-files delete left it).

    An empty folder is not journal-only: it was made by the user, so a campaign
    whose name matches it is refused.
    """
    try:
        nonempty = any(not is_clutter(n) for n in os.listdir(path))
    except OSError:
        return False
    return nonempty and holds_only_wisper(path, folder)


def folder_exists(name: str, *, output_dir: Optional[Path] = None,
                  exclude_folder: Optional[str] = None) -> bool:
    """Whether the output root holds an entry folding to ``name`` that wisper can't re-claim.

    An absent output root finds nothing: the folder is claimed later, on first
    write. ``exclude_folder`` is this campaign's current folder (a no-op rename).
    A directory holding only its own journal (a keep-files delete) is re-claimable.
    """
    want = _fold(name)
    if exclude_folder is not None and _fold(exclude_folder) == want:
        return False
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if not output.is_dir():
        return False
    # The two-layer guard on the user-derived name (CWE-22); the loop then matches
    # a differently-cased on-disk entry, whose own name is not user input.
    if inside(output, name) is None:
        return False
    try:
        entries = list(os.scandir(output))
    except OSError:
        return False
    for entry in entries:
        if _fold(entry.name) != want:
            continue
        path = inside(output, entry.name)
        if path is None:
            continue
        if path.is_dir() and holds_only_journal(path, path.name):
            continue
        return True
    return False


def check_available(name: str, conn: Optional[sqlite3.Connection] = None, *,
                    output_dir: Optional[Path] = None,
                    exclude_id: Optional[int] = None) -> Optional[str]:
    """Why ``name`` can't be a campaign folder, or None when it can.

    ``taken`` when another campaign's ``folder`` or ``folder_pending`` folds to
    ``name``. ``folder_exists`` when an entry in the output root folds to
    ``name`` and isn't this campaign's current folder. An absent output root
    finds nothing on disk. A directory holding only its own journal (a
    keep-files delete) is available and is re-claimed.
    """
    def run(c: sqlite3.Connection) -> Optional[str]:
        if taken_by_campaign(c, name, exclude_id=exclude_id):
            return "taken"
        current = None
        if exclude_id is not None:
            row = c.execute("SELECT folder FROM campaigns WHERE id = ?", (exclude_id,)).fetchone()
            current = row[0] if row is not None else None
        if folder_exists(name, output_dir=output_dir, exclude_folder=current):
            return "folder_exists"
        return None

    if conn is not None:
        return run(conn)
    with db.connection() as c:
        return run(c)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

def is_clutter(name: str) -> bool:
    """OS and wisper temp files that don't make a folder non-empty."""
    from .transcript_store import TEMP_PREFIX

    return name.lower() in _CLUTTER or name.startswith("._") or name.startswith(TEMP_PREFIX)


def _row(campaign_id: int, data_dir: Optional[Path]):
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT folder, folder_pending, folder_claimed FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"Campaign {campaign_id} not found")
    return row[0], row[1], bool(row[2])


def _is_wisper_journal(path: Path) -> bool:
    from .journal import parse_journal

    try:
        meta, _ = parse_journal(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
    return isinstance(meta, dict) and meta.get("type") == "campaign-journal"


def _claimable(path: Path, folder: str) -> bool:
    """Make the folder if absent; true when it's new, empty, or holds only wisper's journal."""
    try:
        path.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        log.warning("cannot create campaign folder %r: %s (errno %s)", folder, exc, exc.errno)
        return False
    return path.is_dir() and holds_only_wisper(path, folder)


def holds_only_wisper(path: Path, folder: str) -> bool:
    """True when the existing folder is empty or holds only wisper's journal (no mkdir)."""
    try:
        names = [n for n in os.listdir(path) if not is_clutter(n)]
    except OSError:
        return False
    if not names:
        return True
    return (names == [journal_name(folder)]
            and _is_wisper_journal(path / names[0]))


def _claim_cas(conn: sqlite3.Connection, campaign_id: int, folder: str) -> bool:
    """Claim ``folder`` only if it's still the campaign's name and no rename is pending."""
    cur = conn.execute(
        "UPDATE campaigns SET folder_claimed = 1 WHERE id = ? "
        "AND folder = ? COLLATE BINARY AND folder_pending IS NULL",
        (campaign_id, folder),
    )
    return cur.rowcount == 1


def ensure_folder(campaign_id: int, *, data_dir: Optional[Path] = None,
                  output_dir: Optional[Path] = None) -> Path:
    """The campaign's claimed folder, claiming (and creating) it when it's free.

    Call with no transaction open: the claim opens its own. Raises
    ``FileNotFoundError`` when the output root is absent, ``FolderPendingError``
    during a rename, ``FolderMissingError`` when a claimed folder vanished, and
    ``FolderTakenError`` when the folder holds files wisper doesn't own.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if not output.is_dir():
        raise FileNotFoundError(f"output folder {output} is not available")
    for _ in range(2):
        folder, pending, claimed = _row(campaign_id, data_dir)
        if pending is not None:
            # Finish the rename rather than writing into a half-moved folder.
            if not finish_folder_rename(campaign_id, data_dir=data_dir, output_dir=output):
                raise FolderPendingError(folder)
            folder, pending, claimed = _row(campaign_id, data_dir)
            if pending is not None:
                raise FolderPendingError(folder)
        path = output / folder
        if claimed:
            if path.is_dir():
                return path
            raise FolderMissingError(folder)
        if not _claimable(path, folder):
            raise FolderTakenError(folder)
        with db.transaction(data_dir) as conn:
            if _claim_cas(conn, campaign_id, folder):
                _register_journal(conn, campaign_id, path / journal_name(folder),
                                  data_dir, output)
                return path
    _, pending, _ = _row(campaign_id, data_dir)
    raise (FolderPendingError if pending is not None else FolderTakenError)(folder)


def _register_journal(conn: sqlite3.Connection, campaign_id: int, path: Path,
                      data_dir: Optional[Path], output: Path) -> None:
    """Register a re-claimed folder's journal (a keep-files delete left it untracked),
    so the campaign's delete finds it."""
    if path.is_file() and _is_wisper_journal(path):
        from . import file_registry

        file_registry.add_if_owned(path, kind="journal",
                                   owner=file_registry.Owner("campaign", campaign_id),
                                   conn=conn, data_dir=data_dir, output_dir=output)


def recreate_folder(campaign_id: int, *, data_dir: Optional[Path] = None,
                    output_dir: Optional[Path] = None) -> Path:
    """Make a claimed campaign's vanished folder again. Only the user's
    "Recreate folder" action calls this."""
    output = Path(output_dir) if output_dir is not None else get_output_root()
    if not output.is_dir():
        raise FileNotFoundError(f"output folder {output} is not available")
    folder, pending, claimed = _row(campaign_id, data_dir)
    if pending is not None:
        raise FolderPendingError(folder)
    if not claimed:
        raise FolderTakenError(folder)
    path = output / folder
    path.mkdir(exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenameOutcome:
    """What a campaign rename did.

    ``status`` is ``renamed``, or one of the refusal codes from the campaign
    table (``pending``, ``busy``, ``invalid``, ``slug_taken``, ``taken``,
    ``folder_exists``, ``folder_taken``, ``folder_missing``, ``reserved``,
    ``legacy_journal``). ``new_slug`` is set once the slug changed.
    """

    status: str
    new_slug: Optional[str] = None


def _active_jobs(conn: sqlite3.Connection, campaign_id: int, slug: str) -> list:
    from .job_history import active_jobs

    return active_jobs(conn, campaign_ids={campaign_id}, campaign_slugs={slug})


def rename_campaign(slug: str, new_display_name: str, *, data_dir: Optional[Path] = None,
                    output_dir: Optional[Path] = None) -> RenameOutcome:
    """Rename a campaign: display name, slug, folder, and journal file.

    The display name and slug change with the row; a claimed folder moves on
    disk first and the row's ``folder`` changes after (``folder_pending``
    carries it across a crash). An unclaimed campaign changes only in the
    database. A legacy journal's data-dir directory follows the slug change.
    Raises ``KeyError`` for an unknown slug; every refusal is returned as a
    status from the campaign table.
    """
    from .campaign_manager import _integrity_code, _make_slug, _validate_campaign_slug

    safe = _validate_campaign_slug(slug)
    if safe is None:
        return RenameOutcome("invalid")
    display = new_display_name.strip()
    new_slug = _make_slug(display) if display else ""
    if not display or not new_slug:
        return RenameOutcome("invalid")

    output = Path(output_dir) if output_dir is not None else get_output_root()
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT id, folder, folder_pending FROM campaigns WHERE slug = ?", (safe,)
        ).fetchone()
    if row is None:
        raise KeyError(f"Campaign {safe!r} not found")
    cid = row["id"]

    # An earlier rename still pending: finish it first, never overwrite it.
    if row["folder_pending"] is not None:
        if not finish_folder_rename(cid, data_dir=data_dir, output_dir=output):
            return RenameOutcome("pending", safe)

    new_folder = folder_name(display)
    with db.connection(data_dir) as conn:
        if conn.execute("SELECT 1 FROM campaigns WHERE slug = ? AND id <> ?",
                        (new_slug, cid)).fetchone():
            return RenameOutcome("slug_taken")
    code = check_available(new_folder, output_dir=output, exclude_id=cid)
    if code is not None:
        return RenameOutcome(code)

    legacy_dir: Optional[Path] = None
    legacy_target: Optional[Path] = None
    if new_slug != safe:
        from .journal import (
            _pending_path, adopt_legacy_journal, legacy_journal_path, sync_journal,
        )
        adopt_legacy_journal(safe, data_dir, output)
        sync_journal(safe, data_dir)
        legacy = legacy_journal_path(safe, data_dir)
        if legacy.is_file() or _pending_path(legacy).is_file():
            legacy_dir = legacy.parent
            legacy_target = legacy_dir.parent / new_slug
            if legacy_target.exists():
                return RenameOutcome("legacy_journal")
            try:
                os.replace(legacy_dir, legacy_target)
            except OSError as exc:
                log.warning("Could not move the legacy journal %s: %s", legacy_dir, exc)
                return RenameOutcome("legacy_journal")

    status = "retry"
    try:
        for attempt in range(2):
            try:
                status = _rename_attempt(cid, safe, display, new_slug, new_folder, data_dir)
            except sqlite3.IntegrityError as exc:
                status = _integrity_code(exc)
                break
            if status != "retry":
                break
        if status == "retry":
            # A claim changed twice under us; re-read rather than guess.
            with db.connection(data_dir) as conn:
                r = conn.execute(
                    "SELECT slug, folder_pending FROM campaigns WHERE id = ?", (cid,)
                ).fetchone()
            if r is None:
                raise KeyError(f"Campaign {cid} not found")
            if r["slug"] == new_slug:
                status = "renamed"
            elif r["folder_pending"] is not None:
                status = "pending"
            else:
                status = "busy"
        if status == "pending" and finish_folder_rename(
                cid, data_dir=data_dir, output_dir=output):
            status = "renamed"
    except BaseException:
        _legacy_back(legacy_dir, legacy_target)
        raise
    if status not in ("renamed", "pending"):
        _legacy_back(legacy_dir, legacy_target)
        return RenameOutcome(status)
    return RenameOutcome(status, new_slug)


def _legacy_back(legacy_dir: Optional[Path], legacy_target: Optional[Path]) -> None:
    if legacy_dir is None or legacy_target is None:
        return
    try:
        os.replace(legacy_target, legacy_dir)
    except OSError as exc:
        log.error("Could not move the legacy journal %s back: %s", legacy_target, exc)


def _rename_attempt(cid: int, slug: str, display: str, new_slug: str, new_folder: str,
                    data_dir: Optional[Path]) -> str:
    """One compare-and-swap rename attempt; ``retry`` when the row changed under it."""
    with db.transaction(data_dir) as conn:
        r = conn.execute(
            "SELECT folder, folder_pending, folder_claimed FROM campaigns WHERE id = ?",
            (cid,),
        ).fetchone()
        if r is None:
            raise KeyError(f"Campaign {cid} not found")
        if r["folder_pending"] is not None:
            return "pending"
        claimed = bool(r["folder_claimed"])
        if _active_jobs(conn, cid, slug):
            return "busy"
        if conn.execute("SELECT 1 FROM campaigns WHERE slug = ? AND id <> ?",
                        (new_slug, cid)).fetchone():
            return "slug_taken"
        if taken_by_campaign(conn, new_folder, exclude_id=cid):
            return "taken"
        reserved = {_fold(journal_name(new_folder)[:-len(".md")]),
                    _fold(combined_summary_name(new_folder)[:-len(".md")])}
        if any(_fold(s[0]) in reserved for s in conn.execute(
                "SELECT stem FROM transcripts WHERE campaign_id = ?", (cid,))):
            return "reserved"
        pending_folder = (new_folder if claimed and new_folder != r["folder"]
                          else None)
        if claimed:
            cur = conn.execute(
                "UPDATE campaigns SET display_name = ?, slug = ?, folder_pending = ? "
                "WHERE id = ? AND folder_pending IS NULL AND folder_claimed = 1",
                (display, new_slug, pending_folder, cid),
            )
        else:
            cur = conn.execute(
                "UPDATE campaigns SET display_name = ?, slug = ?, folder = ? "
                "WHERE id = ? AND folder_pending IS NULL AND folder_claimed = 0",
                (display, new_slug, new_folder, cid),
            )
        if cur.rowcount == 0:
            return "retry"
        return "pending" if pending_folder is not None else "renamed"


def finish_folder_rename(campaign_id: int, *, without_folder: bool = False,
                         data_dir: Optional[Path] = None,
                         output_dir: Optional[Path] = None) -> bool:
    """Finish a campaign folder rename: move the folder, then the database.

    Returns True when no rename is pending any more. The folder moves first,
    then one compare-and-swap commits ``folder = folder_pending`` and rewrites
    every ``files.rel_path`` under the old prefix. A crash between the two is
    recovered here: the disk is inspected first. ``without_folder=True`` is the
    "neither directory" case: the swap is committed database-only and the
    claim is dropped, to be re-made on first write.

    Returns False without committing when a job holds the campaign, the output
    root is absent, the disk isn't in a state the rename can finish, or another
    caller changed the row. Raises ``KeyError`` for an unknown id.
    """
    from .job_history import active_jobs

    try:
        with db.connection(data_dir) as conn:
            meta = conn.execute(
                "SELECT id, slug, folder, folder_pending FROM campaigns WHERE id = ?",
                (campaign_id,),
            ).fetchone()
    except sqlite3.OperationalError as exc:  # no table yet (a very early startup)
        log.warning("Could not read campaign %s to finish its rename: %s", campaign_id, exc)
        return False
    if meta is None:
        raise KeyError(f"Campaign {campaign_id} not found")
    old, new = meta["folder"], meta["folder_pending"]
    if new is None:
        return True

    # Busy (early exit). The transaction in step 5 checks again.
    with db.connection(data_dir) as conn:
        if active_jobs(conn, campaign_ids={campaign_id}, campaign_slugs={meta["slug"]}):
            return False

    output = Path(output_dir) if output_dir is not None else get_output_root()
    if not output.is_dir():
        return False  # an unmounted drive must never commit the swap

    with _LOCATION_LOCK:
        old_dir, new_dir = output / old, output / new
        renamed_here = False
        if not old_dir.exists() and not new_dir.exists():
            if not without_folder:
                return False
        elif without_folder:
            # A folder is on disk after all: finish normally, never database-only.
            without_folder = False
        if without_folder:
            pass
        elif not old_dir.exists() and new_dir.is_dir():
            # The crash-recovery case: check the target belongs to this rename.
            if not _all_belong_to(old_dir, new_dir, data_dir, output):
                return False
        elif old_dir.exists() and new_dir.exists() and os.path.samefile(old_dir, new_dir):
            try:
                os.rename(old_dir, new_dir)  # changes the case
            except OSError as exc:
                log.warning("Could not rename %s to %s: %s", old_dir, new_dir, exc)
                return False
            renamed_here = True
        elif old_dir.is_dir() and not new_dir.exists():
            if not _rename_with_retry(old_dir, new_dir, retries=3):
                return False
            renamed_here = True
        else:
            return False  # both present and different, or the old name is a file

        try:
            _swap_rename(campaign_id, old, new, meta["slug"], without_folder,
                         data_dir, output)
        except FolderBusy:
            if renamed_here:
                _rename_back(old_dir, new_dir, campaign_id, old, new, data_dir)
            return False
        except _DiskChanged:
            return False  # another process moved the folder back; nothing committed
        except _Taken:
            if renamed_here:
                _rename_back(old_dir, new_dir, campaign_id, old, new, data_dir)
            return False
        except BaseException:
            if renamed_here:
                _rename_back(old_dir, new_dir, campaign_id, old, new, data_dir)
            raise

        _rename_journal(campaign_id, output, old, new, data_dir)
        _rename_digests(campaign_id, output, old, new, data_dir)
    return True


class FolderBusy(Exception):
    """A job holds the campaign inside the commit transaction."""


def _rename_with_retry(old_dir: Path, new_dir: Path, *, retries: int) -> bool:
    for attempt in range(retries):
        try:
            os.rename(old_dir, new_dir)
            return True
        except OSError as exc:
            if attempt + 1 >= retries:
                log.warning("Could not rename %s to %s: %s", old_dir, new_dir, exc)
            else:
                time.sleep(0.5)
    return False


def _all_belong_to(old_dir: Path, new_dir: Path, data_dir: Optional[Path], output: Path) -> bool:
    """Whether ``new_dir`` holds only clutter and files registered under ``old/``.

    A folder holding a file name wisper doesn't know under the old prefix is a
    folder the user made, and is left alone.
    """
    from . import file_registry

    want_prefix = file_registry._key(old_dir.name, True)
    with db.connection(data_dir) as conn:
        registered = {
            file_registry._key(r["rel_path"].partition("/")[2].partition("/")[0], True)
            for r in conn.execute("SELECT rel_path FROM files WHERE root = 'output'")
            if file_registry._key(r["rel_path"].partition("/")[0], True) == want_prefix
        }
    for name in os.listdir(new_dir):
        if not is_clutter(name) and file_registry._key(name, True) not in registered:
            return False
    return True


def _swap_rename(campaign_id: int, old: str, new: str, slug: str, without_folder: bool,
                 data_dir: Optional[Path], output: Path) -> None:
    """Commit the swap and rewrite the prefix, in one transaction."""
    from .job_history import active_jobs

    old_dir, new_dir = output / old, output / new
    with db.transaction(data_dir) as conn:
        if active_jobs(conn, campaign_ids={campaign_id}, campaign_slugs={slug}):
            raise FolderBusy()
        if not without_folder and not (
                new_dir.is_dir() and (not old_dir.exists() or os.path.samefile(old_dir, new_dir))):
            raise _DiskChanged()
        cur = conn.execute(
            "UPDATE campaigns SET folder = folder_pending, folder_pending = NULL, "
            "folder_claimed = CASE WHEN :without THEN 0 ELSE folder_claimed END "
            "WHERE id = :id AND folder = :old COLLATE BINARY "
            "AND folder_pending = :new COLLATE BINARY RETURNING folder",
            {"without": 1 if without_folder else 0, "id": campaign_id,
             "old": old, "new": new},
        )
        row = cur.fetchone()
        if row is None:
            return  # another caller finished it; nothing to touch
        committed = row[0]
        if taken_by_campaign(conn, old, exclude_id=campaign_id):
            raise _Taken()
        _rewrite_prefix(conn, old, committed)
        _assert_prefix(conn, old, committed)


class _DiskChanged(Exception):
    """The folder moved between the disk check and the write lock."""


class _Taken(Exception):
    """Another campaign claims the old name (ASCII-NOCASE compare)."""


def _rewrite_prefix(conn: sqlite3.Connection, old: str, new: str) -> None:
    from . import file_registry

    rows = conn.execute(
        "SELECT id, rel_path FROM files WHERE root = 'output' AND instr(rel_path, '/') > 0"
    ).fetchall()
    for row in rows:
        component, _, rest = row["rel_path"].partition("/")
        if file_registry._key(component, fold=True) == file_registry._key(old, fold=True):
            file_registry.repoint_rel(conn, row["id"], f"{new}/{rest}")


def _assert_prefix(conn: sqlite3.Connection, old: str, new: str) -> None:
    """The same transaction must leave no output row folding to ``old``."""
    from . import file_registry

    want = file_registry._key(old, fold=True)
    rows = conn.execute(
        "SELECT rel_path FROM files WHERE root = 'output' AND instr(rel_path, '/') > 0"
    ).fetchall()
    for row in rows:
        component = row["rel_path"].partition("/")[0]
        if file_registry._key(component, fold=True) == want and component != new:
            raise RuntimeError(f"folder prefix not rewritten: {row['rel_path']!r}")


def _rename_back(old_dir: Path, new_dir: Path, campaign_id: int, old: str, new: str,
                 data_dir: Optional[Path]) -> None:
    """Undo a rename whose database commit raised, while holding the lock."""
    with db.transaction(data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM campaigns WHERE id = :id AND folder = :old COLLATE BINARY "
            "AND folder_pending = :new COLLATE BINARY",
            {"old": old, "new": new, "id": campaign_id},
        ).fetchone()
        if row is None:
            return
        try:
            os.rename(new_dir, old_dir)
        except OSError as exc:
            log.error("Could not rename %s back to %s: %s", new_dir, old_dir, exc)


def _rename_journal(campaign_id: int, output: Path, old_folder: str, new_folder: str,
                    data_dir: Optional[Path]) -> None:
    """Move the journal file to the renamed folder's journal name (best effort).

    A journal still under the old name but unregistered (a crash between its
    write and its registration) is registered first: left behind, the next
    ``sync_journal`` would find no journal at the new name and reset it.
    """
    from . import file_registry

    owner = file_registry.Owner("campaign", campaign_id)
    with db.transaction(data_dir) as conn:
        row = file_registry.file_for(owner, "journal", conn=conn, data_dir=data_dir,
                                     output_dir=output)
        if row is None:
            stray = output / new_folder / journal_name(old_folder)
            if not (stray.is_file() and _is_wisper_journal(stray)):
                return
            file_registry.add_if_owned(stray, kind="journal", owner=owner, conn=conn,
                                       data_dir=data_dir, output_dir=output)
            row = file_registry.file_for(owner, "journal", conn=conn, data_dir=data_dir,
                                         output_dir=output)
            if row is None:
                return
    target_name = journal_name(new_folder)
    if row.path.name == target_name:
        return
    if row.path.parent.name != new_folder:
        return  # a misplaced journal: reconcile lists it, don't move it here
    file_registry.move(row, row.path.parent / target_name,
                       data_dir=data_dir, output_dir=output)


def _rename_digests(campaign_id: int, output: Path, old_folder: str, new_folder: str,
                    data_dir: Optional[Path]) -> None:
    """Rename the combined summary and each recap to the new folder's name.

    A digest is named after the folder, so a folder rename renames the file too
    (the same way the journal does). Best effort: a file that can't move keeps
    its name and row for the next reconcile.
    """
    from . import file_registry

    owner = file_registry.Owner("campaign", campaign_id)
    rows = file_registry.files_for(owner, data_dir=data_dir, output_dir=output)
    for row in rows:
        if row.kind == "combined_summary":
            target_name = combined_summary_name(new_folder)
        elif row.kind == "recap" and row.label:
            target_name = recap_name(new_folder, row.label)
        else:
            continue
        if row.path.name == target_name or row.path.parent.name != new_folder:
            continue
        file_registry.move(row, row.path.parent / target_name,
                           data_dir=data_dir, output_dir=output)


def finish_pending_renames(data_dir: Optional[Path] = None) -> None:
    """Finish every campaign's pending folder rename. Never raises, never loops.

    Startup calls this before reconcile. A rename that can't finish (a locked
    folder, an absent output root, a job) logs and stays pending for Needs
    attention; the next startup or Retry finishes it.
    """
    try:
        with db.connection(data_dir) as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM campaigns WHERE folder_pending IS NOT NULL ORDER BY id")]
    except sqlite3.Error:
        log.warning("Could not list campaigns with a pending rename", exc_info=True)
        return
    for campaign_id in ids:
        try:
            finish_folder_rename(campaign_id, data_dir=data_dir)
        except Exception:
            log.exception("Could not finish the folder rename for campaign %s", campaign_id)


def claim_folder(campaign_id: int, conn: Optional[sqlite3.Connection] = None, *,
                 data_dir: Optional[Path] = None) -> None:
    """Claim an existing non-empty folder. Only the user's "Use this folder"
    action calls this, after confirming every ``.md`` in it becomes a session."""
    def run(c: sqlite3.Connection) -> None:
        row = c.execute(
            "SELECT folder, folder_pending FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Campaign {campaign_id} not found")
        if not _claim_cas(c, campaign_id, row[0]):
            raise FolderPendingError(row[0])

    if conn is not None:
        run(conn)
    else:
        with db.transaction(data_dir) as c:
            run(c)


def rmdir_if_empty(folder: str, *, output_dir: Optional[Path] = None) -> bool:
    """Remove ``folder`` under the output root only when it holds nothing.

    Never raises: a non-empty folder, an absent one, and an ``OSError`` all
    leave it in place. Returns whether it was removed.
    """
    output = Path(output_dir) if output_dir is not None else get_output_root()
    path = inside(output, folder)
    if path is None:
        return False
    try:
        path.rmdir()
        return True
    except OSError:
        return False


def is_claimed(campaign_id: int, conn: Optional[sqlite3.Connection] = None, *,
               data_dir: Optional[Path] = None) -> bool:
    sql = "SELECT folder_claimed FROM campaigns WHERE id = ?"
    if conn is not None:
        row = conn.execute(sql, (campaign_id,)).fetchone()
    else:
        with db.connection(data_dir) as c:
            row = c.execute(sql, (campaign_id,)).fetchone()
    return bool(row and row[0])
