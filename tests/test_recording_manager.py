"""Tests for recording_manager.py — CRUD, validation, and reconciliation.

All tests use tmp_path for isolation (no real data_dir touched).
Mirrors test_campaign_manager.py patterns.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wisper_transcribe.models import RejoinAttempt, Recording, SegmentRecord
from wisper_transcribe.recording_manager import (
    _validate_recording_id,
    append_segment,
    create_recording,
    delete_recording,
    get_recordings_path,
    load_recordings,
    reconcile_on_startup,
    save_recordings,
    update_recording,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_segment(index: int = 0, stream: str = "mixed") -> SegmentRecord:
    return SegmentRecord(
        index=index,
        stream=stream,
        started_at=_now(),
        duration_s=60.0,
        path=f"combined/{index:04d}.opus",
        finalized=True,
    )


# ---------------------------------------------------------------------------
# load / save roundtrip
# ---------------------------------------------------------------------------


def test_load_recordings_missing_file_returns_empty(tmp_path: Path) -> None:
    result = load_recordings(tmp_path)
    assert result == {}


def test_load_save_roundtrip(tmp_path: Path) -> None:
    rec = create_recording(
        voice_channel_id="111",
        guild_id="222",
        campaign_slug="dnd-mondays",
        data_dir=tmp_path,
    )
    loaded = load_recordings(tmp_path)
    assert rec.id in loaded
    r = loaded[rec.id]
    assert r.voice_channel_id == "111"
    assert r.guild_id == "222"
    assert r.campaign_slug == "dnd-mondays"
    assert r.status == "recording"
    assert r.segment_manifest == []
    assert r.rejoin_log == []


def test_save_recordings_creates_parent_dirs(tmp_path: Path) -> None:
    deep_dir = tmp_path / "a" / "b" / "c"
    # Parent does not exist — save_recordings must create it
    rec = create_recording("111", "222", data_dir=deep_dir)
    assert get_recordings_path(deep_dir).exists()
    loaded = load_recordings(deep_dir)
    assert rec.id in loaded


# ---------------------------------------------------------------------------
# create_recording
# ---------------------------------------------------------------------------


def test_create_recording_generates_uuid(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    # uuid4 format: 8-4-4-4-12 hex chars joined by hyphens
    parts = rec.id.split("-")
    assert len(parts) == 5
    assert all(c in "0123456789abcdef-" for c in rec.id)


def test_create_recording_builds_directory_tree(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    base = tmp_path / "recordings" / rec.id
    assert (base / "combined").is_dir()
    assert (base / "per-user").is_dir()
    assert (base / "final").is_dir()


def test_create_recording_status_is_recording(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    assert rec.status == "recording"


def test_create_recording_without_campaign(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    assert rec.campaign_slug is None


def test_create_two_recordings_have_different_ids(tmp_path: Path) -> None:
    a = create_recording("111", "222", data_dir=tmp_path)
    b = create_recording("333", "444", data_dir=tmp_path)
    assert a.id != b.id


# ---------------------------------------------------------------------------
# update_recording
# ---------------------------------------------------------------------------


def test_update_recording_status(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    updated = update_recording(rec.id, data_dir=tmp_path, status="completed")
    assert updated.status == "completed"
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].status == "completed"


def test_update_recording_unknown_field_raises(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    with pytest.raises(ValueError, match="Unknown field"):
        update_recording(rec.id, data_dir=tmp_path, nonexistent_field="oops")


def test_update_recording_missing_id_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        update_recording("does-not-exist", data_dir=tmp_path, status="completed")


# ---------------------------------------------------------------------------
# delete_recording
# ---------------------------------------------------------------------------


def test_delete_recording_removes_entry(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    delete_recording(rec.id, data_dir=tmp_path)
    loaded = load_recordings(tmp_path)
    assert rec.id not in loaded


def test_delete_recording_missing_id_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        delete_recording("does-not-exist", data_dir=tmp_path)


def test_delete_recording_does_not_remove_audio_files(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    audio_file = tmp_path / "recordings" / rec.id / "combined" / "0000.opus"
    audio_file.write_bytes(b"fake audio")
    delete_recording(rec.id, data_dir=tmp_path)
    assert audio_file.exists(), "delete_recording must not remove audio files"


# ---------------------------------------------------------------------------
# _validate_recording_id — CodeQL four-step pattern
# ---------------------------------------------------------------------------


_VALID_IDS = [
    "550e8400-e29b-41d4-a716-446655440000",  # uuid4
    "abc-123",
    "test_rec_0001",
    "a",
]

_INVALID_IDS = [
    "",                        # empty
    "\x00",                    # null byte
    "some\x00name",            # embedded null byte
    "../etc/passwd",           # dotdot traversal
    "/etc/passwd",             # absolute path
    "a/b",                     # path separator
    "a\\b",                    # Windows path separator
    "invalid*name",            # regex-busting glob
    "invalid+name",            # disallowed character
    "name!@#",                 # special chars
    "id/with/slashes",         # multiple path components
    ".",                       # dot
    "..",                      # dotdot
    "\r\nLocation: evil.com",  # CRLF injection
    "javascript:alert(1)",     # open-redirect attempt
]


@pytest.mark.parametrize("valid_id", _VALID_IDS)
def test_validate_recording_id_accepts_valid(valid_id: str) -> None:
    result = _validate_recording_id(valid_id)
    assert result is not None, f"Expected {valid_id!r} to be accepted"


@pytest.mark.parametrize("bad_id", _INVALID_IDS)
def test_validate_recording_id_rejects_traversal_payloads(bad_id: str) -> None:
    result = _validate_recording_id(bad_id)
    assert result is None, f"Expected {bad_id!r} to be rejected"


# ---------------------------------------------------------------------------
# append_segment — atomic manifest updates
# ---------------------------------------------------------------------------


def test_append_segment_adds_to_manifest(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    seg = _make_segment(index=0, stream="mixed")
    append_segment(rec.id, seg, data_dir=tmp_path)
    loaded = load_recordings(tmp_path)
    assert len(loaded[rec.id].segment_manifest) == 1
    stored = loaded[rec.id].segment_manifest[0]
    assert stored.index == 0
    assert stored.stream == "mixed"
    assert stored.finalized is True


def test_append_segment_missing_id_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        append_segment("does-not-exist", _make_segment(), data_dir=tmp_path)


def test_append_segment_atomic_under_concurrent_calls(tmp_path: Path) -> None:
    """Concurrent append_segment calls must not corrupt the manifest."""
    rec = create_recording("111", "222", data_dir=tmp_path)

    n_threads = 8
    n_segs_per_thread = 5
    errors: list[Exception] = []

    def writer(thread_idx: int) -> None:
        for i in range(n_segs_per_thread):
            seg = SegmentRecord(
                index=thread_idx * n_segs_per_thread + i,
                stream=f"user_{thread_idx}",
                started_at=_now(),
                duration_s=60.0,
                path=f"per-user/user_{thread_idx}/{i:04d}.opus",
                finalized=True,
            )
            try:
                append_segment(rec.id, seg, data_dir=tmp_path)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent append raised: {errors}"
    loaded = load_recordings(tmp_path)
    manifest = loaded[rec.id].segment_manifest
    assert len(manifest) == n_threads * n_segs_per_thread


# ---------------------------------------------------------------------------
# reconcile_on_startup
# ---------------------------------------------------------------------------


def test_reconcile_on_startup_clean_completion(tmp_path: Path) -> None:
    """reconcile_on_startup must not change already-completed recordings."""
    rec = create_recording("111", "222", data_dir=tmp_path)
    update_recording(rec.id, data_dir=tmp_path, status="completed")

    n = reconcile_on_startup(tmp_path)

    assert n == 0
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].status == "completed"


def test_reconcile_on_startup_orphaned_recording_marked_failed(tmp_path: Path) -> None:
    """A recording stuck in 'recording' status must be marked 'failed'."""
    rec = create_recording("111", "222", data_dir=tmp_path)
    assert load_recordings(tmp_path)[rec.id].status == "recording"

    n = reconcile_on_startup(tmp_path)

    assert n == 1
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].status == "failed"


def test_reconcile_on_startup_degraded_marked_failed(tmp_path: Path) -> None:
    """A recording in 'degraded' status must also be marked 'failed'."""
    rec = create_recording("111", "222", data_dir=tmp_path)
    update_recording(rec.id, data_dir=tmp_path, status="degraded")

    n = reconcile_on_startup(tmp_path)

    assert n == 1
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].status == "failed"


def test_reconcile_on_startup_mixed_statuses(tmp_path: Path) -> None:
    """Only active recordings are corrected; finished ones are untouched."""
    active = create_recording("111", "222", data_dir=tmp_path)  # status=recording
    done = create_recording("333", "444", data_dir=tmp_path)
    update_recording(done.id, data_dir=tmp_path, status="transcribed")

    n = reconcile_on_startup(tmp_path)

    assert n == 1
    loaded = load_recordings(tmp_path)
    assert loaded[active.id].status == "failed"
    assert loaded[done.id].status == "transcribed"


def test_reconcile_on_startup_corrupt_recordings_json_logs_and_returns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt recordings.json must be handled gracefully: log + return 0."""
    rec_path = get_recordings_path(tmp_path)
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    rec_path.write_text("{not valid json {{{{")

    with caplog.at_level(logging.WARNING, logger="wisper_transcribe.recording_manager"):
        result = reconcile_on_startup(tmp_path)

    assert result == 0
    assert len(caplog.records) > 0


def test_reconcile_on_startup_no_file_returns_zero(tmp_path: Path) -> None:
    """reconcile_on_startup on an empty data_dir must return 0 without error."""
    result = reconcile_on_startup(tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# SegmentRecord and RejoinAttempt roundtrip through JSON
# ---------------------------------------------------------------------------


def test_segment_record_roundtrip(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    seg = SegmentRecord(
        index=7,
        stream="user_abc123",
        started_at="2026-05-05T12:00:00+00:00",
        duration_s=59.84,
        path="per-user/user_abc123/0007.opus",
        finalized=True,
    )
    append_segment(rec.id, seg, data_dir=tmp_path)

    loaded_seg = load_recordings(tmp_path)[rec.id].segment_manifest[0]
    assert loaded_seg.index == 7
    assert loaded_seg.stream == "user_abc123"
    assert loaded_seg.duration_s == pytest.approx(59.84)
    assert loaded_seg.finalized is True


def test_rejoin_attempt_roundtrip(tmp_path: Path) -> None:
    rec = create_recording("111", "222", data_dir=tmp_path)
    attempt = RejoinAttempt(
        timestamp="2026-05-05T12:01:00+00:00",
        close_code=4015,
        attempt_number=2,
    )
    update_recording(
        rec.id,
        data_dir=tmp_path,
        rejoin_log=[attempt],
    )

    loaded = load_recordings(tmp_path)[rec.id]
    assert len(loaded.rejoin_log) == 1
    r = loaded.rejoin_log[0]
    assert r.close_code == 4015
    assert r.attempt_number == 2
