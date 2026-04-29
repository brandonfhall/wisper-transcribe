"""Transcripts route — browse, view, and post-process markdown transcripts."""
from __future__ import annotations

import html as _html_module
import os
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from wisper_transcribe.campaign_manager import (
    _validate_campaign_slug,
    get_campaign_for_transcript,
    load_campaigns,
    move_transcript_to_campaign,
    remove_transcript_from_campaign,
)

from . import templates

router = APIRouter(prefix="/transcripts")


def _output_dir(request: Request) -> Path:
    """Resolve the output directory for transcripts."""
    out_dir = Path("output")
    if not out_dir.exists():
        from wisper_transcribe.config import get_data_dir
        out_dir = Path(get_data_dir()) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


class _HtmlSanitizer(HTMLParser):
    """Strip <script> elements and on* event-handler attributes from HTML.

    Uses Python's built-in HTMLParser rather than regex so that all syntactic
    variants of tags (e.g. ``</script >``, ``</SCRIPT>``) are handled
    correctly — regex-based approaches can be bypassed by whitespace or
    case variations in closing tags (A03 XSS — CWE-79).
    """

    _STRIP_TAGS: frozenset[str] = frozenset({"script"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._output: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
        if tag.lower() in self._STRIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        safe_attrs = [(k, v) for k, v in attrs if not k.lower().startswith("on")]
        attr_str = "".join(
            f' {k}="{_html_module.escape(v)}"' if v is not None else f" {k}"
            for k, v in safe_attrs
        )
        self._output.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag.lower() in self._STRIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        self._output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if not self._skip_depth:
            self._output.append(_html_module.escape(data))

    def get_output(self) -> str:
        return "".join(self._output)


def _sanitize_html(html_input: str) -> str:
    """Return *html_input* with script elements and on* handlers removed."""
    sanitizer = _HtmlSanitizer()
    sanitizer.feed(html_input)
    return sanitizer.get_output()


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


def _get_safe_transcript_path(request: Request, name: str) -> Path | None:
    """Resolve and sanitize transcript path, mitigating path traversal."""
    if not name or "\x00" in name:
        return None

    safe_name = os.path.basename(name)
    if safe_name != name or safe_name in {".", ".."}:
        return None

    out_dir = _output_dir(request).resolve()

    base_dir = os.path.abspath(str(out_dir))
    if not base_dir.endswith(os.sep):
        base_dir += os.sep

    target_path = os.path.abspath(os.path.join(str(out_dir), f"{safe_name}.md"))
    if not target_path.startswith(base_dir):
        return None

    return Path(target_path)


def _get_safe_summary_path(request: Request, name: str) -> Path | None:
    """Like _get_safe_transcript_path but for .summary.md sidecar files."""
    if not name or "\x00" in name:
        return None

    safe_name = os.path.basename(name)
    if safe_name != name or safe_name in {".", ".."}:
        return None

    out_dir = _output_dir(request).resolve()

    base_dir = os.path.abspath(str(out_dir))
    if not base_dir.endswith(os.sep):
        base_dir += os.sep

    target_path = os.path.abspath(
        os.path.join(str(out_dir), f"{safe_name}.summary.md")
    )
    if not target_path.startswith(base_dir):
        return None

    return Path(target_path)


@router.get("", response_class=HTMLResponse)
async def transcripts_list(request: Request) -> HTMLResponse:
    out_dir = _output_dir(request)
    # Exclude .summary.md sidecars — they are shown via the transcript detail page
    files = sorted(
        [f for f in out_dir.glob("*.md") if not f.name.endswith(".summary.md")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    campaigns = load_campaigns()

    # Build stem → campaign slug mapping
    stem_to_campaign: dict[str, str] = {}
    for slug, c in campaigns.items():
        for stem in c.transcripts:
            stem_to_campaign[stem] = slug

    items = []
    for f in files:
        meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
        summary_file = f.with_name(f"{f.stem}.summary.md")
        items.append({
            "stem": f.stem,
            "name": f.name,
            "title": meta.get("title", f.stem),
            "date_processed": meta.get("date_processed", ""),
            "duration": meta.get("duration", ""),
            "speakers": meta.get("speakers", []),
            "has_summary": summary_file.exists(),
            "campaign_slug": stem_to_campaign.get(f.stem),
        })

    return templates.TemplateResponse(
        request,
        "transcripts.html",
        {
            "request": request,
            "transcripts": items,
            "campaigns": campaigns,
            "stem_to_campaign": stem_to_campaign,
        },
    )


@router.get("/{name}", response_class=HTMLResponse)
async def transcript_detail(request: Request, name: str) -> HTMLResponse:
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    content = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _sanitize_html(_md.markdown(body, extensions=["nl2br"]))

    # Check for summary sidecar
    summary_path = _get_safe_summary_path(request, name)
    has_summary = bool(summary_path and summary_path.exists())

    # Load current LLM config for display
    from wisper_transcribe.config import load_config
    cfg = load_config()
    llm_provider = cfg.get("llm_provider", "ollama") or "ollama"
    llm_model = cfg.get("llm_model", "") or ""

    campaigns = load_campaigns()
    current_campaign_slug = get_campaign_for_transcript(md_path.stem)

    return templates.TemplateResponse(
        request,
        "transcript_detail.html",
        {
            "request": request,
            "name": name,
            "meta": meta,
            "html_body": html_body,
            "raw_path": str(md_path),
            "has_summary": has_summary,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "campaigns": campaigns,
            "current_campaign_slug": current_campaign_slug,
        },
    )


@router.get("/{name}/download")
async def transcript_download(request: Request, name: str):
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    return FileResponse(
        path=str(md_path),
        media_type="text/markdown",
        filename=md_path.name,
    )


@router.post("/{name}/delete", response_class=HTMLResponse)
async def delete_transcript(request: Request, name: str) -> HTMLResponse:
    """Delete a transcript .md file (and its summary sidecar if present)."""
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)
    if md_path.exists():
        md_path.unlink()
    # Also remove summary sidecar if present
    summary_path = _get_safe_summary_path(request, name)
    if summary_path and summary_path.exists():
        summary_path.unlink()
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": "/transcripts"},
    )


@router.post("/{name}/fix-speaker", response_class=HTMLResponse)
async def fix_speaker(request: Request, name: str) -> HTMLResponse:
    """Rename a speaker in an existing transcript."""
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)
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
        headers={"Location": f"/transcripts/{quote(name)}"},
    )


@router.post("/{name}/refine", response_class=HTMLResponse)
async def post_refine(request: Request, name: str) -> HTMLResponse:
    """Submit a vocabulary-refine LLM job for an existing transcript."""
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    from . import get_queue
    from ..jobs import JOB_REFINE

    queue = get_queue(request)
    job = queue.submit_llm(
        transcript_path=str(md_path),
        job_type=JOB_REFINE,
        name=name,
    )
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcribe/jobs/{job.id}"},
    )


@router.post("/{name}/summarize", response_class=HTMLResponse)
async def post_summarize(request: Request, name: str) -> HTMLResponse:
    """Submit a campaign-summary LLM job for an existing transcript."""
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    from . import get_queue
    from ..jobs import JOB_SUMMARIZE

    queue = get_queue(request)
    job = queue.submit_llm(
        transcript_path=str(md_path),
        job_type=JOB_SUMMARIZE,
        name=name,
    )
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcribe/jobs/{job.id}"},
    )


@router.get("/{name}/summary", response_class=HTMLResponse)
async def summary_detail(request: Request, name: str) -> HTMLResponse:
    """Render the campaign-notes summary for a transcript."""
    md_path = _get_safe_transcript_path(request, name)
    if not md_path:
        return HTMLResponse(content="Invalid name", status_code=400)

    summary_path = _get_safe_summary_path(request, name)
    if not summary_path or not summary_path.exists():
        return HTMLResponse(content="Summary not found", status_code=404)

    content = summary_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _sanitize_html(_md.markdown(body, extensions=["nl2br"]))

    return templates.TemplateResponse(
        request,
        "summary_detail.html",
        {
            "request": request,
            "name": name,
            "meta": meta,
            "html_body": html_body,
            "title": meta.get("title", f"{name} — Campaign Notes"),
        },
    )


@router.get("/{name}/summary/download")
async def summary_download(request: Request, name: str):
    """Download the .summary.md sidecar file."""
    summary_path = _get_safe_summary_path(request, name)
    if not summary_path:
        return HTMLResponse(content="Invalid name", status_code=400)
    if not summary_path.exists():
        return HTMLResponse(content="Summary not found", status_code=404)
    return FileResponse(
        path=str(summary_path),
        media_type="text/markdown",
        filename=summary_path.name,
    )


@router.post("/{name}/campaign", response_class=HTMLResponse)
async def assign_campaign(request: Request, name: str) -> HTMLResponse:
    """Assign or remove a campaign association for a transcript."""
    safe_name = _get_safe_transcript_path(request, name)
    if not safe_name:
        return HTMLResponse(content="Invalid name", status_code=400)

    form = await request.form()
    campaign_slug = str(form.get("campaign", "")).strip()

    if campaign_slug:
        safe_slug = _validate_campaign_slug(campaign_slug)
        if safe_slug is None:
            return HTMLResponse(
                content="",
                status_code=303,
                headers={"Location": f"/transcripts/{quote(name)}?error=invalid_campaign"},
            )
        try:
            move_transcript_to_campaign(safe_name.stem, safe_slug)
        except KeyError:
            return HTMLResponse(
                content="",
                status_code=303,
                headers={"Location": f"/transcripts/{quote(name)}?error=not_found"},
            )
    else:
        remove_transcript_from_campaign(safe_name.stem)

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{quote(name)}"},
    )
