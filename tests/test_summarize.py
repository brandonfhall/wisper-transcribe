"""Tests for summarize.py — campaign notes generation + Obsidian rendering."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wisper_transcribe.models import (
    LootChange,
    NPCMention,
    SpeakerProfile,
    SpeakerSuggestion,
    SummaryNote,
)
from wisper_transcribe.summarize import (
    _linkify,
    _link_terms,
    default_summary_path,
    render_markdown,
    summarize,
    summarize_transcript,
)


def _fake_profile(name: str, display: str, role: str = "Player", notes: str = "") -> SpeakerProfile:
    return SpeakerProfile(
        name=name, display_name=display, role=role,
        embedding_path=Path(f"{name}.npy"),
        enrolled_date="2026-04-05", enrollment_source="test.mp3",
        notes=notes,
    )


def _mock_client(payload: dict):
    client = MagicMock()
    client.complete_json.return_value = payload
    client.provider = "mock"
    client.model = "mock-1"
    return client


# ---------------------------------------------------------------------------
# summarize()
# ---------------------------------------------------------------------------

_PAYLOAD = {
    "summary": "The party explored the crypt.",
    "session_title": "Into the Crypt",
    "loot": [
        {"item": "Wand of Magic Missile", "quantity": "1", "recipient": "Kyra", "note": "in iron chest"},
        {"item": "gold", "quantity": "+120 gp", "recipient": "Thorin", "note": ""},
    ],
    "npcs": [
        {"name": "Aziel", "role": "dragon",
         "first_mentioned_at": "14:22", "description": "guarded the hoard"},
        {"name": "Kyra", "role": "player",
         "first_mentioned_at": "01:10", "description": "should be filtered out"},
    ],
    "followups": ["Who sent the letter?", ""],
}


def test_summarize_basic_structure():
    client = _mock_client(_PAYLOAD)
    profiles = {
        "kyra": _fake_profile("kyra", "Kyra", role="Player"),
        "thorin": _fake_profile("thorin", "Thorin", role="Player"),
    }
    note = summarize(
        body="body text", frontmatter={"title": "Raw Title", "duration": "1:00"},
        profiles=profiles, client=client,
        source_transcript="ep01.md",
    )
    assert isinstance(note, SummaryNote)
    assert note.summary.startswith("The party")
    assert note.session_title == "Into the Crypt"
    # NPC "Kyra" is filtered because she's an enrolled player.
    assert [n.name for n in note.npcs] == ["Aziel"]
    assert len(note.loot) == 2
    # Empty follow-up strings are filtered out.
    assert note.followups == ["Who sent the letter?"]
    assert note.provider == "mock"
    assert note.model == "mock-1"


def test_summarize_defaults_title_to_frontmatter():
    payload = dict(_PAYLOAD)
    payload["session_title"] = ""
    client = _mock_client(payload)
    note = summarize("body", {"title": "Raw Title"}, {}, client)
    assert note.session_title == "Raw Title"


def test_summarize_invalid_response_raises():
    from wisper_transcribe.llm.errors import LLMResponseError

    client = MagicMock()
    client.complete_json.return_value = "not a dict"
    client.provider = "mock"
    client.model = "mock-1"
    with pytest.raises(LLMResponseError):
        summarize("body", {}, {}, client)


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

def test_render_markdown_has_expected_sections():
    profiles = {
        "kyra": _fake_profile("kyra", "Kyra"),
        "thorin": _fake_profile("thorin", "Thorin"),
    }
    note = SummaryNote(
        summary="Kyra found a wand.",
        loot=[LootChange(item="Wand", quantity="1", recipient="Kyra", note="in chest")],
        npcs=[NPCMention(name="Aziel", role="dragon", first_mentioned_at="14:22",
                         description="guarded the hoard")],
        followups=["Who sent the letter?"],
        session_title="Into the Crypt",
        source_transcript="ep01.md",
        generated_at="2026-04-15T12:00:00",
        provider="anthropic",
        model="claude-sonnet-4-6",
        refined=True,
    )
    out = render_markdown(note, profiles=profiles)
    assert "---" in out
    assert "type: session-summary" in out
    assert "refined: true" in out
    assert "# Into the Crypt" in out
    assert "## Summary" in out
    assert "## Loot & Inventory" in out
    assert "## NPCs" in out
    assert "## Follow-ups" in out
    # Obsidian wiki-links for enrolled speakers.
    assert "[[Kyra]]" in out
    # Unknown name (Aziel) is not an enrolled speaker — no wiki-link.
    assert "[[Aziel]]" not in out


def test_render_markdown_empty_sections_show_placeholder():
    note = SummaryNote(
        summary="A short session.",
        session_title="s", generated_at="t",
    )
    out = render_markdown(note, profiles={})
    assert "_No inventory changes recorded._" in out
    assert "_No notable NPCs recorded._" in out
    assert "_None flagged._" in out


def test_render_markdown_unresolved_speakers_section():
    note = SummaryNote(
        summary="x", session_title="s", generated_at="t",
        unresolved_speakers=[
            SpeakerSuggestion(line_idx=4, current_label="Unknown Speaker 1",
                              suggested_name="Bob", confidence=0.82,
                              reason="first-person action verb"),
        ],
    )
    out = render_markdown(note, profiles={})
    assert "## Unresolved Speakers" in out
    assert "Line 5" in out
    assert "Unknown Speaker 1" in out
    assert "Bob" in out
    assert "82%" in out


def test_render_markdown_respects_sections_filter():
    note = SummaryNote(summary="s", session_title="t", generated_at="u",
                       loot=[LootChange(item="x")])
    out = render_markdown(note, sections=["summary"])
    assert "## Summary" in out
    assert "## Loot" not in out
    assert "## NPCs" not in out


# ---------------------------------------------------------------------------
# _link_terms / _linkify
# ---------------------------------------------------------------------------

def test_link_terms_collects_display_name_and_notes():
    profiles = {
        "alice": _fake_profile("alice", "Alice", notes="Aziel, Kyra"),
        "bob": _fake_profile("bob", "Bob", notes="voice_of:alice"),
    }
    terms = _link_terms(profiles)
    assert "Alice" in terms
    assert "Aziel" in terms and "Kyra" in terms
    assert "Bob" in terms
    # voice_of: prefix is stripped.
    assert "voice_of:alice" not in terms


def test_linkify_wraps_whole_words_only():
    out = _linkify("Alice met Alicia", {"Alice"})
    assert "[[Alice]]" in out
    # "Alicia" should not be wrapped because we require word-boundary match.
    assert "Alicia" in out and "[[Alicia]]" not in out


def test_linkify_idempotent():
    out1 = _linkify("Alice said hi", {"Alice"})
    out2 = _linkify(out1, {"Alice"})
    # Running twice should not double-wrap.
    assert out1 == out2
    assert out2.count("[[Alice]]") == 1


# ---------------------------------------------------------------------------
# default_summary_path / summarize_transcript orchestration
# ---------------------------------------------------------------------------

def test_default_summary_path():
    assert default_summary_path(Path("/tmp/ep01.md")) == Path("/tmp/ep01.summary.md")


def test_summarize_transcript_parses_frontmatter():
    md = "---\ntitle: My Session\n---\n**Alice**: hi\n"
    client = _mock_client({"summary": "ok"})
    note = summarize_transcript(md, profiles={}, client=client,
                                source_transcript="ep.md")
    # The LLM call saw the frontmatter-stripped body.
    user_prompt = client.complete_json.call_args[0][1]
    assert "Transcript body:\n**Alice**: hi" in user_prompt
    # Title propagated from frontmatter because payload didn't set one.
    assert note.session_title == "My Session"
