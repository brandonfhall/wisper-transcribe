"""Recording manager — Discord and local recording sessions.

Data lives in ``wisper.db`` (``recordings`` plus the ``recording_discord`` /
``recording_devices`` subtypes, ``recording_speakers``, ``recording_segments``,
``recording_markers``, ``recording_rejoins``). Audio stays on disk in a fixed
layout under ``$DATA_DIR/recordings/<id>/``:

    per-user/<track>/NNNN.wav   60 s segments per Discord user id, or mic/system
    combined/NNNN.wav           60 s segments of the mixed track
    combined.wav                concatenated at session end
    live_transcript.md          live draft (local sessions)

so paths are never stored: :class:`~wisper_transcribe.models.Recording`'s
``combined_path``, ``per_user_dir``, and segment ``path`` are derived from
the layout. So is ``status``: only the capture lifecycle is stored
(``capture_status``); ``transcribed`` means the recording has a transcript
(``transcript_id``, which ``ON DELETE SET NULL`` clears when the transcript
is deleted) and ``transcribing`` means a pending or running transcription
job for it exists in ``jobs``. Startup marks jobs left pending or running as
interrupted, so a recording can never be stuck in ``transcribing``.

Writes never lose each other's updates: :func:`save_recording` updates the
recording's own fields and only *adds* missing segment, marker, and rejoin
rows, so a long-lived ``Recording`` held by a capture manager can't erase a
marker another request appended meanwhile.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
import wave
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Optional

from . import db
from .config import get_data_dir
from .models import Marker, Recording, RejoinAttempt, SegmentRecord
from .path_utils import validate_path_component

log = logging.getLogger(__name__)

CAPTURE_STATUSES = ("recording", "degraded", "completed", "failed")
ACTIVE_CAPTURE = ("recording", "degraded")
# The capture hot path waits at most this long for the database, then logs
# and carries on (startup reconcile restores missing segment rows).
HOT_PATH_BUSY_MS = 500


# ---------------------------------------------------------------------------
# Path helpers (fixed layout)
# ---------------------------------------------------------------------------

def get_recordings_dir(data_dir: Optional[Path] = None) -> Path:
    base = Path(data_dir) if data_dir else get_data_dir()
    return base / "recordings"


def get_recording_dir(recording_id: str, data_dir: Optional[Path] = None) -> Path:
    return get_recordings_dir(data_dir) / recording_id


def combined_path_for(recording_id: str, data_dir: Optional[Path] = None) -> Path:
    return get_recording_dir(recording_id, data_dir) / "combined.wav"


def segment_path_for(recording_id: str, idx: int, data_dir: Optional[Path] = None) -> Path:
    return get_recording_dir(recording_id, data_dir) / "combined" / f"{idx:04d}.wav"


# ---------------------------------------------------------------------------
# Security: recording ID validation (regex + os.path round-trip; .claude/rules/web-security.md)
# ---------------------------------------------------------------------------

def _validate_recording_id(recording_id: str) -> Optional[str]:
    return validate_path_component(recording_id, "_recordings_guard")


# ---------------------------------------------------------------------------
# Timestamps: ISO-8601 UTC with microseconds (markers can be a second apart)
# ---------------------------------------------------------------------------

def _ts(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def _load(conn: sqlite3.Connection, data_dir: Optional[Path],
          where: str = "", params: tuple = ()) -> dict[str, Recording]:
    from .path_utils import get_output_dir

    rows = conn.execute(
        "SELECT r.*, c.slug AS campaign_slug, t.stem AS transcript_stem, "
        "d.guild_id, d.voice_channel_id, "
        "(SELECT j.id FROM jobs j WHERE j.recording_id = r.id AND j.type = 'transcription' "
        " ORDER BY j.created_at DESC, j.rowid DESC LIMIT 1) AS latest_job_id, "
        "EXISTS (SELECT 1 FROM jobs j WHERE j.recording_id = r.id AND j.type = 'transcription' "
        " AND j.status IN ('pending', 'running')) AS job_active "
        "FROM recordings r "
        "LEFT JOIN campaigns c ON c.id = r.campaign_id "
        "LEFT JOIN transcripts t ON t.id = r.transcript_id "
        "LEFT JOIN recording_discord d ON d.recording_id = r.id "
        f"{where} ORDER BY r.rowid",
        params,
    ).fetchall()
    if not rows:
        return {}
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))

    def _children(sql: str) -> dict[str, list]:
        out: dict[str, list] = {}
        for row in conn.execute(sql.format(marks=marks), ids):
            out.setdefault(row["recording_id"], []).append(row)
        return out

    devices = _children("SELECT * FROM recording_devices WHERE recording_id IN ({marks})")
    speakers = _children(
        "SELECT s.recording_id, s.discord_user_id, p.key FROM recording_speakers s "
        "LEFT JOIN profiles p ON p.id = s.profile_id WHERE s.recording_id IN ({marks}) "
        "ORDER BY s.rowid")
    segments = _children("SELECT * FROM recording_segments WHERE recording_id IN ({marks}) ORDER BY idx")
    markers = _children("SELECT * FROM recording_markers WHERE recording_id IN ({marks}) ORDER BY marked_at")
    rejoins = _children("SELECT * FROM recording_rejoins WHERE recording_id IN ({marks}) ORDER BY attempted_at")

    output_dir = get_output_dir() if any(r["transcript_stem"] for r in rows) else None
    recordings: dict[str, Recording] = {}
    for r in rows:
        rid = r["id"]
        started = _dt(r["started_at"])
        if r["transcript_stem"] is not None:
            status = "transcribed"
        elif r["capture_status"] == "completed" and r["job_active"]:
            status = "transcribing"
        else:
            status = r["capture_status"]
        combined = combined_path_for(rid, data_dir)
        has_combined = combined.exists()
        spk = speakers.get(rid, [])
        recordings[rid] = Recording(
            id=rid,
            campaign_slug=r["campaign_slug"],
            started_at=started,
            ended_at=_dt(r["ended_at"]),
            status=status,
            voice_channel_id=r["voice_channel_id"] or "",
            guild_id=r["guild_id"] or "",
            discord_speakers={s["discord_user_id"]: s["key"] or "" for s in spk},
            segment_manifest=[
                SegmentRecord(index=s["idx"], stream="mixed", started_at=_dt(s["started_at"]),
                              duration_s=s["duration_s"],
                              path=segment_path_for(rid, s["idx"], data_dir),
                              finalized=bool(s["finalized"]))
                for s in segments.get(rid, [])
            ],
            combined_path=combined if has_combined else None,
            per_user_dir=get_recording_dir(rid, data_dir) / "per-user",
            transcript_path=(output_dir / f"{r['transcript_stem']}.md") if r["transcript_stem"] else None,
            rejoin_log=[
                RejoinAttempt(timestamp=_dt(j["attempted_at"]), close_code=j["close_code"],
                              attempt_number=j["attempt_number"])
                for j in rejoins.get(rid, [])
            ],
            notes=r["notes"],
            unbound_speakers=[s["discord_user_id"] for s in spk if s["key"] is None],
            job_id=r["latest_job_id"],
            source=r["source"],
            devices={d["role"]: d["device_name"] for d in devices.get(rid, [])},
            name=r["name"],
            markers=[
                Marker(timestamp=_dt(m["marked_at"]),
                       elapsed_s=(_dt(m["marked_at"]) - started).total_seconds())
                for m in markers.get(rid, [])
            ],
            recovered_at=_dt(r["recovered_at"]),
            recoverable=(r["capture_status"] == "failed" and not has_combined
                         and _has_segment_files(rid, data_dir)),
        )
    return recordings


def _has_segment_files(recording_id: str, data_dir: Optional[Path]) -> bool:
    combined_dir = get_recording_dir(recording_id, data_dir) / "combined"
    return combined_dir.is_dir() and any(combined_dir.glob("*.wav"))


def load_recordings(data_dir: Optional[Path] = None) -> dict[str, Recording]:
    """All recordings by id, oldest first, with status and paths derived."""
    with db.connection(data_dir) as conn:
        return _load(conn, data_dir)


def load_recording(recording_id: str, data_dir: Optional[Path] = None) -> Optional[Recording]:
    with db.connection(data_dir) as conn:
        return _load(conn, data_dir, "WHERE r.id = ?", (recording_id,)).get(recording_id)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def _check_derived(rec: Recording, data_dir: Optional[Path]) -> None:
    """Refuse values that can only be derived, so a caller can't believe it
    stored something that is silently recomputed."""
    if rec.status not in CAPTURE_STATUSES and rec.status not in ("transcribing", "transcribed"):
        raise ValueError(f"unknown recording status {rec.status!r}")
    if rec.combined_path is not None and Path(rec.combined_path) != combined_path_for(rec.id, data_dir):
        raise ValueError("combined_path is fixed: recordings/<id>/combined.wav")
    for seg in rec.segment_manifest:
        if seg.stream != "mixed" or Path(seg.path) != segment_path_for(rec.id, seg.index, data_dir):
            raise ValueError("segment paths are fixed: recordings/<id>/combined/NNNN.wav (mixed)")
    if rec.source not in ("discord", "local"):
        raise ValueError(f"unknown recording source {rec.source!r}")


def _capture_status(conn: sqlite3.Connection, rec: Recording) -> str:
    """The stored capture state for ``rec.status``. ``transcribing`` and
    ``transcribed`` are derived, so they leave the stored state as it is."""
    if rec.status in CAPTURE_STATUSES:
        return rec.status
    row = conn.execute("SELECT capture_status FROM recordings WHERE id = ?", (rec.id,)).fetchone()
    return row[0] if row else "completed"


def _profile_id(conn: sqlite3.Connection, key: str) -> Optional[int]:
    if not key:
        return None
    row = conn.execute("SELECT id FROM profiles WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _transcript_id(conn: sqlite3.Connection, path: Optional[Path]) -> Optional[int]:
    if path is None:
        return None
    import os

    from .path_utils import get_output_dir
    from .transcript_store import ensure_row

    out = get_output_dir()
    if os.path.realpath(Path(path).parent) != os.path.realpath(out):
        log.warning("Recording transcript %s is outside the transcripts folder; not linked", Path(path).name)
        return None
    return ensure_row(conn, Path(path).stem, out)


def save_recording(recording: Recording, data_dir: Optional[Path] = None) -> None:
    """Persist a recording's own fields; add (never remove) its segment,
    marker, and rejoin rows.

    For creating a recording and for edits made from a freshly loaded
    object. Capture code holds a long-lived ``Recording`` whose name and notes
    may be stale, so it uses the targeted writers instead
    (``update_recording_status``, ``bind_recording_speaker``,
    ``append_rejoin``, ``append_segment``, ``append_marker``); a test enforces it.

    Raises ValueError for values that are derived, not stored (a
    ``combined_path`` or segment path off the fixed layout).
    """
    rec = recording
    _check_derived(rec, data_dir)
    with db.transaction(data_dir) as conn:
        campaign_id = None
        if rec.campaign_slug:
            row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (rec.campaign_slug,)).fetchone()
            campaign_id = row[0] if row else None
        capture = _capture_status(conn, rec)
        ended = None if capture in ACTIVE_CAPTURE else _ts(rec.ended_at)
        recovered = _ts(getattr(rec, "recovered_at", None)) if capture == "completed" else None
        conn.execute(
            "INSERT INTO recordings (id, source, name, notes, campaign_id, transcript_id, "
            "capture_status, started_at, ended_at, recovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, notes = excluded.notes, "
            "campaign_id = excluded.campaign_id, transcript_id = excluded.transcript_id, "
            "capture_status = excluded.capture_status, started_at = excluded.started_at, "
            "ended_at = excluded.ended_at, "
            "recovered_at = coalesce(excluded.recovered_at, recordings.recovered_at)",
            (rec.id, rec.source, rec.name, rec.notes, campaign_id,
             _transcript_id(conn, rec.transcript_path), capture,
             _ts(rec.started_at), ended, recovered),
        )
        if rec.source == "discord":
            conn.execute(
                "INSERT INTO recording_discord (recording_id, guild_id, voice_channel_id) "
                "VALUES (?, ?, ?) ON CONFLICT (recording_id) DO UPDATE SET "
                "guild_id = excluded.guild_id, voice_channel_id = excluded.voice_channel_id",
                (rec.id, rec.guild_id or "", rec.voice_channel_id or ""),
            )
            heard = dict.fromkeys(list(rec.discord_speakers) + list(rec.unbound_speakers))
            for uid in heard:
                if not str(uid).isdigit():
                    log.warning("Ignoring non-numeric Discord user id on recording %s", rec.id)
                    continue
                pid = _profile_id(conn, rec.discord_speakers.get(uid, ""))
                # A binding is never undone by a save: a long-lived capture
                # object may predate an enrollment that bound this user.
                conn.execute(
                    "INSERT INTO recording_speakers (recording_id, discord_user_id, profile_id) "
                    "VALUES (?, ?, ?) ON CONFLICT (recording_id, discord_user_id) DO UPDATE SET "
                    "profile_id = coalesce(excluded.profile_id, recording_speakers.profile_id)",
                    (rec.id, str(uid), pid),
                )
            for attempt in rec.rejoin_log:
                _insert_rejoin(conn, rec.id, attempt)
        else:
            conn.execute("DELETE FROM recording_devices WHERE recording_id = ?", (rec.id,))
            for role, name in (rec.devices or {}).items():
                if role in ("mic", "system") and name:
                    conn.execute(
                        "INSERT INTO recording_devices (recording_id, role, device_name) VALUES (?, ?, ?)",
                        (rec.id, role, str(name)),
                    )
        for seg in rec.segment_manifest:
            _insert_segment(conn, rec.id, seg)
        for marker in rec.markers:
            conn.execute(
                "INSERT OR IGNORE INTO recording_markers (recording_id, marked_at) VALUES (?, ?)",
                (rec.id, _ts(marker.timestamp)),
            )


def _insert_segment(conn: sqlite3.Connection, recording_id: str, seg: SegmentRecord) -> None:
    conn.execute(
        "INSERT INTO recording_segments (recording_id, idx, started_at, duration_s, finalized) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (recording_id, idx) DO UPDATE SET "
        "finalized = max(recording_segments.finalized, excluded.finalized), "
        "duration_s = max(recording_segments.duration_s, excluded.duration_s)",
        (recording_id, int(seg.index), _ts(seg.started_at), max(0.0, float(seg.duration_s)),
         int(bool(seg.finalized))),
    )


def _insert_rejoin(conn: sqlite3.Connection, recording_id: str, attempt: RejoinAttempt) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO recording_rejoins (recording_id, attempted_at, close_code, "
        "attempt_number) VALUES (?, ?, ?, ?)",
        (recording_id, _ts(attempt.timestamp), int(attempt.close_code),
         max(1, int(attempt.attempt_number))),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_recording(
    voice_channel_id: str,
    guild_id: str,
    campaign_slug: Optional[str] = None,
    data_dir: Optional[Path] = None,
    source: str = "discord",
    devices: Optional[dict] = None,
    name: Optional[str] = None,
) -> Recording:
    """Create and persist a new Recording in 'recording' status.

    `source`/`devices` back local capture sessions (`web/local_capture.py`):
    `voice_channel_id`/`guild_id` are empty strings there, and `devices`
    carries the chosen mic/system device names for the detail page.
    `name` is an optional user-supplied session title, set at session
    start -- display-only, never used in a file path.
    """
    recording_id = str(uuid.uuid4())
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
        per_user_dir=get_recording_dir(recording_id, data_dir) / "per-user",
        transcript_path=None,
        rejoin_log=[],
        source=source,
        devices=devices or {},
        name=name,
    )
    save_recording(recording, data_dir)
    return recording


def update_recording_status(
    recording_id: str,
    status: str,
    data_dir: Optional[Path] = None,
    ended_at: Optional[datetime] = None,
) -> None:
    if status not in CAPTURE_STATUSES:
        raise ValueError(f"{status!r} is derived, not stored")
    with db.transaction(data_dir) as conn:
        row = conn.execute("SELECT ended_at FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        if row is None:
            raise KeyError(f"Recording {recording_id!r} not found")
        ended = None if status in ACTIVE_CAPTURE else (_ts(ended_at) or row["ended_at"])
        conn.execute("UPDATE recordings SET capture_status = ?, ended_at = ? WHERE id = ?",
                     (status, ended, recording_id))


def append_segment(
    recording_id: str,
    segment: SegmentRecord,
    data_dir: Optional[Path] = None,
) -> None:
    """Add one segment row (a single short insert; safe from any thread)."""
    with db.transaction(data_dir, busy_timeout_ms=HOT_PATH_BUSY_MS) as conn:
        if conn.execute("SELECT 1 FROM recordings WHERE id = ?", (recording_id,)).fetchone() is None:
            raise KeyError(f"Recording {recording_id!r} not found")
        _insert_segment(conn, recording_id, segment)


def record_completed_wav_segment(
    recording_id: str,
    path: Path,
    started_at: datetime,
    *,
    finalized: bool,
    data_dir: Optional[Path] = None,
) -> datetime:
    """Record a completed "mixed" combined-track segment; return the next segment's start.

    Called by both recording managers whenever the combined writer rotates or
    finalises. Duration is wall-clock since ``started_at`` (the writer doesn't
    expose per-segment media time); good enough for the UI.

    Zero-frame or unreadable segments are skipped, matching
    ``concat_wav_segments()``, so a no-audio session shows no segments.

    Never raises: it runs on the capture hot path. If the database stays busy
    past a short timeout the row is skipped and logged; startup reconcile
    restores it from the file on disk.
    """
    now = datetime.now(timezone.utc)
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnframes() == 0:
                return now
    except (wave.Error, EOFError, OSError):
        return now

    try:
        segment = SegmentRecord(
            index=int(Path(path).stem),
            stream="mixed",
            started_at=started_at,
            duration_s=(now - started_at).total_seconds(),
            path=Path(path),
            finalized=finalized,
        )
        append_segment(recording_id, segment, data_dir=data_dir)
    except Exception:
        log.warning("Failed to append segment record for recording %s", recording_id, exc_info=True)
    return now


def append_marker(recording_id: str, data_dir: Optional[Path] = None) -> Marker:
    """Add a marker at the current time -- the "Add marker" button on `/record`.

    `elapsed_s` is derived from `started_at` (which never changes). Marker
    times carry microseconds, so two clicks in one second are two markers.
    Not on the capture hot path (it's a web request), so it waits the normal
    busy timeout: unlike a segment row, a dropped marker can't be restored.
    """
    now = datetime.now(timezone.utc)
    with db.transaction(data_dir) as conn:
        row = conn.execute("SELECT started_at FROM recordings WHERE id = ?", (recording_id,)).fetchone()
        if row is None:
            raise KeyError(f"Recording {recording_id!r} not found")
        conn.execute("INSERT OR IGNORE INTO recording_markers (recording_id, marked_at) VALUES (?, ?)",
                     (recording_id, _ts(now)))
    return Marker(timestamp=now, elapsed_s=(now - _dt(row["started_at"])).total_seconds())


def bind_recording_speaker(recording_id: str, discord_user_id: str, profile_key: str = "",
                           data_dir: Optional[Path] = None) -> None:
    """Record that a Discord user was heard, bound to ``profile_key`` if given.

    A binding is never undone: an empty ``profile_key`` leaves an existing
    one in place. Unknown profile keys count as unbound. Runs on the capture
    hot path (short busy timeout).
    """
    uid = str(discord_user_id)
    if not uid.isdigit():
        log.warning("Ignoring non-numeric Discord user id on recording %s", recording_id)
        return
    with db.transaction(data_dir, busy_timeout_ms=HOT_PATH_BUSY_MS) as conn:
        conn.execute(
            "INSERT INTO recording_speakers (recording_id, discord_user_id, profile_id) "
            "VALUES (?, ?, ?) ON CONFLICT (recording_id, discord_user_id) DO UPDATE SET "
            "profile_id = coalesce(excluded.profile_id, recording_speakers.profile_id)",
            (recording_id, uid, _profile_id(conn, profile_key)),
        )


def append_rejoin(recording_id: str, attempt: RejoinAttempt, data_dir: Optional[Path] = None) -> None:
    """Log one Discord auto-rejoin attempt."""
    with db.transaction(data_dir, busy_timeout_ms=HOT_PATH_BUSY_MS) as conn:
        _insert_rejoin(conn, recording_id, attempt)


def recover_recording(recording_id: str, data_dir: Optional[Path] = None) -> Recording:
    """Rebuild ``combined.wav`` for a session that crashed, from its segments.

    Segments are self-contained WAVs (the writer patches each header when it
    closes), so recovery is a plain join; only the last partial minute can be
    missing. The recording becomes ``completed`` with ``recovered_at`` set and
    can then be transcribed like any other. Slow for long sessions: call it
    off the request thread.

    Raises KeyError (unknown id) or ValueError (active, not failed, already
    has ``combined.wav``, or no readable segments).
    """
    from .web.audio_writer import concat_wav_segments

    rec = load_recording(recording_id, data_dir)
    if rec is None:
        raise KeyError(f"Recording {recording_id!r} not found")
    if rec.status in ACTIVE_CAPTURE:
        raise ValueError("the session is still recording")
    if not rec.recoverable:
        raise ValueError("nothing to recover")
    combined = combined_path_for(recording_id, data_dir)
    merged = concat_wav_segments(combined.parent / "combined", combined)
    if merged is None:
        raise ValueError("no readable audio segments")
    with db.transaction(data_dir) as conn:
        conn.execute(
            "UPDATE recordings SET capture_status = 'completed', recovered_at = ?, "
            "ended_at = coalesce(ended_at, ?) WHERE id = ? AND capture_status = 'failed'",
            (_ts(datetime.now(timezone.utc)), _ts(datetime.now(timezone.utc)), recording_id),
        )
    log.info("Recovered recording %s from its segments", recording_id)
    return load_recording(recording_id, data_dir)


def link_transcript(recording_id: str, transcript_path: Path,
                    data_dir: Optional[Path] = None) -> None:
    """Record that ``transcript_path`` is this recording's transcript (it then
    reads as ``transcribed``). A transcript can belong to one recording."""
    with db.transaction(data_dir) as conn:
        tid = _transcript_id(conn, Path(transcript_path))
        conn.execute("UPDATE recordings SET transcript_id = NULL WHERE transcript_id = ? AND id <> ?",
                     (tid, recording_id))
        conn.execute("UPDATE recordings SET transcript_id = ? WHERE id = ?", (tid, recording_id))


def delete_recording(recording_id: str, data_dir: Optional[Path] = None) -> None:
    """Delete the recording's rows. Does NOT delete audio files.

    The web routes that expose deletion (``web/routes/record.py``) call
    ``_purge_recording_files()`` first to remove the files themselves -- a
    route-layer concern kept separate on purpose, so callers that only want
    the entry gone still have that option.
    """
    with db.transaction(data_dir) as conn:
        conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def reconcile_on_startup(data_dir: Optional[Path] = None) -> None:
    """On server start: an active capture state means the server crashed
    mid-session, so mark it 'failed' (its audio is kept); then restore any
    combined-track segment rows the hot path couldn't write."""
    now = _ts(datetime.now(timezone.utc))
    try:
        with db.transaction(data_dir) as conn:
            crashed = conn.execute(
                "UPDATE recordings SET capture_status = 'failed', ended_at = coalesce(ended_at, ?) "
                "WHERE capture_status IN ('recording', 'degraded') RETURNING id",
                (now,),
            ).fetchall()
            for (rid,) in crashed:
                log.warning("Recording %s was active at startup — marked failed (crash recovery)", rid)
            _restore_segment_rows(conn, data_dir)
    except Exception:
        log.warning("reconcile_on_startup failed", exc_info=True)


def _restore_segment_rows(conn: sqlite3.Connection, data_dir: Optional[Path]) -> None:
    rows = conn.execute("SELECT id, started_at FROM recordings").fetchall()
    for r in rows:
        combined_dir = get_recording_dir(r["id"], data_dir) / "combined"
        if not combined_dir.is_dir():
            continue
        known = {x[0] for x in conn.execute(
            "SELECT idx FROM recording_segments WHERE recording_id = ?", (r["id"],))}
        for wav in sorted(combined_dir.glob("*.wav")):
            if not wav.stem.isdigit() or int(wav.stem) in known:
                continue
            try:
                with wave.open(str(wav), "rb") as wf:
                    frames, rate = wf.getnframes(), wf.getframerate()
            except (wave.Error, EOFError, OSError):
                continue
            if frames == 0:
                continue
            duration = frames / float(rate or 16000)
            # The file was last written when the segment ended.
            started = datetime.fromtimestamp(wav.stat().st_mtime - duration, UTC)
            conn.execute(
                "INSERT OR IGNORE INTO recording_segments (recording_id, idx, started_at, "
                "duration_s, finalized) VALUES (?, ?, ?, ?, 1)",
                (r["id"], int(wav.stem), _ts(started), duration),
            )
            log.info("Restored segment %s of recording %s from disk", wav.name, r["id"])
