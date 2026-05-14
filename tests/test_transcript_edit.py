"""Tests for the per-line speaker rename edit routes."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


_SAMPLE_MD = """\
---
title: Session 01
source_file: session01.mp3
date_processed: '2026-05-01'
duration: 1:00:00
speakers:
- name: Alice
- name: Bob
---

# Session 01

**Alice** *(00:00)*: Welcome everyone
**Bob** *(00:12)*: Thanks for having me
**Alice** *(00:18)*: Let's get started

---
*Transcribed by wisper-transcribe v1.0*
"""


@pytest.fixture()
def app():
    from wisper_transcribe.web.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def transcript_file(tmp_path: Path) -> Path:
    f = tmp_path / "session01.md"
    f.write_text(_SAMPLE_MD, encoding="utf-8")
    return f


def _patch_output(tmp_path: Path):
    return patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path)


# ---------------------------------------------------------------------------
# GET /transcripts/{name}/edit
# ---------------------------------------------------------------------------


def test_edit_get_renders_page(client: TestClient, transcript_file: Path):
    tmp_path = transcript_file.parent
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/edit")
    assert resp.status_code == 200
    assert b"Alice" in resp.content
    assert b"Bob" in resp.content
    assert b"Welcome everyone" in resp.content
    assert b"edit-form" in resp.content


def test_edit_get_shows_all_blocks(client: TestClient, transcript_file: Path):
    tmp_path = transcript_file.parent
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/edit")
    body = resp.content.decode()
    # Three speaker blocks: Alice, Bob, Alice
    assert body.count('name="speaker_') == 3


def test_edit_get_missing_transcript_returns_404(client: TestClient, tmp_path: Path):
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/nonexistent/edit")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /transcripts/{name}/edit
# ---------------------------------------------------------------------------


def test_edit_post_renames_single_block(client: TestClient, transcript_file: Path):
    tmp_path = transcript_file.parent
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/edit",
            data={"speaker_1": "Charlie"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcripts/session01"

    updated = transcript_file.read_text(encoding="utf-8")
    assert "**Charlie**" in updated
    assert "**Bob**" not in updated
    assert updated.count("**Alice**") == 2


def test_edit_post_multiple_changes(client: TestClient, transcript_file: Path):
    tmp_path = transcript_file.parent
    with _patch_output(tmp_path):
        client.post(
            "/transcripts/session01/edit",
            data={"speaker_0": "Diana", "speaker_2": "Eve"},
            follow_redirects=False,
        )
    updated = transcript_file.read_text(encoding="utf-8")
    assert "**Diana**" in updated
    assert "**Eve**" in updated
    assert "**Bob**" in updated


def test_edit_post_no_changes_leaves_file_intact(client: TestClient, transcript_file: Path):
    tmp_path = transcript_file.parent
    original = transcript_file.read_text(encoding="utf-8")
    with _patch_output(tmp_path):
        client.post("/transcripts/session01/edit", data={}, follow_redirects=False)
    assert transcript_file.read_text(encoding="utf-8") == original


def test_edit_post_missing_transcript_returns_404(client: TestClient, tmp_path: Path):
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/nonexistent/edit",
            data={"speaker_0": "Alice"},
            follow_redirects=False,
        )
    assert resp.status_code == 404


def test_edit_post_strips_newlines_from_speaker(client: TestClient, transcript_file: Path):
    tmp_path = transcript_file.parent
    with _patch_output(tmp_path):
        client.post(
            "/transcripts/session01/edit",
            data={"speaker_0": "Di\nana"},
            follow_redirects=False,
        )
    updated = transcript_file.read_text(encoding="utf-8")
    assert "**Diana**" in updated
