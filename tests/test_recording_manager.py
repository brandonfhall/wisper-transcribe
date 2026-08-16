"""Tests for recording_manager.py — Phase 1 storage layer."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wisper_transcribe.models import Recording, SegmentRecord
from wisper_transcribe.recording_manager import (
    _validate_recording_id,
    append_marker,
    append_segment,
    create_recording,
    delete_recording,
    get_metadata_path,
    get_recordings_index_path,
    load_recordings,
    reconcile_on_startup,
    save_recording,
    update_recording_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_recording(tmp_path: Path, **kwargs) -> Recording:
    return create_recording(
        voice_channel_id=kwargs.get("voice_channel_id", "VC1"),
        guild_id=kwargs.get("guild_id", "G1"),
        campaign_slug=kwargs.get("campaign_slug", None),
        data_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# Load / save round-trip
# ---------------------------------------------------------------------------

def test_load_save_roundtrip(tmp_path):
    rec = _make_recording(tmp_path)
    loaded = load_recordings(tmp_path)
    assert rec.id in loaded
    r = loaded[rec.id]
    assert r.voice_channel_id == "VC1"
    assert r.guild_id == "G1"
    assert r.status == "recording"
    assert isinstance(r.started_at, datetime)


def test_create_recording_defaults_to_discord_source(tmp_path):
    rec = _make_recording(tmp_path)
    assert rec.source == "discord"
    assert rec.devices == {}


def test_create_recording_local_source_and_devices_roundtrip(tmp_path):
    rec = create_recording(
        voice_channel_id="",
        guild_id="",
        data_dir=tmp_path,
        source="local",
        devices={"mic": "Built-in Microphone", "system": "Speakers (loopback)"},
    )
    loaded = load_recordings(tmp_path)
    r = loaded[rec.id]
    assert r.source == "local"
    assert r.devices == {"mic": "Built-in Microphone", "system": "Speakers (loopback)"}
    assert r.voice_channel_id == ""
    assert r.guild_id == ""


def test_load_legacy_json_without_source_defaults_to_discord(tmp_path):
    """A metadata.json written before source/devices existed must still load."""
    rec = _make_recording(tmp_path)
    meta_path = get_metadata_path(rec.id, tmp_path)
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "source" in data and "devices" in data  # sanity: current writer includes them
    del data["source"]
    del data["devices"]
    meta_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_recordings(tmp_path)
    r = loaded[rec.id]
    assert r.source == "discord"
    assert r.devices == {}


def test_create_recording_generates_uuid(tmp_path):
    r1 = _make_recording(tmp_path)
    r2 = _make_recording(tmp_path)
    assert r1.id != r2.id
    assert len(r1.id) == 36   # uuid4 with dashes


def test_create_recording_name_roundtrips(tmp_path):
    rec = create_recording(
        voice_channel_id="", guild_id="", data_dir=tmp_path, source="local",
        name="Session 14 — the ambush",
    )
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].name == "Session 14 — the ambush"


def test_create_recording_defaults_name_to_none(tmp_path):
    rec = _make_recording(tmp_path)
    assert rec.name is None
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].name is None


def test_load_legacy_json_without_name_defaults_to_none(tmp_path):
    """A metadata.json written before `name` existed must still load."""
    rec = _make_recording(tmp_path)
    meta_path = get_metadata_path(rec.id, tmp_path)
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "name" in data  # sanity: current writer includes it
    del data["name"]
    meta_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].name is None


# ---------------------------------------------------------------------------
# Markers ("Add marker" button on /record)
# ---------------------------------------------------------------------------

def test_append_marker_roundtrips(tmp_path):
    rec = _make_recording(tmp_path)
    marker = append_marker(rec.id, tmp_path)

    assert marker.elapsed_s >= 0.0
    loaded = load_recordings(tmp_path)
    assert len(loaded[rec.id].markers) == 1
    assert loaded[rec.id].markers[0].elapsed_s == marker.elapsed_s


def test_append_marker_computes_elapsed_since_started_at(tmp_path):
    from datetime import timedelta

    rec = _make_recording(tmp_path)
    rec.started_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    save_recording(rec, tmp_path)

    marker = append_marker(rec.id, tmp_path)
    assert 89.0 <= marker.elapsed_s <= 91.0


def test_append_marker_multiple_calls_accumulate(tmp_path):
    rec = _make_recording(tmp_path)
    append_marker(rec.id, tmp_path)
    append_marker(rec.id, tmp_path)
    append_marker(rec.id, tmp_path)

    loaded = load_recordings(tmp_path)
    assert len(loaded[rec.id].markers) == 3


def test_append_marker_unknown_id_raises(tmp_path):
    with pytest.raises(KeyError):
        append_marker("does-not-exist", tmp_path)


def test_append_marker_atomic_under_concurrent_calls(tmp_path):
    rec = _make_recording(tmp_path)
    n = 20

    threads = [threading.Thread(target=append_marker, args=(rec.id, tmp_path)) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = load_recordings(tmp_path)
    assert len(loaded[rec.id].markers) == n


def test_create_recording_defaults_markers_to_empty_list(tmp_path):
    rec = _make_recording(tmp_path)
    assert rec.markers == []


def test_load_legacy_json_without_markers_defaults_to_empty_list(tmp_path):
    """A metadata.json written before `markers` existed must still load."""
    rec = _make_recording(tmp_path)
    meta_path = get_metadata_path(rec.id, tmp_path)
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "markers" in data  # sanity: current writer includes it
    del data["markers"]
    meta_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].markers == []


def test_load_returns_empty_when_no_file(tmp_path):
    assert load_recordings(tmp_path) == {}


def test_load_returns_empty_on_corrupt_index(tmp_path):
    idx = get_recordings_index_path(tmp_path)
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text("NOT JSON", encoding="utf-8")
    assert load_recordings(tmp_path) == {}


def test_load_skips_recording_with_missing_metadata(tmp_path):
    rec = _make_recording(tmp_path)
    # Remove the metadata file but keep the index entry
    get_metadata_path(rec.id, tmp_path).unlink()
    loaded = load_recordings(tmp_path)
    assert rec.id not in loaded


def test_update_recording_status(tmp_path):
    rec = _make_recording(tmp_path)
    update_recording_status(rec.id, "completed", tmp_path,
                            ended_at=datetime.now(timezone.utc))
    loaded = load_recordings(tmp_path)
    assert loaded[rec.id].status == "completed"
    assert loaded[rec.id].ended_at is not None


def test_update_recording_status_raises_for_unknown(tmp_path):
    with pytest.raises(KeyError):
        update_recording_status("no-such-id", "failed", tmp_path)


def test_delete_recording_removes_from_index(tmp_path):
    rec = _make_recording(tmp_path)
    delete_recording(rec.id, tmp_path)
    assert rec.id not in load_recordings(tmp_path)


# ---------------------------------------------------------------------------
# Segment manifest
# ---------------------------------------------------------------------------

def test_append_segment_atomic_under_concurrent_calls(tmp_path):
    rec = _make_recording(tmp_path)
    n = 20

    def _append(i):
        seg = SegmentRecord(
            index=i,
            stream="mixed",
            started_at=datetime.now(timezone.utc),
            duration_s=60.0,
            path=Path(f"/tmp/fake/{i:04d}.opus"),
            finalized=True,
        )
        append_segment(rec.id, seg, tmp_path)

    threads = [threading.Thread(target=_append, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = load_recordings(tmp_path)
    assert len(loaded[rec.id].segment_manifest) == n


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def test_reconcile_on_startup_clean_completion(tmp_path):
    rec = _make_recording(tmp_path)
    update_recording_status(rec.id, "completed", tmp_path)
    reconcile_on_startup(tmp_path)
    # Completed recordings are left alone
    assert load_recordings(tmp_path)[rec.id].status == "completed"


def test_reconcile_on_startup_orphaned_segment_marked_failed(tmp_path):
    rec = _make_recording(tmp_path)
    assert load_recordings(tmp_path)[rec.id].status == "recording"
    reconcile_on_startup(tmp_path)
    assert load_recordings(tmp_path)[rec.id].status == "failed"


def test_reconcile_on_startup_degraded_marked_failed(tmp_path):
    rec = _make_recording(tmp_path)
    update_recording_status(rec.id, "degraded", tmp_path)
    reconcile_on_startup(tmp_path)
    assert load_recordings(tmp_path)[rec.id].status == "failed"


def test_reconcile_on_startup_corrupt_recordings_json_logs_and_returns(tmp_path, caplog):
    idx = get_recordings_index_path(tmp_path)
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text("{bad json", encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING, logger="wisper_transcribe.recording_manager"):
        reconcile_on_startup(tmp_path)   # must not raise


# ---------------------------------------------------------------------------
# _validate_recording_id — security (CodeQL Pattern 2)
# ---------------------------------------------------------------------------

_TRAVERSAL_PAYLOADS = [
    "\x00",
    "some\x00name",
    "../evil",
    "../../etc/passwd",
    "invalid*name",
    "invalid+name",
    "id/with/slashes",
    "",
    ".",
    "..",
]

_VALID_IDS = [
    "550e8400-e29b-41d4-a716-446655440000",
    "abc123",
    "my-recording-01",
]


@pytest.mark.parametrize("payload", _TRAVERSAL_PAYLOADS)
def test_validate_recording_id_rejects_traversal_payloads(payload):
    assert _validate_recording_id(payload) is None


@pytest.mark.parametrize("valid_id", _VALID_IDS)
def test_validate_recording_id_accepts_valid_ids(valid_id):
    result = _validate_recording_id(valid_id)
    assert result is not None
    assert result == valid_id
