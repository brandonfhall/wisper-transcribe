"""Tests for campaign_manager — wisper.db only, no ML mocking required."""
import sqlite3
import unicodedata
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
    get_transcripts_for_campaign,
    load_campaigns,
    lookup_profile_by_discord_id,
    move_transcript_to_campaign,
    remove_member,
    remove_transcript_from_campaign,
    reorder_campaign_transcript,
    save_campaigns,
    set_campaign_transcript_order,
)
from wisper_transcribe import db
from wisper_transcribe.models import Campaign, CampaignMember

from ._seed import seed_profiles


@pytest.fixture(autouse=True)
def _profiles(tmp_path):
    """Members must be real profiles now (campaign_members has a foreign key)."""
    seed_profiles("alice", "bob", data_dir=tmp_path)


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
    create_campaign("Test Campaign", data_dir=tmp_path)
    add_member("test-campaign", "alice", data_dir=tmp_path)
    move_transcript_to_campaign("s01", "test-campaign", data_dir=tmp_path)
    delete_campaign("test-campaign", data_dir=tmp_path)

    assert "test-campaign" not in load_campaigns(tmp_path)
    from wisper_transcribe.speaker_manager import load_profiles
    assert "alice" in load_profiles(tmp_path), "delete_campaign must not touch profiles"
    with db.connection(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM campaign_members").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM transcripts WHERE stem = 's01'").fetchone()[0] == 1


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


# ---------------------------------------------------------------------------
# reorder_campaign_transcript / set_campaign_transcript_order
# ---------------------------------------------------------------------------

def _seed_three(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    for stem in ("s1", "s2", "s3"):
        move_transcript_to_campaign(stem, "alpha", data_dir=tmp_path)


def test_reorder_up_swaps_with_previous(tmp_path):
    _seed_three(tmp_path)
    reorder_campaign_transcript("alpha", "s2", "up", data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["s2", "s1", "s3"]


def test_reorder_down_swaps_with_next(tmp_path):
    _seed_three(tmp_path)
    reorder_campaign_transcript("alpha", "s2", "down", data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["s1", "s3", "s2"]


def test_reorder_up_at_start_is_noop(tmp_path):
    _seed_three(tmp_path)
    reorder_campaign_transcript("alpha", "s1", "up", data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["s1", "s2", "s3"]


def test_reorder_down_at_end_is_noop(tmp_path):
    _seed_three(tmp_path)
    reorder_campaign_transcript("alpha", "s3", "down", data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["s1", "s2", "s3"]


def test_reorder_invalid_direction_raises(tmp_path):
    _seed_three(tmp_path)
    with pytest.raises(ValueError):
        reorder_campaign_transcript("alpha", "s1", "sideways", data_dir=tmp_path)


def test_reorder_unknown_stem_raises(tmp_path):
    _seed_three(tmp_path)
    with pytest.raises(ValueError):
        reorder_campaign_transcript("alpha", "ghost", "up", data_dir=tmp_path)


def test_reorder_unknown_campaign_raises(tmp_path):
    with pytest.raises(KeyError):
        reorder_campaign_transcript("no-such-slug", "s1", "up", data_dir=tmp_path)


def test_set_transcript_order_replaces_whole_list(tmp_path):
    _seed_three(tmp_path)
    set_campaign_transcript_order("alpha", ["s3", "s1", "s2"], data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["s3", "s1", "s2"]


def test_set_transcript_order_rejects_non_permutation(tmp_path):
    _seed_three(tmp_path)
    with pytest.raises(ValueError):
        set_campaign_transcript_order("alpha", ["s1", "s2"], data_dir=tmp_path)  # missing s3
    with pytest.raises(ValueError):
        set_campaign_transcript_order("alpha", ["s1", "s2", "s3", "s4"], data_dir=tmp_path)  # extra


def test_set_transcript_order_unknown_campaign_raises(tmp_path):
    with pytest.raises(KeyError):
        set_campaign_transcript_order("no-such-slug", [], data_dir=tmp_path)


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


# ---------------------------------------------------------------------------
# Profile rename / removal — memberships reference the profile's id
# ---------------------------------------------------------------------------

def test_profile_rename_keeps_memberships_in_every_campaign(tmp_path):
    from wisper_transcribe.speaker_manager import rename_profile

    c1 = create_campaign("Campaign One", data_dir=tmp_path)
    c2 = create_campaign("Campaign Two", data_dir=tmp_path)
    add_member(c1.slug, "alice", role="dm", character="Kyra", data_dir=tmp_path)
    add_member(c2.slug, "alice", role="player", data_dir=tmp_path)
    bind_discord_id(c1.slug, "alice", "123456789012345678", data_dir=tmp_path)

    rename_profile("alice", "Alicia", data_dir=tmp_path)

    campaigns = load_campaigns(data_dir=tmp_path)
    m1 = campaigns[c1.slug].members
    m2 = campaigns[c2.slug].members
    assert "alice" not in m1 and "alice" not in m2
    assert m1["alicia"].role == "dm"
    assert m1["alicia"].character == "Kyra"
    assert m1["alicia"].discord_user_id == "123456789012345678"
    assert m1["alicia"].profile_key == "alicia"
    assert m2["alicia"].role == "player"


def test_profile_removal_drops_its_memberships(tmp_path):
    from wisper_transcribe.speaker_manager import remove_profile

    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    add_member("test", "bob", data_dir=tmp_path)
    remove_profile("alice", data_dir=tmp_path)
    assert set(load_campaigns(tmp_path)["test"].members) == {"bob"}


# ---------------------------------------------------------------------------
# Constraints the schema now enforces
# ---------------------------------------------------------------------------

def test_add_member_requires_existing_profile(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    with pytest.raises(KeyError, match="ghost"):
        add_member("test", "ghost", data_dir=tmp_path)


def test_add_member_again_resets_discord_binding(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    bind_discord_id("test", "alice", "42", data_dir=tmp_path)
    add_member("test", "alice", role="DM", data_dir=tmp_path)
    member = load_campaigns(tmp_path)["test"].members["alice"]
    assert member.role == "DM" and member.discord_user_id is None


def test_bind_discord_id_rejects_non_numeric(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    with pytest.raises(ValueError):
        bind_discord_id("test", "alice", "12ab", data_dir=tmp_path)


def test_schema_rejects_duplicate_discord_id_in_campaign(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    add_member("test", "bob", data_dir=tmp_path)
    bind_discord_id("test", "alice", "42", data_dir=tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(tmp_path) as conn:
            conn.execute("UPDATE campaign_members SET discord_user_id = '42' "
                         "WHERE profile_id = (SELECT id FROM profiles WHERE key = 'bob')")


def test_schema_rejects_discord_id_with_non_digits(tmp_path):
    create_campaign("Test", data_dir=tmp_path)
    add_member("test", "alice", data_dir=tmp_path)
    for bad in ("", "1a", "a1", " 1"):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(tmp_path) as conn:
                conn.execute("UPDATE campaign_members SET discord_user_id = ?", (bad,))


def test_transcript_belongs_to_one_campaign_in_schema(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    move_transcript_to_campaign("s01", "alpha", data_dir=tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(tmp_path) as conn:
            conn.execute(
                "INSERT INTO campaign_transcripts (transcript_id, campaign_id, position) "
                "SELECT t.id, c.id, 5 FROM transcripts t, campaigns c "
                "WHERE t.stem = 's01' AND c.slug = 'beta'")


def test_reorder_rewrites_positions_under_unique_constraint(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    stems = [f"s{i:02d}" for i in range(6)]
    for st in stems:
        move_transcript_to_campaign(st, "alpha", data_dir=tmp_path)
    set_campaign_transcript_order("alpha", list(reversed(stems)), data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == list(reversed(stems))
    reorder_campaign_transcript("alpha", "s00", "up", data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path)[-2:] == ["s00", "s01"]
    with db.connection(tmp_path) as conn:
        positions = [r[0] for r in conn.execute(
            "SELECT position FROM campaign_transcripts ORDER BY position")]
    assert positions == list(range(6))


def test_move_appends_to_end_of_target(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    move_transcript_to_campaign("b1", "beta", data_dir=tmp_path)
    move_transcript_to_campaign("a1", "alpha", data_dir=tmp_path)
    move_transcript_to_campaign("a2", "alpha", data_dir=tmp_path)
    move_transcript_to_campaign("a1", "beta", data_dir=tmp_path)
    assert get_transcripts_for_campaign("beta", data_dir=tmp_path) == ["b1", "a1"]
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["a2"]


def test_stems_are_nfc_normalized(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    nfd = unicodedata.normalize("NFD", "Café Session")
    move_transcript_to_campaign(nfd, "alpha", data_dir=tmp_path)
    nfc = unicodedata.normalize("NFC", "Café Session")
    assert get_campaign_for_transcript(nfc, data_dir=tmp_path) == "alpha"
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == [nfc]


def test_transcript_without_md_is_registered_missing(tmp_path):
    from wisper_transcribe.path_utils import get_output_dir

    (get_output_dir() / "present.md").write_text("x", encoding="utf-8")
    create_campaign("Alpha", data_dir=tmp_path)
    move_transcript_to_campaign("present", "alpha", data_dir=tmp_path)
    move_transcript_to_campaign("absent", "alpha", data_dir=tmp_path)
    with db.connection(tmp_path) as conn:
        flags = dict(conn.execute("SELECT stem, missing_since IS NOT NULL FROM transcripts"))
    assert flags == {"present": 0, "absent": 1}


def test_save_campaigns_rejects_unknown_member(tmp_path):
    with pytest.raises(KeyError):
        save_campaigns({"x": Campaign(slug="x", display_name="X", created="2026-01-01",
                                      members={"ghost": CampaignMember(profile_key="ghost")})},
                       tmp_path)
    assert load_campaigns(tmp_path) == {}


def test_save_campaigns_deletes_absent_slugs(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    campaigns = load_campaigns(tmp_path)
    del campaigns["alpha"]
    save_campaigns(campaigns, tmp_path)
    assert set(load_campaigns(tmp_path)) == {"beta"}


# ---------------------------------------------------------------------------
# Concurrent mutation must not lose writes
# ---------------------------------------------------------------------------

def test_add_member_atomic_under_concurrent_calls(tmp_path):
    """Concurrent add_member() calls (each its own BEGIN IMMEDIATE) must
    not lose writes."""
    import threading

    campaign = create_campaign("Concurrency Test", data_dir=tmp_path)
    n = 20
    seed_profiles(*(f"member_{i:02d}" for i in range(n)), data_dir=tmp_path)

    def _add(i):
        add_member(campaign.slug, f"member_{i:02d}", data_dir=tmp_path)

    threads = [threading.Thread(target=_add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    loaded = load_campaigns(tmp_path)
    assert len(loaded[campaign.slug].members) == n
    for i in range(n):
        assert f"member_{i:02d}" in loaded[campaign.slug].members
