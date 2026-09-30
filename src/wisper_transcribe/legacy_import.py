"""One-time import of the pre-SQLite JSON stores into ``wisper.db``.

Each function runs inside its migration's ``BEGIN IMMEDIATE`` transaction
(see ``db.migrate``), with foreign keys deferred. Existing installs contain
exactly what the new constraints forbid, so dirty data is repaired or skipped
and always reported (``ctx.note``) instead of aborting. Only an unreadable
top-level store aborts, because importing an empty store would lose data.

These importers are frozen once the migration ships: they describe the old
file formats, not the current code.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .db import MigrationContext, MigrationFailed, now_utc

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DIGITS_RE = re.compile(r"^\d+$")


def _read_store(path: Path, ctx: MigrationContext) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level is not an object")
        return raw
    except (OSError, ValueError) as exc:
        where = f" A copy is in {ctx.backup_dir}." if ctx.backup_dir else ""
        raise MigrationFailed(
            f"Could not read {path} ({exc}). Nothing was changed.{where} "
            "Fix or remove the file, then start wisper again."
        ) from exc


def _as_timestamp(value: object) -> str | None:
    """A legacy ``YYYY-MM-DD`` date as the schema's UTC timestamp."""
    if isinstance(value, str) and _DATE_RE.match(value):
        return f"{value}T00:00:00Z"
    return None


def _file_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_npy(path: Path) -> np.ndarray | None:
    try:
        vec = np.load(str(path), allow_pickle=False)
    except (OSError, ValueError):
        return None
    vec = np.asarray(vec, dtype=np.float32).reshape(-1)
    return vec if vec.size else None


def ensure_transcript_row(conn: sqlite3.Connection, stem: str, output_dir: Path) -> int:
    """Registry row for ``stem``, created (flagged missing if no ``.md``) if absent."""
    row = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()
    if row is not None:
        return row[0]
    md = output_dir / f"{stem}.md"
    exists = md.is_file()
    now = now_utc()
    return conn.execute(
        "INSERT INTO transcripts (stem, created_at, missing_since) VALUES (?, ?, ?) RETURNING id",
        (stem, _file_timestamp(md) if exists else now, None if exists else now),
    ).fetchone()[0]


def import_profiles_and_campaigns(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """v2: ``speakers.json`` + ``.npy`` embeddings, and ``campaigns.json``."""
    from .path_utils import get_output_dir

    data_dir = ctx.data_dir
    profiles_dir = data_dir / "profiles"
    speakers_json = profiles_dir / "speakers.json"
    campaigns_json = data_dir / "campaigns" / "campaigns.json"
    npys = sorted((profiles_dir / "embeddings").glob("*.npy"))
    legacy = [p for p in (speakers_json, campaigns_json) if p.exists()] + npys
    if not legacy:
        return
    ctx.backup_legacy(legacy)

    # --- profiles ---------------------------------------------------------
    profile_ids: dict[str, int] = {}
    if speakers_json.exists():
        profiles_root = os.path.realpath(profiles_dir)
        for key, data in _read_store(speakers_json, ctx).items():
            if not isinstance(data, dict) or not key:
                ctx.note(f"speaker {key!r}: unreadable entry skipped")
                continue
            embedding = None
            emb_file = data.get("embedding_file") or f"embeddings/{key}.npy"
            emb_path = os.path.realpath(profiles_dir / emb_file)
            if emb_path.startswith(profiles_root + os.sep) and os.path.isfile(emb_path):
                embedding = _load_npy(Path(emb_path))
            if embedding is None:
                ctx.note(f"speaker {key!r}: no readable embedding; re-enroll to match this speaker")
            space = data.get("embedding_space") or ""  # untagged = older model
            enrolled = data.get("enrolled_date")
            profile_ids[key] = conn.execute(
                "INSERT INTO profiles (key, display_name, role, notes, enrolled_date, "
                "enrollment_source, embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "RETURNING id",
                (
                    key,
                    str(data.get("display_name") or key),
                    str(data.get("role") or ""),
                    str(data.get("notes") or ""),
                    enrolled if isinstance(enrolled, str) else "",
                    str(data.get("enrollment_source") or ""),
                    None if embedding is None else embedding.tobytes(),
                    None if embedding is None else space,
                ),
            ).fetchone()[0]

    # --- campaigns --------------------------------------------------------
    if campaigns_json.exists():
        output_dir = get_output_dir()
        claimed: dict[str, str] = {}  # stem -> slug that kept it
        for slug, data in _read_store(campaigns_json, ctx).items():
            if not isinstance(data, dict) or not slug:
                ctx.note(f"campaign {slug!r}: unreadable entry skipped")
                continue
            created = _as_timestamp(data.get("created"))
            if created is None:
                created = now_utc()
                ctx.note(f"campaign {slug!r}: no valid created date; set to today")
            campaign_id = conn.execute(
                "INSERT INTO campaigns (slug, display_name, created_at) VALUES (?, ?, ?) RETURNING id",
                (slug, str(data.get("display_name") or slug), created),
            ).fetchone()[0]

            bound: dict[str, str] = {}  # discord id -> member that kept it
            for key, m in (data.get("members") or {}).items():
                if key not in profile_ids:
                    ctx.note(f"campaign {slug!r}: member {key!r} has no speaker profile; dropped")
                    continue
                m = m if isinstance(m, dict) else {}
                discord_id = str(m.get("discord_user_id") or "").strip() or None
                if discord_id is not None and not _DIGITS_RE.match(discord_id):
                    ctx.note(f"campaign {slug!r}: member {key!r} Discord id {discord_id!r} is not numeric; cleared")
                    discord_id = None
                if discord_id is not None and discord_id in bound:
                    ctx.note(
                        f"campaign {slug!r}: Discord id {discord_id} bound to both "
                        f"{bound[discord_id]!r} and {key!r}; kept on {bound[discord_id]!r}"
                    )
                    discord_id = None
                if discord_id is not None:
                    bound[discord_id] = key
                conn.execute(
                    "INSERT INTO campaign_members (campaign_id, profile_id, role, character, "
                    "discord_user_id) VALUES (?, ?, ?, ?, ?)",
                    (campaign_id, profile_ids[key], str(m.get("role") or ""),
                     str(m.get("character") or ""), discord_id),
                )

            position = 0
            for raw_stem in data.get("transcripts") or []:
                stem = unicodedata.normalize("NFC", str(raw_stem))
                if not stem or "/" in stem or "\\" in stem:
                    ctx.note(f"campaign {slug!r}: transcript {raw_stem!r} is not a valid name; dropped")
                    continue
                if stem in claimed:
                    if claimed[stem] != slug:
                        ctx.note(
                            f"transcript {stem!r} is in campaigns {claimed[stem]!r} and "
                            f"{slug!r}; kept in {claimed[stem]!r}"
                        )
                    continue
                claimed[stem] = slug
                transcript_id = ensure_transcript_row(conn, stem, output_dir)
                conn.execute(
                    "INSERT INTO campaign_transcripts (transcript_id, campaign_id, position) "
                    "VALUES (?, ?, ?)",
                    (transcript_id, campaign_id, position),
                )
                position += 1

    def _delete_legacy() -> None:
        for path in legacy:
            path.unlink(missing_ok=True)

    ctx.after_commit.append(_delete_legacy)
