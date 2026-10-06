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

This module owns path resolution, session discovery, digest rows, staleness,
the prompt builders, and generation.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from . import campaign_folders, db, file_registry
from .campaign_manager import _validate_campaign_slug, get_transcripts_for_campaign
from .config import get_output_root
from .journal import _check_campaign, _strip_code_fence, _summary_path
from .llm import LLMClient
from .models import SpeakerProfile
from .transcript_store import atomic_write_text, nfc

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


def digest_for_file(slug: str, file_id: int,
                    data_dir: Optional[Path] = None) -> Optional[Digest]:
    """The digest whose file is ``file_id`` and belongs to ``slug``, or None.

    The route layer uses this to serve a recap by its ``files.id`` (an integer
    path parameter, so no name reaches a path); a file id from another campaign
    returns None.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return None
    cid = _campaign_id(safe, data_dir)
    if cid is None:
        return None
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT 1 FROM campaign_digests WHERE campaign_id = ? AND file_id = ?",
            (cid, file_id),
        ).fetchone()
        if row is None:
            return None
        for kind in DIGEST_KINDS:
            for digest in _digest_rows(conn, cid, kind, data_dir=data_dir):
                if digest.file_id == file_id:
                    return digest
    return None


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


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def render_digest(slug: str, kind: str, body: str, provider: str, model: str,
                  sessions: list[str]) -> str:
    """Render a digest markdown: YAML frontmatter + body, like the journal."""
    meta = {
        "type": "campaign-combined-summary" if kind == COMBINED_SUMMARY else "campaign-recap",
        "campaign": slug,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
        "sessions": list(sessions),
    }
    fm = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False,
                        allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_COMBINED_SYSTEM_PROMPT = (
    "You are the campaign archivist for an ongoing tabletop RPG actual-play. "
    "You will be given the enrolled speaker roster and the summaries of every "
    "session so far, in order. Produce ONE combined campaign summary in "
    "Markdown — a self-contained retrospective for a new or returning reader.\n\n"
    "Rules:\n"
    " - Keep these sections: '## The Story So Far', '## The Party', "
    "'## Major NPCs', '## Open Threads', '## Loot & Resources'.\n"
    " - The Story So Far: the through-line across all sessions, in order.\n"
    " - Open Threads: unresolved plot hooks; move resolved ones into the story.\n"
    " - Do NOT invent events that are not supported by the session summaries.\n"
    " - Output ONLY the summary Markdown body — no YAML frontmatter and no code "
    "fences."
)

_RECAP_SYSTEM_PROMPT = (
    "You write the spoiler-free 'Previously on…' recap shown to players at the "
    "start of a tabletop RPG session. You will be given the enrolled speaker "
    "roster and the summaries of the most recent sessions, in order.\n\n"
    "Rules:\n"
    " - Player-facing: 200 to 400 words of prose. Write it as a short "
    "voice-over recap of what the party did, in order.\n"
    " - SPOILER-FREE: exclude DM-only material — secret plans, hidden "
    "identities, unrevealed villains, and future plot. Include only what the "
    "players already witnessed at the table.\n"
    " - No lists, headings, or code fences; plain paragraphs.\n"
    " - Do NOT invent events that are not supported by the session summaries.\n"
    " - Output ONLY the recap prose."
)


def _roster_lines(profiles: dict[str, SpeakerProfile]) -> str:
    if not profiles:
        return "(no speakers enrolled)"
    out = []
    for p in profiles.values():
        role = f" [{p.role}]" if p.role else ""
        note = f" — {p.notes}" if p.notes else ""
        out.append(f"- {p.display_name}{role}{note}")
    return "\n".join(out)


def _session_block(stem: str, summary_md: str) -> str:
    return f"=== SESSION — {stem} ===\n{summary_md.strip()}"


def combined_summary_prompt(session_materials: list[tuple[str, str]],
                            profiles: dict[str, SpeakerProfile]) -> str:
    """The user prompt for the combined summary over every summarized session."""
    blocks = "\n\n".join(_session_block(stem, md) for stem, md in session_materials)
    return (
        f"Enrolled speakers (the players):\n{_roster_lines(profiles)}\n\n"
        f"Every session so far, in campaign order:\n\n{blocks}"
    )


def recap_prompt(session_materials: list[tuple[str, str]],
                 profiles: dict[str, SpeakerProfile]) -> str:
    """The user prompt for the player-facing recap of the latest session(s)."""
    blocks = "\n\n".join(_session_block(stem, md) for stem, md in session_materials)
    return (
        f"Enrolled speakers (the players):\n{_roster_lines(profiles)}\n\n"
        f"The most recent session(s), in order:\n\n{blocks}\n\n"
        f"Write the 200–400 word spoiler-free 'Previously on…' recap."
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

@dataclass
class DigestResult:
    """Outcome of a single combined-summary or recap generation."""
    path: Path
    kind: str
    sessions: list[str]
    provider: str
    model: str
    replaced: bool = False


def _materials(safe: str, cid: int, stems: list[str],
               data_dir: Optional[Path]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for stem in stems:
        summary = _summary_path(cid, stem, data_dir)
        if summary is not None and summary.exists():
            out.append((stem, summary.read_text(encoding="utf-8")))
    return out


def _generate(slug: str, kind: str, target: Path, stems: list[str], *, label: Optional[str],
              system_prompt: str, user_prompt: str, client: LLMClient,
              data_dir: Optional[Path], output_dir: Optional[Path]) -> DigestResult:
    """The common write path: run the LLM, write the file, register, record."""
    body = _strip_code_fence(client.complete(system_prompt, user_prompt))
    rendered = render_digest(slug, kind, body, getattr(client, "provider", ""),
                             getattr(client, "model", ""), stems)
    target.parent.mkdir(parents=True, exist_ok=True)
    replaced = target.exists()
    atomic_write_text(target, rendered)

    owner = file_registry.Owner.for_campaign_slug(slug, data_dir=data_dir)
    with db.transaction(data_dir) as conn:
        file_registry.add_if_owned(target, kind=kind, owner=owner, label=label,
                                   conn=conn, data_dir=data_dir, output_dir=output_dir)
    # Re-read the file row so the digest links to the row's id, not a guess.
    row = file_registry.file_for(owner, kind, label, data_dir=data_dir,
                                 output_dir=output_dir)
    if row is not None:
        record_digest(slug, kind, row.id, stems,
                      getattr(client, "provider", ""), getattr(client, "model", ""),
                      data_dir=data_dir)
    return DigestResult(path=target, kind=kind, sessions=list(stems),
                        provider=getattr(client, "provider", ""),
                        model=getattr(client, "model", ""), replaced=replaced)


def _prepare(slug: str, data_dir: Optional[Path]) -> tuple[str, int, Path]:
    """Validate the campaign and resolve its folder; returns (slug, id, folder)."""
    safe = _check_campaign(slug, data_dir)
    output = get_output_root()
    with db.connection(data_dir) as conn:
        cid = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()["id"]
    try:
        folder = campaign_folders.ensure_folder(cid, data_dir=data_dir, output_dir=output)
    except campaign_folders.FolderTakenError:
        raise DigestLocationError("digest_folder_taken") from None
    except FileNotFoundError:
        raise DigestLocationError("digest_output_unavailable") from None
    return safe, cid, folder


def generate_combined_summary(slug: str, client: LLMClient,
                              profiles: dict[str, SpeakerProfile], *,
                              data_dir: Optional[Path] = None) -> Optional[DigestResult]:
    """Summarize every summarized session into the campaign's combined summary.

    Returns the result, or None when the campaign has no summarized session.

    Raises:
        ValueError: invalid slug.
        KeyError: campaign not found.
        DigestLocationError: the folder or output root is unusable.
        LLMUnavailableError / LLMResponseError: provider failure (propagated).
    """
    safe, cid, folder = _prepare(slug, data_dir)
    stems = summarized_sessions(safe, data_dir)
    if not stems:
        return None
    materials = _materials(safe, cid, stems, data_dir)
    if not materials:
        return None
    output = get_output_root()
    target = (combined_summary_path(safe, data_dir, output_dir=output)
              or folder / campaign_folders.combined_summary_name(folder.name))
    return _generate(safe, COMBINED_SUMMARY, target, stems, label=None,
                     system_prompt=_COMBINED_SYSTEM_PROMPT,
                     user_prompt=combined_summary_prompt(materials, profiles),
                     client=client, data_dir=data_dir, output_dir=output)


def generate_recap(slug: str, client: LLMClient,
                   profiles: dict[str, SpeakerProfile], *,
                   sessions: int = RECAP_DEFAULT_SESSIONS,
                   data_dir: Optional[Path] = None) -> Optional[DigestResult]:
    """Write a player-facing recap from the last 1–3 summarized sessions.

    Returns the result, or None when no session is summarized.

    Raises:
        ValueError: invalid slug.
        KeyError: campaign not found.
        DigestLocationError: the folder or output root is unusable.
        LLMUnavailableError / LLMResponseError: provider failure (propagated).
    """
    safe, cid, folder = _prepare(slug, data_dir)
    stems = recap_sessions(safe, sessions, data_dir)
    if not stems:
        return None
    materials = _materials(safe, cid, stems, data_dir)
    if not materials:
        return None
    output = get_output_root()
    newest = stems[-1]
    label = nfc(newest)
    target = (recap_path(safe, newest, data_dir, output_dir=output)
              or folder / campaign_folders.recap_name(folder.name, newest))
    return _generate(safe, RECAP, target, stems, label=label,
                     system_prompt=_RECAP_SYSTEM_PROMPT,
                     user_prompt=recap_prompt(materials, profiles),
                     client=client, data_dir=data_dir, output_dir=output)
