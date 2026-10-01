"""/search page, sidebar box, and the transcript/summary deep-link anchors."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wisper_transcribe import search_index as si, transcript_store as ts
from wisper_transcribe.campaign_manager import create_campaign, move_transcript_to_campaign
from wisper_transcribe.path_utils import get_output_dir

from .test_search_index import SUMMARY, TRANSCRIPT, _bump


@pytest.fixture
def out() -> Path:
    d = get_output_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def client():
    from wisper_transcribe.web.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _add(out: Path, stem: str, text: str = TRANSCRIPT, summary: str | None = None) -> Path:
    md = out / f"{stem}.md"
    md.write_text(text, encoding="utf-8")
    if summary is not None:
        (out / f"{stem}.summary.md").write_text(summary, encoding="utf-8")
    ts.register(stem, origin="job")
    return md


def test_empty_search_page_shows_help_and_sidebar_box(client):
    r = client.get("/search")
    assert r.status_code == 200
    assert 'data-testid="sidebar-search"' in r.text
    assert "match a phrase" in r.text


def test_sidebar_box_on_every_page(client):
    assert 'data-testid="sidebar-search"' in client.get("/transcripts").text


def test_results_link_to_block_with_query(out, client):
    _add(out, "Session — 1 (x)", summary=SUMMARY)
    r = client.get("/search", params={"q": "strahd"})
    assert r.status_code == 200
    assert r.text.count('data-testid="search-group"') == 1
    assert "<mark>Strahd</mark>" in r.text
    assert '/transcripts/Session%20%E2%80%94%201%20%28x%29?q=strahd#b-0' in r.text
    assert '/transcripts/Session%20%E2%80%94%201%20%28x%29/summary?q=strahd#s-1' in r.text
    assert "00:05" in r.text and "Alice" in r.text


def test_title_hit_links_to_the_transcript_top(out, client):
    _add(out, "Cumstone - Year One")
    page = client.get("/search", params={"q": "cumstone"}).text
    assert "TITLE" in page
    assert 'href="/transcripts/Cumstone%20-%20Year%20One?q=cumstone"' in page
    assert "<mark>Cumstone</mark>" in page


def test_query_with_ampersand_is_encoded_in_links(out, client):
    _add(out, "s1")
    r = client.get("/search", params={"q": "strahd & castle"})
    assert "?q=strahd%20%26%20castle#b-0" in r.text


def test_xss_in_query_and_text_is_escaped(out, client):
    _add(out, "s1", TRANSCRIPT.replace("Strahd", "<script>alert(1)</script> Strahd"))
    r = client.get("/search", params={"q": "<script>alert(1)</script> strahd"})
    assert 'data-testid="search-hit"' in r.text
    assert "<script>alert(1)" not in r.text
    assert "&lt;<mark>script</mark>&gt;<mark>alert</mark>(<mark>1</mark>)&lt;/" in r.text


def test_filters_and_unknown_values_ignored(out, client):
    _add(out, "s1")
    _add(out, "s2")
    create_campaign("Curse")
    move_transcript_to_campaign("s1", "curse")
    r = client.get("/search", params={"q": "strahd", "campaign": "curse"})
    assert r.text.count('data-testid="search-group"') == 1
    assert "Curse" in r.text
    r = client.get("/search", params={"q": "strahd", "campaign": "nope", "speaker": "Nobody",
                                      "kind": "bogus", "page": "-3"})
    assert r.status_code == 200
    assert r.text.count('data-testid="search-group"') == 2


def test_paging_links(out, client, monkeypatch):
    monkeypatch.setattr(si, "PAGE_SIZE", 1)
    for i in range(3):
        _add(out, f"s{i}")
    first = client.get("/search", params={"q": "strahd"}).text
    assert first.count('data-testid="search-group"') == 1
    assert "page=2" in first and "previous" not in first
    middle = client.get("/search", params={"q": "strahd", "page": 2}).text
    assert "page=1" in middle and "page=3" in middle
    beyond = client.get("/search", params={"q": "strahd", "page": 99999})
    assert beyond.status_code == 200 and 'data-testid="search-empty"' in beyond.text


def test_no_matches_and_indexing_progress(out, client):
    _add(out, "s1")
    (out / "s2.md").write_text(TRANSCRIPT, encoding="utf-8")
    ts.reconcile(out)  # registered, not yet indexed
    r = client.get("/search", params={"q": "zzzqqq"})
    assert 'data-testid="search-empty"' in r.text
    assert "INDEXING 1 OF 2" in r.text
    assert "1 transcript still being indexed" in r.text


def test_rejected_query_shows_generic_error(out, client, monkeypatch):
    _add(out, "s1")
    monkeypatch.setattr(si, "build_match", lambda q: "NEAR(")
    r = client.get("/search", params={"q": "x"})
    assert 'data-testid="search-error"' in r.text
    assert "fts5" not in r.text.lower() and "syntax" not in r.text.lower()


def test_changed_file_shows_reindexing(out, client):
    md = _add(out, "s1")
    _bump(md, TRANSCRIPT.replace("Strahd", "Someone"))
    r = client.get("/search", params={"q": "strahd"})
    assert 'data-testid="search-stale"' in r.text
    assert "#b-" not in r.text


# ---------------------------------------------------------------------------
# Anchors and highlighting on the target pages
# ---------------------------------------------------------------------------

def test_transcript_page_has_block_anchors(out, client):
    _add(out, "s1")
    html = client.get("/transcripts/s1").text
    assert re.findall(r'id="(b-\d+)"', html) == ["b-0", "b-1", "b-2"]
    assert "new RegExp" not in html  # no query, no highlight script


def test_anchor_numbering_matches_index_without_blank_lines(out, client):
    text = TRANSCRIPT.replace("\n\n**", "\n**")  # blocks on consecutive lines
    _add(out, "s1", text)
    html = client.get("/transcripts/s1").text
    assert re.findall(r'id="(b-\d+)"', html) == ["b-0", "b-1", "b-2"]
    assert '<span id="b-1" class="block-anchor"><strong>Bob</strong>' in html


def test_transcript_page_highlight_script_with_query(out, client):
    _add(out, "s1")
    html = client.get("/transcripts/s1", params={"q": "fights"}).text
    assert 'new RegExp("\\\\b(?:fight)\\\\w*", "gi")' in html


def test_highlight_pattern_cannot_break_out_of_script(out, client):
    _add(out, "s1")
    html = client.get("/transcripts/s1", params={"q": '</script><img src=x onerror=alert(1)>'}).text
    assert "<img src=x" not in html
    assert html.count("</script>") == html.count("<script")


def test_summary_page_has_section_anchors(out, client):
    _add(out, "s1", summary=SUMMARY)
    html = client.get("/transcripts/s1/summary", params={"q": "dagger"}).text
    assert re.findall(r'id="(s-\d+)"', html) == ["s-0", "s-1", "s-2"]
    assert "new RegExp" in html
