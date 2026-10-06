"""Tests for campaign_digest.py — combined-summary and recap paths, session
discovery, digest rows, and staleness.

No real LLM or network: digests are recorded directly, and session
``.summary.md`` sidecars are written to a tmp output root (``WISPER_OUTPUT_DIR``).
"""
from pathlib import Path

import pytest

from wisper_transcribe import campaign_digest as cd
from wisper_transcribe import campaign_folders, db, file_registry
from wisper_transcribe.campaign_manager import create_campaign, load_campaigns
from wisper_transcribe.journal import parse_journal

from . import _seed


class FakeClient:
    """Minimal stand-in for LLMClient: records prompts, returns a canned body."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, body: str = "The party met in a tavern.", error: Exception | None = None):
        self._body = body
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self._body


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    d = tmp_path / "output"
    d.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(d))
    return d


def _summary(out_dir: Path, stem: str, text: str = "A session happened.") -> None:
    (out_dir / f"{stem}.summary.md").write_text(text, encoding="utf-8")


def _game(tmp_path, out_dir, stems=("s1", "s2", "s3"), summarized=("s1", "s2", "s3")):
    create_campaign("My Game", data_dir=tmp_path)
    for stem in stems:
        _seed.seed_transcript(stem, campaign="my-game", write_md=True, data_dir=tmp_path)
    for stem in summarized:
        _summary(out_dir, stem)
    cid = load_campaigns(tmp_path)["my-game"].id
    campaign_folders.ensure_folder(cid, data_dir=tmp_path, output_dir=out_dir)
    return cid


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_combined_summary_path_in_a_claimed_folder(tmp_path, out_dir):
    _seed.seed_campaign("My Game", "my-game", data_dir=tmp_path)  # unclaimed
    assert cd.combined_summary_path("my-game", data_dir=tmp_path) is None
    cid = load_campaigns(tmp_path)["my-game"].id
    campaign_folders.ensure_folder(cid, data_dir=tmp_path, output_dir=out_dir)
    assert (cd.combined_summary_path("my-game", data_dir=tmp_path)
            == out_dir / "My Game" / "My Game Combined Summary.md")


def test_recap_path_carries_the_stem(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    assert (cd.recap_path("my-game", "s3", data_dir=tmp_path)
            == out_dir / "My Game" / "My Game Recap \u2014 s3.md")


def test_paths_for_unknown_or_invalid_slug_are_none(tmp_path, out_dir):
    assert cd.combined_summary_path("nope", data_dir=tmp_path) is None
    assert cd.combined_summary_path("../escape", data_dir=tmp_path) is None
    assert cd.recap_path("nope", "s1", data_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def test_summarized_sessions_in_campaign_order(tmp_path, out_dir):
    _game(tmp_path, out_dir, summarized=("s1", "s3"))
    assert cd.summarized_sessions("my-game", data_dir=tmp_path) == ["s1", "s3"]


def test_summarized_sessions_empty_without_summaries(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    _seed.seed_transcript("s1", campaign="my-game", write_md=True, data_dir=tmp_path)
    assert cd.summarized_sessions("my-game", data_dir=tmp_path) == []


def test_summarized_sessions_invalid_slug(tmp_path):
    assert cd.summarized_sessions("../x", data_dir=tmp_path) == []


def test_recap_sessions_takes_the_last_n(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    assert cd.recap_sessions("my-game", 1, data_dir=tmp_path) == ["s3"]
    assert cd.recap_sessions("my-game", 2, data_dir=tmp_path) == ["s2", "s3"]
    assert cd.recap_sessions("my-game", 9, data_dir=tmp_path) == ["s1", "s2", "s3"]


@pytest.mark.parametrize("n,expected", [(0, 1), (-5, 1), (4, 3), (99, 3)])
def test_clamp_recap_sessions(n, expected):
    assert cd.clamp_recap_sessions(n) == expected


def test_clamp_recap_sessions_defaults_on_garbage():
    assert cd.clamp_recap_sessions("nonsense") == cd.RECAP_DEFAULT_SESSIONS


# ---------------------------------------------------------------------------
# Digest rows
# ---------------------------------------------------------------------------

def _record(slug, kind, path, stems, tmp_path, out_dir):
    from wisper_transcribe.config import get_output_root
    label = (None if kind == cd.COMBINED_SUMMARY
             else path.name.split("\u2014 ")[-1][:-3])
    owner = file_registry.Owner.for_campaign_slug(slug, data_dir=tmp_path)
    file_registry.add(path, kind=kind, owner=owner, label=label,
                      output_dir=get_output_root(), data_dir=tmp_path)
    row = file_registry.file_for(owner, kind, label,
                                 data_dir=tmp_path, output_dir=get_output_root())
    assert row is not None
    return cd.record_digest(slug, kind, row.id, stems, "fake", "m", data_dir=tmp_path)


def test_record_and_list_recaps_newest_first(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    p1 = cd.recap_path("my-game", "s1", data_dir=tmp_path)
    p1.write_text("older", encoding="utf-8")
    _record("my-game", cd.RECAP, p1, ["s1"], tmp_path, out_dir)
    p3 = cd.recap_path("my-game", "s3", data_dir=tmp_path)
    p3.write_text("newer", encoding="utf-8")
    _record("my-game", cd.RECAP, p3, ["s3"], tmp_path, out_dir)

    recaps = cd.list_recaps("my-game", data_dir=tmp_path)
    assert [r.path.name for r in recaps] == [
        "My Game Recap \u2014 s3.md", "My Game Recap \u2014 s1.md"]
    assert recaps[0].sessions == ["s3"]
    assert recaps[0].provider == "fake"


def test_recording_the_same_file_again_replaces_the_digest(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    p3 = cd.recap_path("my-game", "s3", data_dir=tmp_path)
    p3.write_text("one", encoding="utf-8")
    _record("my-game", cd.RECAP, p3, ["s3"], tmp_path, out_dir)
    p3.write_text("two", encoding="utf-8")
    _record("my-game", cd.RECAP, p3, ["s3"], tmp_path, out_dir)

    with db.connection(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM campaign_digests").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM campaign_digest_sessions").fetchone()[0] == 1
    assert len(cd.list_recaps("my-game", data_dir=tmp_path)) == 1


def test_combined_summary_digest_roundtrip(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    p = cd.combined_summary_path("my-game", data_dir=tmp_path)
    p.write_text("all sessions", encoding="utf-8")
    _record("my-game", cd.COMBINED_SUMMARY, p, ["s1", "s2", "s3"], tmp_path, out_dir)

    digest = cd.combined_summary_digest("my-game", data_dir=tmp_path)
    assert digest is not None
    assert digest.sessions == ["s1", "s2", "s3"]
    assert digest.kind == cd.COMBINED_SUMMARY


def test_record_digest_unknown_campaign_raises(tmp_path, out_dir):
    with pytest.raises(KeyError):
        cd.record_digest("ghost", cd.RECAP, 1, ["s1"], data_dir=tmp_path)


def test_record_digest_invalid_kind_raises(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    with pytest.raises(ValueError):
        cd.record_digest("my-game", "overview", 1, ["s1"], data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def test_summary_stale_when_a_session_is_added(tmp_path, out_dir):
    _game(tmp_path, out_dir, summarized=("s1", "s2"))
    p = cd.combined_summary_path("my-game", data_dir=tmp_path)
    p.write_text("x", encoding="utf-8")
    _record("my-game", cd.COMBINED_SUMMARY, p, ["s1", "s2"], tmp_path, out_dir)
    assert cd.combined_summary_stale_since("my-game", data_dir=tmp_path) is None

    _summary(out_dir, "s3")  # a new session is summarized
    assert cd.combined_summary_stale_since("my-game", data_dir=tmp_path) is not None


def test_summary_stale_when_a_session_is_removed(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    p = cd.combined_summary_path("my-game", data_dir=tmp_path)
    p.write_text("x", encoding="utf-8")
    _record("my-game", cd.COMBINED_SUMMARY, p, ["s1", "s2", "s3"], tmp_path, out_dir)
    _seed.remove_from_campaign("s3", data_dir=tmp_path)
    assert cd.combined_summary_stale_since("my-game", data_dir=tmp_path) is not None


def test_summary_stale_when_a_covered_summary_changed(tmp_path, out_dir):
    import os
    import time

    _game(tmp_path, out_dir)
    p = cd.combined_summary_path("my-game", data_dir=tmp_path)
    p.write_text("x", encoding="utf-8")
    _record("my-game", cd.COMBINED_SUMMARY, p, ["s1", "s2", "s3"], tmp_path, out_dir)

    summary = out_dir / "s2.summary.md"
    summary.write_text("rewritten", encoding="utf-8")
    future = time.time() + 10
    os.utime(summary, (future, future))
    assert cd.combined_summary_stale_since("my-game", data_dir=tmp_path) is not None


def test_summary_not_stale_without_a_digest(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    assert cd.combined_summary_stale_since("my-game", data_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Generation: combined summary
# ---------------------------------------------------------------------------

def test_generate_combined_summary_writes_registers_and_records(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    client = FakeClient(body="## The Story So Far\n\nEverything happened.")

    result = cd.generate_combined_summary("my-game", client, {}, data_dir=tmp_path)

    assert result is not None and result.kind == cd.COMBINED_SUMMARY
    assert result.path.exists()
    assert result.sessions == ["s1", "s2", "s3"]
    meta, body = parse_journal(result.path.read_text(encoding="utf-8"))
    assert meta["type"] == "campaign-combined-summary"
    assert meta["sessions"] == ["s1", "s2", "s3"]
    assert "Everything happened." in body
    # All three summaries reached the prompt.
    assert "s1" in client.calls[0][1] and "s3" in client.calls[0][1]
    # Registered as a campaign-owned file and recorded as a digest.
    owner = cd.file_registry.Owner.for_campaign_slug("my-game", data_dir=tmp_path)
    row = cd.file_registry.file_for(owner, cd.COMBINED_SUMMARY, data_dir=tmp_path)
    assert row is not None and row.rel_path == "My Game/My Game Combined Summary.md"
    digest = cd.combined_summary_digest("my-game", data_dir=tmp_path)
    assert digest is not None and digest.sessions == ["s1", "s2", "s3"]


def test_generate_combined_summary_is_not_stale_after_writing(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    cd.generate_combined_summary("my-game", FakeClient(), {}, data_dir=tmp_path)
    assert cd.combined_summary_stale_since("my-game", data_dir=tmp_path) is None


def test_generate_combined_summary_with_no_summaries_returns_none(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    _seed.seed_transcript("s1", campaign="my-game", write_md=True, data_dir=tmp_path)
    client = FakeClient()
    assert cd.generate_combined_summary("my-game", client, {}, data_dir=tmp_path) is None
    assert client.calls == []


def test_generate_combined_summary_strips_code_fence(tmp_path, out_dir):
    _game(tmp_path, out_dir, summarized=("s1",))
    client = FakeClient(body="```markdown\n## The Story So Far\n\nFenced.\n```")
    result = cd.generate_combined_summary("my-game", client, {}, data_dir=tmp_path)
    _, body = parse_journal(result.path.read_text(encoding="utf-8"))
    assert body.startswith("## The Story So Far")
    assert "```" not in body


# ---------------------------------------------------------------------------
# Generation: recap
# ---------------------------------------------------------------------------

def test_generate_recap_default_is_the_newest_session(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    result = cd.generate_recap("my-game", FakeClient(body="Previously..."), {},
                               data_dir=tmp_path)

    assert result.path.name == "My Game Recap \u2014 s3.md"
    assert result.sessions == ["s3"]
    assert result.path.exists()


def test_generate_recap_with_two_sessions_covers_the_last_two(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    result = cd.generate_recap("my-game", FakeClient(), {}, sessions=2, data_dir=tmp_path)
    assert result.sessions == ["s2", "s3"]


def test_generate_recap_clamps_sessions_to_the_range(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    result = cd.generate_recap("my-game", FakeClient(), {}, sessions=99, data_dir=tmp_path)
    assert result.sessions == ["s1", "s2", "s3"]


def test_rerun_for_the_same_newest_session_replaces_only_that_file(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    cd.generate_recap("my-game", FakeClient(body="First."), {}, data_dir=tmp_path)
    # A second recap for the same newest session replaces the one file and row.
    result = cd.generate_recap("my-game", FakeClient(body="Second."), {}, data_dir=tmp_path)

    assert "Second." in result.path.read_text(encoding="utf-8")
    assert result.replaced is True
    recaps = cd.list_recaps("my-game", data_dir=tmp_path)
    assert len(recaps) == 1
    assert "Second." in recaps[0].path.read_text(encoding="utf-8")


def test_a_different_newest_session_adds_a_second_recap(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    cd.generate_recap("my-game", FakeClient(), {}, data_dir=tmp_path)  # s3
    _seed.seed_transcript("s4", campaign="my-game", write_md=True, data_dir=tmp_path)
    _summary(out_dir, "s4")
    cd.generate_recap("my-game", FakeClient(), {}, data_dir=tmp_path)  # s4

    recaps = cd.list_recaps("my-game", data_dir=tmp_path)
    assert [r.path.name for r in recaps] == [
        "My Game Recap \u2014 s4.md", "My Game Recap \u2014 s3.md"]


def test_recap_prompt_states_the_constraints(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    client = FakeClient()
    cd.generate_recap("my-game", client, {}, data_dir=tmp_path)
    system = client.calls[0][0]
    assert "spoiler" in system.lower()
    assert "200 to 400 words" in system
    assert "player" in system.lower()


def test_generate_recap_with_no_summaries_returns_none(tmp_path, out_dir):
    create_campaign("My Game", data_dir=tmp_path)
    _seed.seed_transcript("s1", campaign="my-game", write_md=True, data_dir=tmp_path)
    client = FakeClient()
    assert cd.generate_recap("my-game", client, {}, data_dir=tmp_path) is None
    assert client.calls == []


def test_generation_propagates_the_llm_failure(tmp_path, out_dir):
    from wisper_transcribe.llm.errors import LLMResponseError

    _game(tmp_path, out_dir)
    with pytest.raises(LLMResponseError):
        cd.generate_combined_summary(
            "my-game", FakeClient(error=LLMResponseError("boom")), {}, data_dir=tmp_path)


# ---------------------------------------------------------------------------
# CLI: wisper campaigns summarize / recap
# ---------------------------------------------------------------------------

def test_cli_campaigns_summarize_writes_the_file(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _game(tmp_path, out_dir)
    monkeypatch.setattr(cli, "_get_llm_client", lambda *a, **k: FakeClient())

    result = CliRunner().invoke(cli.main, ["campaigns", "summarize", "my-game"])
    assert result.exit_code == 0, result.output
    assert (out_dir / "My Game" / "My Game Combined Summary.md").exists()
    assert cd.combined_summary_digest("my-game", data_dir=tmp_path) is not None


def test_cli_campaigns_summarize_refuses_without_summaries(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    create_campaign("My Game", data_dir=tmp_path)
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM client should not be created")

    monkeypatch.setattr(cli, "_get_llm_client", _boom)
    result = CliRunner().invoke(cli.main, ["campaigns", "summarize", "my-game"])
    assert result.exit_code != 0
    assert "no summarized sessions" in result.output
    assert called["n"] == 0


def test_cli_campaigns_recap_writes_the_newest(tmp_path, out_dir, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    _game(tmp_path, out_dir)
    monkeypatch.setattr(cli, "_get_llm_client", lambda *a, **k: FakeClient())

    result = CliRunner().invoke(cli.main, ["campaigns", "recap", "my-game", "--sessions", "2"])
    assert result.exit_code == 0, result.output
    assert (out_dir / "My Game" / "My Game Recap \u2014 s3.md").exists()


def test_cli_campaigns_recap_unknown_campaign(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from wisper_transcribe import cli

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli.main, ["campaigns", "recap", "ghost"])
    assert result.exit_code != 0
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# JobQueue worker: _run_digest_job
# ---------------------------------------------------------------------------

class _Devnull:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def _run_digest(tmp_path, out_dir, monkeypatch, job, client):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "_StderrCapture", lambda job: _Devnull())
    import wisper_transcribe.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_client", lambda *a, **k: client)
    q = jobs_mod.JobQueue()
    try:
        q._run_digest_job(job)
    except Exception as exc:  # the worker would map this to a generic error
        job._raised = exc
    return q, jobs_mod


def test_run_digest_job_combined_summary_completes(tmp_path, out_dir, monkeypatch):
    _game(tmp_path, out_dir)
    from wisper_transcribe.web import jobs as jobs_mod

    job = jobs_mod.Job(id="j1", status=jobs_mod.PENDING, created_at=__import__("datetime").datetime.now(),
                       input_path="", kwargs={"slug": "my-game"},
                       job_type=jobs_mod.JOB_CAMPAIGN_SUMMARY)
    _, jobs_mod = _run_digest(tmp_path, out_dir, monkeypatch, job, FakeClient())

    assert job.status == jobs_mod.COMPLETED
    assert job.output_path and job.output_path.endswith("My Game Combined Summary.md")
    assert cd.combined_summary_digest("my-game", data_dir=tmp_path) is not None


def test_run_digest_job_recap_replaces_for_same_newest(tmp_path, out_dir, monkeypatch):
    _game(tmp_path, out_dir)
    from wisper_transcribe.web import jobs as jobs_mod

    def make(sessions):
        return jobs_mod.Job(id=f"j{sessions}", status=jobs_mod.PENDING,
                            created_at=__import__("datetime").datetime.now(),
                            input_path="", kwargs={"slug": "my-game", "sessions": sessions},
                            job_type=jobs_mod.JOB_CAMPAIGN_RECAP)

    _run_digest(tmp_path, out_dir, monkeypatch, make(1), FakeClient(body="One."))
    job = make(1)
    _run_digest(tmp_path, out_dir, monkeypatch, job, FakeClient(body="Two."))

    assert job.status == jobs_mod.COMPLETED
    assert len(cd.list_recaps("my-game", data_dir=tmp_path)) == 1


def test_run_digest_job_no_summaries_is_a_clean_completion(tmp_path, out_dir, monkeypatch):
    create_campaign("My Game", data_dir=tmp_path)
    _seed.seed_transcript("s1", campaign="my-game", write_md=True, data_dir=tmp_path)
    from wisper_transcribe.web import jobs as jobs_mod

    job = jobs_mod.Job(id="j1", status=jobs_mod.PENDING, created_at=__import__("datetime").datetime.now(),
                       input_path="", kwargs={"slug": "my-game"},
                       job_type=jobs_mod.JOB_CAMPAIGN_SUMMARY)
    _, jobs_mod = _run_digest(tmp_path, out_dir, monkeypatch, job, FakeClient())

    assert job.status == jobs_mod.COMPLETED
    assert any("No summarized session" in line for line in job.log_lines)


def test_run_digest_job_llm_failure_soft_fails_generic(tmp_path, out_dir, monkeypatch):
    from wisper_transcribe.llm.errors import LLMUnavailableError

    _game(tmp_path, out_dir)
    from wisper_transcribe.web import jobs as jobs_mod

    job = jobs_mod.Job(id="j1", status=jobs_mod.PENDING, created_at=__import__("datetime").datetime.now(),
                       input_path="", kwargs={"slug": "my-game"},
                       job_type=jobs_mod.JOB_CAMPAIGN_SUMMARY)
    _run_digest(tmp_path, out_dir, monkeypatch, job,
                FakeClient(error=LLMUnavailableError("ollama unreachable")))

    assert job.status == jobs_mod.FAILED
    assert job.error is None  # the worker's generic message is applied by the queue, not here
    assert not (out_dir / "My Game" / "My Game Combined Summary.md").exists()


# ---------------------------------------------------------------------------
# Delete, rename, and the file registry
# ---------------------------------------------------------------------------

def test_campaign_delete_removes_both_outputs_and_their_rows(tmp_path, out_dir):
    from wisper_transcribe.campaign_manager import delete_campaign

    _game(tmp_path, out_dir)
    cd.generate_combined_summary("my-game", FakeClient(), {}, data_dir=tmp_path)
    recap = cd.generate_recap("my-game", FakeClient(), {}, data_dir=tmp_path)

    delete_campaign("my-game", delete_transcripts=True, data_dir=tmp_path)

    assert not recap.path.exists()
    assert not (out_dir / "My Game" / "My Game Combined Summary.md").exists()
    with db.connection(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM campaign_digests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM files WHERE campaign_id IS NOT NULL").fetchone()[0] == 0


def test_campaign_delete_keep_files_keeps_the_digests_on_disk(tmp_path, out_dir):
    """Keep the files leaves every document in the campaign folder, like the
    journal; only the rows go."""
    from wisper_transcribe.campaign_manager import delete_campaign

    _game(tmp_path, out_dir)
    cd.generate_combined_summary("my-game", FakeClient(), {}, data_dir=tmp_path)
    recap = cd.generate_recap("my-game", FakeClient(), {}, data_dir=tmp_path)
    delete_campaign("my-game", delete_transcripts=False, data_dir=tmp_path)

    assert (out_dir / "My Game" / "My Game Combined Summary.md").exists()
    assert recap.path.exists()
    with db.connection(tmp_path) as conn:
        assert conn.execute("SELECT count(*) FROM campaign_digests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM files WHERE campaign_id IS NOT NULL").fetchone()[0] == 0


def test_folder_rename_carries_the_digests(tmp_path, out_dir):
    _game(tmp_path, out_dir)
    cd.generate_combined_summary("my-game", FakeClient(), {}, data_dir=tmp_path)
    recap = cd.generate_recap("my-game", FakeClient(), {}, data_dir=tmp_path)

    outcome = campaign_folders.rename_campaign("my-game", "Renamed Game", data_dir=tmp_path)
    assert outcome.status == "renamed"

    moved = out_dir / "Renamed Game" / "Renamed Game Combined Summary.md"
    assert moved.exists()
    assert (out_dir / "Renamed Game"
            / "Renamed Game Recap \u2014 s3.md").exists()
    assert not recap.path.exists()
    owner = file_registry.Owner.for_campaign_slug("renamed-game", data_dir=tmp_path)
    row = file_registry.file_for(owner, cd.COMBINED_SUMMARY, data_dir=tmp_path)
    assert row is not None and row.rel_path == "Renamed Game/Renamed Game Combined Summary.md"


def test_sync_registers_an_unregistered_digest(tmp_path, out_dir):
    """A digest written before its register is picked up, not listed as a session."""
    from wisper_transcribe import file_registry

    _game(tmp_path, out_dir)
    path = out_dir / "My Game" / "My Game Combined Summary.md"
    path.write_text("## The Story So Far\n\nText.", encoding="utf-8")

    report = file_registry.sync(output_dir=out_dir, data_dir=tmp_path, scan_only=False)
    owner = file_registry.Owner.for_campaign_slug("my-game", data_dir=tmp_path)
    row = file_registry.file_for(owner, cd.COMBINED_SUMMARY, data_dir=tmp_path)
    assert row is not None
    assert path not in report.unclaimed


def test_sync_does_not_mistake_a_digest_for_a_session(tmp_path, out_dir):
    """An unregistered recap and combined summary never become transcripts."""
    from wisper_transcribe import file_registry, transcript_store

    _game(tmp_path, out_dir)
    (out_dir / "My Game" / "My Game Combined Summary.md").write_text("x", encoding="utf-8")
    (out_dir / "My Game" / "My Game Recap \u2014 s3.md").write_text("x", encoding="utf-8")
    file_registry.sync(output_dir=out_dir, data_dir=tmp_path, scan_only=False)
    found = transcript_store._transcript_files(
        transcript_store.transcript_dirs(data_dir=tmp_path, output_dir=out_dir),
        out_dir, False, set())
    names = {p.name for p in found.values()}
    assert "My Game Combined Summary.md" not in names
    assert "My Game Recap \u2014 s3.md" not in names



def test_digest_job_failure_maps_to_a_generic_error():
    """The queue maps an unhandled digest failure to a generic, path-free message."""
    from wisper_transcribe.web import jobs as jobs_mod

    job = jobs_mod.Job(id="j1", status=jobs_mod.RUNNING,
                       created_at=__import__("datetime").datetime.now(),
                       input_path="", kwargs={}, job_type=jobs_mod.JOB_CAMPAIGN_SUMMARY)
    _set = jobs_mod._set_job_error
    _set(job, RuntimeError("/secret/path/leaked"))
    assert job.error == "Summary generation failed — see server logs"
    job2 = jobs_mod.Job(id="j2", status=jobs_mod.RUNNING,
                        created_at=__import__("datetime").datetime.now(),
                        input_path="", kwargs={}, job_type=jobs_mod.JOB_CAMPAIGN_RECAP)
    _set(job2, RuntimeError("boom"))
    assert job2.error == "Recap generation failed — see server logs"


