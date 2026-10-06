"""Campaign manager — campaigns, rosters, and transcript order.

Campaigns are an optional layer over the global speaker profiles: a roster of
profiles with per-campaign role/character overrides and Discord bindings, plus
the ordered list of the campaign's transcripts.

Data lives in ``wisper.db`` (tables ``campaigns``, ``campaign_members``, and
each transcript's ``campaign_id``/``position``); each campaign's journal is a
file in its folder under the output root. Every change is one ``db.transaction()``, so
concurrent requests and processes can't lose each other's updates.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from . import campaign_folders, db
from .config import get_data_dir, get_output_root
from .models import Campaign, CampaignMember
from .path_utils import validate_path_component


class CampaignError(ValueError):
    """A campaign name couldn't be used, with a code from the campaign table."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class DeleteOutcome:
    """What a campaign delete did.

    ``status`` is ``deleted``, ``busy`` (a job holds the campaign),
    ``kept`` (a session's file couldn't be deleted or moved), or
    ``delete_incomplete`` (a reconcile registered a new session mid-delete).
    ``kept`` names the sessions left behind.
    """

    status: Literal["deleted", "busy", "kept", "delete_incomplete"]
    kept: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_campaigns_dir(data_dir: Optional[Path] = None) -> Path:
    """Parent of the per-campaign ``<slug>/`` folders (journals)."""
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "campaigns"


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def _make_slug(name: str) -> str:
    """Convert a display name to a URL/filesystem-safe slug."""
    return re.sub(r"[^\w]+", "-", name.lower()).strip("-")


def _validate_campaign_slug(slug: str) -> Optional[str]:
    return validate_path_component(slug, "_campaigns_guard")


def _validate_profile_key(profile_key: str) -> Optional[str]:
    return validate_path_component(profile_key, "_guard")


def _nfc(stem: str) -> str:
    return unicodedata.normalize("NFC", stem)


# ---------------------------------------------------------------------------
# Row helpers (all take an open connection)
# ---------------------------------------------------------------------------

def _campaign_id(conn: sqlite3.Connection, slug: str) -> int:
    row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise KeyError(f"Campaign {slug!r} not found")
    return row[0]


def _profile_id(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT id FROM profiles WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(f"Speaker profile {key!r} not found")
    return row[0]


def _transcript_id(conn: sqlite3.Connection, campaign_id: int, stem: str) -> int:
    """The id of the campaign's session named ``stem``. Raises KeyError if absent."""
    row = conn.execute(
        "SELECT id FROM transcripts WHERE campaign_id = ? AND stem = ?", (campaign_id, stem)
    ).fetchone()
    if row is None:
        raise KeyError(f"Transcript {stem!r} is not in the campaign")
    return row[0]


def _assign(conn: sqlite3.Connection, tid: int, campaign_id: Optional[int],
            position: Optional[int], stem: Optional[str] = None) -> None:
    """Set a session's campaign slot, and its stem when ``stem`` is given.

    The one primitive that writes a session's assignment; only
    ``transcript_store.move_transcript`` moves a session between campaigns. A
    name already taken in the target (a campaign, or the root) violates a
    partial unique index; that is reported as a ValueError. The ``stem`` column
    is written only when it changes, so a plain assignment doesn't re-fire the
    title trigger.
    """
    row = conn.execute("SELECT stem FROM transcripts WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise KeyError(f"No transcript with id {tid}")
    new_stem = row[0] if stem is None else stem
    try:
        if new_stem != row[0]:
            conn.execute(
                "UPDATE transcripts SET campaign_id = ?, position = ?, stem = ? WHERE id = ?",
                (campaign_id, position, new_stem, tid),
            )
        else:
            conn.execute(
                "UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?",
                (campaign_id, position, tid),
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"a session named {new_stem!r} is already there") from None


def _write_order(conn: sqlite3.Connection, campaign_id: int, transcript_ids: list[int]) -> None:
    """Set the campaign's rows to ``transcript_ids``, in that order.

    Every id must already belong to the campaign (``ValueError`` otherwise):
    moving a session in or out goes through ``move_transcript``, so this never
    changes membership and never unassigns a row it wasn't given.
    ``UNIQUE(campaign_id, position)`` is checked row by row, so positions are
    written in two steps: shift every row above the current maximum, then write
    the final positions.
    """
    for tid in transcript_ids:
        row = conn.execute("SELECT campaign_id FROM transcripts WHERE id = ?", (tid,)).fetchone()
        if row is None or row[0] != campaign_id:
            raise ValueError(f"transcript {tid} is not in this campaign")
    shift = conn.execute(
        "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()[0] + len(transcript_ids)
    conn.execute(
        "UPDATE transcripts SET position = position + ? WHERE campaign_id = ?",
        (shift, campaign_id),
    )
    for pos, tid in enumerate(transcript_ids):
        conn.execute("UPDATE transcripts SET position = ? WHERE id = ?", (pos, tid))


def _stems(conn: sqlite3.Connection, campaign_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT stem FROM transcripts WHERE campaign_id = ? ORDER BY position",
        (campaign_id,),
    )]


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_campaigns(data_dir: Optional[Path] = None) -> dict[str, Campaign]:
    """All campaigns by slug, with rosters and ordered transcript stems."""
    with db.connection(data_dir) as conn:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
        members = conn.execute(
            "SELECT m.campaign_id, p.key, m.role, m.character, m.discord_user_id "
            "FROM campaign_members m JOIN profiles p ON p.id = m.profile_id ORDER BY m.rowid"
        ).fetchall()
        transcripts = conn.execute(
            "SELECT campaign_id, id, stem FROM transcripts "
            "WHERE campaign_id IS NOT NULL ORDER BY campaign_id, position"
        ).fetchall()

    by_id: dict[int, Campaign] = {}
    campaigns: dict[str, Campaign] = {}
    for r in rows:
        c = Campaign(
            slug=r["slug"],
            display_name=r["display_name"],
            created=r["created_at"][:10],
            members={},
            transcripts=[],
            id=r["id"],
            folder=r["folder"],
        )
        by_id[r["id"]] = c
        campaigns[c.slug] = c
    for m in members:
        by_id[m["campaign_id"]].members[m["key"]] = CampaignMember(
            profile_key=m["key"], role=m["role"], character=m["character"],
            discord_user_id=m["discord_user_id"],
        )
    for t in transcripts:
        by_id[t["campaign_id"]].transcripts.append(t["stem"])
        by_id[t["campaign_id"]].transcript_ids.append(t["id"])
    return campaigns


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_campaign(display_name: str, data_dir: Optional[Path] = None, *,
                    output_dir: Optional[Path] = None) -> Campaign:
    """Create a new campaign and claim its folder.

    Raises ``CampaignError`` with a code from the campaign table (``invalid``,
    ``slug_taken``, ``taken``, ``folder_exists``). The folder is claimed after
    the row commits: if that can't happen (an absent output root, or a folder
    that appeared meanwhile) the campaign still exists and its folder is made
    and claimed on first write.
    """
    display_name = display_name.strip()
    if not display_name:
        raise CampaignError("invalid", "Campaign display name cannot be empty")

    slug = _make_slug(display_name)
    if not slug:
        raise CampaignError("invalid", f"Cannot derive a valid slug from name: {display_name!r}")

    output = Path(output_dir) if output_dir is not None else get_output_root()
    folder = campaign_folders.folder_name(display_name)
    # The folder_exists disk check can't be atomic; a folder appearing in between
    # is caught by ensure_folder. The database check runs inside the transaction.
    code = campaign_folders.check_available(folder, output_dir=output)
    if code is not None:
        raise CampaignError(code, f"A campaign folder named {folder!r} already exists")

    created = db.now_utc()
    try:
        with db.transaction(data_dir) as conn:
            if conn.execute("SELECT 1 FROM campaigns WHERE slug = ?", (slug,)).fetchone():
                raise CampaignError("slug_taken", f"Campaign with slug {slug!r} already exists")
            if campaign_folders.taken_by_campaign(conn, folder):
                raise CampaignError("taken", f"A campaign folder named {folder!r} already exists")
            cid = conn.execute(
                "INSERT INTO campaigns (slug, display_name, folder, created_at) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (slug, display_name, folder, created),
            ).fetchone()[0]
    except sqlite3.IntegrityError as exc:
        raise CampaignError(_integrity_code(exc), str(exc)) from None
    try:
        campaign_folders.ensure_folder(cid, data_dir=data_dir, output_dir=output)
    except (campaign_folders.FolderTakenError, OSError):
        pass  # claimed on first write
    return Campaign(slug=slug, display_name=display_name, created=created[:10], members={},
                    id=cid, folder=folder)


def _integrity_code(exc: sqlite3.IntegrityError) -> str:
    """Map a campaign write's ``IntegrityError`` to a campaign table code."""
    text = str(exc)
    if "slug" in text:
        return "slug_taken"
    return "taken"


def delete_campaign(slug: str, *, delete_transcripts: bool = False,
                    data_dir: Optional[Path] = None,
                    output_dir: Optional[Path] = None) -> DeleteOutcome:
    """Delete a campaign and handle its folder. Raises KeyError if not found.

    Both ways first refuse while a job is pending or running for the campaign
    (``busy``). With ``delete_transcripts`` each session goes through
    ``transcript_store.delete_transcript``; a session whose ``.md`` can't be
    deleted (held open) stops the delete and the campaign stays with what's
    left (``kept``). Otherwise each session moves to the output root (a clash
    becomes ``<name> (2)``); any session that can't move stops the delete
    (``kept``). Only an empty campaign is deleted, so a session left behind
    keeps the campaign and its folder.

    Delete everything deletes the journal file, then the campaign; a claimed
    folder is removed only when nothing is left in it. Keep the files leaves
    the journal file and its folder on disk, untracked; either way a claimed
    folder left empty is removed. Profiles are untouched.
    """
    from . import file_registry
    from .job_history import active_jobs

    output = Path(output_dir) if output_dir is not None else get_output_root()
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)  # KeyError before anything is deleted
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM transcripts WHERE campaign_id = ? ORDER BY position", (cid,))]
        stems = {r[0]: r[1] for r in conn.execute(
            "SELECT id, stem FROM transcripts WHERE campaign_id = ?", (cid,))}
        claimed = bool(conn.execute(
            "SELECT folder_claimed FROM campaigns WHERE id = ?", (cid,)).fetchone()[0])
        folder = conn.execute("SELECT folder FROM campaigns WHERE id = ?", (cid,)).fetchone()[0]
        if active_jobs(conn, campaign_ids={cid}, campaign_slugs={slug}):
            return DeleteOutcome("busy")

    if delete_transcripts:
        from .transcript_store import delete_transcript

        kept = []
        for tid in ids:
            if delete_transcript(tid, data_dir=data_dir, output_dir=output) == "kept":
                kept.append(stems[tid])
                break
        if kept:
            return DeleteOutcome("kept", kept)
    else:
        from .transcript_store import move_transcript

        kept = []
        for tid in ids:
            outcome = move_transcript(tid, None, clash="keep_both",
                                      data_dir=data_dir, output_dir=output)
            if outcome.status not in ("moved", "unchanged"):
                kept.append(stems[tid])
        if kept:
            return DeleteOutcome("kept", kept)

    journal_files: list[Path] = []
    digest_files: list[Path] = []
    try:
        with db.transaction(data_dir) as conn:
            cid = _campaign_id(conn, slug)
            owner = file_registry.Owner("campaign", cid)
            # A campaign's digests are derived and campaign-specific, so they go
            # with it in both modes (the journal file, by contrast, stays when the
            # files are kept). Their rows go too; deleting the files rows cascades
            # the campaign_digests rows.
            for kind in ("combined_summary", "recap"):
                digest_files += file_registry.forget_kind(
                    owner, kind, conn=conn, data_dir=data_dir, output_dir=output)
            if delete_transcripts:
                journal_files = file_registry.paths_for_delete(owner, conn, data_dir=data_dir,
                                                               output_dir=output)
            conn.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
    except sqlite3.IntegrityError:
        # A reconcile in another process registered a new .md in the folder.
        return DeleteOutcome("delete_incomplete")

    if journal_files or digest_files:
        file_registry.unlink_paths([*journal_files, *digest_files])
    if claimed:
        # Either way an empty folder goes, so re-creating the campaign isn't
        # refused by a folder wisper left; one holding the journal stays.
        campaign_folders.rmdir_if_empty(folder, output_dir=output)
    # A later campaign with this slug must not adopt this one's journal.
    from .journal import ADOPTED_SUFFIX, _pending_path, legacy_journal_path

    legacy = legacy_journal_path(slug, data_dir)
    for path in (legacy, _pending_path(legacy), legacy.with_name(legacy.name + ADOPTED_SUFFIX)):
        try:
            path.unlink()
        except OSError:
            pass
    return DeleteOutcome("deleted")


def add_member(
    slug: str,
    profile_key: str,
    role: str = "",
    character: str = "",
    data_dir: Optional[Path] = None,
) -> None:
    """Add or replace a profile's membership in a campaign.

    Replacing resets the Discord binding, as re-adding always has. Raises
    KeyError if the campaign or the profile doesn't exist.
    """
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)
        pid = _profile_id(conn, profile_key)
        conn.execute(
            "INSERT INTO campaign_members (campaign_id, profile_id, role, character) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (campaign_id, profile_id) DO UPDATE SET "
            "role = excluded.role, character = excluded.character, discord_user_id = NULL",
            (cid, pid, role, character),
        )


def remove_member(slug: str, profile_key: str, data_dir: Optional[Path] = None) -> None:
    """Remove a profile from a campaign roster. No-op if profile not in roster."""
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)
        conn.execute(
            "DELETE FROM campaign_members WHERE campaign_id = ? AND profile_id = "
            "(SELECT id FROM profiles WHERE key = ?)",
            (cid, profile_key),
        )


def get_campaign_profile_keys(slug: str, data_dir: Optional[Path] = None) -> set[str]:
    """Return the set of profile keys enrolled in a campaign. Empty set if slug unknown."""
    with db.connection(data_dir) as conn:
        return {r[0] for r in conn.execute(
            "SELECT p.key FROM campaign_members m JOIN profiles p ON p.id = m.profile_id "
            "JOIN campaigns c ON c.id = m.campaign_id WHERE c.slug = ?",
            (slug,),
        )}


# ---------------------------------------------------------------------------
# Discord ID binding
# ---------------------------------------------------------------------------

def bind_discord_id(
    slug: str,
    profile_key: str,
    discord_user_id: Optional[str],
    data_dir: Optional[Path] = None,
) -> None:
    """Bind or clear the Discord user ID for a campaign member.

    Enforces one-to-one mapping: if discord_user_id is already bound to another
    member in the same campaign, that existing binding is cleared first
    (``UNIQUE(campaign_id, discord_user_id)`` backs this up).
    Pass discord_user_id=None to clear the binding.
    Raises KeyError if the campaign or member is not found, ValueError if the
    id isn't a numeric Discord snowflake.
    """
    discord_user_id = discord_user_id or None
    if discord_user_id is not None and not discord_user_id.isdigit():
        raise ValueError("Discord user id must be numeric")
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)
        row = conn.execute(
            "SELECT m.profile_id FROM campaign_members m JOIN profiles p ON p.id = m.profile_id "
            "WHERE m.campaign_id = ? AND p.key = ?",
            (cid, profile_key),
        ).fetchone()
        if row is None:
            raise KeyError(f"Member {profile_key!r} not in campaign {slug!r}")
        if discord_user_id is not None:
            conn.execute(
                "UPDATE campaign_members SET discord_user_id = NULL "
                "WHERE campaign_id = ? AND discord_user_id = ? AND profile_id <> ?",
                (cid, discord_user_id, row[0]),
            )
        conn.execute(
            "UPDATE campaign_members SET discord_user_id = ? WHERE campaign_id = ? AND profile_id = ?",
            (discord_user_id, cid, row[0]),
        )


def lookup_profile_by_discord_id(
    slug: str,
    discord_user_id: str,
    data_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the profile_key bound to discord_user_id in the given campaign, or None."""
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT p.key FROM campaign_members m JOIN profiles p ON p.id = m.profile_id "
            "JOIN campaigns c ON c.id = m.campaign_id WHERE c.slug = ? AND m.discord_user_id = ?",
            (slug, discord_user_id),
        ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Transcript association
# ---------------------------------------------------------------------------


def reorder_campaign_transcript(
    slug: str, stem: str, direction: str, data_dir: Optional[Path] = None
) -> None:
    """Move one transcript one position up or down in a campaign's transcript
    order.

    This order is the campaign's source of truth for session sequence —
    ``get_transcripts_for_campaign()`` (and so ``unjournalled_sessions()``/
    ``rebuild_campaign()`` in journal.py) fold sessions into the journal in
    this order, not by any date parsed from the filename. A no-op if the
    transcript is already at that end of the list.

    Raises:
        KeyError: campaign not found.
        ValueError: invalid direction, or stem not in the campaign.
    """
    if direction not in ("up", "down"):
        raise ValueError(f"Invalid direction: {direction!r} (must be 'up' or 'down')")

    stem = _nfc(stem)
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)
        stems = _stems(conn, cid)
        if stem not in stems:
            raise ValueError(f"Transcript {stem!r} is not in campaign {slug!r}")
        idx = stems.index(stem)
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(stems):
            stems[idx], stems[swap_idx] = stems[swap_idx], stems[idx]
            _write_order(conn, cid, [_transcript_id(conn, cid, s) for s in stems])


def set_campaign_transcript_order(
    slug: str, order: list[str], data_dir: Optional[Path] = None
) -> None:
    """Replace a campaign's whole transcript order in one call.

    ``order`` must be exactly a permutation of the campaign's current
    transcripts (same set, no additions/removals) — a session joins or leaves a
    campaign only through ``transcript_store.move_transcript``. Bulk
    counterpart to ``reorder_campaign_transcript``'s single-step move, for
    fixing a badly-out-of-order campaign (e.g. from the CLI) without many
    individual up/down calls.

    Raises:
        KeyError: campaign not found.
        ValueError: ``order`` is not a permutation of the current transcripts.
    """
    order = [_nfc(s) for s in order]
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)
        current = _stems(conn, cid)
        if sorted(order) != sorted(current):
            raise ValueError(
                "order must be a permutation of the campaign's current transcripts "
                f"(got {sorted(order)!r}, expected {sorted(current)!r})"
            )
        _write_order(conn, cid, [_transcript_id(conn, cid, s) for s in order])


def get_campaign_for_transcript(transcript_id: int, data_dir: Optional[Path] = None) -> Optional[str]:
    """Return the slug of the campaign that owns this transcript id, or None."""
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT c.slug FROM transcripts t JOIN campaigns c ON c.id = t.campaign_id "
            "WHERE t.id = ?",
            (transcript_id,),
        ).fetchone()
    return row[0] if row else None


def get_transcripts_for_campaign(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Return the ordered transcript stems for a campaign. Empty list if slug unknown."""
    with db.connection(data_dir) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
        return _stems(conn, row[0]) if row else []
