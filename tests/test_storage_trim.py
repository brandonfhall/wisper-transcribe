"""wisper storage trim: plan (read-only) and apply, with encode/probe/backfill mocked."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from tests._seed import seed_recording, seed_sidecar
from wisper_transcribe import db, file_registry, recording_manager, storage_trim, transcript_store
from wisper_transcribe.config import EMBEDDING_SPACE


@pytest.fixture
def out(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(path))
    return path


def _fake_encode(src, dst):
    Path(dst).write_bytes(b"flac")


# The frame count an encode produced, keyed by destination: probe_frames
# returns it so a recording's combined.wav → combined.flac verifies.
_flac_frames: dict[str, int] = {}


def _recording_encode(src, dst):
    import wave

    try:
        with wave.open(str(src), "rb") as wf:
            _flac_frames[str(dst)] = wf.getnframes()
    except Exception:
        _flac_frames[str(dst)] = 0
    Path(dst).write_bytes(b"flac")


@pytest.fixture
def encode(monkeypatch):
    _flac_frames.clear()
    monkeypatch.setattr("wisper_transcribe.audio_utils.probe_frames",
                        lambda p: _flac_frames.get(str(p), 0))
    with mock.patch("wisper_transcribe.audio_utils.encode_flac",
                    side_effect=_recording_encode) as m:
        yield m


@pytest.fixture
def probe():
    with mock.patch("wisper_transcribe.audio_utils.probe_format", return_value=(16000, 1)) as m:
        yield m


def _transcript(out: Path, stem: str, audio_name: str, *, embedded: bool = True,
                payload: bytes = b"x" * 100) -> Path:
    md = out / f"{stem}.md"
    md.write_text("# t\n", encoding="utf-8")
    audio = out / audio_name
    audio.write_bytes(payload)
    diar = {
        "diarization_segments": [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}],
        "speaker_map": {"SPEAKER_00": "Alice"},
        "input_path": str(audio),
    }
    if embedded:
        diar["speaker_embeddings"] = {"SPEAKER_00": [1.0, 0.0]}
        diar["embedding_space"] = EMBEDDING_SPACE
    seed_sidecar(md, diar)
    return md


def _audio_row(md: Path):
    loc = transcript_store.locate_path(md)
    owner = file_registry.Owner("transcript", loc.id)
    return file_registry.file_for(owner, "audio")


def _tree(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size)
                  for p in root.rglob("*") if p.is_file())


# --- upload transcripts ------------------------------------------------------

def test_mp4_is_converted_to_flac(out, encode, probe):
    md = _transcript(out, "Session 1", "Session 1.mp4")
    report = storage_trim.apply()
    assert (out / "Session 1.flac").is_file()
    assert not (out / "Session 1.mp4").exists()
    assert _audio_row(md).path.name == "Session 1.flac"
    assert report.converted == ["Session 1"] and not report.errors


def test_embeddings_are_stored_before_conversion(out, encode, probe):
    md = _transcript(out, "S", "S.mp4", embedded=False)
    parent = mock.Mock()
    vec = {"SPEAKER_00": np.ones(4, dtype=np.float32)}
    parent.backfill.side_effect = lambda *a, **k: vec
    parent.encode.side_effect = _fake_encode
    with mock.patch("wisper_transcribe.speaker_registry._backfill_embeddings", parent.backfill), \
         mock.patch("wisper_transcribe.audio_utils.encode_flac", parent.encode):
        storage_trim.apply()
    names = [c[0] for c in parent.mock_calls]
    assert names.index("backfill") < names.index("encode")
    diar = transcript_store.read_sidecar(md)
    assert "SPEAKER_00" in diar["speaker_embeddings"]


def test_backfill_failure_is_reported_and_conversion_continues(out, encode, probe):
    _transcript(out, "S", "S.mp4", embedded=False)
    with mock.patch("wisper_transcribe.speaker_registry._backfill_embeddings",
                    side_effect=RuntimeError("corrupt")):
        report = storage_trim.apply()
    assert any("voices not extracted" in e for e in report.errors)
    assert report.converted == ["S"]


def test_kept_flac_is_untouched(out, encode, probe):
    md = _transcript(out, "S", "S.flac")
    before = _tree(out)
    report = storage_trim.apply()
    assert _tree(out) == before and not report.converted
    encode.assert_not_called()
    assert _audio_row(md).path.name == "S.flac"


def test_wrong_format_flac_is_converted_in_place_through_a_temp(out, encode):
    md = _transcript(out, "S", "S.flac")
    with mock.patch("wisper_transcribe.audio_utils.probe_format", return_value=(48000, 2)):
        report = storage_trim.apply()
    assert report.converted == ["S"]
    (src, dst), _ = encode.call_args
    assert Path(dst).name.startswith(transcript_store.TEMP_PREFIX)
    assert (out / "S.flac").read_bytes() == b"flac"
    assert _audio_row(md).path.name == "S.flac"
    assert not [p for p in out.iterdir() if p.name.startswith(transcript_store.TEMP_PREFIX)]


def test_probe_failure_converts(out, encode):
    _transcript(out, "S", "S.flac")
    with mock.patch("wisper_transcribe.audio_utils.probe_format", side_effect=RuntimeError("x")):
        report = storage_trim.apply()
    assert report.converted == ["S"]


def test_encode_failure_leaves_original_and_row(out, probe):
    md = _transcript(out, "S", "S.mp4")
    with mock.patch("wisper_transcribe.audio_utils.encode_flac", side_effect=ValueError("boom")):
        report = storage_trim.apply()
    assert (out / "S.mp4").is_file() and not (out / "S.flac").exists()
    assert _audio_row(md).path.name == "S.mp4"
    assert report.errors and not report.converted


def test_foreign_flac_is_never_overwritten(out, encode, probe):
    md = _transcript(out, "S", "S.mp4")
    (out / "S.flac").write_bytes(b"users own")
    report = storage_trim.apply()
    assert (out / "S.flac").read_bytes() == b"users own"
    assert (out / "S.mp4").is_file() and _audio_row(md).path.name == "S.mp4"
    assert report.errors


# --- recordings and orphans --------------------------------------------------

def test_recording_linked_copy_is_dropped(out, encode, probe):
    rec = seed_recording(as_flac=True)
    md = _transcript(out, "Rec", f"{rec.id}.wav")
    tid = transcript_store.locate_path(md).id
    recording_manager.link_transcript(rec.id, tid)
    report = storage_trim.apply()
    assert not (out / f"{rec.id}.wav").exists()
    assert _audio_row(md) is None
    assert transcript_store.audio_path(md) == recording_manager.combined_path_for(rec.id)
    assert report.dropped == ["Rec"]
    encode.assert_not_called()


def test_orphans_only_unreferenced_recording_wavs(out, encode, probe):
    rec = seed_recording()
    orphan = out / f"{rec.id}.wav"
    orphan.write_bytes(b"w")
    stray_uuid = out / f"{uuid.uuid4()}.wav"
    stray_uuid.write_bytes(b"w")
    keep = {
        "unrelated.mp3": out / "unrelated.mp3",
        "ep1.mp3": out / "ep1.mp3",
        "ep1.md": out / "ep1.md",
        "Session_1.mp4": out / "Session_1.mp4",
        "Session.md": out / "Session.md",
    }
    for p in keep.values():
        p.write_bytes(b"k")
    # ep1.md is registered with a CLI-style transcript: no audio row.
    transcript_store.reconcile(out)
    report = storage_trim.apply()
    assert not orphan.exists()
    assert report.orphans == [orphan.name]
    assert stray_uuid.exists()
    assert all(p.exists() for p in keep.values())


def test_registered_recording_wav_is_not_an_orphan(out, encode, probe):
    rec = seed_recording(as_flac=True)
    md = _transcript(out, "Rec", f"{rec.id}.wav")  # an audio row names it, no link
    plan = storage_trim.plan()
    assert not [a for a in plan.actions if a.kind == storage_trim.ORPHAN]
    assert [a.kind for a in plan.actions] == [storage_trim.CONVERT]
    assert md.exists()


def test_recordings_are_trimmed(out, encode, probe):
    """A legacy recording's WAV is converted to FLAC first, then trimmed."""
    rec = seed_recording()
    seg = recording_manager.get_recording_dir(rec.id) / "combined"
    seg.mkdir()
    import shutil
    shutil.copy(recording_manager.combined_wav_path_for(rec.id), seg / "0000.wav")
    plan = storage_trim.plan()
    assert [a.kind for a in plan.actions] == [storage_trim.CONVERT_RECORDING,
                                             storage_trim.TRIM_RECORDING]
    assert seg.exists()  # a plan changes nothing
    report = storage_trim.apply()
    assert not seg.exists()
    assert recording_manager.combined_path_for(rec.id).is_file()  # now .flac
    assert not recording_manager.combined_wav_path_for(rec.id).exists()
    assert report.converted_recordings and report.trimmed
    assert storage_trim.plan().actions == []


def _combined_row(rec_id):
    return file_registry.file_for(file_registry.Owner("recording", rec_id), "combined")


def test_legacy_combined_wav_is_converted_in_place(out, encode, probe):
    rec = seed_recording()
    wav = recording_manager.combined_wav_path_for(rec.id)
    before = wav.stat().st_size
    assert storage_trim.plan().actions[0].kind == storage_trim.CONVERT_RECORDING

    report = storage_trim.apply()

    flac = recording_manager.combined_path_for(rec.id)
    assert flac.is_file() and not wav.exists()
    assert _combined_row(rec.id).path == flac
    assert rec.id in report.converted_recordings
    assert storage_trim.plan().actions == []  # idempotent


def test_a_zero_frame_combined_wav_blocks_conversion(out, encode, probe):
    rec = seed_recording()
    wav = recording_manager.combined_wav_path_for(rec.id)
    import wave
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
    report = storage_trim.apply()
    assert wav.is_file() and not recording_manager.combined_path_for(rec.id).exists()
    assert report.errors and not report.converted_recordings
    assert _combined_row(rec.id).path == wav


def test_a_truncated_combined_wav_blocks_conversion(out, encode, probe):
    rec = seed_recording()
    wav = recording_manager.combined_wav_path_for(rec.id)
    wav.write_bytes(b"RIFF" + b"\x00" * 10)  # an unreadable header
    report = storage_trim.apply()
    assert wav.is_file() and not recording_manager.combined_path_for(rec.id).exists()
    assert report.errors and not report.converted_recordings


def test_a_frame_count_mismatch_keeps_the_wav(out, encode, probe):
    """The FLAC probe disagrees with the WAV, so the conversion is refused."""
    rec = seed_recording()
    wav = recording_manager.combined_wav_path_for(rec.id)
    with mock.patch("wisper_transcribe.audio_utils.probe_frames", return_value=1):
        report = storage_trim.apply()
    assert wav.is_file() and not recording_manager.combined_path_for(rec.id).exists()
    assert any("verify" in e for e in report.errors)
    assert _combined_row(rec.id).path == wav


def test_conversion_updates_the_combined_row_size(out, encode, probe):
    rec = seed_recording()
    recording_manager.register_capture_files(rec.id)
    wav = recording_manager.combined_wav_path_for(rec.id)
    before = _combined_row(rec.id)
    assert before.path == wav and before.size == wav.stat().st_size
    storage_trim.apply()
    after = _combined_row(rec.id)
    flac = recording_manager.combined_path_for(rec.id)
    assert after.path == flac and after.size == flac.stat().st_size


def test_a_crash_leftover_flac_still_plans_the_conversion(out, encode, probe):
    """A .flac beside a .wav whose combined row still names the .wav (a crash
    before the row re-pointed) is re-encoded, and the WAV is deleted after."""
    rec = seed_recording()
    recording_manager.register_capture_files(rec.id)
    wav = recording_manager.combined_wav_path_for(rec.id)
    leftover = recording_manager.combined_path_for(rec.id)
    leftover.write_bytes(b"partial")  # a crash's half-written FLAC
    assert _combined_row(rec.id).path == wav  # its row still names the WAV
    plan = storage_trim.plan()
    assert [a.kind for a in plan.actions] == [storage_trim.CONVERT_RECORDING]
    storage_trim.apply()
    assert leftover.read_bytes() == b"flac" and not wav.exists()
    assert _combined_row(rec.id).path == leftover


def test_a_wav_left_after_the_repoint_is_converted_and_deleted(out, encode, probe):
    """A crash after the row re-pointed to the .flac but before the WAV was
    deleted: the next trim re-verifies and deletes the WAV."""
    rec = seed_recording()
    wav = recording_manager.combined_wav_path_for(rec.id)
    flac = recording_manager.combined_path_for(rec.id)
    wav_bytes = wav.read_bytes()
    storage_trim.apply()
    wav.write_bytes(wav_bytes)  # the WAV the crash left behind
    assert _combined_row(rec.id).path == flac

    assert [a.kind for a in storage_trim.plan().actions] == [storage_trim.CONVERT_RECORDING]
    storage_trim.apply()
    assert flac.is_file() and not wav.exists()
    assert _combined_row(rec.id).path == flac


def test_an_active_capture_is_not_converted(out, encode, probe):
    rec = seed_recording(status="recording")
    plan = storage_trim.plan()
    assert not [a for a in plan.actions if a.kind == storage_trim.CONVERT_RECORDING]


def test_trim_still_runs_after_a_recording_conversion(out, encode, probe):
    rec = seed_recording(source="local")
    rec_dir = recording_manager.get_recording_dir(rec.id)
    seg = rec_dir / "combined"
    seg.mkdir()
    import shutil
    shutil.copy(recording_manager.combined_wav_path_for(rec.id), seg / "0000.wav")
    mic = rec_dir / "per-user" / "mic"
    mic.mkdir(parents=True)
    shutil.copy(recording_manager.combined_wav_path_for(rec.id), mic / "0000.wav")

    report = storage_trim.apply()

    assert report.converted_recordings and report.trimmed
    assert not seg.exists() and not (rec_dir / "per-user").exists()
    assert recording_manager.combined_path_for(rec.id).is_file()


# --- dry run and idempotence -------------------------------------------------

def _snapshot(out: Path) -> dict:
    with sqlite3.connect(db.db_path()) as conn:
        rows = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in ("transcripts", "transcript_speakers", "files")}
    return {"tree": _tree(out), "rows": rows}


def test_plan_changes_nothing(out, encode, probe):
    rec = seed_recording()
    _transcript(out, "A", "A.mp4", embedded=False)
    md = _transcript(out, "B", f"{rec.id}.wav")
    recording_manager.link_transcript(rec.id, transcript_store.locate_path(md).id)
    (out / f"{uuid.uuid4()}.wav").write_bytes(b"w")
    (out / "stray.flac").write_bytes(b"s")
    (out / "ghost.summary.md").write_text("g", encoding="utf-8")
    before = _snapshot(out)
    plan = storage_trim.plan()
    assert plan.actions and plan.attention.total
    assert _snapshot(out) == before
    encode.assert_not_called()


def test_second_run_is_a_noop(out, encode, probe):
    rec = seed_recording()
    _transcript(out, "A", "A.mp4")
    md = _transcript(out, "B", f"{rec.id}.wav")
    recording_manager.link_transcript(rec.id, transcript_store.locate_path(md).id)
    (out / f"{uuid.uuid4()}.wav").write_bytes(b"w")
    storage_trim.apply()
    assert storage_trim.plan().actions == []
    before = _snapshot(out)
    report = storage_trim.apply()
    assert not (report.converted or report.dropped or report.orphans or report.trimmed)
    assert _snapshot(out) == before


# --- organize: move sessions into their campaign folders ---------------------

def _campaign_with_misplaced(stem: str, *, display: str = "Game"):
    """A claimed campaign and a session assigned to it whose files are in the root.

    Returns ``(slug, folder, tid, md)``.
    """
    from tests._moves import claimed_campaign, placed_session

    _cid, slug, folder = claimed_campaign(display)
    tid, md = placed_session(stem, campaign=slug)
    return slug, folder, tid, md


def test_plan_lists_misplaced_sessions_and_moves_nothing(out):
    _slug, folder, tid, md = _campaign_with_misplaced("Stray")
    before = _tree(out)
    plan = storage_trim.plan()

    moves = [a for a in plan.actions if a.kind == storage_trim.ORGANIZE]
    assert [(a.stem, a.transcript_id, a.path.name, a.note) for a in moves] == [
        ("Stray", tid, "Stray.md", f"→ {folder}/")]
    assert plan.move_bytes >= md.stat().st_size
    assert _tree(out) == before          # a plan changes nothing
    assert plan.attention.misplaced and plan.attention.total


def test_apply_moves_every_file_into_the_folder_and_a_rerun_plans_nothing(out, encode, probe):
    from tests._moves import add_companion

    _slug, folder, tid, md = _campaign_with_misplaced("Stray")
    add_companion(tid, md, ".summary.md")
    add_companion(tid, md, "_diar.json")
    add_companion(tid, md, ".flac")

    report = storage_trim.apply()
    assert report.organized == ["Stray"] and not report.errors
    assert (out / folder / "Stray.md").is_file()
    assert (out / folder / "Stray.summary.md").is_file()
    assert (out / folder / "Stray_diar.json").is_file()
    assert (out / folder / "Stray.flac").is_file()
    assert not (out / "Stray.md").exists()
    assert transcript_store.locate(tid).misplaced is False
    assert not [a for a in storage_trim.plan().actions if a.kind == storage_trim.ORGANIZE]


def test_organize_clash_is_reported_and_skipped(out, encode, probe):
    _slug, folder, _tid, md = _campaign_with_misplaced("Stray")
    (out / folder / "Stray.md").write_text("someone else\n", encoding="utf-8")

    report = storage_trim.apply()
    assert any("Stray: a file with that name is already in Game" in e for e in report.errors)
    assert not report.organized
    assert md.is_file() and (out / folder / "Stray.md").read_text(encoding="utf-8") == "someone else\n"


def test_organize_moves_a_legacy_journal(out, encode, probe):
    from tests._seed import seed_campaign
    from wisper_transcribe import journal

    seed_campaign("Game", slug="game")   # unclaimed: adoption claims the folder
    legacy = journal.legacy_journal_path("game")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("---\ntitle: Game\n---\n## Story So Far\n", encoding="utf-8")

    plan = storage_trim.plan()
    assert any(a.slug == "game" and a.note == "journal" for a in plan.actions)

    report = storage_trim.apply()
    target = out / "Game" / "Game Journal.md"
    assert target.is_file() and "## Story So Far" in target.read_text(encoding="utf-8")
    assert not legacy.exists()
    assert (legacy.parent / "journal.md.v11-adopted").is_file()
    assert report.organized == ["game"]


def test_organize_runs_before_convert_so_the_flac_lands_in_the_folder(out, encode, probe):
    from tests import _seed

    slug, folder, tid, _md = _campaign_with_misplaced("Session 1")
    audio = out / "Session 1.mp4"
    audio.write_bytes(b"v" * 64)
    file_registry.add(audio, kind="audio",
                      owner=file_registry.Owner("transcript", tid),
                      output_dir=out)

    plan = storage_trim.plan()
    kinds = [a.kind for a in plan.actions]
    assert kinds.index(storage_trim.ORGANIZE) < kinds.index(storage_trim.CONVERT)

    storage_trim.apply()
    assert (out / folder / "Session 1.flac").is_file()
    assert not (out / folder / "Session 1.mp4").exists()
    assert not (out / "Session 1.mp4").exists() and not (out / "Session 1.flac").exists()
    assert transcript_store.locate(tid).misplaced is False


def test_blocked_campaign_is_reported_not_planned_on_every_run(out):
    from tests import _seed

    cid = _seed.seed_campaign("Game", slug="game")   # unclaimed
    (out / "Game").mkdir()
    (out / "Game" / "notes.md").write_text("# mine\n", encoding="utf-8")
    tid = _seed.seed_transcript("Stray", campaign="game", write_md=True)

    for _ in range(2):
        plan = storage_trim.plan()
        assert [a for a in plan.actions if a.kind == storage_trim.ORGANIZE] == []
        assert plan.blocked == [("Game", 1, "folder taken")]
        assert _seed.transcript_id("Stray") == tid
    assert (out / "Stray.md").is_file()


# --- locks -------------------------------------------------------------------

def test_server_lock_is_exclusive_within_a_process(tmp_path):
    first = db.ServerLock(tmp_path).acquire()
    with pytest.raises(db.ServerLockHeld):
        db.ServerLock(tmp_path).acquire()
    first.release()
    db.ServerLock(tmp_path).acquire().release()


def test_lock_file_existing_is_not_held(tmp_path):
    (tmp_path / db.SERVER_LOCK_FILENAME).write_text("stale")
    db.ServerLock(tmp_path).acquire().release()
    assert (tmp_path / db.SERVER_LOCK_FILENAME).read_text() == "stale"


def test_lock_is_held_across_processes(tmp_path):
    import subprocess
    import sys
    code = ("import sys,time;from pathlib import Path;from wisper_transcribe import db;"
            "l=db.ServerLock(Path(sys.argv[1])).acquire();print('ok',flush=True);time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ok"
        with pytest.raises(db.ServerLockHeld):
            db.ServerLock(tmp_path).acquire()
    finally:
        proc.kill()
        proc.wait()
    db.ServerLock(tmp_path).acquire().release()


def _lease(runtime: str, age_s: float = 0) -> None:
    from datetime import UTC, datetime, timedelta
    stamp = (datetime.now(UTC) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.connect().close()
    with sqlite3.connect(db.db_path()) as conn:
        conn.execute("INSERT OR REPLACE INTO runtime_leases VALUES (?, 'other:1', 0, ?)",
                     (runtime, stamp))


def test_container_refuses_fresh_host_lease(monkeypatch):
    _lease("host")
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("container", True))
    with pytest.raises(db.RuntimeConflict):
        storage_trim.check_runtime()


def test_container_allows_its_own_lease_and_stale_host(monkeypatch):
    _lease("container")
    _lease("host", age_s=db.LEASE_TTL_S + 60)
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("container", True))
    storage_trim.check_runtime()


def test_check_runtime_does_not_create_the_database(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path / "fresh"))
    monkeypatch.setattr(db, "detect_runtime", lambda: db.RuntimeInfo("container", True))
    storage_trim.check_runtime()
    assert not (tmp_path / "fresh" / db.DB_FILENAME).exists()
