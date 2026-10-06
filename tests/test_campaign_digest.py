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

from . import _seed


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
