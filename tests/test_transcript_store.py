"""transcript_store: registry rows, the delete ordering rule, atomic writes."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from wisper_transcribe import db, file_registry, transcript_store as ts
from wisper_transcribe.campaign_manager import (
    create_campaign,
    get_transcripts_for_campaign,
)
from wisper_transcribe.path_utils import get_output_dir

SRC = Path(__file__).parent.parent / "src" / "wisper_transcribe"


@pytest.fixture
def out() -> Path:
    return get_output_dir()


def _md(out: Path, stem: str, text: str = "---\ntitle: x\n---\n\nbody\n") -> Path:
    p = out / f"{stem}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _rows() -> dict[str, bool]:
    with db.connection() as conn:
        return {r[0]: r[1] is not None for r in conn.execute(
            "SELECT stem, missing_since FROM transcripts")}


def _tid(stem: str) -> int:
    """The id of the single transcript named ``stem``."""
    with db.connection() as conn:
        rows = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchall()
    assert len(rows) == 1, f"{stem!r} matches {len(rows)} rows"
    return rows[0][0]


# ---------------------------------------------------------------------------
# atomic_write_text
# ---------------------------------------------------------------------------

def test_atomic_write_creates_and_replaces(tmp_path):
    target = tmp_path / "a.md"
    ts.atomic_write_text(target, "one")
    ts.atomic_write_text(target, "two — ünïcode")
    assert target.read_text(encoding="utf-8") == "two — ünïcode"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


def test_atomic_write_failure_keeps_old_file_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "a.md"
    target.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ts.os, "replace", boom)
    with pytest.raises(OSError):
        ts.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


def _sharing_violation(code: int = 32) -> PermissionError:
    exc = PermissionError(13, "The process cannot access the file")
    exc.winerror = code
    return exc


def test_windows_replace_retries_sharing_violations(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_IS_WINDOWS", True)
    monkeypatch.setattr(ts.time, "sleep", lambda s: None)
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise _sharing_violation(32)
        real_replace(src, dst)

    monkeypatch.setattr(ts.os, "replace", flaky)
    target = tmp_path / "a.md"
    ts.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert len(calls) == 3


def test_windows_replace_falls_back_to_in_place_write(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ts, "_IS_WINDOWS", True)
    sleeps = []
    monkeypatch.setattr(ts.time, "sleep", sleeps.append)

    def locked(src, dst):
        raise _sharing_violation(5)

    monkeypatch.setattr(ts.os, "replace", locked)
    target = tmp_path / "a.md"
    target.write_text("old", encoding="utf-8")
    ts.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]
    assert "locked by another program" in caplog.text
    assert len(sleeps) == ts._REPLACE_ATTEMPTS - 1
    assert sum(sleeps) <= 1.5


def test_windows_other_permission_errors_are_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_IS_WINDOWS", True)

    def denied(src, dst):
        raise _sharing_violation(1224)  # not a sharing violation

    monkeypatch.setattr(ts.os, "replace", denied)
    with pytest.raises(PermissionError):
        ts.atomic_write_text(tmp_path / "a.md", "new")


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows file locking")
def test_windows_write_while_another_handle_holds_the_target(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("old", encoding="utf-8")
    with open(target, encoding="utf-8"):  # opened without FILE_SHARE_DELETE
        ts.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["a.md"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_register_creates_row(out):
    md = _md(out, "s01")
    ts.register(md, origin="job")
    assert _rows() == {"s01": False}


def test_register_keeps_identity_and_campaign(out):
    create_campaign("Game")
    md = _md(out, "s01")
    first = ts.register(md, origin="job")
    _seed.move_to_campaign("s01", "game")
    assert ts.register(md, origin="job") == first  # overwrite keeps the row
    assert get_transcripts_for_campaign("game") == ["s01"]


def test_register_clears_missing_flag(out):
    create_campaign("Game")
    _seed.seed_transcript("s01", campaign="game")  # no .md yet: flagged missing
    assert _rows() == {"s01": True}
    md = _md(out, "s01")
    ts.register(md, origin="reconcile")
    assert _rows() == {"s01": False}


def test_register_not_in_a_transcript_folder_returns_none(out, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    md = elsewhere / "s01.md"
    md.write_text("x", encoding="utf-8")
    assert ts.register(md, origin="job") is None
    assert _rows() == {}


def test_register_rejects_unknown_origin(out):
    with pytest.raises(ValueError):
        ts.register(out / "s01.md", origin="guess")


def test_register_normalizes_to_nfc(out):
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Café")
    ts.register(out / f"{nfd}.md", origin="job")
    assert list(_rows()) == [unicodedata.normalize("NFC", "Café")]


def test_register_a_misplaced_sessions_root_md_keeps_its_campaign_row(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Stray", campaign=slug)
    md = _place(out, out, "Stray", tid)   # registered in the root though it belongs to Game

    assert ts.register(md, origin="job") == tid
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1
    assert ts.locate(tid).misplaced is True


def test_register_a_file_in_an_unrelated_folder_returns_none(out):
    folder = out / "notes"
    folder.mkdir()
    md = folder / "x.md"
    md.write_text("x", encoding="utf-8")
    assert ts.register(md, origin="job") is None
    assert _rows() == {}


def test_register_a_newcomer_folder_file_does_not_take_a_misplaced_row(out):
    """B's ``S`` is registered in the root; ``B/S.md`` must not become B's row."""
    cid, slug, folder = _claimed_campaign("B")
    tid = _seed.seed_transcript("S", campaign=slug)
    root_md = _place(out, out, "S", tid)          # S's registered .md is in the root
    newcomer = out / folder / "S.md"
    newcomer.write_text("# newcomer\n", encoding="utf-8")

    # The folder file is a newcomer: register creates a second row for B rather than
    # stealing the misplaced row's registration... but B already holds S, so it is refused.
    assert ts.register(newcomer, origin="job") is None
    assert ts.locate(tid).md == root_md
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1


def test_ensure_row_in_a_campaign_appends_a_position(out):
    cid, slug, folder = _claimed_campaign("Game")
    first = _seed.seed_transcript("one", campaign=slug)
    with db.transaction() as conn:
        second = ts.ensure_row(conn, "two", campaign_id=cid)
        positions = dict(conn.execute(
            "SELECT id, position FROM transcripts WHERE campaign_id = ?", (cid,)).fetchall())
    assert positions[first] == 0 and positions[second] == 1


# ---------------------------------------------------------------------------
# delete_transcript
# ---------------------------------------------------------------------------

def _with_companions(out: Path, stem: str, audio_name: str) -> list[Path]:
    audio = out / audio_name
    audio.write_bytes(b"audio")
    files = [
        out / f"{stem}.summary.md",
        out / f"{stem}_excerpt_SPEAKER_00.mp3",
        out / f"{stem}_excerpt_SPEAKER_00.txt",
        audio,
    ]
    for f in files[:3]:
        f.write_text("x", encoding="utf-8")
    sidecar = out / f"{stem}_diar.json"
    sidecar.write_text(json.dumps({"input_path": str(audio)}), encoding="utf-8")
    return files + [sidecar]


def test_delete_removes_file_row_links_and_companions(out):
    create_campaign("Game")
    md = _md(out, "s01")
    tid = ts.register(md, origin="job")
    _seed.move_to_campaign("s01", "game")
    companions = _with_companions(out, "s01", "s01_1.wav")  # collision-suffixed audio

    assert ts.delete_transcript(tid) == "deleted"
    assert not md.exists()
    assert all(not f.exists() for f in companions)
    assert _rows() == {}
    assert get_transcripts_for_campaign("game") == []


def test_delete_keeps_audio_outside_output_root(out, tmp_path):
    md = _md(out, "s01")
    tid = ts.register(md, origin="job")
    outside = tmp_path / "user-file.wav"
    outside.write_bytes(b"audio")
    (out / "s01_diar.json").write_text(json.dumps({"input_path": str(outside)}), encoding="utf-8")
    ts.delete_transcript(tid)
    assert outside.exists()


def test_delete_escapes_glob_in_stem(out):
    # "[1]" is a glob character class that's also a legal Windows filename.
    md = _md(out, "mix[1]")
    tid = ts.register(md, origin="job")
    other_clip = out / "mix1_excerpt_SPEAKER_00.mp3"   # matched by an unescaped "mix[1]_excerpt_*"
    own_clip = out / "mix[1]_excerpt_SPEAKER_00.mp3"
    other_clip.write_bytes(b"x")
    own_clip.write_bytes(b"x")
    ts.delete_transcript(tid)
    assert other_clip.exists()
    assert not own_clip.exists()


def test_delete_only_that_campaigns_copy_when_two_share_a_stem(out):
    ca, _, folda = _claimed_campaign("A")
    cb, _, foldb = _claimed_campaign("B")
    ta = _insert_session("S", ca, 0)
    tb = _insert_session("S", cb, 0)
    _place(out, out / folda, "S", ta)
    _place(out, out / foldb, "S", tb)

    assert ts.delete_transcript(ta) == "deleted"
    assert not (out / folda / "S.md").exists()
    assert (out / foldb / "S.md").exists()
    with db.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM transcripts")] == [tb]


def test_delete_missing_transcript_still_removes_row(out):
    create_campaign("Game")
    _seed.seed_transcript("gone", campaign="game")
    assert ts.delete_transcript(_tid("gone")) == "deleted"
    assert _rows() == {}


def test_delete_absent_id_is_absent(out):
    assert ts.delete_transcript(999999) == "absent"


def test_delete_reverts_recording_link(out):
    from wisper_transcribe.recording_manager import (
        create_recording, link_transcript, load_recordings, update_recording_status,
    )

    rec = create_recording("VC1", "G1")
    update_recording_status(rec.id, "completed")
    md = _md(out, rec.id)
    tid = ts.register(md, origin="job")
    link_transcript(rec.id, tid)
    assert load_recordings()[rec.id].status == "transcribed"

    ts.delete_transcript(tid)

    loaded = load_recordings()[rec.id]
    assert loaded.transcript_path is None
    assert loaded.status == "completed"


def test_row_delete_happens_after_md_unlink(out, monkeypatch):
    """Ordering rule: if the .md can't be removed, the row stays."""
    md = _md(out, "s01")
    tid = ts.register(md, origin="job")
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self == md:
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert ts.delete_transcript(tid) == "kept"
    assert md.exists()
    assert _rows() == {"s01": False}


# ---------------------------------------------------------------------------
# Guards over the codebase
# ---------------------------------------------------------------------------

def _src_lines():
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield rel, no, line


# (file, text on the line) pairs allowed to call .write_text() directly.
_WRITE_TEXT_ALLOWED = {
    ("web/app.py", "_sj.write_text"),                   # server.json runtime pointer
    ("web/jobs.py", "Path(job.live_output_path).write_text("),  # live draft, appended line by line
    ("db.py", "path.write_text("),                        # migration import report
}


def test_file_writes_are_atomic():
    """Transcript, summary, sidecar, and journal writes go through
    atomic_write_text(); a new direct write_text() must be reviewed here."""
    offenders = [
        f"{rel}:{no}: {line.strip()}"
        for rel, no, line in _src_lines()
        if ".write_text(" in line
        and not any(rel == f and s in line for f, s in _WRITE_TEXT_ALLOWED)
    ]
    assert offenders == [], "use transcript_store.atomic_write_text():\n" + "\n".join(offenders)


def test_only_transcript_store_deletes_transcripts():
    """Deleting a transcript .md anywhere but transcript_store skips the row
    and its links."""
    suspicious = re.compile(r"(md_path|transcript_path|transcript|\.md\b)[^\n]*\.unlink\(|\.unlink\([^\n]*\.md\b")
    offenders = [
        f"{rel}:{no}: {line.strip()}"
        for rel, no, line in _src_lines()
        if rel not in ("transcript_store.py", "file_registry.py") and suspicious.search(line)
    ]
    assert offenders == [], "use transcript_store.delete_transcript():\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

@pytest.fixture
def case_sensitive(monkeypatch):
    monkeypatch.setattr(ts, "_is_case_insensitive", lambda d: False)


def test_reconcile_registers_new_files(out, case_sensitive):
    _md(out, "s01")
    (out / "s01.summary.md").write_text("x", encoding="utf-8")  # not a transcript
    assert ts.reconcile(out)["added"] == 1
    assert _rows() == {"s01": False}


def test_reconcile_flags_missing_keeps_order_and_restores(out, case_sensitive):
    create_campaign("Game")
    for stem in ("s01", "s02", "s03"):
        md = _md(out, stem)
        tid = ts.register(md, origin="job")
        _seed.move_to_campaign(stem, "game")
    companion = out / "s02.summary.md"
    companion.write_text("x", encoding="utf-8")

    (out / "s02.md").unlink()   # deleted in Finder / sync hiccup
    ts.reconcile(out, sweep=True)
    assert _rows() == {"s01": False, "s02": True, "s03": False}
    assert get_transcripts_for_campaign("game") == ["s01", "s02", "s03"]
    assert companion.exists()  # kept: the row still exists

    _md(out, "s02")             # it comes back
    assert ts.reconcile(out)["restored"] == 1
    assert _rows()["s02"] is False


def test_reconcile_case_only_rename_keeps_row(out, monkeypatch):
    monkeypatch.setattr(ts, "_is_case_insensitive", lambda d: True)
    create_campaign("Game")
    md = _md(out, "session one")
    tid = ts.register(md, origin="job")
    _seed.move_to_campaign("session one", "game")

    (out / "session one.md").rename(out / "Session One.md")
    counts = ts.reconcile(out)
    assert counts["renamed"] == 1 and counts["added"] == 0
    assert _rows() == {"Session One": False}
    assert get_transcripts_for_campaign("game") == ["Session One"]


def test_reconcile_maps_nfd_filenames_to_nfc_rows(out, case_sensitive):
    import unicodedata

    nfc_name = unicodedata.normalize("NFC", "Café")
    ts.register(out / f"{nfc_name}.md", origin="job")
    _md(out, unicodedata.normalize("NFD", "Café"))
    counts = ts.reconcile(out)
    assert counts["added"] == 0
    assert _rows() == {nfc_name: False}


def test_reconcile_sweeps_old_temps_but_never_a_companion(out, case_sensitive):
    import time as _time

    md = _md(out, "kept")
    ts.register(md, origin="job")
    companions = ["kept.summary.md", "gone_diar.json", "gone_excerpt_SPEAKER_00.mp3",
                  "gone.summary.md", "gone.md.bak", "gone.flac", "random-audio.wav"]
    for name in companions:
        (out / name).write_text("x", encoding="utf-8")
    old_temp = out / f"{ts.TEMP_PREFIX}kept.md.1-1"
    new_temp = out / f"{ts.TEMP_PREFIX}kept.md.2-2"
    old_temp.write_text("x", encoding="utf-8")
    new_temp.write_text("x", encoding="utf-8")
    stale = _time.time() - ts._TEMP_MAX_AGE_S - 60
    os.utime(old_temp, (stale, stale))

    ts.reconcile(out)                     # list pages: no sweep
    assert old_temp.exists()
    ts.reconcile(out, sweep=True)         # startup
    assert all((out / n).exists() for n in companions)
    assert not old_temp.exists() and new_temp.exists()
    orphans = {p.name for p in ts.needs_attention(out).unclaimed}
    assert orphans == {"gone_diar.json", "gone_excerpt_SPEAKER_00.mp3", "gone.summary.md",
                       "gone.md.bak", "gone.flac"}


def test_case_probe_leaves_no_file(out):
    ts._case_insensitive.clear()
    ts._is_case_insensitive(out)
    assert not list(out.glob(f"{ts.TEMP_PREFIX}*"))


# ---------------------------------------------------------------------------
# relink
# ---------------------------------------------------------------------------

def _missing_entry(out):
    create_campaign("Game")
    md = _md(out, "old name")
    tid = ts.register(md, origin="job")
    _seed.move_to_campaign("old name", "game")
    (out / "old name.md").rename(out / "new name.md")
    # A new mtime, so reconcile can't prove the rename and lists it for relink.
    os.utime(out / "new name.md", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    ts.reconcile(out)  # old flagged missing, new registered
    return tid


def test_relink_moves_identity_to_new_file(out, case_sensitive):
    old_id = _missing_entry(out)
    assert [c.stem for c in ts.relink_candidates()] == ["new name"]

    ts.relink(old_id, out / "new name.md")

    with db.connection() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT id, stem, missing_since FROM transcripts")]
    assert rows == [(old_id, "new name", None)]
    assert get_transcripts_for_campaign("game") == ["new name"]
    assert ts.relink_candidates() == []


def test_relink_refuses_present_source_and_linked_target(out, case_sensitive):
    old_id = _missing_entry(out)
    present = _md(out, "present")
    present_id = ts.register(present, origin="job")
    with pytest.raises(ValueError):
        ts.relink(present_id, out / "new name.md")   # not missing
    _seed.move_to_campaign("new name", "game")
    with pytest.raises(ValueError):
        ts.relink(old_id, out / "new name.md")       # target already in a campaign    with pytest.raises(KeyError):
        ts.relink(old_id, out / "no such file.md")


# ---------------------------------------------------------------------------
# Renames keep a transcript's files
# ---------------------------------------------------------------------------

_BUMP_NS = 1_700_000_000_000_000_000
_COMPANION_SUFFIXES = (".summary.md", "_diar.json", "_excerpt_SPEAKER_00.mp3",
                       "_excerpt_SPEAKER_01.mp3", ".md.bak", ".flac")


def _session(out: Path, stem: str) -> int:
    """A registered transcript with a summary, sidecar, two excerpts, a backup, and audio."""
    md = _md(out, stem)
    tid = ts.register(md, origin="job")
    for suffix in _COMPANION_SUFFIXES:
        (out / f"{stem}{suffix}").write_text(suffix, encoding="utf-8")
    file_registry.add(out / f"{stem}.flac", kind="audio",
                      owner=file_registry.Owner("transcript", tid))
    file_registry.sync(out)
    return tid


def _registered_names(tid: int) -> set[str]:
    rows = file_registry.files_for(file_registry.Owner("transcript", tid))
    return {r.path.name for r in rows}


def _rename_provably_different(out: Path, old: str, new: str) -> None:
    """Rename a transcript's ``.md`` and change its mtime, so reconcile can't
    prove it is the same file."""
    (out / f"{old}.md").rename(out / f"{new}.md")
    os.utime(out / f"{new}.md", ns=(_BUMP_NS, _BUMP_NS))


def test_relink_carries_every_companion(out, case_sensitive):
    tid = _session(out, "Session 5")
    _rename_provably_different(out, "Session 5", "Hanataz 05")
    ts.reconcile(out, sweep=True)
    assert _rows()["Session 5"] is True

    assert ts.relink(tid, out / "Hanataz 05.md") == []

    expected = {"Hanataz 05.md"} | {f"Hanataz 05{s}" for s in _COMPANION_SUFFIXES}
    assert {p.name for p in out.iterdir()} == expected
    assert _registered_names(tid) == expected
    ts.reconcile(out, sweep=True)                         # the sweep deletes nothing
    assert {p.name for p in out.iterdir()} == expected
    assert ts.needs_attention(out).total == 0


def test_relink_conflict_keeps_both_files(out, case_sensitive):
    tid = _session(out, "Session 5")
    _rename_provably_different(out, "Session 5", "Hanataz 05")
    (out / "Hanataz 05.summary.md").write_text("someone else's", encoding="utf-8")
    ts.reconcile(out)

    kept = ts.relink(tid, out / "Hanataz 05.md")

    assert kept == [out / "Session 5.summary.md"]
    assert (out / "Session 5.summary.md").read_text(encoding="utf-8") == ".summary.md"
    assert (out / "Hanataz 05.summary.md").read_text(encoding="utf-8") == "someone else's"
    assert "Session 5.summary.md" in _registered_names(tid)
    assert "Hanataz 05_diar.json" in _registered_names(tid)    # the rest followed


def test_rename_registers_an_unregistered_companion_first(out, case_sensitive):
    tid = _session(out, "Session 5")
    (out / "Session 5_excerpt_SPEAKER_02.txt").write_text("late", encoding="utf-8")
    assert "Session 5_excerpt_SPEAKER_02.txt" not in _registered_names(tid)
    _rename_provably_different(out, "Session 5", "Hanataz 05")
    ts.reconcile(out)
    ts.relink(tid, out / "Hanataz 05.md")
    assert (out / "Hanataz 05_excerpt_SPEAKER_02.txt").is_file()
    assert "Hanataz 05_excerpt_SPEAKER_02.txt" in _registered_names(tid)


def test_rename_does_not_touch_another_transcripts_files(out, case_sensitive):
    tid = _session(out, "Session 5")
    other = _session(out, "Session 5 B")
    ts.rename_companions(tid, "Session 5", "Hanataz 05", src_dir=out)
    assert (out / "Session 5 B.summary.md").is_file()
    assert "Session 5 B.summary.md" in _registered_names(other)
    assert (out / "Hanataz 05.summary.md").is_file()


def test_automatic_match_renames_the_transcript_and_its_files(out, case_sensitive):
    create_campaign("Game")
    for stem in ("s01", "Session 5", "s03"):
        sid = _session(out, stem)
        _seed.move_to_campaign(stem, "game")
    tid = _tid("Session 5")
    ts.write_sidecar(out / "Session 5.md", {"diarization_segments": _SEGS,
                                            "speaker_map": {"SPEAKER_00": "Alice"},
                                            "input_path": str(out / "Session 5.flac")})

    os.replace(out / "Session 5.md", out / "Hanataz 05.md")   # keeps the mtime
    counts = ts.reconcile(out, sweep=True)

    assert counts["renamed"] == 1 and counts["added"] == 0 and counts["missing"] == 0
    assert _rows() == {"s01": False, "Hanataz 05": False, "s03": False}
    assert get_transcripts_for_campaign("game") == ["s01", "Hanataz 05", "s03"]
    assert ts.read_sidecar(out / "Hanataz 05.md")["speaker_map"] == {"SPEAKER_00": "Alice"}
    expected = {"Hanataz 05.md"} | {f"Hanataz 05{s}" for s in _COMPANION_SUFFIXES}
    assert _registered_names(tid) == expected
    assert not list(out.glob("Session 5*"))
    found = ts.needs_attention(out)
    assert not found.missing_transcripts and not found.unclaimed and not found.missing_files
    # The three campaign sessions still sit in the root, so all three are misplaced.
    assert {loc.stem for loc in found.misplaced} == {"s01", "Hanataz 05", "s03"}


def test_automatic_match_needs_one_missing_row_and_one_new_file(out, case_sensitive):
    for stem in ("a", "b"):
        md = _md(out, stem)
        os.utime(out / f"{stem}.md", ns=(_BUMP_NS, _BUMP_NS))
        ts.register(md, origin="job")
    os.replace(out / "a.md", out / "c.md")
    (out / "b.md").unlink()                    # two missing rows share c.md's size and mtime
    counts = ts.reconcile(out)
    assert counts["renamed"] == 0 and counts["added"] == 1
    assert _rows() == {"a": True, "b": True, "c": False}


def test_automatic_match_refuses_two_new_files_with_one_stat(out, case_sensitive):
    import shutil

    md = _md(out, "a")
    ts.register(md, origin="job")
    os.replace(out / "a.md", out / "c.md")
    shutil.copy2(out / "c.md", out / "d.md")   # same size and mtime
    counts = ts.reconcile(out)
    assert counts["renamed"] == 0 and counts["added"] == 2
    assert _rows() == {"a": True, "c": False, "d": False}


def test_case_only_rename_on_a_case_insensitive_filesystem_moves_companions(out, monkeypatch):
    monkeypatch.setattr(ts, "_is_case_insensitive", lambda d: True)
    tid = _session(out, "session one")

    (out / "session one.md").rename(out / "Session One.md")
    counts = ts.reconcile(out)

    assert counts["renamed"] == 1
    names = {p.name for p in out.iterdir()}
    assert names == {"Session One.md"} | {f"Session One{s}" for s in _COMPANION_SUFFIXES}
    assert _registered_names(tid) == names


def test_a_locked_companion_stays_put_and_is_reported(out, case_sensitive, monkeypatch):
    tid = _session(out, "s")

    def locked(src, dst):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(ts.os, "replace", locked)
    kept = ts.rename_companions(tid, "s", "t", src_dir=out)

    assert {p.name for p in kept} == {f"s{s}" for s in _COMPANION_SUFFIXES}
    assert all((out / f"s{s}").is_file() for s in _COMPANION_SUFFIXES)
    assert _registered_names(tid) == {"s.md"} | {f"s{s}" for s in _COMPANION_SUFFIXES}


def test_rename_companions_ignores_an_unchanged_stem(out, case_sensitive):
    tid = _session(out, "s")
    assert ts.rename_companions(tid, "s", "s", src_dir=out) == []
    assert (out / "s.summary.md").is_file()


def test_rename_companions_moves_registered_leftovers_to_dst(out, case_sensitive):
    """A companion left under an older name by a partial rename moves with dst_dir."""
    tid = _session(out, "Session 5")
    old_sidecar = out / "Old name_diar.json"
    old_sidecar.write_text("{}", encoding="utf-8")
    file_registry.add(old_sidecar, kind="sidecar", owner=file_registry.Owner("transcript", tid),
                      output_dir=out)
    dst = out / "Camp"
    dst.mkdir()

    kept = ts.rename_companions(tid, "Session 5", "Session 5", src_dir=out, dst_dir=dst)

    assert kept == []
    assert (dst / "Old name_diar.json").is_file()


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------

def test_needs_attention_lists_orphans_and_vanished_files(out, case_sensitive):
    create_campaign("Game")
    _session(out, "kept")
    lost = _md(out, "lost")
    lost_id = ts.register(lost, origin="job")
    _seed.move_to_campaign("lost", "game")
    (out / "lost.md").unlink()
    (out / "ghost.summary.md").write_text("orphan", encoding="utf-8")
    (out / "kept.summary.md").unlink()
    ts.reconcile(out, sweep=True)

    found = ts.needs_attention(out)

    assert [(m.stem, m.campaign) for m in found.missing_transcripts] == [("lost", "Game")]
    assert [r.path.name for r in found.missing_files] == ["kept.summary.md"]
    assert [p.name for p in found.unclaimed] == ["ghost.summary.md"]
    assert found.total == 3
    assert (out / "ghost.summary.md").exists()           # the sweep never deletes it


def test_needs_attention_syncs_when_no_report_exists(out, case_sensitive):
    (out / "ghost_diar.json").write_text("{}", encoding="utf-8")
    file_registry.reset_state()
    assert [p.name for p in ts.needs_attention(out).unclaimed] == ["ghost_diar.json"]


def test_delete_unowned_file_deletes_only_what_nothing_owns(out, case_sensitive, tmp_path):
    _session(out, "s")
    (out / "ghost.summary.md").write_text("x", encoding="utf-8")
    outside = tmp_path / "elsewhere.summary.md"
    outside.write_text("x", encoding="utf-8")
    (out / "sub").mkdir()

    assert ts.delete_unowned_file(out / "s.summary.md") is False      # registered
    assert ts.delete_unowned_file(out / "s.md") is False              # the transcript itself
    assert ts.delete_unowned_file(outside) is False                   # outside the root
    assert ts.delete_unowned_file(out / "sub") is False               # not a file
    assert ts.delete_unowned_file(out / "sub" / ".." / "s.summary.md") is False
    assert ts.delete_unowned_file(out / "missing.summary.md") is False
    assert ts.delete_unowned_file(out / "ghost.summary.md") is True
    assert not (out / "ghost.summary.md").exists() and (out / "s.summary.md").exists()
    assert outside.exists()


def test_companion_stem_recognises_backups_and_audio():
    assert ts._companion_stem("a b.md.bak") == "a b"
    assert ts._companion_stem("a b.flac") == "a b"
    assert ts._companion_stem("a b.FLAC") == "a b"
    assert ts._companion_stem("a b.md") is None
    assert ts._companion_stem("a b.wav") is None


# ---------------------------------------------------------------------------
# Diarization sidecar: speakers in the DB, segments in the file
# ---------------------------------------------------------------------------

import numpy as np  # noqa: E402

from wisper_transcribe.config import EMBEDDING_SPACE  # noqa: E402

_SEGS = [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
         {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"}]


def _full_diar(audio: Path) -> dict:
    return {
        "input_path": str(audio),
        "diarization_segments": _SEGS,
        "speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"},
        "speaker_map_source": {"SPEAKER_00": "manual", "SPEAKER_01": "auto"},
        "embedding_space": EMBEDDING_SPACE,
        "speaker_embeddings": {"SPEAKER_00": [0.6, 0.8], "SPEAKER_01": [1.0, 0.0]},
    }


def test_sidecar_round_trip(out):
    md = _md(out, "s01")
    audio = out / "s01_1.wav"
    audio.write_bytes(b"a")
    ts.write_sidecar(md, _full_diar(audio))

    assert json.loads((out / "s01_diar.json").read_text(encoding="utf-8")) == {"diarization_segments": _SEGS}
    diar = ts.read_sidecar(md)
    assert diar["speaker_map"] == {"SPEAKER_00": "Alice", "SPEAKER_01": "Unknown Speaker 1"}
    assert diar["speaker_map_source"] == {"SPEAKER_00": "manual", "SPEAKER_01": "auto"}
    assert diar["embedding_space"] == EMBEDDING_SPACE
    np.testing.assert_allclose(diar["speaker_embeddings"]["SPEAKER_00"], [0.6, 0.8], rtol=1e-6)
    assert os.path.realpath(diar["input_path"]) == os.path.realpath(audio)
    with db.connection() as conn:
        owner = file_registry.Owner("transcript", ts.locate_path(md, conn=conn).id)
        assert file_registry.file_for(owner, "audio", conn=conn).rel_path == "s01_1.wav"


def test_missing_provenance_is_derived_like_is_relabelable(out):
    md = _md(out, "s01")
    ts.write_sidecar(md, {"diarization_segments": _SEGS,
                          "speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "SPEAKER_01"}})
    assert ts.read_sidecar(md)["speaker_map_source"] == {"SPEAKER_00": "manual", "SPEAKER_01": "auto"}


def test_legacy_sidecar_fields_used_until_db_has_data(out):
    md = _md(out, "s01")
    (out / "s01_diar.json").write_text(json.dumps({
        "diarization_segments": _SEGS, "speaker_map": {"SPEAKER_00": "Bob"},
        "input_path": "/somewhere/else.mp3"}), encoding="utf-8")
    diar = ts.read_sidecar(md)
    assert diar["speaker_map"] == {"SPEAKER_00": "Bob"}
    ts.write_sidecar(md, diar)          # the next write moves them into the DB
    raw = json.loads((out / "s01_diar.json").read_text(encoding="utf-8"))
    assert set(raw) == {"diarization_segments"}
    assert ts.read_sidecar(md)["speaker_map"] == {"SPEAKER_00": "Bob"}


def test_rewrite_with_new_audio_deletes_the_old_copy(out):
    md = _md(out, "s01")
    old_audio, new_audio = out / "s01.wav", out / "s01_1.wav"
    old_audio.write_bytes(b"a")
    new_audio.write_bytes(b"b")
    ts.write_sidecar(md, _full_diar(old_audio))
    ts.write_sidecar(md, _full_diar(new_audio))
    assert not old_audio.exists() and new_audio.exists()


def test_delete_uses_stored_audio_path(out):
    md = _md(out, "s01")
    tid = ts.register(md, origin="job")
    audio = out / "s01_1.wav"
    audio.write_bytes(b"a")
    ts.write_sidecar(md, _full_diar(audio))
    ts.delete_transcript(tid)
    assert not audio.exists()
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcript_speakers").fetchone()[0] == 0


def test_audio_path_resolves_flac_recording_and_unlinked(out):
    from wisper_transcribe.recording_manager import link_transcript

    from ._seed import seed_recording

    md = _md(out, "s01")
    ts.register(md, origin="job")
    assert ts.audio_path(md) is None  # unlinked, no audio row

    flac = out / "s01.flac"
    flac.write_bytes(b"f")
    ts.set_audio(md, flac)
    assert ts.audio_path(md) == flac

    rec_md = _md(out, "rec")
    rec_tid = ts.register(rec_md, origin="job")
    rec = seed_recording()
    link_transcript(rec.id, rec_tid)  # no audio row and no _diar.json
    assert ts.audio_path(rec_md) == rec.combined_path


def test_read_sidecar_input_path_for_a_recording_without_speaker_rows(out):
    from wisper_transcribe.recording_manager import link_transcript

    from ._seed import seed_recording

    md = _md(out, "rec")
    rec_tid = ts.register(md, origin="job")
    rec = seed_recording()
    link_transcript(rec.id, rec_tid)
    (out / "rec_diar.json").write_text(json.dumps({"diarization_segments": []}), encoding="utf-8")

    diar = ts.read_sidecar(md)

    assert diar is not None and diar["input_path"] == str(rec.combined_path)
    assert diar["input_path"] == str(ts.audio_path(md))


def test_set_audio_replaces_the_row_and_deletes_the_previous_file(out):
    md = _md(out, "s01")
    old, new = out / "s01.mp4", out / "s01.flac"
    old.write_bytes(b"video")
    new.write_bytes(b"flac")
    ts.set_audio(md, old)
    ts.set_audio(md, new)
    assert ts.audio_path(md) == new
    assert not old.exists() and new.exists()


def test_set_audio_never_deletes_a_file_outside_the_registry(out):
    md = _md(out, "s01")
    bystander = out / "s01.mp3"
    bystander.write_bytes(b"user's own file")
    new = out / "s01.flac"
    new.write_bytes(b"flac")
    ts.set_audio(md, new)
    ts.set_audio(md, None)
    assert bystander.exists()
    assert not new.exists()  # the registered file goes when cleared


def test_recording_transcript_in_the_data_dir_has_no_audio_row_and_keeps_combined_wav(
        tmp_path, monkeypatch):
    """Even with the output root set to the data dir (so recordings/ lies
    inside it), a recording's transcript gets no audio row, and deleting the
    transcript leaves combined.wav."""
    from datetime import datetime
    from unittest.mock import patch

    from wisper_transcribe import file_registry
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.recording_manager import link_transcript
    from wisper_transcribe.web.jobs import JobQueue

    from ._seed import seed_recording

    data = Path(os.environ["WISPER_DATA_DIR"])
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(data))
    rec = seed_recording()
    combined = rec.combined_path
    queue = JobQueue()
    job = queue.submit(str(combined), original_stem=rec.id, recording_id=rec.id,
                       output_dir=data, overwrite=True)

    def _process(path, _result_store=None, job_id=None, **kwargs):
        md = data / (kwargs["output_stem"] + ".md")
        md.write_text("# t", encoding="utf-8")
        ts.register(md, origin="job")
        _result_store["diarization_segments"] = [
            DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")]
        return md

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process), \
            patch("wisper_transcribe.web.jobs._extract_speaker_excerpts"):
        queue._run_job(job)

    assert job.status == "completed"
    md = data / f"{rec.id}.md"
    owner = file_registry.Owner("transcript", ts.locate_path(md).id)
    assert file_registry.file_for(owner, "audio") is None
    link_transcript(rec.id, owner.id)
    assert ts.audio_path(md) == combined

    ts.delete_transcript(owner.id)

    assert combined.exists()
    assert not md.exists()


def test_overwrite_clears_stale_speakers_and_segments(out):
    md = _md(out, "s01")
    ts.write_sidecar(md, _full_diar(out / "none.wav"))
    ts.register(md, origin="job")      # e.g. `wisper transcribe --overwrite`
    assert ts.read_sidecar(md) is None
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcript_speakers").fetchone()[0] == 0


def test_speaker_schema_constraints(out):
    import sqlite3

    md = _md(out, "s01")
    ts.write_sidecar(md, _full_diar(out / "none.wav"))
    for sql in ("UPDATE transcript_speakers SET source = 'guess'",
                "UPDATE transcript_speakers SET embedding_space = NULL",
                "UPDATE transcript_speakers SET embedding = x'0102'"):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(sql)


# ---------------------------------------------------------------------------
# Narrow speaker writers
# ---------------------------------------------------------------------------

def _speaker_rows(stem: str) -> dict:
    with db.connection() as conn:
        return {r["label"]: (r["display_name"], r["source"], r["embedding"] is not None)
                for r in conn.execute(
                    "SELECT s.* FROM transcript_speakers s JOIN transcripts t ON t.id = s.transcript_id "
                    "WHERE t.stem = ?", (stem,))}


def test_set_speaker_names_keeps_embeddings_and_segments(out):
    md = _md(out, "s01")
    ts.write_sidecar(md, {"diarization_segments": [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}],
                          "speaker_map": {"SPEAKER_00": "Unknown Speaker 1"},
                          "speaker_map_source": {"SPEAKER_00": "auto"},
                          "embedding_space": "x", "speaker_embeddings": {"SPEAKER_00": [1.0, 0.0]}})
    sidecar = out / "s01_diar.json"
    before = sidecar.read_bytes(), sidecar.stat().st_mtime_ns
    ts.set_speaker_names(md, {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}, {"SPEAKER_00": "manual"})
    assert _speaker_rows("s01") == {"SPEAKER_00": ("Alice", "manual", True),
                                    "SPEAKER_01": ("Bob", "manual", False)}
    assert (sidecar.read_bytes(), sidecar.stat().st_mtime_ns) == before
    assert ts.read_sidecar(md)["speaker_embeddings"]["SPEAKER_00"] == [1.0, 0.0]


def test_set_speaker_embeddings_keeps_names(out):
    md = _md(out, "s01")
    ts.set_speaker_names(md, {"SPEAKER_00": "Alice"}, {"SPEAKER_00": "manual"})
    ts.set_speaker_embeddings(md, {"SPEAKER_00": [0.5, 0.5], "SPEAKER_03": [1.0, 0.0]}, "space-a")
    assert _speaker_rows("s01") == {"SPEAKER_00": ("Alice", "manual", True),
                                    "SPEAKER_03": ("SPEAKER_03", "auto", True)}


# ---------------------------------------------------------------------------
# NFD filenames on normalization-sensitive filesystems
# ---------------------------------------------------------------------------

def test_existing_form_falls_back_to_nfd(monkeypatch, tmp_path):
    import unicodedata
    nfc_path = str(tmp_path / unicodedata.normalize("NFC", "Café.md"))
    nfd_path = str(tmp_path / unicodedata.normalize("NFD", "Café.md"))
    monkeypatch.setattr(os.path, "exists", lambda p: p == nfd_path)  # ext4-like
    assert ts.existing_form(nfc_path) == nfd_path
    monkeypatch.setattr(os.path, "exists", lambda p: False)  # neither: write NFC
    assert ts.existing_form(nfc_path) == nfc_path
    assert ts.existing_form(str(tmp_path / "plain.md")) == str(tmp_path / "plain.md")


def test_nfd_file_is_found_indexed_and_deleted(out):
    """On Linux (ext4) this exercises the real fallback; on APFS/NTFS the
    filesystem already treats both spellings as one file."""
    import unicodedata
    from wisper_transcribe import search_index
    nfd = unicodedata.normalize("NFD", "Café night")
    (out / f"{nfd}.md").write_text("**A** *(00:01)*: croissants for everyone\n", encoding="utf-8")
    ts.reconcile(out)
    assert search_index.run_backfill() == 1
    assert search_index.progress() == (1, 1)
    assert [g.stem for g in search_index.search("croissants").groups] == [
        unicodedata.normalize("NFC", "Café night")]
    ts.delete_transcript(_tid(unicodedata.normalize("NFC", "Café night")))
    assert not any(p.suffix == ".md" for p in out.iterdir())


# ---------------------------------------------------------------------------
# Locations: locate, locate_path, dir_campaign, find_by_stem, Located
# ---------------------------------------------------------------------------

from wisper_transcribe import campaign_folders as cf  # noqa: E402

from . import _seed  # noqa: E402


def _claimed_campaign(display_name: str) -> tuple[int, str, str]:
    """A campaign whose folder exists and is claimed. Returns (id, slug, folder)."""
    cid = _seed.seed_campaign(display_name, claimed=True)
    with db.connection() as conn:
        slug, folder = conn.execute(
            "SELECT slug, folder FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
    return cid, slug, folder


def _insert_session(stem: str, campaign_id: int | None = None,
                    position: int | None = None) -> int:
    """A transcript row only (two campaigns may share a stem, so no seed helper)."""
    with db.transaction() as conn:
        return conn.execute(
            "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (stem, campaign_id, position, db.now_utc()),
        ).fetchone()[0]


def _place(root: Path, directory: Path, stem: str, tid: int) -> Path:
    """Write ``<stem>.md`` in ``directory`` and register it as ``tid``'s file."""
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / f"{stem}.md"
    md.write_text(f"# {stem}\n", encoding="utf-8")
    file_registry.add(md, kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=root)
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (tid,))
    return md


def test_locate_root_and_campaign_sessions(out):
    cid, slug, folder = _claimed_campaign("Game")
    root_tid = _seed.seed_transcript("Root Session", write_md=True)
    camp_tid = _seed.seed_transcript("Camp Session", campaign=slug)
    md = _place(out, out / folder, "Camp Session", camp_tid)

    root = ts.locate(root_tid)
    assert root.stem == "Root Session" and root.campaign_id is None
    assert root.expected_dir == out and root.dir == out
    assert root.md == out / "Root Session.md"
    assert root.missing is False and root.misplaced is False

    camp = ts.locate(camp_tid)
    assert camp.campaign_id == cid and camp.stem == "Camp Session"
    assert camp.expected_dir == out / folder and camp.dir == out / folder and camp.md == md
    assert camp.misplaced is False

    assert ts.locate(999999) is None


def test_misplaced_is_true_when_a_campaign_session_stays_in_the_root(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Stray", campaign=slug)
    _place(out, out, "Stray", tid)  # registered in the root though it belongs to Game

    loc = ts.locate(tid)
    assert loc.expected_dir == out / folder and loc.dir == out
    assert loc.misplaced is True


def test_misplaced_is_false_for_a_case_only_difference_on_a_case_insensitive_filesystem():
    same = ts.Located(id=1, stem="s", campaign_id=None,
                      expected_dir=Path("/tmp/Out"), md=Path("/tmp/out/s.md"),
                      missing=False, companions={}, target_blocked=False, fold=True)
    assert same.misplaced is False
    other = ts.Located(id=1, stem="s", campaign_id=None,
                       expected_dir=Path("/tmp/Out"), md=Path("/tmp/out/s.md"),
                       missing=False, companions={}, target_blocked=False, fold=False)
    assert other.misplaced is True


def test_misplaced_is_false_when_the_target_folder_is_blocked(out):
    _seed.seed_campaign("Game")  # unclaimed: the folder is not wisper's
    (out / "Game").mkdir()
    (out / "Game" / "notes.md").write_text("mine\n", encoding="utf-8")
    tid = _seed.seed_transcript("S", campaign="game")
    _place(out, out, "S", tid)

    loc = ts.locate(tid)
    assert loc.target_blocked is True and loc.misplaced is False


def test_expected_dir(out):
    cid, slug, folder = _claimed_campaign("Game")
    assert ts.expected_dir(None) == out
    assert ts.expected_dir(cid) == out / folder


def test_locate_path_by_registered_path_and_by_folder_stem(out):
    cid, slug, folder = _claimed_campaign("Game")
    root_tid = _seed.seed_transcript("Root", write_md=True)
    assert ts.locate_path(out / "Root.md").id == root_tid

    camp_tid = _seed.seed_transcript("Camp", campaign=slug)  # no files row yet
    loc = ts.locate_path(out / folder / "Camp.md")
    assert loc is not None and loc.id == camp_tid and loc.dir == out / folder


def test_locate_path_matches_nfd_and_case(out, monkeypatch):
    import unicodedata
    tid = _seed.seed_transcript("Café night", write_md=True)
    nfd = unicodedata.normalize("NFD", "Café night")
    assert ts.locate_path(out / f"{nfd}.md").id == tid

    monkeypatch.setattr(file_registry, "_fold", lambda d: True)
    assert ts.locate_path(out / "café night.md").id == tid


def test_locate_path_of_a_folder_file_that_is_a_newcomer(out):
    cid, slug, folder = _claimed_campaign("B")
    tid = _seed.seed_transcript("S", campaign=slug)
    _place(out, out, "S", tid)  # S's registered .md is in the root

    assert ts.locate_path(out / folder / "S.md") is None


def test_dir_campaign(out):
    assert ts.dir_campaign(out) == (True, None)
    cid, slug, folder = _claimed_campaign("Game")
    assert ts.dir_campaign(out / folder) == (True, cid)

    other = _seed.seed_campaign("Other")  # unclaimed
    with db.connection() as conn:
        other_folder = conn.execute(
            "SELECT folder FROM campaigns WHERE id = ?", (other,)).fetchone()[0]
    (out / other_folder).mkdir(exist_ok=True)
    assert ts.dir_campaign(out / other_folder) == (False, None)

    assert ts.dir_campaign(out / "unrelated") == (False, None)
    assert ts.dir_campaign(out.parent) == (False, None)


def test_dir_campaign_pending_folder(out):
    cid, slug, folder = _claimed_campaign("Game")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    assert ts.dir_campaign(out / folder) == (True, cid)     # old folder still there
    assert ts.dir_campaign(out / "Renamed") == (False, None)
    (out / folder).rmdir()
    assert ts.dir_campaign(out / "Renamed") == (True, cid)  # old gone: pending matches


def test_find_by_stem_two_campaigns_share_a_stem(out):
    ca, _, folda = _claimed_campaign("A")
    cb, _, foldb = _claimed_campaign("B")
    ta = _insert_session("S", ca, 0)
    tb = _insert_session("S", cb, 0)
    _place(out, out / folda, "S", ta)
    _place(out, out / foldb, "S", tb)
    tm = _insert_session("S")  # in the root, and flagged missing
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = ? WHERE id = ?", (db.now_utc(), tm))

    assert {loc.id for loc in ts.find_by_stem("S")} == {ta, tb, tm}
    assert {loc.id for loc in ts.find_by_stem("S", present_only=True)} == {ta, tb}
    assert [loc.id for loc in ts.find_by_stem("S", campaign_id=ca)] == [ta]
    assert [loc.id for loc in ts.find_by_stem("S", campaign_id=None)] == [tm]
    assert ts.find_by_stem("no such stem") == []


# ---------------------------------------------------------------------------
# check_move: pure clash / busy / validation for a move or rename
# ---------------------------------------------------------------------------

def test_validate_new_stem_accepts_and_refuses():
    assert ts.validate_new_stem("  Session 3  ") == "Session 3"
    assert ts.validate_new_stem("Café") == "Café"
    for name in ("", "   ", ".hidden", ".wisper-tmp-1", "COM1", "nul", "x" * 101,
                 "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b",
                 "trailing.", "a\x00b", "a\x1fb", "a\x7fb", "Notes.md",
                 "Notes.SUMMARY", "Notes.summary"):
        assert ts.validate_new_stem(name) is None, name

    # Strip removes a surrounding space, so only a dot can trail.
    assert ts.validate_new_stem("trailing ") == "trailing"


def _campaign_slug(cid: int) -> str:
    with db.connection() as conn:
        return conn.execute("SELECT slug FROM campaigns WHERE id = ?", (cid,)).fetchone()[0]


def test_check_move_to_a_campaign_is_ok_and_creates_nothing(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("S", write_md=True)

    check = ts.check_move(tid, slug)
    assert check.status == "ok" and check.dst_dir == out / folder and check.stem == "S"
    assert check.files_move is True and check.existing_id is None
    # Pure: no mkdir, and no write.
    before = {p.name for p in out.iterdir()}
    ts.check_move(tid, slug)
    assert {p.name for p in out.iterdir()} == before


def test_check_move_unchanged_when_campaign_and_stem_match(out):
    tid = _seed.seed_transcript("S", write_md=True)
    check = ts.check_move(tid, None)
    assert check.status == "unchanged" and check.files_move is False


def test_check_move_invalid_name_returns_invalid_without_disk_access(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("S", write_md=True)
    for name in ("../x", "a/b", ".hidden", "COM1", "x" * 101):
        check = ts.check_move(tid, slug, new_stem=name)
        assert check.status == "invalid", name

    # An invalid name never reaches the disk: an unclaimed folder stays absent.
    other = _seed.seed_campaign("Unclaimed")  # no folder on disk
    with db.connection() as conn:
        other_slug = conn.execute(
            "SELECT slug FROM campaigns WHERE id = ?", (other,)).fetchone()[0]
    check = ts.check_move(tid, other_slug, new_stem="../x")
    assert check.status == "invalid"


def test_check_move_clash_names_the_existing_transcript(out):
    cid, slug, folder = _claimed_campaign("Game")
    moved = _seed.seed_transcript("S", write_md=True)
    other = _insert_session("S", campaign_id=cid, position=0)
    md = out / folder / "S.md"
    md.write_text("x", encoding="utf-8")
    file_registry.add(md, kind="transcript",
                      owner=file_registry.Owner("transcript", other), output_dir=out)
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (other,))

    check = ts.check_move(moved, slug)
    assert check.status == "clash" and check.existing_id == other
    assert check.overwrite_allowed is True and check.clash_modified


def test_check_move_clash_ignores_case_on_a_case_insensitive_filesystem(out, monkeypatch):
    """A rename differing only in case clashes with another session there."""
    cid, slug, folder = _claimed_campaign("Game")
    mover = _seed.seed_transcript("Session", write_md=True)
    other = _insert_session("Other", campaign_id=cid, position=0)
    md = out / folder / "Other.md"
    md.write_text("x", encoding="utf-8")
    file_registry.add(md, kind="transcript",
                      owner=file_registry.Owner("transcript", other), output_dir=out)
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (other,))

    monkeypatch.setattr(file_registry, "_fold", lambda d: True)
    check = ts.check_move(mover, slug, new_stem="other")
    assert check.status == "clash" and check.existing_id == other


def test_check_move_reserved_when_the_name_is_the_journal(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("S", write_md=True)
    check = ts.check_move(tid, slug, new_stem=f"{folder} Journal")
    assert check.status == "reserved"


def test_check_move_busy_when_a_job_targets_the_campaign(out):
    from wisper_transcribe.web.jobs import JobQueue

    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("S", write_md=True)

    q = JobQueue()
    job = q.submit(str(out / "s.mp3"), original_stem="S", campaign=slug,
                   output_dir=str(out / folder))
    assert ts.check_move(tid, slug).status == "busy"

    job.status = "failed"
    from datetime import datetime
    job.finished_at = datetime.now()
    job.error = "x"
    from wisper_transcribe import job_history
    job_history.record(job)
    assert ts.check_move(tid, slug).status == "ok"


def test_check_move_missing_session_moves_as_a_database_change_only(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _insert_session("Gone", campaign_id=cid, position=0)
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = ? WHERE id = ?", (db.now_utc(), tid))
    check = ts.check_move(tid, None)
    assert check.status == "ok" and check.files_move is False


def test_check_move_misplaced_session_is_not_its_own_clash(out):
    """Removing a misplaced session (its .md in the root) from its campaign is
    a move to the root, not a clash with the .md that is already there, and
    moves nothing on disk."""
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("S", campaign=slug, write_md=True)  # .md in the root
    check = ts.check_move(tid, None)
    assert check.status == "ok" and check.files_move is False


def test_check_move_case_only_rename_on_a_case_insensitive_filesystem(out, monkeypatch):
    monkeypatch.setattr(file_registry, "_fold", lambda d: True)
    tid = _seed.seed_transcript("Session", write_md=True)
    check = ts.check_move(tid, None, new_stem="SESSION")
    assert check.status == "ok" and check.files_move is True


def test_list_transcripts_present_only_newest_first(out):
    """list_transcripts returns present rows ordered by the transcript file's
    mtime (newest first), skipping rows flagged missing."""
    ca, slug_a, folda = _claimed_campaign("A")
    older = _seed.seed_transcript("Older", write_md=True)
    newer = _seed.seed_transcript("Newer", write_md=True)
    os.utime(out / "Older.md", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.utime(out / "Newer.md", ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))
    gone = _seed.seed_transcript("Gone")  # no file: flagged missing
    ts.reconcile(out, sync="never")

    all_ids = [loc.id for loc in ts.list_transcripts()]
    assert all_ids.index(newer) < all_ids.index(older)
    assert gone not in all_ids

    camp_tid = _seed.seed_transcript("In A", campaign=slug_a)
    _place(out, out / folda, "In A", camp_tid)
    assert [loc.id for loc in ts.list_transcripts(campaign_id=ca)] == [camp_tid]
    assert all(loc.id != camp_tid for loc in ts.list_transcripts(campaign_id=None))


def test_companion_prefers_the_registered_path(out):
    tid = _seed.seed_transcript("s01", write_md=True)
    audio = out / "s01_1.wav"
    audio.write_bytes(b"a")
    ts.set_audio(out / "s01.md", audio)

    loc = ts.locate(tid)
    assert loc.companion(".flac") == audio                    # registered row, any suffix
    assert loc.companion(".summary.md") == out / "s01.summary.md"  # derived name


def test_registry_paths_for_a_session_seeded_in_a_folder(out):
    """A session in a campaign folder registers every file under ``Folder/``."""
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("S", campaign=slug)
    md = _place(out, out / folder, "S", tid)
    ts.register(md, origin="job")
    audio = out / folder / "S.flac"
    audio.write_bytes(b"a")
    ts.set_audio(md, audio)
    ts.write_sidecar(md, {"diarization_segments": []})

    with db.connection() as conn:
        paths = [r[0] for r in conn.execute(
            "SELECT rel_path FROM files WHERE transcript_id = ?", (tid,))]
    assert paths and all(p.startswith(f"{folder}/") for p in paths), paths


def test_set_speaker_names_on_an_existing_session_keeps_other_speakers(out):
    """No second transaction: the rename doesn't clear the session's other speakers."""
    md = _md(out, "s01")
    ts.write_sidecar(md, {"diarization_segments": [],
                          "speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"},
                          "speaker_map_source": {"SPEAKER_00": "auto", "SPEAKER_01": "auto"}})
    ts.set_speaker_names(md, {"SPEAKER_00": "Zanthor"}, {"SPEAKER_00": "manual"})
    assert _speaker_rows("s01") == {"SPEAKER_00": ("Zanthor", "manual", False),
                                    "SPEAKER_01": ("Bob", "auto", False)}


def test_register_same_stem_in_root_and_folder_leaves_the_root_sidecar(out):
    """Registering ``Folder/S.md`` doesn't touch root session ``S``'s sidecar."""
    cid, slug, folder = _claimed_campaign("Game")
    root_tid = _seed.seed_transcript("S", write_md=True)
    root_sidecar = out / "S_diar.json"
    root_sidecar.write_text("{}", encoding="utf-8")
    file_registry.add(root_sidecar, kind="sidecar", owner=file_registry.Owner("transcript", root_tid),
                      output_dir=out)

    camp_tid = _insert_session("S", cid, 0)
    md = _place(out, out / folder, "S", camp_tid)
    ts.register(md, origin="job")

    loc = ts.locate(root_tid)
    assert loc.companions[("sidecar", "")] == root_sidecar
    assert ts.locate(camp_tid).id == camp_tid


def test_register_job_in_a_folder_does_not_delete_a_root_sessions_sidecar(out):
    """A job overwrite of ``Folder/S.md`` deletes only its own folder sidecar.

    The stale-sidecar fallback must resolve beside the ``.md`` being registered,
    never the output root, so a same-named root session keeps its ``S_diar.json``.
    """
    cid, slug, folder = _claimed_campaign("Game")
    root_tid = _seed.seed_transcript("S", write_md=True)
    root_sidecar = out / "S_diar.json"
    root_sidecar.write_text("{}", encoding="utf-8")
    file_registry.add(root_sidecar, kind="sidecar", owner=file_registry.Owner("transcript", root_tid),
                      output_dir=out)

    camp_tid = _insert_session("S", cid, 0)
    md = _place(out, out / folder, "S", camp_tid)
    folder_sidecar = out / folder / "S_diar.json"    # unregistered, beside the .md
    folder_sidecar.write_text("{}", encoding="utf-8")
    ts.register(md, origin="job")                    # e.g. a web job overwriting it

    assert root_sidecar.exists()
    assert not folder_sidecar.exists()
    loc = ts.locate(root_tid)
    assert loc.companions[("sidecar", "")] == root_sidecar


# ---------------------------------------------------------------------------
# Campaign folders: reconcile scans each claimed folder
# ---------------------------------------------------------------------------

def _campaign(cid: int) -> tuple[str, str]:
    with db.connection() as conn:
        return conn.execute("SELECT slug, folder FROM campaigns WHERE id = ?",
                            (cid,)).fetchone()


def _sessions() -> dict[str, tuple[str, int | None]]:
    """stem → (campaign slug or '', campaign position)."""
    with db.connection() as conn:
        return {r[0]: (r[1] or "", r[2]) for r in conn.execute(
            "SELECT t.stem, c.slug, t.position FROM transcripts t "
            "LEFT JOIN campaigns c ON c.id = t.campaign_id ORDER BY t.id")}


def test_a_md_copied_into_a_campaign_folder_becomes_that_campaigns_transcript(out):
    cid, slug, folder = _claimed_campaign("Game")
    md = out / folder / "Session 1.md"
    md.write_text("# s\n", encoding="utf-8")

    counts = ts.reconcile(out, sync="never")

    assert counts["added"] == 1
    assert _sessions() == {"Session 1": (slug, 0)}


def test_drag_between_campaigns_moves_the_transcript_and_its_companions(out):
    _ca, slug_a, fold_a = _claimed_campaign("A")
    cb, slug_b, fold_b = _claimed_campaign("B")
    tid = _seed.seed_transcript("Session 1", campaign=slug_a)
    md = _place(out, out / fold_a, "Session 1", tid)
    for suffix in (".summary.md", "_diar.json", "_excerpt_SPEAKER_00.mp3"):
        (out / fold_a / f"Session 1{suffix}").write_text("x", encoding="utf-8")
    audio = out / fold_a / "Session 1.flac"
    audio.write_text("x", encoding="utf-8")
    file_registry.add(audio, kind="audio", owner=file_registry.Owner("transcript", tid),
                      output_dir=out)
    ts.write_sidecar(md, {"diarization_segments": _SEGS,
                          "speaker_map": {"SPEAKER_00": "Alice"},
                          "input_path": str(audio)})
    # It is folded into A's journal; the move must un-journal it.
    with db.transaction() as conn:
        conn.execute("INSERT INTO journal_entries (transcript_id, campaign_id, folded_at) "
                     "VALUES (?, ?, ?)", (tid, _ca, db.now_utc()))

    # A drag in Obsidian: os.replace keeps the mtime.
    for name in list((out / fold_a).iterdir()):
        os.replace(name, out / fold_b / name.name)
    ts.reconcile(out, sweep=True)

    assert _sessions()["Session 1"][0] == slug_b
    for suffix in (".md", ".summary.md", "_diar.json", ".flac", "_excerpt_SPEAKER_00.mp3"):
        assert (out / fold_b / f"Session 1{suffix}").exists(), suffix
    assert not list((out / fold_a).iterdir())
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM journal_entries").fetchone()[0] == 0
    from wisper_transcribe.journal import journal_stale_since
    assert journal_stale_since(slug_a) is not None


def test_drag_into_the_root_unassigns_the_transcript(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Session 1", campaign=slug)
    md = _place(out, out / folder, "Session 1", tid)

    os.replace(md, out / "Session 1.md")
    ts.reconcile(out, sweep=True)

    loc = ts.locate(tid)
    assert loc.campaign_id is None and loc.dir == out


def test_two_campaigns_share_a_stem_and_keeps_each_folders_companions(out):
    ca, slug_a, fold_a = _claimed_campaign("A")
    cb, slug_b, fold_b = _claimed_campaign("B")
    for cid, folder in ((ca, fold_a), (cb, fold_b)):
        md = out / folder / "Session 1.md"
        md.write_text("# s\n", encoding="utf-8")
        (out / folder / "Session 1.summary.md").write_text("x", encoding="utf-8")
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                "VALUES ('Session 1', ?, 0, ?)", (cid, db.now_utc()))

    ts.reconcile(out, sync="always")

    rows = ts.find_by_stem("Session 1")
    assert len(rows) == 2 and {r.campaign_id for r in rows} == {ca, cb}
    for r in rows:
        assert r.companions[("summary", "")].parent == r.dir


def test_a_campaign_folders_journal_is_never_a_transcript(out):
    cid, slug, folder = _claimed_campaign("Game")
    (out / folder / "Game Journal.md").write_text("---\ntype: campaign-journal\n---\n\nx\n",
                                                  encoding="utf-8")
    counts = ts.reconcile(out, sync="never")
    assert counts["added"] == 0 and _sessions() == {}


def test_unrelated_and_unclaimed_folders_are_ignored(out):
    _seed.seed_campaign("Unclaimed")  # folder exists but is not claimed
    (out / "Unclaimed").mkdir()
    (out / "Unclaimed" / "notes.md").write_text("# mine\n", encoding="utf-8")
    (out / "random").mkdir()
    (out / "random" / "other.md").write_text("# other\n", encoding="utf-8")

    counts = ts.reconcile(out, sync="never")

    assert counts["added"] == 0 and _sessions() == {}


def test_a_pending_renames_foreign_target_is_not_scanned(out):
    cid, slug, folder = _claimed_campaign("Game")
    (out / "Elsewhere").mkdir()
    (out / "Elsewhere" / "stray.md").write_text("# stray\n", encoding="utf-8")
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Elsewhere' WHERE id = ?", (cid,))

    counts = ts.reconcile(out, sync="never")
    assert counts["added"] == 0 and _sessions() == {}


def test_drag_into_a_misplaced_rows_campaign_is_unclaimed(out):
    ca, slug_a, fold_a = _claimed_campaign("A")
    cb, slug_b, fold_b = _claimed_campaign("B")
    # B's Session 1 is misplaced: registered in the root.
    tid_b = _seed.seed_transcript("Session 1", campaign=slug_b, write_md=True)
    # A's Session 1 is in A/, and gets dragged into B/.
    md_a = out / fold_a / "Session 1.md"
    md_a.write_text("# a\n", encoding="utf-8")
    with db.transaction() as conn:
        tid_a = conn.execute(
            "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
            "VALUES ('Session 1', ?, 0, ?) RETURNING id", (ca, db.now_utc())).fetchone()[0]
    file_registry.add(md_a, kind="transcript", owner=file_registry.Owner("transcript", tid_a),
                      output_dir=out)

    os.replace(md_a, out / fold_b / "Session 1.md")
    ts.reconcile(out, sync="never")

    # The file is unclaimed: B's row still points at the root file, A's row keeps A/.
    assert ts.locate(tid_b).md == out / "Session 1.md"
    assert ts.locate(tid_a).md == out / fold_a / "Session 1.md"  # row unchanged (file gone)
    assert (out / fold_b / "Session 1.md").is_file()


def test_a_rows_registered_md_basename_that_differs_sets_its_stem(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Old name", campaign=slug)
    _place(out, out / folder, "New name", tid)  # the .md disagrees with the stem

    counts = ts.reconcile(out, sync="never")

    assert counts["renamed"] == 1
    with db.connection() as conn:
        assert conn.execute("SELECT stem FROM transcripts WHERE id = ?",
                            (tid,)).fetchone()[0] == "New name"


def test_drag_a_misplaced_session_home_keeps_id_and_position(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Session 1", campaign=slug)
    # Misplaced: registered in the root.
    _place(out, out, "Session 1", tid)
    _seed.seed_transcript("Session 0", campaign=slug)  # another session in the campaign
    with db.connection() as conn:
        before = conn.execute("SELECT position FROM transcripts WHERE id = ?",
                              (tid,)).fetchone()[0]

    os.replace(out / "Session 1.md", out / folder / "Session 1.md")
    ts.reconcile(out, sync="never")

    loc = ts.locate(tid)
    assert loc.campaign_id == cid and loc.dir == out / folder and not loc.misplaced
    with db.connection() as conn:
        assert conn.execute("SELECT position FROM transcripts WHERE id = ?",
                            (tid,)).fetchone()[0] == before


def test_drag_a_misplaced_session_home_after_editing_it_keeps_its_row(out):
    # Its size and modified time disagree with the registry, so only the
    # (campaign, name) row can claim it: the row's own .md is gone.
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Session 1", campaign=slug)
    _place(out, out, "Session 1", tid)

    os.replace(out / "Session 1.md", out / folder / "Session 1.md")
    (out / folder / "Session 1.md").write_text("edited in Obsidian", encoding="utf-8")
    counts = ts.reconcile(out, sync="never")

    loc = ts.locate(tid)
    assert loc.md == out / folder / "Session 1.md" and not loc.missing and not loc.misplaced
    assert counts["added"] == 0
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1


def test_rename_in_place_keeps_its_position_in_the_order(out):
    cid, slug, folder = _claimed_campaign("Game")
    _seed.seed_transcript("A", campaign=slug)
    tid_b = _seed.seed_transcript("B", campaign=slug)
    _seed.seed_transcript("C", campaign=slug)
    _place(out, out / folder, "B", tid_b)

    os.replace(out / folder / "B.md", out / folder / "Prologue.md")
    ts.reconcile(out, sync="never")

    with db.connection() as conn:
        rows = [r[0] for r in conn.execute(
            "SELECT stem FROM transcripts WHERE campaign_id = ? ORDER BY position", (cid,))]
    assert rows == ["A", "Prologue", "C"]


def test_drag_with_companions_repoints_the_audio_row(out):
    ca, slug_a, fold_a = _claimed_campaign("A")
    cb, slug_b, fold_b = _claimed_campaign("B")
    tid = _seed.seed_transcript("Session 1", campaign=slug_a)
    _place(out, out / fold_a, "Session 1", tid)
    audio = out / fold_a / "Session 1.flac"
    audio.write_text("x", encoding="utf-8")
    file_registry.add(audio, kind="audio", owner=file_registry.Owner("transcript", tid),
                      output_dir=out)

    # Both files dragged together: the audio row is repointed, not forgotten.
    os.replace(out / fold_a / "Session 1.md", out / fold_b / "Session 1.md")
    os.replace(audio, out / fold_b / "Session 1.flac")
    ts.reconcile(out, sweep=True)

    row = file_registry.file_for(file_registry.Owner("transcript", tid), "audio")
    assert row is not None and row.path == out / fold_b / "Session 1.flac"


def test_mid_rename_campaign_skips_scanning_and_missing(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Session 1", campaign=slug)
    _place(out, out / folder, "Session 1", tid)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    counts = ts.reconcile(out, sync="never")

    assert counts == {"added": 0, "missing": 0, "restored": 0, "renamed": 0, "swept": 0}
    assert ts.locate(tid).missing is False


def test_no_companion_moves_when_the_transaction_rolls_back(out, monkeypatch):
    ca, slug_a, fold_a = _claimed_campaign("A")
    cb, slug_b, fold_b = _claimed_campaign("B")
    tid = _seed.seed_transcript("Session 1", campaign=slug_a)
    _place(out, out / fold_a, "Session 1", tid)
    summary = out / fold_a / "Session 1.summary.md"
    summary.write_text("x", encoding="utf-8")
    # The .md is dragged to B/ alone; the summary stays behind.
    os.replace(out / fold_a / "Session 1.md", out / fold_b / "Session 1.md")

    def boom(*a, **k):
        raise RuntimeError("reconcile transaction failed")

    monkeypatch.setattr(file_registry, "repoint", boom)
    with pytest.raises(RuntimeError):
        ts.reconcile(out, sync="never")

    # The move rolled back: nothing moved and the row still names A/.
    assert summary.is_file()
    assert ts.locate(tid).campaign_id == ca


def test_registered_campaign_files_are_not_sessions(out):
    cid, slug, folder = _claimed_campaign("Game")
    owner = file_registry.Owner("campaign", cid)
    old = out / folder / "Old Journal.md"
    old.write_text("x", encoding="utf-8")
    file_registry.add(old, kind="journal", owner=owner, output_dir=out)

    counts = ts.reconcile(out, sync="never")

    assert counts["added"] == 0 and _sessions() == {}


def test_a_pending_campaigns_misplaced_root_file_isnt_duplicated(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Session 1", campaign=slug)
    _place(out, out, "Session 1", tid)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Renamed' WHERE id = ?", (cid,))

    counts = ts.reconcile(out, sync="never")

    assert counts["added"] == 0
    assert len(ts.find_by_stem("Session 1")) == 1


# ---------------------------------------------------------------------------
# The location lock
# ---------------------------------------------------------------------------

def test_reconcile_holds_the_location_lock(out):
    """reconcile takes ``_LOCATION_LOCK`` around its scan and write.

    A second thread that wants the lock blocks until the reconcile finishes.
    """
    import threading
    started = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def holder():
        with ts._LOCATION_LOCK:
            started.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(timeout=5)

    def contender():
        with ts._LOCATION_LOCK:
            acquired.set()

    c = threading.Thread(target=contender)
    c.start()
    assert not acquired.wait(timeout=0.2)   # held by the first thread
    release.set()
    t.join(timeout=5)
    assert acquired.wait(timeout=5)        # released, so the contender proceeds
    c.join(timeout=5)


def test_page_load_reconcile_skips_while_the_lock_is_held(out):
    _seed.seed_campaign("Game", claimed=True)
    (out / "Game" / "Session 1.md").write_text("# s\n", encoding="utf-8")
    import threading
    held = threading.Event()
    release = threading.Event()

    def holder():
        with ts._LOCATION_LOCK:
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=5)
    try:
        counts = ts.reconcile(out, sync="never", blocking=False)
    finally:
        release.set()
        t.join(timeout=5)
    assert counts == {"added": 0, "missing": 0, "restored": 0, "renamed": 0, "swept": 0}
    assert _sessions() == {}


def test_a_campaign_renamed_between_the_scan_and_the_write_is_skipped(out, monkeypatch):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Session 1", campaign=slug)
    _place(out, out / folder, "Session 1", tid)

    from wisper_transcribe import db as _db
    real = ts._transcript_files

    def scan_then_rename(*a, **k):
        found = real(*a, **k)
        with _db.transaction() as c:
            c.execute("UPDATE campaigns SET folder = 'Renamed' WHERE id = ?", (cid,))
        return found

    monkeypatch.setattr(ts, "_transcript_files", scan_then_rename)
    counts = ts.reconcile(out, sync="never")
    assert counts == {"added": 0, "missing": 0, "restored": 0, "renamed": 0, "swept": 0}
    assert ts.locate(tid).missing is False


def test_misplaced_listing_for_a_campaign_transcript_whose_files_are_in_the_root(out):
    cid, slug, folder = _claimed_campaign("Game")
    tid = _seed.seed_transcript("Stray", campaign=slug, write_md=True)  # .md in the root
    found = ts.needs_attention(out)
    assert [loc.stem for loc in found.misplaced] == ["Stray"]


def test_folder_taken_and_missing_folder_and_pending_are_listed(out):
    taken = _seed.seed_campaign("Taken")           # unclaimed, folder with a user note
    (out / "Taken").mkdir()
    (out / "Taken" / "notes.md").write_text("# mine\n", encoding="utf-8")
    gone = _seed.seed_campaign("Gone", claimed=True)
    (out / "Gone").rmdir()
    pend = _seed.seed_campaign("Pend", claimed=True)
    with db.transaction() as conn:
        conn.execute("UPDATE campaigns SET folder_pending = 'Pend (2)' WHERE id = ?", (pend,))

    found = ts.needs_attention(out)

    assert {cid for cid, _n, _f in found.folder_taken} == {taken}
    assert {cid for cid, _n, _f in found.missing_folders} == {gone}
    assert {p.folder for p in found.pending_folders} == {"Pend"}
    assert {p.pending for p in found.pending_folders} == {"Pend (2)"}


def test_delete_unowned_file_accepts_one_folder_level(out):
    cid, slug, folder = _claimed_campaign("Game")
    orphan = out / folder / "ghost.summary.md"
    orphan.write_text("x", encoding="utf-8")

    assert ts.delete_unowned_file(orphan, out) is True
    assert not orphan.exists()


def test_delete_unowned_file_refuses_a_folder_that_isnt_a_campaign(out):
    _seed.seed_campaign("Unclaimed")               # exists but not claimed
    (out / "Unclaimed").mkdir()
    orphan = out / "Unclaimed" / "ghost.summary.md"
    orphan.write_text("x", encoding="utf-8")
    nested = out / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "ghost.summary.md").write_text("x", encoding="utf-8")

    assert ts.delete_unowned_file(orphan, out) is False
    assert ts.delete_unowned_file(nested / "ghost.summary.md", out) is False
    assert orphan.exists()


# ---------------------------------------------------------------------------
# move_transcript: rows first, then files
# ---------------------------------------------------------------------------

def _files_root_rel(tid: int) -> list[str]:
    with db.connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT rel_path FROM files WHERE transcript_id = ? ORDER BY id", (tid,))]


def test_move_carries_every_registered_file(out, tmp_path):
    from ._moves import add_companion, claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S")
    add_companion(tid, md, ".summary.md", "sum")
    add_companion(tid, md, "_diar.json", "{}")
    add_companion(tid, md, ".flac", "x")

    outcome = ts.move_transcript(tid, slug)
    assert outcome.status == "moved" and outcome.new_stem == "S"
    assert not md.exists()
    assert sorted(p.name for p in (out / folder).iterdir()) == [
        "S.flac", "S.md", "S.summary.md", "S_diar.json"]
    assert all(rel.startswith(f"{folder}/") for rel in _files_root_rel(tid))
    assert ts.locate(tid).misplaced is False


def test_move_to_the_root(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S", campaign=slug, directory=out / folder)

    outcome = ts.move_transcript(tid, None)
    assert outcome.status == "moved"
    assert (out / "S.md").exists() and not md.exists()
    assert ts.locate(tid).campaign_id is None


def test_move_ask_on_a_clash_changes_nothing(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    mover, md = placed_session("S")

    before = _files_root_rel(mover)
    outcome = ts.move_transcript(mover, slug)
    assert outcome.status == "clash" and outcome.overwrite_allowed is True
    assert md.exists() and other_md.exists()
    assert _files_root_rel(mover) == before
    assert ts.locate(mover).campaign_id is None


def test_move_keep_both_picks_the_next_free_name(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    mover, md = placed_session("S")

    outcome = ts.move_transcript(mover, slug, clash="keep_both")
    assert outcome.status == "moved" and outcome.new_stem == "S (2)"
    assert (out / folder / "S (2).md").exists() and other_md.exists()


def test_move_overwrite_deletes_the_target(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    mover, md = placed_session("S")

    outcome = ts.move_transcript(mover, slug, clash="overwrite")
    assert outcome.status == "moved"
    assert other_md.exists()  # the moved file took the overwritten session's place
    assert ts.locate(other) is None  # the overwritten session is gone
    assert not md.exists()
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1


def test_move_overwrite_refuses_an_unregistered_md(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    (out / folder / "S.md").write_text("mine", encoding="utf-8")  # no row
    mover, md = placed_session("S")

    outcome = ts.move_transcript(mover, slug, clash="overwrite")
    assert outcome.status == "clash" and outcome.overwrite_allowed is False
    assert (out / folder / "S.md").read_text(encoding="utf-8") == "mine"
    assert md.exists()


def test_move_locked_md_returns_locked_and_leaves_the_row_home(out, monkeypatch):
    from ._moves import add_companion, claimed_campaign, lock_os_replace, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S")
    add_companion(tid, md, ".summary.md")
    lock_os_replace(monkeypatch, {"S.md"})

    outcome = ts.move_transcript(tid, slug)
    assert outcome.status == "locked" and md in outcome.kept
    assert md.exists() and (out / "S.summary.md").exists()
    assert not (out / folder).exists() or not list((out / folder).iterdir())
    loc = ts.locate(tid)
    assert loc.campaign_id is None and loc.dir == out and loc.misplaced is False


def test_move_locked_flac_is_partial_and_move_files_home_finishes(out, monkeypatch):
    from ._moves import add_companion, claimed_campaign, lock_os_replace, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S")
    flac = add_companion(tid, md, ".flac")
    state = lock_os_replace(monkeypatch, {"S.flac"})

    outcome = ts.move_transcript(tid, slug)
    assert outcome.status == "partial" and flac in outcome.kept
    loc = ts.locate(tid)
    assert loc.campaign_id == cid and loc.md == out / folder / "S.md"
    assert loc.misplaced is True  # the .flac stayed in the root
    assert flac.exists()

    state["lock"] = False  # "close the file" without undoing any fixture
    done = ts.move_files_home(tid)
    assert done.status == "moved"
    assert (out / folder / "S.flac").exists() and not flac.exists()
    assert ts.locate(tid).misplaced is False


def test_move_locked_md_with_keep_both_reverts_the_whole_assignment(out, monkeypatch):
    from ._moves import claimed_campaign, lock_os_replace, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    tid, md = placed_session("S")
    lock_os_replace(monkeypatch, {"S.md"})

    outcome = ts.move_transcript(tid, slug, clash="keep_both")
    assert outcome.status == "locked" and md in outcome.kept
    assert other_md.exists()
    loc = ts.locate(tid)
    assert loc.campaign_id is None and loc.stem == "S"   # its old name, back home
    assert loc.dir == out and loc.misplaced is False


def test_move_keep_both_revert_blocked_by_a_new_source_name_is_partial(out, monkeypatch):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    source_cid, source_slug, source_folder = claimed_campaign("Source")
    tid, md = placed_session("S", campaign=source_slug, directory=out)

    real_replace = os.replace

    def guarded(src, dst, *a, **k):
        if Path(dst).name == "S (2).md" and Path(dst).parent.name == folder:
            # The source campaign gains its own "S" between step 5 and the revert.
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                    "VALUES ('S', ?, 0, ?)", (source_cid, db.now_utc()))
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", guarded)
    monkeypatch.setattr(os, "rename", guarded)

    outcome = ts.move_transcript(tid, slug, clash="keep_both")
    assert outcome.status == "partial"
    loc = ts.locate(tid)
    assert loc.stem == "S (2)" and loc.campaign_id == cid  # left as step 5 wrote it


def test_move_overwrite_locked_target_returns_locked_and_changes_nothing(out, monkeypatch):
    from ._moves import claimed_campaign, lock_os_replace, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    mover, md = placed_session("S")
    lock_os_replace(monkeypatch, {"S.md"})

    # The overwritten session's own delete unlinks its .md directly, so the
    # delete's unlink is what a lock must stop (patch it, not os.replace).
    real_unlink = Path.unlink

    def guarded_unlink(self, *a, **k):
        if state["lock"] and self == other_md:
            raise PermissionError(32, "The process cannot access the file")
        return real_unlink(self, *a, **k)

    state = lock_os_replace(monkeypatch, set())  # arms the toggle, locks nothing yet
    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    outcome = ts.move_transcript(mover, slug, clash="overwrite")
    assert outcome.status == "locked"
    assert other_md.exists() and md.exists()
    assert ts.locate(other).stem == "S" and ts.locate(mover).campaign_id is None


def test_move_overwrite_locked_mover_keeps_the_deleted_target(out, monkeypatch):
    """When Overwrite deletes the target and the mover's own move is then
    refused, the overwritten session stays deleted and the mover is unchanged."""
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    mover, md = placed_session("S")

    real_replace = os.replace

    def guarded(src, dst, *a, **k):
        if Path(dst).name == "S.md" and Path(dst).parent.name == folder:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", guarded)
    monkeypatch.setattr(os, "rename", guarded)

    outcome = ts.move_transcript(mover, slug, clash="overwrite")
    assert outcome.status == "locked"
    assert not other_md.exists()          # the overwritten session's file is gone
    assert ts.locate(other) is None       # the overwritten session stays deleted
    assert md.exists() and ts.locate(mover).campaign_id is None


def test_move_overwrite_refused_late_by_a_job_on_the_target(out, tmp_path):
    from ._moves import claimed_campaign, placed_session
    from wisper_transcribe.web.jobs import JobQueue

    cid, slug, folder = claimed_campaign("Game")
    other, other_md = placed_session("S", campaign=slug, directory=out / folder)
    mover, md = placed_session("S")

    q = JobQueue()
    job = q.submit(str(out / "upload.mp3"), original_stem="S", campaign=slug,
                   output_dir=str(out / folder))
    job.transcript_id = other
    from wisper_transcribe import job_history
    job_history.record_required(job)

    outcome = ts.move_transcript(mover, slug, clash="overwrite")
    assert outcome.status == "busy" and job.id in outcome.busy
    assert other_md.exists() and ts.locate(other) is not None
    assert md.exists() and ts.locate(mover).campaign_id is None


def test_move_missing_session_changes_only_its_row(out, tmp_path):
    from ._moves import claimed_campaign

    cid, slug, folder = claimed_campaign("Game")
    tid = _insert_session("Gone")
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = ? WHERE id = ?", (db.now_utc(), tid))

    outcome = ts.move_transcript(tid, slug)
    assert outcome.status == "moved"
    assert not (out / folder).exists() or not list((out / folder).iterdir())
    assert ts.locate(tid).campaign_id == cid


def test_move_misplaced_self_removal_is_not_a_clash(out, tmp_path):
    from ._moves import claimed_campaign

    cid, slug, folder = claimed_campaign("Game")
    tid = _insert_session("S", campaign_id=cid, position=0)
    _place(out, out, "S", tid)  # its .md is in the root, not the folder

    outcome = ts.move_transcript(tid, None)
    assert outcome.status == "moved"
    assert (out / "S.md").exists() and not (out / folder / "S.md").exists()
    assert ts.locate(tid).campaign_id is None


def test_move_busy_for_a_pending_upload_into_the_target(out, tmp_path):
    from ._moves import claimed_campaign, placed_session
    from wisper_transcribe.web.jobs import JobQueue

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S")
    q = JobQueue()
    job = q.submit(str(out / "upload.mp3"), original_stem="T", campaign=slug,
                   output_dir=str(out / folder))

    outcome = ts.move_transcript(tid, slug)
    assert outcome.status == "busy" and job.id in outcome.busy
    assert md.exists() and ts.locate(tid).campaign_id is None


def test_move_busy_for_a_queued_re_transcribe_of_the_session(out, tmp_path):
    from ._moves import claimed_campaign, placed_session
    from wisper_transcribe.web.jobs import JobQueue

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S", campaign=slug, directory=out / folder)
    q = JobQueue()
    job = q.submit(str(out / "s.flac"), original_stem="S", output_dir=str(out / folder))
    assert job.transcript_id == tid

    assert ts.move_transcript(tid, None).status == "busy"


def test_move_busy_for_a_pending_journal_job_on_the_campaign(out, tmp_path):
    from ._moves import claimed_campaign, placed_session
    from wisper_transcribe.web.jobs import JobQueue

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S", campaign=slug, directory=out / folder)
    JobQueue().submit_journal(slug)

    assert ts.move_transcript(tid, None).status == "busy"


def test_move_busy_when_a_capture_is_recording_the_campaign(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S")
    import uuid
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO recordings (id, source, capture_status, started_at, campaign_id) "
            "VALUES (?, 'discord', 'recording', ?, ?)", (str(uuid.uuid4()), db.now_utc(), cid))

    assert ts.move_transcript(tid, slug).status == "busy"


def test_move_out_of_a_journaled_campaign_marks_it_stale(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid = _insert_session("S", campaign_id=cid, position=0)
    _place(out, out / folder, "S", tid)
    with db.transaction() as conn:
        conn.execute("INSERT INTO journal_entries (transcript_id, campaign_id, folded_at) "
                     "VALUES (?, ?, 'now')", (tid, cid))

    assert ts.move_transcript(tid, None).status == "moved"
    with db.connection() as conn:
        assert conn.execute("SELECT journal_stale_since FROM campaigns WHERE id = ?",
                            (cid,)).fetchone()[0] is not None


def test_move_files_home_busy_on_a_job(out, tmp_path):
    from ._moves import claimed_campaign, placed_session
    from wisper_transcribe.web.jobs import JobQueue

    cid, slug, folder = claimed_campaign("Game")
    tid = _insert_session("S", campaign_id=cid, position=0)
    _place(out, out, "S", tid)
    q = JobQueue()
    q.submit(str(out / "upload.mp3"), original_stem="T", campaign=slug,
             output_dir=str(out / folder))

    assert ts.move_files_home(tid).status == "busy"


def test_move_files_home_clash_is_never_overwritten(out, tmp_path):
    from ._moves import claimed_campaign

    cid, slug, folder = claimed_campaign("Game")
    tid = _insert_session("S", campaign_id=cid, position=0)
    _place(out, out, "S", tid)  # the misplaced .md
    (out / folder).mkdir(exist_ok=True)
    (out / folder / "S.md").write_text("taken", encoding="utf-8")

    outcome = ts.move_files_home(tid)
    assert outcome.status == "clash"
    assert (out / "S.md").exists()
    assert (out / folder / "S.md").read_text(encoding="utf-8") == "taken"


def test_move_no_clash_and_no_missing_folder_is_a_pure_read(out, tmp_path):
    """check_move renders the clash page with no mkdir: re-run leaves no folder."""
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S")
    before = sorted(p.name for p in out.iterdir())
    ts.check_move(tid, slug)
    assert sorted(p.name for p in out.iterdir()) == before


# ---------------------------------------------------------------------------
# rename_transcript
# ---------------------------------------------------------------------------

def test_rename_carries_companions(out, tmp_path):
    from ._moves import add_companion, placed_session

    tid, md = placed_session("Old")
    add_companion(tid, md, ".summary.md")
    add_companion(tid, md, "_diar.json", "{}")
    add_companion(tid, md, ".flac")

    outcome = ts.rename_transcript(tid, "New")
    assert outcome.status == "moved" and outcome.new_stem == "New"
    assert not md.exists()
    assert sorted(p.name for p in out.iterdir()) == ["New.flac", "New.md", "New.summary.md",
                                                     "New_diar.json"]
    assert all(rel.startswith("New") for rel in _files_root_rel(tid))


def test_rename_reserved_and_invalid_names_are_refused(out, tmp_path):
    from ._moves import claimed_campaign, placed_session

    cid, slug, folder = claimed_campaign("Game")
    tid, md = placed_session("S", campaign=slug, directory=out / folder)
    for bad in (".hidden", "COM1", "x" * 101, "a/b", "Notes.md"):
        assert ts.rename_transcript(tid, bad).status == "invalid", bad
    assert ts.rename_transcript(tid, f"{folder} Journal").status == "reserved"
    assert md.exists() and not (out / folder / "Game Journal.md").exists()


def test_rename_clash_ask_and_keep_both(out, tmp_path):
    from ._moves import placed_session

    tid, md = placed_session("Old")
    other, other_md = placed_session("New")
    outcome = ts.rename_transcript(tid, "New")
    assert outcome.status == "clash" and outcome.overwrite_allowed is True
    assert md.exists() and other_md.exists()

    outcome = ts.rename_transcript(tid, "New", clash="keep_both")
    assert outcome.status == "moved" and outcome.new_stem == "New (2)"
    assert (out / "New (2).md").exists() and other_md.exists()


def test_rename_locked_md_restores_the_old_stem(out, monkeypatch):
    from ._moves import lock_os_replace, placed_session

    tid, md = placed_session("Old")
    lock_os_replace(monkeypatch, {"New.md"})
    outcome = ts.rename_transcript(tid, "New")
    assert outcome.status == "locked" and md in outcome.kept
    assert md.exists() and ts.locate(tid).stem == "Old"


def test_rename_case_only_on_a_case_insensitive_filesystem(out, monkeypatch):
    from ._moves import placed_session

    tid, md = placed_session("Session")
    monkeypatch.setattr(file_registry, "_fold", lambda d: True)
    monkeypatch.setattr(ts, "_clash_path", lambda *a, **k: None)  # fs itself is sensitive
    outcome = ts.rename_transcript(tid, "SESSION")
    assert outcome.status == "moved" and outcome.new_stem == "SESSION"
    with db.connection() as conn:
        assert conn.execute("SELECT stem FROM transcripts WHERE id = ?", (tid,)).fetchone()[0] == \
            "SESSION"

