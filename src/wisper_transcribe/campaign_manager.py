"""Campaign manager — campaigns, rosters, and transcript order.

Campaigns are an optional layer over the global speaker profiles: a roster of
profiles with per-campaign role/character overrides and Discord bindings, plus
the ordered list of the campaign's transcripts.

Data lives in ``wisper.db`` (tables ``campaigns``, ``campaign_members``,
``campaign_transcripts``); each campaign's journal stays a file under
``$DATA_DIR/campaigns/<slug>/``. Every change is one ``db.transaction()``, so
concurrent requests and processes can't lose each other's updates.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Optional

from . import db
from .config import get_data_dir
from .models import Campaign, CampaignMember
from .path_utils import validate_path_component


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


def _transcript_id(conn: sqlite3.Connection, stem: str) -> int:
    """The registry row for ``stem``, created if absent."""
    from .legacy_import import ensure_transcript_row
    from .path_utils import get_output_dir

    return ensure_transcript_row(conn, _nfc(stem), get_output_dir())


def _write_order(conn: sqlite3.Connection, campaign_id: int, transcript_ids: list[int]) -> None:
    """Set the campaign's rows to exactly ``transcript_ids``, in that order.

    ``UNIQUE(campaign_id, position)`` is checked row by row, so positions are
    written in two steps: shift every row above the current maximum, then
    write the final positions.
    """
    keep = set(transcript_ids)
    for (tid,) in conn.execute(
        "SELECT transcript_id FROM campaign_transcripts WHERE campaign_id = ?", (campaign_id,)
    ).fetchall():
        if tid not in keep:
            conn.execute("DELETE FROM campaign_transcripts WHERE transcript_id = ?", (tid,))
    top = conn.execute(
        "SELECT coalesce(max(position), -1) + 1 FROM campaign_transcripts WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()[0]
    offset = top + len(transcript_ids)
    conn.execute(
        "UPDATE campaign_transcripts SET position = position + ? WHERE campaign_id = ?",
        (offset, campaign_id),
    )
    for pos, tid in enumerate(transcript_ids):
        # A transcript in another campaign moves here (one campaign per transcript).
        conn.execute(
            "INSERT INTO campaign_transcripts (transcript_id, campaign_id, position) "
            "VALUES (?, ?, ?) ON CONFLICT (transcript_id) DO UPDATE SET "
            "campaign_id = excluded.campaign_id, position = excluded.position",
            (tid, campaign_id, pos),
        )


def _stems(conn: sqlite3.Connection, campaign_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT t.stem FROM campaign_transcripts ct JOIN transcripts t ON t.id = ct.transcript_id "
        "WHERE ct.campaign_id = ? ORDER BY ct.position",
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
            "SELECT ct.campaign_id, t.stem FROM campaign_transcripts ct "
            "JOIN transcripts t ON t.id = ct.transcript_id ORDER BY ct.campaign_id, ct.position"
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
    return campaigns


def save_campaigns(campaigns: dict[str, Campaign], data_dir: Optional[Path] = None) -> None:
    """Make the campaign store exactly ``campaigns``, in one transaction.

    Campaigns are matched by slug and updated in place; slugs not in
    ``campaigns`` are deleted. Members must be existing profiles (``KeyError``
    otherwise); a transcript listed here moves out of any other campaign.
    Prefer the targeted functions below; this whole-store form remains for
    callers and tests that build the store directly.
    """
    with db.transaction(data_dir) as conn:
        existing = {r[0] for r in conn.execute("SELECT slug FROM campaigns")}
        for slug in existing - set(campaigns):
            conn.execute("DELETE FROM campaigns WHERE slug = ?", (slug,))
        for slug, c in campaigns.items():
            created = c.created if len(c.created or "") > 10 else (
                f"{c.created}T00:00:00Z" if c.created else db.now_utc()
            )
            cid = conn.execute(
                "INSERT INTO campaigns (slug, display_name, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (slug) DO UPDATE SET display_name = excluded.display_name, "
                "created_at = excluded.created_at RETURNING id",
                (slug, c.display_name or slug, created),
            ).fetchone()[0]
            conn.execute("DELETE FROM campaign_members WHERE campaign_id = ?", (cid,))
            for key, m in c.members.items():
                conn.execute(
                    "INSERT INTO campaign_members (campaign_id, profile_id, role, character, "
                    "discord_user_id) VALUES (?, ?, ?, ?, ?)",
                    (cid, _profile_id(conn, key), m.role or "", m.character or "",
                     m.discord_user_id or None),
                )
            _write_order(conn, cid, [_transcript_id(conn, st) for st in c.transcripts])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_campaign(display_name: str, data_dir: Optional[Path] = None) -> Campaign:
    """Create a new campaign. Raises ValueError for empty name or duplicate slug."""
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("Campaign display name cannot be empty")

    slug = _make_slug(display_name)
    if not slug:
        raise ValueError(f"Cannot derive a valid slug from name: {display_name!r}")

    created = db.now_utc()
    with db.transaction(data_dir) as conn:
        if conn.execute("SELECT 1 FROM campaigns WHERE slug = ?", (slug,)).fetchone():
            raise ValueError(f"Campaign with slug {slug!r} already exists")
        conn.execute(
            "INSERT INTO campaigns (slug, display_name, created_at) VALUES (?, ?, ?)",
            (slug, display_name, created),
        )
    return Campaign(slug=slug, display_name=display_name, created=created[:10], members={})


def delete_campaign(slug: str, data_dir: Optional[Path] = None) -> None:
    """Delete a campaign and its roster and order. Raises KeyError if not found.

    Profiles and transcripts are untouched.
    """
    with db.transaction(data_dir) as conn:
        if conn.execute("DELETE FROM campaigns WHERE slug = ?", (slug,)).rowcount == 0:
            raise KeyError(f"Campaign {slug!r} not found")


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


def move_transcript_to_campaign(
    stem: str, slug: str, data_dir: Optional[Path] = None
) -> None:
    """Associate a transcript stem with a campaign, appended at the end.

    Removes it from any other campaign first (one transcript → one campaign;
    the schema enforces it). A no-op if it's already in this campaign.
    Raises KeyError if the target campaign slug is not found.
    """
    with db.transaction(data_dir) as conn:
        cid = _campaign_id(conn, slug)
        tid = _transcript_id(conn, stem)
        current = conn.execute(
            "SELECT campaign_id FROM campaign_transcripts WHERE transcript_id = ?", (tid,)
        ).fetchone()
        if current is not None and current[0] == cid:
            return
        conn.execute("DELETE FROM campaign_transcripts WHERE transcript_id = ?", (tid,))
        conn.execute(
            "INSERT INTO campaign_transcripts (transcript_id, campaign_id, position) "
            "VALUES (?, ?, (SELECT coalesce(max(position), -1) + 1 FROM campaign_transcripts "
            "WHERE campaign_id = ?))",
            (tid, cid, cid),
        )


def remove_transcript_from_campaign(stem: str, data_dir: Optional[Path] = None) -> None:
    """Disassociate a transcript stem from whichever campaign it belongs to (no-op if none)."""
    with db.transaction(data_dir) as conn:
        conn.execute(
            "DELETE FROM campaign_transcripts WHERE transcript_id = "
            "(SELECT id FROM transcripts WHERE stem = ?)",
            (_nfc(stem),),
        )


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
            _write_order(conn, cid, [_transcript_id(conn, s) for s in stems])


def set_campaign_transcript_order(
    slug: str, order: list[str], data_dir: Optional[Path] = None
) -> None:
    """Replace a campaign's whole transcript order in one call.

    ``order`` must be exactly a permutation of the campaign's current
    transcripts (same set, no additions/removals) — use
    ``move_transcript_to_campaign``/``remove_transcript_from_campaign`` to
    change membership. Bulk counterpart to ``reorder_campaign_transcript``'s
    single-step move, for fixing a badly-out-of-order campaign (e.g. from
    the CLI) without many individual up/down calls.

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
        _write_order(conn, cid, [_transcript_id(conn, s) for s in order])


def get_campaign_for_transcript(stem: str, data_dir: Optional[Path] = None) -> Optional[str]:
    """Return the slug of the campaign that owns this transcript stem, or None."""
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT c.slug FROM transcripts t JOIN campaign_transcripts ct ON ct.transcript_id = t.id "
            "JOIN campaigns c ON c.id = ct.campaign_id WHERE t.stem = ?",
            (_nfc(stem),),
        ).fetchone()
    return row[0] if row else None


def get_transcripts_for_campaign(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Return the ordered transcript stems for a campaign. Empty list if slug unknown."""
    with db.connection(data_dir) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
        return _stems(conn, row[0]) if row else []
