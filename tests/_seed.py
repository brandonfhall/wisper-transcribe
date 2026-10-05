"""Seed wisper.db directly for tests (synthetic data only)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from wisper_transcribe import db
from wisper_transcribe.config import EMBEDDING_SPACE
from wisper_transcribe import campaign_manager as cm, speaker_manager as sm
from wisper_transcribe.models import Campaign, SpeakerProfile
from wisper_transcribe.transcript_store import ANY


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


def _find_or_create_transcript(conn, stem: str) -> int:
    """The session named ``stem`` in any campaign or the root; created in the root if none."""
    from wisper_transcribe.transcript_store import ensure_row

    rows = conn.execute("SELECT id FROM transcripts WHERE stem = ? LIMIT 2", (stem,)).fetchall()
    if len(rows) > 1:
        raise ValueError(f"more than one session is named {stem!r}")
    return rows[0][0] if rows else ensure_row(conn, stem)


def _assign_stems(conn, cid: int, stems: list[str]) -> None:
    """Make campaign ``cid`` hold exactly ``stems``, in that order.

    Two steps so UNIQUE(campaign_id, position) never collides mid-way: shift
    the campaign's rows above every final position, then write the final ones.
    """
    shift = conn.execute(
        "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?", (cid,)
    ).fetchone()[0] + len(stems)
    conn.execute("UPDATE transcripts SET position = position + ? WHERE campaign_id = ?",
                 (shift, cid))
    for pos, stem in enumerate(stems):
        conn.execute("UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?",
                     (cid, pos, _find_or_create_transcript(conn, stem)))
    conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL "
                 "WHERE campaign_id = ? AND position >= ?", (cid, shift))


def save_campaigns(campaigns: dict[str, Campaign], data_dir: Optional[Path] = None) -> None:
    """Make the campaign store exactly ``campaigns``, in one transaction.

    Campaigns are matched by slug and updated in place; slugs not in
    ``campaigns`` are deleted, their transcripts unassigned. Members must be
    existing profiles (``KeyError`` otherwise); a transcript listed here moves
    out of any other campaign. Test seeding only; the app uses the targeted
    functions.
    """
    from wisper_transcribe import campaign_folders

    with db.transaction(data_dir) as conn:
        existing = {r[0] for r in conn.execute("SELECT slug FROM campaigns")}
        for slug in existing - set(campaigns):
            conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL "
                         "WHERE campaign_id = (SELECT id FROM campaigns WHERE slug = ?)", (slug,))
            conn.execute("DELETE FROM campaigns WHERE slug = ?", (slug,))
        for slug, c in campaigns.items():
            created = c.created if len(c.created or "") > 10 else (
                f"{c.created}T00:00:00Z" if c.created else db.now_utc()
            )
            display_name = c.display_name or slug
            row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                cid = conn.execute(
                    "INSERT INTO campaigns (slug, display_name, folder, created_at) "
                    "VALUES (?, ?, ?, ?) RETURNING id",
                    (slug, display_name, campaign_folders.unique_folder(display_name, conn),
                     created),
                ).fetchone()[0]
            else:
                cid = row[0]
                conn.execute("UPDATE campaigns SET display_name = ?, created_at = ? WHERE id = ?",
                             (display_name, created, cid))
            conn.execute("DELETE FROM campaign_members WHERE campaign_id = ?", (cid,))
            for key, m in c.members.items():
                conn.execute(
                    "INSERT INTO campaign_members (campaign_id, profile_id, role, character, "
                    "discord_user_id) VALUES (?, ?, ?, ?, ?)",
                    (cid, cm._profile_id(conn, key), m.role or "", m.character or "",
                     m.discord_user_id or None),
                )
            _assign_stems(conn, cid, c.transcripts)


def seed_campaign(display_name: str, slug: Optional[str] = None, *, claimed: bool = False,
                  data_dir: Optional[Path] = None) -> int:
    """A campaign row; returns its id. Without ``slug`` the slug derives from the
    name. ``claimed`` also creates and claims its folder.

    A direct insert (test seeding): it makes no folder, so a test that needs one
    on disk makes it, or claims it with ``claimed=True``.
    """
    from wisper_transcribe import campaign_folders

    with db.transaction(data_dir) as conn:
        cid = conn.execute(
            "INSERT INTO campaigns (slug, display_name, folder, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (slug or cm._make_slug(display_name), display_name,
             campaign_folders.unique_folder(display_name, conn), db.now_utc()),
        ).fetchone()[0]
    if claimed:
        campaign_folders.ensure_folder(cid, data_dir=data_dir)
    return cid


def seed_transcript(stem: str, *, campaign: Optional[str] = None, write_md: bool = False,
                    data_dir: Optional[Path] = None,
                    output_dir: Optional[Path] = None) -> int:
    """A session row; returns its id. ``campaign`` is a slug; the session is
    appended to it. ``write_md`` writes ``<stem>.md`` in the output root and
    registers it."""
    with db.transaction(data_dir) as conn:
        tid = _find_or_create_transcript(conn, stem)
        if campaign is not None:
            cid = cm._campaign_id(conn, campaign)
            pos = conn.execute(
                "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
                (cid,),
            ).fetchone()[0]
            conn.execute("UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?",
                         (cid, pos, tid))
    if write_md:
        from wisper_transcribe import file_registry
        from wisper_transcribe.config import get_output_root

        root = Path(output_dir) if output_dir is not None else get_output_root()
        md = root / f"{stem}.md"
        md.write_text(f"# {stem}\n", encoding="utf-8")
        file_registry.add(md, kind="transcript", owner=file_registry.Owner("transcript", tid),
                          output_dir=root, data_dir=data_dir)
        with db.transaction(data_dir) as conn:
            conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (tid,))
    return tid


def transcript_id(stem: str, *, campaign_slug=ANY, data_dir: Optional[Path] = None) -> int:
    """The id of the session named ``stem`` (``KeyError`` if absent).

    ``campaign_slug`` defaults to any campaign (including the root); ``None``
    means the root only. Raises ``ValueError`` when two rows match.
    """
    from wisper_transcribe.transcript_store import find_by_stem

    if campaign_slug is ANY or campaign_slug is None:
        campaign_id = campaign_slug
    else:
        with db.connection(data_dir) as conn:
            row = conn.execute(
                "SELECT id FROM campaigns WHERE slug = ?", (campaign_slug,)
            ).fetchone()
        if row is None:
            raise KeyError(campaign_slug)
        campaign_id = row[0]
    found = find_by_stem(stem, campaign_id=campaign_id, data_dir=data_dir)
    if not found:
        raise KeyError(stem)
    if len(found) > 1:
        raise ValueError(f"more than one session is named {stem!r}")
    return found[0].id


def move_to_campaign(stem: str, slug: str, data_dir: Optional[Path] = None) -> int:
    """Attach the session named ``stem`` (created in the root if absent) to
    ``slug``; returns its id.

    A direct ``UPDATE`` (test seeding): it moves no files, so the session reads
    misplaced until ``transcript_store.move_files_home``.
    """
    with db.transaction(data_dir) as conn:
        tid = _find_or_create_transcript(conn, stem)
        cid = cm._campaign_id(conn, slug)
        pos = conn.execute(
            "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
            (cid,),
        ).fetchone()[0]
        conn.execute("UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?",
                     (cid, pos, tid))
    return tid


def remove_from_campaign(stem: str, data_dir: Optional[Path] = None) -> int:
    """Unassign the session named ``stem`` (any campaign); returns its id.

    A direct ``UPDATE`` (test seeding): it moves no files.
    """
    with db.transaction(data_dir) as conn:
        tid = _find_or_create_transcript(conn, stem)
        conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE id = ?",
                     (tid,))
    return tid


def assign_campaign(tid: int, slug: str, data_dir: Optional[Path] = None) -> None:
    """Attach an existing session id to ``slug`` (test seeding, moves no files)."""
    with db.transaction(data_dir) as conn:
        cid = cm._campaign_id(conn, slug)
        pos = conn.execute(
            "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
            (cid,),
        ).fetchone()[0]
        conn.execute("UPDATE transcripts SET campaign_id = ?, position = ? WHERE id = ?",
                     (cid, pos, tid))


def unassign_campaign(tid: int, data_dir: Optional[Path] = None) -> None:
    """Detach an existing session id from any campaign (test seeding, moves no files)."""
    with db.transaction(data_dir) as conn:
        conn.execute("UPDATE transcripts SET campaign_id = NULL, position = NULL WHERE id = ?",
                     (tid,))


def seed_sidecar(md_path: Path, diar: dict, data_dir: Optional[Path] = None) -> None:
    """Store a transcript's diarization data the way a transcription job does
    (``transcript_store.write_sidecar``): speakers and the audio path in the
    database, segments in ``<stem>_diar.json``. Registers the transcript.
    An ``input_path`` outside the transcript's folder is not tracked."""
    from wisper_transcribe.transcript_store import write_sidecar
    write_sidecar(Path(md_path), diar, data_dir)


def seed_file(path: Path, kind: str, owner, label: Optional[str] = None, *,
              data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> None:
    """A ``files`` row for ``path`` (which needn't exist yet)."""
    from wisper_transcribe import file_registry

    file_registry.add(Path(path), kind=kind, owner=owner, label=label,
                      data_dir=data_dir, output_dir=output_dir)
