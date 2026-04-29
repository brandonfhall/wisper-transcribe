"""Tests for campaign_manager — pure disk I/O, no ML mocking required."""
import json
from pathlib import Path

import pytest

from wisper_transcribe.campaign_manager import (
    _make_slug,
    _validate_campaign_slug,
    add_member,
    create_campaign,
    delete_campaign,
    get_campaign_profile_keys,
    get_campaigns_path,
    load_campaigns,
    remove_member,
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
