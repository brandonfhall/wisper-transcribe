"""Recording manager — CRUD for Discord session recordings.

Each recording captures per-user and combined Opus-in-Ogg audio segments from
a Discord voice channel session.  Metadata is stored in a flat JSON index;
audio lives in per-recording subdirectories under data_dir/recordings/.

Data lives at:
    $DATA_DIR/recordings/recordings.json
    $DATA_DIR/recordings/<recording_id>/metadata.json  (redundant sidecar, future)
    $DATA_DIR/recordings/<recording_id>/combined/      segmented combined track
    $DATA_DIR/recordings/<recording_id>/per-user/      per-speaker tracks
    $DATA_DIR/recordings/<recording_id>/final/         finalized combined.wav + transcript
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .config import get_data_dir
from .models import Recording, RejoinAttempt, SegmentRecord

_log = logging.getLogger(__name__)
_manifest_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_recordings_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "recordings"


def get_recordings_path(data_dir: Optional[Path] = None) -> Path:
    return get_recordings_dir(data_dir) / "recordings.json"


# ---------------------------------------------------------------------------
# ID validation (CodeQL four-step pattern — mirrors _validate_campaign_slug)
# ---------------------------------------------------------------------------


def _validate_recording_id(recording_id: str) -> Optional[str]:
    """Four-step CodeQL-safe guard for recording IDs.

    Returns the sanitised ID on success, None on rejection.
    Steps: null-byte check → os.path.basename strip → regex guard →
    os.path.abspath round-trip (breaks CodeQL taint chain).
    """
    if not recording_id or "\x00" in recording_id:
        return None

    safe = os.path.basename(recording_id)
    if safe != recording_id or safe in {".", ".."}:
        return None

    if not re.match(r"^[\w\-]+$", safe):
        return None

    # os.path round-trip breaks the CodeQL taint chain
    _guard_base = os.path.abspath("_recordings_guard")
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe))
    if not _guard_path.startswith(_guard_base):
        return None

    return os.path.basename(_guard_path)


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------


def _seg_to_dict(seg: SegmentRecord) -> dict:
    return {
        "index": seg.index,
        "stream": seg.stream,
        "started_at": seg.started_at,
        "duration_s": seg.duration_s,
        "path": seg.path,
        "finalized": seg.finalized,
    }


def _seg_from_dict(d: dict) -> SegmentRecord:
    return SegmentRecord(
        index=d.get("index", 0),
        stream=d.get("stream", "mixed"),
        started_at=d.get("started_at", ""),
        duration_s=float(d.get("duration_s", 0.0)),
        path=d.get("path", ""),
        finalized=bool(d.get("finalized", False)),
    )


def _rejoin_to_dict(r: RejoinAttempt) -> dict:
    return {
        "timestamp": r.timestamp,
        "close_code": r.close_code,
        "attempt_number": r.attempt_number,
    }


def _rejoin_from_dict(d: dict) -> RejoinAttempt:
    return RejoinAttempt(
        timestamp=d.get("timestamp", ""),
        close_code=int(d.get("close_code", 0)),
        attempt_number=int(d.get("attempt_number", 0)),
    )


def _recording_to_dict(rec: Recording) -> dict:
    return {
        "id": rec.id,
        "campaign_slug": rec.campaign_slug,
        "started_at": rec.started_at,
        "ended_at": rec.ended_at,
        "status": rec.status,
        "voice_channel_id": rec.voice_channel_id,
        "guild_id": rec.guild_id,
        "discord_speakers": rec.discord_speakers,
        "segment_manifest": [_seg_to_dict(s) for s in rec.segment_manifest],
        "combined_path": rec.combined_path,
        "per_user_dir": rec.per_user_dir,
        "transcript_path": rec.transcript_path,
        "rejoin_log": [_rejoin_to_dict(r) for r in rec.rejoin_log],
        "notes": rec.notes,
    }


def _recording_from_dict(d: dict) -> Recording:
    return Recording(
        id=d["id"],
        campaign_slug=d.get("campaign_slug"),
        started_at=d.get("started_at", ""),
        ended_at=d.get("ended_at"),
        status=d.get("status", "failed"),
        voice_channel_id=d.get("voice_channel_id", ""),
        guild_id=d.get("guild_id", ""),
        discord_speakers=dict(d.get("discord_speakers", {})),
        segment_manifest=[_seg_from_dict(s) for s in d.get("segment_manifest", [])],
        combined_path=d.get("combined_path"),
        per_user_dir=d.get("per_user_dir"),
        transcript_path=d.get("transcript_path"),
        rejoin_log=[_rejoin_from_dict(r) for r in d.get("rejoin_log", [])],
        notes=d.get("notes", ""),
    )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def load_recordings(data_dir: Optional[Path] = None) -> dict[str, Recording]:
    """Load all recordings from recordings.json.  Returns {} when file is absent."""
    path = get_recordings_path(data_dir)
    if not path.exists():
        return {}

    with open(path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError:
            _log.warning("recordings.json is corrupt; returning empty recordings dict")
            return {}

    recordings: dict[str, Recording] = {}
    for rec_id, data in raw.items():
        try:
            recordings[rec_id] = _recording_from_dict(data)
        except (KeyError, TypeError, ValueError):
            _log.warning("Skipping corrupt recording entry %r", rec_id)
    return recordings


def save_recordings(
    recordings: dict[str, Recording],
    data_dir: Optional[Path] = None,
) -> None:
    """Persist recordings to recordings.json, creating parent directories as needed."""
    path = get_recordings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    raw = {rec_id: _recording_to_dict(rec) for rec_id, rec in recordings.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_recording(
    voice_channel_id: str,
    guild_id: str,
    campaign_slug: Optional[str] = None,
    data_dir: Optional[Path] = None,
) -> Recording:
    """Create a new recording with a fresh uuid4 ID and status='recording'.

    Creates the on-disk directory tree:
        recordings/<id>/combined/
        recordings/<id>/per-user/
        recordings/<id>/final/
    """
    rec_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    rec_dir = get_recordings_dir(data_dir) / rec_id
    per_user_path = rec_dir / "per-user"
    for sub in ("combined", "per-user", "final"):
        (rec_dir / sub).mkdir(parents=True, exist_ok=True)

    recording = Recording(
        id=rec_id,
        campaign_slug=campaign_slug,
        started_at=now,
        ended_at=None,
        status="recording",
        voice_channel_id=voice_channel_id,
        guild_id=guild_id,
        discord_speakers={},
        segment_manifest=[],
        combined_path=None,
        per_user_dir=str(per_user_path),
        transcript_path=None,
        rejoin_log=[],
        notes="",
    )

    recordings = load_recordings(data_dir)
    recordings[rec_id] = recording
    save_recordings(recordings, data_dir)
    return recording


def update_recording(
    recording_id: str,
    data_dir: Optional[Path] = None,
    **kwargs,
) -> Recording:
    """Update named fields on a recording.  Raises KeyError if not found."""
    recordings = load_recordings(data_dir)
    if recording_id not in recordings:
        raise KeyError(f"Recording {recording_id!r} not found")

    rec = recordings[recording_id]
    for key, value in kwargs.items():
        if not hasattr(rec, key):
            raise ValueError(f"Unknown field {key!r} on Recording")
        setattr(rec, key, value)

    recordings[recording_id] = rec
    save_recordings(recordings, data_dir)
    return rec


def delete_recording(recording_id: str, data_dir: Optional[Path] = None) -> None:
    """Remove a recording from the index.  Raises KeyError if not found.

    Audio files on disk are NOT removed — the caller is responsible for
    cleanup (e.g. via the web UI delete button which calls shutil.rmtree).
    """
    recordings = load_recordings(data_dir)
    if recording_id not in recordings:
        raise KeyError(f"Recording {recording_id!r} not found")
    del recordings[recording_id]
    save_recordings(recordings, data_dir)


def append_segment(
    recording_id: str,
    segment: SegmentRecord,
    data_dir: Optional[Path] = None,
) -> None:
    """Atomically append one SegmentRecord to a recording's manifest.

    Thread-safe via a module-level lock so concurrent per-user audio writer
    threads cannot interleave their save operations.

    Satisfies v1 file-format invariant 2 (manifest is append-only and atomic).
    """
    with _manifest_lock:
        recordings = load_recordings(data_dir)
        if recording_id not in recordings:
            raise KeyError(f"Recording {recording_id!r} not found")
        recordings[recording_id].segment_manifest.append(segment)
        save_recordings(recordings, data_dir)


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def reconcile_on_startup(data_dir: Optional[Path] = None) -> int:
    """Mark any recordings stuck in 'recording' or 'degraded' as 'failed'.

    Called from the FastAPI lifespan startup hook so interrupted sessions
    (e.g. server killed mid-recording) are surfaced as failures rather than
    left in an ambiguous active state.

    Returns the number of recordings whose status was corrected.
    """
    try:
        recordings = load_recordings(data_dir)
    except Exception:
        _log.warning("Could not load recordings.json during reconciliation; skipping")
        return 0

    corrected = 0
    for rec in recordings.values():
        if rec.status in ("recording", "degraded"):
            rec.status = "failed"
            corrected += 1

    if corrected:
        save_recordings(recordings, data_dir)
        _log.warning(
            "reconcile_on_startup: marked %d interrupted recording(s) as failed",
            corrected,
        )

    return corrected
