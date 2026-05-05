"""Recording manager — CRUD + index for Discord voice recordings.

Data lives at:
    $DATA_DIR/recordings/recordings.json     — index (id → summary)
    $DATA_DIR/recordings/<id>/metadata.json  — full Recording + segment manifest

Mirrors the pattern in campaign_manager.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import get_data_dir
from .models import Recording, RejoinAttempt, SegmentRecord

log = logging.getLogger(__name__)

_DT_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"

# Per-recording mutex: ensures load → modify → save is atomic across threads.
_recording_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _get_recording_lock(recording_id: str) -> threading.Lock:
    with _registry_lock:
        if recording_id not in _recording_locks:
            _recording_locks[recording_id] = threading.Lock()
        return _recording_locks[recording_id]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_recordings_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "recordings"


def get_recordings_index_path(data_dir: Optional[Path] = None) -> Path:
    return get_recordings_dir(data_dir) / "recordings.json"


def get_recording_dir(recording_id: str, data_dir: Optional[Path] = None) -> Path:
    return get_recordings_dir(data_dir) / recording_id


def get_metadata_path(recording_id: str, data_dir: Optional[Path] = None) -> Path:
    return get_recording_dir(recording_id, data_dir) / "metadata.json"


# ---------------------------------------------------------------------------
# Security: recording ID validation (CodeQL Pattern 2)
# ---------------------------------------------------------------------------

def _validate_recording_id(recording_id: str) -> Optional[str]:
    """Four-step CodeQL-safe guard for recording IDs used in file paths and redirects.

    Returns the sanitised ID on success, None on rejection.
    """
    if not recording_id or "\x00" in recording_id:
        return None

    safe = os.path.basename(recording_id)
    if safe != recording_id or safe in {".", ".."}:
        return None

    if not re.match(r"^[\w\-]+$", safe):
        return None

    _guard_base = os.path.abspath("_recordings_guard")
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe))
    if not _guard_path.startswith(_guard_base):
        return None

    return os.path.basename(_guard_path)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_DT_FMT) if dt else None


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, _DT_FMT)
    except ValueError:
        return datetime.fromisoformat(s)


def _segment_to_dict(seg: SegmentRecord) -> dict:
    return {
        "index": seg.index,
        "stream": seg.stream,
        "started_at": _dt_to_str(seg.started_at),
        "duration_s": seg.duration_s,
        "path": str(seg.path),
        "finalized": seg.finalized,
    }


def _segment_from_dict(d: dict) -> SegmentRecord:
    return SegmentRecord(
        index=d["index"],
        stream=d["stream"],
        started_at=_str_to_dt(d["started_at"]),
        duration_s=d.get("duration_s", 0.0),
        path=Path(d["path"]),
        finalized=d.get("finalized", False),
    )


def _rejoin_to_dict(r: RejoinAttempt) -> dict:
    return {
        "timestamp": _dt_to_str(r.timestamp),
        "close_code": r.close_code,
        "attempt_number": r.attempt_number,
    }


def _rejoin_from_dict(d: dict) -> RejoinAttempt:
    return RejoinAttempt(
        timestamp=_str_to_dt(d["timestamp"]),
        close_code=d["close_code"],
        attempt_number=d["attempt_number"],
    )


def _recording_to_dict(r: Recording) -> dict:
    return {
        "id": r.id,
        "campaign_slug": r.campaign_slug,
        "started_at": _dt_to_str(r.started_at),
        "ended_at": _dt_to_str(r.ended_at),
        "status": r.status,
        "voice_channel_id": r.voice_channel_id,
        "guild_id": r.guild_id,
        "discord_speakers": dict(r.discord_speakers),
        "segment_manifest": [_segment_to_dict(s) for s in r.segment_manifest],
        "combined_path": str(r.combined_path) if r.combined_path else None,
        "per_user_dir": str(r.per_user_dir) if r.per_user_dir else None,
        "transcript_path": str(r.transcript_path) if r.transcript_path else None,
        "rejoin_log": [_rejoin_to_dict(rj) for rj in r.rejoin_log],
        "notes": r.notes,
        "unbound_speakers": list(r.unbound_speakers),
    }


def _recording_from_dict(d: dict) -> Recording:
    return Recording(
        id=d["id"],
        campaign_slug=d.get("campaign_slug"),
        started_at=_str_to_dt(d["started_at"]),
        ended_at=_str_to_dt(d.get("ended_at")),
        status=d.get("status", "failed"),
        voice_channel_id=d.get("voice_channel_id", ""),
        guild_id=d.get("guild_id", ""),
        discord_speakers=dict(d.get("discord_speakers", {})),
        segment_manifest=[_segment_from_dict(s) for s in d.get("segment_manifest", [])],
        combined_path=Path(d["combined_path"]) if d.get("combined_path") else None,
        per_user_dir=Path(d["per_user_dir"]) if d.get("per_user_dir") else None,
        transcript_path=Path(d["transcript_path"]) if d.get("transcript_path") else None,
        rejoin_log=[_rejoin_from_dict(r) for r in d.get("rejoin_log", [])],
        notes=d.get("notes"),
        unbound_speakers=list(d.get("unbound_speakers", [])),
    )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_recordings(data_dir: Optional[Path] = None) -> dict[str, Recording]:
    """Load all recordings from the index + per-recording metadata.json files."""
    index_path = get_recordings_index_path(data_dir)
    if not index_path.exists():
        return {}

    try:
        with open(index_path, encoding="utf-8") as f:
            index: dict = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("recordings.json unreadable (%s); returning empty index", exc)
        return {}

    recordings: dict[str, Recording] = {}
    for recording_id in index:
        meta_path = get_metadata_path(recording_id, data_dir)
        if not meta_path.exists():
            log.warning("metadata.json missing for recording %s; skipping", recording_id)
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                recordings[recording_id] = _recording_from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            log.warning("Failed to load recording %s (%s); skipping", recording_id, exc)
    return recordings


def save_recording(recording: Recording, data_dir: Optional[Path] = None) -> None:
    """Persist one recording's metadata.json and update the index atomically."""
    rec_dir = get_recording_dir(recording.id, data_dir)
    rec_dir.mkdir(parents=True, exist_ok=True)

    meta_path = get_metadata_path(recording.id, data_dir)
    # Use NamedTemporaryFile in the same directory so os.replace is atomic
    # (same filesystem). Unique tmp name avoids concurrent-thread collisions.
    with tempfile.NamedTemporaryFile(
        mode="w", dir=rec_dir, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(_recording_to_dict(recording), tf, indent=2)
        tmp_name = tf.name
    Path(tmp_name).replace(meta_path)

    _update_index(recording.id, data_dir)


def _update_index(recording_id: str, data_dir: Optional[Path] = None) -> None:
    index_path = get_recordings_index_path(data_dir)
    index_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        index = {}

    index[recording_id] = True
    with tempfile.NamedTemporaryFile(
        mode="w", dir=index_path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(index, tf, indent=2)
        tmp_name = tf.name
    Path(tmp_name).replace(index_path)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_recording(
    voice_channel_id: str,
    guild_id: str,
    campaign_slug: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Recording:
    """Create and persist a new Recording in 'recording' status."""
    recording_id = str(uuid.uuid4())
    recordings_dir = get_recordings_dir(data_dir)
    rec_dir = recordings_dir / recording_id

    recording = Recording(
        id=recording_id,
        campaign_slug=campaign_slug,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        status="recording",
        voice_channel_id=voice_channel_id,
        guild_id=guild_id,
        discord_speakers={},
        segment_manifest=[],
        combined_path=None,
        per_user_dir=rec_dir / "per-user",
        transcript_path=None,
        rejoin_log=[],
    )
    save_recording(recording, data_dir)
    return recording


def update_recording_status(
    recording_id: str,
    status: str,
    data_dir: Optional[Path] = None,
    ended_at: Optional[datetime] = None,
) -> None:
    recordings = load_recordings(data_dir)
    if recording_id not in recordings:
        raise KeyError(f"Recording {recording_id!r} not found")
    rec = recordings[recording_id]
    rec.status = status
    if ended_at:
        rec.ended_at = ended_at
    save_recording(rec, data_dir)


def append_segment(
    recording_id: str,
    segment: SegmentRecord,
    data_dir: Optional[Path] = None,
) -> None:
    """Atomically append a segment record to the manifest.

    Per-recording mutex prevents lost-update races when multiple threads
    append concurrently (e.g. mixed + per-user writers running in parallel).
    """
    with _get_recording_lock(recording_id):
        recordings = load_recordings(data_dir)
        if recording_id not in recordings:
            raise KeyError(f"Recording {recording_id!r} not found")
        recordings[recording_id].segment_manifest.append(segment)
        save_recording(recordings[recording_id], data_dir)


def delete_recording(recording_id: str, data_dir: Optional[Path] = None) -> None:
    """Remove recording from index. Does NOT delete audio files."""
    index_path = get_recordings_index_path(data_dir)
    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    index.pop(recording_id, None)
    tmp = index_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    tmp.replace(index_path)


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def reconcile_on_startup(data_dir: Optional[Path] = None) -> None:
    """On server start, mark any 'recording'/'degraded' recordings as 'failed'.

    A recording in an active state after a restart means the server crashed
    mid-session. The audio segments on disk are preserved; only the status
    is updated so the UI surfaces them correctly.
    """
    try:
        recordings = load_recordings(data_dir)
    except Exception as exc:
        log.warning("reconcile_on_startup: could not load recordings (%s)", exc)
        return

    for rec in recordings.values():
        if rec.status in {"recording", "degraded"}:
            log.warning(
                "Recording %s was in status %r at startup — marking failed (crash recovery)",
                rec.id, rec.status,
            )
            rec.status = "failed"
            rec.ended_at = datetime.now(timezone.utc)
            try:
                save_recording(rec, data_dir)
            except Exception as exc:
                log.error("Failed to save crash-recovered recording %s: %s", rec.id, exc)
