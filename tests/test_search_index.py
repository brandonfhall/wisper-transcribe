"""search_index: block extraction, indexing, freshness, backfill, queries, snippets."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from wisper_transcribe import db, search_index as si, transcript_store as ts
from wisper_transcribe.campaign_manager import create_campaign, move_transcript_to_campaign
from wisper_transcribe.path_utils import get_output_dir

SRC = Path(__file__).parent.parent / "src" / "wisper_transcribe"

TRANSCRIPT = """---
title: Session 1
speakers:
- name: Alice
  role: DM
---

# Session 1

**Alice** *(00:05)*: The party fights Strahd at the castle gate.

**Bob** *(01:02:03)*: We flee from the fight and hide in the café.

**Alice** *(01:05:00)*: Nothing else happens tonight.

---
*Transcribed by wisper-transcribe v1.0*
"""

SUMMARY = """---
type: session-summary
model: vampire-llm
---

# The Gate

## Summary

The heroes met Strahd.

## Loot & Inventory

- Alice — a silver dagger
"""


@pytest.fixture
def out() -> Path:
    d = get_output_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(out: Path, stem: str, text: str = TRANSCRIPT, *, summary: str | None = None) -> Path:
    md = out / f"{stem}.md"
    md.write_text(text, encoding="utf-8")
    if summary is not None:
        (out / f"{stem}.summary.md").write_text(summary, encoding="utf-8")
    return md


def _add(out: Path, stem: str, text: str = TRANSCRIPT, *, summary: str | None = None) -> Path:
    """A transcript the app just wrote: registered and indexed."""
    md = _write(out, stem, text, summary=summary)
    ts.register(stem, origin="job")
    return md


def _count(table: str) -> int:
    with db.connection() as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _fts_hits(term: str) -> list[int]:
    with db.connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT rowid FROM search_fts WHERE search_fts MATCH ?", (f'"{term}"',))]


def _indexed(stem: str) -> set[str]:
    with db.connection() as conn:
        return {r[0] for r in conn.execute(
            "SELECT s.kind FROM search_index_state s JOIN transcripts t ON t.id = s.transcript_id "
            "WHERE t.stem = ?", (stem,))}


def _bump(path: Path, text: str) -> None:
    """Rewrite ``path`` and make sure its mtime changes."""
    before = path.stat().st_mtime_ns
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before + 5_000_000_000, before + 5_000_000_000))


def _stems(page: si.SearchPage) -> list[str]:
    return [g.stem for g in page.groups]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_transcript_blocks_skip_frontmatter_headings_and_footer():
    blocks = si.transcript_blocks(TRANSCRIPT)
    assert [(b.idx, b.speaker, b.start_s) for b in blocks] == [
        (0, "Alice", 5.0), (1, "Bob", 3723.0), (2, "Alice", 3900.0)]
    assert blocks[0].text == "The party fights Strahd at the castle gate."
    assert not any("DM" in b.text or "Transcribed" in b.text for b in blocks)


def test_blocks_numbered_like_the_edit_page():
    from wisper_transcribe.formatter import parse_transcript_blocks
    body = TRANSCRIPT.split("---", 2)[2]
    assert [b.idx for b in si.transcript_blocks(TRANSCRIPT)] == \
        [b["index"] for b in parse_transcript_blocks(body)]


def test_plain_transcript_falls_back_to_lines():
    text = "# T\n\nfirst line of speech\n\nsecond line\n\n---\n*Transcribed by wisper-transcribe v1*\n"
    blocks = si.transcript_blocks(text)
    assert [(b.idx, b.speaker, b.start_s, b.text) for b in blocks] == [
        (0, None, None, "first line of speech"), (1, None, None, "second line")]


def test_summary_sections_keep_indexes_and_drop_frontmatter():
    sections = si.summary_sections(SUMMARY)
    assert [s.idx for s in sections] == [0, 1, 2]
    assert sections[0].text == "# The Gate"
    assert sections[1].text.startswith("## Summary")
    assert "vampire-llm" not in " ".join(s.text for s in sections)


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def test_register_job_indexes_transcript_and_summary(out):
    _add(out, "s1", summary=SUMMARY)
    assert _indexed("s1") == {"transcript", "summary"}
    with db.connection() as conn:
        rows = conn.execute("SELECT kind, block_idx, speaker FROM search_blocks ORDER BY kind, block_idx").fetchall()
    assert [tuple(r) for r in rows] == [
        ("summary", 0, None), ("summary", 1, None), ("summary", 2, None),
        ("transcript", 0, "Alice"), ("transcript", 1, "Bob"), ("transcript", 2, "Alice")]
    assert _count("search_blocks") == len(_fts_hits("strahd")) + 4


def test_register_from_reconcile_leaves_indexing_to_backfill(out):
    _write(out, "s1")
    ts.reconcile(out)
    assert _indexed("s1") == set()
    assert si.progress() == (0, 1)
    assert si.run_backfill() == 1
    assert _indexed("s1") == {"transcript"}
    assert si.progress() == (1, 1)


def test_reindex_replaces_old_blocks(out):
    md = _add(out, "s1")
    assert len(_fts_hits("strahd")) == 1
    ts.save_transcript(md, TRANSCRIPT.replace("Strahd", "Ireena"))
    assert _fts_hits("strahd") == []
    assert len(_fts_hits("ireena")) == 1
    assert _count("search_blocks") == 3


def test_unregistered_or_outside_root_is_not_indexed(out, tmp_path):
    _write(out, "loose")
    assert si.reindex("loose") is False
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ts.save_transcript(elsewhere / "x.md", TRANSCRIPT)
    ts.save_summary(out / "custom-name.md", SUMMARY)  # summarize --output
    assert _count("search_blocks") == 0


def test_undecodable_file_is_indexed_once(out):
    md = out / "bad.md"
    md.write_bytes(b"**Alice** *(00:01)*: caf\xe9 \xff broken\n")
    ts.register("bad", origin="job")
    assert _indexed("bad") == {"transcript"}
    assert si.run_backfill() == 0


def test_delete_cascades_to_fts(out):
    _add(out, "s1", summary=SUMMARY)
    _add(out, "s2")
    ts.delete_transcript("s1")
    with db.connection() as conn:
        tid2 = conn.execute("SELECT id FROM transcripts WHERE stem = 's2'").fetchone()[0]
        owners = {r[0] for r in conn.execute("SELECT transcript_id FROM search_blocks")}
    assert owners == {tid2}
    assert len(_fts_hits("strahd")) == 1  # only s2's block
    assert _fts_hits("dagger") == []      # s1's summary is gone


def test_save_helpers_reindex(out):
    md = _add(out, "s1")
    ts.save_summary(out / "s1.summary.md", SUMMARY)
    assert _indexed("s1") == {"transcript", "summary"}
    assert _fts_hits("dagger")
    ts.save_transcript(md, TRANSCRIPT.replace("castle", "tower"))
    assert _fts_hits("castle") == [] and _fts_hits("tower")


def test_rebuild_drops_and_rebuilds(out):
    _add(out, "s1", summary=SUMMARY)
    with db.transaction() as conn:  # corrupt-looking leftovers
        conn.execute("INSERT INTO search_fts (rowid, text) VALUES (999, 'orphan ghost')")
    assert si.rebuild() == 1
    assert _fts_hits("ghost") == []
    assert _indexed("s1") == {"transcript", "summary"}


def test_relink_reindexes_new_file(out):
    _add(out, "old")
    create_campaign("Curse")
    move_transcript_to_campaign("old", "curse")
    (out / "old.md").unlink()
    ts.reconcile(out)
    _write(out, "new", TRANSCRIPT.replace("Strahd", "Rahadin"))
    ts.relink("old", "new")
    assert _indexed("new") == {"transcript"}
    assert _fts_hits("strahd") == [] and _fts_hits("rahadin")


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def test_reconcile_marks_changed_file_stale_and_backfill_reindexes(out):
    md = _add(out, "s1")
    _bump(md, TRANSCRIPT.replace("Strahd", "Ireena"))
    ts.reconcile(out)
    assert _indexed("s1") == set()
    assert _fts_hits("strahd") == []  # stale blocks went with the state row
    si.run_backfill()
    assert _fts_hits("ireena")


def test_size_change_alone_marks_stale(out):
    md = _add(out, "s1")
    st = md.stat()
    md.write_text(TRANSCRIPT + "\nmore\n", encoding="utf-8")
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert si.check_freshness(out) == 1


def test_summary_appearing_or_vanishing_marks_stale(out):
    _add(out, "s1")
    (out / "s1.summary.md").write_text(SUMMARY, encoding="utf-8")
    assert si.check_freshness(out) == 1
    si.run_backfill()
    assert _indexed("s1") == {"transcript", "summary"}
    (out / "s1.summary.md").unlink()
    assert si.check_freshness(out) == 1


def test_unchanged_files_stay_indexed(out):
    _add(out, "s1", summary=SUMMARY)
    assert si.check_freshness(out) == 0
    assert _indexed("s1") == {"transcript", "summary"}


def test_missing_transcript_keeps_index_but_is_not_searched(out):
    md = _add(out, "s1")
    md.rename(out.parent / "away.md")
    ts.reconcile(out)
    assert _indexed("s1") == {"transcript"}
    assert si.search("strahd").groups == []
    (out.parent / "away.md").rename(md)
    ts.reconcile(out)
    assert _stems(si.search("strahd")) == ["s1"]


def test_backfill_resumes_after_interruption(out):
    for i in range(3):
        _write(out, f"s{i}")
    ts.reconcile(out)
    stop = threading.Event()

    def report(done, todo):
        stop.set()

    assert si.run_backfill(report=report, stop=stop) == 1
    assert si.progress() == (1, 3)
    assert si.run_backfill() == 2
    assert si.progress() == (3, 3)


def test_worker_indexes_in_background(out, monkeypatch):
    monkeypatch.setattr(si, "AUTOSTART_WORKER", True)
    _write(out, "s1")
    ts.reconcile(out)
    si.start_worker()
    try:
        for _ in range(200):
            if si.progress() == (1, 1):
                break
            threading.Event().wait(0.01)
        assert si.progress() == (1, 1)
    finally:
        si.stop_worker()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def test_search_finds_block_with_speaker_timestamp_and_highlight(out):
    _add(out, "s1")
    page = si.search("fights")
    (group,) = page.groups
    hits = [(h.kind, h.block_idx, h.speaker, h.timestamp) for h in group.hits]
    # porter: "fights" also matches "fight" in Bob's block
    assert hits[0][:2] in {("transcript", 0), ("transcript", 1)}
    assert {h[1] for h in hits} == {0, 1}
    by_idx = {h.block_idx: h for h in group.hits}
    assert "<mark>fights</mark>" in by_idx[0].snippet
    assert "<mark>fight</mark>" in by_idx[1].snippet  # stemmed match highlighted
    assert by_idx[1].timestamp == "01:02:03" and by_idx[1].anchor == "b-1"


def test_accent_and_prefix_search(out):
    _add(out, "s1")
    assert _stems(si.search("cafe")) == ["s1"]
    assert _stems(si.search("Stra*")) == ["s1"]
    assert _stems(si.search("Stra")) == []


def test_phrase_search(out):
    _add(out, "s1")
    assert _stems(si.search('"castle gate"')) == ["s1"]
    assert _stems(si.search('"gate castle"')) == []


@pytest.mark.parametrize("query", [
    '"', '"unclosed', 'NEAR(strahd', 'strahd OR', '-strahd', 'text:strahd',
    'strahd AND', '(', '*', '^strahd', 'strahd"castle', "col:'x'",
])
def test_query_syntax_is_inert(out, query):
    _add(out, "s1")
    page = si.search(query)
    assert page.error is None


def test_operators_are_plain_words(out):
    _add(out, "s1")
    # "OR" is a word to find, not an operator: nothing has both.
    assert _stems(si.search("strahd OR ireena")) == []
    assert _stems(si.search("-strahd")) == ["s1"]


def test_empty_query_returns_nothing(out):
    _add(out, "s1")
    assert si.search("   ").groups == []
    assert si.search('"" ***').groups == []


def test_rejected_query_returns_generic_error(out, monkeypatch):
    _add(out, "s1")
    monkeypatch.setattr(si, "build_match", lambda q: "NEAR(")
    page = si.search("x")
    assert page.error == si.SEARCH_ERROR
    assert "fts5" not in page.error


def test_snippet_escapes_html(out):
    _add(out, "s1", TRANSCRIPT.replace("Strahd", "<script>alert(1)</script> Strahd"))
    (group,) = si.search("strahd").groups
    html = str(group.hits[0].snippet)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<mark>Strahd</mark>" in html


def test_snippet_windows_long_text():
    text = ("word " * 200) + "needle " + ("tail " * 200)
    html = str(si.snippet(text, si.highlight_pattern("needle")))
    assert html.startswith("…") and html.endswith("…")
    assert "<mark>needle</mark>" in html
    assert len(html) < si.SNIPPET_CHARS + 60


def test_filters_campaign_speaker_kind(out):
    _add(out, "s1", summary=SUMMARY)
    _add(out, "s2")
    create_campaign("Curse")
    move_transcript_to_campaign("s1", "curse")
    assert _stems(si.search("strahd", campaign="curse")) == ["s1"]
    assert si.search("strahd", campaign="curse").groups[0].campaign_name == "Curse"
    assert sorted(_stems(si.search("strahd"))) == ["s1", "s2"]
    assert {h.speaker for g in si.search("fight", speaker="Bob").groups for h in g.hits} == {"Bob"}
    kinds = {h.kind for g in si.search("strahd", kind="summary").groups for h in g.hits}
    assert kinds == {"summary"}
    assert _stems(si.search("dagger", kind="transcript")) == []
    assert si.search("strahd", kind="bogus").groups == []
    assert si.speakers() == ["Alice", "Bob"]
    every = si.search("strahd", campaign="curse", speaker="Alice", kind="transcript")
    assert _stems(every) == ["s1"] and every.error is None


def test_paging_by_transcript(out):
    for i in range(5):
        _add(out, f"s{i}")
    p1 = si.search("strahd", per_page=2)
    p2 = si.search("strahd", page=2, per_page=2)
    p3 = si.search("strahd", page=3, per_page=2)
    assert (len(p1.groups), p1.has_next) == (2, True)
    assert (len(p2.groups), p2.has_next) == (2, True)
    assert (len(p3.groups), p3.has_next) == (1, False)
    assert len({*_stems(p1), *_stems(p2), *_stems(p3)}) == 5


def test_hits_per_transcript_capped(out):
    text = "\n\n".join(f"**A** *(00:{i:02d})*: strahd number {i}" for i in range(6))
    _add(out, "s1", text)
    (group,) = si.search("strahd").groups
    assert group.total_hits == 6 and len(group.hits) == si.HITS_PER_TRANSCRIPT


def test_changed_file_shows_reindexing_state(out):
    md = _add(out, "s1")
    _bump(md, "**Zed** *(00:01)*: completely different text now")
    (group,) = si.search("strahd").groups
    assert group.stale and all(not h.snippet for h in group.hits)
    assert _indexed("s1") == set()  # marked for the backfill
    si.run_backfill()
    assert si.search("strahd").groups == []


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

# Every atomic_write_text() call outside transcript_store, by (file, target).
# Transcript and summary rewrites must use save_transcript()/save_summary()
# so the search index follows; a new call must be reviewed here.
_ATOMIC_WRITE_ALLOWED = {
    ("journal.py", "pending_file"),        # campaign journal
    ("cli.py", "backup"),                  # <stem>.md.bak before refine
    ("web/jobs.py", "backup"),
    ("web/jobs.py", "text_path"),          # speaker excerpt .txt
    ("legacy_import.py", "path"),          # slimmed _diar.json sidecar
    ("pipeline.py", "out_path"),           # new transcript; register() indexes it
}


def test_transcript_writes_reindex():
    calls = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel == "transcript_store.py":
            continue
        for m in re.finditer(r"\batomic_write_text\(\s*([\w.]+)", path.read_text(encoding="utf-8")):
            calls.add((rel, m.group(1)))
    assert calls - _ATOMIC_WRITE_ALLOWED == set(), (
        "rewrite transcripts/summaries with transcript_store.save_transcript()/save_summary()")


def test_search_kind_check_mirrors_constants():
    ddl = "\n".join(m.ddl for m in db.MIGRATIONS)
    m = re.search(r"kind\s+TEXT NOT NULL CHECK \(kind IN \(([^)]*)\)\)", ddl)
    assert m and set(re.findall(r"'([^']+)'", m.group(1))) == set(si.KINDS)


def test_blocks_require_their_state_row():
    with db.transaction() as conn:
        conn.execute("INSERT INTO transcripts (stem, created_at) VALUES ('x', 'now')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO search_blocks (transcript_id, kind, block_idx) "
                         "VALUES (1, 'transcript', 0)")
        conn.execute("INSERT INTO search_index_state VALUES (1, 'summary', 1, 1)")
        with pytest.raises(sqlite3.IntegrityError):  # summaries have no speaker
            conn.execute("INSERT INTO search_blocks (transcript_id, kind, block_idx, speaker) "
                         "VALUES (1, 'summary', 0, 'Alice')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO search_index_state VALUES (1, 'journal', 1, 1)")


# ---------------------------------------------------------------------------
# App write paths
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from wisper_transcribe.web.app import create_app
    with TestClient(create_app()) as c:
        yield c


def test_edit_page_save_reindexes(out, client):
    _add(out, "s1")
    r = client.post("/transcripts/s1/edit", data={"speaker_1": "Zanthor"}, follow_redirects=False)
    assert r.status_code == 303
    assert si.search("fight", speaker="Zanthor").groups


def test_fix_speaker_reindexes(out, client):
    _add(out, "s1")
    client.post("/transcripts/s1/fix-speaker", data={"old_name": "Bob", "new_name": "Rudolph"})
    assert si.speakers() == ["Alice", "Rudolph"]


def test_cli_fix_reindexes(out):
    from click.testing import CliRunner

    from wisper_transcribe.cli import main
    _add(out, "s1")
    result = CliRunner().invoke(main, ["fix", str(out / "s1.md"), "--speaker", "Bob", "--name", "Ezmerelda"])
    assert result.exit_code == 0, result.output
    assert "Ezmerelda" in si.speakers()
