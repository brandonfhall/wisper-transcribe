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
    from .config import get_output_root

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
        output_dir = get_output_root()  # not created: a missing folder just flags rows missing
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


def import_journal_entries(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """v3: each journal's ``journaled_sessions`` frontmatter → ``journal_entries``.

    Records the journal's current hash, so a later edit in Obsidian is
    recognised as an edit, not a crash. ``journal.md`` itself is not
    rewritten: new folds simply stop writing ``journaled_sessions``. A listed
    session that isn't in that campaign (deleted, moved, or never assigned)
    is dropped and reported; the composite foreign key would reject it.
    """
    import hashlib

    import yaml

    for campaign_id, slug in conn.execute("SELECT id, slug FROM campaigns").fetchall():
        jpath = ctx.data_dir / "campaigns" / slug / "journal.md"
        if not jpath.is_file():
            continue
        raw = jpath.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        meta: dict = {}
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    ctx.note(f"campaign {slug!r}: journal frontmatter unreadable; no sessions marked as folded")
        if not isinstance(meta, dict):
            meta = {}
        folded_at = _as_utc(meta.get("updated_at")) or _file_timestamp(jpath)
        for raw_stem in meta.get("journaled_sessions") or []:
            stem = unicodedata.normalize("NFC", str(raw_stem))
            row = conn.execute(
                "SELECT ct.transcript_id FROM campaign_transcripts ct "
                "JOIN transcripts t ON t.id = ct.transcript_id "
                "WHERE ct.campaign_id = ? AND t.stem = ?",
                (campaign_id, stem),
            ).fetchone()
            if row is None:
                ctx.note(f"campaign {slug!r}: journaled session {stem!r} is not in the campaign; dropped")
                continue
            conn.execute(
                "INSERT OR IGNORE INTO journal_entries (transcript_id, campaign_id, folded_at) "
                "VALUES (?, ?, ?)",
                (row[0], campaign_id, folded_at),
            )
        conn.execute(
            "UPDATE campaigns SET journal_sha256 = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), campaign_id),
        )


def _as_utc(value: object) -> str | None:
    """A frontmatter ``updated_at`` (local ISO time or datetime) as a UTC timestamp."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = local time, as render_journal() wrote it
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_diarization_sidecars(conn: sqlite3.Connection, ctx: MigrationContext) -> None:
    """v4: each ``<stem>_diar.json`` in the output root → ``transcript_speakers``
    and ``transcripts.audio_rel_path``; the file keeps only its segments.

    The old ``campaign`` key associates a transcript with no campaign when
    that campaign exists. Sidecars are backed up first and slimmed after the
    commit. A sidecar with neither a ``.md`` nor a registry row is an orphan
    and left alone; an unreadable one is skipped. Everything repaired or
    dropped is reported.
    """
    import json
    import os
    import re as _re

    from .config import get_output_root

    output_dir = get_output_root()  # never created here: it may be an unmounted drive
    if not output_dir.is_dir():
        # Fresh install, or an unmounted drive: read_sidecar() falls back to
        # the old fields still in each file, and the next write moves them.
        import logging
        logging.getLogger(__name__).info("No transcripts folder at %s; no sidecars to import", output_dir)
        return
    sidecars = sorted(output_dir.glob("*_diar.json"))
    if not sidecars:
        return
    ctx.backup_legacy(sidecars, root=output_dir, into="output")
    auto_name = _re.compile(r"^(SPEAKER_\d+|Unknown Speaker \d+|Recurring Speaker \d+)$")
    root = os.path.realpath(output_dir)
    slim: list[tuple[Path, list]] = []

    for path in sidecars:
        stem = unicodedata.normalize("NFC", path.name[: -len("_diar.json")])
        try:
            diar = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(diar, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            ctx.note(f"sidecar {path.name}: unreadable; skipped (left as it was)")
            continue
        row = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()
        if row is None:
            if not (output_dir / f"{stem}.md").is_file():
                continue  # orphan companion; reconcile's sweep decides
            row = (ensure_transcript_row(conn, stem, output_dir),)
        tid = row[0]

        segments = diar.get("diarization_segments")
        speaker_map = diar.get("speaker_map") or {}
        sources = diar.get("speaker_map_source") or {}
        space = diar.get("embedding_space")
        stored = diar.get("speaker_embeddings") if space else None
        embeddings: dict[str, np.ndarray] = {}
        if isinstance(stored, dict):
            for label, vec in stored.items():
                try:
                    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
                except (TypeError, ValueError):
                    arr = np.array([], dtype=np.float32)
                if arr.size:
                    embeddings[str(label)] = arr
                else:
                    ctx.note(f"transcript {stem!r}: speaker {label!r} embedding unreadable; dropped")
        if not isinstance(speaker_map, dict):
            speaker_map = {}
        for label in sorted({str(k) for k in speaker_map} | set(embeddings)):
            name = str(speaker_map.get(label, label))
            source = sources.get(label) if isinstance(sources, dict) else None
            if source not in ("auto", "manual"):
                source = "auto" if auto_name.match(name) else "manual"
            emb = embeddings.get(label)
            conn.execute(
                "INSERT OR REPLACE INTO transcript_speakers (transcript_id, label, display_name, "
                "source, embedding, embedding_space) VALUES (?, ?, ?, ?, ?, ?)",
                (tid, label, name, source,
                 None if emb is None else emb.tobytes(), None if emb is None else str(space)),
            )

        input_path = diar.get("input_path")
        if input_path:
            real = os.path.realpath(str(input_path))
            if real.startswith(root + os.sep) and os.path.isfile(real):
                rel = Path(os.path.relpath(real, root)).as_posix()
                conn.execute("UPDATE transcripts SET audio_rel_path = ? WHERE id = ?", (rel, tid))
            else:
                ctx.note(f"transcript {stem!r}: source audio is not in the transcripts folder "
                         "(or is gone); speaker enrollment from it is unavailable")

        slug = diar.get("campaign")
        if slug:
            in_campaign = conn.execute(
                "SELECT 1 FROM campaign_transcripts WHERE transcript_id = ?", (tid,)
            ).fetchone()
            campaign = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (str(slug),)).fetchone()
            if in_campaign is None and campaign is not None:
                conn.execute(
                    "INSERT INTO campaign_transcripts (transcript_id, campaign_id, position) VALUES "
                    "(?, ?, (SELECT coalesce(max(position), -1) + 1 FROM campaign_transcripts "
                    "WHERE campaign_id = ?))",
                    (tid, campaign[0], campaign[0]),
                )
                ctx.note(f"transcript {stem!r}: added to campaign {slug!r} (from its sidecar)")
        slim.append((path, segments if isinstance(segments, list) else []))

    def _slim() -> None:
        from .transcript_store import atomic_write_text
        for path, segments in slim:
            atomic_write_text(path, json.dumps({"diarization_segments": segments}, indent=2))

    ctx.after_commit.append(_slim)
