"""transcript_store: registry rows, the delete ordering rule, atomic writes."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from wisper_transcribe import db, transcript_store as ts
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
    and its links (the #64 bug class)."""
    suspicious = re.compile(r"(md_path|transcript_path|transcript|\.md\b)[^\n]*\.unlink\(|\.unlink\([^\n]*\.md\b")
    offenders = [
        f"{rel}:{no}: {line.strip()}"
        for rel, no, line in _src_lines()
        if rel != "transcript_store.py" and suspicious.search(line)
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


def test_reconcile_sweeps_only_true_orphans_and_old_temps(out, case_sensitive):
    import time as _time

    _md(out, "kept")
    ts.register("kept", origin="job")
    files = {
        "kept.summary.md": True,          # has a .md
        "gone_diar.json": False,          # no .md, no row
        "gone_excerpt_SPEAKER_00.mp3": False,
        "gone.summary.md": False,
        "random-audio.wav": True,         # never a generic audio file
    }
    for name in files:
        (out / name).write_text("x", encoding="utf-8")
    old_temp = out / f"{ts.TEMP_PREFIX}kept.md.1-1"
    new_temp = out / f"{ts.TEMP_PREFIX}kept.md.2-2"
    old_temp.write_text("x", encoding="utf-8")
    new_temp.write_text("x", encoding="utf-8")
    stale = _time.time() - ts._TEMP_MAX_AGE_S - 60
    os.utime(old_temp, (stale, stale))

    ts.reconcile(out)                     # list pages: no sweep
    assert all((out / n).exists() for n in files)
    ts.reconcile(out, sweep=True)         # startup
    for name, keep in files.items():
        assert (out / name).exists() == keep, name
    assert not old_temp.exists() and new_temp.exists()


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
        assert conn.execute("SELECT audio_rel_path FROM transcripts").fetchone()[0] == "s01_1.wav"


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
