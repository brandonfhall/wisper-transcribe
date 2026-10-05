"""Tests for campaign_folders.py: folder names, claiming, and the output root."""
from __future__ import annotations

import os
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


def test_ensure_folder_refuses_during_a_pending_rename():
    cid = _seed.seed_campaign("Game", claimed=True)
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
