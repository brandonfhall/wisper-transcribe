"""Rolling campaign journal — a living per-campaign document the LLM rewrites
as each new session summary is folded in.

This is the campaign-level counterpart to per-session ``wisper summarize``.
Where ``summarize.py`` turns one transcript into one ``<stem>.summary.md``,
this module accumulates those session summaries into a single
``data_dir/campaigns/<slug>/journal.md`` that grows with the campaign.

Design — bounded context:
    On each fold the LLM receives only ``[current journal] + [one new session
    summary]`` and returns a rewritten journal. Even at session 50 the prompt
    stays ~2–5 k tokens, so cost/latency do not grow with campaign length.

The session ``.summary.md`` sidecars (written by ``summarize``) are the source
of truth and are never modified. The journal tracks which sessions it has
already absorbed via a ``journaled_sessions`` list in its YAML frontmatter, so
re-running only folds in what is new.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .campaign_manager import (
    _validate_campaign_slug,
    get_campaigns_dir,
    get_transcripts_for_campaign,
    load_campaigns,
)
from .llm import LLMClient
from .models import SpeakerProfile
from .path_utils import get_output_dir

JOURNAL_FILENAME = "journal.md"

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

def journal_path(slug: str, data_dir: Optional[Path] = None) -> Optional[Path]:
    """Return ``campaigns/<slug>/journal.md``, or None if the slug is invalid."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return None
    return get_campaigns_dir(data_dir) / safe / JOURNAL_FILENAME


def _summary_path(stem: str) -> Path:
    """Return the ``<stem>.summary.md`` sidecar path in the output dir."""
    return get_output_dir() / f"{stem}.summary.md"


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


def render_journal(slug: str, body: str, journaled_sessions: list[str],
                   provider: str, model: str) -> str:
    """Render the journal markdown: YAML frontmatter + body."""
    meta = {
        "type": "campaign-journal",
        "campaign": slug,
        "journaled_sessions": list(journaled_sessions),
        "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
    }
    fm = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False,
                        allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def _load_journaled(slug: str, data_dir: Optional[Path]) -> list[str]:
    """Return the list of session stems already folded into the journal."""
    jpath = journal_path(slug, data_dir)
    if jpath is None or not jpath.exists():
        return []
    meta, _ = parse_journal(jpath.read_text(encoding="utf-8"))
    return list(meta.get("journaled_sessions") or [])


def unjournalled_sessions(slug: str, data_dir: Optional[Path] = None) -> list[str]:
    """Session stems that have a ``.summary.md`` but are not yet in the journal.

    Returned in campaign transcript order. Sessions without a summary sidecar
    are skipped — there is nothing to fold in until they are summarized.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return []
    journaled = set(_load_journaled(safe, data_dir))
    out: list[str] = []
    for stem in get_transcripts_for_campaign(safe, data_dir):
        if stem in journaled:
            continue
        if _summary_path(stem).exists():
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
        LLMUnavailableError / LLMResponseError: provider failure (propagated).
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        raise ValueError(f"Invalid campaign slug: {slug!r}")
    if safe not in load_campaigns(data_dir):
        raise KeyError(f"Campaign {safe!r} not found")

    jpath = journal_path(safe, data_dir)
    meta: dict = {}
    body = ""
    if jpath.exists():
        meta, body = parse_journal(jpath.read_text(encoding="utf-8"))
    journaled = list(meta.get("journaled_sessions") or [])

    # Choose the session to fold.
    if session_stem is not None:
        target = session_stem
    else:
        pending = unjournalled_sessions(safe, data_dir)
        if not pending:
            return None
        target = pending[0]

    summary_file = _summary_path(target)
    if not summary_file.exists():
        raise FileNotFoundError(
            f"No summary for session {target!r}. Run `wisper summarize` on it first."
        )
    summary_md = summary_file.read_text(encoding="utf-8")

    new_body = client.complete(_SYSTEM_PROMPT,
                               _user_prompt(body, target, summary_md, profiles))
    new_body = _strip_code_fence(new_body)

    if target not in journaled:
        journaled.append(target)

    rendered = render_journal(safe, new_body, journaled,
                              getattr(client, "provider", ""),
                              getattr(client, "model", ""))
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(rendered, encoding="utf-8")

    return JournalResult(
        path=jpath,
        folded=target,
        journaled_sessions=journaled,
        provider=getattr(client, "provider", ""),
        model=getattr(client, "model", ""),
    )
