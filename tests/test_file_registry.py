"""file_registry: owners, rows, moves, deletes, and sync against the disk."""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from tests._seed import seed_file, seed_profile, seed_recording
from wisper_transcribe import db, file_registry as fr, transcript_store as ts
from wisper_transcribe.campaign_manager import create_campaign
from wisper_transcribe.config import get_data_dir
from wisper_transcribe.path_utils import get_output_dir


@pytest.fixture(autouse=True)
def _case_sensitive(monkeypatch):
    """Whatever the host filesystem does, tests start with exact-case names;
    the ones about folding turn it on."""
    _case(monkeypatch, False)


@pytest.fixture
def out() -> Path:
    return get_output_dir()


@pytest.fixture
def data() -> Path:
    return get_data_dir()


def _owner(stem: str) -> fr.Owner:
    """A transcript row for ``stem`` (no file needed) and its owner."""
    with db.transaction() as conn:
        tid = ts.ensure_row(conn, stem)
    return fr.Owner("transcript", tid)


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _case(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(ts, "_is_case_insensitive", lambda directory: value)


def _count() -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) FROM files").fetchone()[0]


# ---------------------------------------------------------------------------
# Constants, owners
# ---------------------------------------------------------------------------

def test_root_follows_kind():
    assert {k for k, r in fr.ROOT_OF_KIND.items() if r == "output"} == {
        "transcript", "summary", "sidecar", "excerpt", "excerpt_text", "audio", "backup"}
    assert {k for k, r in fr.ROOT_OF_KIND.items() if r == "data"} == {
        "combined", "per_user", "live_draft", "reference_clip", "journal"}
    assert fr.SINGLE_KINDS == set(fr.KINDS) - {"excerpt", "excerpt_text", "per_user"}


def test_owner_lookups(out, monkeypatch):
    t = _owner("Session One")
    assert fr.Owner.for_stem("Session One") == t
    assert fr.Owner.for_stem("session one") is None
    _case(monkeypatch, True)
    assert fr.Owner.for_stem("session one") == t
    seed_profile("alice")
    camp = create_campaign("Camp")
    rec = seed_recording()
    assert fr.Owner.for_profile_key("alice").kind == "profile"
    assert fr.Owner.for_profile_key("nobody") is None
    assert fr.Owner.for_campaign_slug(camp.slug).kind == "campaign"
    assert fr.Owner.for_recording(rec.id) == fr.Owner("recording", rec.id)
    assert fr.Owner.for_recording("0" * 36) is None


def test_owner_lookup_is_nfc():
    t = _owner("Café")
    assert fr.Owner.for_stem(unicodedata.normalize("NFD", "Café")) == t


# ---------------------------------------------------------------------------
# add / add_if_owned
# ---------------------------------------------------------------------------

def test_add_records_owner_kind_and_stats(out):
    owner = _owner("s")
    md = _touch(out / "s.md", b"hello")
    assert fr.add(md, kind="transcript", owner=owner) is None
    row = fr.file_for(owner, "transcript")
    assert (row.root, row.rel_path, row.size) == ("output", "s.md", 5)
    assert row.path == md and row.mtime_ns is not None
    assert [r.id for r in fr.files_for(owner)] == [row.id]


def test_add_missing_file_has_no_stats(out):
    owner = _owner("s")
    fr.add(out / "s.flac", kind="audio", owner=owner)
    row = fr.file_for(owner, "audio")
    assert row.size is None and row.mtime_ns is None


def test_data_kinds_use_the_data_root(data):
    camp = create_campaign("Camp")
    owner = fr.Owner.for_campaign_slug(camp.slug)
    journal = _touch(data / "campaigns" / camp.slug / "journal.md")
    fr.add(journal, kind="journal", owner=owner)
    row = fr.file_for(owner, "journal")
    assert row.root == "data" and row.rel_path == f"campaigns/{camp.slug}/journal.md"


@pytest.mark.parametrize("where", ["equal", "inside"])
def test_output_root_equal_to_or_inside_the_data_dir(data, where):
    out = data if where == "equal" else data / "nested" / "out"
    owner = _owner("s")
    camp = create_campaign("Camp")
    cowner = fr.Owner.for_campaign_slug(camp.slug)
    md = _touch(out / "s.md")
    journal = _touch(data / "campaigns" / camp.slug / "journal.md")
    fr.add(md, kind="transcript", owner=owner, data_dir=None, output_dir=out)
    fr.add(journal, kind="journal", owner=cowner, output_dir=out)
    assert fr.file_for(owner, "transcript", output_dir=out).path == md
    assert fr.file_for(cowner, "journal", output_dir=out).root == "data"


def test_outside_the_root_raises_or_returns_none(out, tmp_path):
    owner = _owner("s")
    elsewhere = _touch(tmp_path / "elsewhere" / "s.md")
    with pytest.raises(ValueError):
        fr.add(elsewhere, kind="transcript", owner=owner)
    assert fr.add_if_owned(elsewhere, kind="transcript", owner=owner) is None
    assert fr.add_if_owned(out / "s.md", kind="transcript", owner=None) is None
    assert _count() == 0


def test_audio_under_recordings_is_refused(data):
    owner = _owner("s")
    rec = seed_recording()
    from wisper_transcribe.recording_manager import combined_path_for

    with pytest.raises(ValueError):
        fr.add(combined_path_for(rec.id), kind="audio", owner=owner,
               output_dir=data)
    assert fr.add_if_owned(combined_path_for(rec.id), kind="audio", owner=owner,
                           output_dir=data) is None


def test_kind_must_match_owner_type(out):
    with pytest.raises(ValueError):
        fr.add(out / "s.md", kind="journal", owner=_owner("s"))
    with pytest.raises(ValueError):
        fr.add(out / "s.md", kind="nonsense", owner=_owner("s"))


def test_reowning_a_path_raises(out):
    a, b = _owner("a"), _owner("b")
    audio = _touch(out / "a.flac")
    fr.add(audio, kind="audio", owner=a)
    with pytest.raises(fr.OwnershipConflict):
        fr.add(audio, kind="audio", owner=b)
    assert fr.add_if_owned(audio, kind="audio", owner=b) is None
    assert fr.file_for(a, "audio").path == audio and fr.file_for(b, "audio") is None


def test_same_path_different_kind_is_a_conflict(out):
    owner = _owner("a")
    path = _touch(out / "a.flac")
    fr.add(path, kind="audio", owner=owner)
    with pytest.raises(fr.OwnershipConflict):
        fr.add(path, kind="backup", owner=owner)


def test_adding_again_restats(out):
    owner = _owner("a")
    path = _touch(out / "a.flac", b"1")
    fr.add(path, kind="audio", owner=owner)
    path.write_bytes(b"123")
    assert fr.add(path, kind="audio", owner=owner) is None
    assert fr.file_for(owner, "audio").size == 3 and _count() == 1


def test_single_kind_replacement_returns_the_old_path(out):
    owner = _owner("a")
    old, new = _touch(out / "a.flac"), _touch(out / "a.mp4")
    fr.add(old, kind="audio", owner=owner)
    assert fr.add(new, kind="audio", owner=owner) == old
    assert fr.file_for(owner, "audio").path == new and _count() == 1


def test_labelled_kinds_are_unique_per_label(out):
    owner = _owner("a")
    a, b = _touch(out / "a_excerpt_Ann.mp3"), _touch(out / "a_excerpt_Bob.mp3")
    fr.add(a, kind="excerpt", owner=owner, label="Ann")
    fr.add(b, kind="excerpt", owner=owner, label="Bob")
    assert _count() == 2
    moved = _touch(out / "a_excerpt_Ann2.mp3")
    assert fr.add(moved, kind="excerpt", owner=owner, label="Ann") == a
    assert fr.file_for(owner, "excerpt", "Bob").path == b
    other = _owner("z")
    with pytest.raises(fr.OwnershipConflict):
        fr.add(b, kind="excerpt", owner=other, label="Bob")


def test_add_if_owned_swallows_a_failed_add(out):
    owner = _owner("x.summary")           # fails the transcript kind's CHECK
    assert fr.add_if_owned(_touch(out / "x.summary.md"), kind="transcript", owner=owner) is None
    assert _count() == 0


def test_add_in_the_callers_transaction(out):
    owner = _owner("a")
    path = _touch(out / "a.flac")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            fr.add(path, kind="audio", owner=owner, conn=conn)
            raise RuntimeError
    assert _count() == 0                  # rolled back with the caller's transaction


# ---------------------------------------------------------------------------
# forget, delete
# ---------------------------------------------------------------------------

def test_forget_and_forget_kind_return_paths(out):
    owner = _owner("a")
    flac, bak = _touch(out / "a.flac"), _touch(out / "a.md.bak")
    ex1, ex2 = _touch(out / "a_excerpt_A.mp3"), _touch(out / "a_excerpt_B.mp3")
    fr.add(flac, kind="audio", owner=owner)
    fr.add(bak, kind="backup", owner=owner)
    fr.add(ex1, kind="excerpt", owner=owner, label="A")
    fr.add(ex2, kind="excerpt", owner=owner, label="B")
    assert fr.forget(bak) == [bak]
    assert fr.forget(bak) == []
    assert fr.forget_kind(owner, "excerpt", "A") == [ex1]
    assert fr.forget_kind(owner, "excerpt") == [ex2]
    assert fr.forget_kind(owner, "audio") == [flac]
    assert _count() == 0


def test_delete_flow_removes_files_and_rows(out):
    owner = _owner("a")
    paths = [_touch(out / "a.flac"), _touch(out / "a.summary.md"),
             _touch(out / "a_excerpt_A.mp3")]
    fr.add(paths[0], kind="audio", owner=owner)
    fr.add(paths[1], kind="summary", owner=owner)
    fr.add(paths[2], kind="excerpt", owner=owner, label="A")
    with db.transaction() as conn:
        doomed = fr.paths_for_delete(owner, conn)
        conn.execute("DELETE FROM transcripts WHERE id = ?", (owner.id,))
    assert sorted(doomed) == sorted(paths)
    assert fr.unlink_paths(doomed) == []
    assert not any(p.exists() for p in paths) and _count() == 0


def test_unlink_failure_is_returned(out, monkeypatch):
    stuck, fine = _touch(out / "stuck.flac"), _touch(out / "fine.flac")
    real = Path.unlink

    def unlink(self, *a, **kw):
        if self.name == "stuck.flac":
            raise PermissionError(13, "in use")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert fr.unlink_paths([stuck, fine]) == [stuck]
    assert stuck.exists() and not fine.exists()


def test_unlink_paths_removes_track_directories(data):
    track = data / "recordings" / "r" / "per-user" / "mic"
    _touch(track / "a.wav")
    assert fr.unlink_paths([track]) == []
    assert not track.exists()
    assert fr.unlink_paths([track]) == []        # already gone


# ---------------------------------------------------------------------------
# move, repoint, refresh
# ---------------------------------------------------------------------------

def _registered(out, name="a.flac", stem="a", kind="audio"):
    owner = _owner(stem)
    path = _touch(out / name, b"data")
    fr.add(path, kind=kind, owner=owner)
    return owner, path, fr.file_for(owner, kind)


def test_move_moves_the_file_and_the_row(out):
    owner, path, row = _registered(out)
    target = out / "b.flac"
    assert fr.move(row, target) == "moved"
    assert target.read_bytes() == b"data" and not path.exists()
    assert fr.file_for(owner, "audio").path == target


def test_move_conflict_leaves_everything(out):
    owner, path, row = _registered(out)
    other = _touch(out / "b.flac", b"other")
    assert fr.move(row, other) == "conflict"
    assert path.exists() and other.read_bytes() == b"other"
    assert fr.file_for(owner, "audio").path == path


@pytest.mark.parametrize("insensitive", [True, False])
def test_move_case_only_rename(out, monkeypatch, insensitive):
    _case(monkeypatch, insensitive)
    owner, path, row = _registered(out, "a.flac")
    assert fr.move(row, out / "A.flac") == "moved"
    assert "A.flac" in os.listdir(out)
    assert fr.file_for(owner, "audio").rel_path == "A.flac"


def test_move_missing_source_forgets_the_row(out):
    owner, path, row = _registered(out)
    path.unlink()
    assert fr.move(row, out / "b.flac") == "missing"
    assert fr.file_for(owner, "audio") is None


def test_move_failure_leaves_the_row_unchanged(out, monkeypatch):
    owner, path, row = _registered(out)

    def locked(src, dst):
        raise PermissionError(13, "in use")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", locked)
        assert fr.move(row, out / "b.flac") == "error"
    assert path.exists() and fr.file_for(owner, "audio").path == path


def test_move_persistent_sharing_violation_is_an_error(out, monkeypatch):
    owner, path, row = _registered(out)
    monkeypatch.setattr(ts, "_replace", lambda src, dst: False)
    assert fr.move(row, out / "b.flac") == "error"
    assert fr.file_for(owner, "audio").path == path


def test_move_puts_the_file_back_when_the_row_update_fails(out):
    owner, path, row = _registered(out, "a.md", kind="transcript")
    with pytest.raises(sqlite3.IntegrityError):
        fr.move(row, out / "a.txt")          # a transcript row must end in .md
    assert path.exists() and not (out / "a.txt").exists()
    assert fr.file_for(owner, "transcript").path == path


def test_move_outside_the_root_raises_before_touching_the_disk(out, tmp_path):
    _, path, row = _registered(out)
    with pytest.raises(ValueError):
        fr.move(row, tmp_path / "elsewhere" / "a.flac")
    assert path.exists()


def test_repoint_and_refresh(out):
    owner, path, row = _registered(out)
    renamed = out / "renamed.flac"
    path.rename(renamed)
    renamed.write_bytes(b"longer data")
    fr.repoint(row, renamed)
    again = fr.file_for(owner, "audio")
    assert again.path == renamed and again.size == len(b"longer data")
    renamed.write_bytes(b"x")
    fr.refresh(again)
    assert fr.file_for(owner, "audio").size == 1
    renamed.unlink()
    fr.refresh(fr.file_for(owner, "audio"))      # a missing file keeps its stats
    assert fr.file_for(owner, "audio").size == 1


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def test_sync_registers_pattern_files_for_existing_owners(out):
    owner = _owner("Session")
    names = ["Session.md", "Session.summary.md", "Session_diar.json", "Session.md.bak",
             "Session_excerpt_Ann.mp3", "Session_excerpt_Ann.txt"]
    for n in names:
        _touch(out / n)
    report = fr.sync()
    assert sorted(p.name for p in report.registered) == sorted(names)
    assert report.unclaimed == [] and report.errors == []
    kinds = {r.kind: r.label for r in fr.files_for(owner)}
    assert kinds == {"transcript": None, "summary": None, "sidecar": None, "backup": None,
                     "excerpt": "Ann", "excerpt_text": "Ann"}
    assert fr.last_report() is report


def test_sync_registers_data_files_and_skips_active_recordings(data):
    done = seed_recording(status="completed")
    active = seed_recording(status="recording")
    degraded = seed_recording(status="recording")
    rec_dir = data / "recordings" / done.id
    _touch(rec_dir / "per-user" / "mic" / "a.wav")
    _touch(rec_dir / "per-user" / "12345" / "a.wav")
    _touch(rec_dir / "per-user" / "other" / "a.wav")      # not a track name
    _touch(rec_dir / "live_transcript.md")
    _touch(data / "recordings" / active.id / "per-user" / "mic" / "a.wav")
    seed_profile("alice")
    _touch(data / "profiles" / "embeddings" / "alice.mp3")
    camp = create_campaign("Camp")
    _touch(data / "campaigns" / camp.slug / "journal.md")
    with db.transaction() as conn:
        conn.execute("UPDATE recordings SET capture_status = 'degraded' WHERE id = ?",
                     (degraded.id,))

    report = fr.sync()
    assert report.errors == [] and report.unclaimed == []
    owner = fr.Owner("recording", done.id)
    got = {(r.kind, r.label): r for r in fr.files_for(owner)}
    assert set(got) == {("combined", None), ("live_draft", None),
                        ("per_user", "mic"), ("per_user", "12345")}
    assert got[("per_user", "mic")].size is None and got[("combined", None)].size > 0
    assert fr.files_for(fr.Owner("recording", active.id)) == []
    assert fr.files_for(fr.Owner("recording", degraded.id)) == []
    assert fr.file_for(fr.Owner.for_profile_key("alice"), "reference_clip") is not None
    assert fr.file_for(fr.Owner.for_campaign_slug(camp.slug), "journal") is not None


def test_sync_does_not_register_flac_audio(out):
    owner = _owner("Session")
    flac = _touch(out / "Session.flac")
    report = fr.sync()
    assert flac in report.unclaimed and fr.file_for(owner, "audio") is None
    fr.add(flac, kind="audio", owner=owner)
    assert fr.sync().unclaimed == []


def test_sync_lists_unclaimed_and_second_files_for_an_owned_slot(out):
    owner = _owner("Session")
    stray = _touch(out / "Nobody.summary.md")
    fr.add(out / "Renamed.summary.md", kind="summary", owner=owner)   # the slot is taken
    again = _touch(out / "Session.summary.md")
    unrelated = _touch(out / "notes.txt")
    report = fr.sync()
    assert stray in report.unclaimed and again in report.unclaimed
    assert unrelated not in report.unclaimed
    assert fr.file_for(owner, "summary").rel_path == "Renamed.summary.md"


def test_sync_excerpt_stem_containing_the_marker(out):
    owner = _owner("a_excerpt_b")
    ex = _touch(out / "a_excerpt_b_excerpt_Ann.mp3")
    fr.sync()
    assert fr.file_for(owner, "excerpt", "Ann").path == ex


def test_sync_lists_missing_files_but_not_missing_transcripts(out):
    owner = _owner("Session")
    md, summary = _touch(out / "Session.md"), _touch(out / "Session.summary.md")
    fr.sync()
    md.unlink()
    summary.unlink()
    report = fr.sync()
    assert [r.kind for r in report.missing] == ["summary"]
    assert report.missing[0].size == 1                    # stats kept
    assert fr.file_for(owner, "summary") is not None      # never deleted by sync


def test_sync_refreshes_changed_stats(out):
    owner = _owner("Session")
    md = _touch(out / "Session.md", b"1")
    fr.sync()
    md.write_bytes(b"12345")
    report = fr.sync()
    assert report.refreshed == [md] and fr.file_for(owner, "transcript").size == 5


def test_sync_fills_stats_of_imported_rows(out):
    owner = _owner("Session")
    flac = _touch(out / "Session.flac", b"abc")
    seed_file(flac, "audio", owner)
    with db.transaction() as conn:
        conn.execute("UPDATE files SET size = NULL, mtime_ns = NULL")
    assert fr.sync().refreshed == [flac]
    assert fr.file_for(owner, "audio").size == 3


def test_second_sync_changes_nothing_and_opens_no_transaction(out, monkeypatch):
    _owner("Session")
    _touch(out / "Session.md")
    _touch(out / "Session_diar.json")
    first = fr.sync()
    assert len(first.registered) == 2

    def no_transaction(*a, **kw):
        raise AssertionError("sync wrote when nothing changed")

    monkeypatch.setattr(db, "transaction", no_transaction)
    second = fr.sync()
    assert (second.registered, second.refreshed, second.missing, second.errors) == ([], [], [], [])


def test_sync_skips_a_file_deleted_between_scan_and_write(out, monkeypatch):
    _owner("Session")
    sidecar = _touch(out / "Session_diar.json")
    scan = fr._scan_output

    def scan_then_delete(*a, **kw):
        found = scan(*a, **kw)
        sidecar.unlink()
        return found

    monkeypatch.setattr(fr, "_scan_output", scan_then_delete)
    report = fr.sync()
    assert report.registered == [] and _count() == 0


def test_sync_isolates_per_file_errors(out, monkeypatch):
    owner = _owner("Session")
    _touch(out / "Session_diar.json")
    _touch(out / "Session.summary.md")
    real = fr._stat

    def stat(path, kind):
        if Path(path).name == "Session_diar.json":
            raise OSError("boom")
        return real(path, kind)

    monkeypatch.setattr(fr, "_stat", stat)
    report = fr.sync()
    assert [p.name for p in report.registered] == ["Session.summary.md"]
    assert len(report.errors) == 1 and "Session_diar.json" in report.errors[0]
    assert fr.file_for(owner, "sidecar") is None


def test_sync_never_raises_on_a_database_error(out, monkeypatch):
    _owner("Session")
    _touch(out / "Session.md")

    def broken(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "transaction", broken)
    report = fr.sync()
    assert report.registered == [] and any("locked" in e for e in report.errors)


def test_sync_reports_a_transcript_row_that_does_not_match_its_stem(out):
    owner = _owner("Session")
    fr.add(_touch(out / "Other.md"), kind="transcript", owner=owner)
    assert any("Session" in e for e in fr.sync().errors)


def test_sync_maps_an_nfd_file_to_its_nfc_owner(out):
    stem = "Café Session"
    owner = _owner(stem)
    nfd = unicodedata.normalize("NFD", stem) + ".md"
    path = _touch(out / nfd)
    report = fr.sync()
    assert report.errors == [] and report.unclaimed == []
    row = fr.file_for(owner, "transcript")
    assert row.rel_path == nfd and row.path.exists()      # the on-disk spelling is stored
    second = fr.sync()
    assert second.registered == [] and second.unclaimed == []
    assert fr.forget(out / (unicodedata.normalize("NFC", stem) + ".md")) == [path]


def test_sync_matches_case_variants_only_where_names_fold(out, monkeypatch):
    owner = _owner("Session")
    _touch(out / "session.summary.md")
    _case(monkeypatch, False)
    assert fr.sync().unclaimed == [out / "session.summary.md"]
    assert fr.file_for(owner, "summary") is None
    _case(monkeypatch, True)
    fr.sync()
    assert fr.file_for(owner, "summary").rel_path == "session.summary.md"


def test_sync_skips_the_output_root_inside_the_data_dir(data):
    done = seed_recording()
    out = data / "recordings"                 # an output root that is also a data subtree
    _owner("x")
    report = fr.sync(output_dir=out)
    assert fr.files_for(fr.Owner("recording", done.id)) == [] and report.errors == []


# ---------------------------------------------------------------------------
# The cached report, the throttle, and reconcile
# ---------------------------------------------------------------------------

def test_drop_from_report(out):
    stray = _touch(out / "Nobody.summary.md")
    owner = _owner("Session")
    summary = _touch(out / "Session.summary.md")
    fr.sync()
    summary.unlink()
    report = fr.sync()
    assert report.unclaimed == [stray] and len(report.missing) == 1
    fr.drop_from_report(file_ids=[report.missing[0].id], paths=[stray])
    assert fr.last_report().missing == [] and fr.last_report().unclaimed == []


def test_sync_if_due_throttles_per_directory_pair(out, tmp_path):
    first = fr.sync_if_due()
    assert first is not None
    assert fr.sync_if_due() is None
    assert fr.sync_if_due(force=True) is not None
    other = tmp_path / "other-out"
    other.mkdir()
    assert fr.sync_if_due(output_dir=other) is not None


def test_reset_state_clears_report_and_throttle(out):
    fr.sync_if_due()
    fr.reset_state()
    assert fr.last_report() is None and fr.sync_if_due() is not None


@pytest.mark.parametrize("mode,calls", [("always", ["sync"]), ("throttled", ["due"]), ("never", [])])
def test_reconcile_sync_modes(out, monkeypatch, mode, calls):
    seen = []
    monkeypatch.setattr(fr, "sync", lambda *a, **kw: seen.append("sync"))
    monkeypatch.setattr(fr, "sync_if_due", lambda *a, **kw: seen.append("due"))
    ts.reconcile(out, sync=mode)
    assert seen == calls


def test_reconcile_registers_files_by_default(out):
    _touch(out / "Session.md", b"---\ntitle: x\n---\n\nbody\n")
    ts.reconcile(out)
    owner = fr.Owner.for_stem("Session")
    assert fr.file_for(owner, "transcript").path == out / "Session.md"
    assert fr.last_report().registered == [out / "Session.md"]


# ---------------------------------------------------------------------------
# Write sites register what they write
# ---------------------------------------------------------------------------

def _row(owner: fr.Owner, kind: str, label: str | None = None):
    return fr.file_for(owner, kind, label)


def test_register_adds_the_transcript_row(out):
    _touch(out / "s01.md", b"text")
    tid = ts.register("s01", origin="job")
    row = _row(fr.Owner("transcript", tid), "transcript")
    assert row.path == out / "s01.md" and row.size == 4


def test_overwriting_a_job_drops_the_stale_sidecar_row(out):
    _touch(out / "s01.md")
    tid = ts.register("s01", origin="job")
    ts.write_sidecar(out / "s01.md", {"diarization_segments": [], "speaker_map": {"SPEAKER_00": "A"}})
    owner = fr.Owner("transcript", tid)
    assert _row(owner, "sidecar") is not None
    ts.register("s01", origin="job")
    assert _row(owner, "sidecar") is None
    assert not (out / "s01_diar.json").exists()


def test_write_sidecar_registers_the_sidecar_and_the_audio(out):
    md = _touch(out / "s01.md")
    audio = _touch(out / "s01.mp4")
    ts.write_sidecar(md, {"diarization_segments": [], "input_path": str(audio)})
    owner = fr.Owner.for_stem("s01")
    assert _row(owner, "sidecar").path == out / "s01_diar.json"
    assert _row(owner, "audio").path == audio


def test_write_sidecar_outside_the_output_folder_clears_the_audio(out, tmp_path):
    md = _touch(out / "s01.md")
    audio = _touch(out / "s01.mp4")
    ts.write_sidecar(md, {"diarization_segments": [], "input_path": str(audio)})
    ts.write_sidecar(md, {"diarization_segments": [], "input_path": str(tmp_path / "elsewhere.mp4")})
    assert _row(fr.Owner.for_stem("s01"), "audio") is None
    assert not audio.exists()  # the replaced copy goes with it


def test_write_sidecar_never_tracks_a_recordings_audio(out, data):
    md = _touch(out / "s01.md")
    combined = _touch(data / "recordings" / "r1" / "combined.wav")
    ts.write_sidecar(md, {"diarization_segments": [], "input_path": str(combined)})
    assert _row(fr.Owner.for_stem("s01"), "audio") is None
    assert combined.exists()


def test_set_audio_refuses_a_recordings_audio(out, data):
    md = _touch(out / "s01.md")
    combined = _touch(data / "recordings" / "r1" / "combined.wav")
    with pytest.raises(ValueError):
        ts.set_audio(md, combined)


def test_set_audio_replaces_and_deletes_the_previous_file(out):
    md = _touch(out / "s01.md")
    first, second = _touch(out / "s01.mp4"), _touch(out / "s01.flac")
    ts.set_audio(md, first)
    ts.set_audio(md, second)
    assert _row(fr.Owner.for_stem("s01"), "audio").path == second
    assert not first.exists() and second.exists()
    ts.set_audio(md, None)
    assert _row(fr.Owner.for_stem("s01"), "audio") is None and not second.exists()


def test_save_summary_registers_beside_a_registered_transcript(out, tmp_path):
    _touch(out / "s01.md")
    ts.register("s01", origin="job")
    ts.save_summary(out / "s01.summary.md", "summary")
    assert _row(fr.Owner.for_stem("s01"), "summary").path == out / "s01.summary.md"


def test_save_summary_outside_the_roots_registers_nothing(out, tmp_path):
    _touch(out / "s01.md")
    ts.register("s01", origin="job")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = _count()
    ts.save_summary(elsewhere / "s01.summary.md", "summary")  # a CLI --output next to a copy
    ts.save_summary(elsewhere / "custom.md", "summary")
    assert (elsewhere / "s01.summary.md").is_file() and _count() == before


def test_save_transcript_restats_the_row(out):
    md = _touch(out / "s01.md", b"one")
    tid = ts.register("s01", origin="job")
    ts.save_transcript(md, "longer text")
    assert _row(fr.Owner("transcript", tid), "transcript").size == len("longer text")


def test_relink_repoints_the_transcript_row(out):
    _touch(out / "old.md")
    tid = ts.register("old", origin="job")
    (out / "old.md").rename(out / "new.md")
    ts.reconcile(out)
    ts.relink("old", "new", output_dir=out)
    row = _row(fr.Owner("transcript", tid), "transcript")
    assert row.rel_path == "new.md"


def test_relink_registers_when_the_transcript_had_no_row(out):
    _touch(out / "old.md")
    with db.transaction() as conn:
        tid = ts.ensure_row(conn, "old", out)
        conn.execute("UPDATE transcripts SET missing_since = '2026-01-01T00:00:00Z' WHERE id = ?", (tid,))
    _touch(out / "new.md")
    ts.relink("old", "new", output_dir=out)
    assert _row(fr.Owner("transcript", tid), "transcript").rel_path == "new.md"


def test_reconcile_case_only_rename_repoints_the_row(out, monkeypatch):
    _case(monkeypatch, True)
    _touch(out / "Session.md")
    tid = ts.register("Session", origin="job")
    (out / "Session.md").rename(out / "session-tmp.md")
    (out / "session-tmp.md").rename(out / "SESSION.md")
    ts.reconcile(out)
    assert _row(fr.Owner("transcript", tid), "transcript").rel_path == "SESSION.md"


def test_extract_speaker_excerpts_registers_clips_with_sanitised_labels(out):
    from datetime import datetime
    from unittest.mock import MagicMock, patch

    from wisper_transcribe.models import AlignedSegment
    from wisper_transcribe.web.jobs import COMPLETED, Job, _extract_speaker_excerpts

    md = _touch(out / "s01.md")
    tid = ts.register("s01", origin="job")
    audio = _touch(out / "in.mp3")
    job = Job(id="j1", status=COMPLETED, created_at=datetime.now(), input_path=str(audio),
              kwargs={}, output_path=str(md))
    aligned = [AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER.00")]

    def fake_ffmpeg(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"mp3")
        return MagicMock(returncode=0)

    with patch("wisper_transcribe.web.jobs.subprocess.run", side_effect=fake_ffmpeg):
        _extract_speaker_excerpts(job, md, aligned_segments=aligned)

    owner = fr.Owner("transcript", tid)
    assert _row(owner, "excerpt", "SPEAKER_00").path == out / "s01_excerpt_SPEAKER_00.mp3"
    assert _row(owner, "excerpt_text", "SPEAKER_00").path == out / "s01_excerpt_SPEAKER_00.txt"


def test_extract_speaker_excerpts_skips_a_clip_ffmpeg_did_not_write(out):
    from datetime import datetime
    from unittest.mock import patch

    from wisper_transcribe.models import AlignedSegment
    from wisper_transcribe.web.jobs import COMPLETED, Job, _extract_speaker_excerpts

    md = _touch(out / "s01.md")
    tid = ts.register("s01", origin="job")
    job = Job(id="j1", status=COMPLETED, created_at=datetime.now(),
              input_path=str(_touch(out / "in.mp3")), kwargs={}, output_path=str(md))
    aligned = [AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00")]
    with patch("wisper_transcribe.web.jobs.subprocess.run", side_effect=OSError("no ffmpeg")):
        _extract_speaker_excerpts(job, md, aligned_segments=aligned)
    owner = fr.Owner("transcript", tid)
    assert _row(owner, "excerpt", "SPEAKER_00") is None
    assert _row(owner, "excerpt_text", "SPEAKER_00") is not None


def test_refine_registers_its_backup(out):
    from unittest.mock import MagicMock, patch

    from click.testing import CliRunner

    from wisper_transcribe.cli import main

    md = _touch(out / "ep.md", b"---\ntitle: x\n---\n\n**A** *(00:00)*: Kira said hi.\n")
    ts.register("ep", origin="job")
    client = MagicMock(provider="mock", model="m1")
    client.complete_json.return_value = {"changes": [{"original": "Kira", "corrected": "Kyra"}]}
    with patch("wisper_transcribe.cli._get_llm_client", return_value=client):
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra"])
        result = CliRunner().invoke(main, ["refine", str(md), "--apply", "--no-color"])
    assert result.exit_code == 0, result.output
    assert _row(fr.Owner.for_stem("ep"), "backup").path == out / "ep.md.bak"


def test_refine_backup_outside_the_roots_registers_nothing(out, tmp_path):
    from unittest.mock import MagicMock, patch

    from click.testing import CliRunner

    from wisper_transcribe.cli import main

    elsewhere = tmp_path / "elsewhere"
    md = _touch(elsewhere / "ep.md", b"---\ntitle: x\n---\n\n**A** *(00:00)*: Kira said hi.\n")
    client = MagicMock(provider="mock", model="m1")
    client.complete_json.return_value = {"changes": [{"original": "Kira", "corrected": "Kyra"}]}
    before = _count()
    with patch("wisper_transcribe.cli._get_llm_client", return_value=client):
        CliRunner().invoke(main, ["config", "set", "hotwords", "Kyra"])
        result = CliRunner().invoke(main, ["refine", str(md), "--apply", "--no-color"])
    assert result.exit_code == 0, result.output
    assert (elsewhere / "ep.md.bak").is_file() and _count() == before


def test_enroll_registers_the_reference_clip_and_rename_moves_the_row(data):
    from unittest.mock import patch

    import numpy as np

    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.speaker_manager import enroll_speaker, rename_profile

    def fake_clip(audio, segments, label, out_path, **kwargs):
        Path(out_path).write_bytes(b"mp3")

    with patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=np.ones(512)), \
            patch("wisper_transcribe.speaker_manager._save_reference_clip", side_effect=fake_clip):
        enroll_speaker("alice", "Alice", "DM", Path("a.wav"),
                       [DiarizationSegment(0.0, 5.0, "SPEAKER_00")], "SPEAKER_00")
    owner = fr.Owner.for_profile_key("alice")
    assert _row(owner, "reference_clip").rel_path == "profiles/embeddings/alice.mp3"

    rename_profile("alice", "Alicia")
    assert _row(owner, "reference_clip").rel_path == "profiles/embeddings/alicia.mp3"
    assert (data / "profiles" / "embeddings" / "alicia.mp3").is_file()


def test_rename_profile_repoints_the_row_when_the_clip_cannot_move(data, monkeypatch):
    seed_profile("alice")
    clip = _touch(data / "profiles" / "embeddings" / "alice.mp3")
    fr.add(clip, kind="reference_clip", owner=fr.Owner.for_profile_key("alice"))

    def refuse(self, target):
        raise PermissionError("held open")

    monkeypatch.setattr(Path, "rename", refuse)
    from wisper_transcribe.speaker_manager import rename_profile
    rename_profile("alice", "Alicia")
    row = _row(fr.Owner.for_profile_key("alicia"), "reference_clip")
    assert row.path == clip and clip.exists()


def test_removing_a_profile_deletes_its_clip_and_row(data):
    from wisper_transcribe.speaker_manager import remove_profile

    seed_profile("alice")
    clip = _touch(data / "profiles" / "embeddings" / "alice.mp3")
    fr.add(clip, kind="reference_clip", owner=fr.Owner.for_profile_key("alice"))
    remove_profile("alice")
    assert not clip.exists() and _count() == 0


def test_resetting_profiles_deletes_every_clip_and_row(data):
    from wisper_transcribe.speaker_manager import reset_profiles

    clips = []
    for key in ("alice", "bob"):
        seed_profile(key)
        clips.append(_touch(data / "profiles" / "embeddings" / f"{key}.mp3"))
        fr.add(clips[-1], kind="reference_clip", owner=fr.Owner.for_profile_key(key))
    assert reset_profiles() == 2
    assert not any(c.exists() for c in clips) and _count() == 0


def _fold_setup(out):
    from wisper_transcribe.campaign_manager import move_transcript_to_campaign

    create_campaign("My Game")
    _touch(out / "s1.md")
    ts.register("s1", origin="job")
    move_transcript_to_campaign("s1", "my-game")
    _touch(out / "s1.summary.md", b"A session happened.")


class _Client:
    provider, model = "fake", "m"

    def complete(self, system, user):
        return "## Story So Far\n\nIt happened."


def test_journal_fold_registers_journal_md(out):
    from wisper_transcribe import journal

    _fold_setup(out)
    result = journal.update_journal("my-game", _Client(), {})
    row = fr.file_for(fr.Owner.for_campaign_slug("my-game"), "journal")
    assert row is not None and row.path == result.path


def test_resetting_a_journal_forgets_its_row(out):
    from wisper_transcribe import journal

    _fold_setup(out)
    journal.update_journal("my-game", _Client(), {})
    journal.reset_journal("my-game")
    assert fr.file_for(fr.Owner.for_campaign_slug("my-game"), "journal") is None


def test_sync_journal_finishes_an_interrupted_fold_and_registers_it(out, data):
    from wisper_transcribe import journal

    _fold_setup(out)
    journal.update_journal("my-game", _Client(), {})
    jpath = journal.journal_path("my-game")
    pending = journal._pending_path(jpath)
    pending.write_bytes(jpath.read_bytes())  # a fold that committed but crashed before its move
    jpath.unlink()
    fr.forget(jpath)
    journal.sync_journal("my-game")
    assert fr.file_for(fr.Owner.for_campaign_slug("my-game"), "journal").path == jpath


def test_sync_journal_forgets_the_row_of_a_deleted_journal(out):
    from wisper_transcribe import journal

    _fold_setup(out)
    journal.update_journal("my-game", _Client(), {})
    journal.journal_path("my-game").unlink()
    journal.sync_journal("my-game")
    assert fr.file_for(fr.Owner.for_campaign_slug("my-game"), "journal") is None


def test_deleting_a_campaign_keeps_the_journal_file_unclaimed(out, data):
    from wisper_transcribe import journal
    from wisper_transcribe.campaign_manager import delete_campaign

    _fold_setup(out)
    result = journal.update_journal("my-game", _Client(), {})
    delete_campaign("my-game")
    assert result.path.is_file() and _count() == 1  # only the transcript row is left
    assert result.path in fr.sync(out, data).unclaimed


def test_local_finalise_registers_combined_per_user_and_the_live_draft(data):
    from tests.test_local_capture import (
        _block, _run_session_to_completion, instant_ticker, scripted_capture_factory,
    )
    from wisper_transcribe.web.local_capture import LocalCaptureManager

    blocks = {"mic-dev": [_block() for _ in range(3)], "sys-dev": [_block() for _ in range(3)]}
    mgr = LocalCaptureManager(data_dir=data, capture_factory=scripted_capture_factory(blocks),
                              ticker=instant_ticker(3))
    rec = mgr.start_session(None, "mic-dev", "sys-dev")
    _touch(data / "recordings" / rec.id / "live_transcript.md")
    _run_session_to_completion(mgr)

    owner = fr.Owner.for_recording(rec.id)
    assert _row(owner, "combined").rel_path == f"recordings/{rec.id}/combined.wav"
    assert _row(owner, "per_user", "mic").rel_path == f"recordings/{rec.id}/per-user/mic"
    assert _row(owner, "per_user", "system") is not None
    assert _row(owner, "live_draft") is not None


def test_recover_registers_combined_wav(data):
    from tests.test_recording_manager import _crashed_session
    from wisper_transcribe import recording_manager as rm

    rec = _crashed_session(data)
    rm.recover_recording(rec.id, data)
    assert _row(fr.Owner.for_recording(rec.id), "combined") is not None


def test_register_capture_files_registers_combined_and_numeric_tracks(data):
    from wisper_transcribe import recording_manager as rm

    rec = rm.create_recording("VC1", "G1", data_dir=data)
    _touch(data / "recordings" / rec.id / "combined.wav")
    _touch(data / "recordings" / rec.id / "per-user" / "123" / "0000.wav")
    rm.register_capture_files(rec.id, data)
    owner = fr.Owner.for_recording(rec.id)
    assert _row(owner, "combined") is not None
    assert _row(owner, "per_user", "123") is not None


# ---------------------------------------------------------------------------
# Deleting a transcript
# ---------------------------------------------------------------------------

def test_delete_transcript_removes_registered_and_unregistered_companions(out):
    md = _touch(out / "s01.md")
    ts.register("s01", origin="job")
    registered = [_touch(out / "s01.summary.md"), _touch(out / "s01.md.bak"),
                  _touch(out / "s01_excerpt_A.mp3")]
    owner = fr.Owner.for_stem("s01")
    fr.add(registered[0], kind="summary", owner=owner)
    fr.add(registered[1], kind="backup", owner=owner)
    fr.add(registered[2], kind="excerpt", owner=owner, label="A")
    audio = _touch(out / "s01 (take 2).flac")  # a name the stem can't derive
    fr.add(audio, kind="audio", owner=owner)
    unregistered = [_touch(out / "s01_diar.json"), _touch(out / "s01_excerpt_B.txt")]
    keep = _touch(out / "s02.summary.md")

    assert ts.delete_transcript("s01")
    for path in [md, audio, *registered, *unregistered]:
        assert not path.exists(), path
    assert keep.exists() and _count() == 0


def test_delete_transcript_never_deletes_a_recordings_audio(out, data):
    md = _touch(out / "s01.md")
    ts.register("s01", origin="job")
    combined = _touch(data / "recordings" / "r1" / "combined.wav")
    sidecar = _touch(out / "s01_diar.json", b'{"diarization_segments": [], "input_path": "%s"}'
                     % str(combined).encode())
    ts.delete_transcript("s01")
    assert not md.exists() and not sidecar.exists() and combined.exists()


# ---------------------------------------------------------------------------
# audio_path
# ---------------------------------------------------------------------------

def test_audio_path_prefers_the_audio_row_then_the_recordings_combined_wav(out, data):
    from tests._seed import seed_recording
    from wisper_transcribe.recording_manager import link_transcript

    md = _touch(out / "s01.md")
    ts.register("s01", origin="job")
    assert ts.audio_path(md) is None
    rec = seed_recording()
    link_transcript(rec.id, md)
    combined = data / "recordings" / rec.id / "combined.wav"
    assert ts.audio_path(md) == combined
    audio = _touch(out / "s01.flac")
    ts.set_audio(md, audio)
    assert ts.audio_path(md) == audio
    audio.unlink()  # a row whose file is gone falls through
    assert ts.audio_path(md) == combined


def test_read_sidecar_input_path_comes_from_audio_path(out, data):
    md = _touch(out / "s01.md")
    audio = _touch(out / "s01.flac")
    ts.write_sidecar(md, {"diarization_segments": [], "input_path": str(audio),
                          "speaker_map": {"SPEAKER_00": "A"}})
    assert ts.read_sidecar(md)["input_path"] == str(audio)


# ---------------------------------------------------------------------------
# Guards over the codebase
# ---------------------------------------------------------------------------

SRC = Path(__file__).parent.parent / "src" / "wisper_transcribe"


def _src_lines():
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield rel, no, line


def test_only_file_registry_writes_the_files_table():
    pattern = re.compile(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+files\b|UPDATE\s+files\b|DELETE\s+FROM\s+files\b",
                         re.IGNORECASE)
    offenders = [f"{rel}:{no}: {line.strip()}" for rel, no, line in _src_lines()
                 if rel not in ("file_registry.py", "db.py") and pattern.search(line)]
    assert offenders == [], "write `files` through file_registry:\n" + "\n".join(offenders)


# (file, function) pairs that write a file wisper owns and must register it.
_WRITE_SITES = [
    ("transcript_store.py", "register", "add_if_owned"),
    ("transcript_store.py", "_point_transcript_row", "add_if_owned"),
    ("transcript_store.py", "_refresh_transcript_row", "refresh"),
    ("transcript_store.py", "_register_summary", "add_if_owned"),
    ("transcript_store.py", "write_sidecar", "add_if_owned"),
    ("transcript_store.py", "set_audio", "file_registry.add("),
    ("web/jobs.py", "_extract_speaker_excerpts", "add_if_owned"),
    ("web/jobs.py", "_do_llm_work", "add_if_owned"),
    ("web/jobs.py", "_run_live_job", "add_if_owned"),
    ("cli.py", "refine", "add_if_owned"),
    ("cli.py", "summarize", "add_if_owned"),
    ("speaker_manager.py", "enroll_speaker", "add_if_owned"),
    ("journal.py", "sync_journal", "add_if_owned"),
    ("journal.py", "update_journal", "add_if_owned"),
    ("journal.py", "reset_journal", "forget_kind"),
    ("recording_manager.py", "register_capture_files", "add_if_owned"),
    ("recording_manager.py", "recover_recording", "add_if_owned"),
    ("web/local_capture.py", "_finalise", "register_capture_files"),
    ("web/discord_bot.py", "_finalise", "register_capture_files"),
]


def _function_source(rel: str, name: str) -> str:
    import ast

    text = (SRC / rel).read_text(encoding="utf-8")
    lines = text.splitlines()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{rel} has no function {name}")


@pytest.mark.parametrize("rel, name, call", _WRITE_SITES)
def test_write_sites_register_their_files(rel, name, call):
    assert call in _function_source(rel, name), f"{rel}:{name} must call {call}"


def test_audio_rel_path_lives_only_in_the_migrations_and_the_importer():
    offenders = sorted({rel for rel, _no, line in _src_lines()
                        if "audio_rel_path" in line and rel not in ("db.py", "legacy_import.py")})
    assert offenders == []
