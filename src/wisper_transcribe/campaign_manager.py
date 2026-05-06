"""Campaign manager — CRUD for per-campaign speaker rosters.

Campaigns are an optional layer over the global speaker profile store.
Voice embeddings remain global (one .npy per person); campaigns hold
roster references with per-campaign role/character overrides.

Data lives at:
    $DATA_DIR/campaigns/campaigns.json
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from .config import get_data_dir
from .models import Campaign, CampaignMember


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_campaigns_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "campaigns"


def get_campaigns_path(data_dir: Optional[Path] = None) -> Path:
    return get_campaigns_dir(data_dir) / "campaigns.json"


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def _make_slug(name: str) -> str:
    """Convert a display name to a URL/filesystem-safe slug."""
    return re.sub(r"[^\w]+", "-", name.lower()).strip("-")


def _validate_campaign_slug(slug: str) -> Optional[str]:
    """Two-layer security guard for campaign slugs.

    Returns the sanitised slug on success, None on rejection.
    Mirrors the _validate_job_id pattern from web/routes/transcribe.py so
    CodeQL's taint tracker recognises the result as clean.
    """
    if not slug or "\x00" in slug:
        return None

    safe = os.path.basename(slug)
    if safe != slug or safe in {".", ".."}:
        return None

    if not re.match(r"^[\w\-]+$", safe):
        return None

    # os.path round-trip breaks the CodeQL taint chain
    _guard_base = os.path.abspath("_campaigns_guard")
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe))
    if not _guard_path.startswith(_guard_base):
        return None

    return os.path.basename(_guard_path)


def _validate_profile_key(profile_key: str) -> Optional[str]:
    """Two-layer security guard for profile keys (same pattern as _validate_campaign_slug).

    Returns the sanitised key on success, None on rejection.
    """
    if not profile_key or "\x00" in profile_key:
        return None
    safe_key = os.path.basename(profile_key)
    if safe_key != profile_key or safe_key in {".", ".."} or not re.match(r"^[\w\-]+$", safe_key):
        return None
    _guard_base = os.path.abspath("_guard")
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe_key))
    if not _guard_path.startswith(_guard_base):
        return None
    return os.path.basename(_guard_path)


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_campaigns(data_dir: Optional[Path] = None) -> dict[str, Campaign]:
    """Load all campaigns from campaigns.json. Returns {} when file is absent."""
    path = get_campaigns_path(data_dir)
    if not path.exists():
        return {}

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    campaigns: dict[str, Campaign] = {}
    for slug, data in raw.items():
        members: dict[str, CampaignMember] = {}
        for profile_key, mdata in data.get("members", {}).items():
            members[profile_key] = CampaignMember(
                profile_key=profile_key,
                role=mdata.get("role", ""),
                character=mdata.get("character", ""),
                discord_user_id=mdata.get("discord_user_id"),
            )
        campaigns[slug] = Campaign(
            slug=slug,
            display_name=data.get("display_name", slug),
            created=data.get("created", ""),
            members=members,
            transcripts=list(data.get("transcripts", [])),
        )
    return campaigns


def save_campaigns(campaigns: dict[str, Campaign], data_dir: Optional[Path] = None) -> None:
    """Persist campaigns to campaigns.json, creating parent directories as needed."""
    path = get_campaigns_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict = {}
    for slug, campaign in campaigns.items():
        raw[slug] = {
            "display_name": campaign.display_name,
            "created": campaign.created,
            "members": {
                key: {
                    "role": m.role,
                    "character": m.character,
                    "discord_user_id": m.discord_user_id,
                }
                for key, m in campaign.members.items()
            },
            "transcripts": list(campaign.transcripts),
        }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


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

    campaigns = load_campaigns(data_dir)
    if slug in campaigns:
        raise ValueError(f"Campaign with slug {slug!r} already exists")

    campaign = Campaign(
        slug=slug,
        display_name=display_name,
        created=date.today().isoformat(),
        members={},
    )
    campaigns[slug] = campaign
    save_campaigns(campaigns, data_dir)
    return campaign


def delete_campaign(slug: str, data_dir: Optional[Path] = None) -> None:
    """Delete a campaign. Raises KeyError if not found. Never touches profiles or embeddings."""
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        raise KeyError(f"Campaign {slug!r} not found")
    del campaigns[slug]
    save_campaigns(campaigns, data_dir)


def add_member(
    slug: str,
    profile_key: str,
    role: str = "",
    character: str = "",
    data_dir: Optional[Path] = None,
) -> None:
    """Add or update a profile's membership in a campaign. Raises KeyError if campaign missing."""
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        raise KeyError(f"Campaign {slug!r} not found")
    campaigns[slug].members[profile_key] = CampaignMember(
        profile_key=profile_key,
        role=role,
        character=character,
    )
    save_campaigns(campaigns, data_dir)


def remove_member(slug: str, profile_key: str, data_dir: Optional[Path] = None) -> None:
    """Remove a profile from a campaign roster. No-op if profile not in roster."""
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        raise KeyError(f"Campaign {slug!r} not found")
    campaigns[slug].members.pop(profile_key, None)
    save_campaigns(campaigns, data_dir)


def get_campaign_profile_keys(slug: str, data_dir: Optional[Path] = None) -> set[str]:
    """Return the set of profile keys enrolled in a campaign. Empty set if slug unknown."""
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        return set()
    return set(campaigns[slug].members.keys())


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
    member in the same campaign, that existing binding is cleared first.
    Pass discord_user_id=None to clear the binding.
    Raises KeyError if the campaign or member is not found.
    """
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        raise KeyError(f"Campaign {slug!r} not found")
    if profile_key not in campaigns[slug].members:
        raise KeyError(f"Member {profile_key!r} not in campaign {slug!r}")

    if discord_user_id:
        # Clear any existing binding for this discord_user_id (one-to-one)
        for key, member in campaigns[slug].members.items():
            if member.discord_user_id == discord_user_id and key != profile_key:
                member.discord_user_id = None

    campaigns[slug].members[profile_key].discord_user_id = discord_user_id or None
    save_campaigns(campaigns, data_dir)


def lookup_profile_by_discord_id(
    slug: str,
    discord_user_id: str,
    data_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the profile_key bound to discord_user_id in the given campaign, or None."""
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        return None
    for profile_key, member in campaigns[slug].members.items():
        if member.discord_user_id == discord_user_id:
            return profile_key
    return None


# ---------------------------------------------------------------------------
# Transcript association
# ---------------------------------------------------------------------------


def move_transcript_to_campaign(
    stem: str, slug: str, data_dir: Optional[Path] = None
) -> None:
    """Associate a transcript stem with a campaign.

    Removes the stem from any other campaign first (one transcript → one campaign).
    Raises KeyError if the target campaign slug is not found.
    """
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        raise KeyError(f"Campaign {slug!r} not found")
    # Remove from any existing campaign first
    for s, c in campaigns.items():
        if stem in c.transcripts and s != slug:
            c.transcripts.remove(stem)
    if stem not in campaigns[slug].transcripts:
        campaigns[slug].transcripts.append(stem)
    save_campaigns(campaigns, data_dir)


def remove_transcript_from_campaign(stem: str, data_dir: Optional[Path] = None) -> None:
    """Disassociate a transcript stem from whichever campaign it belongs to (no-op if none)."""
    campaigns = load_campaigns(data_dir)
    changed = False
    for c in campaigns.values():
        if stem in c.transcripts:
            c.transcripts.remove(stem)
            changed = True
    if changed:
        save_campaigns(campaigns, data_dir)


def get_campaign_for_transcript(stem: str, data_dir: Optional[Path] = None) -> Optional[str]:
    """Return the slug of the campaign that owns this transcript stem, or None."""
    for slug, c in load_campaigns(data_dir).items():
        if stem in c.transcripts:
            return slug
    return None


def get_transcripts_for_campaign(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Return the list of transcript stems for a campaign. Empty list if slug unknown."""
    campaigns = load_campaigns(data_dir)
    if slug not in campaigns:
        return []
    return list(campaigns[slug].transcripts)
