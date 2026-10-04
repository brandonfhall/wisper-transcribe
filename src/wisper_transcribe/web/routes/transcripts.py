"""Transcripts route — browse, view, and post-process markdown transcripts."""
from __future__ import annotations

import html as _html_module
import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import quote

log = logging.getLogger(__name__)

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from wisper_transcribe.campaign_manager import (
    _validate_campaign_slug,
    get_campaign_for_transcript,
    load_campaigns,
    move_transcript_to_campaign,
    remove_transcript_from_campaign,
)
from wisper_transcribe.config import get_data_dir
from wisper_transcribe.recording_manager import load_recordings

from . import templates
from wisper_transcribe.path_utils import get_output_dir
from wisper_transcribe import transcript_store
from wisper_transcribe.web._responses import invalid_input_response

router = APIRouter(prefix="/transcripts")


class _HtmlSanitizer(HTMLParser):
    """Strip dangerous elements and attributes from HTML.

    Removes ``<script>``/``<iframe>``/``<object>``/``<embed>``, ``on*``
    attributes, and ``href``/``src`` values with a ``javascript:``/``data:``/
    ``vbscript:`` scheme. Uses HTMLParser rather than regex so variants like
    ``</script >`` and ``</SCRIPT>`` can't slip through (CWE-79).
    """

    # Content-bearing tags stripped together with everything inside them.
    _STRIP_TAGS: frozenset[str] = frozenset({"script", "iframe", "object"})
    # Void tags dropped outright (no closing tag, so no depth tracking —
    # an unclosed <embed> must not swallow the rest of the document).
    _VOID_STRIP_TAGS: frozenset[str] = frozenset({"embed"})
    # Attributes whose value is a URL and must not carry an active scheme.
    _URL_ATTRS: frozenset[str] = frozenset({"href", "src"})
    _BAD_SCHEMES: tuple[str, ...] = ("javascript:", "data:", "vbscript:")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._output: list[str] = []
        self._skip_depth: int = 0

    @classmethod
    def _is_unsafe_url(cls, value: str) -> bool:
        """True when *value* is a javascript:/data:/vbscript: URL.

        Drops every char <= 0x20 and lowercases first, since browsers accept
        obfuscations like ``java\\tscript:``. Character references are already
        decoded by HTMLParser.
        """
        cleaned = "".join(ch for ch in value if ord(ch) > 0x20).lower()
        return cleaned.startswith(cls._BAD_SCHEMES)

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
        t = tag.lower()
        if t in self._STRIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t in self._VOID_STRIP_TAGS:
            return
        safe_attrs = [
            (k, v) for k, v in attrs
            if not k.lower().startswith("on")
            and not (k.lower() in self._URL_ATTRS and v is not None and self._is_unsafe_url(v))
        ]
        attr_str = "".join(
            f' {k}="{_html_module.escape(v)}"' if v is not None else f" {k}"
            for k, v in safe_attrs
        )
        self._output.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        t = tag.lower()
        if t in self._STRIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if t in self._VOID_STRIP_TAGS:
            return
        self._output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if not self._skip_depth:
            self._output.append(_html_module.escape(data))

    def get_output(self) -> str:
        return "".join(self._output)


def _sanitize_html(html_input: str) -> str:
    """Return *html_input* with script/iframe/object/embed elements, on*
    handlers, and javascript:/data: URLs removed (see _HtmlSanitizer)."""
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


def _anchor_blocks(body: str) -> str:
    """Wrap each transcript block in ``<span id="b-<index>">`` so search
    results can deep-link to it. Numbered by ``searchable_blocks()``, the same
    numbering the search index stores."""
    from wisper_transcribe.formatter import searchable_blocks

    lines = body.splitlines()
    for lineno, block in searchable_blocks(body):
        lines[lineno] = (f'<span id="b-{block["index"]}" class="block-anchor">'
                         f'{lines[lineno].strip()}</span>')
    return "\n".join(lines)


def _anchor_sections(html: str) -> str:
    """Give the summary's ``<h2>`` sections ids ``s-1``, ``s-2``, … and the top
    ``s-0``, matching ``search_index.summary_sections()``."""
    count = 0

    def number(_match) -> str:
        nonlocal count
        count += 1
        return f'<h2 id="s-{count}" class="block-anchor">'

    return '<span id="s-0" class="block-anchor"></span>' + re.sub(r"<h2>", number, html)


def _highlight(q: str) -> str | None:
    """The JS-compatible highlight regex for a search query, or None."""
    from wisper_transcribe.search_index import highlight_pattern

    pattern = highlight_pattern(q[:500]) if q else None
    return pattern.pattern if pattern else None


def _get_safe_content_path(name: str, suffix: str) -> Path | None:
    """Resolve and sanitize a transcript output path, mitigating path traversal.

    `suffix` is the file extension to append, e.g. ".md" or ".summary.md".
    """
    if not name or "\x00" in name:
        return None

    safe_name = os.path.basename(name)
    if safe_name != name or safe_name in {".", ".."}:
        return None

    out_dir = get_output_dir().resolve()

    base_dir = os.path.abspath(str(out_dir))
    if not base_dir.endswith(os.sep):
        base_dir += os.sep

    target_path = os.path.abspath(os.path.join(str(out_dir), f"{safe_name}{suffix}"))
    if not target_path.startswith(base_dir):
        return None
    # Only a path already inside the base is probed on disk; re-checked after.
    target_path = transcript_store.existing_form(target_path)
    if not target_path.startswith(base_dir):
        return None

    return Path(target_path)


@router.get("/partials/recent", response_class=HTMLResponse)
async def recent_transcripts_partial(request: Request) -> HTMLResponse:
    """HTMX partial: 6 most recent transcripts for the dashboard archive section."""
    out_dir = get_output_dir()
    files = sorted(
        [f for f in out_dir.glob("*.md") if not f.name.endswith(".summary.md")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:6]
    items = []
    for f in files:
        meta, _ = _parse_frontmatter(f.read_text(encoding="utf-8"))
        items.append({
            "stem": f.stem,
            "title": meta.get("title", f.stem),
            "duration": meta.get("duration", ""),
            "date_processed": meta.get("date_processed", ""),
        })
    return templates.TemplateResponse(
        request,
        "partials/recent_transcripts.html",
        {"request": request, "transcripts": items},
    )


def _pending_recordings(data_dir: Path) -> tuple[list, set[str]]:
    """Recordings with status "completed" and audio on disk, newest first.

    Shown as "Awaiting transcription" on /transcripts. Returns
    ``(recordings, live_draft_ids)``, where the second is the subset that still
    has a ``live_transcript.md`` draft.
    """
    recordings = load_recordings(data_dir)
    pending = [
        r for r in recordings.values()
        if r.status == "completed" and r.combined_path and r.combined_path.exists()
    ]
    pending.sort(key=lambda r: r.started_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    live_draft_ids = {
        r.id for r in pending
        if (data_dir / "recordings" / r.id / "live_transcript.md").exists()
    }
    return pending, live_draft_ids


@router.get("", response_class=HTMLResponse)
async def transcripts_list(request: Request) -> HTMLResponse:
    out_dir = get_output_dir()
    transcript_store.reconcile(out_dir, sync="throttled")  # register new files, flag deleted ones
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

    pending_recordings, live_draft_ids = _pending_recordings(get_data_dir())
    attention = _attention_context(out_dir)

    return templates.TemplateResponse(
        request,
        "transcripts.html",
        {
            "request": request,
            "transcripts": items,
            "campaigns": campaigns,
            "stem_to_campaign": stem_to_campaign,
            "pending_recordings": pending_recordings,
            "live_draft_ids": live_draft_ids,
            "attention": attention,
            "relink_candidates": transcript_store.relink_candidates() if attention else [],
        },
    )


def _attention_context(out_dir: Path) -> Optional[dict]:
    """The Needs attention panel's data, or None when there is nothing to show."""
    try:
        found = transcript_store.needs_attention(out_dir)
    except Exception:
        log.warning("Could not list items needing attention", exc_info=True)
        return None
    if not found.total:
        return None
    base = os.path.abspath(str(out_dir))
    data_base = os.path.abspath(str(get_data_dir()))
    unclaimed = []
    for path in found.unclaimed:
        try:
            st = path.stat()
        except OSError:
            continue
        in_output = os.path.dirname(os.path.abspath(path)) == base
        unclaimed.append({
            "name": path.name if in_output else os.path.relpath(path, data_base),
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            # Only an output-root file with a recognised companion name may be deleted here.
            "deletable": in_output and transcript_store._companion_stem(path.name) is not None,
        })
    return {
        "missing_transcripts": found.missing_transcripts,
        "missing_files": [
            {"id": r.id, "name": r.rel_path, "kind": r.kind} for r in found.missing_files
        ],
        "unclaimed": unclaimed,
    }


@router.post("/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_transcripts(request: Request) -> HTMLResponse:
    """Delete multiple transcripts (and their summary sidecars) in one request."""
    form = await request.form()
    stems = form.getlist("stems")
    for stem in stems:
        md_path = _get_safe_content_path(stem, ".md")
        if not md_path:
            continue
        transcript_store.delete_transcript(md_path.stem, output_dir=md_path.parent)
    return HTMLResponse(content="", status_code=303, headers={"Location": "/transcripts"})


@router.post("/bulk-campaign", response_class=HTMLResponse)
async def bulk_assign_campaign(request: Request) -> HTMLResponse:
    """Assign or remove a campaign for multiple transcripts in one request."""
    form = await request.form()
    stems = form.getlist("stems")
    campaign = str(form.get("campaign", "")).strip()

    safe_slug: "Optional[str]" = None
    if campaign:
        safe_slug = _validate_campaign_slug(campaign)
        if safe_slug is None:
            return HTMLResponse(
                content="", status_code=303,
                headers={"Location": "/transcripts?error=invalid_campaign"},
            )

    for stem in stems:
        path = _get_safe_content_path(stem, ".md")
        if path is None:
            continue
        try:
            if safe_slug:
                move_transcript_to_campaign(path.stem, safe_slug)
            else:
                remove_transcript_from_campaign(path.stem)
        except Exception:
            pass

    return HTMLResponse(content="", status_code=303, headers={"Location": "/transcripts"})


@router.post("/relink", response_class=HTMLResponse)
async def relink_transcript(request: Request) -> RedirectResponse:
    """Give a missing transcript's identity (and companion files) to a renamed file."""
    form = await request.form()
    old_md = _get_safe_content_path(str(form.get("old_stem", "")), ".md")
    new_md = _get_safe_content_path(str(form.get("new_stem", "")), ".md")
    if old_md is None or new_md is None:
        return RedirectResponse(url="/transcripts?error=relink_failed", status_code=303)
    try:
        kept = transcript_store.relink(old_md.stem, new_md.stem, output_dir=old_md.parent)
    except (KeyError, ValueError):
        return RedirectResponse(url="/transcripts?error=relink_failed", status_code=303)
    notice = "?notice=relink_kept_names" if kept else ""
    return RedirectResponse(url=f"/transcripts{notice}", status_code=303)


@router.post("/needs-attention/forget", response_class=HTMLResponse)
async def forget_missing_file(request: Request) -> HTMLResponse:
    """Drop the record of a registered file that is gone from disk."""
    from wisper_transcribe import file_registry

    form = await request.form()
    try:
        file_id = int(str(form.get("file_id", "")))
    except ValueError:
        return invalid_input_response("Invalid file id")
    out_dir = get_output_dir()
    report = file_registry.last_report(None, out_dir)
    row = next((r for r in report.missing if r.id == file_id), None) if report else None
    # Only a row the latest sync reported missing whose file is still gone.
    if row is None or os.path.lexists(row.path):
        return RedirectResponse(url="/transcripts?error=forget_failed", status_code=303)
    file_registry.forget_id(row.id)
    file_registry.drop_from_report(None, out_dir, file_ids=[row.id])
    return RedirectResponse(url="/transcripts", status_code=303)


@router.post("/needs-attention/delete-file", response_class=HTMLResponse)
async def delete_unclaimed_file(request: Request) -> HTMLResponse:
    """Delete an output-root file with a companion-file name that nothing owns."""
    form = await request.form()
    name = str(form.get("name", ""))
    safe_name = os.path.basename(name)
    if (not name or "\x00" in name or safe_name != name or safe_name in {".", ".."}
            or transcript_store._companion_stem(safe_name) is None):
        return invalid_input_response("Invalid file name")
    out_dir = get_output_dir()
    base = os.path.abspath(str(out_dir))
    if not base.endswith(os.sep):
        base += os.sep
    target = os.path.abspath(os.path.join(base, safe_name))
    if not target.startswith(base):
        return invalid_input_response("Invalid file name")
    if not transcript_store.delete_unowned_file(Path(target), output_dir=out_dir):
        return RedirectResponse(url="/transcripts?error=delete_failed", status_code=303)
    return RedirectResponse(url="/transcripts", status_code=303)


@router.get("/{name}", response_class=HTMLResponse)
async def transcript_detail(request: Request, name: str, q: str = "") -> HTMLResponse:
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    content = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _sanitize_html(_md.markdown(_anchor_blocks(body), extensions=["nl2br"]))

    # Check for summary sidecar
    summary_path = _get_safe_content_path(name, ".summary.md")
    has_summary = bool(summary_path and summary_path.exists())

    # Check for enrollment sidecar (transcript-centric wizard)
    diar_path = _get_safe_content_path(name, "_diar.json")
    has_diar_sidecar = bool(diar_path and diar_path.exists())

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
            "has_diar_sidecar": has_diar_sidecar,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "campaigns": campaigns,
            "current_campaign_slug": current_campaign_slug,
            "highlight": _highlight(q),
        },
    )


@router.get("/{name}/download")
async def transcript_download(request: Request, name: str):
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    return FileResponse(
        path=str(md_path),
        media_type="text/markdown",
        filename=md_path.name,
    )


@router.post("/{name}/delete", response_class=HTMLResponse)
async def delete_transcript(request: Request, name: str) -> HTMLResponse:
    """Delete a transcript, its campaign/journal links, and its companion files."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    transcript_store.delete_transcript(md_path.stem, output_dir=md_path.parent)
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": "/transcripts"},
    )


@router.get("/{name}/edit", response_class=HTMLResponse)
async def transcript_edit(request: Request, name: str) -> HTMLResponse:
    """Edit page — shows each speaker block with an editable speaker field."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    content = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    from wisper_transcribe.formatter import parse_transcript_blocks
    blocks = parse_transcript_blocks(body)

    unique_speakers = list(dict.fromkeys(b["speaker"] for b in blocks if b["has_speaker"]))

    return templates.TemplateResponse(
        request,
        "transcript_edit.html",
        {
            "request": request,
            "name": name,
            "meta": meta,
            "blocks": blocks,
            "unique_speakers": unique_speakers,
        },
    )


@router.post("/{name}/edit", response_class=HTMLResponse)
async def transcript_edit_save(request: Request, name: str) -> HTMLResponse:
    """Save per-block speaker name changes."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    form = await request.form()

    updated_speakers: dict[int, str] = {}
    for key, value in form.multi_items():
        if key.startswith("speaker_"):
            try:
                idx = int(key[len("speaker_"):])
            except ValueError:
                continue
            speaker_val = str(value).strip()
            if speaker_val:
                updated_speakers[idx] = speaker_val

    if updated_speakers:
        from wisper_transcribe.formatter import rewrite_transcript_blocks
        content = md_path.read_text(encoding="utf-8")
        content = rewrite_transcript_blocks(content, updated_speakers)
        transcript_store.save_transcript(md_path, content)

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{quote(name)}"},
    )


@router.post("/{name}/fix-speaker", response_class=HTMLResponse)
async def fix_speaker(request: Request, name: str) -> HTMLResponse:
    """Rename a speaker in an existing transcript."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    form = await request.form()
    old_name = str(form.get("old_name", "")).strip()
    new_name = str(form.get("new_name", "")).strip()

    if old_name and new_name:
        from wisper_transcribe.formatter import update_speaker_names
        content = md_path.read_text(encoding="utf-8")
        content = update_speaker_names(content, old_name, new_name)
        transcript_store.save_transcript(md_path, content)

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{quote(name)}"},
    )


@router.post("/{name}/refine", response_class=HTMLResponse)
async def post_refine(request: Request, name: str) -> HTMLResponse:
    """Submit a vocabulary-refine LLM job for an existing transcript."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
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
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
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
async def summary_detail(request: Request, name: str, q: str = "") -> HTMLResponse:
    """Render the campaign-notes summary for a transcript."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")

    summary_path = _get_safe_content_path(name, ".summary.md")
    if not summary_path or not summary_path.exists():
        return HTMLResponse(content="Summary not found", status_code=404)

    content = summary_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _anchor_sections(_sanitize_html(_md.markdown(body, extensions=["nl2br"])))

    return templates.TemplateResponse(
        request,
        "summary_detail.html",
        {
            "request": request,
            "name": name,
            "meta": meta,
            "html_body": html_body,
            "title": meta.get("title", f"{name} — Campaign Notes"),
            "highlight": _highlight(q),
        },
    )


@router.get("/{name}/summary/download")
async def summary_download(request: Request, name: str):
    """Download the .summary.md sidecar file."""
    summary_path = _get_safe_content_path(name, ".summary.md")
    if not summary_path:
        return invalid_input_response("Invalid name")
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
    safe_name = _get_safe_content_path(name, ".md")
    if not safe_name:
        return invalid_input_response("Invalid name")

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


# ---------------------------------------------------------------------------
# Transcript-centric enrollment wizard
# ---------------------------------------------------------------------------

# Shares its logic with the job-based wizard via web/enroll_shared.py.
from wisper_transcribe.web.enroll_shared import (
    _load_diar_sidecar,
    apply_renames,
    build_legacy_label_map as _build_legacy_label_map,
    enrollable_labels,
    excerpt_candidates,
    resolve_current_names,
    template_current_names,
)


@router.get("/{name}/enroll", response_class=HTMLResponse)
async def transcript_enroll_form(request: Request, name: str) -> HTMLResponse:
    """Speaker enrollment wizard — transcript-centric, restart-safe."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    diar = _load_diar_sidecar(md_path)
    if not diar:
        return HTMLResponse(content="No enrollment data found for this transcript", status_code=404)

    # Derive speaker labels ordered by first appearance
    seen: dict[str, float] = {}
    for seg in diar.get("diarization_segments", []):
        if seg["speaker"] not in seen:
            seen[seg["speaker"]] = seg["start"]
    speakers = sorted(seen.keys(), key=lambda s: seen[s])

    # Clips are keyed by raw label; older transcripts keyed them by display
    # name, so map those back via first-appearance timestamps.
    out_dir = md_path.parent
    stem = md_path.stem

    legacy_label_map = _build_legacy_label_map(md_path, diar.get("diarization_segments", []))

    speaker_excerpts: dict[str, str] = {}
    speaker_excerpt_texts: dict[str, str] = {}
    for sp in speakers:
        for safe_label in excerpt_candidates(sp, legacy_label_map):
            clip = out_dir / f"{stem}_excerpt_{safe_label}.mp3"
            if clip.exists():
                speaker_excerpts[sp] = str(clip)
                break
        for safe_label in excerpt_candidates(sp, legacy_label_map):
            txt = out_dir / f"{stem}_excerpt_{safe_label}.txt"
            if txt.exists():
                try:
                    speaker_excerpt_texts[sp] = txt.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
                break

    from wisper_transcribe.speaker_manager import load_profiles

    # Warn before submit when some speakers can't be enrolled (no saved
    # embedding and the source audio is gone); renames still work.
    audio_missing = bool(enrollable_labels(diar, speakers)[1])

    return templates.TemplateResponse(
        request,
        "speaker_enroll.html",
        {
            "request": request,
            "form_action": f"/transcripts/{quote(name)}/enroll",
            "back_url": f"/transcripts/{quote(name)}",
            "excerpt_base_url": f"/transcripts/{quote(name)}/excerpt",
            "display_name": name,
            "detected_speakers": speakers,
            "existing_profiles": load_profiles(),
            "speaker_excerpts": speaker_excerpts,
            "speaker_excerpt_texts": speaker_excerpt_texts,
            # Prefill previously applied names; raw-label values are dropped so
            # untouched inputs start empty.
            "current_names": template_current_names(
                resolve_current_names(md_path, diar, diar.get("diarization_segments", []))
            ),
            "audio_missing": audio_missing,
        },
    )


@router.post("/{name}/enroll", response_class=HTMLResponse)
async def transcript_enroll_submit(request: Request, name: str) -> HTMLResponse:
    """Apply speaker name assignments from the transcript enrollment wizard."""
    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    diar = _load_diar_sidecar(md_path)
    if not diar:
        return HTMLResponse(content="No enrollment data found for this transcript", status_code=404)

    form_data = await request.form()
    renames: dict[str, str] = {}
    for key, value in form_data.multi_items():
        if key.startswith("speaker_") and str(value).strip():
            renames[key[len("speaker_"):]] = str(value).strip()

    if not renames:
        return HTMLResponse(
            content="", status_code=303,
            headers={"Location": f"/transcripts/{quote(name)}"},
        )

    # Rename now; embedding extraction runs as a JOB_ENROLL job.
    from wisper_transcribe.models import DiarizationSegment

    raw_segments = [
        DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
        for s in diar.get("diarization_segments", [])
    ]
    rename_result = apply_renames(md_path, raw_segments, renames)

    location = f"/transcripts/{quote(name)}"
    if not rename_result.groups:
        # Nothing eligible for enrollment (all skipped/unchanged/refused) --
        # the rename (if any) already happened; no job needed.
        return HTMLResponse(
            content="", status_code=303,
            headers={"Location": location},
        )

    # Enqueue when any group can be enrolled; flag the speakers that can't.
    submitted = [lb for labels in rename_result.groups.values() for lb in labels]
    enrollable, skipped = enrollable_labels(diar, submitted)
    if skipped:
        location += "?notice=enroll_audio_missing"
    if not any(all(lb in enrollable for lb in labels)
               for labels in rename_result.groups.values()):
        return HTMLResponse(
            content="", status_code=303,
            headers={"Location": location},
        )

    from wisper_transcribe.config import get_device, load_config
    from . import get_queue

    device = load_config().get("device", "auto")
    if device == "auto":
        device = get_device()

    queue = get_queue(request)
    job = queue.submit_enroll(
        md_path=str(md_path),
        transcript_name=name,
        groups=rename_result.groups,
        device=device,
    )
    job_location = f"/transcribe/jobs/{job.id}"
    if skipped:
        job_location += "?notice=enroll_audio_missing"
    return HTMLResponse(
        content="", status_code=303,
        headers={"Location": job_location},
    )


@router.get("/{name}/excerpt/{speaker_name}")
async def transcript_excerpt(request: Request, name: str, speaker_name: str):
    """Serve a speaker excerpt clip for the transcript-centric enrollment wizard."""
    if not speaker_name or "\x00" in speaker_name:
        return invalid_input_response("Invalid speaker name")
    safe_sp = os.path.basename(speaker_name)
    if safe_sp != speaker_name or safe_sp in {".", ".."}:
        return invalid_input_response("Invalid speaker name")

    md_path = _get_safe_content_path(name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")

    from fastapi.responses import FileResponse

    # Try the raw label first; fall back to the legacy display-name file
    # (older transcripts key excerpts by display name).
    candidates: list[str] = [safe_sp]
    diar = _load_diar_sidecar(md_path)
    if diar:
        legacy_label_map = _build_legacy_label_map(md_path, diar.get("diarization_segments", []))
        legacy = legacy_label_map.get(safe_sp)
        if legacy:
            candidates.append(legacy)

    # Shared lookup + CodeQL guard (also used by the job-based wizard).
    from wisper_transcribe.web.enroll_shared import find_excerpt_clip

    clip = find_excerpt_clip(md_path.parent, md_path.stem, candidates)
    if clip is not None:
        return FileResponse(path=str(clip), media_type="audio/mpeg")
    return HTMLResponse(content="Excerpt not available", status_code=404)
