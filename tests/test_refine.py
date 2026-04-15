"""Tests for refine.py — transcript vocabulary fix + unknown-speaker ID.

All LLM calls are mocked; no network, no SDK required.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wisper_transcribe.models import Edit, SpeakerProfile, SpeakerSuggestion
from wisper_transcribe.refine import (
    _validate_vocab_edit,
    apply_edits,
    fix_vocabulary,
    identify_unknown_speakers,
    parse_transcript,
    refine_transcript,
    render_diff,
)


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------

def test_parse_transcript_with_frontmatter():
    md = (
        "---\n"
        "title: Session 01\n"
        "speakers:\n"
        "  - name: Alice\n"
        "---\n"
        "**Alice** *(00:01)*: Hello.\n"
    )
    fm, body, raw = parse_transcript(md)
    assert fm["title"] == "Session 01"
    assert body.startswith("**Alice**")
    assert raw.startswith("---\n") and raw.endswith("---\n")


def test_parse_transcript_no_frontmatter():
    md = "**Alice**: Hello.\n"
    fm, body, raw = parse_transcript(md)
    assert fm == {}
    assert body == md
    assert raw == ""


def test_parse_transcript_invalid_yaml_returns_empty_dict():
    md = "---\nnot: valid: : yaml\n---\nbody here\n"
    fm, body, raw = parse_transcript(md)
    # yaml.safe_load will raise; parse_transcript handles defensively.
    assert fm == {}
    assert "body here" in body


# ---------------------------------------------------------------------------
# _validate_vocab_edit / apply_edits
# ---------------------------------------------------------------------------

def test_validate_vocab_edit_accepts_close_match():
    edit = Edit(original="Kira", corrected="Kyra")
    assert _validate_vocab_edit(edit, ["Kyra", "Golarion"])


def test_validate_vocab_edit_rejects_freeform():
    edit = Edit(original="The party entered", corrected="The heroes stepped in")
    assert not _validate_vocab_edit(edit, ["Kyra", "Golarion"])


def test_validate_vocab_edit_rejects_identical():
    edit = Edit(original="Kyra", corrected="Kyra")
    assert not _validate_vocab_edit(edit, ["Kyra"])


def test_validate_vocab_edit_rejects_empty():
    assert not _validate_vocab_edit(Edit("", ""), ["Kyra"])


def test_apply_edits_replaces_substrings():
    body = "**Alice** *(00:01)*: I saw Kira rolling Golarian dice.\n"
    edits = [Edit("Kira", "Kyra"), Edit("Golarian", "Golarion")]
    out = apply_edits(body, edits)
    assert "Kyra" in out and "Golarion" in out
    assert "Kira" not in out


def test_apply_edits_idempotent():
    body = "name Kyra name\n"
    edits = [Edit("Kira", "Kyra")]
    # `original` not present — output unchanged.
    assert apply_edits(body, edits) == body


# ---------------------------------------------------------------------------
# fix_vocabulary
# ---------------------------------------------------------------------------

def _mock_client_json(return_value):
    client = MagicMock()
    client.complete_json.return_value = return_value
    client.provider = "mock"
    client.model = "mock-1"
    return client


def test_fix_vocabulary_accepts_valid_edit():
    body = "**Alice**: I met Kira in Golarian.\n"
    client = _mock_client_json({
        "changes": [
            {"original": "Kira", "corrected": "Kyra"},
            {"original": "Golarian", "corrected": "Golarion"},
        ]
    })
    edits = fix_vocabulary(body, hotwords=["Kyra", "Golarion"],
                           character_names=[], client=client)
    assert {(e.original, e.corrected) for e in edits} == {
        ("Kira", "Kyra"),
        ("Golarian", "Golarion"),
    }


def test_fix_vocabulary_rejects_freeform_edits():
    body = "**Alice**: I rolled a 20.\n"
    client = _mock_client_json({
        "changes": [
            {"original": "I rolled a 20", "corrected": "I achieved a critical success"}
        ]
    })
    with pytest.warns(UserWarning, match="rejected"):
        edits = fix_vocabulary(body, hotwords=["Kyra"], character_names=[], client=client)
    assert edits == []


def test_fix_vocabulary_no_known_terms_returns_empty():
    with pytest.warns(UserWarning, match="no hotwords"):
        out = fix_vocabulary("body", hotwords=[], character_names=[], client=MagicMock())
    assert out == []


def test_fix_vocabulary_soft_fails_on_llm_error():
    from wisper_transcribe.llm.errors import LLMUnavailableError

    client = MagicMock()
    client.complete_json.side_effect = LLMUnavailableError("unreachable")
    with pytest.warns(UserWarning, match="LLM call failed"):
        edits = fix_vocabulary("**Alice**: Kira\n", hotwords=["Kyra"],
                                character_names=[], client=client)
    assert edits == []


def test_fix_vocabulary_dedupes_across_batches():
    # Two batches that both propose the same change → only one Edit returned.
    from wisper_transcribe import refine as _refine

    body = "\n".join([f"**Alice**: line Kira {i}" for i in range(50)])
    change = {"original": "Kira", "corrected": "Kyra"}
    client = _mock_client_json({"changes": [change]})
    # Force small batch size so we have more than one call.
    old = _refine.VOCABULARY_BATCH_SIZE
    _refine.VOCABULARY_BATCH_SIZE = 10
    try:
        edits = fix_vocabulary(body, hotwords=["Kyra"], character_names=[], client=client)
    finally:
        _refine.VOCABULARY_BATCH_SIZE = old
    assert len(edits) == 1


# ---------------------------------------------------------------------------
# identify_unknown_speakers
# ---------------------------------------------------------------------------

def _fake_profile(name: str, display: str, role: str = "Player", notes: str = "") -> SpeakerProfile:
    return SpeakerProfile(
        name=name, display_name=display, role=role,
        embedding_path=Path(f"{name}.npy"),
        enrolled_date="2026-04-05", enrollment_source="test.mp3",
        notes=notes,
    )


def test_identify_unknown_speakers_filters_low_confidence():
    body = (
        "**Alice**: Let's enter the dungeon.\n"
        "**Unknown Speaker 1** *(02:10)*: I attack the goblin.\n"
        "**Alice**: Nice hit.\n"
    )
    profiles = {"alice": _fake_profile("alice", "Alice", role="DM"),
                "bob": _fake_profile("bob", "Bob", role="Player")}
    client = _mock_client_json({
        "suggestions": [
            {"line_number": 2, "current_label": "Unknown Speaker 1",
             "suggested_name": "Bob", "confidence": 0.9, "reason": "player action"},
            {"line_number": 2, "current_label": "Unknown Speaker 1",
             "suggested_name": "Alice", "confidence": 0.4, "reason": "weak"},
        ]
    })
    out = identify_unknown_speakers(body, profiles, client)
    assert len(out) == 1
    assert out[0].suggested_name == "Bob"
    assert out[0].confidence >= 0.75


def test_identify_unknown_speakers_rejects_hallucinated_names():
    body = "**Unknown Speaker 1**: Hi!\n"
    profiles = {"alice": _fake_profile("alice", "Alice")}
    client = _mock_client_json({
        "suggestions": [{
            "line_number": 1, "current_label": "Unknown Speaker 1",
            "suggested_name": "Carol", "confidence": 0.99,
        }]
    })
    out = identify_unknown_speakers(body, profiles, client)
    # Carol is not enrolled → dropped.
    assert out == []


def test_identify_unknown_speakers_skips_when_no_unknowns():
    body = "**Alice**: hi\n**Bob**: hello\n"
    profiles = {"alice": _fake_profile("alice", "Alice")}
    client = _mock_client_json({"suggestions": []})
    assert identify_unknown_speakers(body, profiles, client) == []
    # Client should not have been called because there were no windows with unknowns.
    assert client.complete_json.call_count == 0


def test_identify_unknown_speakers_empty_profiles_returns_empty():
    body = "**Unknown Speaker 1**: Anyone here?\n"
    assert identify_unknown_speakers(body, {}, MagicMock()) == []


# ---------------------------------------------------------------------------
# render_diff
# ---------------------------------------------------------------------------

def test_render_diff_plain():
    diff = render_diff("aaa\nbbb\n", "aaa\nBBB\n", colour=False)
    assert "-bbb" in diff
    assert "+BBB" in diff


def test_render_diff_colour_contains_ansi():
    diff = render_diff("aaa\n", "AAA\n", colour=True)
    assert "\x1b[" in diff


# ---------------------------------------------------------------------------
# refine_transcript orchestration
# ---------------------------------------------------------------------------

def test_refine_transcript_preserves_frontmatter_verbatim():
    md = "---\ntitle: T\n---\n**Alice**: Kira rolls.\n"
    client = _mock_client_json({
        "changes": [{"original": "Kira", "corrected": "Kyra"}]
    })
    refined, edits, sugg = refine_transcript(
        md, client=client, hotwords=["Kyra"], character_names=[],
        profiles={}, tasks=["vocabulary"],
    )
    # Frontmatter is preserved byte-for-byte, only the body is edited.
    assert refined.startswith("---\ntitle: T\n---\n")
    assert "Kyra" in refined and "Kira" not in refined
    assert len(edits) == 1
    assert sugg == []


def test_refine_transcript_vocabulary_only_does_not_call_unknown_pass():
    md = "**Unknown Speaker 1**: I attack.\n"
    client = MagicMock()
    client.complete_json.return_value = {"changes": []}
    client.provider = "mock"
    client.model = "mock-1"

    refined, edits, sugg = refine_transcript(
        md, client=client, hotwords=["Kyra"], character_names=[],
        profiles={"alice": _fake_profile("alice", "Alice")},
        tasks=["vocabulary"],
    )
    # Only the vocabulary pass should have called the client (one batch).
    assert client.complete_json.call_count == 1
    assert sugg == []


def test_refine_transcript_unknown_task_returns_suggestions():
    md = "**Unknown Speaker 1**: I attack.\n**Alice**: Nice.\n"
    profiles = {"alice": _fake_profile("alice", "Alice"),
                "bob": _fake_profile("bob", "Bob")}
    # First call: vocabulary (empty). Second call: unknown suggestions.
    client = MagicMock()
    client.complete_json.side_effect = [
        {"changes": []},
        {"suggestions": [{
            "line_number": 1, "current_label": "Unknown Speaker 1",
            "suggested_name": "Bob", "confidence": 0.88, "reason": "attack verb",
        }]},
    ]
    client.provider = "mock"
    client.model = "mock-1"

    refined, edits, sugg = refine_transcript(
        md, client=client, hotwords=["Kyra"], character_names=[],
        profiles=profiles, tasks=["vocabulary", "unknown"],
    )
    assert edits == []
    assert len(sugg) == 1 and sugg[0].suggested_name == "Bob"
