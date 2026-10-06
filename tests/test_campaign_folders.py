"""Tests for campaign_folders.py: folder names, claiming, and the output root."""
from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

from . import _seed
from wisper_transcribe import campaign_folders as cf
from wisper_transcribe import db
from wisper_transcribe.config import get_output_root

NAMES = [
    ("Hanataz: Act I?", "Hanataz Act I"),
    ("  spaced   out  ", "spaced out"),
    ("a/b\\c|d", "a b c d"),
    ("con", "con_"),
    ("CON", "CON_"),
    ("con.txt", "con_.txt"),
    ("COM0", "COM0_"),
    ("COM¹", "COM¹_"),
    ("nul .txt", "nul_ .txt"),
    ("CONIN$", "CONIN$_"),
    ("console", "console"),
    (". Hidden Tomb", "Hidden Tomb"),
    (" .. x .. ", "x"),
    ("trailing dots...", "trailing dots"),
    ("a\x7fb", "a b"),
    ("a\x00b\x1fc", "a b c"),
    ("???", "Campaign"),
    ("", "Campaign"),
    ("#9", "#9"),
]


@pytest.mark.parametrize("raw,expected", NAMES)
def test_folder_name(raw, expected):
    assert cf.folder_name(raw) == expected
    assert db._v11_folder_name(raw) == expected


def test_folder_name_normalizes_to_nfc():
    nfd = unicodedata.normalize("NFD", "café")
    assert cf.folder_name(nfd) == "café"


def test_folder_name_is_cut_to_80_characters():
    assert cf.folder_name("x" * 85) == "x" * 80
    assert cf.folder_name("x" * 85, room=76) == "x" * 76


def test_folder_name_is_cut_to_200_bytes():
    name = cf.folder_name("\U0001f600" * 80)
    assert name == "\U0001f600" * 50 and len(name.encode()) == 200


def test_folder_name_retrims_after_the_cut():
    assert cf.folder_name("a" * 79 + " b") == "a" * 79


def test_the_migration_and_the_app_sanitizer_agree():
    for raw, _ in NAMES:
        for room, byte_room in ((80, 200), (76, 196)):
            assert cf.folder_name(raw, room, byte_room) == db._v11_folder_name(raw, room, byte_room)


@pytest.mark.parametrize("name,reserved", [
    ("CON", True), ("con.txt", True), ("nul .md", True), ("COM²", True), ("LPT9.x", True),
    ("CONIN$", True), ("console", False), ("Game", False), ("COM10", False),
])
def test_is_reserved(name, reserved):
    assert cf.is_reserved(name) is reserved


def test_journal_name():
    assert cf.journal_name("Hanataz") == "Hanataz Journal.md"


# ---------------------------------------------------------------------------
# unique_folder
# ---------------------------------------------------------------------------

def test_unique_folder_suffixes_case_only_twins():
    _seed.seed_campaign("Hanataz")
    _seed.seed_campaign("HANATAZ", "other")
    assert cf.unique_folder("hanataz") == "hanataz (3)"
    with db.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT folder FROM campaigns ORDER BY id")] == [
            "Hanataz", "HANATAZ (2)"]


def test_unique_folder_suffixes_unicode_case_twins():
    """SQLite's NOCASE folds ASCII only, so the app compares casefolded names."""
    _seed.seed_campaign("été")
    assert cf.unique_folder("ÉTÉ") == "ÉTÉ (2)"


def test_unique_folder_keeps_long_names_within_80_characters_and_200_bytes():
    _seed.seed_campaign("x" * 85)
    assert cf.unique_folder("X" * 85) == "X" * 76 + " (2)"
    _seed.seed_campaign("\U0001f600" * 80, "emoji")
    twin = cf.unique_folder("\U0001f600" * 80)
    assert twin == "\U0001f600" * 49 + " (2)" and len(twin.encode()) <= 200


def test_unique_folder_compares_pending_names_and_can_exclude_a_campaign():
    cid = _seed.seed_campaign("Game")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
        assert cf.unique_folder("renamed", conn) == "renamed (2)"
        assert cf.unique_folder("game", conn, exclude_id=cid) == "game"
        assert cf.unique_folder("game", conn) == "game (2)"


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------

def test_check_available_returns_taken_for_another_campaigns_folder():
    _seed.seed_campaign("Hanataz", "hanataz")
    assert cf.check_available("hanataz") == "taken"
    assert cf.check_available("HANATAZ") == "taken"
    assert cf.check_available("Hanataz", exclude_id=None) == "taken"


def test_check_available_ignores_the_excluded_campaigns_own_folder():
    cid = _seed.seed_campaign("Game")
    assert cf.check_available("game", exclude_id=cid) is None


def test_check_available_returns_folder_exists_for_a_disk_entry():
    (get_output_root() / "hanataz").mkdir()
    assert cf.check_available("Hanataz") == "folder_exists"


def test_check_available_skips_the_current_folder_on_a_no_op_rename():
    cid = _seed.seed_campaign("Game")
    (get_output_root() / "Game").mkdir()
    assert cf.check_available("Game", exclude_id=cid) is None


def test_check_available_reclaims_a_folder_holding_only_its_journal():
    (get_output_root() / "hanataz").mkdir()
    (get_output_root() / "hanataz" / "hanataz Journal.md").write_text(
        "---\ntype: campaign-journal\n---\n\nbody\n")
    assert cf.check_available("Hanataz") is None


def test_check_available_refuses_an_empty_folder():
    (get_output_root() / "hanataz").mkdir()
    assert cf.check_available("Hanataz") == "folder_exists"


def test_check_available_with_the_output_root_absent():
    _seed.seed_campaign("Hanataz", "hanataz")
    assert cf.check_available("Hanataz") == "taken"  # the database claim is enough


def test_check_available_reports_a_pending_folder():
    cid = _seed.seed_campaign("Game")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
    assert cf.check_available("renamed") == "taken"


# ---------------------------------------------------------------------------
# ensure_folder and friends
# ---------------------------------------------------------------------------

def _row(cid):
    with db.connection() as conn:
        r = conn.execute("SELECT folder, folder_pending, folder_claimed FROM campaigns WHERE id = ?",
                         (cid,)).fetchone()
    return tuple(r)


def test_ensure_folder_creates_and_claims_an_absent_folder():
    cid = _seed.seed_campaign("Game")
    path = cf.ensure_folder(cid)
    assert path == get_output_root() / "Game" and path.is_dir()
    assert cf.is_claimed(cid)


def test_ensure_folder_claims_an_empty_folder_ignoring_clutter():
    cid = _seed.seed_campaign("Game")
    folder = get_output_root() / "Game"
    folder.mkdir()
    for name in (".DS_Store", "._x", "Thumbs.db", "desktop.ini", ".wisper-tmp-a.md.1-2"):
        (folder / name).write_text("x")
    assert cf.ensure_folder(cid) == folder
    assert cf.is_claimed(cid)


def test_ensure_folder_claims_a_folder_holding_only_wisper_journal():
    cid = _seed.seed_campaign("Game")
    folder = get_output_root() / "Game"
    folder.mkdir()
    (folder / "Game Journal.md").write_text("---\ntype: campaign-journal\n---\n\nbody\n")
    assert cf.ensure_folder(cid) == folder
    assert cf.is_claimed(cid)


def test_ensure_folder_refuses_a_users_note_named_like_the_journal():
    cid = _seed.seed_campaign("Game")
    folder = get_output_root() / "Game"
    folder.mkdir()
    (folder / "Game Journal.md").write_text("my own notes\n")
    with pytest.raises(cf.FolderTakenError):
        cf.ensure_folder(cid)
    assert not cf.is_claimed(cid)


def test_ensure_folder_refuses_a_folder_with_a_users_note_and_writes_nothing():
    cid = _seed.seed_campaign("Game")
    folder = get_output_root() / "Game"
    folder.mkdir()
    (folder / "notes.md").write_text("mine\n")
    with pytest.raises(cf.FolderTakenError):
        cf.ensure_folder(cid)
    assert sorted(os.listdir(folder)) == ["notes.md"]
    assert not cf.is_claimed(cid)


def test_ensure_folder_refuses_when_a_file_has_the_folders_name():
    cid = _seed.seed_campaign("Game")
    (get_output_root() / "Game").write_text("a file\n")
    with pytest.raises(cf.FolderTakenError):
        cf.ensure_folder(cid)


def test_ensure_folder_on_a_claimed_folder_runs_no_content_checks():
    cid = _seed.seed_campaign("Game", claimed=True)
    (get_output_root() / "Game" / "anything.md").write_text("x")
    assert cf.ensure_folder(cid) == get_output_root() / "Game"


def test_ensure_folder_never_recreates_a_claimed_folder_that_vanished():
    cid = _seed.seed_campaign("Game", claimed=True)
    (get_output_root() / "Game").rmdir()
    with pytest.raises(cf.FolderMissingError):
        cf.ensure_folder(cid)
    assert not (get_output_root() / "Game").exists()


def test_ensure_folder_with_the_output_root_absent_creates_nothing(monkeypatch, tmp_path):
    cid = _seed.seed_campaign("Game")
    gone = tmp_path / "gone"
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(gone))
    with pytest.raises(FileNotFoundError):
        cf.ensure_folder(cid)
    assert not gone.exists()


def test_ensure_folder_refuses_during_a_stuck_pending_rename():
    cid = _seed.seed_campaign("Game", claimed=True)
    (get_output_root() / "Next").mkdir()  # both names present: the rename is stuck
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Next' WHERE id = ?", (cid,))
    with pytest.raises(cf.FolderPendingError):
        cf.ensure_folder(cid)
    assert issubclass(cf.FolderPendingError, cf.FolderTakenError)
    assert issubclass(cf.FolderMissingError, cf.FolderTakenError)


def test_ensure_folder_unknown_campaign():
    with pytest.raises(KeyError):
        cf.ensure_folder(999)


def test_a_rename_committed_before_the_claim_leaves_the_stale_name_unclaimed(monkeypatch):
    """The claim is a compare-and-swap on the folder name checked on disk."""
    cid = _seed.seed_campaign("Game")
    (get_output_root() / "Renamed").mkdir()  # the target exists too: finishing is stuck
    real = cf._claimable

    def rename_after_the_disk_check(path, folder):
        ok = real(path, folder)
        with db.transaction() as conn:
            conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
        return ok

    monkeypatch.setattr(cf, "_claimable", rename_after_the_disk_check)
    with pytest.raises(cf.FolderPendingError):
        cf.ensure_folder(cid)
    assert _row(cid)[2] == 0


def test_a_folder_name_changed_before_the_claim_is_not_claimed(monkeypatch):
    cid = _seed.seed_campaign("Game")
    real = cf._claimable
    calls = []

    def rename_after_the_disk_check(path, folder):
        ok = real(path, folder)
        if not calls:
            calls.append(1)
            with db.transaction() as conn:
                conn.execute("UPDATE campaigns SET folder = 'Renamed' WHERE id = ?", (cid,))
        return ok

    monkeypatch.setattr(cf, "_claimable", rename_after_the_disk_check)
    path = cf.ensure_folder(cid)  # the second pass checks and claims the new name
    assert path == get_output_root() / "Renamed" and _row(cid) == ("Renamed", None, 1)


def test_recreate_folder_makes_a_claimed_folder_again_and_refuses_otherwise():
    cid = _seed.seed_campaign("Game", claimed=True)
    (get_output_root() / "Game").rmdir()
    assert cf.recreate_folder(cid) == get_output_root() / "Game"
    assert (get_output_root() / "Game").is_dir()
    other = _seed.seed_campaign("Other")
    with pytest.raises(cf.FolderTakenError):
        cf.recreate_folder(other)


def test_claim_folder_claims_a_non_empty_folder():
    cid = _seed.seed_campaign("Game")
    folder = get_output_root() / "Game"
    folder.mkdir()
    (folder / "notes.md").write_text("mine\n")
    cf.claim_folder(cid)
    assert cf.is_claimed(cid)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Next' WHERE id = ?", (cid,))
        conn.execute("UPDATE campaigns SET folder_claimed = 0 WHERE id = ?", (cid,))
        with pytest.raises(cf.FolderPendingError):
            cf.claim_folder(cid, conn)


def test_is_clutter():
    for name in (".DS_Store", "._x.md", "thumbs.db", "Desktop.ini", ".wisper-tmp-x.1-2"):
        assert cf.is_clutter(name), name
    for name in ("x.md", "DS_Store", "notes.txt"):
        assert not cf.is_clutter(name), name


def test_ensure_folder_reports_an_unmakeable_folder_as_taken(monkeypatch):
    import errno

    cid = _seed.seed_campaign("Game")

    def too_long(self, *a, **k):
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(Path, "mkdir", too_long)
    with pytest.raises(cf.FolderTakenError):
        cf.ensure_folder(cid)


# ---------------------------------------------------------------------------
# rename_campaign / finish_folder_rename
# ---------------------------------------------------------------------------

def _renamed_files() -> list[str]:
    with db.connection() as conn:
        return [r[0] for r in conn.execute("SELECT rel_path FROM files ORDER BY rel_path")]


def _campaign_row(cid: int):
    with db.connection() as conn:
        r = conn.execute("SELECT slug, folder, folder_pending, folder_claimed FROM campaigns "
                         "WHERE id = ?", (cid,)).fetchone()
    return tuple(r) if r is not None else None


def test_rename_campaign_moves_the_folder_and_rewrites_rows():
    cid = _seed.seed_campaign("Hanataz", claimed=True)
    folder = get_output_root() / "Hanataz"
    tid = _seed.seed_transcript("S1", campaign="hanataz")
    from wisper_transcribe import file_registry

    (folder / "S1.md").write_text("x", encoding="utf-8")
    file_registry.add(folder / "S1.md", kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=get_output_root())
    (folder / "Hanataz Journal.md").write_text("---\ntype: campaign-journal\n---\n\nbody\n")
    file_registry.sync(get_output_root())

    outcome = cf.rename_campaign("hanataz", "Hanataz: Act I?")
    assert outcome.status == "renamed" and outcome.new_slug == "hanataz-act-i"
    assert not folder.exists()
    new = get_output_root() / "Hanataz Act I"
    assert (new / "S1.md").exists()
    assert (new / "Hanataz Act I Journal.md").exists()
    assert not (new / "Hanataz Journal.md").exists()
    assert _campaign_row(cid) == ("hanataz-act-i", "Hanataz Act I", None, 1)
    assert _renamed_files() == ["Hanataz Act I/Hanataz Act I Journal.md", "Hanataz Act I/S1.md"]


def test_rename_campaign_rewrites_prefixes_with_glob_characters():
    from wisper_transcribe import file_registry

    cid = _seed.seed_campaign("Hanataz", claimed=True)
    folder = get_output_root() / "Hanataz"
    # Windows forbids * and ? in file names; [ is the one that breaks GLOB anyway.
    name = "S [1].md" if sys.platform == "win32" else "S [1] * ?.md"
    tid = _seed.seed_transcript(name, campaign="hanataz")
    (folder / name).write_text("x", encoding="utf-8")
    file_registry.add(folder / name, kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=get_output_root())

    assert cf.rename_campaign("hanataz", "Act II").status == "renamed"
    assert _renamed_files() == [f"Act II/{name}"]


def test_rename_campaign_case_only_rename_commits_and_is_not_pending():
    _seed.seed_campaign("Hanataz", "hanataz", claimed=True)
    outcome = cf.rename_campaign("hanataz", "HANATAZ")
    assert outcome.status == "renamed"
    with db.connection() as conn:
        row = conn.execute("SELECT folder, folder_pending FROM campaigns").fetchone()
    assert row[0] == "HANATAZ" and row[1] is None
    assert (get_output_root() / "HANATAZ").is_dir()


def test_rename_campaign_with_a_prefix_spelled_differently_on_disk():
    """Rows carry the disk's spelling; only the fold matches them to the folder."""
    cid = _seed.seed_campaign("Hanataz", "hanataz", claimed=True)
    tid = _seed.seed_transcript("S1", campaign="hanataz")
    (get_output_root() / "Hanataz" / "S1.md").write_text("x", encoding="utf-8")
    # The stored row's prefix differs in case from the folder on disk (Finder,
    # a network share, or a hand-made row).
    with db.transaction() as conn:
        conn.execute("INSERT INTO files (kind, root, rel_path, transcript_id) "
                     "VALUES ('transcript', 'output', 'hanataz/S1.md', ?)", (tid,))
    assert cf.rename_campaign("hanataz", "Act II").status == "renamed"
    assert _renamed_files() == ["Act II/S1.md"]


def test_rename_campaign_rewrites_an_nfd_prefix():
    nfd_campaign = unicodedata.normalize("NFD", "Été")
    _seed.seed_campaign("Été", "ete", claimed=True)
    tid = _seed.seed_transcript("S2", campaign="ete")
    with db.transaction() as conn:
        conn.execute("INSERT INTO files (kind, root, rel_path, transcript_id) "
                     "VALUES ('transcript', 'output', ?, ?)", (f"{nfd_campaign}/S2.md", tid))
    assert cf.rename_campaign("ete", "After").status == "renamed"
    assert _renamed_files() == ["After/S2.md"]


def test_rename_campaign_leftover_old_journal_registers_no_new_session(monkeypatch):
    """A journal left under its old name after a failed post-commit rename."""
    from wisper_transcribe import file_registry, transcript_store as ts

    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "Game Journal.md").write_text(
        "---\ntype: campaign-journal\n---\n\nbody\n")
    file_registry.sync(get_output_root())

    real_move = file_registry.move

    def no_journal_rename(row, new_path, **kwargs):
        if row.kind == "journal":
            return "error"
        return real_move(row, new_path, **kwargs)

    monkeypatch.setattr(file_registry, "move", no_journal_rename)
    assert cf.rename_campaign("game", "Renamed").status == "renamed"
    out = get_output_root()
    assert (out / "Renamed" / "Game Journal.md").exists()  # kept its old name
    assert ts.reconcile(out, sync="never")["added"] == 0


def test_rename_campaign_registers_and_renames_an_unregistered_journal():
    """A journal written but not yet registered (a crash) follows the rename."""
    from wisper_transcribe import journal

    _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "Game Journal.md").write_text(
        "---\ntype: campaign-journal\n---\n\nbody\n")
    assert cf.rename_campaign("game", "Renamed").status == "renamed"
    path = journal.journal_path("renamed")
    assert path == get_output_root() / "Renamed" / "Renamed Journal.md"
    assert path.read_text().endswith("body\n")


def test_rename_campaign_refusals(tmp_path):
    _seed.seed_campaign("Alpha", "alpha", claimed=True)
    _seed.seed_campaign("Beta", "beta", claimed=True)
    assert cf.rename_campaign("alpha", "Beta").status == "slug_taken"
    assert cf.rename_campaign("alpha", "   ").status == "invalid"

    with pytest.raises(KeyError):
        cf.rename_campaign("nope", "X")


def test_rename_campaign_taken_when_another_campaign_holds_the_folder():
    """The slug differs, but the sanitized folder collides with another campaign's."""
    _seed.seed_campaign("Foo Bar", "b")
    _seed.seed_campaign("Alpha", "alpha", claimed=True)
    assert cf.rename_campaign("alpha", "Foo Bar").status == "taken"


def test_rename_campaign_slug_taken_by_a_sibling():
    # A campaign whose slug is ``beta`` but whose folder is ``Bee``.
    _seed.seed_campaign("Bee", "beta")
    _seed.seed_campaign("Alpha", "alpha")
    assert cf.rename_campaign("alpha", "Beta").status == "slug_taken"


def test_rename_campaign_refuses_a_session_named_like_the_journal():
    cid = _seed.seed_campaign("Y", "y", claimed=True)
    _seed.seed_transcript("X Journal", campaign="y")
    assert cf.rename_campaign("y", "X").status == "reserved"


def test_rename_campaign_unclaimed_changes_only_the_database():
    cid = _seed.seed_campaign("Game", "game")  # no folder, folder_claimed default 0
    outcome = cf.rename_campaign("game", "Renamed")
    assert outcome.status == "renamed"
    assert _campaign_row(cid) == ("renamed", "Renamed", None, 0)
    assert not (get_output_root() / "Renamed").exists()


def test_rename_campaign_busy_with_a_pending_job():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, status, created_at, started_at, campaign_id, params_json) "
            "VALUES ('00000000-0000-0000-0000-0000000000a1', 'campaign_journal', 'running', "
            "'now', 'now', ?, '{}')", (cid,))
    outcome = cf.rename_campaign("game", "Renamed")
    assert outcome.status == "busy"
    assert _campaign_row(cid) == ("game", "Game", None, 1)


def test_rename_campaign_busy_with_a_pending_upload_into_the_campaign():
    _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, status, created_at, params_json) "
            "VALUES ('00000000-0000-0000-0000-0000000000d4', 'transcription', 'pending', 'now', "
            "'{\"campaign\": \"game\"}')")
    assert cf.rename_campaign("game", "Renamed").status == "busy"


def test_rename_campaign_locked_folder_is_pending_then_finishes(monkeypatch):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")

    real = os.rename
    calls = []

    def locked(src, dst):
        calls.append((src, dst))
        raise PermissionError(32, "in use")

    monkeypatch.setattr(os, "rename", locked)
    outcome = cf.rename_campaign("game", "Renamed")
    assert outcome.status == "pending"
    assert _campaign_row(cid) == ("renamed", "Game", "Renamed", 1)
    assert (get_output_root() / "Game").is_dir()
    assert _renamed_files() == []

    monkeypatch.setattr(os, "rename", real)
    assert cf.finish_folder_rename(cid) is True
    assert _campaign_row(cid) == ("renamed", "Renamed", None, 1)
    assert (get_output_root() / "Renamed").is_dir()


def test_finish_folder_rename_crash_after_move_before_commit():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    tid = _seed.seed_transcript("S1", campaign="game")
    from wisper_transcribe import file_registry

    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    file_registry.add(get_output_root() / "Game" / "S1.md", kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=get_output_root())
    # The crash: the folder moved, the row still says Game → Renamed.
    os.rename(get_output_root() / "Game", get_output_root() / "Renamed")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert cf.finish_folder_rename(cid) is True
    assert _campaign_row(cid) == ("game", "Renamed", None, 1)
    assert _renamed_files() == ["Renamed/S1.md"]


def test_finish_folder_rename_neither_directory_updates_only_the_database():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game").rmdir()
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
    assert cf.finish_folder_rename(cid) is False
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)

    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
    assert cf.finish_folder_rename(cid, without_folder=True) is True
    assert _campaign_row(cid) == ("game", "Renamed", None, 0)


def test_finish_folder_rename_renames_the_folder_back_when_the_commit_fails(monkeypatch):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cf, "_rewrite_prefix", fail)
    with pytest.raises(RuntimeError):
        cf.finish_folder_rename(cid)
    out = get_output_root()
    assert (out / "Game").is_dir() and not (out / "Renamed").exists()
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)


def test_finish_without_folder_finishes_normally_when_the_old_folder_exists():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert cf.finish_folder_rename(cid, without_folder=True) is True
    assert (get_output_root() / "Renamed").is_dir()
    assert _campaign_row(cid) == ("game", "Renamed", None, 1)


def test_folder_renames_and_transcript_moves_share_one_lock():
    from wisper_transcribe import transcript_store

    assert transcript_store._LOCATION_LOCK is cf._LOCATION_LOCK


def test_finish_folder_rename_foreign_target_folder_is_left_alone():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game").rmdir()
    (get_output_root() / "Renamed").mkdir()
    (get_output_root() / "Renamed" / "notes.md").write_text("mine", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert cf.finish_folder_rename(cid) is False
    assert (get_output_root() / "Renamed" / "notes.md").read_text(encoding="utf-8") == "mine"
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)


def test_finish_folder_rename_root_absent_leaves_everything_pending(monkeypatch, tmp_path):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
    gone = tmp_path / "gone"
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(gone))
    assert cf.finish_folder_rename(cid) is False
    assert not gone.exists()
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)


def test_finish_pending_renames_output_root_absent_stays_pending(monkeypatch, tmp_path):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
    gone = tmp_path / "gone"
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(gone))
    cf.finish_pending_renames()  # startup catches and logs; never raises
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)


def test_finish_folder_rename_second_caller_after_the_first_commits():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert cf.finish_folder_rename(cid) is True
    # The second caller sees no pending rename and never renames back.
    assert cf.finish_folder_rename(cid) is True
    assert (get_output_root() / "Renamed").is_dir()
    assert _campaign_row(cid) == ("game", "Renamed", None, 1)


def test_rename_campaign_stale_compare_and_swap_changes_nothing():
    """A stale old→new pair (case-different) must not match a later pending rename.

    Both folder columns are COLLATE NOCASE, so without COLLATE BINARY a caller
    holding ``Game → alpha2`` would match a later pending ``Game → ALPHA2``.
    """
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'ALPHA2' WHERE id = ?", (cid,))

    assert cf._swap_rename(cid, "Game", "alpha2", "game", True,
                           None, get_output_root()) is None
    assert _campaign_row(cid) == ("game", "Game", "ALPHA2", 1)


def test_finish_folder_rename_disk_recheck_returns_false(monkeypatch):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    real_rename = os.rename

    def rename_back_before_commit(src, dst):
        # Step 4 moves Game → Renamed; rename it back before step 5's re-check.
        real_rename(src, dst)
        if dst.name == "Renamed":
            real_rename(get_output_root() / "Renamed", get_output_root() / "Game")

    monkeypatch.setattr(os, "rename", rename_back_before_commit)
    assert cf.finish_folder_rename(cid) is False
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)
    assert (get_output_root() / "Game").is_dir()


def test_finish_folder_rename_busy_inside_the_swap(monkeypatch):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    real_rename = os.rename

    def rename_then_submit(src, dst):
        real_rename(src, dst)
        if dst.name != "Renamed":
            return  # the back-move must not open a second transaction
        with db.transaction() as conn:
            conn.execute("INSERT INTO jobs (id, type, status, created_at, started_at, "
                         "campaign_id, params_json) VALUES "
                         "('00000000-0000-0000-0000-0000000000b2', 'campaign_journal', 'pending', "
                         "'now', NULL, ?, '{}')", (cid,))

    monkeypatch.setattr(os, "rename", rename_then_submit)
    assert cf.finish_folder_rename(cid) is False
    # The folder is renamed back so the state stays consistent.
    assert (get_output_root() / "Game").is_dir()
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)


def test_finish_pending_renames_logs_and_leaves_a_failing_one_pending(monkeypatch, caplog):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    def boom(campaign_id, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(cf, "finish_folder_rename", boom)
    cf.finish_pending_renames()  # never raises
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)


def test_finish_pending_renames_completes_a_crash(monkeypatch):
    from wisper_transcribe import file_registry

    cid = _seed.seed_campaign("Game", "game", claimed=True)
    tid = _seed.seed_transcript("S1", campaign="game")
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    file_registry.add(get_output_root() / "Game" / "S1.md", kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=get_output_root())
    os.rename(get_output_root() / "Game", get_output_root() / "Renamed")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert cf.finish_pending_renames() is None
    assert _campaign_row(cid) == ("game", "Renamed", None, 1)
    assert _renamed_files() == ["Renamed/S1.md"]


def test_ensure_folder_finishes_a_pending_rename_before_writing():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    os.rename(get_output_root() / "Game", get_output_root() / "Renamed")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert cf.ensure_folder(cid) == get_output_root() / "Renamed"
    assert _campaign_row(cid) == ("game", "Renamed", None, 1)


def test_rename_campaign_legacy_journal_directory_follows_the_slug():
    from wisper_transcribe.campaign_manager import get_campaigns_dir

    cid = _seed.seed_campaign("Hanataz", "hanataz", claimed=True)
    # The campaign's folder holds a different journal, so adoption keeps the old one.
    (get_output_root() / "Hanataz" / "Hanataz Journal.md").write_text("different", encoding="utf-8")
    legacy_dir = get_campaigns_dir() / "hanataz"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "journal.md").write_text("old text", encoding="utf-8")

    outcome = cf.rename_campaign("hanataz", "Hanataz Act II")
    assert outcome.status == "renamed"
    assert not legacy_dir.exists()
    assert (get_campaigns_dir() / "hanataz-act-ii" / "journal.md").read_text(
        encoding="utf-8") == "old text"


def test_rename_campaign_legacy_journal_target_taken_is_refused():
    from wisper_transcribe.campaign_manager import get_campaigns_dir

    _seed.seed_campaign("Hanataz", "hanataz", claimed=True)
    (get_output_root() / "Hanataz" / "Hanataz Journal.md").write_text("different", encoding="utf-8")
    legacy_dir = get_campaigns_dir() / "hanataz"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "journal.md").write_text("old", encoding="utf-8")
    (get_campaigns_dir() / "hanataz-act-ii").mkdir(parents=True)

    outcome = cf.rename_campaign("hanataz", "Hanataz Act II")
    assert outcome.status == "legacy_journal"
    assert (legacy_dir / "journal.md").is_file()
    with db.connection() as conn:
        assert conn.execute("SELECT slug FROM campaigns").fetchone()[0] == "hanataz"


def test_rename_campaign_finishes_an_earlier_pending_one_first():
    from wisper_transcribe import file_registry

    cid = _seed.seed_campaign("Game", "game", claimed=True)
    tid = _seed.seed_transcript("S1", campaign="game")
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")
    file_registry.add(get_output_root() / "Game" / "S1.md", kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=get_output_root())
    os.rename(get_output_root() / "Game", get_output_root() / "Renamed")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    outcome = cf.rename_campaign("game", "Final")
    assert outcome.status == "renamed" and outcome.new_slug == "final"
    assert _campaign_row(cid) == ("final", "Final", None, 1)
    assert (get_output_root() / "Final").is_dir()


def test_rename_campaign_second_rename_while_pending_returns_pending(monkeypatch):
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    (get_output_root() / "Game" / "S1.md").write_text("x", encoding="utf-8")

    def locked(src, dst):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(os, "rename", locked)
    assert cf.rename_campaign("game", "Renamed").status == "pending"
    assert _campaign_row(cid) == ("renamed", "Game", "Renamed", 1)

    # A second rename must not overwrite the pending target.
    outcome = cf.rename_campaign("renamed", "Other")
    assert outcome.status == "pending"
    assert _campaign_row(cid) == ("renamed", "Game", "Renamed", 1)


def test_finish_folder_rename_refuses_while_an_upload_is_queued():
    cid = _seed.seed_campaign("Game", "game", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))
        conn.execute(
            "INSERT INTO jobs (id, type, status, created_at, params_json) "
            "VALUES ('00000000-0000-0000-0000-0000000000c3', 'transcription', 'pending', 'now', "
            "'{\"campaign\": \"game\"}')")

    assert cf.finish_folder_rename(cid) is False
    assert _campaign_row(cid) == ("game", "Game", "Renamed", 1)
