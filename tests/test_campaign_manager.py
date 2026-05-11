"""Tests for campaign_manager — pure disk I/O, no ML mocking required."""
import json
from pathlib import Path

import pytest

from wisper_transcribe.campaign_manager import (
    _make_slug,
    _validate_campaign_slug,
    add_member,
    bind_discord_id,
    create_campaign,
    delete_campaign,
    get_campaign_for_transcript,
    get_campaign_profile_keys,
    get_campaigns_path,
    get_transcripts_for_campaign,
    load_campaigns,
    lookup_profile_by_discord_id,
    move_transcript_to_campaign,
    remove_member,
    remove_transcript_from_campaign,
    save_campaigns,
)
from wisper_transcribe.models import Campaign, CampaignMember


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------

def test_load_campaigns_missing_file_returns_empty(tmp_path):
    result = load_campaigns(tmp_path)
    assert result == {}


def test_save_then_load_roundtrip(tmp_path):
    campaigns = {
        "dnd-mondays": Campaign(
            slug="dnd-mondays",
            display_name="D&D Mondays",
            created="2026-04-28",
            members={
                "alice": CampaignMember(profile_key="alice", role="DM", character=""),
                "bob": CampaignMember(profile_key="bob", role="Player", character="Thorin"),
            },
        ),
        "pathfinder-fridays": Campaign(
            slug="pathfinder-fridays",
            display_name="Pathfinder Fridays",
            created="2026-04-28",
            members={},
        ),
    }
    save_campaigns(campaigns, tmp_path)
    loaded = load_campaigns(tmp_path)

    assert set(loaded.keys()) == {"dnd-mondays", "pathfinder-fridays"}
    assert loaded["dnd-mondays"].display_name == "D&D Mondays"
    assert loaded["dnd-mondays"].members["alice"].role == "DM"
    assert loaded["dnd-mondays"].members["bob"].character == "Thorin"
    assert loaded["pathfinder-fridays"].members == {}


# ---------------------------------------------------------------------------
# create_campaign
# ---------------------------------------------------------------------------

def test_create_campaign_generates_slug(tmp_path):
    campaign = create_campaign("D&D Mondays", data_dir=tmp_path)
    assert campaign.slug == "d-d-mondays"
    assert campaign.display_name == "D&D Mondays"

    loaded = load_campaigns(tmp_path)
    assert "d-d-mondays" in loaded


def test_create_campaign_rejects_duplicate(tmp_path):
    create_campaign("My Campaign", data_dir=tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        create_campaign("My Campaign", data_dir=tmp_path)


def test_create_campaign_rejects_empty_name(tmp_path):
    with pytest.raises(ValueError):
        create_campaign("", data_dir=tmp_path)


def test_create_campaign_rejects_whitespace_only(tmp_path):
    with pytest.raises(ValueError):
        create_campaign("   ", data_dir=tmp_path)


def test_create_campaign_persists_created_date(tmp_path):
    campaign = create_campaign("Test", data_dir=tmp_path)
    assert campaign.created  # non-empty ISO date
    loaded = load_campaigns(tmp_path)
    assert loaded["test"].created == campaign.created


# ---------------------------------------------------------------------------
# delete_campaign
# ---------------------------------------------------------------------------

def test_delete_campaign_removes_entry_only(tmp_path):
    # Create a fake .npy to confirm it is never touched
    profiles_dir = tmp_path / "profiles" / "embeddings"
    profiles_dir.mkdir(parents=True)
    fake_npy = profiles_dir / "alice.npy"
    fake_npy.write_bytes(b"fake")

    create_campaign("Test Campaign", data_dir=tmp_path)
    delete_campaign("test-campaign", data_dir=tmp_path)

    loaded = load_campaigns(tmp_path)
    assert "test-campaign" not in loaded
    assert fake_npy.exists(), "delete_campaign must not touch embeddings"


def test_delete_campaign_raises_keyerror_if_missing(tmp_path):
    with pytest.raises(KeyError):
        delete_campaign("nonexistent", data_dir=tmp_path)


# ---------------------------------------------------------------------------
# add_member / remove_member
# ---------------------------------------------------------------------------

def test_add_member_then_remove(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", role="DM", data_dir=tmp_path)

    loaded = load_campaigns(tmp_path)
    assert "alice" in loaded["test"].members
    assert loaded["test"].members["alice"].role == "DM"

    remove_member("test", "alice", data_dir=tmp_path)
    loaded = load_campaigns(tmp_path)
    assert "alice" not in loaded["test"].members


def test_add_member_overwrites_existing_role(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", role="Player", data_dir=tmp_path)
    add_member("test", "alice", role="DM", character="Kyra", data_dir=tmp_path)

    loaded = load_campaigns(tmp_path)
    assert loaded["test"].members["alice"].role == "DM"
    assert loaded["test"].members["alice"].character == "Kyra"


def test_add_member_raises_keyerror_for_missing_campaign(tmp_path):
    with pytest.raises(KeyError):
        add_member("nonexistent", "alice", data_dir=tmp_path)


def test_remove_member_noop_when_not_in_roster(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    # Should not raise
    remove_member("test", "nobody", data_dir=tmp_path)


# ---------------------------------------------------------------------------
# get_campaign_profile_keys
# ---------------------------------------------------------------------------

def test_get_campaign_profile_keys_returns_set(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    add_member("test", "bob", data_dir=tmp_path)

    keys = get_campaign_profile_keys("test", data_dir=tmp_path)
    assert keys == {"alice", "bob"}


def test_get_campaign_profile_keys_unknown_slug_returns_empty(tmp_path):
    keys = get_campaign_profile_keys("does-not-exist", data_dir=tmp_path)
    assert keys == set()


# ---------------------------------------------------------------------------
# _make_slug
# ---------------------------------------------------------------------------

def test_make_slug_strips_punctuation_and_spaces():
    assert _make_slug("D&D Mondays") == "d-d-mondays"
    assert _make_slug("  Curse of Strahd!  ") == "curse-of-strahd"
    assert _make_slug("Pathfinder 2E") == "pathfinder-2e"
    assert _make_slug("A") == "a"


# ---------------------------------------------------------------------------
# _validate_campaign_slug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [
    "dnd-mondays",
    "abc_123",
    "Slug-1",
    "my-campaign",
    "UPPER",
])
def test_validate_campaign_slug_accepts_valid(slug):
    result = _validate_campaign_slug(slug)
    assert result is not None


@pytest.mark.parametrize("slug", [
    "",
    "\x00",
    "../etc/passwd",
    "a/b",
    "evil\r\nHeader: injected",
    "javascript:alert(1)",
    ".",
    "..",
    " leading-space",
    "trailing-space ",
])
def test_validate_campaign_slug_rejects_invalid(slug):
    result = _validate_campaign_slug(slug)
    assert result is None


# ---------------------------------------------------------------------------
# Transcript association
# ---------------------------------------------------------------------------


def test_move_transcript_to_campaign(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "alpha", data_dir=tmp_path)
    assert "session01" in get_transcripts_for_campaign("alpha", data_dir=tmp_path)


def test_move_transcript_changes_campaign(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "alpha", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "beta", data_dir=tmp_path)
    assert "session01" not in get_transcripts_for_campaign("alpha", data_dir=tmp_path)
    assert "session01" in get_transcripts_for_campaign("beta", data_dir=tmp_path)


def test_move_transcript_unknown_campaign_raises(tmp_path):
    with pytest.raises(KeyError):
        move_transcript_to_campaign("session01", "no-such-slug", data_dir=tmp_path)


def test_remove_transcript_from_campaign(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "alpha", data_dir=tmp_path)
    remove_transcript_from_campaign("session01", data_dir=tmp_path)
    assert "session01" not in get_transcripts_for_campaign("alpha", data_dir=tmp_path)


def test_remove_transcript_noop_when_not_associated(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    remove_transcript_from_campaign("orphan", data_dir=tmp_path)  # must not raise


def test_get_campaign_for_transcript_returns_slug(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "alpha", data_dir=tmp_path)
    assert get_campaign_for_transcript("session01", data_dir=tmp_path) == "alpha"


def test_get_campaign_for_transcript_returns_none_when_not_associated(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    assert get_campaign_for_transcript("orphan", data_dir=tmp_path) is None


def test_get_transcripts_for_campaign_returns_empty_for_unknown_slug(tmp_path):
    assert get_transcripts_for_campaign("no-such", data_dir=tmp_path) == []


def test_transcripts_persisted_in_json(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "alpha", data_dir=tmp_path)
    campaigns = load_campaigns(tmp_path)
    assert "session01" in campaigns["alpha"].transcripts


def test_transcripts_loaded_from_existing_json(tmp_path):
    """Campaigns.json with existing transcripts field loads correctly."""
    create_campaign("Alpha", data_dir=tmp_path)
    move_transcript_to_campaign("s01", "alpha", data_dir=tmp_path)
    move_transcript_to_campaign("s02", "alpha", data_dir=tmp_path)
    fresh = load_campaigns(tmp_path)
    assert set(fresh["alpha"].transcripts) == {"s01", "s02"}


# ---------------------------------------------------------------------------
# Discord ID binding
# ---------------------------------------------------------------------------

def test_bind_discord_id_persists(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    bind_discord_id("test", "alice", "123456789012345678", data_dir=tmp_path)

    loaded = load_campaigns(tmp_path)
    assert loaded["test"].members["alice"].discord_user_id == "123456789012345678"


def test_lookup_profile_by_discord_id_returns_profile_key(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    bind_discord_id("test", "alice", "123456789012345678", data_dir=tmp_path)

    result = lookup_profile_by_discord_id("test", "123456789012345678", data_dir=tmp_path)
    assert result == "alice"


def test_lookup_returns_none_for_unknown_id(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)

    result = lookup_profile_by_discord_id("test", "999999999999999999", data_dir=tmp_path)
    assert result is None


def test_bind_discord_id_overwrites_previous_binding(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    add_member("test", "bob", data_dir=tmp_path)

    bind_discord_id("test", "alice", "123456789012345678", data_dir=tmp_path)
    # Rebind the same Discord ID to bob — alice's binding should be cleared
    bind_discord_id("test", "bob", "123456789012345678", data_dir=tmp_path)

    loaded = load_campaigns(tmp_path)
    assert loaded["test"].members["alice"].discord_user_id is None
    assert loaded["test"].members["bob"].discord_user_id == "123456789012345678"
