"""Tests for journal.py — rolling campaign journal.

No real LLM or network: a FakeClient stands in for LLMClient, and session
``.summary.md`` sidecars are written to a tmp output dir that
``journal.get_output_dir`` is patched to return.
"""
from pathlib import Path

import pytest

from wisper_transcribe import journal
from wisper_transcribe.campaign_manager import (
    create_campaign,
    move_transcript_to_campaign,
)


class FakeClient:
    """Minimal stand-in for LLMClient: records prompts, returns a canned body."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, body: str = "## Story So Far\n\nIt happened."):
        self._body = body
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._body

    def complete_json(self, system, user, schema):  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """A tmp output dir for .summary.md sidecars, wired into journal lookups."""
    d = tmp_path / "output"
    d.mkdir()
    monkeypatch.setattr(journal, "get_output_dir", lambda: d)
    return d


def _write_summary(out_dir: Path, stem: str, text: str = "A session happened.") -> None:
    (out_dir / f"{stem}.summary.md").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Paths + frontmatter
# ---------------------------------------------------------------------------

def test_journal_path_uses_campaign_slug_dir(tmp_path):
    p = journal.journal_path("my-game", data_dir=tmp_path)
    assert p == tmp_path / "campaigns" / "my-game" / "journal.md"


def test_journal_path_invalid_slug_returns_none(tmp_path):
    assert journal.journal_path("../escape", data_dir=tmp_path) is None


def test_render_parse_roundtrip():
    rendered = journal.render_journal(
        "my-game", "## Story So Far\n\nStuff.", ["s1", "s2"], "ollama", "llama3.1:8b"
    )
    meta, body = journal.parse_journal(rendered)
    assert meta["type"] == "campaign-journal"
    assert meta["campaign"] == "my-game"
    assert meta["journaled_sessions"] == ["s1", "s2"]
    assert body == "## Story So Far\n\nStuff."


def test_parse_journal_no_frontmatter():
    meta, body = journal.parse_journal("just a body, no frontmatter")
    assert meta == {}
    assert body == "just a body, no frontmatter"


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def test_unjournalled_lists_only_summarized_sessions(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    for stem in ("s1", "s2", "s3"):
        move_transcript_to_campaign(stem, "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")
    _write_summary(out_dir, "s3")  # s2 has no summary → skipped

    pending = journal.unjournalled_sessions("my-game", data_dir=tmp_path)
    assert pending == ["s1", "s3"]


def test_unjournalled_excludes_already_folded(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("s2", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")
    _write_summary(out_dir, "s2")

    journal.update_journal("my-game", FakeClient(), {}, data_dir=tmp_path)  # folds s1
    pending = journal.unjournalled_sessions("my-game", data_dir=tmp_path)
    assert pending == ["s2"]


def test_unjournalled_invalid_slug_returns_empty(tmp_path):
    assert journal.unjournalled_sessions("../x", data_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# update_journal
# ---------------------------------------------------------------------------

def test_update_journal_first_fold_writes_file(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1", "The party met in a tavern.")
    client = FakeClient(body="## Story So Far\n\nThe party met.")

    result = journal.update_journal("my-game", client, {}, data_dir=tmp_path)

    assert result is not None
    assert result.folded == "s1"
    assert result.journaled_sessions == ["s1"]
    assert result.path.exists()
    meta, body = journal.parse_journal(result.path.read_text(encoding="utf-8"))
    assert meta["journaled_sessions"] == ["s1"]
    assert "The party met." in body
    # The session summary must have been handed to the LLM.
    assert "The party met in a tavern." in client.calls[0][1]


def test_update_journal_second_fold_includes_prior_journal(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("s2", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")
    _write_summary(out_dir, "s2")

    c1 = FakeClient(body="## Story So Far\n\nSession one body.")
    journal.update_journal("my-game", c1, {}, data_dir=tmp_path)

    c2 = FakeClient(body="## Story So Far\n\nSessions one and two.")
    result = journal.update_journal("my-game", c2, {}, data_dir=tmp_path)

    assert result.folded == "s2"
    assert result.journaled_sessions == ["s1", "s2"]
    # The previous journal body must be fed back into the second fold.
    assert "Session one body." in c2.calls[0][1]


def test_update_journal_explicit_session(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("s2", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")
    _write_summary(out_dir, "s2")

    result = journal.update_journal(
        "my-game", FakeClient(), {}, session_stem="s2", data_dir=tmp_path
    )
    assert result.folded == "s2"
    assert result.journaled_sessions == ["s2"]


def test_update_journal_nothing_pending_returns_none(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    # no summary written → nothing to fold
    result = journal.update_journal("my-game", FakeClient(), {}, data_dir=tmp_path)
    assert result is None


def test_update_journal_explicit_missing_summary_raises(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        journal.update_journal(
            "my-game", FakeClient(), {}, session_stem="s1", data_dir=tmp_path
        )


def test_update_journal_unknown_campaign_raises(tmp_path, out_dir):
    with pytest.raises(KeyError):
        journal.update_journal("ghost", FakeClient(), {}, data_dir=tmp_path)


def test_update_journal_invalid_slug_raises(tmp_path, out_dir):
    with pytest.raises(ValueError):
        journal.update_journal("../x", FakeClient(), {}, data_dir=tmp_path)


def test_update_journal_strips_code_fence(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")
    client = FakeClient(body="```markdown\n## Story So Far\n\nFenced.\n```")

    result = journal.update_journal("my-game", client, {}, data_dir=tmp_path)
    _, body = journal.parse_journal(result.path.read_text(encoding="utf-8"))
    assert body.startswith("## Story So Far")
    assert "```" not in body


# ---------------------------------------------------------------------------
# CLI: wisper campaigns journal
# ---------------------------------------------------------------------------

def test_cli_campaigns_journal_folds_next(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")

    monkeypatch.setattr(cli, "_get_llm_client", lambda *a, **k: FakeClient())

    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "my-game"])
    assert result.exit_code == 0, result.output
    assert journal.journal_path("my-game", data_dir=tmp_path).exists()


def test_cli_campaigns_journal_nothing_to_do(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    # no summary → nothing to fold; must not call the LLM
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM client should not be created")

    monkeypatch.setattr(cli, "_get_llm_client", _boom)
    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "my-game"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert called["n"] == 0


def test_cli_campaigns_journal_unknown_campaign(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "ghost"])
    assert result.exit_code != 0
    assert "not found" in result.output
