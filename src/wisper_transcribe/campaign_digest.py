"""Campaign-level LLM digests: the combined summary and the "Previously on…"
recap.

Where ``journal.py`` accumulates one living document, this module produces two
bounded, whole-campaign artifacts from the per-session ``.summary.md`` sidecars:

- the **combined summary** — one LLM pass over every summarized session, in
  campaign order, written to ``<folder> Combined Summary.md`` (one per campaign,
  overwritten on each run). For retrospectives and onboarding.
- the **"Previously on…" recap** — 200–400 words, spoiler-free and player-facing,
  from the last 1–3 summarized sessions, written to
  ``<folder> Recap — <newest stem>.md``. One file per newest session, so history
  is kept; re-running for the same newest session replaces that one file.

Both are campaign-owned ``files`` rows (a folder rename carries them) and each
generation is recorded in ``campaign_digests`` with the sessions it covered.
The LLM call runs outside any transaction; the file is written with
``transcript_store.atomic_write_text`` and registered right after.

This module owns path resolution, session discovery, digest rows, and staleness.
The prompt builders and generation lives in :mod:`campaign_summaries`.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import campaign_folders, db, file_registry
from .campaign_manager import _validate_campaign_slug, get_transcripts_for_campaign
from .config import get_output_root
from .journal import _summary_path
from .transcript_store import nfc

COMBINED_SUMMARY = "combined_summary"
RECAP = "recap"
DIGEST_KINDS = (COMBINED_SUMMARY, RECAP)

# The recap is built from the last 1–3 summarized sessions (default 1).
RECAP_MIN_SESSIONS = 1
RECAP_MAX_SESSIONS = 3
RECAP_DEFAULT_SESSIONS = 1


class DigestLocationError(RuntimeError):
    """A digest can't be written where it belongs. The message is a fixed code."""


@dataclass(frozen=True)
class Digest:
    """One generated document: its file, when it was made, and what it covered."""
    id: int
    campaign_id: int
    kind: str
    file_id: int
    path: Path
    label: Optional[str]
    generated_at: str
    provider: str
    model: str
    sessions: list[str]


def clamp_recap_sessions(n: int) -> int:
    """``n`` sessions brought into the recap's 1–3 range (default 1)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return RECAP_DEFAULT_SESSIONS
    return max(RECAP_MIN_SESSIONS, min(RECAP_MAX_SESSIONS, n))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _digest_path(slug: str, kind: str, label: Optional[str] = None,
                 data_dir: Optional[Path] = None, *,
                 conn: Optional[sqlite3.Connection] = None,
                 output_dir: Optional[Path] = None) -> Optional[Path]:
    """A campaign digest's file, or None when there is none to name.

    That is the registered path, else the derived name when wisper has claimed
    the folder. None for an invalid slug or an unknown campaign. wisper never
    reads or deletes a file in a folder it hasn't claimed.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return None
    output = Path(output_dir) if output_dir is not None else get_output_root()

    def lookup(c: sqlite3.Connection) -> Optional[Path]:
        row = c.execute(
            "SELECT id, folder, folder_claimed FROM campaigns WHERE slug = ?", (safe,)
        ).fetchone()
        if row is None:
            return None
        registered = file_registry.file_for(
            file_registry.Owner("campaign", row["id"]), kind, label,
            conn=c, data_dir=data_dir, output_dir=output)
        if registered is not None:
            return registered.path
        if row["folder_claimed"]:
            folder = row["folder"]
            if kind == COMBINED_SUMMARY:
                name = campaign_folders.combined_summary_name(folder)
            else:
                if not label:
                    return None
                name = campaign_folders.recap_name(folder, label)
            return output / folder / name
        return None

    if conn is not None:
        return lookup(conn)
    with db.connection(data_dir) as c:
        return lookup(c)


def combined_summary_path(slug: str, data_dir: Optional[Path] = None, *,
                          conn: Optional[sqlite3.Connection] = None,
                          output_dir: Optional[Path] = None) -> Optional[Path]:
    """The campaign's combined-summary file, or None."""
    return _digest_path(slug, COMBINED_SUMMARY, None, data_dir,
                        conn=conn, output_dir=output_dir)


def recap_path(slug: str, stem: str, data_dir: Optional[Path] = None, *,
               conn: Optional[sqlite3.Connection] = None,
               output_dir: Optional[Path] = None) -> Optional[Path]:
    """The recap file for ``stem`` as its newest session, or None."""
    return _digest_path(slug, RECAP, nfc(stem), data_dir,
                        conn=conn, output_dir=output_dir)


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def _campaign_id(slug: str, data_dir: Optional[Path] = None) -> Optional[int]:
    with db.connection(data_dir) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else None


def summarized_sessions(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Every campaign session that has a ``.summary.md``, in campaign order.

    Mirrors ``journal.unjournalled_sessions``'s discovery, but returns all
    summarized sessions rather than the unfolded ones.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return []
    cid = _campaign_id(safe, data_dir)
    if cid is None:
        return []
    out: list[str] = []
    for stem in get_transcripts_for_campaign(safe, data_dir):
        summary = _summary_path(cid, stem, data_dir)
        if summary is not None and summary.exists():
            out.append(stem)
    return out


def recap_sessions(slug: str, n: int = RECAP_DEFAULT_SESSIONS,
                   data_dir: Optional[Path] = None) -> list[str]:
    """The last ``n`` summarized sessions (1–3), in campaign order."""
    return summarized_sessions(slug, data_dir)[-clamp_recap_sessions(n):]


# ---------------------------------------------------------------------------
# Digest rows
# ---------------------------------------------------------------------------

def _digest_rows(conn: sqlite3.Connection, cid: int, kind: str, *,
                 data_dir: Optional[Path] = None,
                 output_dir: Optional[Path] = None) -> list[Digest]:
    data = db._data_dir(data_dir)
    output = Path(output_dir) if output_dir is not None else get_output_root()
    rows = conn.execute(
        "SELECT d.id, d.campaign_id, d.kind, d.file_id, d.generated_at, d.provider, d.model, "
        "       f.root, f.rel_path, f.label "
        "FROM campaign_digests d JOIN files f ON f.id = d.file_id "
        "WHERE d.campaign_id = ? AND d.kind = ? "
        "ORDER BY d.generated_at DESC, d.id DESC",
        (cid, kind),
    ).fetchall()
    out: list[Digest] = []
    for r in rows:
        sessions = [s[0] for s in conn.execute(
            "SELECT t.stem FROM campaign_digest_sessions ds "
            "JOIN transcripts t ON t.id = ds.transcript_id "
            "WHERE ds.digest_id = ? ORDER BY t.position",
            (r["id"],),
        )]
        root_dir = output if r["root"] == "output" else data
        out.append(Digest(
            id=r["id"], campaign_id=r["campaign_id"], kind=r["kind"],
            file_id=r["file_id"], path=db.from_rel(r["rel_path"], root_dir),
            label=r["label"], generated_at=r["generated_at"],
            provider=r["provider"], model=r["model"], sessions=sessions,
        ))
    return out


def list_recaps(slug: str, data_dir: Optional[Path] = None) -> list[Digest]:
    """A campaign's recaps, newest first."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return []
    cid = _campaign_id(safe, data_dir)
    if cid is None:
        return []
    with db.connection(data_dir) as conn:
        return _digest_rows(conn, cid, RECAP, data_dir=data_dir)


def combined_summary_digest(slug: str, data_dir: Optional[Path] = None) -> Optional[Digest]:
    """The campaign's combined-summary digest, or None."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return None
    cid = _campaign_id(safe, data_dir)
    if cid is None:
        return None
    with db.connection(data_dir) as conn:
        rows = _digest_rows(conn, cid, COMBINED_SUMMARY, data_dir=data_dir)
    return rows[0] if rows else None


def record_digest(slug: str, kind: str, file_id: int, session_stems: list[str],
                  provider: str = "", model: str = "",
                  data_dir: Optional[Path] = None) -> int:
    """Record ``file_id`` and the sessions it covered, in one transaction.

    Re-recording a digest for the same file (a rerun) replaces the prior row and
    its session links. Returns the new digest id.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise ValueError(f"Invalid campaign slug: {slug!r}")
    if kind not in DIGEST_KINDS:
        raise ValueError(f"Unknown digest kind: {kind!r}")
    with db.transaction(data_dir) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()
        if row is None:
            raise KeyError(f"Campaign {safe!r} not found")
        cid = row[0]
        conn.execute("DELETE FROM campaign_digests WHERE file_id = ?", (file_id,))
        did = conn.execute(
            "INSERT INTO campaign_digests (campaign_id, kind, file_id, generated_at, provider, model) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (cid, kind, file_id, db.now_utc(), provider, model),
        ).fetchone()[0]
        for stem in session_stems:
            member = conn.execute(
                "SELECT id FROM transcripts WHERE campaign_id = ? AND stem = ?",
                (cid, nfc(stem)),
            ).fetchone()
            if member is None:
                continue
            conn.execute(
                "INSERT INTO campaign_digest_sessions (digest_id, transcript_id) "
                "VALUES (?, ?) ON CONFLICT DO NOTHING",
                (did, member[0]),
            )
    return did


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def _mtime_stamp(path: Path) -> Optional[str]:
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def combined_summary_stale_since(slug: str,
                                 data_dir: Optional[Path] = None) -> Optional[str]:
    """The baseline time a combined summary is out of date from, or None.

    Stale when the campaign's summarized sessions differ from the digest's
    session set, or a covered session's ``.summary.md`` changed after the digest
    was generated. Returns the digest's generated-at time when stale (the point
    after which the input diverged), else None. No digest means nothing to mark
    stale.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return None
    digest = combined_summary_digest(safe, data_dir)
    if digest is None:
        return None
    current = summarized_sessions(safe, data_dir)
    if set(current) != set(digest.sessions):
        return digest.generated_at
    cid = _campaign_id(safe, data_dir)
    if cid is None:
        return None
    for stem in digest.sessions:
        summary = _summary_path(cid, stem, data_dir)
        if summary is None:
            continue
        stamp = _mtime_stamp(summary)
        if stamp is not None and stamp > digest.generated_at:
            return digest.generated_at
    return None
