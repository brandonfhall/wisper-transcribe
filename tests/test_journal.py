"""Tests for journal.py — rolling campaign journal.

No real LLM or network: a FakeClient stands in for LLMClient, and session
``.summary.md`` sidecars are written to a tmp output root (``WISPER_OUTPUT_DIR``).
"""
from pathlib import Path

import pytest

from wisper_transcribe import campaign_folders, journal
from wisper_transcribe.campaign_manager import (
    create_campaign,
    load_campaigns,
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
    """A tmp output root for .summary.md sidecars and campaign folders."""
    d = tmp_path / "output"
    d.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(d))
    return d


def _write_summary(out_dir: Path, stem: str, text: str = "A session happened.") -> None:
    (out_dir / f"{stem}.summary.md").write_text(text, encoding="utf-8")


def _write_transcript(out_dir: Path, stem: str, text: str = "**Speaker A:** Hello there.\n") -> None:
    (out_dir / f"{stem}.md").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Paths + frontmatter
# ---------------------------------------------------------------------------

def test_journal_path_is_in_a_claimed_campaign_folder(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    assert journal.journal_path("my-game", data_dir=tmp_path) is None  # unclaimed
    cid = load_campaigns(tmp_path)["my-game"].id
    campaign_folders.ensure_folder(cid, data_dir=tmp_path)
    assert (journal.journal_path("my-game", data_dir=tmp_path)
            == out_dir / "My Game" / "My Game Journal.md")


def test_journal_path_for_an_unknown_campaign_is_none(tmp_path, out_dir):
    assert journal.journal_path("nope", data_dir=tmp_path) is None


def test_journal_path_invalid_slug_returns_none(tmp_path):
    assert journal.journal_path("../escape", data_dir=tmp_path) is None


def test_render_parse_roundtrip():
    rendered = journal.render_journal(
        "my-game", "## Story So Far\n\nStuff.", "ollama", "llama3.1:8b"
    )
    meta, body = journal.parse_journal(rendered)
    assert meta["type"] == "campaign-journal"
    assert meta["campaign"] == "my-game"
    assert "journaled_sessions" not in meta  # lives in the database now
    assert body == "## Story So Far\n\nStuff."
    exported = journal.render_journal("my-game", "x", "ollama", "m", journaled_sessions=["s1"])
    assert journal.parse_journal(exported)[0]["journaled_sessions"] == ["s1"]


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
    assert "journaled_sessions" not in meta
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s1"]
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
    assert "Summarized: 1" in result.output  # s1 had no summary yet
    assert journal.journal_path("my-game", data_dir=tmp_path).exists()


def _cli_game(tmp_path, out_dir, monkeypatch, client):
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out_dir))
    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    _write_summary(out_dir, "s1", "KEEP ME")
    monkeypatch.setattr(cli, "_get_llm_client", lambda *a, **k: client)
    return cli


def test_cli_rebuild_refolds_existing_summaries(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner

    client = FakeClient()
    cli = _cli_game(tmp_path, out_dir, monkeypatch, client)
    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "my-game", "--rebuild"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "About 1 LLM calls" in result.output
    assert (out_dir / "s1.summary.md").read_text(encoding="utf-8") == "KEEP ME"
    assert client.json_calls == []


def test_cli_rebuild_resummarize_overwrites_summaries(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner

    client = FakeClient(json_body={"summary": "Fresh."})
    cli = _cli_game(tmp_path, out_dir, monkeypatch, client)
    result = CliRunner().invoke(
        cli.main, ["campaigns", "journal", "my-game", "--rebuild", "--resummarize"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "2 LLM calls" in result.output
    assert "Fresh." in (out_dir / "s1.summary.md").read_text(encoding="utf-8")


def test_cli_resummarize_requires_rebuild(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner

    cli = _cli_game(tmp_path, out_dir, monkeypatch, FakeClient())
    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "my-game", "--resummarize"])
    assert result.exit_code != 0
    assert "only applies with --rebuild" in result.output


def test_cli_export_includes_journaled_sessions(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner

    cli = _cli_game(tmp_path, out_dir, monkeypatch, FakeClient())
    journal.update_journal("my-game", FakeClient(), {}, session_stem="s1")
    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "my-game", "--export"])
    assert result.exit_code == 0, result.output
    assert journal.parse_journal(result.output)[0]["journaled_sessions"] == ["s1"]

    dest = tmp_path / "exported.md"
    result = CliRunner().invoke(cli.main, ["campaigns", "journal", "my-game", "--export", "-o", str(dest)])
    assert result.exit_code == 0
    assert "journaled_sessions" in dest.read_text(encoding="utf-8")


def test_cli_campaigns_show_reports_stale_journal(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner

    from wisper_transcribe.campaign_manager import remove_transcript_from_campaign

    cli = _cli_game(tmp_path, out_dir, monkeypatch, FakeClient())
    journal.update_journal("my-game", FakeClient(), {}, session_stem="s1")
    result = CliRunner().invoke(cli.main, ["campaigns", "show", "my-game"])
    assert "Journal:  up to date" in result.output
    remove_transcript_from_campaign("s1")
    result = CliRunner().invoke(cli.main, ["campaigns", "show", "my-game"])
    assert "STALE since" in result.output
    assert "--rebuild" in result.output


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
    assert job.output_path and job.output_path.endswith("My Game Journal.md")


def test_run_journal_job_rebuild_resummarizes_and_completes(tmp_path, out_dir, monkeypatch):
    """rebuild + resummarize redrives every transcript instead of folding pending ones."""
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
    job = q.submit_journal("my-game", rebuild=True, resummarize=True)
    assert job.kwargs["rebuild"] is True and job.kwargs["resummarize"] is True
    q._run_journal_job(job)

    assert job.status == jobs_mod.COMPLETED
    assert "STALE" not in (out_dir / "s1.summary.md").read_text(encoding="utf-8")
    assert "Rebuilt via job." in (out_dir / "s1.summary.md").read_text(encoding="utf-8")
    assert job.output_path and job.output_path.endswith("My Game Journal.md")
    assert any("Summarized: 1" in line for line in job.log_lines)


def test_run_journal_job_rebuild_refolds_existing_summaries(tmp_path, out_dir, monkeypatch):
    """rebuild alone re-folds the summaries as they are (no re-summarize)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web import jobs as jobs_mod

    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s1")
    _write_summary(out_dir, "s1", "KEEP ME")
    client = FakeClient()

    monkeypatch.setattr(jobs_mod, "_StderrCapture", lambda job: _Devnull())
    import wisper_transcribe.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_client", lambda *a, **k: client)

    q = jobs_mod.JobQueue()
    job = q.submit_journal("my-game", rebuild=True)
    q._run_journal_job(job)

    assert job.status == jobs_mod.COMPLETED
    assert (out_dir / "s1.summary.md").read_text(encoding="utf-8") == "KEEP ME"
    assert client.json_calls == [] and len(client.calls) == 1
    assert "KEEP ME" in client.calls[0][1]


class _Devnull:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# Journal write rule: entries in the DB, body in the file
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402

from wisper_transcribe import db  # noqa: E402


def _campaign_row(data_dir, slug="my-game"):
    with db.connection(data_dir) as conn:
        return dict(conn.execute(
            "SELECT journal_sha256, journal_stale_since FROM campaigns WHERE slug = ?", (slug,)
        ).fetchone())


def _folded_game(tmp_path, out_dir, stems=("s1", "s2")):
    create_campaign("My Game", data_dir=tmp_path)
    for stem in stems:
        move_transcript_to_campaign(stem, "my-game", data_dir=tmp_path)
        _write_summary(out_dir, stem)
        journal.update_journal("my-game", FakeClient(), {}, session_stem=stem, data_dir=tmp_path)
    return journal.journal_path("my-game", data_dir=tmp_path)


def test_fold_records_hash_of_written_file(tmp_path, out_dir):
    jpath = _folded_game(tmp_path, out_dir, ("s1",))
    assert _campaign_row(tmp_path)["journal_sha256"] == hashlib.sha256(jpath.read_bytes()).hexdigest()
    assert not journal._pending_path(jpath).exists()


def test_committed_but_unmoved_fold_is_finished_on_next_read(tmp_path, out_dir):
    """Crash between the commit and os.replace: the pending file's hash matches."""
    jpath = _folded_game(tmp_path, out_dir, ("s1",))
    pending = journal._pending_path(jpath)
    pending.write_text("---\ntype: campaign-journal\n---\n\nNewer body\n", encoding="utf-8")
    with db.transaction(tmp_path) as conn:
        conn.execute("UPDATE campaigns SET journal_sha256 = ?",
                     (hashlib.sha256(pending.read_bytes()).hexdigest(),))

    journal.sync_journal("my-game", data_dir=tmp_path)

    assert "Newer body" in jpath.read_text(encoding="utf-8")
    assert not pending.exists()


def test_uncommitted_pending_file_is_discarded(tmp_path, out_dir):
    """Crash before the commit: the pending file's hash doesn't match."""
    jpath = _folded_game(tmp_path, out_dir, ("s1",))
    before = jpath.read_text(encoding="utf-8")
    pending = journal._pending_path(jpath)
    pending.write_text("never committed", encoding="utf-8")

    journal.sync_journal("my-game", data_dir=tmp_path)

    assert jpath.read_text(encoding="utf-8") == before
    assert not pending.exists()
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s1"]


def test_deleted_journal_resets_entries(tmp_path, out_dir):
    jpath = _folded_game(tmp_path, out_dir)
    jpath.unlink()
    assert journal.unjournalled_sessions("my-game", data_dir=tmp_path) == ["s1", "s2"]
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == []
    row = _campaign_row(tmp_path)
    assert row["journal_sha256"] is None and row["journal_stale_since"] is None


def test_edited_journal_keeps_entries_and_adopts_hash(tmp_path, out_dir):
    jpath = _folded_game(tmp_path, out_dir)
    jpath.write_text(jpath.read_text(encoding="utf-8") + "\nMy own note.\n", encoding="utf-8")
    journal.sync_journal("my-game", data_dir=tmp_path)
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s1", "s2"]
    assert _campaign_row(tmp_path)["journal_sha256"] == hashlib.sha256(jpath.read_bytes()).hexdigest()


def test_fold_aborts_if_journal_changed_during_llm_call(tmp_path, out_dir):
    jpath = _folded_game(tmp_path, out_dir, ("s1",))
    move_transcript_to_campaign("s2", "my-game", data_dir=tmp_path)
    _write_summary(out_dir, "s2")

    class RacingClient(FakeClient):
        def complete(self, system, user):
            with db.transaction(tmp_path) as conn:  # another fold committed meanwhile
                conn.execute("UPDATE campaigns SET journal_sha256 = ?", ("f" * 64,))
            return super().complete(system, user)

    with pytest.raises(RuntimeError, match="changed while"):
        journal.update_journal("my-game", RacingClient(), {}, session_stem="s2", data_dir=tmp_path)
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s1"]
    assert not journal._pending_path(jpath).exists()


def test_export_adds_journaled_sessions(tmp_path, out_dir):
    _folded_game(tmp_path, out_dir)
    meta, body = journal.parse_journal(journal.export_journal("my-game", data_dir=tmp_path))
    assert meta["journaled_sessions"] == ["s1", "s2"]
    assert "It happened." in body


# ---------------------------------------------------------------------------
# Stale journal: moves, removals, re-transcribes; reorders don't count
# ---------------------------------------------------------------------------

def test_reorder_keeps_entries_and_not_stale(tmp_path, out_dir):
    from wisper_transcribe.campaign_manager import reorder_campaign_transcript, set_campaign_transcript_order

    _folded_game(tmp_path, out_dir)
    reorder_campaign_transcript("my-game", "s2", "up", data_dir=tmp_path)
    set_campaign_transcript_order("my-game", ["s1", "s2"], data_dir=tmp_path)
    assert set(journal.journaled_stems("my-game", data_dir=tmp_path)) == {"s1", "s2"}
    assert _campaign_row(tmp_path)["journal_stale_since"] is None


def test_move_to_other_campaign_drops_entry_and_marks_stale(tmp_path, out_dir):
    _folded_game(tmp_path, out_dir)
    create_campaign("Other", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "other", data_dir=tmp_path)
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s2"]
    assert journal.journal_stale_since("my-game", data_dir=tmp_path) is not None
    assert journal.journal_stale_since("other", data_dir=tmp_path) is None


def test_unassign_marks_stale(tmp_path, out_dir):
    from wisper_transcribe.campaign_manager import remove_transcript_from_campaign

    _folded_game(tmp_path, out_dir)
    remove_transcript_from_campaign("s1", data_dir=tmp_path)
    assert journal.journal_stale_since("my-game", data_dir=tmp_path) is not None


def test_transcript_delete_marks_stale(tmp_path, out_dir, monkeypatch):
    from wisper_transcribe import transcript_store

    _folded_game(tmp_path, out_dir)
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript_store.delete_transcript("s1", output_dir=out_dir)
    assert journal.journaled_stems("my-game") == ["s2"]
    assert journal.journal_stale_since("my-game") is not None


def test_retranscribe_of_journaled_session_marks_stale(tmp_path, out_dir, monkeypatch):
    from wisper_transcribe import transcript_store

    _folded_game(tmp_path, out_dir)
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript_store.register("s1", origin="job")
    assert journal.journaled_stems("my-game") == ["s1", "s2"]  # never un-folded
    assert journal.journal_stale_since("my-game") is not None


def test_register_from_reconcile_does_not_mark_stale(tmp_path, out_dir, monkeypatch):
    from wisper_transcribe import transcript_store

    _folded_game(tmp_path, out_dir)
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    transcript_store.register("s1", origin="reconcile")
    assert journal.journal_stale_since("my-game") is None


def test_moving_a_session_to_another_campaign_drops_its_journal_entry(tmp_path, out_dir):
    _folded_game(tmp_path, out_dir, ("s1",))
    create_campaign("Other", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "other", data_dir=tmp_path)
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == []
    assert journal.journal_stale_since("my-game", data_dir=tmp_path) is not None


# ---------------------------------------------------------------------------
# refold_campaign — rebuild from existing summaries
# ---------------------------------------------------------------------------

def test_refold_uses_existing_summaries_one_call_each(tmp_path, out_dir):
    jpath = _folded_game(tmp_path, out_dir, ("s1", "s2"))
    _write_summary(out_dir, "s1", "Edited by hand.")
    create_campaign("Other", data_dir=tmp_path)
    move_transcript_to_campaign("s2", "other", data_dir=tmp_path)  # marks stale
    move_transcript_to_campaign("s3", "my-game", data_dir=tmp_path)
    _write_transcript(out_dir, "s3")  # no summary yet
    client = FakeClient(body="## Story So Far\n\nRefolded.")

    result = journal.refold_campaign("my-game", client, {}, data_dir=tmp_path)

    assert len(client.json_calls) == 1          # only s3 was summarized
    assert len(client.calls) == 2               # s1 and s3 folded
    assert "Edited by hand." in client.calls[0][1]
    assert (out_dir / "s1.summary.md").read_text(encoding="utf-8") == "Edited by hand."
    assert result.resummarized == ["s3"]
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s1", "s3"]
    assert _campaign_row(tmp_path)["journal_stale_since"] is None
    assert "Refolded." in jpath.read_text(encoding="utf-8")
    # The first fold starts from an empty journal, not the stale text.
    assert "It happened." not in client.calls[0][1]


def test_rebuild_clears_stale(tmp_path, out_dir):
    _folded_game(tmp_path, out_dir)
    for stem in ("s1", "s2"):
        _write_transcript(out_dir, stem)
    from wisper_transcribe.campaign_manager import remove_transcript_from_campaign
    remove_transcript_from_campaign("s2", data_dir=tmp_path)
    journal.rebuild_campaign("my-game", FakeClient(), {}, data_dir=tmp_path)
    assert _campaign_row(tmp_path)["journal_stale_since"] is None
    assert journal.journaled_stems("my-game", data_dir=tmp_path) == ["s1"]


# ---------------------------------------------------------------------------
# Legacy journal adoption
# ---------------------------------------------------------------------------

LEGACY_TEXT = "---\ntype: campaign-journal\ncampaign: my-game\n---\n\n## Story So Far\n\nOld text.\n"


def _legacy_campaign(tmp_path, folded=("s1", "s2"), text=LEGACY_TEXT):
    """A campaign whose journal still sits at the data-dir location, with entries."""
    import hashlib

    from wisper_transcribe import db

    create_campaign("My Game", data_dir=tmp_path)
    for stem in folded:
        move_transcript_to_campaign(stem, "my-game", data_dir=tmp_path)
    legacy = journal.legacy_journal_path("my-game", tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with db.transaction(tmp_path) as conn:
        conn.execute("INSERT INTO journal_entries SELECT id, campaign_id, 'now' FROM transcripts "
                     "WHERE campaign_id IS NOT NULL")
        conn.execute("UPDATE campaigns SET journal_sha256 = ?", (sha,))
    return legacy, sha


def _journal_state(tmp_path):
    from wisper_transcribe import db

    with db.connection(tmp_path) as conn:
        row = conn.execute("SELECT journal_sha256 FROM campaigns").fetchone()
        return (journal.journaled_stems("my-game", tmp_path), row[0])


def test_a_legacy_journal_moves_into_the_campaign_folder_on_first_read(tmp_path, out_dir):
    from wisper_transcribe import file_registry

    legacy, sha = _legacy_campaign(tmp_path)
    journal.unjournalled_sessions("my-game", tmp_path)

    target = out_dir / "My Game" / "My Game Journal.md"
    assert target.read_text(encoding="utf-8") == LEGACY_TEXT
    assert _journal_state(tmp_path) == (["s1", "s2"], sha)
    owner = file_registry.Owner.for_campaign_slug("my-game", data_dir=tmp_path)
    row = file_registry.file_for(owner, "journal", data_dir=tmp_path)
    assert row.rel_path == "My Game/My Game Journal.md" and row.root == "output"
    assert campaign_folders.is_claimed(owner.id, data_dir=tmp_path)


def test_adoption_sets_the_old_file_aside(tmp_path, out_dir):
    legacy, _ = _legacy_campaign(tmp_path)
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "adopted"
    assert not legacy.exists()
    adopted = legacy.with_name("journal.md.v11-adopted")
    assert adopted.read_text(encoding="utf-8") == LEGACY_TEXT
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "none"


def test_adoption_copies_when_the_first_replace_fails_across_volumes(tmp_path, out_dir, monkeypatch):
    import errno
    import os

    legacy, sha = _legacy_campaign(tmp_path)
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        return real(src, dst)

    monkeypatch.setattr(journal.os, "replace", flaky)
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "adopted"
    assert (out_dir / "My Game" / "My Game Journal.md").read_text(encoding="utf-8") == LEGACY_TEXT
    assert not any(p.name.startswith(".wisper-tmp-") for p in (out_dir / "My Game").iterdir())


def test_a_failed_copy_keeps_the_legacy_file_and_every_entry(tmp_path, out_dir, monkeypatch):
    legacy, sha = _legacy_campaign(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(journal.shutil, "copyfileobj", boom)
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "failed"
    journal.unjournalled_sessions("my-game", tmp_path)  # a read must not reset anything
    assert legacy.exists() and _journal_state(tmp_path) == (["s1", "s2"], sha)
    assert not any(p.name.startswith(".wisper-tmp-") for p in (out_dir / "My Game").iterdir())


def test_a_legacy_pending_file_whose_hash_matches_is_the_one_adopted(tmp_path, out_dir):
    import hashlib

    legacy, _ = _legacy_campaign(tmp_path)
    newer = LEGACY_TEXT + "\nNewer fold.\n"
    pending = legacy.with_name("journal.md.wisper-pending")
    pending.write_text(newer, encoding="utf-8")
    with db.transaction(tmp_path) as conn:
        conn.execute("UPDATE campaigns SET journal_sha256 = ?",
                     (hashlib.sha256(newer.encode()).hexdigest(),))
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "adopted"
    assert (out_dir / "My Game" / "My Game Journal.md").read_text(encoding="utf-8") == newer
    assert not legacy.exists() and not pending.exists()


def test_a_legacy_pending_file_whose_hash_differs_is_deleted(tmp_path, out_dir):
    legacy, _ = _legacy_campaign(tmp_path)
    pending = legacy.with_name("journal.md.wisper-pending")
    pending.write_text("never committed", encoding="utf-8")
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "adopted"
    assert not pending.exists()
    assert (out_dir / "My Game" / "My Game Journal.md").read_text(encoding="utf-8") == LEGACY_TEXT


def test_both_journals_present_and_different_changes_nothing(tmp_path, out_dir):
    legacy, sha = _legacy_campaign(tmp_path)
    target = out_dir / "My Game" / "My Game Journal.md"
    target.parent.mkdir()
    target.write_text("---\ntype: campaign-journal\n---\n\nSomething else.\n", encoding="utf-8")
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "kept"
    assert legacy.exists() and target.read_text(encoding="utf-8").endswith("Something else.\n")
    assert _journal_state(tmp_path) == (["s1", "s2"], sha)


def test_update_journal_with_a_kept_legacy_journal_fails_and_changes_nothing(tmp_path, out_dir):
    legacy, sha = _legacy_campaign(tmp_path)
    target = out_dir / "My Game" / "My Game Journal.md"
    target.parent.mkdir()
    target.write_text("---\ntype: campaign-journal\n---\n\nSomething else.\n", encoding="utf-8")
    with pytest.raises(journal.JournalLocationError, match="journal_legacy_pending"):
        journal.update_journal("my-game", FakeClient(), {}, session_stem="s1", data_dir=tmp_path)
    assert legacy.exists() and _journal_state(tmp_path) == (["s1", "s2"], sha)


def test_a_target_already_holding_the_legacy_text_finishes_the_adoption(tmp_path, out_dir):
    """A crash after the copy and before the old file was set aside."""
    legacy, _ = _legacy_campaign(tmp_path)
    target = out_dir / "My Game" / "My Game Journal.md"
    target.parent.mkdir()
    target.write_text(LEGACY_TEXT, encoding="utf-8")
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "adopted"
    assert not legacy.exists() and legacy.with_name("journal.md.v11-adopted").exists()


def test_a_foreign_folder_blocks_adoption_and_nothing_is_written_into_it(tmp_path, out_dir):
    legacy, _ = _legacy_campaign(tmp_path)
    folder = out_dir / "My Game"
    folder.mkdir()
    (folder / "notes.md").write_text("mine", encoding="utf-8")
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "folder_taken"
    assert sorted(p.name for p in folder.iterdir()) == ["notes.md"] and legacy.exists()
    with pytest.raises(journal.JournalLocationError, match="journal_folder_taken"):
        journal.update_journal("my-game", FakeClient(), {}, session_stem="s1", data_dir=tmp_path)


def test_adoption_with_the_output_root_absent_is_unavailable(tmp_path, out_dir):
    import shutil

    legacy, sha = _legacy_campaign(tmp_path)
    shutil.rmtree(out_dir)
    assert journal.adopt_legacy_journal("my-game", tmp_path) == "unavailable"
    assert journal.adopt_legacy_journals(tmp_path) == {"my-game": "unavailable"}
    with pytest.raises(journal.JournalLocationError, match="journal_output_unavailable"):
        journal.update_journal("my-game", FakeClient(), {}, session_stem="s1", data_dir=tmp_path)
    assert legacy.exists() and _journal_state(tmp_path) == (["s1", "s2"], sha)


def test_adopt_legacy_journals_survives_one_campaign_raising(tmp_path, out_dir, monkeypatch):
    _legacy_campaign(tmp_path)
    create_campaign("Other", data_dir=tmp_path)
    real = journal.adopt_legacy_journal

    def picky(slug, *a, **k):
        if slug == "other":
            raise RuntimeError("boom")
        return real(slug, *a, **k)

    monkeypatch.setattr(journal, "adopt_legacy_journal", picky)
    assert journal.adopt_legacy_journals(tmp_path) == {"my-game": "adopted", "other": "failed"}


def test_reset_journal_removes_the_legacy_files_so_a_rebuild_never_re_adopts(tmp_path, out_dir):
    legacy, _ = _legacy_campaign(tmp_path)
    legacy.with_name("journal.md.wisper-pending").write_text("p", encoding="utf-8")
    legacy.with_name("journal.md.v11-adopted").write_text("a", encoding="utf-8")
    journal.reset_journal("my-game", tmp_path)
    assert not any(p.exists() for p in legacy.parent.iterdir())
    assert _journal_state(tmp_path) == ([], None)


def test_reset_journal_with_no_journal_yet_does_not_raise(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    journal.reset_journal("my-game", tmp_path)
    with pytest.raises(KeyError):
        journal.reset_journal("nope", tmp_path)
    with pytest.raises(ValueError):
        journal.reset_journal("../x", tmp_path)


def test_export_journal_returns_the_adopted_text_on_the_first_call(tmp_path, out_dir):
    _legacy_campaign(tmp_path)
    text = journal.export_journal("my-game", tmp_path)
    assert "Old text." in text and "journaled_sessions" in text
    assert journal.journaled_stems("my-game", tmp_path) == ["s1", "s2"]


def test_an_unclaimed_folders_note_survives_a_read_a_rebuild_and_a_campaign_delete(tmp_path, out_dir):
    from wisper_transcribe.campaign_manager import delete_campaign

    create_campaign("My Game", data_dir=tmp_path)
    move_transcript_to_campaign("s1", "my-game", data_dir=tmp_path)
    note = out_dir / "My Game" / "My Game Journal.md"
    note.parent.mkdir()
    note.write_text("my own notes", encoding="utf-8")

    assert journal.journal_path("my-game", tmp_path) is None
    journal.sync_journal("my-game", tmp_path)
    journal.unjournalled_sessions("my-game", tmp_path)
    journal.reset_journal("my-game", tmp_path)
    delete_campaign("my-game", delete_transcripts=True, data_dir=tmp_path)
    assert note.read_text(encoding="utf-8") == "my own notes"


def test_sync_keeps_the_entries_while_the_folder_or_output_root_is_unavailable(tmp_path, out_dir):
    import shutil

    _legacy_campaign(tmp_path, folded=("s1",))
    journal.adopt_legacy_journal("my-game", tmp_path)
    _, sha = _journal_state(tmp_path)
    folder = out_dir / "My Game"
    shutil.rmtree(folder)
    journal.sync_journal("my-game", tmp_path)
    assert _journal_state(tmp_path) == (["s1"], sha)
    shutil.rmtree(out_dir)
    journal.sync_journal("my-game", tmp_path)
    assert _journal_state(tmp_path) == (["s1"], sha)


def test_sync_resets_when_the_folder_is_there_and_only_the_journal_is_gone(tmp_path, out_dir):
    _legacy_campaign(tmp_path, folded=("s1",))
    journal.adopt_legacy_journal("my-game", tmp_path)
    (out_dir / "My Game" / "My Game Journal.md").unlink()
    journal.sync_journal("my-game", tmp_path)
    assert _journal_state(tmp_path) == ([], None)
