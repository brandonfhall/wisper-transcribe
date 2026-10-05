"""Transcripts route — browse, view, and post-process markdown transcripts."""
from __future__ import annotations

import html as _html_module
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from wisper_transcribe.campaign_manager import (
    _validate_campaign_slug,
    get_campaign_for_transcript,
    load_campaigns,
    move_transcript_to_campaign,
    remove_transcript_from_campaign,
)
from wisper_transcribe.config import get_data_dir, get_output_root
from wisper_transcribe import db as _db, file_registry
from wisper_transcribe.recording_manager import load_recordings, recording_for_transcript

from . import templates
from wisper_transcribe import transcript_store
from wisper_transcribe.web._responses import invalid_input_response

router = APIRouter(prefix="/transcripts")


db_connection = _db.connection


def _as_basename(value: object) -> Optional[str]:
    """A single path component, or None when it names a path or is empty."""
    name = str(value)
    if not name or "\x00" in name or "\\" in name or ":" in name:
        return None
    if os.path.basename(name) != name or name in {".", ".."}:
        return None
    return name


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
    numbering the search index stores. A block with a timestamp also gets
    ``data-start`` (seconds) for audio playback."""
    from wisper_transcribe.formatter import searchable_blocks
    from wisper_transcribe.time_utils import parse_timestamp

    lines = body.splitlines()
    for lineno, block in searchable_blocks(body):
        start = parse_timestamp(block["timestamp"])
        timing = "" if start is None else f' data-start="{start:g}"'
        lines[lineno] = (f'<span id="b-{block["index"]}" class="block-anchor"{timing}>'
                         f'{lines[lineno].strip()}</span>')
    return "\n".join(lines)


_AUDIO_TYPES = {".wav": "audio/wav", ".flac": "audio/flac"}


def _playback(loc: transcript_store.Located) -> tuple[str | None, list[dict]]:
    """The audio URL for the player (None without audio) and, for a transcript
    made from a recording, that recording's markers as ``{label, seconds}``."""
    if transcript_store.audio_path(loc.md, output_dir=get_output_root()) is None:
        return None, []
    audio_url = f"/transcripts/{loc.id}/audio"
    rec = recording_for_transcript(loc.id)
    markers = []
    for marker in (rec.markers if rec else []):
        secs = int(marker.elapsed_s)
        markers.append({"label": f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}",
                        "seconds": secs})
    return audio_url, markers


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


def _located_or_redirect(transcript_id: int) -> "transcript_store.Located | RedirectResponse":
    """The :class:`Located` for ``transcript_id``, or a 303 to the list page.

    A row that doesn't exist redirects; a row flagged missing is returned so
    the caller renders its missing state (the ``.md`` is gone).
    """
    loc = transcript_store.locate(transcript_id)
    if loc is None:
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)
    return loc


def _form_transcript_id(value: object) -> Optional[int]:
    """A transcript id posted in a form, or None when it isn't a whole number."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_transcript_ids(values: list) -> list[int]:
    """The valid transcript ids among ``values``; malformed entries are dropped."""
    ids = []
    for value in values:
        tid = _form_transcript_id(value)
        if tid is not None:
            ids.append(tid)
    return ids


@router.get("/partials/recent", response_class=HTMLResponse)
async def recent_transcripts_partial(request: Request) -> HTMLResponse:
    """HTMX partial: 6 most recent transcripts for the dashboard archive section."""
    found = transcript_store.list_transcripts()[:6]
    items = []
    for loc in found:
        meta, _ = _parse_frontmatter(loc.md.read_text(encoding="utf-8"))
        items.append({
            "id": loc.id,
            "stem": loc.stem,
            "title": meta.get("title", loc.stem),
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
    out_dir = get_output_root()
    transcript_store.reconcile(out_dir, sync="throttled", blocking=False)

    campaigns = load_campaigns()
    campaign_slug_by_id = {c.id: slug for slug, c in campaigns.items()}

    items = []
    for loc in transcript_store.list_transcripts(output_dir=out_dir):
        meta, _ = _parse_frontmatter(loc.md.read_text(encoding="utf-8"))
        items.append({
            "id": loc.id,
            "stem": loc.stem,
            "name": loc.md.name,
            "title": meta.get("title", loc.stem),
            "date_processed": meta.get("date_processed", ""),
            "duration": meta.get("duration", ""),
            "speakers": meta.get("speakers", []),
            "has_summary": loc.companion(".summary.md").exists(),
            "campaign_slug": campaign_slug_by_id.get(loc.campaign_id),
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
            "pending_recordings": pending_recordings,
            "live_draft_ids": live_draft_ids,
            "attention": attention,
            "relink_candidates": transcript_store.relink_candidates() if attention else [],
        },
    )


def _attention_context(out_dir: "Path | None") -> Optional[dict]:
    """The Needs attention panel's data, or None when there is nothing to show."""
    if out_dir is None:
        out_dir = get_output_root()
    try:
        found = transcript_store.needs_attention(out_dir)
    except Exception:
        log.warning("Could not list items needing attention", exc_info=True)
        return None
    if not found.total:
        return None
    base = os.path.abspath(str(out_dir))
    unclaimed = []
    for path in found.unclaimed:
        try:
            st = path.stat()
        except OSError:
            continue
        rel = os.path.relpath(os.path.abspath(path), base)
        in_output = os.sep not in rel
        # Only a scanned directory's file with a recognised companion name may
        # be deleted here; anything else is listed only.
        deletable = (transcript_store._companion_stem(path.name) is not None
                     and (in_output or rel.split(os.sep)[0] in _campaign_folder_names()))
        unclaimed.append({
            "name": path.name if in_output else rel.replace(os.sep, "/"),
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "deletable": deletable,
        })
    campaigns = load_campaigns()
    campaign_name = {c.id: c.display_name for c in campaigns.values()}
    misplaced = []
    for loc in found.misplaced:
        try:
            where = os.path.relpath(os.path.abspath(loc.dir), base).replace(os.sep, "/")
        except ValueError:
            where = str(loc.dir)
        misplaced.append({
            "id": loc.id,
            "stem": loc.stem,
            "campaign": campaign_name.get(loc.campaign_id),
            "dir": where,
        })
    return {
        "missing_transcripts": found.missing_transcripts,
        "missing_files": [
            {"id": r.id, "name": r.rel_path, "kind": r.kind} for r in found.missing_files
        ],
        "unclaimed": unclaimed,
        "misplaced": misplaced,
        "pending_folders": found.pending_folders,
        "folder_taken": found.folder_taken,
        "legacy_journals": found.legacy_journals,
        "missing_folders": found.missing_folders,
    }


def _campaign_folder_names() -> set[str]:
    """Current campaign folder names, for validating a two-part delete path."""
    return {c.folder for c in load_campaigns().values()}


@router.post("/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_transcripts(request: Request) -> HTMLResponse:
    """Delete multiple transcripts (and their summary sidecars) in one request."""
    form = await request.form()
    for tid in _parse_transcript_ids(form.getlist("transcript_id")):
        transcript_store.delete_transcript(tid)
    return HTMLResponse(content="", status_code=303, headers={"Location": "/transcripts"})


@router.post("/bulk-campaign", response_class=HTMLResponse)
async def bulk_assign_campaign(request: Request) -> HTMLResponse:
    """Assign or remove a campaign for multiple transcripts in one request."""
    form = await request.form()
    campaign = str(form.get("campaign", "")).strip()

    safe_slug: "Optional[str]" = None
    if campaign:
        safe_slug = _validate_campaign_slug(campaign)
        if safe_slug is None:
            return HTMLResponse(
                content="", status_code=303,
                headers={"Location": "/transcripts?error=invalid_campaign"},
            )

    for tid in _parse_transcript_ids(form.getlist("transcript_id")):
        try:
            if safe_slug:
                move_transcript_to_campaign(tid, safe_slug)
            else:
                remove_transcript_from_campaign(tid)
        except Exception:
            pass

    return HTMLResponse(content="", status_code=303, headers={"Location": "/transcripts"})


@router.post("/relink", response_class=HTMLResponse)
async def relink_transcript(request: Request) -> RedirectResponse:
    """Give a missing transcript's identity (and companion files) to a renamed file."""
    form = await request.form()
    old_id = _form_transcript_id(form.get("old_id"))
    new_id = _form_transcript_id(form.get("new_id"))
    if old_id is None or new_id is None:
        return RedirectResponse(url="/transcripts?error=relink_failed", status_code=303)
    new_loc = transcript_store.locate(new_id)
    if new_loc is None:
        return RedirectResponse(url="/transcripts?error=relink_failed", status_code=303)
    try:
        kept = transcript_store.relink(old_id, new_loc.md)
    except (KeyError, ValueError):
        return RedirectResponse(url="/transcripts?error=relink_failed", status_code=303)
    notice = "?notice=relink_kept_names" if kept else ""
    return RedirectResponse(url=f"/transcripts{notice}", status_code=303)


@router.post("/needs-attention/forget", response_class=HTMLResponse)
async def forget_missing_file(request: Request) -> HTMLResponse:
    """Drop the record of a registered file that is gone from disk."""
    form = await request.form()
    try:
        file_id = int(str(form.get("file_id", "")))
    except ValueError:
        return invalid_input_response("Invalid file id")
    out_dir = get_output_root()
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
    """Delete an unowned file with a companion-file name in a scanned directory.

    ``name`` is ``<file>`` or ``<folder>/<file>``; each component is a plain
    name (no separator, colon, or NUL), and ``<folder>`` must be a current
    campaign's folder. ``delete_unowned_file`` re-checks the directory.
    """
    form = await request.form()
    parts = str(form.get("name", "")).split("/")
    if len(parts) == 1:
        safe_name = _as_basename(parts[0])
        folder = None
    elif len(parts) == 2:
        folder, safe_name = _as_basename(parts[0]), _as_basename(parts[1])
        if folder is None or folder not in _campaign_folder_names():
            return invalid_input_response("Invalid file name")
    else:
        return invalid_input_response("Invalid file name")
    if safe_name is None or transcript_store._companion_stem(safe_name) is None:
        return invalid_input_response("Invalid file name")
    out_dir = get_output_root()
    target = out_dir / (safe_name if folder is None else f"{folder}/{safe_name}")
    if not transcript_store.delete_unowned_file(Path(target), output_dir=out_dir):
        return RedirectResponse(url="/transcripts?error=delete_failed", status_code=303)
    return RedirectResponse(url="/transcripts", status_code=303)


@router.post("/needs-attention/claim-folder", response_class=HTMLResponse)
async def claim_taken_folder(request: Request) -> HTMLResponse:
    """Claim an existing non-empty folder as the campaign's, for "Use this folder".

    Only a folder-taken campaign qualifies: it exists, wisper doesn't own it,
    and no rename is pending. The next reconcile registers its `.md` files.
    """
    from wisper_transcribe import campaign_folders
    from wisper_transcribe.config import get_output_root

    form = await request.form()
    try:
        campaign_id = int(str(form.get("campaign_id", "")))
    except ValueError:
        return invalid_input_response("Invalid campaign id")
    with db_connection() as conn:
        row = conn.execute(
            "SELECT folder, folder_pending, folder_claimed FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
    if row is None or row["folder_pending"] is not None or row["folder_claimed"]:
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)
    folder = get_output_root() / row["folder"]
    if not folder.is_dir() or campaign_folders.holds_only_wisper(folder, row["folder"]):
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)
    try:
        campaign_folders.claim_folder(campaign_id)
    except (KeyError, campaign_folders.FolderPendingError):
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)
    return RedirectResponse(url="/transcripts", status_code=303)


@router.post("/needs-attention/recreate-folder", response_class=HTMLResponse)
async def recreate_missing_folder(request: Request) -> HTMLResponse:
    """Make a claimed campaign's vanished folder again, for "Recreate folder"."""
    from wisper_transcribe import campaign_folders

    form = await request.form()
    try:
        campaign_id = int(str(form.get("campaign_id", "")))
    except ValueError:
        return invalid_input_response("Invalid campaign id")
    try:
        campaign_folders.recreate_folder(campaign_id)
    except KeyError:
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)
    except campaign_folders.FolderTakenError:
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)
    return RedirectResponse(url="/transcripts", status_code=303)


@router.get("/{transcript_id:int}", response_class=HTMLResponse)
async def transcript_detail(request: Request, transcript_id: int, q: str = "") -> Response:
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

    content = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _sanitize_html(_md.markdown(_anchor_blocks(body), extensions=["nl2br"]))

    # Check for summary sidecar
    summary_path = loc.companion(".summary.md")
    has_summary = summary_path.exists()

    # Check for enrollment sidecar (transcript-centric wizard)
    diar_path = loc.companion(transcript_store.SIDECAR_SUFFIX)
    has_diar_sidecar = diar_path.exists()

    # Load current LLM config for display
    from wisper_transcribe.config import load_config
    cfg = load_config()
    llm_provider = cfg.get("llm_provider", "ollama") or "ollama"
    llm_model = cfg.get("llm_model", "") or ""

    campaigns = load_campaigns()
    current_campaign_slug = get_campaign_for_transcript(loc.id)
    audio_url, markers = _playback(loc)

    return templates.TemplateResponse(
        request,
        "transcript_detail.html",
        {
            "request": request,
            "id": loc.id,
            "name": loc.stem,
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
            "audio_url": audio_url,
            "markers": markers,
            "has_timing": "data-start=" in html_body,
        },
    )


@router.get("/{transcript_id:int}/audio")
async def transcript_audio(transcript_id: int):
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Not found", status_code=404)
    audio = transcript_store.audio_path(loc.md, output_dir=get_output_root())
    if audio is None:
        return HTMLResponse(content="No audio", status_code=404)
    target = os.path.abspath(str(audio))
    roots = [os.path.abspath(str(get_output_root())),
             os.path.abspath(str(get_data_dir() / "recordings"))]
    if not any(target.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
        return HTMLResponse(content="Not found", status_code=404)
    suffix = os.path.splitext(target)[1].lower()
    media_type = _AUDIO_TYPES.get(suffix) or mimetypes.guess_type(target)[0]
    if not media_type or not os.path.isfile(target):
        return HTMLResponse(content="Not found", status_code=404)
    return FileResponse(target, media_type=media_type)


@router.get("/{transcript_id:int}/download")
async def transcript_download(request: Request, transcript_id: int):
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    return FileResponse(
        path=str(loc.md),
        media_type="text/markdown",
        filename=loc.md.name,
    )


@router.post("/{transcript_id:int}/delete", response_class=HTMLResponse)
async def delete_transcript(request: Request, transcript_id: int) -> HTMLResponse:
    """Delete a transcript, its campaign/journal links, and its companion files."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    transcript_store.delete_transcript(loc.id)
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": "/transcripts"},
    )


@router.get("/{transcript_id:int}/edit", response_class=HTMLResponse)
async def transcript_edit(request: Request, transcript_id: int) -> Response:
    """Edit page — shows each speaker block with an editable speaker field."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

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
            "id": loc.id,
            "name": loc.stem,
            "meta": meta,
            "blocks": blocks,
            "unique_speakers": unique_speakers,
        },
    )


@router.post("/{transcript_id:int}/edit", response_class=HTMLResponse)
async def transcript_edit_save(request: Request, transcript_id: int) -> Response:
    """Save per-block speaker name changes."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

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
        headers={"Location": f"/transcripts/{loc.id}"},
    )


@router.post("/{transcript_id:int}/fix-speaker", response_class=HTMLResponse)
async def fix_speaker(request: Request, transcript_id: int) -> Response:
    """Rename a speaker in an existing transcript."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

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
        headers={"Location": f"/transcripts/{loc.id}"},
    )


@router.post("/{transcript_id:int}/refine", response_class=HTMLResponse)
async def post_refine(request: Request, transcript_id: int) -> Response:
    """Submit a vocabulary-refine LLM job for an existing transcript."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

    from . import get_queue
    from ..jobs import JOB_REFINE

    queue = get_queue(request)
    job = queue.submit_llm(
        transcript_path=str(md_path),
        job_type=JOB_REFINE,
        name=loc.stem,
    )
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcribe/jobs/{job.id}"},
    )


@router.post("/{transcript_id:int}/summarize", response_class=HTMLResponse)
async def post_summarize(request: Request, transcript_id: int) -> Response:
    """Submit a campaign-summary LLM job for an existing transcript."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

    from . import get_queue
    from ..jobs import JOB_SUMMARIZE

    queue = get_queue(request)
    job = queue.submit_llm(
        transcript_path=str(md_path),
        job_type=JOB_SUMMARIZE,
        name=loc.stem,
    )
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcribe/jobs/{job.id}"},
    )


@router.get("/{transcript_id:int}/summary", response_class=HTMLResponse)
async def summary_detail(request: Request, transcript_id: int, q: str = "") -> Response:
    """Render the campaign-notes summary for a transcript."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc

    summary_path = loc.companion(".summary.md")
    if not summary_path.exists():
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
            "id": loc.id,
            "name": loc.stem,
            "meta": meta,
            "html_body": html_body,
            "title": meta.get("title", f"{loc.stem} — Campaign Notes"),
            "highlight": _highlight(q),
        },
    )


@router.get("/{transcript_id:int}/summary/download")
async def summary_download(request: Request, transcript_id: int):
    """Download the .summary.md sidecar file."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    summary_path = loc.companion(".summary.md")
    if not summary_path.exists():
        return HTMLResponse(content="Summary not found", status_code=404)
    return FileResponse(
        path=str(summary_path),
        media_type="text/markdown",
        filename=summary_path.name,
    )


@router.post("/{transcript_id:int}/campaign", response_class=HTMLResponse)
async def assign_campaign(request: Request, transcript_id: int) -> Response:
    """Assign or remove a campaign association for a transcript."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc

    form = await request.form()
    campaign_slug = str(form.get("campaign", "")).strip()

    if campaign_slug:
        safe_slug = _validate_campaign_slug(campaign_slug)
        if safe_slug is None:
            return HTMLResponse(
                content="",
                status_code=303,
                headers={"Location": f"/transcripts/{loc.id}?error=invalid_campaign"},
            )
        try:
            move_transcript_to_campaign(loc.id, safe_slug)
        except KeyError:
            return HTMLResponse(
                content="",
                status_code=303,
                headers={"Location": f"/transcripts/{loc.id}?error=not_found"},
            )
        except ValueError:  # a session with that name is already in the campaign
            return HTMLResponse(
                content="",
                status_code=303,
                headers={"Location": f"/transcripts/{loc.id}?error=move_failed"},
            )
    else:
        try:
            remove_transcript_from_campaign(loc.id)
        except ValueError:  # an unassigned session already has this name
            return HTMLResponse(
                content="",
                status_code=303,
                headers={"Location": f"/transcripts/{loc.id}?error=move_failed"},
            )

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{loc.id}"},
    )


_RETRANSCRIBE_ERRORS = {"no_audio", "not_ready"}


def _back_to_transcript(loc: transcript_store.Located, error: str) -> HTMLResponse:
    """303 to the transcript page with a fixed error code (a Location header, as the other routes do)."""
    code = error if error in _RETRANSCRIBE_ERRORS else "not_ready"
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{loc.id}?error={code}"},
    )


@router.post("/{transcript_id:int}/retranscribe")
async def retranscribe(request: Request, transcript_id: int):
    """Re-run a transcript from its saved audio, replacing it in place."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md
    out_dir = get_output_root()
    rec = recording_for_transcript(loc.id)
    if rec is not None:
        from wisper_transcribe.web.routes.record import _submit_recording_transcription

        job, error = _submit_recording_transcription(rec, request, get_data_dir())
        if job is None:
            return _back_to_transcript(loc, error or "not_ready")
        return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)

    audio = transcript_store.audio_path(md_path, output_dir=out_dir)
    if audio is None:
        return _back_to_transcript(loc, "no_audio")
    from wisper_transcribe.job_history import last_transcription_params

    meta, _ = _parse_frontmatter(md_path.read_text(encoding="utf-8"))
    kwargs = last_transcription_params(loc.id)
    if meta.get("title"):
        kwargs["title"] = str(meta["title"])
    campaign = get_campaign_for_transcript(loc.id)
    if campaign:
        kwargs["campaign"] = campaign
    job = request.app.state.job_queue.submit(
        str(audio),
        original_stem=loc.stem,
        output_dir=str(loc.dir),
        source_name=str(meta.get("source_file") or audio.name),
        overwrite=True,
        **kwargs,
    )
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


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


@router.get("/{transcript_id:int}/enroll", response_class=HTMLResponse)
async def transcript_enroll_form(request: Request, transcript_id: int) -> Response:
    """Speaker enrollment wizard — transcript-centric, restart-safe."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

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
    out_dir = loc.dir
    stem = loc.stem

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
            "form_action": f"/transcripts/{loc.id}/enroll",
            "back_url": f"/transcripts/{loc.id}",
            "excerpt_base_url": f"/transcripts/{loc.id}/excerpt",
            "display_name": loc.stem,
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


@router.post("/{transcript_id:int}/enroll", response_class=HTMLResponse)
async def transcript_enroll_submit(request: Request, transcript_id: int) -> Response:
    """Apply speaker name assignments from the transcript enrollment wizard."""
    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    if not loc.md.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)
    md_path = loc.md

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
            headers={"Location": f"/transcripts/{loc.id}"},
        )

    # Rename now; embedding extraction runs as a JOB_ENROLL job.
    from wisper_transcribe.models import DiarizationSegment

    raw_segments = [
        DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
        for s in diar.get("diarization_segments", [])
    ]
    rename_result = apply_renames(md_path, raw_segments, renames)

    location = f"/transcripts/{loc.id}"
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
        transcript_name=loc.stem,
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


@router.get("/{transcript_id:int}/excerpt/{speaker_name}")
async def transcript_excerpt(request: Request, transcript_id: int, speaker_name: str):
    """Serve a speaker excerpt clip for the transcript-centric enrollment wizard."""
    if not speaker_name or "\x00" in speaker_name:
        return invalid_input_response("Invalid speaker name")
    safe_sp = os.path.basename(speaker_name)
    if safe_sp != speaker_name or safe_sp in {".", ".."}:
        return invalid_input_response("Invalid speaker name")

    loc = _located_or_redirect(transcript_id)
    if isinstance(loc, RedirectResponse):
        return loc
    md_path = loc.md

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

    clip = find_excerpt_clip(loc.dir, loc.stem, candidates)
    if clip is not None:
        return FileResponse(path=str(clip), media_type="audio/mpeg")
    return HTMLResponse(content="Excerpt not available", status_code=404)


# ---------------------------------------------------------------------------
# Legacy name URLs
# ---------------------------------------------------------------------------

@router.get("/{name}", response_class=HTMLResponse)
async def transcript_legacy(request: Request, name: str) -> Response:
    """Redirect an old ``/transcripts/{name}`` link to the id-based URL.

    Declared after every static and integer route, so a numeric segment never
    reaches here. The name is only ever a bound SQL parameter: it is never
    turned into a path. Exactly one match redirects; several offer a chooser
    (the same name may exist in two campaigns); none redirects with an error.
    """
    matches = transcript_store.find_by_stem(name, present_only=True)
    if len(matches) == 1:
        return RedirectResponse(url=f"/transcripts/{matches[0].id}", status_code=303)
    if not matches:
        return RedirectResponse(url="/transcripts?error=not_found", status_code=303)

    items = []
    campaigns = load_campaigns()
    for loc in matches:
        slug = get_campaign_for_transcript(loc.id)
        campaign = campaigns.get(slug).display_name if slug in campaigns else None
        items.append(
            f'<li><a href="/transcripts/{loc.id}">{_html_module.escape(loc.stem)}</a>'
            f'{" — " + _html_module.escape(campaign) if campaign else ""}</li>'
        )
    body = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
        f"<title>Choose a transcript — wisper</title></head><body>"
        f"<p>More than one transcript is named <strong>{_html_module.escape(matches[0].stem)}</strong>.</p>"
        f"<ul>{''.join(items)}</ul>"
        "<p><a href=\"/transcripts\">Back to transcripts</a></p>"
        "</body></html>"
    )
    return HTMLResponse(content=body)
