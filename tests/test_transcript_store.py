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
    _md(out, "mix*")
    other_clip = out / "mixdown_excerpt_SPEAKER_00.mp3"
    other_clip.write_bytes(b"x")
    ts.delete_transcript("mix*")
    assert other_clip.exists()


def test_delete_missing_transcript_still_removes_row(out):
    create_campaign("Game")
    move_transcript_to_campaign("gone", "game")
    assert ts.delete_transcript("gone") is True
    assert _rows() == {}


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "..", "x\x00y"])
def test_delete_refuses_unsafe_stem(bad):
    assert ts.delete_transcript(bad) is False


def test_delete_reverts_recording_link(out):
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording

    rec = create_recording("VC1", "G1")
    rec.status = "transcribed"
    rec.transcript_path = _md(out, rec.id)
    save_recording(rec)
    ts.register(rec.id, origin="job")

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
    ("db.py", '"import-report.txt").write_text('),        # migration report in the backup dir
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
