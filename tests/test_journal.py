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
    """Minimal stand-in for LLMClient: records prompts, returns a canned body.

    ``complete`` backs journal folds (``update_journal``); ``complete_json``
    backs summarize (``summarize_transcript``, used by ``rebuild_campaign``)
    -- returns a fixed valid ``_SUMMARY_SCHEMA``-shaped dict unless a test
    overrides ``json_body`` or ``json_error``.
    """

    provider = "fake"
    model = "fake-model"

    def __init__(self, body: str = "## Story So Far\n\nIt happened.",
                json_body: dict | None = None, json_error: Exception | None = None):
        self._body = body
        self.json_body = json_body if json_body is not None else {"summary": "A session happened."}
        self.json_error = json_error
        self.calls: list[tuple[str, str]] = []
        self.json_calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._body

    def complete_json(self, system, user, schema):
        self.json_calls.append((system, user))
        if self.json_error is not None:
            raise self.json_error
        return dict(self.json_body)


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """A tmp output dir for .summary.md sidecars, wired into journal lookups."""
    d = tmp_path / "output"
    d.mkdir()
    monkeypatch.setattr(journal, "get_output_dir", lambda: d)
    return d


def _write_summary(out_dir: Path, stem: str, text: str = "A session happened.") -> None:
    (out_dir / f"{stem}.summary.md").write_text(text, encoding="utf-8")


def _write_transcript(out_dir: Path, stem: str, text: str = "**Speaker A:** Hello there.\n") -> None:
    (out_dir / f"{stem}.md").write_text(text, encoding="utf-8")


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
# rebuild_campaign — full redrive
# ---------------------------------------------------------------------------

def test_rebuild_campaign_resummarizes_and_refolds(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("s2", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1", "**A:** First session stuff.\n")
    _write_transcript(out_dir, "s2", "**A:** Second session stuff.\n")
    # Stale summaries from a previous run -- must be overwritten, not reused.
    _write_summary(out_dir, "s1", "STALE s1 summary")
    _write_summary(out_dir, "s2", "STALE s2 summary")

    client = FakeClient(
        body="## Story So Far\n\nRebuilt narrative.",
        json_body={"summary": "Freshly regenerated recap."},
    )
    result = journal.rebuild_campaign("my-game", client, {}, data_dir=tmp_path)

    assert result.resummarized == ["s1", "s2"]
    assert result.skipped == []
    # Both summary sidecars were regenerated with the new content.
    assert "Freshly regenerated recap." in (out_dir / "s1.summary.md").read_text(encoding="utf-8")
    assert "Freshly regenerated recap." in (out_dir / "s2.summary.md").read_text(encoding="utf-8")
    assert "STALE" not in (out_dir / "s1.summary.md").read_text(encoding="utf-8")
    # Both transcripts were actually fed to the summarizer.
    assert "First session stuff." in client.json_calls[0][1]
    assert "Second session stuff." in client.json_calls[1][1]
    # Journal folded both, in order.
    assert result.journal is not None
    assert result.journal.journaled_sessions == ["s1", "s2"]


def test_rebuild_campaign_resets_journal_instead_of_building_on_stale_content(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    _write_summary(out_dir, "s1")

    # Pre-existing journal that names a session no longer in the campaign.
    journal.update_journal("my-game", FakeClient(body="## Story So Far\n\nOld stale journal."),
                           {}, data_dir=tmp_path)

    client = FakeClient(body="## Story So Far\n\nFresh rebuild.")
    result = journal.rebuild_campaign("my-game", client, {}, data_dir=tmp_path)

    # The fold prompt must NOT contain the stale journal body -- rebuild
    # starts from a blank journal, not last run's content.
    assert "Old stale journal" not in client.calls[0][1]
    assert result.journal.journaled_sessions == ["s1"]


def test_rebuild_campaign_skips_missing_transcript(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("ghost", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    # "ghost" has no .md on disk at all.

    client = FakeClient()
    result = journal.rebuild_campaign("my-game", client, {}, data_dir=tmp_path)

    assert result.resummarized == ["s1"]
    assert len(result.skipped) == 1
    assert result.skipped[0][0] == "ghost"
    assert "not found" in result.skipped[0][1]


def test_rebuild_campaign_skips_llm_failure_and_continues(tmp_path, out_dir):
    from wisper_transcribe.llm.errors import LLMResponseError

    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("bad", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("good", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "bad")
    _write_transcript(out_dir, "good")

    client = FakeClient(json_error=LLMResponseError("boom"))
    result = journal.rebuild_campaign("my-game", client, {}, data_dir=tmp_path)

    # Both sessions hit the failing client (json_error applies to every
    # complete_json call), so nothing gets summarized and nothing folds --
    # exercises that a summarize failure is recorded, not raised.
    assert result.resummarized == []
    assert {stem for stem, _ in result.skipped} == {"bad", "good"}
    assert result.journal is None


def test_rebuild_campaign_partial_llm_failure_still_folds_successes(tmp_path, out_dir):
    from wisper_transcribe.llm.errors import LLMResponseError

    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    move_transcript_to_campaign("s2", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    _write_transcript(out_dir, "s2")

    class FlakyClient(FakeClient):
        def complete_json(self, system, user, schema):
            if "s1" in user or len(self.json_calls) == 0:
                self.json_calls.append((system, user))
                raise LLMResponseError("s1 boom")
            return super().complete_json(system, user, schema)

    client = FlakyClient()
    result = journal.rebuild_campaign("my-game", client, {}, data_dir=tmp_path)

    assert result.resummarized == ["s2"]
    assert [s for s, _ in result.skipped] == ["s1"]
    assert result.journal is not None
    assert result.journal.journaled_sessions == ["s2"]


def test_rebuild_campaign_unknown_campaign_raises(tmp_path, out_dir):
    with pytest.raises(KeyError):
        journal.rebuild_campaign("ghost", FakeClient(), {}, data_dir=tmp_path)


def test_rebuild_campaign_invalid_slug_raises(tmp_path, out_dir):
    with pytest.raises(ValueError):
        journal.rebuild_campaign("../x", FakeClient(), {}, data_dir=tmp_path)


def test_rebuild_campaign_reports_progress(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")

    messages: list[str] = []
    journal.rebuild_campaign("my-game", FakeClient(), {}, data_dir=tmp_path,
                             on_progress=messages.append)

    assert any("Summarizing s1" in m for m in messages)
    assert any("Folding s1" in m for m in messages)


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


def test_cli_campaigns_journal_rebuild_with_yes(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")

    monkeypatch.setattr(cli, "_get_llm_client", lambda *a, **k: FakeClient())

    result = CliRunner().invoke(
        cli.main, ["campaigns", "journal", "my-game", "--rebuild", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "Re-summarized: 1" in result.output
    assert journal.journal_path("my-game", data_dir=tmp_path).exists()


def test_cli_campaigns_journal_rebuild_prompts_without_yes(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    monkeypatch.setattr(cli, "_get_llm_client", lambda *a, **k: FakeClient())

    # Decline the confirmation ("n") -- must abort before any LLM call.
    result = CliRunner().invoke(
        cli.main, ["campaigns", "journal", "my-game", "--rebuild"], input="n\n"
    )
    assert result.exit_code != 0
    assert not (out_dir / "s1.summary.md").exists()


def test_cli_campaigns_journal_rebuild_no_transcripts(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)

    result = CliRunner().invoke(
        cli.main, ["campaigns", "journal", "my-game", "--rebuild", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "no transcripts" in result.output


def test_cli_campaigns_journal_rebuild_mutually_exclusive(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)

    result = CliRunner().invoke(
        cli.main, ["campaigns", "journal", "my-game", "--rebuild", "--all", "--yes"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# JobQueue worker: _run_journal_job
# ---------------------------------------------------------------------------

def test_run_journal_job_folds_and_completes(tmp_path, out_dir, monkeypatch):
    """_run_journal_job builds a client, folds the next session, completes."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web import jobs as jobs_mod

    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s1")

    # The worker resolves get_client from config — hand it our FakeClient.
    monkeypatch.setattr(jobs_mod, "_StderrCapture", lambda job: _Devnull())
    import wisper_transcribe.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_client", lambda *a, **k: FakeClient())

    q = jobs_mod.JobQueue()
    job = q.submit_journal("my-game")
    q._run_journal_job(job)

    assert job.status == jobs_mod.COMPLETED
    assert journal.journal_path("my-game", data_dir=tmp_path).exists()
    assert job.output_path and job.output_path.endswith("journal.md")


def test_run_journal_job_rebuild_resummarizes_and_completes(tmp_path, out_dir, monkeypatch):
    """rebuild=True redrives every transcript instead of folding pending ones."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web import jobs as jobs_mod

    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    _write_summary(out_dir, "s1", "STALE")

    monkeypatch.setattr(jobs_mod, "_StderrCapture", lambda job: _Devnull())
    import wisper_transcribe.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_client", lambda *a, **k: FakeClient(
        json_body={"summary": "Rebuilt via job."}
    ))

    q = jobs_mod.JobQueue()
    job = q.submit_journal("my-game", rebuild=True)
    assert job.kwargs["rebuild"] is True
    q._run_journal_job(job)

    assert job.status == jobs_mod.COMPLETED
    assert "STALE" not in (out_dir / "s1.summary.md").read_text(encoding="utf-8")
    assert "Rebuilt via job." in (out_dir / "s1.summary.md").read_text(encoding="utf-8")
    assert job.output_path and job.output_path.endswith("journal.md")
    assert any("Re-summarized: 1" in line for line in job.log_lines)


class _Devnull:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass
