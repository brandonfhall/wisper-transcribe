"""Transcript refinement — vocabulary correction and unknown-speaker ID.

Design tenets (see CLAUDE.md + the approved plan):
- YAML frontmatter is never sent to the LLM and is never modified.
- Vocabulary edits are validated against a known-term list by edit-distance;
  freeform LLM rewrites are rejected.
- Unknown-speaker suggestions are never auto-applied — they are returned as
  `SpeakerSuggestion` objects for manual review / summary inclusion.
- Network/endpoint failures soft-fail with a warning; callers may catch
  `LLMUnavailableError` to skip without aborting a pipeline.
"""
from __future__ import annotations

import difflib
import re
import warnings
from typing import Iterable, Optional

from .llm import LLMClient
from .llm.errors import LLMResponseError, LLMUnavailableError
from .models import Edit, SpeakerProfile, SpeakerSuggestion

# ---------------------------------------------------------------------------
# Tunables (surfaced as module-level so tests can patch cleanly)
# ---------------------------------------------------------------------------

VOCABULARY_BATCH_SIZE = 25
UNKNOWN_WINDOW_SIZE = 20
UNKNOWN_WINDOW_OVERLAP = 5
EDIT_DISTANCE_THRESHOLD = 0.7    # ratio passed to difflib.get_close_matches
UNKNOWN_CONFIDENCE_THRESHOLD = 0.75

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_SPEAKER_LINE_RE = re.compile(r"^\*\*(?P<speaker>[^*]+)\*\*")
_UNKNOWN_LABEL_RE = re.compile(r"^\*\*(?P<label>Unknown Speaker \d+)\*\*", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Frontmatter / body parsing
# ---------------------------------------------------------------------------

def parse_transcript(md: str) -> tuple[dict, str, str]:
    """Split a transcript markdown file into (frontmatter_dict, body, raw_frontmatter_str).

    raw_frontmatter_str is the full `---\n...\n---\n` prefix (including delimiters),
    preserved byte-for-byte so `apply_edits` can round-trip without disturbing YAML.
    If the transcript has no frontmatter, returns ({}, md, "").
    """
    match = _FRONTMATTER_RE.match(md)
    if not match:
        return {}, md, ""

    raw = match.group(0)
    yaml_text = match.group(1)
    body = md[match.end():]

    # Parse yaml defensively — we never trust the output for logic, only for
    # surfacing existing title/speakers as LLM prompt context.
    try:
        import yaml
        data = yaml.safe_load(yaml_text) or {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    return data, body, raw


# ---------------------------------------------------------------------------
# Vocabulary correction (Task A)
# ---------------------------------------------------------------------------

_VOCAB_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "corrected": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["original", "corrected"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["changes"],
    "additionalProperties": False,
}

_VOCAB_SYSTEM = (
    "You are a transcription proof-reader. The user will give you a list of "
    "proper-noun spellings that MUST appear exactly as given, followed by "
    "numbered transcript lines that may contain phonetic misspellings of those "
    "names. Return a JSON object with a `changes` array of "
    "{original, corrected, reason} substitutions. Change ONLY phonetic "
    "misspellings of names in the known list. Do not rewrite grammar, style, "
    "or anything else. If nothing needs fixing, return {\"changes\": []}."
)


def _batch(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _validate_vocab_edit(edit: Edit, known_terms: list[str]) -> bool:
    """Accept only substitutions whose `corrected` text matches a known term
    by edit-distance ≥ EDIT_DISTANCE_THRESHOLD. Rejects freeform rewrites."""
    if not edit.original or not edit.corrected:
        return False
    if edit.original == edit.corrected:
        return False
    # The corrected token must be close to something in the known list.
    close = difflib.get_close_matches(
        edit.corrected, known_terms, n=1, cutoff=EDIT_DISTANCE_THRESHOLD
    )
    return bool(close)


def fix_vocabulary(body: str, hotwords: list[str], character_names: list[str],
                   client: LLMClient) -> list[Edit]:
    """Run the vocabulary-correction pass over transcript body text.

    Returns validated Edit objects ready to be applied. Invalid edits
    (freeform rewrites, changes to tokens not in the known list) are dropped
    silently with a warning count emitted at the end.
    """
    known_terms = sorted({t.strip() for t in (hotwords or []) + (character_names or []) if t and t.strip()})
    if not known_terms:
        # Nothing to anchor validation against — refuse to propose any change.
        warnings.warn(
            "fix_vocabulary: no hotwords or character names provided; skipping.",
            stacklevel=2,
        )
        return []

    lines = body.splitlines()
    edits: list[Edit] = []
    rejected = 0

    known_header = "Proper nouns (must be spelled exactly): " + ", ".join(known_terms)
    for chunk in _batch(lines, VOCABULARY_BATCH_SIZE):
        numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(chunk))
        user = f"{known_header}\n\nLines:\n{numbered}"
        try:
            data = client.complete_json(_VOCAB_SYSTEM, user, _VOCAB_SCHEMA)
        except (LLMUnavailableError, LLMResponseError) as exc:
            warnings.warn(f"fix_vocabulary: LLM call failed ({exc}); returning partial results.",
                          stacklevel=2)
            break

        changes = data.get("changes") if isinstance(data, dict) else None
        if not isinstance(changes, list):
            continue
        for raw in changes:
            if not isinstance(raw, dict):
                continue
            edit = Edit(
                original=str(raw.get("original", "")),
                corrected=str(raw.get("corrected", "")),
                reason=str(raw.get("reason", "")),
            )
            if _validate_vocab_edit(edit, known_terms):
                edits.append(edit)
            else:
                rejected += 1

    if rejected:
        warnings.warn(
            f"fix_vocabulary: rejected {rejected} freeform edit(s) that did not match any known term.",
            stacklevel=2,
        )
    # Deduplicate while preserving order (same original→corrected repeated across batches).
    seen: set[tuple[str, str]] = set()
    unique: list[Edit] = []
    for e in edits:
        key = (e.original, e.corrected)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique


def apply_edits(body: str, edits: list[Edit]) -> str:
    """Apply line-level string substitutions.

    Only the body (no YAML frontmatter) should be passed. Each edit's `original`
    substring is replaced with `corrected` across the body. Order-preserving;
    duplicates across edits do not compound.
    """
    out = body
    for edit in edits:
        if not edit.original:
            continue
        out = out.replace(edit.original, edit.corrected)
    return out


# ---------------------------------------------------------------------------
# Unknown-speaker identification (Task D)
# ---------------------------------------------------------------------------

_UNKNOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_number": {"type": "integer"},
                    "current_label": {"type": "string"},
                    "suggested_name": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["line_number", "current_label", "suggested_name", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}

_UNKNOWN_SYSTEM = (
    "You are assisting a tabletop RPG transcription tool. Some lines are "
    "labelled 'Unknown Speaker N' because the automatic speaker-matcher could "
    "not identify the voice. Using the enrolled speaker list (names and any "
    "notes / character names) and the surrounding dialogue, suggest which "
    "enrolled person is most likely speaking each Unknown line. "
    "Return JSON {\"suggestions\": [...]} with one entry per Unknown line you "
    "can resolve. Use `confidence` between 0.0 and 1.0. Do NOT suggest names "
    "that are not in the enrolled list. Skip any Unknown line you cannot "
    "confidently attribute (confidence < 0.5)."
)


def _describe_profiles(profiles: dict[str, SpeakerProfile]) -> str:
    if not profiles:
        return "(none enrolled)"
    parts = []
    for p in profiles.values():
        suffix = f" — {p.notes}" if p.notes else ""
        role = f" [{p.role}]" if p.role else ""
        parts.append(f"- {p.display_name}{role}{suffix}")
    return "\n".join(parts)


def identify_unknown_speakers(body: str, profiles: dict[str, SpeakerProfile],
                              client: LLMClient) -> list[SpeakerSuggestion]:
    """Run the unknown-speaker identification pass.

    Emits `SpeakerSuggestion` objects with `confidence >= UNKNOWN_CONFIDENCE_THRESHOLD`.
    Callers never auto-apply these; they surface them for manual review.
    """
    if not profiles:
        return []

    lines = body.splitlines()
    unknown_line_indices = [i for i, line in enumerate(lines) if _UNKNOWN_LABEL_RE.match(line)]
    if not unknown_line_indices:
        return []

    profile_desc = _describe_profiles(profiles)
    valid_names = {p.display_name for p in profiles.values()}

    suggestions: list[SpeakerSuggestion] = []
    seen_keys: set[tuple[int, str]] = set()

    step = max(1, UNKNOWN_WINDOW_SIZE - UNKNOWN_WINDOW_OVERLAP)
    for start in range(0, len(lines), step):
        end = min(start + UNKNOWN_WINDOW_SIZE, len(lines))
        window = lines[start:end]
        window_has_unknown = any(_UNKNOWN_LABEL_RE.match(line) for line in window)
        if not window_has_unknown:
            if end >= len(lines):
                break
            continue

        numbered = "\n".join(f"{start + i + 1}. {line}" for i, line in enumerate(window))
        user = (
            f"Enrolled speakers:\n{profile_desc}\n\n"
            f"Transcript window (line numbers match the full transcript):\n{numbered}"
        )
        try:
            data = client.complete_json(_UNKNOWN_SYSTEM, user, _UNKNOWN_SCHEMA)
        except (LLMUnavailableError, LLMResponseError) as exc:
            warnings.warn(
                f"identify_unknown_speakers: LLM call failed ({exc}); returning partial results.",
                stacklevel=2,
            )
            break

        raw = data.get("suggestions") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                line_no = int(item.get("line_number", 0))
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            suggested = str(item.get("suggested_name", "")).strip()
            current = str(item.get("current_label", "")).strip()
            if not suggested or not current:
                continue
            if suggested not in valid_names:
                # LLM hallucinated a name not in the enrolled list; drop silently.
                continue
            if confidence < UNKNOWN_CONFIDENCE_THRESHOLD:
                continue
            idx = line_no - 1
            key = (idx, current)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            suggestions.append(SpeakerSuggestion(
                line_idx=idx,
                current_label=current,
                suggested_name=suggested,
                confidence=confidence,
                reason=str(item.get("reason", "")),
            ))

        if end >= len(lines):
            break

    suggestions.sort(key=lambda s: s.line_idx)
    return suggestions


# ---------------------------------------------------------------------------
# Diff rendering (used by --dry-run)
# ---------------------------------------------------------------------------

def render_diff(original: str, modified: str, *, colour: bool = True,
                context_lines: int = 1) -> str:
    """Return a unified diff between two transcript strings, optionally colourised
    for terminal output. Used by `wisper refine --dry-run`.
    """
    diff = difflib.unified_diff(
        original.splitlines(keepends=False),
        modified.splitlines(keepends=False),
        fromfile="original",
        tofile="refined",
        n=context_lines,
        lineterm="",
    )
    if not colour:
        return "\n".join(diff)

    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    CYAN = "\x1b[36m"
    RESET = "\x1b[0m"
    out = []
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{CYAN}{line}{RESET}")
        elif line.startswith("@@"):
            out.append(f"{CYAN}{line}{RESET}")
        elif line.startswith("+"):
            out.append(f"{GREEN}{line}{RESET}")
        elif line.startswith("-"):
            out.append(f"{RED}{line}{RESET}")
        else:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Top-level orchestrator used by the CLI
# ---------------------------------------------------------------------------

def refine_transcript(md: str, *, client: LLMClient,
                      hotwords: Optional[list[str]] = None,
                      character_names: Optional[list[str]] = None,
                      profiles: Optional[dict[str, SpeakerProfile]] = None,
                      tasks: Optional[list[str]] = None
                      ) -> tuple[str, list[Edit], list[SpeakerSuggestion]]:
    """Run selected refine tasks.

    Returns (refined_markdown, applied_edits, unresolved_suggestions).
    YAML frontmatter in the input is preserved verbatim.
    `tasks` is a list of {"vocabulary", "unknown"}; default: ["vocabulary"].
    """
    tasks = tasks or ["vocabulary"]
    _, body, raw_fm = parse_transcript(md)

    applied: list[Edit] = []
    new_body = body
    if "vocabulary" in tasks:
        edits = fix_vocabulary(new_body, hotwords or [], character_names or [], client)
        new_body = apply_edits(new_body, edits)
        applied = edits

    suggestions: list[SpeakerSuggestion] = []
    if "unknown" in tasks:
        suggestions = identify_unknown_speakers(new_body, profiles or {}, client)

    return raw_fm + new_body, applied, suggestions
