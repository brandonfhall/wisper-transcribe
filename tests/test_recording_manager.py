"""Tests for recording_manager.py (recordings in wisper.db)."""
from __future__ import annotations

import sqlite3
import threading
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wisper_transcribe import db
from wisper_transcribe import recording_manager as rm
from wisper_transcribe.models import RejoinAttempt, SegmentRecord
from wisper_transcribe.recording_manager import (
    _validate_recording_id,
    append_marker,
    append_segment,
    create_recording,
    delete_recording,
    link_transcript,
    load_recordings,
    reconcile_on_startup,
    record_completed_wav_segment,
    save_recording,
    update_recording_status,
)

from ._seed import seed_profile


def _make_recording(tmp_path: Path, **kwargs):
    return create_recording(
        voice_channel_id=kwargs.get("voice_channel_id", "VC1"),
        guild_id=kwargs.get("guild_id", "G1"),
        campaign_slug=kwargs.get("campaign_slug"),
        data_dir=tmp_path,
        source=kwargs.get("source", "discord"),
        devices=kwargs.get("devices"),
        name=kwargs.get("name"),
    )


def _write_wav(path: Path, n_frames: int = 320) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * n_frames)


def _join(threads):
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()


# ---------------------------------------------------------------------------
# Create / load / save
# ---------------------------------------------------------------------------

def test_load_save_roundtrip(tmp_path):
    rec = _make_recording(tmp_path, name="Session 14 — the ambush")
    r = load_recordings(tmp_path)[rec.id]
    assert r.voice_channel_id == "VC1"
    assert r.guild_id == "G1"
    assert r.status == "recording"
    assert r.source == "discord"
    assert r.name == "Session 14 — the ambush"
    assert r.started_at == rec.started_at  # microseconds and timezone survive
    assert r.per_user_dir == tmp_path / "recordings" / rec.id / "per-user"
    assert r.combined_path is None and r.markers == [] and r.devices == {}


def test_local_source_and_devices_roundtrip(tmp_path):
    rec = _make_recording(tmp_path, source="local", voice_channel_id="", guild_id="",
                          devices={"mic": "USB Mic", "system": "BlackHole 2ch"})
    r = load_recordings(tmp_path)[rec.id]
    assert r.source == "local"
    assert r.devices == {"mic": "USB Mic", "system": "BlackHole 2ch"}


def test_create_recording_generates_uuid(tmp_path):
    import uuid

    rec = _make_recording(tmp_path)
    assert uuid.UUID(rec.id).version == 4


def test_load_is_in_creation_order(tmp_path):
    ids = [_make_recording(tmp_path).id for _ in range(3)]
    assert list(load_recordings(tmp_path)) == ids


def test_load_returns_empty_when_none(tmp_path):
    assert load_recordings(tmp_path) == {}


def test_update_recording_status(tmp_path):
    rec = _make_recording(tmp_path)
    ended = datetime.now(timezone.utc)
    update_recording_status(rec.id, "completed", tmp_path, ended_at=ended)
    r = load_recordings(tmp_path)[rec.id]
    assert r.status == "completed" and r.ended_at == ended


def test_update_recording_status_raises_for_unknown(tmp_path):
    with pytest.raises(KeyError):
        update_recording_status("00000000-0000-4000-8000-000000000000", "failed", tmp_path)


def test_update_recording_status_refuses_derived_states(tmp_path):
    rec = _make_recording(tmp_path)
    with pytest.raises(ValueError):
        update_recording_status(rec.id, "transcribed", tmp_path)


def test_delete_recording_removes_rows_not_files(tmp_path):
    rec = _make_recording(tmp_path)
    audio = tmp_path / "recordings" / rec.id / "combined" / "0000.wav"
    _write_wav(audio)
    delete_recording(rec.id, tmp_path)
    assert rec.id not in load_recordings(tmp_path)
    assert audio.exists()


# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------

def test_combined_path_is_derived_from_the_layout(tmp_path):
    rec = _make_recording(tmp_path)
    combined = tmp_path / "recordings" / rec.id / "combined.wav"
    _write_wav(combined)
    assert load_recordings(tmp_path)[rec.id].combined_path == combined


def test_save_refuses_off_layout_paths(tmp_path):
    rec = _make_recording(tmp_path)
    rec.combined_path = tmp_path / "elsewhere.wav"
    with pytest.raises(ValueError, match="combined_path"):
        save_recording(rec, tmp_path)
    rec.combined_path = None
    rec.segment_manifest = [SegmentRecord(0, "mixed", rec.started_at, 1.0, tmp_path / "x.wav")]
    with pytest.raises(ValueError, match="segment"):
        save_recording(rec, tmp_path)


def test_marker_elapsed_is_derived_from_started_at(tmp_path):
    rec = _make_recording(tmp_path)
    marker = append_marker(rec.id, tmp_path)
    [loaded] = load_recordings(tmp_path)[rec.id].markers
    assert loaded.timestamp == marker.timestamp
    assert loaded.elapsed_s == pytest.approx((marker.timestamp - rec.started_at).total_seconds())


def test_markers_in_the_same_second_are_kept(tmp_path):
    rec = _make_recording(tmp_path)
    append_marker(rec.id, tmp_path)
    append_marker(rec.id, tmp_path)
    assert len(load_recordings(tmp_path)[rec.id].markers) == 2


def test_append_marker_unknown_id_raises(tmp_path):
    with pytest.raises(KeyError):
        append_marker("00000000-0000-4000-8000-000000000000", tmp_path)


def test_transcribed_follows_the_transcript_link(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe import transcript_store
    from wisper_transcribe.path_utils import get_output_dir

    rec = _make_recording(tmp_path)
    update_recording_status(rec.id, "completed", tmp_path)
    md = get_output_dir() / f"{rec.id}.md"
    md.write_text("x", encoding="utf-8")
    link_transcript(rec.id, md, tmp_path)
    r = load_recordings(tmp_path)[rec.id]
    assert r.status == "transcribed" and r.transcript_path == md

    transcript_store.delete_transcript(rec.id)       # ON DELETE SET NULL
    r = load_recordings(tmp_path)[rec.id]
    assert r.status == "completed" and r.transcript_path is None


def test_transcribing_only_while_a_job_is_active(tmp_path):
    from wisper_transcribe import job_history

    from ._seed import seed_job

    job_id = "11111111-1111-4111-8111-111111111111"
    rec = _make_recording(tmp_path)
    update_recording_status(rec.id, "completed", tmp_path)
    seed_job(job_id, status="running", recording_id=rec.id, data_dir=tmp_path)
    r = load_recordings(tmp_path)[rec.id]
    assert r.status == "transcribing" and r.job_id == job_id
    job_history.mark_interrupted(tmp_path)           # a restart
    r = load_recordings(tmp_path)[rec.id]
    assert r.status == "completed" and r.job_id == job_id


def test_saving_a_derived_status_keeps_the_capture_state(tmp_path):
    rec = _make_recording(tmp_path)
    update_recording_status(rec.id, "completed", tmp_path)
    rec = load_recordings(tmp_path)[rec.id]
    rec.status = "transcribing"                      # as a stale object might carry
    save_recording(rec, tmp_path)
    assert load_recordings(tmp_path)[rec.id].status == "completed"


# ---------------------------------------------------------------------------
# Discord speakers
# ---------------------------------------------------------------------------

def test_unbound_speakers_are_rows_without_a_profile(tmp_path):
    seed_profile("alice", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec.discord_speakers = {"111": "alice", "222": ""}
    rec.unbound_speakers = ["222", "333"]
    save_recording(rec, tmp_path)
    r = load_recordings(tmp_path)[rec.id]
    assert r.discord_speakers == {"111": "alice", "222": "", "333": ""}
    assert r.unbound_speakers == ["222", "333"]


def test_deleting_a_profile_unbinds_its_speakers(tmp_path):
    from wisper_transcribe.speaker_manager import remove_profile

    seed_profile("alice", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec.discord_speakers = {"111": "alice"}
    save_recording(rec, tmp_path)
    remove_profile("alice", tmp_path)
    r = load_recordings(tmp_path)[rec.id]
    assert r.unbound_speakers == ["111"]


def test_profile_rename_follows_into_recordings(tmp_path):
    from wisper_transcribe.speaker_manager import rename_profile

    seed_profile("alice", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec.discord_speakers = {"111": "alice"}
    save_recording(rec, tmp_path)
    rename_profile("alice", "Alicia", tmp_path)
    assert load_recordings(tmp_path)[rec.id].discord_speakers == {"111": "alicia"}


def test_stale_save_never_unbinds_a_speaker(tmp_path):
    seed_profile("bob", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec.unbound_speakers = ["222"]
    save_recording(rec, tmp_path)
    fresh = load_recordings(tmp_path)[rec.id]
    fresh.discord_speakers["222"] = "bob"            # the enroll job binds him
    fresh.unbound_speakers = []
    save_recording(fresh, tmp_path)
    save_recording(rec, tmp_path)                    # the capture manager's stale copy
    assert load_recordings(tmp_path)[rec.id].discord_speakers == {"222": "bob"}


def test_subtype_constraints(tmp_path):
    local = _make_recording(tmp_path, source="local", voice_channel_id="", guild_id="")
    discord = _make_recording(tmp_path)
    bad = [
        ("INSERT INTO recording_discord (recording_id, guild_id, voice_channel_id) VALUES (?, 'g', 'c')",
         local.id),
        ("INSERT INTO recording_devices (recording_id, role, device_name) VALUES (?, 'mic', 'x')",
         discord.id),
        ("INSERT INTO recording_speakers (recording_id, discord_user_id) VALUES (?, '1')", local.id),
        ("INSERT INTO recording_rejoins (recording_id, attempted_at, close_code, attempt_number) "
         "VALUES (?, 'x', 1, 1)", local.id),
        ("UPDATE recordings SET capture_status = 'recording', ended_at = 'x' WHERE id = ?", discord.id),
        ("UPDATE recordings SET recovered_at = 'x', capture_status = 'failed' WHERE id = ?", discord.id),
        ("UPDATE recordings SET capture_status = 'transcribed' WHERE id = ?", discord.id),
    ]
    for sql, rid in bad:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(tmp_path) as conn:
                conn.execute(sql, (rid,))


# ---------------------------------------------------------------------------
# Concurrency: appends are never lost to a stale save
# ---------------------------------------------------------------------------

def test_stale_save_keeps_markers_segments_and_rejoins_appended_meanwhile(tmp_path):
    rec = _make_recording(tmp_path)                  # long-lived capture object
    seg_path = tmp_path / "recordings" / rec.id / "combined" / "0000.wav"
    _write_wav(seg_path)

    def other_writers():
        append_marker(rec.id, tmp_path)
        record_completed_wav_segment(rec.id, seg_path, rec.started_at, finalized=True, data_dir=tmp_path)
        rm.append_rejoin(rec.id, RejoinAttempt(datetime.now(timezone.utc), 4006, 1), tmp_path)

    _join([threading.Thread(target=other_writers)])
    rec.status = "completed"
    rec.ended_at = datetime.now(timezone.utc)
    save_recording(rec, tmp_path)

    r = load_recordings(tmp_path)[rec.id]
    assert len(r.markers) == 1 and len(r.segment_manifest) == 1 and len(r.rejoin_log) == 1
    assert r.status == "completed"


def test_concurrent_appends_are_all_kept(tmp_path):
    """Markers wait out contention and are all kept. A segment row the hot
    path drops after its short busy timeout is restored from the file on
    disk by startup reconcile, so every segment ends up recorded either way."""
    rec = _make_recording(tmp_path)
    for i in range(15):
        _write_wav(rm.segment_path_for(rec.id, i, tmp_path))

    def _append(i):
        rm.record_completed_wav_segment(rec.id, rm.segment_path_for(rec.id, i, tmp_path),
                                        datetime.now(timezone.utc), finalized=True,
                                        data_dir=tmp_path)
        append_marker(rec.id, tmp_path)

    _join([threading.Thread(target=_append, args=(i,)) for i in range(15)])
    assert len(load_recordings(tmp_path)[rec.id].markers) == 15
    rm.reconcile_on_startup(tmp_path)
    assert len(load_recordings(tmp_path)[rec.id].segment_manifest) == 15


# ---------------------------------------------------------------------------
# Capture hot path
# ---------------------------------------------------------------------------

def test_record_completed_wav_segment_appends_and_returns_new_started_at(tmp_path):
    rec = _make_recording(tmp_path)
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "recordings" / rec.id / "combined" / "0000.wav"
    _write_wav(path)

    new_started_at = record_completed_wav_segment(rec.id, path, started_at, finalized=True,
                                                  data_dir=tmp_path)

    [seg] = load_recordings(tmp_path)[rec.id].segment_manifest
    assert (seg.index, seg.stream, seg.started_at, seg.path, seg.finalized) == (
        0, "mixed", started_at, path, True)
    assert seg.duration_s > 0
    assert new_started_at > started_at


def test_record_completed_wav_segment_parses_index_from_filename(tmp_path):
    rec = _make_recording(tmp_path)
    path = tmp_path / "recordings" / rec.id / "combined" / "0042.wav"
    _write_wav(path)
    record_completed_wav_segment(rec.id, path, datetime.now(timezone.utc), finalized=False,
                                 data_dir=tmp_path)
    [seg] = load_recordings(tmp_path)[rec.id].segment_manifest
    assert seg.index == 42 and seg.finalized is False


def test_record_completed_wav_segment_skips_zero_frame_and_unreadable(tmp_path):
    rec = _make_recording(tmp_path)
    empty = tmp_path / "recordings" / rec.id / "combined" / "0000.wav"
    _write_wav(empty, n_frames=0)
    junk = tmp_path / "recordings" / rec.id / "combined" / "0001.wav"
    junk.write_bytes(b"not a wav")
    for p in (empty, junk):
        record_completed_wav_segment(rec.id, p, datetime.now(timezone.utc), finalized=True,
                                     data_dir=tmp_path)
    assert load_recordings(tmp_path)[rec.id].segment_manifest == []


def test_record_completed_wav_segment_swallows_errors_for_unknown_recording(tmp_path):
    path = tmp_path / "0000.wav"
    _write_wav(path)
    result = record_completed_wav_segment("00000000-0000-4000-8000-000000000000", path,
                                          datetime.now(timezone.utc), finalized=True, data_dir=tmp_path)
    assert isinstance(result, datetime)


def test_hot_path_gives_up_quickly_when_the_database_is_busy(tmp_path):
    """A writer holding the lock must not stall capture for the full 5 s."""
    import time

    rec = _make_recording(tmp_path)
    path = tmp_path / "recordings" / rec.id / "combined" / "0000.wav"
    _write_wav(path)
    holder = db.connect(tmp_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        record_completed_wav_segment(rec.id, path, datetime.now(timezone.utc), finalized=True,
                                     data_dir=tmp_path)
        assert time.monotonic() - t0 < 2.5
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert load_recordings(tmp_path)[rec.id].segment_manifest == []
    reconcile_on_startup(tmp_path)                   # restored from the file on disk
    [seg] = load_recordings(tmp_path)[rec.id].segment_manifest
    assert seg.index == 0 and seg.duration_s == pytest.approx(320 / 16000)


# ---------------------------------------------------------------------------
# Startup reconcile
# ---------------------------------------------------------------------------

def test_reconcile_on_startup_marks_active_sessions_failed(tmp_path):
    active = _make_recording(tmp_path)
    degraded = _make_recording(tmp_path)
    update_recording_status(degraded.id, "degraded", tmp_path)
    done = _make_recording(tmp_path)
    update_recording_status(done.id, "completed", tmp_path, ended_at=datetime.now(timezone.utc))

    reconcile_on_startup(tmp_path)

    loaded = load_recordings(tmp_path)
    assert loaded[active.id].status == "failed" and loaded[active.id].ended_at is not None
    assert loaded[degraded.id].status == "failed"
    assert loaded[done.id].status == "completed"


# ---------------------------------------------------------------------------
# Recording ID validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ["../etc", "a/b", "", "\x00", "..", "id with spaces"])
def test_validate_recording_id_rejects_traversal_payloads(payload):
    assert _validate_recording_id(payload) is None


@pytest.mark.parametrize("valid_id", ["550e8400-e29b-41d4-a716-446655440000", "abc-123"])
def test_validate_recording_id_accepts_valid_ids(valid_id):
    assert _validate_recording_id(valid_id) == valid_id


def test_timestamps_round_trip_with_microseconds():
    dt = datetime(2026, 9, 30, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-4)))
    assert rm._dt(rm._ts(dt)) == dt
    assert rm._ts(dt).endswith("Z")


# ---------------------------------------------------------------------------
# Recover crashed sessions
# ---------------------------------------------------------------------------

def _crashed_session(tmp_path, n_segments=2):
    rec = _make_recording(tmp_path)
    for i in range(n_segments):
        _write_wav(tmp_path / "recordings" / rec.id / "combined" / f"{i:04d}.wav", n_frames=1600)
    reconcile_on_startup(tmp_path)                   # the restart after the crash
    return rec


def test_crashed_session_with_segments_is_recoverable(tmp_path):
    rec = _crashed_session(tmp_path)
    r = load_recordings(tmp_path)[rec.id]
    assert r.status == "failed" and r.recoverable is True and r.combined_path is None

    recovered = rm.recover_recording(rec.id, tmp_path)

    assert recovered.status == "completed" and recovered.recovered_at is not None
    assert recovered.combined_path is not None and recovered.recoverable is False
    with wave.open(str(recovered.combined_path), "rb") as wf:
        assert wf.getnframes() == 3200


def test_crashed_session_without_segments_is_not_recoverable(tmp_path):
    rec = _crashed_session(tmp_path, n_segments=0)
    assert load_recordings(tmp_path)[rec.id].recoverable is False
    with pytest.raises(ValueError):
        rm.recover_recording(rec.id, tmp_path)


def test_recover_refuses_an_active_session(tmp_path):
    rec = _make_recording(tmp_path)
    _write_wav(tmp_path / "recordings" / rec.id / "combined" / "0000.wav")
    with pytest.raises(ValueError, match="still recording"):
        rm.recover_recording(rec.id, tmp_path)


def test_recover_unknown_recording(tmp_path):
    with pytest.raises(KeyError):
        rm.recover_recording("00000000-0000-4000-8000-000000000000", tmp_path)


# ---------------------------------------------------------------------------
# Targeted writers used by capture code
# ---------------------------------------------------------------------------

def test_bind_recording_speaker_never_unbinds(tmp_path):
    from ._seed import seed_profile
    seed_profile("alice", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rm.bind_recording_speaker(rec.id, "111", "", tmp_path)
    assert load_recordings(tmp_path)[rec.id].unbound_speakers == ["111"]
    rm.bind_recording_speaker(rec.id, "111", "alice", tmp_path)
    rm.bind_recording_speaker(rec.id, "111", "", tmp_path)  # rejoin, unresolved
    r = load_recordings(tmp_path)[rec.id]
    assert r.discord_speakers == {"111": "alice"} and r.unbound_speakers == []
    rm.bind_recording_speaker(rec.id, "not-a-number", "alice", tmp_path)
    assert set(load_recordings(tmp_path)[rec.id].discord_speakers) == {"111"}


def test_capture_writes_keep_edits_made_during_recording(tmp_path):
    """The capture object is long-lived; its writes must not revert a name or
    notes edited in the UI mid-session."""
    rec = _make_recording(tmp_path, name="Session")
    edited = load_recordings(tmp_path)[rec.id]
    edited.notes = "the party split up"
    edited.name = "Session 12"
    save_recording(edited, tmp_path)
    rm.bind_recording_speaker(rec.id, "111", "", tmp_path)
    rm.append_rejoin(rec.id, RejoinAttempt(timestamp=datetime.now(timezone.utc), close_code=4000,
                                           attempt_number=1), tmp_path)
    rm.update_recording_status(rec.id, "completed", tmp_path, ended_at=datetime.now(timezone.utc))
    r = load_recordings(tmp_path)[rec.id]
    assert (r.name, r.notes, r.status) == ("Session 12", "the party split up", "completed")


def test_capture_code_uses_targeted_writers():
    src = Path(__file__).parent.parent / "src" / "wisper_transcribe" / "web"
    offenders = [f.name for f in (src / "discord_bot.py", src / "local_capture.py", src / "jobs.py")
                 if "save_recording(" in f.read_text(encoding="utf-8")]
    assert offenders == []


# ---------------------------------------------------------------------------
# Trim a recording to combined.wav
# ---------------------------------------------------------------------------

def _write_segments(rec_dir: Path, frames_list, *, rows_for=None, combined=True):
    """``combined/NNNN.wav`` files with real headers plus a matching ``combined.wav``.

    ``rows_for`` is the recording id to add segment rows for (the capture path).
    """
    for i, n in enumerate(frames_list):
        seg = rec_dir / "combined" / f"{i:04d}.wav"
        _write_wav(seg, n_frames=n)
        if rows_for and n > 0:
            record_completed_wav_segment(rows_for, seg, datetime.now(timezone.utc),
                                         finalized=True, data_dir=rec_dir.parent.parent)
    if combined:
        _write_wav(rec_dir / "combined.wav", n_frames=sum(frames_list))


def _add_track(tmp_path, rec_id, track, kind_label=None):
    from wisper_transcribe import file_registry
    from ._seed import seed_file

    d = tmp_path / "recordings" / rec_id / "per-user" / track
    _write_wav(d / "0000.wav", n_frames=800)
    seed_file(d, "per_user", file_registry.Owner("recording", rec_id), track, data_dir=tmp_path)
    return d


def _per_user_labels(tmp_path, rec_id):
    from wisper_transcribe import file_registry

    return sorted(r.label for r in file_registry.files_for(
        file_registry.Owner("recording", rec_id), data_dir=tmp_path) if r.kind == "per_user")


def _snapshot(rec_dir: Path):
    return sorted(str(p.relative_to(rec_dir)) for p in rec_dir.rglob("*"))


def test_trim_without_combined_wav_changes_nothing(tmp_path):
    rec = _make_recording(tmp_path, source="local")
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400, 400], rows_for=rec.id, combined=False)
    _add_track(tmp_path, rec.id, "mic")
    before = _snapshot(rec_dir)
    assert rm.trim_recording_audio(rec.id, tmp_path) == 0
    assert _snapshot(rec_dir) == before


def test_trim_with_an_empty_combined_wav_changes_nothing(tmp_path):
    rec = _make_recording(tmp_path, source="local")
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400], rows_for=rec.id, combined=False)
    _write_wav(rec_dir / "combined.wav", n_frames=0)
    _add_track(tmp_path, rec.id, "mic")
    before = _snapshot(rec_dir)
    assert rm.trim_recording_audio(rec.id, tmp_path) == 0
    assert _snapshot(rec_dir) == before


def test_trim_with_a_fake_combined_wav_changes_nothing(tmp_path):
    rec = _make_recording(tmp_path, source="local")
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400], rows_for=rec.id, combined=False)
    (rec_dir / "combined.wav").write_bytes(b"fake")
    _add_track(tmp_path, rec.id, "mic")
    before = _snapshot(rec_dir)
    assert rm.trim_recording_audio(rec.id, tmp_path) == 0
    assert _snapshot(rec_dir) == before
    assert (rec_dir / "combined.wav").read_bytes() == b"fake"


def test_trim_with_a_combined_wav_one_frame_short_changes_nothing(tmp_path):
    rec = _make_recording(tmp_path, source="local")
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400, 400], rows_for=rec.id, combined=False)
    _write_wav(rec_dir / "combined.wav", n_frames=799)
    _add_track(tmp_path, rec.id, "mic")
    before = _snapshot(rec_dir)
    assert rm.trim_recording_audio(rec.id, tmp_path) == 0
    assert _snapshot(rec_dir) == before
    assert load_recordings(tmp_path)[rec.id].combined_path is not None


def test_trim_with_fewer_readable_segments_than_rows_changes_nothing(tmp_path):
    """A segment file lost after its row was written: combined.wav may be
    missing its audio, so nothing is deleted."""
    rec = _make_recording(tmp_path, source="local")
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400, 400, 400], rows_for=rec.id)
    (rec_dir / "combined" / "0001.wav").write_bytes(b"corrupt")
    _write_wav(rec_dir / "combined.wav", n_frames=800)  # matches the two readable files
    before = _snapshot(rec_dir)
    assert rm.trim_recording_audio(rec.id, tmp_path) == 0
    assert _snapshot(rec_dir) == before


def test_trim_unknown_or_invalid_id_changes_nothing(tmp_path):
    assert rm.trim_recording_audio("../escape", tmp_path) == 0
    assert rm.trim_recording_audio("00000000-0000-0000-0000-000000000000", tmp_path) == 0


def test_trim_local_recording_loses_segments_and_per_user(tmp_path):
    rec = _make_recording(tmp_path, source="local")
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400, 400, 400], rows_for=rec.id)
    _add_track(tmp_path, rec.id, "mic")
    _add_track(tmp_path, rec.id, "system")
    (rec_dir / "live_transcript.md").write_text("draft", encoding="utf-8")
    combined_bytes = (rec_dir / "combined.wav").read_bytes()
    expected = sum(p.stat().st_size for p in (rec_dir / "combined").glob("*.wav")) + \
        2 * (rec_dir / "per-user" / "mic" / "0000.wav").stat().st_size

    freed = rm.trim_recording_audio(rec.id, tmp_path)

    assert freed == expected
    assert not (rec_dir / "combined").exists() and not (rec_dir / "per-user").exists()
    assert (rec_dir / "combined.wav").read_bytes() == combined_bytes
    assert (rec_dir / "live_transcript.md").exists()
    assert not [p for p in rec_dir.iterdir() if p.name.startswith(".wisper-trash-")]
    assert _per_user_labels(tmp_path, rec.id) == []
    assert len(load_recordings(tmp_path)[rec.id].segment_manifest) == 3  # rows stay
    assert rm.trim_recording_audio(rec.id, tmp_path) == 0  # idempotent


def test_trim_discord_recording_keeps_unbound_and_drops_bound(tmp_path):
    seed_profile("alice", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400, 400], rows_for=rec.id)
    for uid in ("111111111111111111", "222222222222222222"):
        _add_track(tmp_path, rec.id, uid)
    rm.bind_recording_speaker(rec.id, "111111111111111111", "alice", tmp_path)
    rm.bind_recording_speaker(rec.id, "222222222222222222", "", tmp_path)

    freed = rm.trim_recording_audio(rec.id, tmp_path)

    assert freed > 0
    assert not (rec_dir / "combined").exists()
    assert not (rec_dir / "per-user" / "111111111111111111").exists()
    assert (rec_dir / "per-user" / "222222222222222222" / "0000.wav").exists()
    assert (rec_dir / "combined.wav").exists()
    assert _per_user_labels(tmp_path, rec.id) == ["222222222222222222"]


def test_trim_keeps_a_directory_whose_rename_fails(tmp_path, monkeypatch):
    seed_profile("alice", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400], rows_for=rec.id)
    _add_track(tmp_path, rec.id, "111111111111111111")
    rm.bind_recording_speaker(rec.id, "111111111111111111", "alice", tmp_path)

    with monkeypatch.context() as m:
        m.setattr("wisper_transcribe.transcript_store._replace", lambda src, dst: False)
        assert rm.trim_recording_audio(rec.id, tmp_path) == 0

    assert (rec_dir / "combined").is_dir()
    assert (rec_dir / "per-user" / "111111111111111111").is_dir()
    assert _per_user_labels(tmp_path, rec.id) == ["111111111111111111"]
    assert (rec_dir / "combined.wav").exists()


def test_trim_forgets_per_user_rows_whose_directory_is_gone(tmp_path):
    rec = _make_recording(tmp_path)
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400], rows_for=rec.id)
    gone = _add_track(tmp_path, rec.id, "111111111111111111")
    kept = _add_track(tmp_path, rec.id, "222222222222222222")
    import shutil
    shutil.rmtree(gone)  # a crash between the rename and the forget
    rm.bind_recording_speaker(rec.id, "222222222222222222", "", tmp_path)

    rm.trim_recording_audio(rec.id, tmp_path)

    assert kept.is_dir()
    assert _per_user_labels(tmp_path, rec.id) == ["222222222222222222"]


def test_recover_recording_trims_after_rebuilding(tmp_path):
    rec = _crashed_session(tmp_path)
    rec_dir = tmp_path / "recordings" / rec.id

    recovered = rm.recover_recording(rec.id, tmp_path)

    assert recovered.combined_path is not None and recovered.combined_path.exists()
    assert not (rec_dir / "combined").exists()


def test_bind_after_trim_deletes_that_users_track(tmp_path):
    """combined/ is gone after the first trim, so only combined.wav is checked."""
    seed_profile("alice", data_dir=tmp_path)
    seed_profile("bob", data_dir=tmp_path)
    rec = _make_recording(tmp_path)
    rec_dir = tmp_path / "recordings" / rec.id
    _write_segments(rec_dir, [400, 400], rows_for=rec.id)
    uids = ("111111111111111111", "222222222222222222", "333333333333333333")
    for uid in uids:
        _add_track(tmp_path, rec.id, uid)
    rm.bind_recording_speaker(rec.id, uids[0], "alice", tmp_path)
    rm.bind_recording_speaker(rec.id, uids[1], "", tmp_path)
    rm.bind_recording_speaker(rec.id, uids[2], "", tmp_path)
    rm.trim_recording_audio(rec.id, tmp_path)
    assert not (rec_dir / "combined").exists()
    assert _per_user_labels(tmp_path, rec.id) == [uids[1], uids[2]]

    rm.bind_recording_speaker(rec.id, uids[1], "bob", tmp_path)
    assert rm.trim_recording_audio(rec.id, tmp_path) > 0

    assert not (rec_dir / "per-user" / uids[1]).exists()
    assert (rec_dir / "per-user" / uids[2]).is_dir()
    assert _per_user_labels(tmp_path, rec.id) == [uids[2]]
    assert (rec_dir / "combined.wav").exists()


def _transcribed_in_campaign(tmp_path, monkeypatch, slug="dnd"):
    from wisper_transcribe import campaign_manager as cm
    from wisper_transcribe.path_utils import get_output_dir

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    for name in ("dnd", "other"):
        cm.create_campaign(name, tmp_path)
    rec = _make_recording(tmp_path, campaign_slug=slug)
    update_recording_status(rec.id, "completed", tmp_path)
    md = get_output_dir() / "s1.md"
    md.write_text("x", encoding="utf-8")
    link_transcript(rec.id, md, tmp_path)
    return rec, md


def _stored_campaign_id(rec_id, tmp_path):
    with db.connection(tmp_path) as conn:
        return conn.execute("SELECT campaign_id FROM recordings WHERE id = ?",
                            (rec_id,)).fetchone()[0]


def test_transcribed_recording_campaign_follows_its_transcript(tmp_path, monkeypatch):
    from wisper_transcribe import campaign_manager as cm
    from wisper_transcribe import transcript_store

    rec, md = _transcribed_in_campaign(tmp_path, monkeypatch)
    stored = _stored_campaign_id(rec.id, tmp_path)
    assert rm.load_recording(rec.id, tmp_path).campaign_slug is None  # transcript in no campaign
    cm.move_transcript_to_campaign("s1", "other", tmp_path)
    assert rm.load_recording(rec.id, tmp_path).campaign_slug == "other"
    assert _stored_campaign_id(rec.id, tmp_path) == stored
    cm.remove_transcript_from_campaign("s1", tmp_path)
    assert rm.load_recording(rec.id, tmp_path).campaign_slug is None
    transcript_store.delete_transcript("s1")
    assert rm.load_recording(rec.id, tmp_path).campaign_slug == "dnd"


def test_save_recording_keeps_a_transcribed_recordings_stored_campaign(tmp_path, monkeypatch):
    from wisper_transcribe import campaign_manager as cm

    rec, md = _transcribed_in_campaign(tmp_path, monkeypatch)
    stored = _stored_campaign_id(rec.id, tmp_path)
    cm.move_transcript_to_campaign("s1", "other", tmp_path)
    loaded = rm.load_recording(rec.id, tmp_path)
    loaded.name = "renamed"
    save_recording(loaded, tmp_path)
    assert _stored_campaign_id(rec.id, tmp_path) == stored
    assert rm.load_recording(rec.id, tmp_path).campaign_slug == "other"


def test_save_recording_sets_an_untranscribed_recordings_campaign(tmp_path, monkeypatch):
    from wisper_transcribe import campaign_manager as cm

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    cm.create_campaign("dnd", tmp_path)
    rec = _make_recording(tmp_path)
    rec.campaign_slug = "dnd"
    save_recording(rec, tmp_path)
    assert rm.load_recording(rec.id, tmp_path).campaign_slug == "dnd"
