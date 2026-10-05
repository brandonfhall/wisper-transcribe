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
    move_transcript_to_campaign,
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
    _md(out, "s01")
    ts.register("s01", origin="job")
    assert _rows() == {"s01": False}


def test_register_keeps_identity_and_campaign(out):
    create_campaign("Game")
    _md(out, "s01")
    first = ts.register("s01", origin="job")
    move_transcript_to_campaign("s01", "game")
    assert ts.register("s01", origin="job") == first  # overwrite keeps the row
    assert get_transcripts_for_campaign("game") == ["s01"]


def test_register_clears_missing_flag(out):
    create_campaign("Game")
    move_transcript_to_campaign("s01", "game")  # no .md yet: flagged missing
    assert _rows() == {"s01": True}
    _md(out, "s01")
    ts.register("s01", origin="reconcile")
    assert _rows() == {"s01": False}


def test_register_rejects_unknown_origin():
    with pytest.raises(ValueError):
        ts.register("s01", origin="guess")


def test_register_normalizes_to_nfc(out):
    import unicodedata

    ts.register(unicodedata.normalize("NFD", "Café"), origin="job")
    assert list(_rows()) == [unicodedata.normalize("NFC", "Café")]


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
    ts.register("s01", origin="job")
    move_transcript_to_campaign("s01", "game")
    companions = _with_companions(out, "s01", "s01_1.wav")  # collision-suffixed audio

    assert ts.delete_transcript("s01") is True
    assert not md.exists()
    assert all(not f.exists() for f in companions)
    assert _rows() == {}
    assert get_transcripts_for_campaign("game") == []


def test_delete_keeps_audio_outside_output_root(out, tmp_path):
    _md(out, "s01")
    outside = tmp_path / "user-file.wav"
    outside.write_bytes(b"audio")
    (out / "s01_diar.json").write_text(json.dumps({"input_path": str(outside)}), encoding="utf-8")
    ts.delete_transcript("s01")
    assert outside.exists()


def test_delete_escapes_glob_in_stem(out):
    # "[1]" is a glob character class that's also a legal Windows filename.
    _md(out, "mix[1]")
    other_clip = out / "mix1_excerpt_SPEAKER_00.mp3"   # matched by an unescaped "mix[1]_excerpt_*"
    own_clip = out / "mix[1]_excerpt_SPEAKER_00.mp3"
    other_clip.write_bytes(b"x")
    own_clip.write_bytes(b"x")
    ts.delete_transcript("mix[1]")
    assert other_clip.exists()
    assert not own_clip.exists()


def test_delete_missing_transcript_still_removes_row(out):
    create_campaign("Game")
    move_transcript_to_campaign("gone", "game")
    assert ts.delete_transcript("gone") is True
    assert _rows() == {}


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "..", "x\x00y"])
def test_delete_refuses_unsafe_stem(bad):
    assert ts.delete_transcript(bad) is False


def test_delete_reverts_recording_link(out):
    from wisper_transcribe.recording_manager import (
        create_recording, link_transcript, load_recordings, update_recording_status,
    )

    rec = create_recording("VC1", "G1")
    update_recording_status(rec.id, "completed")
    ts.register(rec.id, origin="job")
    link_transcript(rec.id, _md(out, rec.id))
    assert load_recordings()[rec.id].status == "transcribed"

    ts.delete_transcript(rec.id)

    loaded = load_recordings()[rec.id]
    assert loaded.transcript_path is None
    assert loaded.status == "completed"


def test_row_delete_happens_after_md_unlink(out, monkeypatch):
    """Ordering rule: if the .md can't be removed, the row stays."""
    md = _md(out, "s01")
    ts.register("s01", origin="job")
    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self == md:
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    ts.delete_transcript("s01")
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
        _md(out, stem)
        ts.register(stem, origin="job")
        move_transcript_to_campaign(stem, "game")
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
    _md(out, "session one")
    ts.register("session one", origin="job")
    move_transcript_to_campaign("session one", "game")

    (out / "session one.md").rename(out / "Session One.md")
    counts = ts.reconcile(out)
    assert counts["renamed"] == 1 and counts["added"] == 0
    assert _rows() == {"Session One": False}
    assert get_transcripts_for_campaign("game") == ["Session One"]


def test_reconcile_maps_nfd_filenames_to_nfc_rows(out, case_sensitive):
    import unicodedata

    nfc_name = unicodedata.normalize("NFC", "Café")
    ts.register(nfc_name, origin="job")
    _md(out, unicodedata.normalize("NFD", "Café"))
    counts = ts.reconcile(out)
    assert counts["added"] == 0
    assert _rows() == {nfc_name: False}


def test_reconcile_sweeps_old_temps_but_never_a_companion(out, case_sensitive):
    import time as _time

    _md(out, "kept")
    ts.register("kept", origin="job")
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
    _md(out, "old name")
    ts.register("old name", origin="job")
    move_transcript_to_campaign("old name", "game")
    (out / "old name.md").rename(out / "new name.md")
    # A new mtime, so reconcile can't prove the rename and lists it for relink.
    os.utime(out / "new name.md", ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    ts.reconcile(out)  # old flagged missing, new registered


def test_relink_moves_identity_to_new_file(out, case_sensitive):
    _missing_entry(out)
    with db.connection() as conn:
        old_id = conn.execute("SELECT id FROM transcripts WHERE stem = 'old name'").fetchone()[0]
    assert ts.relink_candidates() == ["new name"]

    ts.relink("old name", "new name")

    with db.connection() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT id, stem, missing_since FROM transcripts")]
    assert rows == [(old_id, "new name", None)]
    assert get_transcripts_for_campaign("game") == ["new name"]
    assert ts.relink_candidates() == []


def test_relink_refuses_present_source_and_linked_target(out, case_sensitive):
    _missing_entry(out)
    _md(out, "present")
    ts.register("present", origin="job")
    with pytest.raises(ValueError):
        ts.relink("present", "new name")          # not missing
    move_transcript_to_campaign("new name", "game")
    with pytest.raises(ValueError):
        ts.relink("old name", "new name")         # target already in a campaign
    with pytest.raises(KeyError):
        ts.relink("old name", "no such file")
    with pytest.raises(ValueError):
        ts.relink("old name", "../escape")


# ---------------------------------------------------------------------------
# Renames keep a transcript's files
# ---------------------------------------------------------------------------

_BUMP_NS = 1_700_000_000_000_000_000
_COMPANION_SUFFIXES = (".summary.md", "_diar.json", "_excerpt_SPEAKER_00.mp3",
                       "_excerpt_SPEAKER_01.mp3", ".md.bak", ".flac")


def _session(out: Path, stem: str) -> int:
    """A registered transcript with a summary, sidecar, two excerpts, a backup, and audio."""
    _md(out, stem)
    tid = ts.register(stem, origin="job")
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

    assert ts.relink("Session 5", "Hanataz 05") == []

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

    kept = ts.relink("Session 5", "Hanataz 05")

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
    ts.relink("Session 5", "Hanataz 05")
    assert (out / "Hanataz 05_excerpt_SPEAKER_02.txt").is_file()
    assert "Hanataz 05_excerpt_SPEAKER_02.txt" in _registered_names(tid)


def test_rename_does_not_touch_another_transcripts_files(out, case_sensitive):
    _session(out, "Session 5")
    other = _session(out, "Session 5 B")
    ts.rename_companions(file_registry.Owner.for_stem("Session 5").id, "Session 5", "Hanataz 05")
    assert (out / "Session 5 B.summary.md").is_file()
    assert "Session 5 B.summary.md" in _registered_names(other)
    assert (out / "Hanataz 05.summary.md").is_file()


def test_automatic_match_renames_the_transcript_and_its_files(out, case_sensitive):
    create_campaign("Game")
    for stem in ("s01", "Session 5", "s03"):
        _session(out, stem)
        move_transcript_to_campaign(stem, "game")
    tid = file_registry.Owner.for_stem("Session 5").id
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
    assert ts.needs_attention(out).total == 0


def test_automatic_match_needs_one_missing_row_and_one_new_file(out, case_sensitive):
    for stem in ("a", "b"):
        _md(out, stem)
        os.utime(out / f"{stem}.md", ns=(_BUMP_NS, _BUMP_NS))
        ts.register(stem, origin="job")
    os.replace(out / "a.md", out / "c.md")
    (out / "b.md").unlink()                    # two missing rows share c.md's size and mtime
    counts = ts.reconcile(out)
    assert counts["renamed"] == 0 and counts["added"] == 1
    assert _rows() == {"a": True, "b": True, "c": False}


def test_automatic_match_refuses_two_new_files_with_one_stat(out, case_sensitive):
    import shutil

    _md(out, "a")
    ts.register("a", origin="job")
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
    kept = ts.rename_companions(tid, "s", "t")

    assert {p.name for p in kept} == {f"s{s}" for s in _COMPANION_SUFFIXES}
    assert all((out / f"s{s}").is_file() for s in _COMPANION_SUFFIXES)
    assert _registered_names(tid) == {"s.md"} | {f"s{s}" for s in _COMPANION_SUFFIXES}


def test_rename_companions_ignores_an_unchanged_stem(out, case_sensitive):
    tid = _session(out, "s")
    assert ts.rename_companions(tid, "s", "s") == []
    assert (out / "s.summary.md").is_file()


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------

def test_needs_attention_lists_orphans_and_vanished_files(out, case_sensitive):
    create_campaign("Game")
    _session(out, "kept")
    _md(out, "lost")
    ts.register("lost", origin="job")
    move_transcript_to_campaign("lost", "game")
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
        owner = file_registry.Owner.for_stem("s01", conn=conn)
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
    audio = out / "s01_1.wav"
    audio.write_bytes(b"a")
    ts.write_sidecar(md, _full_diar(audio))
    ts.delete_transcript("s01")
    assert not audio.exists()
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM transcript_speakers").fetchone()[0] == 0


def test_audio_path_resolves_flac_recording_and_unlinked(out):
    from wisper_transcribe.recording_manager import link_transcript

    from ._seed import seed_recording

    md = _md(out, "s01")
    ts.register("s01", origin="job")
    assert ts.audio_path(md) is None  # unlinked, no audio row

    flac = out / "s01.flac"
    flac.write_bytes(b"f")
    ts.set_audio(md, flac)
    assert ts.audio_path(md) == flac

    rec_md = _md(out, "rec")
    ts.register("rec", origin="job")
    rec = seed_recording()
    link_transcript(rec.id, rec_md)  # no audio row and no _diar.json
    assert ts.audio_path(rec_md) == rec.combined_path


def test_read_sidecar_input_path_for_a_recording_without_speaker_rows(out):
    from wisper_transcribe.recording_manager import link_transcript

    from ._seed import seed_recording

    md = _md(out, "rec")
    ts.register("rec", origin="job")
    rec = seed_recording()
    link_transcript(rec.id, md)
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
        ts.register(md.stem, origin="job")
        _result_store["diarization_segments"] = [
            DiarizationSegment(start=0.0, end=1.0, speaker="SPEAKER_00")]
        return md

    with patch("wisper_transcribe.web.jobs.process_file", side_effect=_process), \
            patch("wisper_transcribe.web.jobs._extract_speaker_excerpts"):
        queue._run_job(job)

    assert job.status == "completed"
    md = data / f"{rec.id}.md"
    owner = file_registry.Owner.for_stem(rec.id)
    assert file_registry.file_for(owner, "audio") is None
    link_transcript(rec.id, md)
    assert ts.audio_path(md) == combined

    ts.delete_transcript(rec.id)

    assert combined.exists()
    assert not md.exists()


def test_overwrite_clears_stale_speakers_and_segments(out):
    md = _md(out, "s01")
    ts.write_sidecar(md, _full_diar(out / "none.wav"))
    ts.register("s01", origin="job")      # e.g. `wisper transcribe --overwrite`
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
    ts.delete_transcript(unicodedata.normalize("NFC", "Café night"))
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


def test_companion_prefers_the_registered_path(out):
    tid = _seed.seed_transcript("s01", write_md=True)
    audio = out / "s01_1.wav"
    audio.write_bytes(b"a")
    ts.set_audio(out / "s01.md", audio)

    loc = ts.locate(tid)
    assert loc.companion(".flac") == audio                    # registered row, any suffix
    assert loc.companion(".summary.md") == out / "s01.summary.md"  # derived name

