"""Tests for campaign_manager — wisper.db only, no ML mocking required."""
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from wisper_transcribe.campaign_manager import (
    CampaignError,
    _make_slug,
    _validate_campaign_slug,
    add_member,
    bind_discord_id,
    create_campaign,
    delete_campaign,
    get_campaign_for_transcript,
    get_campaign_profile_keys,
    get_campaigns_dir,
    get_transcripts_for_campaign,
    load_campaigns,
    lookup_profile_by_discord_id,
    remove_member,
    reorder_campaign_transcript,
    set_campaign_transcript_order,
)
from wisper_transcribe import db
from wisper_transcribe.models import Campaign, CampaignMember

from ._seed import save_campaigns, seed_profiles, transcript_id


def _tid(stem, data_dir=None):
    """The id of the session named ``stem`` (any campaign or the root)."""
    return transcript_id(stem, data_dir=data_dir)


def _move(stem, slug, data_dir=None):
    """Seed/attach a session named ``stem`` to ``slug``; returns its id.

    A direct seed (no files move), so the session reads misplaced; use
    ``transcript_store.move_transcript`` where the file move matters.
    """
    from ._seed import seed_transcript

    return seed_transcript(stem, campaign=slug, data_dir=data_dir)


@pytest.fixture(autouse=True)
def _profiles(tmp_path):
    """Members must be real profiles (campaign_members has a foreign key)."""
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
    _move("s01", "test-campaign", data_dir=tmp_path)
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


def _campaign_with_two_sessions():
    """A campaign whose transcripts have summaries, and a journal file."""
    from wisper_transcribe import file_registry, transcript_store as ts
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    campaign_folders.ensure_folder(create_campaign("Game").id)
    for stem in ("s01", "s02"):
        (out / f"{stem}.md").write_text("x", encoding="utf-8")
        ts.register(out / f"{stem}.md", origin="job")
        (out / f"{stem}.summary.md").write_text("sum", encoding="utf-8")
        _move(stem, "game")
    journal = out / "Game" / "Game Journal.md"
    journal.write_text("journal", encoding="utf-8")
    file_registry.sync(out)
    return out, journal


def test_delete_campaign_everything_removes_transcripts_files_and_journal():
    from wisper_transcribe import file_registry

    out, journal = _campaign_with_two_sessions()
    assert delete_campaign("game", delete_transcripts=True).status == "deleted"

    assert "game" not in load_campaigns()
    assert list(out.iterdir()) == []  # the emptied folder is removed too
    assert not journal.exists()
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert file_registry.sync(out).unclaimed == []


def test_delete_campaign_everything_keeps_a_folder_holding_a_user_file():
    out, _ = _campaign_with_two_sessions()
    (out / "Game" / "notes.md").write_text("mine", encoding="utf-8")
    assert delete_campaign("game", delete_transcripts=True).status == "deleted"

    assert "game" not in load_campaigns()
    assert (out / "Game" / "notes.md").read_text(encoding="utf-8") == "mine"


def test_delete_campaign_keep_the_files_leaves_the_journal_untracked():
    from wisper_transcribe import file_registry, transcript_store as ts

    out, journal = _campaign_with_two_sessions()
    delete_campaign("game")

    assert (out / "s01.md").exists() and (out / "s02.summary.md").exists() and journal.exists()
    assert get_campaign_for_transcript(_tid("s01")) is None
    assert file_registry.sync(out).unclaimed == []  # the deleted campaign claims nothing
    assert ts.needs_attention(out).unclaimed == []
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM files WHERE kind = 'journal'").fetchone()[0] == 0


def test_delete_campaign_everything_keeps_a_transcript_it_cannot_delete(monkeypatch):
    out, journal = _campaign_with_two_sessions()
    real_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if self.name == "s01.md":
            raise PermissionError(32, "in use")
        return real_unlink(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", locked)
        outcome = delete_campaign("game", delete_transcripts=True)

    assert outcome.status == "kept" and outcome.kept == ["s01"]
    assert "game" in load_campaigns()  # the campaign stays with what's left
    assert (out / "s01.md").exists() and journal.exists()
    assert get_campaign_for_transcript(_tid("s01")) == "game"


def test_delete_campaign_everything_raises_keyerror_before_deleting(tmp_path):
    with pytest.raises(KeyError):
        delete_campaign("nonexistent", delete_transcripts=True)


def _campaign_with_files_in_its_folder():
    """A campaign whose two sessions' files are inside its claimed folder."""
    from wisper_transcribe import file_registry, transcript_store as ts
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    campaign_folders.ensure_folder(create_campaign("Game").id)
    for stem in ("s01", "s02"):
        (out / f"{stem}.md").write_text("x", encoding="utf-8")
        ts.register(out / f"{stem}.md", origin="job")
        (out / f"{stem}.summary.md").write_text("sum", encoding="utf-8")
        _move(stem, "game")
    for stem in ("s01", "s02"):
        ts.move_files_home(_tid(stem))
    journal = out / "Game" / "Game Journal.md"
    journal.write_text("journal", encoding="utf-8")
    file_registry.sync(out)
    return out, journal


def test_delete_campaign_keep_files_clashes_into_a_suffixed_name():
    out, _ = _campaign_with_files_in_its_folder()
    (out / "s01.md").write_text("a root copy", encoding="utf-8")

    assert delete_campaign("game").status == "deleted"

    assert (out / "s01.md").read_text(encoding="utf-8") == "a root copy"
    assert (out / "s01 (2).md").read_text(encoding="utf-8") == "x"
    assert "game" not in load_campaigns()


def test_delete_campaign_keep_files_with_a_missing_session_deletes_the_campaign():
    from wisper_transcribe import transcript_store as ts

    out, _ = _campaign_with_files_in_its_folder()
    (out / "Game" / "s02.md").unlink()
    ts.reconcile(out)  # flags s02 missing: its move is database-only

    assert delete_campaign("game").status == "deleted"

    assert (out / "s01.md").exists() and (out / "s01.summary.md").exists()
    assert get_campaign_for_transcript(_tid("s01")) is None
    assert get_campaign_for_transcript(_tid("s02")) is None


@pytest.mark.parametrize("delete_transcripts", [False, True])
def test_delete_campaign_is_busy_while_a_job_holds_it(delete_transcripts):
    out, journal = _campaign_with_two_sessions()
    with db.connection() as conn:
        cid = conn.execute("SELECT id FROM campaigns WHERE slug = 'game'").fetchone()[0]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, status, created_at, started_at, campaign_id, params_json) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'campaign_journal', 'running', "
            "'now', 'now', ?, '{}')", (cid,))

    outcome = delete_campaign("game", delete_transcripts=delete_transcripts)
    assert outcome.status == "busy"
    assert "game" in load_campaigns()
    assert (out / "s01.md").exists() and journal.exists()


def test_delete_campaign_everything_of_an_unclaimed_campaign_spares_a_user_folder():
    from wisper_transcribe.config import get_output_root

    from ._seed import seed_campaign

    seed_campaign("Hanataz", "hanataz")
    folder = get_output_root() / "Hanataz"
    folder.mkdir()
    note = folder / "Hanataz Journal.md"
    note.write_text("my own notes", encoding="utf-8")

    assert delete_campaign("hanataz", delete_transcripts=True).status == "deleted"

    assert note.read_text(encoding="utf-8") == "my own notes"
    assert "hanataz" not in load_campaigns()


def test_delete_campaign_everything_reports_delete_incomplete(monkeypatch):
    import wisper_transcribe.campaign_manager as cm

    out, journal = _campaign_with_two_sessions()
    real = cm._campaign_id
    calls = []

    def register_then_look_up(conn, slug):
        calls.append(slug)
        if slug == "game" and len(calls) == 2:  # the final delete transaction
            cid = conn.execute("SELECT id FROM campaigns WHERE slug = 'game'").fetchone()[0]
            pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM transcripts "
                               "WHERE campaign_id = ?", (cid,)).fetchone()[0]
            conn.execute("INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                         "VALUES ('new', ?, ?, 'now')", (cid, pos))
        return real(conn, slug)

    monkeypatch.setattr(cm, "_campaign_id", register_then_look_up)
    outcome = delete_campaign("game", delete_transcripts=True)

    assert outcome.status == "delete_incomplete"
    assert "game" in load_campaigns()
    assert journal.exists()


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


def test_move_transcript_assigns_campaign(tmp_path):
    from ._seed import seed_transcript
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    tid = seed_transcript("session01", data_dir=tmp_path)  # missing: a database-only move
    assert ts.move_transcript(tid, "alpha", data_dir=tmp_path).status == "moved"
    assert "session01" in get_transcripts_for_campaign("alpha", data_dir=tmp_path)


def test_move_transcript_noop_when_already_there(tmp_path):
    from ._seed import seed_transcript
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    tid = seed_transcript("session01", campaign="alpha", data_dir=tmp_path)
    assert ts.move_transcript(tid, "alpha", data_dir=tmp_path).status == "unchanged"


def test_move_transcript_changes_campaign(tmp_path):
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    tid = _move("session01", "alpha", data_dir=tmp_path)
    assert ts.move_transcript(tid, "beta", data_dir=tmp_path).status == "moved"
    assert "session01" not in get_transcripts_for_campaign("alpha", data_dir=tmp_path)
    assert "session01" in get_transcripts_for_campaign("beta", data_dir=tmp_path)


def test_move_transcript_unknown_campaign_is_invalid(tmp_path):
    from ._seed import seed_transcript
    from wisper_transcribe import transcript_store as ts

    tid = seed_transcript("session01", data_dir=tmp_path)
    assert ts.move_transcript(tid, "no-such-slug", data_dir=tmp_path).status == "invalid"


def test_move_transcript_to_root(tmp_path):
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    tid = _move("session01", "alpha", data_dir=tmp_path)
    assert ts.move_transcript(tid, None, data_dir=tmp_path).status == "moved"
    assert "session01" not in get_transcripts_for_campaign("alpha", data_dir=tmp_path)


def test_move_transcript_unknown_id_is_invalid(tmp_path):
    from wisper_transcribe import transcript_store as ts

    assert ts.move_transcript(999999, None, data_dir=tmp_path).status == "invalid"


def test_get_campaign_for_transcript_returns_slug(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    tid = _move("session01", "alpha", data_dir=tmp_path)
    assert get_campaign_for_transcript(tid, data_dir=tmp_path) == "alpha"


# ---------------------------------------------------------------------------
# reorder_campaign_transcript / set_campaign_transcript_order
# ---------------------------------------------------------------------------

def _seed_three(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    for stem in ("s1", "s2", "s3"):
        _move(stem, "alpha", data_dir=tmp_path)


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
    from ._seed import seed_transcript

    create_campaign("Alpha", data_dir=tmp_path)
    tid = seed_transcript("orphan", data_dir=tmp_path)
    assert get_campaign_for_transcript(tid, data_dir=tmp_path) is None
    assert get_campaign_for_transcript(999999, data_dir=tmp_path) is None


def test_get_transcripts_for_campaign_returns_empty_for_unknown_slug(tmp_path):
    assert get_transcripts_for_campaign("no-such", data_dir=tmp_path) == []


def test_transcripts_persisted_in_json(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    _move("session01", "alpha", data_dir=tmp_path)
    campaigns = load_campaigns(tmp_path)
    assert "session01" in campaigns["alpha"].transcripts


def test_transcripts_loaded_from_existing_json(tmp_path):
    """Campaigns.json with existing transcripts field loads correctly."""
    create_campaign("Alpha", data_dir=tmp_path)
    _move("s01", "alpha", data_dir=tmp_path)
    _move("s02", "alpha", data_dir=tmp_path)
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
# Constraints the schema enforces
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


def test_a_transcript_has_one_campaign_and_a_move_replaces_it(tmp_path):
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    tid = _move("s01", "alpha", data_dir=tmp_path)
    assert ts.move_transcript(tid, "beta", data_dir=tmp_path).status == "moved"
    assert get_campaign_for_transcript(tid, data_dir=tmp_path) == "beta"
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == []
    with db.connection(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM transcripts WHERE stem = 's01'").fetchone()[0] == 1


def test_reorder_rewrites_positions_under_unique_constraint(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    stems = [f"s{i:02d}" for i in range(6)]
    for st in stems:
        _move(st, "alpha", data_dir=tmp_path)
    set_campaign_transcript_order("alpha", list(reversed(stems)), data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == list(reversed(stems))
    reorder_campaign_transcript("alpha", "s00", "up", data_dir=tmp_path)
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path)[-2:] == ["s00", "s01"]
    with db.connection(tmp_path) as conn:
        positions = [r[0] for r in conn.execute(
            "SELECT position FROM transcripts WHERE campaign_id IS NOT NULL ORDER BY position")]
    assert positions == list(range(6))


def test_move_appends_to_end_of_target(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    _move("b1", "beta", data_dir=tmp_path)
    _move("a1", "alpha", data_dir=tmp_path)
    _move("a2", "alpha", data_dir=tmp_path)
    _move("a1", "beta", data_dir=tmp_path)
    assert get_transcripts_for_campaign("beta", data_dir=tmp_path) == ["b1", "a1"]
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["a2"]


def test_stems_are_nfc_normalized(tmp_path):
    from ._seed import seed_transcript

    create_campaign("Alpha", data_dir=tmp_path)
    nfd = unicodedata.normalize("NFD", "Café Session")
    nfc = unicodedata.normalize("NFC", "Café Session")
    tid = seed_transcript(nfc, campaign="alpha", data_dir=tmp_path)
    assert get_campaign_for_transcript(tid, data_dir=tmp_path) == "alpha"
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == [nfc]


def test_transcript_without_md_is_registered_missing(tmp_path):
    from wisper_transcribe.path_utils import get_output_dir

    (get_output_dir() / "present.md").write_text("x", encoding="utf-8")
    create_campaign("Alpha", data_dir=tmp_path)
    _move("present", "alpha", data_dir=tmp_path)
    _move("absent", "alpha", data_dir=tmp_path)
    with db.connection(tmp_path) as conn:
        flags = dict(conn.execute("SELECT stem, missing_since IS NOT NULL FROM transcripts"))
    assert flags == {"present": 0, "absent": 1}


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


# ---------------------------------------------------------------------------
# campaign assignment on transcripts
# ---------------------------------------------------------------------------

def _transcript_rows(tmp_path):
    with db.connection(tmp_path) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT stem, campaign_id, position FROM transcripts ORDER BY id")]


def test_create_campaign_names_its_folder_and_claims_it(tmp_path):
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.config import get_output_root

    c = create_campaign("Hanataz: Act I?", data_dir=tmp_path)
    assert c.folder == "Hanataz Act I" and c.id > 0
    assert (get_output_root() / "Hanataz Act I").is_dir()
    assert campaign_folders.is_claimed(c.id, data_dir=tmp_path)
    assert load_campaigns(tmp_path)[c.slug].folder == "Hanataz Act I"


@pytest.mark.parametrize("on_disk", ["Hanataz", "hanataz"])
def test_create_campaign_refuses_a_folder_that_already_exists(tmp_path, on_disk):
    from wisper_transcribe.config import get_output_root

    (get_output_root() / on_disk).mkdir()
    with pytest.raises(CampaignError) as excinfo:
        create_campaign("Hanataz", data_dir=tmp_path)
    assert excinfo.value.code == "folder_exists"
    assert load_campaigns(tmp_path) == {}


def test_create_campaign_refuses_another_campaigns_folder(tmp_path):
    from ._seed import seed_campaign

    # A different slug sanitizes to the same folder name as an existing campaign's.
    seed_campaign("Hanataz Act", "hanataz", data_dir=tmp_path)
    with pytest.raises(CampaignError) as excinfo:
        create_campaign("Hanataz: Act", data_dir=tmp_path)
    assert excinfo.value.code == "taken"


def test_create_campaign_reclaims_a_folder_left_by_a_keep_files_delete(tmp_path):
    from wisper_transcribe import campaign_folders, journal
    from wisper_transcribe.config import get_output_root

    create_campaign("Hanataz", data_dir=tmp_path)
    folder = get_output_root() / "Hanataz"
    (folder / "Hanataz Journal.md").write_text(
        "---\ntype: campaign-journal\n---\n\nbody\n", encoding="utf-8")
    assert delete_campaign("hanataz", data_dir=tmp_path).status == "deleted"
    assert folder.is_dir()  # keep-files leaves the folder and journal on disk

    again = create_campaign("Hanataz", data_dir=tmp_path)
    assert again.folder == "Hanataz"
    assert campaign_folders.is_claimed(again.id, data_dir=tmp_path)
    assert journal.journal_path("hanataz", data_dir=tmp_path) == folder / "Hanataz Journal.md"


def test_load_campaigns_lists_transcript_ids_in_order(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    for st in ("b", "a", "c"):
        _move(st, "alpha", data_dir=tmp_path)
    set_campaign_transcript_order("alpha", ["c", "a", "b"], data_dir=tmp_path)
    c = load_campaigns(tmp_path)["alpha"]
    assert c.transcripts == ["c", "a", "b"]
    with db.connection(tmp_path) as conn:
        assert c.transcript_ids == [conn.execute("SELECT id FROM transcripts WHERE stem = ?", (s,)
                                                 ).fetchone()[0] for s in c.transcripts]


def test_move_by_id_is_unambiguous_when_a_stem_is_shared(tmp_path):
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    _move("s1", "alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    with db.transaction(tmp_path) as conn:  # a missing root session with the same name
        conn.execute("INSERT INTO transcripts (stem, created_at, missing_since) "
                     "VALUES ('s1', 'now', 'now')")
        root_tid = conn.execute(
            "SELECT id FROM transcripts WHERE campaign_id IS NULL").fetchone()[0]

    assert ts.move_transcript(root_tid, "beta", data_dir=tmp_path).status == "moved"

    with db.connection(tmp_path) as conn:
        bid = conn.execute("SELECT id FROM campaigns WHERE slug='beta'").fetchone()[0]
        assert conn.execute("SELECT campaign_id FROM transcripts WHERE id = ?",
                            (root_tid,)).fetchone()[0] == bid


def test_assigning_a_session_to_a_campaign_that_has_its_name_is_refused(tmp_path):
    from wisper_transcribe import campaign_manager as cm

    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    _move("s1", "alpha", data_dir=tmp_path)
    with db.transaction(tmp_path) as conn:
        conn.execute("INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                     "SELECT 's1', id, 0, 'now' FROM campaigns WHERE slug = 'beta'")
        conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL "
                     "WHERE stem = 's1' AND campaign_id = (SELECT id FROM campaigns WHERE slug = 'alpha')")
    with pytest.raises(ValueError, match="already there"):
        with db.transaction(tmp_path) as conn:
            tid = conn.execute("SELECT id FROM transcripts WHERE campaign_id IS NULL").fetchone()[0]
            cm._assign(conn, tid, cm._campaign_id(conn, "beta"), 5)


def test_delete_campaign_keep_files_keeps_the_campaign_when_a_name_is_taken_in_the_root(tmp_path):
    from wisper_transcribe import campaign_manager as cm
    from wisper_transcribe import transcript_store as ts

    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    tid = _move("s1", "alpha", data_dir=tmp_path)
    with db.transaction(tmp_path) as conn:  # a root session with the moved name
        conn.execute("INSERT INTO transcripts (stem, created_at, missing_since) "
                     "VALUES ('s1', 'now', 'now')")

    # ``_write_order`` only reorders members: it never unassigns a session, so
    # an empty order leaves the campaign's session in place.
    with db.transaction(tmp_path) as conn:
        cm._write_order(conn, cm._campaign_id(conn, "alpha"), [])
    assert get_transcripts_for_campaign("alpha", data_dir=tmp_path) == ["s1"]

    # A keep-files move lands the session as ``s1 (2)``, so the campaign deletes.
    assert delete_campaign("alpha", data_dir=tmp_path).status == "deleted"
    assert "alpha" not in load_campaigns(tmp_path)
    assert sorted(r[0] for r in _transcript_rows(tmp_path)) == ["s1", "s1 (2)"]


def test_transcript_id_is_read_only_and_campaign_scoped(tmp_path):
    from wisper_transcribe import campaign_manager as cm

    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    _move("s1", "alpha", data_dir=tmp_path)
    with db.transaction(tmp_path) as conn:
        alpha, beta = cm._campaign_id(conn, "alpha"), cm._campaign_id(conn, "beta")
        assert cm._transcript_id(conn, alpha, "s1") > 0
        with pytest.raises(KeyError):
            cm._transcript_id(conn, beta, "s1")
        with pytest.raises(KeyError):
            cm._transcript_id(conn, alpha, "missing")
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1


def test_delete_campaign_keep_files_leaves_its_transcripts_unassigned(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    for st in ("a", "b"):
        _move(st, "alpha", data_dir=tmp_path)
    delete_campaign("alpha", data_dir=tmp_path)
    assert [r[1:] for r in _transcript_rows(tmp_path)] == [(None, None), (None, None)]
    assert load_campaigns(tmp_path) == {}


def test_a_move_drops_the_journal_entry_and_marks_the_old_journal_stale_but_a_reorder_does_not(tmp_path):
    create_campaign("Alpha", data_dir=tmp_path)
    create_campaign("Beta", data_dir=tmp_path)
    for st in ("a", "b"):
        _move(st, "alpha", data_dir=tmp_path)
    with db.transaction(tmp_path) as conn:
        conn.execute("INSERT INTO journal_entries SELECT id, campaign_id, 'now' FROM transcripts")

    def state():
        with db.connection(tmp_path) as conn:
            return (conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0],
                    conn.execute("SELECT journal_stale_since FROM campaigns WHERE slug = 'alpha'"
                                 ).fetchone()[0])

    reorder_campaign_transcript("alpha", "a", "down", data_dir=tmp_path)
    assert state() == (2, None)
    _move("a", "beta", data_dir=tmp_path)
    entries, stale = state()
    assert entries == 1 and stale is not None


# ---------------------------------------------------------------------------
# test seeds
# ---------------------------------------------------------------------------

def test_save_campaigns_reorders_and_drops_without_colliding(tmp_path):
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01",
                                  transcripts=["a", "b", "c"])}, tmp_path)
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01",
                                  transcripts=["c", "b", "a"])}, tmp_path)
    assert get_transcripts_for_campaign("g", tmp_path) == ["c", "b", "a"]
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01",
                                  transcripts=["b", "c"])}, tmp_path)
    assert get_transcripts_for_campaign("g", tmp_path) == ["b", "c"]
    assert get_campaign_for_transcript(_tid("a", tmp_path), tmp_path) is None
    assert ("a", None, None) in _transcript_rows(tmp_path)


def test_save_campaigns_handles_an_empty_campaign_and_a_removed_one(tmp_path):
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01"),
                    "h": Campaign(slug="h", display_name="H", created="2026-01-01",
                                  transcripts=["x"])}, tmp_path)
    assert get_transcripts_for_campaign("g", tmp_path) == []
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01")}, tmp_path)
    assert list(load_campaigns(tmp_path)) == ["g"]
    assert _transcript_rows(tmp_path) == [("x", None, None)]


def test_save_campaigns_moves_a_listed_transcript_out_of_another_campaign(tmp_path):
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01", transcripts=["a"]),
                    "h": Campaign(slug="h", display_name="H", created="2026-01-01")}, tmp_path)
    save_campaigns({"g": Campaign(slug="g", display_name="G", created="2026-01-01"),
                    "h": Campaign(slug="h", display_name="H", created="2026-01-01",
                                  transcripts=["a"])}, tmp_path)
    assert get_campaign_for_transcript(_tid("a", tmp_path), tmp_path) == "h"


def test_seed_campaign_and_seed_transcript(tmp_path):
    from wisper_transcribe import file_registry
    from wisper_transcribe.config import get_output_root

    from ._seed import seed_campaign, seed_transcript

    cid = seed_campaign("Hanataz", claimed=True, data_dir=tmp_path)
    assert load_campaigns(tmp_path)["hanataz"].id == cid
    assert (get_output_root() / "Hanataz").is_dir()
    seed_campaign("Other Game", "custom", data_dir=tmp_path)
    assert load_campaigns(tmp_path)["custom"].folder == "Other Game"

    tid = seed_transcript("s1", campaign="hanataz", write_md=True, data_dir=tmp_path)
    assert seed_transcript("s1", data_dir=tmp_path) == tid
    assert get_campaign_for_transcript(tid, tmp_path) == "hanataz"
    md = file_registry.file_for(file_registry.Owner("transcript", tid), "transcript",
                                data_dir=tmp_path)
    assert md is not None and md.path == get_output_root() / "s1.md" and md.path.is_file()


def test_delete_campaign_removes_its_legacy_journal_files_and_spares_an_unclaimed_folder(tmp_path):
    from wisper_transcribe.config import get_output_root

    create_campaign("Hanataz", data_dir=tmp_path)
    legacy = get_campaigns_dir(tmp_path) / "hanataz"
    legacy.mkdir(parents=True)
    names = ("journal.md", "journal.md.wisper-pending", "journal.md.v11-adopted")
    for name in names:
        (legacy / name).write_text("old", encoding="utf-8")
    note = get_output_root() / "Hanataz" / "Hanataz Journal.md"
    note.write_text("my own notes", encoding="utf-8")  # the create claimed the folder

    assert delete_campaign("hanataz", delete_transcripts=True,
                           data_dir=tmp_path).status == "deleted"

    assert not any((legacy / name).exists() for name in names)
    assert note.read_text(encoding="utf-8") == "my own notes"  # a user file keeps the folder


def test_delete_campaign_keep_files_removes_an_empty_folder_so_the_name_can_be_reused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    c = create_campaign("Empty Camp", output_dir=out)
    assert (out / "Empty Camp").is_dir()

    assert delete_campaign(c.slug, output_dir=out).status == "deleted"
    assert not (out / "Empty Camp").exists()
    assert create_campaign("Empty Camp", output_dir=out).folder == "Empty Camp"


def test_delete_everything_after_reclaiming_a_kept_journal_removes_it_and_the_folder(tmp_path):
    from wisper_transcribe import file_registry, journal
    from wisper_transcribe.transcript_store import atomic_write_text

    out = tmp_path / "out"
    out.mkdir()
    c = create_campaign("Camp", output_dir=out)
    jp = journal.journal_path(c.slug, output_dir=out)
    atomic_write_text(jp, "---\ntype: campaign-journal\n---\n\nbody\n")
    file_registry.add_if_owned(jp, kind="journal", owner=file_registry.Owner("campaign", c.id),
                               output_dir=out)
    assert delete_campaign(c.slug, output_dir=out).status == "deleted"
    assert jp.is_file()  # keep the files: the journal stays

    again = create_campaign("Camp", output_dir=out)
    assert delete_campaign(again.slug, delete_transcripts=True, output_dir=out).status == "deleted"
    assert not (out / "Camp").exists()
