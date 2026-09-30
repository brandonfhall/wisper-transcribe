"""Seed wisper.db directly for tests (synthetic data only)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from wisper_transcribe import db
from wisper_transcribe.config import EMBEDDING_SPACE
from wisper_transcribe import campaign_manager as cm, speaker_manager as sm
from wisper_transcribe.models import Campaign, SpeakerProfile


def unit(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    return v / n if n else v


def make_profile(key: str, display_name: Optional[str] = None, *, role: str = "",
                 embedding=None, embedding_space: str = EMBEDDING_SPACE,
                 notes: str = "", enrolled_date: str = "2026-01-01",
                 enrollment_source: str = "session.mp3") -> SpeakerProfile:
    """A SpeakerProfile; ``embedding`` defaults to a key-seeded random unit vector."""
    if embedding is None:
        rng = np.random.default_rng(abs(hash(key)) % (2**32))
        embedding = unit(rng.standard_normal(256))
    return SpeakerProfile(
        name=key,
        display_name=display_name or key.replace("_", " ").title(),
        role=role,
        embedding=unit(embedding),
        enrolled_date=enrolled_date,
        enrollment_source=enrollment_source,
        notes=notes,
        embedding_space=embedding_space,
    )


def seed_profile(key: str, display_name: Optional[str] = None, *,
                 data_dir: Optional[Path] = None, **kwargs) -> SpeakerProfile:
    """Insert (or replace) one profile in the data dir's wisper.db."""
    from wisper_transcribe.speaker_manager import _upsert_profile

    profile = make_profile(key, display_name, **kwargs)
    with db.transaction(data_dir) as conn:
        _upsert_profile(conn, key, profile)
    return profile


def seed_profiles(*keys: str, data_dir: Optional[Path] = None) -> None:
    for key in keys:
        seed_profile(key, data_dir=data_dir)


def sidecar_data(sidecar_path: Path) -> dict:
    """What a ``<stem>_diar.json`` means now: its segments plus the speaker
    fields stored in the database (``transcript_store.read_sidecar``)."""
    from wisper_transcribe.transcript_store import SIDECAR_SUFFIX, read_sidecar

    name = Path(sidecar_path).name
    assert name.endswith(SIDECAR_SUFFIX), name
    md = Path(sidecar_path).with_name(name[: -len(SIDECAR_SUFFIX)] + ".md")
    data = read_sidecar(md)
    assert data is not None, f"no sidecar for {md.name}"
    return data


def seed_job(job_id: str, *, job_type: str = "transcription", status: str = "pending",
             recording_id: Optional[str] = None, data_dir: Optional[Path] = None) -> None:
    """A row in job history (e.g. an active transcription for a recording)."""
    import types
    from datetime import datetime

    from wisper_transcribe import job_history

    now = datetime.now()
    job_history.record(types.SimpleNamespace(
        id=job_id, job_type=job_type, status=status, created_at=now,
        started_at=now if status != "pending" else None,
        finished_at=now if status in ("completed", "failed") else None,
        error="Cancelled" if status == "failed" else None, kwargs={}, log_lines=[],
        recording_id=recording_id, output_path=None,
    ), data_dir)


def seed_recording(data_dir: Optional[Path] = None, *, status: str = "completed",
                   with_audio: bool = True, **kwargs):
    """A recording in ``status`` with the real on-disk layout: when
    ``with_audio``, ``recordings/<id>/combined.wav`` exists (a tiny valid WAV),
    so the derived ``combined_path`` resolves. Returns the loaded Recording."""
    import wave
    from datetime import datetime, timezone

    from wisper_transcribe.recording_manager import (
        combined_path_for, create_recording, load_recording, update_recording_status,
    )

    rec = create_recording(
        kwargs.pop("voice_channel_id", "VC1"), kwargs.pop("guild_id", "G1"),
        data_dir=data_dir, **kwargs,
    )
    if with_audio:
        path = combined_path_for(rec.id, data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 160)
    if status != "recording":
        update_recording_status(rec.id, status, data_dir,
                                ended_at=datetime.now(timezone.utc))
    return load_recording(rec.id, data_dir)


# Whole-store replace, for tests that build a store directly.

def save_profiles(profiles: dict[str, SpeakerProfile], data_dir: Optional[Path] = None) -> None:
    """Make the profile store exactly ``profiles``, in one transaction.

    Existing keys are updated in place (memberships kept); keys not in
    ``profiles`` are deleted, which also drops their campaign memberships.
    Test seeding only; the app uses the targeted functions.
    """
    with db.transaction(data_dir) as conn:
        existing = {r[0] for r in conn.execute("SELECT key FROM profiles")}
        for key in existing - set(profiles):
            conn.execute("DELETE FROM profiles WHERE key = ?", (key,))
        for key, p in profiles.items():
            sm._upsert_profile(conn, key, p)


def save_campaigns(campaigns: dict[str, Campaign], data_dir: Optional[Path] = None) -> None:
    """Make the campaign store exactly ``campaigns``, in one transaction.

    Campaigns are matched by slug and updated in place; slugs not in
    ``campaigns`` are deleted. Members must be existing profiles (``KeyError``
    otherwise); a transcript listed here moves out of any other campaign.
    Test seeding only; the app uses the targeted functions.
    """
    with db.transaction(data_dir) as conn:
        existing = {r[0] for r in conn.execute("SELECT slug FROM campaigns")}
        for slug in existing - set(campaigns):
            conn.execute("DELETE FROM campaigns WHERE slug = ?", (slug,))
        for slug, c in campaigns.items():
            created = c.created if len(c.created or "") > 10 else (
                f"{c.created}T00:00:00Z" if c.created else db.now_utc()
            )
            cid = conn.execute(
                "INSERT INTO campaigns (slug, display_name, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (slug) DO UPDATE SET display_name = excluded.display_name, "
                "created_at = excluded.created_at RETURNING id",
                (slug, c.display_name or slug, created),
            ).fetchone()[0]
            conn.execute("DELETE FROM campaign_members WHERE campaign_id = ?", (cid,))
            for key, m in c.members.items():
                conn.execute(
                    "INSERT INTO campaign_members (campaign_id, profile_id, role, character, "
                    "discord_user_id) VALUES (?, ?, ?, ?, ?)",
                    (cid, cm._profile_id(conn, key), m.role or "", m.character or "",
                     m.discord_user_id or None),
                )
            cm._write_order(conn, cid, [cm._transcript_id(conn, st) for st in c.transcripts])
