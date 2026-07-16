"""Transcripts route — browse, view, and post-process markdown transcripts."""
from __future__ import annotations

import html as _html_module
import json
import logging
import os
from html.parser import HTMLParser
from pathlib import Path
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

from . import templates
from wisper_transcribe.path_utils import get_output_dir
from wisper_transcribe.web._responses import invalid_input_response

router = APIRouter(prefix="/transcripts")


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


def _get_safe_content_path(request: Request, name: str, suffix: str) -> Path | None:
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

    return Path(target_path)


def _delete_diar_sidecar_and_audio(request: Request, name: str) -> None:
    """Remove the ``_diar.json`` enrollment sidecar and the durable audio copy
    it points to, if any.

    F5 decision: the audio file living next to a transcript exists solely to
    back the enrollment wizard (it was moved there from the tempdir by
    ``_move_upload_to_output`` in ``web/jobs.py``). Once the transcript is
    deleted there's nothing left to enroll against, so leaving that audio
    file (and the now-dangling sidecar referencing it) behind would be a
    permanent leak -- the exact kind of leak F5 was fixing in the first
    place, just relocated from the tempdir to the output dir. Deleting both
    here keeps that promise. Excerpt clips (``*_excerpt_*.mp3``/``.txt``) are
    a separate concern handled by ``_delete_excerpt_clips`` (R9-4), called
    alongside this function from both delete routes.
    """
    diar_path = _get_safe_content_path(request, name, "_diar.json")
    if not diar_path or not diar_path.exists():
        return

    try:
        diar = json.loads(diar_path.read_text(encoding="utf-8"))
        stored_input_path = diar.get("input_path")
    except Exception:
        stored_input_path = None

    if stored_input_path:
        # Only delete the referenced audio if it actually resolves inside the
        # output dir -- i.e. it's the durable moved copy, not some external
        # user file (legacy sidecars from before the F5 fix may still point
        # at a tempdir path, which must never be touched here).
        out_dir = get_output_dir().resolve()
        base_dir = os.path.abspath(str(out_dir))
        if not base_dir.endswith(os.sep):
            base_dir += os.sep
        candidate_abs = os.path.abspath(stored_input_path)
        if candidate_abs.startswith(base_dir):
            try:
                Path(candidate_abs).unlink(missing_ok=True)
            except OSError:
                pass

    try:
        diar_path.unlink(missing_ok=True)
    except OSError:
        pass


def _delete_excerpt_clips(request: Request, name: str) -> None:
    """Delete ``<stem>_excerpt_*`` speaker-preview clips (``.mp3`` and
    ``.txt``) left behind by transcription/enrollment (R9-4).

    ``_get_safe_content_path`` already applies the os.path.basename +
    abspath/startswith guard (CLAUDE.md security note) and returns a path
    guaranteed to live inside the output dir, so the glob results derived
    from it -- always children of that same directory -- need no further
    per-file *path-traversal* check. ``md_path.stem`` is still untrusted
    text, though (a transcript can be named e.g. ``mix*``), so it must be
    ``glob.escape()``-d before being embedded in the pattern -- otherwise
    a stem containing ``*``/``?``/``[`` would turn this into a wildcard
    match against *every* transcript's excerpt clips instead of just this
    one's, deleting other transcripts' clips on a single delete.
    """
    import glob as _glob

    md_path = _get_safe_content_path(request, name, ".md")
    if md_path is None:
        return
    out_dir = md_path.parent
    for clip in out_dir.glob(f"{_glob.escape(md_path.stem)}_excerpt_*"):
        try:
            clip.unlink(missing_ok=True)
        except OSError:
            pass


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


@router.get("", response_class=HTMLResponse)
async def transcripts_list(request: Request) -> HTMLResponse:
    out_dir = get_output_dir()
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


@router.post("/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_transcripts(request: Request) -> HTMLResponse:
    """Delete multiple transcripts (and their summary sidecars) in one request."""
    form = await request.form()
    stems = form.getlist("stems")
    for stem in stems:
        md_path = _get_safe_content_path(request, stem, ".md")
        if md_path and md_path.exists():
            md_path.unlink()
        summary = _get_safe_content_path(request, stem, ".summary.md")
        if summary and summary.exists():
            summary.unlink()
        _delete_diar_sidecar_and_audio(request, stem)
        # R9-4: also remove <stem>_excerpt_*.mp3/.txt clips.
        _delete_excerpt_clips(request, stem)
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
        path = _get_safe_content_path(request, stem, ".md")
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


@router.get("/{name}", response_class=HTMLResponse)
async def transcript_detail(request: Request, name: str) -> HTMLResponse:
    md_path = _get_safe_content_path(request, name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    content = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(content)

    import markdown as _md
    html_body = _sanitize_html(_md.markdown(body, extensions=["nl2br"]))

    # Check for summary sidecar
    summary_path = _get_safe_content_path(request, name, ".summary.md")
    has_summary = bool(summary_path and summary_path.exists())

    # Check for enrollment sidecar (transcript-centric wizard)
    diar_path = _get_safe_content_path(request, name, "_diar.json")
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
        },
    )


@router.get("/{name}/download")
async def transcript_download(request: Request, name: str):
    md_path = _get_safe_content_path(request, name, ".md")
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
    """Delete a transcript .md file (and its summary sidecar if present)."""
    md_path = _get_safe_content_path(request, name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if md_path.exists():
        md_path.unlink()
    # Also remove summary sidecar if present
    summary_path = _get_safe_content_path(request, name, ".summary.md")
    if summary_path and summary_path.exists():
        summary_path.unlink()
    # F5: also remove the enrollment sidecar and the durable audio copy it
    # references -- see _delete_diar_sidecar_and_audio for the reasoning.
    _delete_diar_sidecar_and_audio(request, name)
    # R9-4: also remove <stem>_excerpt_*.mp3/.txt clips left behind by
    # transcription/enrollment -- previously left orphaned on delete.
    _delete_excerpt_clips(request, name)
    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": "/transcripts"},
    )


@router.get("/{name}/edit", response_class=HTMLResponse)
async def transcript_edit(request: Request, name: str) -> HTMLResponse:
    """Edit page — shows each speaker block with an editable speaker field."""
    md_path = _get_safe_content_path(request, name, ".md")
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
    md_path = _get_safe_content_path(request, name, ".md")
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
        md_path.write_text(content, encoding="utf-8")

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{quote(name)}"},
    )


@router.post("/{name}/fix-speaker", response_class=HTMLResponse)
async def fix_speaker(request: Request, name: str) -> HTMLResponse:
    """Rename a speaker in an existing transcript."""
    md_path = _get_safe_content_path(request, name, ".md")
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
        md_path.write_text(content, encoding="utf-8")

    return HTMLResponse(
        content="",
        status_code=303,
        headers={"Location": f"/transcripts/{quote(name)}"},
    )


@router.post("/{name}/refine", response_class=HTMLResponse)
async def post_refine(request: Request, name: str) -> HTMLResponse:
    """Submit a vocabulary-refine LLM job for an existing transcript."""
    md_path = _get_safe_content_path(request, name, ".md")
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
    md_path = _get_safe_content_path(request, name, ".md")
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
async def summary_detail(request: Request, name: str) -> HTMLResponse:
    """Render the campaign-notes summary for a transcript."""
    md_path = _get_safe_content_path(request, name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")

    summary_path = _get_safe_content_path(request, name, ".summary.md")
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
    summary_path = _get_safe_content_path(request, name, ".summary.md")
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
    safe_name = _get_safe_content_path(request, name, ".md")
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

# Shared with web/routes/transcribe.py (the legacy job-centric wizard) via
# wisper_transcribe.web.enroll_shared -- see that module for the rationale
# behind the interval-matching approach and the F1/F2/F3 fixes.
from wisper_transcribe.web.enroll_shared import (
    _load_diar_sidecar,
    apply_renames,
    build_legacy_label_map as _build_legacy_label_map,
    resolve_current_names,
    template_current_names,
)


@router.get("/{name}/enroll", response_class=HTMLResponse)
async def transcript_enroll_form(request: Request, name: str) -> HTMLResponse:
    """Speaker enrollment wizard — transcript-centric, restart-safe."""
    md_path = _get_safe_content_path(request, name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")
    if not md_path.exists():
        return HTMLResponse(content="Transcript not found", status_code=404)

    diar = _load_diar_sidecar(md_path)
    if not diar:
        return HTMLResponse(content="No enrollment data found for this transcript", status_code=404)

    # Derive speaker labels ordered by first appearance
    import re as _re
    seen: dict[str, float] = {}
    for seg in diar.get("diarization_segments", []):
        if seg["speaker"] not in seen:
            seen[seg["speaker"]] = seg["start"]
    speakers = sorted(seen.keys(), key=lambda s: seen[s])

    # Locate on-disk excerpt clips and text snippets. New transcripts have
    # files keyed by the raw pyannote label (matches what's in the sidecar).
    # Legacy transcripts (pre-fix) keyed files by the *display* name from
    # the rendered markdown — backfill that mapping by parsing the markdown
    # and matching first-appearance timestamps so the wizard still works
    # without re-transcribing.
    out_dir = md_path.parent
    stem = md_path.stem

    legacy_label_map = _build_legacy_label_map(md_path, diar.get("diarization_segments", []))

    def _excerpt_candidates(raw_label: str) -> list[str]:
        cands = [_re.sub(r"[^\w\-]", "_", raw_label)]
        legacy = legacy_label_map.get(raw_label)
        if legacy:
            cands.append(_re.sub(r"[^\w\-]", "_", legacy))
        return cands

    speaker_excerpts: dict[str, str] = {}
    speaker_excerpt_texts: dict[str, str] = {}
    for sp in speakers:
        for safe_label in _excerpt_candidates(sp):
            clip = out_dir / f"{stem}_excerpt_{safe_label}.mp3"
            if clip.exists():
                speaker_excerpts[sp] = str(clip)
                break
        for safe_label in _excerpt_candidates(sp):
            txt = out_dir / f"{stem}_excerpt_{safe_label}.txt"
            if txt.exists():
                try:
                    speaker_excerpt_texts[sp] = txt.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
                break

    from wisper_transcribe.speaker_manager import load_profiles

    # F5: warn before submission when the sidecar's recorded input_path is
    # gone (e.g. transcript predates the move-to-output fix, or the move
    # failed) rather than letting the wizard silently rename-only on submit.
    diar_input_path = diar.get("input_path", "")
    audio_missing = not (diar_input_path and Path(diar_input_path).exists())

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
            # raw_label -> current display name in the transcript. F7:
            # resolved from the sidecar's authoritative speaker_map when
            # present, falling back to the (fragile) interval-matching
            # heuristic only for legacy sidecars that predate that key --
            # see resolve_current_names. Lets the wizard pre-fill names the
            # user previously applied so they can edit corrections instead
            # of re-typing from scratch. Raw-label-valued entries (first
            # pass, no renames applied yet) are filtered out so the input
            # starts empty rather than prefilled with "SPEAKER_00" (F2) --
            # see template_current_names.
            "current_names": template_current_names(
                resolve_current_names(md_path, diar, diar.get("diarization_segments", []))
            ),
            "audio_missing": audio_missing,
        },
    )


@router.post("/{name}/enroll", response_class=HTMLResponse)
async def transcript_enroll_submit(request: Request, name: str) -> HTMLResponse:
    """Apply speaker name assignments from the transcript enrollment wizard."""
    md_path = _get_safe_content_path(request, name, ".md")
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

    # Rename synchronously (fast) via the shared handler (unifies this path
    # with the legacy job-centric wizard in transcribe.py). See
    # enroll_shared.apply_renames for the current-name resolution (F1) and
    # raw-label refusal (F2) logic. The slow half (WAV convert + embedding
    # extraction, F3's EMA-merge logic) now runs in a JOB_ENROLL job instead
    # of blocking this request (Phase 2.5) -- see enroll_shared.enroll_profiles.
    from wisper_transcribe.models import DiarizationSegment

    raw_segments = [
        DiarizationSegment(start=s["start"], end=s["end"], speaker=s["speaker"])
        for s in diar.get("diarization_segments", [])
    ]
    diar_input_path = diar.get("input_path", "")
    input_path = Path(diar_input_path) if diar_input_path else None

    rename_result = apply_renames(md_path, raw_segments, renames)

    location = f"/transcripts/{quote(name)}"
    if not rename_result.groups:
        # Nothing eligible for enrollment (all skipped/unchanged/refused) --
        # the rename (if any) already happened; no job needed.
        return HTMLResponse(
            content="", status_code=303,
            headers={"Location": location},
        )

    # F5: only enqueue a job when there's actually something eligible to
    # enroll AND the source audio is known to exist. A pre-check here (as
    # opposed to inside the job) avoids enqueueing a job that can only ever
    # fail, and keeps the existing "notice" UX exactly as before.
    if input_path is None or not input_path.exists():
        log.warning("Enrollment skipped: source audio not found at %s", input_path)
        location += "?notice=enroll_audio_missing"
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
    return HTMLResponse(
        content="", status_code=303,
        headers={"Location": f"/transcribe/jobs/{job.id}"},
    )


@router.get("/{name}/excerpt/{speaker_name}")
async def transcript_excerpt(request: Request, name: str, speaker_name: str):
    """Serve a speaker excerpt clip for the transcript-centric enrollment wizard."""
    import re as _re
    if not speaker_name or "\x00" in speaker_name:
        return invalid_input_response("Invalid speaker name")
    safe_sp = os.path.basename(speaker_name)
    if safe_sp != speaker_name or safe_sp in {".", ".."}:
        return invalid_input_response("Invalid speaker name")

    md_path = _get_safe_content_path(request, name, ".md")
    if not md_path:
        return invalid_input_response("Invalid name")

    from fastapi.responses import FileResponse
    out_dir = md_path.parent
    stem = md_path.stem

    # Try the raw label first; fall back to the legacy display-name file
    # (pre-fix transcripts keyed excerpts by display name).
    candidates: list[str] = [_re.sub(r"[^\w\-]", "_", safe_sp)]
    diar = _load_diar_sidecar(md_path)
    if diar:
        legacy_label_map = _build_legacy_label_map(md_path, diar.get("diarization_segments", []))
        legacy = legacy_label_map.get(safe_sp)
        if legacy:
            candidates.append(_re.sub(r"[^\w\-]", "_", legacy))

    # Path-traversal guard: re.sub(r"[^\w\-]", "_", …) is already a tight
    # whitelist, but CodeQL's taint tracker only recognises the
    # os.path.abspath + startswith pattern (CLAUDE.md security note).
    base_dir = os.path.abspath(str(out_dir))
    if not base_dir.endswith(os.sep):
        base_dir += os.sep
    for safe_label in candidates:
        candidate_abs = os.path.abspath(
            os.path.join(base_dir, f"{stem}_excerpt_{safe_label}.mp3")
        )
        if not candidate_abs.startswith(base_dir):
            continue
        clip = Path(candidate_abs)
        if clip.exists():
            return FileResponse(path=str(clip), media_type="audio/mpeg")
    return HTMLResponse(content="Excerpt not available", status_code=404)
