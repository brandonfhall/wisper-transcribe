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
import unicodedata
from pathlib import Path
from typing import Optional

from . import db
from .config import get_output_root

log = logging.getLogger(__name__)

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


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


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
                return path
    _, pending, _ = _row(campaign_id, data_dir)
    raise (FolderPendingError if pending is not None else FolderTakenError)(folder)


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


def is_claimed(campaign_id: int, conn: Optional[sqlite3.Connection] = None, *,
               data_dir: Optional[Path] = None) -> bool:
    sql = "SELECT folder_claimed FROM campaigns WHERE id = ?"
    if conn is not None:
        row = conn.execute(sql, (campaign_id,)).fetchone()
    else:
        with db.connection(data_dir) as c:
            row = c.execute(sql, (campaign_id,)).fetchone()
    return bool(row and row[0])
