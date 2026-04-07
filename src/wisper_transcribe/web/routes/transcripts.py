"""Transcripts route — browse and view markdown transcripts."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from . import templates

router = APIRouter(prefix="/transcripts")


def _output_dir(request: Request) -> Path:
    """Resolve the output directory for transcripts."""
    # Allow overriding via query param for testing, otherwise default to ./output
    out_dir = Path("output")
    if not out_dir.exists():
        from wisper_transcribe.config import get_data_dir
        out_dir = Path(get_data_dir()) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body.  Returns (metadata, body)."""
    import yaml

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return meta, parts[2].strip()
            except Exception:
                pass
    return {}, content


@router.get("", response_class=HTMLResponse)
async def transcripts_list(request: Request) -> HTMLResponse:
    out_dir = _output_dir(request)
    files = sorted(out_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)

    items = []
    for f in files:
        meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
        items.append({
            "stem": f.stem,
            "name": f.name,
            "title": meta.get("title", f.stem),
            "date_processed": meta.get("date_processed", ""),
            "duration": meta.get("duration", ""),
            "speakers": meta.get("speakers", []),
        })

    return templates.TemplateResponse(
        "transcripts.html",
        {"request": request, "transcripts": items},
    )


@router.get("/{name}", response_class=HTMLResponse)
async def transcript_detail(request: Request, name: str) -> HTMLResponse:
    # Sanitize name — allow only alphanumerics, hyphens, underscores
    if not re.match(r"^[\w\-]+$", name):
        return HTMLResponse(content="Invalid name", status_code=400)

    out_dir = _output_dir(request)
    md_path = out_dir / f"{name}.md"
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    content = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _md.markdown(body, extensions=["nl2br"])

    return templates.TemplateResponse(
        "transcript_detail.html",
        {
            "request": request,
            "name": name,
            "meta": meta,
            "html_body": html_body,
            "raw_path": str(md_path),
        },
    )


@router.get("/{name}/download")
async def transcript_download(request: Request, name: str) -> FileResponse:
    if not re.match(r"^[\w\-]+$", name):
        return HTMLResponse(content="Invalid name", status_code=400)
    out_dir = _output_dir(request)
    md_path = out_dir / f"{name}.md"
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    return FileResponse(
        path=str(md_path),
        media_type="text/markdown",
        filename=md_path.name,
    )


@router.post("/{name}/fix-speaker", response_class=HTMLResponse)
async def fix_speaker(request: Request, name: str) -> HTMLResponse:
    """Rename a speaker in an existing transcript."""
    if not re.match(r"^[\w\-]+$", name):
        return HTMLResponse(content="Invalid name", status_code=400)
    out_dir = _output_dir(request)
    md_path = out_dir / f"{name}.md"
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    form = await request.form()
    old_name = str(form.get("old_name", "")).strip()
    new_name = str(form.get("new_name", "")).strip()

    if old_name and new_name:
        from wisper_transcribe.formatter import update_speaker_names
        content = md_path.read_text(encoding="utf-8")
        content = update_speaker_names(content, old_name, new_name)
        md_path.write_text(content, encoding="utf-8")

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{name}"},
    )
