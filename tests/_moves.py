"""Helpers for the transcript move/rename tests (files move on disk)."""
from __future__ import annotations

import os
from pathlib import Path

from wisper_transcribe import db, file_registry

from . import _seed


def claimed_campaign(display_name: str) -> tuple[int, str, str]:
    """A campaign whose folder exists and is claimed. Returns (id, slug, folder)."""
    cid = _seed.seed_campaign(display_name, claimed=True)
    with db.connection() as conn:
        slug, folder = conn.execute(
            "SELECT slug, folder FROM campaigns WHERE id = ?", (cid,)).fetchone()
    return cid, slug, folder


def placed_session(stem: str, *, campaign: str | None = None,
                   directory: Path | None = None) -> tuple[int, Path]:
    """A new transcript whose ``.md`` is written and registered in ``directory``.

    Returns ``(id, md)``. Always a fresh row, so two campaigns may share a stem.
    With a campaign slug, the row carries that ``campaign_id``.
    """
    from wisper_transcribe.config import get_output_root

    root = get_output_root()
    with db.transaction() as conn:
        campaign_id = None
        position = None
        if campaign is not None:
            campaign_id = conn.execute(
                "SELECT id FROM campaigns WHERE slug = ?", (campaign,)).fetchone()[0]
            position = conn.execute(
                "SELECT coalesce(max(position), -1) + 1 FROM transcripts WHERE campaign_id = ?",
                (campaign_id,)).fetchone()[0]
        tid = conn.execute(
            "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (stem, campaign_id, position, db.now_utc())).fetchone()[0]
    directory = root if directory is None else directory
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / f"{stem}.md"
    md.write_text(f"# {stem}\n", encoding="utf-8")
    file_registry.add(md, kind="transcript",
                      owner=file_registry.Owner("transcript", tid), output_dir=root)
    with db.transaction() as conn:
        conn.execute("UPDATE transcripts SET missing_since = NULL WHERE id = ?", (tid,))
    return tid, md


def add_companion(tid: int, md: Path, suffix: str, text: str = "x") -> Path:
    """Write ``<md stem><suffix>`` beside ``md`` and register it."""
    from wisper_transcribe.config import get_output_root

    path = md.with_name(md.stem + suffix)
    path.write_text(text, encoding="utf-8")
    kinds = {".summary.md": "summary", "_diar.json": "sidecar", ".flac": "audio",
             ".md.bak": "backup"}
    file_registry.add(path, kind=kinds[suffix], owner=file_registry.Owner("transcript", tid),
                      output_dir=get_output_root())
    return path


def lock_os_replace(monkeypatch, names: set[str]) -> dict:
    """Make ``os.replace``/``os.rename`` fail for the named files (Windows lock).

    Returns a toggle: set ``state["lock"] = False`` to "close the file".
    """
    real_replace = os.replace
    state = {"lock": True}

    def guarded_replace(src, dst, *a, **k):
        if state["lock"] and (Path(src).name in names or Path(dst).name in names):
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", guarded_replace)
    monkeypatch.setattr(os, "rename", guarded_replace)
    return state
