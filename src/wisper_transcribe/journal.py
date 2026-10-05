"""Rolling campaign journal — a living per-campaign document the LLM rewrites
as each new session summary is folded in.

This is the campaign-level counterpart to per-session ``wisper summarize``.
Where ``summarize.py`` turns one transcript into one ``<stem>.summary.md``,
this module accumulates those session summaries into a single
``<output root>/<folder>/<folder> Journal.md`` in the campaign's folder, a file
that grows with the campaign.

Design — bounded context:
    On each fold the LLM receives only ``[current journal] + [one new session
    summary]`` and returns a rewritten journal. Even at session 50 the prompt
    stays ~2–5 k tokens, so cost/latency do not grow with campaign length.

The session ``.summary.md`` sidecars (written by ``summarize``) are the source
of truth and are never modified by a fold. Which sessions the journal has
absorbed lives in the database (``journal_entries``), so re-running folds only
what is new, and a session that leaves the campaign drops its entry and marks
the journal stale (``campaigns.journal_stale_since``) instead of silently
staying in the text.

Journal write rule (the body is a file, the entries are rows):
    1. The LLM call runs outside any transaction.
    2. The new body goes to ``<folder> Journal.md.wisper-pending``.
    3. One transaction inserts the entry and sets ``campaigns.journal_sha256``
       to the pending file's hash — the fold's commit point.
    4. ``os.replace`` moves the pending file into place.
    On the next read, a pending file whose hash matches ``journal_sha256`` was
    committed but not moved (crash between 3 and 4) and is moved into place;
    any other pending file predates a commit and is deleted. A missing
    journal file in a claimed folder that exists resets the entries (fresh
    start); a present one with a different hash was edited by the user, which
    is allowed. An absent folder or output root means "unavailable", never
    "deleted".

A journal from an older install sits at ``<data>/campaigns/<slug>/journal.md``.
:func:`adopt_legacy_journal` moves it into the campaign folder the first time
the journal is read, and keeps the old file as ``journal.md.v11-adopted``.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import campaign_folders, db, file_registry
from .campaign_manager import (
    _validate_campaign_slug,
    get_campaigns_dir,
    get_transcripts_for_campaign,
    load_campaigns,
)
from .llm import LLMClient
from .models import SpeakerProfile
from .config import get_output_root
from .transcript_store import TEMP_PREFIX, atomic_write_text, find_by_stem

log = logging.getLogger(__name__)

PENDING_SUFFIX = ".wisper-pending"
ADOPTED_SUFFIX = ".v11-adopted"

# Strip a wrapping markdown code fence with any (or no) language tag — e.g.
# ```markdown … ``` or ``` … ```. The prompt tells the model not to use one,
# but models do anyway; this is the defensive cleanup.
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text

_SYSTEM_PROMPT = (
    "You are the campaign archivist for an ongoing tabletop RPG actual-play. "
    "You maintain ONE living campaign journal in Markdown that is rewritten "
    "each time a new session is folded in. You will be given the enrolled "
    "speaker roster, the current journal, and the summary of the newest "
    "session. Produce the updated journal.\n\n"
    "Rules:\n"
    " - Preserve everything from the existing journal that is still relevant; "
    "integrate the new session rather than appending it verbatim.\n"
    " - Keep these sections: '## Story So Far', '## Active Threads', "
    "'## NPCs', '## Party & Decisions', '## Loot & Resources'.\n"
    " - Active Threads: track open plot hooks; move resolved ones into Story "
    "So Far. NPCs: note role and how the relationship has evolved. Party & "
    "Decisions: consequential PC choices. Loot & Resources: a running ledger.\n"
    " - Do NOT invent events that are not supported by the session material.\n"
    " - Output ONLY the journal Markdown body — no YAML frontmatter, no code "
    "fences, no preamble."
)


class JournalLocationError(RuntimeError):
    """The journal can't be written where it belongs. The message is a fixed
    code, which a job reports as its error."""


_ADOPT_FAILURES = {
    "folder_taken": "journal_folder_taken",
    "unavailable": "journal_output_unavailable",
    "kept": "journal_legacy_pending",
    "failed": "journal_legacy_pending",
}


@dataclass
class JournalResult:
    """Outcome of a single ``update_journal`` fold."""
    path: Path
    folded: str                 # the session stem just folded in
    journaled_sessions: list[str]
    provider: str
    model: str


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def journal_path(slug: str, data_dir: Optional[Path] = None, *,
                 conn: Optional[sqlite3.Connection] = None,
                 output_dir: Optional[Path] = None) -> Optional[Path]:
    """The campaign's journal file, or None when there is none to name.

    That is the registered path, else ``<folder>/<folder> Journal.md`` when
    wisper has claimed the folder. wisper never reads, adopts, or deletes a
    file in a folder it hasn't claimed. None also for an invalid slug or an
    unknown campaign.
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
            file_registry.Owner("campaign", row["id"]), "journal", conn=c,
            data_dir=data_dir, output_dir=output)
        if registered is not None:
            return registered.path
        if row["folder_claimed"]:
            return output / row["folder"] / campaign_folders.journal_name(row["folder"])
        return None

    if conn is not None:
        return lookup(conn)
    with db.connection(data_dir) as c:
        return lookup(c)


def legacy_journal_path(slug: str, data_dir: Optional[Path] = None) -> Path:
    """Where an older install kept the journal, in the data dir."""
    return get_campaigns_dir(data_dir) / slug / "journal.md"


def _summary_path(campaign_id: int, stem: str, data_dir: Optional[Path] = None) -> Optional[Path]:
    """The ``<stem>.summary.md`` sidecar of a campaign's session, or None."""
    found = find_by_stem(stem, campaign_id=campaign_id, data_dir=data_dir)
    if not found:
        return None
    return found[0].companion(".summary.md")


def _transcript_path(campaign_id: int, stem: str, data_dir: Optional[Path] = None) -> Optional[Path]:
    """The ``<stem>.md`` path of a campaign's session, or None."""
    found = find_by_stem(stem, campaign_id=campaign_id, data_dir=data_dir)
    return found[0].md if found else None


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def parse_journal(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from the journal body. Returns (metadata, body)."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return meta, parts[2].strip()
            except Exception:
                pass
    return {}, text.strip()


def render_journal(slug: str, body: str, provider: str, model: str,
                   journaled_sessions: Optional[list[str]] = None) -> str:
    """Render the journal markdown: YAML frontmatter + body.

    ``journaled_sessions`` lives in the database; pass it only for an export
    (:func:`export_journal`), never for the file wisper maintains.
    """
    meta = {
        "type": "campaign-journal",
        "campaign": slug,
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
    }
    if journaled_sessions is not None:
        meta["journaled_sessions"] = list(journaled_sessions)
    fm = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False,
                        allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Journal file ↔ database consistency
# ---------------------------------------------------------------------------

def _pending_path(jpath: Path) -> Path:
    return jpath.with_name(jpath.name + PENDING_SUFFIX)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_into_place(source: Path, target: Path) -> bool:
    """Copy ``source`` to ``target`` through a verified temp file in ``target``'s
    folder. The data dir and the output root can be different volumes."""
    tmp = target.with_name(f"{TEMP_PREFIX}{target.name}.{os.getpid()}")
    try:
        with open(source, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
            fout.flush()
            os.fsync(fout.fileno())
        if _sha256(tmp) != _sha256(source):
            raise OSError("copied journal does not match its source")
        try:
            os.replace(tmp, target)
        except OSError:
            target.write_bytes(tmp.read_bytes())
            tmp.unlink(missing_ok=True)
        return True
    except OSError as exc:
        log.warning("Could not copy %s to %s: %s", source, target, exc)
        tmp.unlink(missing_ok=True)
        return False


def adopt_legacy_journal(slug: str, data_dir: Optional[Path] = None,
                         output_dir: Optional[Path] = None) -> str:
    """Move ``<data>/campaigns/<slug>/journal.md`` into the campaign folder.

    Call with no transaction open. Returns ``"none"`` (nothing to adopt),
    ``"adopted"``, ``"kept"`` (the folder holds a different journal; nothing
    changed), ``"folder_taken"``, ``"unavailable"`` (output root absent), or
    ``"failed"`` (the legacy file stays). The old file is set aside as
    ``journal.md.v11-adopted``.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return "none"
    legacy = legacy_journal_path(safe, data_dir)
    legacy_pending = _pending_path(legacy)
    if not legacy.is_file() and not legacy_pending.is_file():
        return "none"
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT id, journal_sha256 FROM campaigns WHERE slug = ?", (safe,)
        ).fetchone()
    if row is None:
        return "none"
    source = legacy if legacy.is_file() else None
    if legacy_pending.is_file():
        # A pending file matching the stored hash is a fold that committed before its move.
        if row["journal_sha256"] and _sha256(legacy_pending) == row["journal_sha256"]:
            source = legacy_pending
        else:
            legacy_pending.unlink(missing_ok=True)
    if source is None:
        return "none"
    output = Path(output_dir) if output_dir is not None else get_output_root()
    try:
        folder = campaign_folders.ensure_folder(row["id"], data_dir=data_dir, output_dir=output)
    except campaign_folders.FolderTakenError:
        return "folder_taken"
    except FileNotFoundError:
        return "unavailable"
    target = folder / campaign_folders.journal_name(folder.name)
    if target.exists():
        existing = _sha256(target)
        if existing != _sha256(source) and existing != row["journal_sha256"]:
            log.warning("Journal for %s not moved: %s already exists and differs", safe, target)
            return "kept"
    elif not _copy_into_place(source, target):
        return "failed"
    file_registry.add_if_owned(
        target, kind="journal", owner=file_registry.Owner("campaign", row["id"]),
        data_dir=data_dir, output_dir=output)
    try:
        os.replace(source, legacy.with_name(legacy.name + ADOPTED_SUFFIX))
        legacy.unlink(missing_ok=True)
        legacy_pending.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not set aside %s: %s", legacy, exc)
        return "failed"
    return "adopted"


def adopt_legacy_journals(data_dir: Optional[Path] = None,
                          output_dir: Optional[Path] = None) -> dict[str, str]:
    """:func:`adopt_legacy_journal` for every campaign. Never raises, so startup
    can't fail on it."""
    results: dict[str, str] = {}
    try:
        with db.connection(data_dir) as conn:
            slugs = [r[0] for r in conn.execute("SELECT slug FROM campaigns ORDER BY id")]
    except sqlite3.Error:
        log.warning("Could not list campaigns to adopt journals", exc_info=True)
        return results
    for slug in slugs:
        try:
            results[slug] = adopt_legacy_journal(slug, data_dir, output_dir)
        except Exception:
            log.warning("Journal adoption failed for %s", slug, exc_info=True)
            results[slug] = "failed"
        if results[slug] != "none":
            log.info("Journal adoption for %s: %s", slug, results[slug])
    return results


def sync_journal(slug: str, data_dir: Optional[Path] = None) -> None:
    """Reconcile the journal file with the database before reading it.

    Adopts a legacy journal first, finishes a fold that committed but crashed
    before its file move, deletes an uncommitted pending file, resets the
    entries when the journal was deleted, and adopts the hash of a journal
    the user edited. Does nothing while the folder is unclaimed, a legacy
    journal is still waiting, or the output root or campaign folder is absent.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return
    if adopt_legacy_journal(safe, data_dir) not in ("none", "adopted"):
        return
    output = get_output_root()
    jpath = journal_path(safe, data_dir, output_dir=output)
    if jpath is None or not output.is_dir() or not jpath.parent.is_dir():
        return
    pending = _pending_path(jpath)
    with db.transaction(data_dir) as conn:
        row = conn.execute(
            "SELECT id, journal_sha256 FROM campaigns WHERE slug = ?", (safe,)
        ).fetchone()
        if row is None:
            return
        if pending.exists():
            if row["journal_sha256"] and _sha256(pending) == row["journal_sha256"]:
                from .transcript_store import _replace
                if not _replace(pending, jpath):
                    jpath.write_bytes(pending.read_bytes())
                    pending.unlink(missing_ok=True)
                file_registry.add_if_owned(
                    jpath, kind="journal", owner=file_registry.Owner("campaign", row["id"]),
                    conn=conn, data_dir=data_dir, output_dir=output)
                log.info("Finished an interrupted journal update for %s", safe)
            else:
                pending.unlink(missing_ok=True)
        if not jpath.exists():
            has_entries = conn.execute(
                "SELECT 1 FROM journal_entries WHERE campaign_id = ? LIMIT 1", (row["id"],)
            ).fetchone()
            if has_entries or row["journal_sha256"]:
                log.info("The journal for %s was deleted; starting a fresh journal", safe)
            file_registry.forget_kind(file_registry.Owner("campaign", row["id"]), "journal",
                                      conn=conn, data_dir=data_dir, output_dir=output)
            _reset_rows(conn, row["id"])
            return
        current = _sha256(jpath)
        if current != row["journal_sha256"]:
            conn.execute("UPDATE campaigns SET journal_sha256 = ? WHERE id = ?", (current, row["id"]))


def _reset_rows(conn, campaign_id: int) -> None:
    """Forget every folded session. The delete trigger marks the journal
    stale, so the flag is cleared afterwards."""
    conn.execute("DELETE FROM journal_entries WHERE campaign_id = ?", (campaign_id,))
    conn.execute(
        "UPDATE campaigns SET journal_sha256 = NULL, journal_stale_since = NULL WHERE id = ?",
        (campaign_id,),
    )


def reset_journal(slug: str, data_dir: Optional[Path] = None) -> None:
    """Delete the journal file and its entries (the start of a rebuild).

    The legacy journal is deleted too, so a rebuild never re-adopts old text.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise ValueError(f"Invalid campaign slug: {slug!r}")
    output = get_output_root()
    jpath = journal_path(safe, data_dir, output_dir=output)
    with db.transaction(data_dir) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()
        if row is None:
            raise KeyError(f"Campaign {safe!r} not found")
        file_registry.forget_kind(file_registry.Owner("campaign", row["id"]), "journal",
                                  conn=conn, data_dir=data_dir, output_dir=output)
        _reset_rows(conn, row["id"])
    legacy = legacy_journal_path(safe, data_dir)
    doomed = [legacy, _pending_path(legacy), legacy.with_name(legacy.name + ADOPTED_SUFFIX)]
    if jpath is not None:
        doomed += [jpath, _pending_path(jpath)]
    for path in doomed:
        path.unlink(missing_ok=True)


def journaled_stems(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Stems folded into the journal, in campaign order."""
    with db.connection(data_dir) as conn:
        return [r[0] for r in conn.execute(
            "SELECT t.stem FROM journal_entries je "
            "JOIN transcripts t ON t.id = je.transcript_id "
            "JOIN campaigns c ON c.id = je.campaign_id "
            "WHERE c.slug = ? ORDER BY t.position",
            (slug,),
        )]


def journal_stale_since(slug: str, data_dir: Optional[Path] = None) -> Optional[str]:
    """When the journal started mentioning sessions that left, changed, or were
    re-transcribed (UTC timestamp), or None if it's current."""
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT journal_stale_since FROM campaigns WHERE slug = ?", (slug,)
        ).fetchone()
    return row[0] if row else None


def export_journal(slug: str, data_dir: Optional[Path] = None) -> Optional[str]:
    """The journal with ``journaled_sessions`` added back into its frontmatter
    (for downloads and ``wisper campaigns journal --export``), or None."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise ValueError("Invalid campaign slug")
    sync_journal(safe, data_dir)
    jpath = journal_path(safe, data_dir)  # after the sync: adoption can create it
    if jpath is None or not jpath.exists():
        return None
    meta, body = parse_journal(jpath.read_text(encoding="utf-8"))
    meta.pop("journaled_sessions", None)
    meta["journaled_sessions"] = journaled_stems(safe, data_dir)
    fm = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False,
                        allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def unjournalled_sessions(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Session stems that have a ``.summary.md`` but are not yet in the journal.

    Returned in campaign transcript order. Sessions without a summary sidecar
    are skipped — there is nothing to fold in until they are summarized.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return []
    sync_journal(safe, data_dir)
    with db.connection(data_dir) as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()
    if row is None:
        return []
    cid = row[0]
    journaled = set(journaled_stems(safe, data_dir))
    out: list[str] = []
    for stem in get_transcripts_for_campaign(safe, data_dir):
        if stem in journaled:
            continue
        summary = _summary_path(cid, stem, data_dir)
        if summary is not None and summary.exists():
            out.append(stem)
    return out


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

def _roster_lines(profiles: dict[str, SpeakerProfile]) -> str:
    if not profiles:
        return "(no speakers enrolled)"
    out = []
    for p in profiles.values():
        role = f" [{p.role}]" if p.role else ""
        note = f" — {p.notes}" if p.notes else ""
        out.append(f"- {p.display_name}{role}{note}")
    return "\n".join(out)


def _user_prompt(current_body: str, session_stem: str, summary_md: str,
                 profiles: dict[str, SpeakerProfile]) -> str:
    journal_block = current_body.strip() or "(none yet — start a new journal)"
    return (
        f"Enrolled speakers (the players):\n{_roster_lines(profiles)}\n\n"
        f"=== CURRENT CAMPAIGN JOURNAL (rewrite and extend this) ===\n"
        f"{journal_block}\n\n"
        f"=== NEW SESSION TO FOLD IN — {session_stem} ===\n"
        f"{summary_md.strip()}"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def update_journal(slug: str, client: LLMClient,
                   profiles: dict[str, SpeakerProfile], *,
                   session_stem: Optional[str] = None,
                   data_dir: Optional[Path] = None) -> Optional[JournalResult]:
    """Fold one session summary into the campaign journal.

    Picks ``session_stem`` if given, else the next unjournalled session in
    campaign order. Returns the JournalResult, or None when there is nothing
    to fold (no pending sessions and no explicit stem).

    Raises:
        ValueError: invalid slug.
        KeyError: campaign not found.
        FileNotFoundError: the target session has no ``.summary.md`` sidecar.
        JournalLocationError: the journal's folder or output root is unusable
            (``journal_folder_taken``, ``journal_output_unavailable``,
            ``journal_legacy_pending``).
        LLMUnavailableError / LLMResponseError: provider failure (propagated).
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise ValueError(f"Invalid campaign slug: {slug!r}")
    if safe not in load_campaigns(data_dir):
        raise KeyError(f"Campaign {safe!r} not found")

    output = get_output_root()
    adopted = adopt_legacy_journal(safe, data_dir, output)
    if adopted not in ("none", "adopted"):
        raise JournalLocationError(_ADOPT_FAILURES[adopted])
    with db.connection(data_dir) as conn:
        cid = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()["id"]
    try:
        campaign_folders.ensure_folder(cid, data_dir=data_dir, output_dir=output)
    except campaign_folders.FolderTakenError:
        raise JournalLocationError("journal_folder_taken") from None
    except FileNotFoundError:
        raise JournalLocationError("journal_output_unavailable") from None
    sync_journal(safe, data_dir)
    jpath = journal_path(safe, data_dir, output_dir=output)
    base_sha: Optional[str] = None
    body = ""
    if jpath.exists():
        raw = jpath.read_bytes()
        base_sha = hashlib.sha256(raw).hexdigest()
        _, body = parse_journal(raw.decode("utf-8"))

    # Choose the session to fold.
    if session_stem is not None:
        target = session_stem
    else:
        pending = unjournalled_sessions(safe, data_dir)
        if not pending:
            return None
        target = pending[0]

    summary_file = _summary_path(cid, target, data_dir)
    if summary_file is None or not summary_file.exists():
        raise FileNotFoundError(
            f"No summary for session {target!r}. Run `wisper summarize` on it first."
        )
    summary_md = summary_file.read_text(encoding="utf-8")

    # The LLM call runs outside any transaction.
    new_body = client.complete(_SYSTEM_PROMPT,
                               _user_prompt(body, target, summary_md, profiles))
    new_body = _strip_code_fence(new_body)

    rendered = render_journal(safe, new_body,
                              getattr(client, "provider", ""),
                              getattr(client, "model", ""))
    from .transcript_store import _replace, atomic_write_text, nfc
    jpath.parent.mkdir(parents=True, exist_ok=True)
    pending_file = _pending_path(jpath)
    atomic_write_text(pending_file, rendered)
    new_sha = _sha256(pending_file)

    # Commit point: the entry and the new body's hash, together.
    try:
        with db.transaction(data_dir) as conn:
            campaign = conn.execute(
                "SELECT id, journal_sha256 FROM campaigns WHERE slug = ?", (safe,)
            ).fetchone()
            if campaign is None:
                raise KeyError(f"Campaign {safe!r} not found")
            if campaign["journal_sha256"] != base_sha:
                raise RuntimeError(
                    "The journal changed while this session was being folded in; "
                    "run the update again."
                )
            member = conn.execute(
                "SELECT id FROM transcripts WHERE campaign_id = ? AND stem = ?",
                (campaign["id"], nfc(target)),
            ).fetchone()
            if member is None:
                raise KeyError(f"Session {target!r} is not in campaign {safe!r}")
            conn.execute(
                "INSERT INTO journal_entries (transcript_id, campaign_id, folded_at) "
                "VALUES (?, ?, ?) ON CONFLICT (transcript_id) DO UPDATE SET folded_at = excluded.folded_at",
                (member[0], campaign["id"], db.now_utc()),
            )
            conn.execute(
                "UPDATE campaigns SET journal_sha256 = ? WHERE id = ?", (new_sha, campaign["id"])
            )
    except BaseException:
        pending_file.unlink(missing_ok=True)
        raise

    if not _replace(pending_file, jpath):
        # Windows: the journal is held open (e.g. by Obsidian) past every retry.
        log.warning("%s is locked by another program; writing it in place", jpath.name)
        jpath.write_bytes(pending_file.read_bytes())
        pending_file.unlink(missing_ok=True)

    # Registered after the file move: journal.md doesn't exist inside the commit above on a first fold.
    try:
        with db.transaction(data_dir) as conn:
            file_registry.add_if_owned(
                jpath, kind="journal",
                owner=file_registry.Owner.for_campaign_slug(safe, conn=conn),
                conn=conn, data_dir=data_dir, output_dir=output)
    except sqlite3.Error:
        log.warning("Could not register %s", jpath, exc_info=True)

    journaled = journaled_stems(safe, data_dir)
    return JournalResult(
        path=jpath,
        folded=target,
        journaled_sessions=journaled,
        provider=getattr(client, "provider", ""),
        model=getattr(client, "model", ""),
    )


# ---------------------------------------------------------------------------
# Rebuilds — re-fold existing summaries (default) or redrive from transcripts
# ---------------------------------------------------------------------------

@dataclass
class RebuildResult:
    """Outcome of ``refold_campaign`` / ``rebuild_campaign``."""
    resummarized: list[str] = field(default_factory=list)   # summaries (re)written
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (stem, reason)
    journal: Optional[JournalResult] = None


def _check_campaign(slug: str, data_dir: Optional[Path]) -> str:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise ValueError(f"Invalid campaign slug: {slug!r}")
    if safe not in load_campaigns(data_dir):
        raise KeyError(f"Campaign {safe!r} not found")
    return safe


def _summarize_into_sidecar(campaign_id: int, stem: str, client: LLMClient,
                            profiles: dict[str, SpeakerProfile],
                            sections: Optional[list[str]], result: RebuildResult,
                            report: Callable[[str], None],
                            data_dir: Optional[Path] = None) -> bool:
    """Summarize a campaign session's ``.md`` into its ``.summary.md``. False = skipped."""
    from .llm.errors import LLMResponseError, LLMUnavailableError
    from .summarize import default_summary_path, render_markdown, summarize_transcript
    from .transcript_store import save_summary

    transcript_path = _transcript_path(campaign_id, stem, data_dir)
    if transcript_path is None or not transcript_path.exists():
        result.skipped.append((stem, "transcript .md not found"))
        report(f"Skipping {stem}: transcript .md not found")
        return False
    report(f"Summarizing {stem} ...")
    try:
        note = summarize_transcript(
            transcript_path.read_text(encoding="utf-8"), profiles, client,
            sections=sections, source_transcript=transcript_path.name,
        )
    except (LLMUnavailableError, LLMResponseError) as exc:
        result.skipped.append((stem, str(exc)))
        report(f"Skipping {stem}: summarize failed ({exc})")
        return False
    save_summary(default_summary_path(transcript_path),
                 render_markdown(note, profiles=profiles, sections=sections))
    result.resummarized.append(stem)
    return True


def _fold_all(safe: str, stems: list[str], client: LLMClient,
              profiles: dict[str, SpeakerProfile], data_dir: Optional[Path],
              result: RebuildResult, report: Callable[[str], None]) -> None:
    from .llm.errors import LLMResponseError, LLMUnavailableError

    for stem in stems:
        report(f"Folding {stem} ...")
        try:
            result.journal = update_journal(safe, client, profiles,
                                            session_stem=stem, data_dir=data_dir)
        except (LLMUnavailableError, LLMResponseError, FileNotFoundError, KeyError) as exc:
            result.skipped.append((stem, f"fold failed: {exc}"))
            report(f"Fold failed for {stem}: {exc}")


def refold_campaign(slug: str, client: LLMClient,
                    profiles: dict[str, SpeakerProfile], *,
                    sections: Optional[list[str]] = None,
                    data_dir: Optional[Path] = None,
                    on_progress: Optional[Callable[[str], None]] = None
                    ) -> RebuildResult:
    """Rebuild the journal from the sessions' existing summaries.

    Resets the journal (file and entries, which also clears the stale flag),
    then folds every session's ``.summary.md`` in campaign order: one LLM
    call per session. Summaries are reused as they are, including any edits;
    a session with no summary yet is summarized first. The default rebuild.

    Raises:
        ValueError: invalid slug.
        KeyError: campaign not found.
    """
    safe = _check_campaign(slug, data_dir)
    report = on_progress or (lambda msg: None)
    result = RebuildResult()
    with db.connection(data_dir) as conn:
        cid = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()[0]

    foldable: list[str] = []
    for stem in get_transcripts_for_campaign(safe, data_dir):
        summary = _summary_path(cid, stem, data_dir)
        if summary is not None and summary.exists():
            foldable.append(stem)
        elif _summarize_into_sidecar(cid, stem, client, profiles, sections, result, report, data_dir):
            foldable.append(stem)

    reset_journal(safe, data_dir)
    _fold_all(safe, foldable, client, profiles, data_dir, result, report)
    return result


def rebuild_campaign(slug: str, client: LLMClient,
                     profiles: dict[str, SpeakerProfile], *,
                     sections: Optional[list[str]] = None,
                     data_dir: Optional[Path] = None,
                     on_progress: Optional[Callable[[str], None]] = None
                     ) -> RebuildResult:
    """Redrive a campaign from its transcripts: re-summarize every session
    (overwriting its ``.summary.md``) and fold them all into a freshly-reset
    journal, in campaign order.

    Two LLM calls per session, so callers must get the user's confirmation
    first. Use it when the summaries themselves are bad; otherwise
    :func:`refold_campaign` is cheaper. A missing transcript or a failed
    summary skips that session (``RebuildResult.skipped``); only freshly
    summarized sessions are folded.

    Raises:
        ValueError: invalid slug.
        KeyError: campaign not found.
    """
    safe = _check_campaign(slug, data_dir)
    report = on_progress or (lambda msg: None)
    result = RebuildResult()
    with db.connection(data_dir) as conn:
        cid = conn.execute("SELECT id FROM campaigns WHERE slug = ?", (safe,)).fetchone()[0]

    for stem in get_transcripts_for_campaign(safe, data_dir):
        _summarize_into_sidecar(cid, stem, client, profiles, sections, result, report, data_dir)

    # Reset so the fold pass starts clean rather than building on stale content.
    reset_journal(safe, data_dir)
    _fold_all(safe, list(result.resummarized), client, profiles, data_dir, result, report)
    return result
