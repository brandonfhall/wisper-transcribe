"""Campaign-notes summarization — session recap with loot, NPCs, follow-ups.

The output is an Obsidian-compatible markdown file with YAML frontmatter,
`[[wiki-links]]` for names matching enrolled speaker profiles, and
`## Summary / ## Loot & Inventory / ## NPCs / ## Follow-ups` headings.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

from .llm import LLMClient
from .llm.errors import LLMResponseError, LLMUnavailableError
from .models import LootChange, NPCMention, SpeakerProfile, SpeakerSuggestion, SummaryNote
from .refine import parse_transcript

SECTIONS = ("summary", "loot", "npcs", "followups")

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "session_title": {"type": "string"},
        "loot": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "quantity": {"type": "string"},
                    "recipient": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["item"],
                "additionalProperties": False,
            },
        },
        "npcs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "first_mentioned_at": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        "followups": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["summary"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are a campaign archivist for a tabletop RPG actual-play podcast. "
    "You will be given the enrolled speaker roster plus a session transcript. "
    "Produce a structured summary as JSON covering the requested sections:\n"
    " - summary: 2 to 4 paragraphs, verbatim-style recap of what happened.\n"
    " - loot: inventory changes (items gained/lost/spent), per recipient if known.\n"
    " - npcs: non-player characters referenced (not the enrolled players). "
    "Record their role/description and the first transcript timestamp they appear at.\n"
    " - followups: open plot threads / questions / hooks the party did not resolve.\n"
    "Do NOT invent events. Leave a field as an empty string or empty array if you "
    "cannot determine it from the transcript."
)


def summarize(body: str, frontmatter: dict, profiles: dict[str, SpeakerProfile],
              client: LLMClient, *,
              sections: Optional[list[str]] = None,
              source_transcript: str = "",
              unresolved_speakers: Optional[list[SpeakerSuggestion]] = None,
              refined: bool = False) -> SummaryNote:
    """Generate a SummaryNote from a transcript body.

    Raises LLMUnavailableError / LLMResponseError on provider failure — the
    CLI wraps this into a click.ClickException with a user-friendly message.
    """
    sections = list(sections) if sections else list(SECTIONS)
    known_names = _known_names(profiles)
    roster = _roster_lines(profiles) or "(no speakers enrolled)"

    user_prompt = (
        f"Sections to include: {', '.join(sections)}\n\n"
        f"Enrolled speakers (these are the players; NPCs are anyone else):\n"
        f"{roster}\n\n"
        f"Existing transcript frontmatter (for context only):\n"
        f"title: {frontmatter.get('title', '')}\n"
        f"duration: {frontmatter.get('duration', '')}\n\n"
        f"Transcript body:\n{body}"
    )

    data = client.complete_json(_SYSTEM_PROMPT, user_prompt, _SUMMARY_SCHEMA)
    if not isinstance(data, dict):
        raise LLMResponseError("Summary response was not a JSON object")

    loot: list[LootChange] = []
    for raw in data.get("loot", []) or []:
        if not isinstance(raw, dict):
            continue
        item = str(raw.get("item", "")).strip()
        if not item:
            continue
        loot.append(LootChange(
            item=item,
            quantity=str(raw.get("quantity", "")),
            recipient=str(raw.get("recipient", "")),
            note=str(raw.get("note", "")),
        ))

    npcs: list[NPCMention] = []
    for raw in data.get("npcs", []) or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name or name in known_names:
            # Filter out anything that is actually an enrolled player.
            continue
        npcs.append(NPCMention(
            name=name,
            role=str(raw.get("role", "")),
            first_mentioned_at=str(raw.get("first_mentioned_at", "")),
            description=str(raw.get("description", "")),
        ))

    followups = [str(x).strip() for x in (data.get("followups") or []) if isinstance(x, str) and x.strip()]

    title = str(data.get("session_title", "")).strip() or str(frontmatter.get("title", "")).strip() or "Session Summary"

    return SummaryNote(
        summary=str(data.get("summary", "")).strip(),
        loot=loot,
        npcs=npcs,
        followups=followups,
        unresolved_speakers=list(unresolved_speakers or []),
        session_title=title,
        source_transcript=source_transcript,
        generated_at=_dt.datetime.now().isoformat(timespec="seconds"),
        provider=getattr(client, "provider", ""),
        model=getattr(client, "model", ""),
        refined=refined,
    )


def render_markdown(note: SummaryNote, profiles: Optional[dict[str, SpeakerProfile]] = None,
                    sections: Optional[list[str]] = None) -> str:
    """Render a SummaryNote as Obsidian-friendly markdown.

    Character/NPC names that match an enrolled profile's display_name or appear
    in a profile's `notes` field are wrapped in `[[...]]`. Unknown names are
    rendered plain to avoid creating orphan vault pages.
    """
    sections = list(sections) if sections else list(SECTIONS)
    link_terms = _link_terms(profiles or {})

    lines: list[str] = []
    lines.append("---")
    lines.append("type: session-summary")
    if note.source_transcript:
        lines.append(f"source: {_yaml_str(note.source_transcript)}")
    lines.append(f"generated_at: {note.generated_at}")
    lines.append(f"provider: {note.provider}")
    lines.append(f"model: {_yaml_str(note.model)}")
    lines.append(f"refined: {'true' if note.refined else 'false'}")
    if note.npcs:
        npc_names = ", ".join(_yaml_str(n.name) for n in note.npcs)
        lines.append(f"npcs: [{npc_names}]")
    lines.append("---")
    lines.append("")
    lines.append(f"# {note.session_title}")
    lines.append("")

    if "summary" in sections and note.summary:
        lines.append("## Summary")
        lines.append("")
        lines.append(_linkify(note.summary, link_terms))
        lines.append("")

    if "loot" in sections:
        lines.append("## Loot & Inventory")
        lines.append("")
        if not note.loot:
            lines.append("_No inventory changes recorded._")
        else:
            for change in note.loot:
                recipient = _linkify(change.recipient, link_terms) if change.recipient else ""
                quantity = change.quantity.strip()
                item = _linkify(change.item, link_terms)
                qty_prefix = f"**{quantity}** " if quantity else ""
                if recipient:
                    line = f"- {recipient} — {qty_prefix}{item}"
                else:
                    line = f"- {qty_prefix}{item}"
                if change.note:
                    line += f" _({change.note})_"
                lines.append(line)
        lines.append("")

    if "npcs" in sections:
        lines.append("## NPCs")
        lines.append("")
        if not note.npcs:
            lines.append("_No notable NPCs recorded._")
        else:
            for npc in note.npcs:
                linked = _linkify(npc.name, link_terms) if npc.name in link_terms else npc.name
                suffix = []
                if npc.role:
                    suffix.append(npc.role)
                if npc.first_mentioned_at:
                    suffix.append(f"first at {npc.first_mentioned_at}")
                meta = f" ({'; '.join(suffix)})" if suffix else ""
                desc = f" — {npc.description}" if npc.description else ""
                lines.append(f"- {linked}{meta}{desc}")
        lines.append("")

    if "followups" in sections:
        lines.append("## Follow-ups")
        lines.append("")
        if not note.followups:
            lines.append("_None flagged._")
        else:
            for item in note.followups:
                lines.append(f"- [ ] {_linkify(item, link_terms)}")
        lines.append("")

    if note.unresolved_speakers:
        lines.append("## Unresolved Speakers")
        lines.append("")
        lines.append(
            "_The refiner suggested these attributions but they were not auto-applied. "
            "Use `wisper fix` to accept any you agree with._"
        )
        lines.append("")
        for sug in note.unresolved_speakers:
            reason = f" — {sug.reason}" if sug.reason else ""
            lines.append(
                f"- Line {sug.line_idx + 1}: `{sug.current_label}` "
                f"→ **{sug.suggested_name}** (confidence {sug.confidence:.0%}){reason}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Orchestration helper used by the CLI
# ---------------------------------------------------------------------------

def summarize_transcript(md: str, profiles: dict[str, SpeakerProfile],
                         client: LLMClient, *,
                         sections: Optional[list[str]] = None,
                         source_transcript: str = "",
                         unresolved_speakers: Optional[list[SpeakerSuggestion]] = None,
                         refined: bool = False) -> SummaryNote:
    """Parse a transcript string and produce a SummaryNote."""
    fm, body, _ = parse_transcript(md)
    return summarize(body, fm, profiles, client,
                     sections=sections,
                     source_transcript=source_transcript,
                     unresolved_speakers=unresolved_speakers,
                     refined=refined)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _known_names(profiles: dict[str, SpeakerProfile]) -> set[str]:
    return {p.display_name for p in profiles.values() if p.display_name}


def _roster_lines(profiles: dict[str, SpeakerProfile]) -> str:
    if not profiles:
        return ""
    out = []
    for p in profiles.values():
        role = f" [{p.role}]" if p.role else ""
        note = f" — {p.notes}" if p.notes else ""
        out.append(f"- {p.display_name}{role}{note}")
    return "\n".join(out)


def _link_terms(profiles: dict[str, SpeakerProfile]) -> set[str]:
    """Return the set of names that should be wrapped in [[...]] on render.

    Includes every enrolled profile's display_name, and any comma-separated
    name mentioned in a profile's `notes` field (character names are commonly
    stored there per CLAUDE.md).
    """
    terms: set[str] = set()
    for p in profiles.values():
        if p.display_name:
            terms.add(p.display_name)
        if p.notes:
            for part in p.notes.replace(";", ",").split(","):
                token = part.strip()
                # Strip a `voice_of:` prefix if present (Approach 1 in plan.md).
                if token.lower().startswith("voice_of:"):
                    continue
                if token and len(token) > 1:
                    terms.add(token)
    return terms


def _linkify(text: str, terms: set[str]) -> str:
    """Wrap whole-word occurrences of each term in [[...]] (Obsidian wiki-link).

    Idempotent: if a term is already inside [[...]], it is not double-wrapped.
    """
    if not text or not terms:
        return text
    import re as _re

    # Sort longest-first so "Bob the Guard" wraps before "Bob".
    for term in sorted(terms, key=len, reverse=True):
        pattern = _re.compile(rf"(?<!\[)\b{_re.escape(term)}\b(?!\])")
        text = pattern.sub(f"[[{term}]]", text)
    return text


def _yaml_str(value: str) -> str:
    """Produce a YAML-safe single-line string for frontmatter.

    We quote with double-quotes and escape embedded quotes. Sufficient for
    filenames and model IDs; we never pass arbitrary LLM output through this.
    """
    if value is None:
        return '""'
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def default_summary_path(transcript_path: Path) -> Path:
    """<stem>.summary.md alongside the transcript."""
    return transcript_path.with_name(f"{transcript_path.stem}.summary.md")
