"""Campaigns route — manage per-campaign speaker rosters."""
from __future__ import annotations

import re
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import get_queue, templates
from wisper_transcribe.campaign_folders import finish_folder_rename, rename_campaign
from wisper_transcribe.campaign_manager import (
    CampaignError,
    _validate_campaign_slug,
    _validate_profile_key,
    add_member,
    bind_discord_id,
    create_campaign,
    delete_campaign,
    load_campaigns,
    remove_member,
    reorder_campaign_transcript,
)
from wisper_transcribe.speaker_manager import load_profiles
from wisper_transcribe.web._responses import error_redirect, invalid_input_response

router = APIRouter(prefix="/campaigns")

#: The ``?error=`` code for each refused campaign rename status.
_RENAME_ERROR_CODES = {
    "invalid": "invalid_name",
    "slug_taken": "campaign_exists",
    "taken": "campaign_exists",
    "folder_exists": "folder_taken",
    "folder_taken": "folder_taken",
    "busy": "busy",
    "pending": "rename_pending",
    "folder_missing": "folder_missing",
    "reserved": "reserved",
    "legacy_journal": "journal_legacy_pending",
}


def _parse_transcript_id(value: object) -> Optional[int]:
    """A transcript id posted in a form, or None when it isn't a whole number."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


@router.get("", response_class=HTMLResponse)
async def campaigns_index(request: Request) -> HTMLResponse:
    campaigns = load_campaigns()
    profiles = load_profiles()
    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {"request": request, "campaigns": campaigns, "profiles": profiles},
    )


@router.post("", response_class=HTMLResponse)
async def campaigns_create_post(
    request: Request,
    display_name: Annotated[str, Form()],
) -> RedirectResponse:
    display_name = display_name.strip()
    if not display_name:
        return error_redirect("/campaigns", "invalid_name")

    try:
        campaign = create_campaign(display_name)
    except CampaignError as exc:
        return error_redirect(
            "/campaigns",
            {"invalid": "invalid_name", "slug_taken": "campaign_exists",
             "taken": "campaign_exists", "folder_exists": "folder_taken"}.get(
                 exc.code, "create_failed"),
        )
    except ValueError:
        return error_redirect("/campaigns", "create_failed")

    # Redirect with the stored slug (from the database), not the raw form value.
    safe = _validate_campaign_slug(campaign.slug)
    if safe is None:
        return error_redirect("/campaigns", "create_failed")
    return RedirectResponse(url=f"/campaigns/{safe}", status_code=303)


@router.get("/{slug}", response_class=HTMLResponse)
async def campaign_detail(request: Request, slug: str) -> HTMLResponse:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaigns = load_campaigns()
    campaign = campaigns.get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    profiles = load_profiles()
    # Profiles not yet in this campaign (for the add-member dropdown)
    unenrolled = {k: v for k, v in profiles.items() if k not in campaign.members}

    from wisper_transcribe import transcript_store
    from wisper_transcribe.config import get_output_root
    transcript_store.reconcile(get_output_root(), sync="throttled", blocking=False)

    from wisper_transcribe.journal import journal_path, journal_stale_since, unjournalled_sessions
    journal_pending = len(unjournalled_sessions(safe))  # also syncs file ↔ DB
    jpath = journal_path(safe)
    journal_exists = bool(jpath and jpath.exists())
    journal_stale = journal_stale_since(safe) if journal_exists else None

    from wisper_transcribe.campaign_digest import (
        combined_summary_digest, combined_summary_stale_since, list_recaps,
    )
    combined = combined_summary_digest(safe)
    combined_stale = combined_summary_stale_since(safe) if combined is not None else None
    recaps = list_recaps(safe)

    # Entries whose transcript is gone (deleted outside wisper, renamed, or
    # on an unmounted drive). Shown as missing with Relink, never pruned: an
    # unavailable output dir would otherwise wipe every assignment.
    located = {tid: transcript_store.locate(tid) for tid in campaign.transcript_ids}
    sessions = []
    for stem, tid in zip(campaign.transcripts, campaign.transcript_ids):
        loc = located.get(tid)
        sessions.append({
            "id": tid,
            "stem": stem,
            "missing": loc.missing if loc is not None else True,
            "summarized": bool(loc is not None and loc.companion(".summary.md").exists()),
        })
    missing = {s["stem"] for s in sessions if s["missing"]}
    # For the rebuild confirmation's LLM-call count: sessions with a summary.
    summarized = sum(1 for s in sessions if s["summarized"])

    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {
            "request": request,
            "campaigns": campaigns,
            "profiles": profiles,
            "active_campaign": campaign,
            "sessions": sessions,
            "unenrolled": unenrolled,
            "journal_exists": journal_exists,
            "journal_pending": journal_pending,
            "journal_stale": journal_stale,
            "combined": combined,
            "combined_stale": combined_stale,
            "recaps": recaps,
            "summarized": summarized,
            "sessions_needing_summary": len(campaign.transcripts) - summarized,
            "missing_transcripts": missing,
            "relink_candidates": transcript_store.relink_candidates() if missing else [],
        },
    )


@router.post("/{slug}/rename", response_class=HTMLResponse)
async def campaign_rename(
    request: Request,
    slug: str,
    display_name: Annotated[str, Form()],
) -> RedirectResponse:
    """Rename a campaign: display name, slug, folder, and journal file."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    try:
        outcome = await run_in_threadpool(rename_campaign, safe, display_name)
    except KeyError:
        return error_redirect("/campaigns", "not_found")

    # Both slugs come from the database, never the URL or the form.
    campaign = load_campaigns().get(outcome.new_slug or safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")
    if outcome.status != "renamed":
        return error_redirect(f"/campaigns/{campaign.slug}",
                              _RENAME_ERROR_CODES.get(outcome.status, "rename_failed"))
    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/finish-rename", response_class=HTMLResponse)
async def campaign_finish_rename(
    request: Request,
    slug: str,
    without_folder: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Finish a pending folder rename (Retry, or Finish without folder)."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    try:
        done = await run_in_threadpool(
            finish_folder_rename, campaign.id, without_folder=bool(without_folder))
    except KeyError:
        return error_redirect("/campaigns", "not_found")
    if not done:
        return error_redirect(f"/campaigns/{campaign.slug}", "rename_pending")
    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/delete", response_class=HTMLResponse)
async def campaign_delete(
    request: Request,
    slug: str,
    mode: Annotated[str, Form()] = "keep",
) -> RedirectResponse:
    """Delete a campaign. ``mode=everything`` also deletes its transcripts and
    journal; anything else keeps them."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    try:
        outcome = await run_in_threadpool(
            delete_campaign, safe, delete_transcripts=(mode == "everything"))
    except KeyError:
        pass  # Already gone — redirect silently
    else:
        if outcome.status == "busy":
            return error_redirect("/campaigns", "busy")
        if outcome.status == "kept":
            # campaign.slug comes from the database, not the URL.
            campaign = load_campaigns().get(safe)
            if campaign is not None:
                return error_redirect(
                    f"/campaigns/{campaign.slug}", f"delete_kept&count={len(outcome.kept)}")
        if outcome.status == "delete_incomplete":
            return error_redirect("/campaigns", "delete_incomplete")
        # A kept journal becomes an unowned file; list it without waiting for the throttle.
        from wisper_transcribe import file_registry
        from wisper_transcribe.path_utils import get_output_dir
        file_registry.sync_if_due(get_output_dir(), force=True)

    return RedirectResponse(url="/campaigns", status_code=303)


@router.post("/{slug}/members", response_class=HTMLResponse)
async def campaign_add_member(
    request: Request,
    slug: str,
    profile_key: Annotated[str, Form()],
    role: Annotated[str, Form()] = "",
    character: Annotated[str, Form()] = "",
) -> RedirectResponse:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaigns = load_campaigns()
    campaign = campaigns.get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    # Validate profile_key by checking it exists in the global profile store.
    # We do NOT use profile_key in path construction — membership check only.
    profiles = load_profiles()
    if profile_key not in profiles:
        return RedirectResponse(
            url=f"/campaigns/{campaign.slug}?error=unknown_profile", status_code=303
        )

    try:
        add_member(safe, profile_key, role=role, character=character)
    except KeyError:
        return RedirectResponse(
            url=f"/campaigns/{campaign.slug}?error=not_found", status_code=303
        )

    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/members/{profile_key}/remove", response_class=HTMLResponse)
async def campaign_remove_member(
    request: Request,
    slug: str,
    profile_key: str,
) -> RedirectResponse:
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")

    clean_key = _validate_profile_key(profile_key)
    if clean_key is None:
        return invalid_input_response("Invalid profile key")

    campaigns = load_campaigns()
    campaign = campaigns.get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    try:
        remove_member(safe_slug, clean_key)
    except KeyError:
        pass  # Campaign gone — redirect silently

    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/members/{profile_key}/discord-id", response_class=HTMLResponse)
async def campaign_bind_discord_id(
    request: Request,
    slug: str,
    profile_key: str,
    discord_user_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")

    clean_key = _validate_profile_key(profile_key)
    if clean_key is None:
        return invalid_input_response("Invalid profile key")

    campaigns = load_campaigns()
    campaign = campaigns.get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    # Validate discord_user_id: Discord snowflake (pure digits) or empty to clear.
    cleaned_id: Optional[str] = None
    stripped = discord_user_id.strip()
    if stripped:
        if not re.match(r"^\d+$", stripped):
            return RedirectResponse(
                url=f"/campaigns/{campaign.slug}?error=invalid_discord_id", status_code=303
            )
        cleaned_id = stripped

    try:
        bind_discord_id(safe_slug, clean_key, cleaned_id)
    except KeyError:
        return RedirectResponse(
            url=f"/campaigns/{campaign.slug}?error=not_found", status_code=303
        )

    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/transcripts/remove", response_class=HTMLResponse)
async def campaign_remove_transcript(
    request: Request,
    slug: str,
    transcript_id: Annotated[str, Form()],
) -> RedirectResponse:
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")

    tid = _parse_transcript_id(transcript_id)
    if tid is None:
        return invalid_input_response("Invalid transcript id")

    campaigns = load_campaigns()
    campaign = campaigns.get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    from wisper_transcribe import transcript_store
    loc = transcript_store.locate(tid)
    if loc is None or loc.campaign_id != campaign.id:
        return error_redirect(f"/campaigns/{campaign.slug}", "not_found")

    outcome = await run_in_threadpool(
        transcript_store.move_transcript, loc.id, None, clash="keep_both")
    if outcome.status in ("moved", "unchanged", "partial"):
        return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)
    from .transcripts import _move_error_code
    return error_redirect(f"/campaigns/{campaign.slug}", _move_error_code(outcome))


@router.post("/{slug}/transcripts/reorder", response_class=HTMLResponse)
async def campaign_reorder_transcript(
    request: Request,
    slug: str,
    transcript_id: Annotated[str, Form()],
    direction: Annotated[str, Form()],
) -> RedirectResponse:
    """Move one transcript one position up/down in the campaign's transcript
    order — the order sessions get folded into the rolling journal in.
    """
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")

    tid = _parse_transcript_id(transcript_id)
    if tid is None:
        return invalid_input_response("Invalid transcript id")

    if direction not in ("up", "down"):
        return invalid_input_response("Invalid direction")

    campaign = load_campaigns().get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    from wisper_transcribe import transcript_store
    loc = transcript_store.locate(tid)
    if loc is None or loc.campaign_id != campaign.id:
        return error_redirect(f"/campaigns/{campaign.slug}", "not_found")

    try:
        reorder_campaign_transcript(safe_slug, loc.stem, direction)
    except ValueError:
        pass  # no-op when already at that end of the list

    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/journal", response_class=HTMLResponse)
async def campaign_journal_update(
    request: Request,
    slug: str,
    mode: Annotated[str, Form()] = "next",
) -> RedirectResponse:
    """Submit a rolling-journal job and redirect to its progress page.

    `mode="all"` folds every pending session; `mode="rebuild"` resets the
    journal and re-folds the existing summaries; `mode="resummarize"`
    redrives the whole campaign from its transcripts. Rebuilds are confirmed
    client-side before this POST (same pattern as "Fold all"); anything else
    folds the next one.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    queue = get_queue(request)
    try:
        if mode in ("rebuild", "resummarize"):
            resummarize = mode == "resummarize"
            label = "Rebuild journal from transcripts" if resummarize else "Rebuild journal"
            job = queue.submit_journal(
                safe, name=f"{label}: {campaign.display_name}",
                rebuild=True, resummarize=resummarize,
            )
        else:
            job = queue.submit_journal(
                safe, name=f"Journal: {campaign.display_name}", fold_all=(mode == "all")
            )
    except Exception:
        # The job's history row couldn't be written, so it wasn't queued.
        return error_redirect(f"/campaigns/{safe}", "submit_failed")
    # job.id is a server-generated uuid4 — never user input (CodeQL-safe redirect).
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.post("/{slug}/summarize", response_class=HTMLResponse)
async def campaign_summarize(request: Request, slug: str) -> RedirectResponse:
    """Submit a combined-summary job and redirect to its progress page."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    try:
        job = get_queue(request).submit_campaign_summary(
            safe, name=f"Combined summary: {campaign.display_name}")
    except Exception:
        # The job's history row couldn't be written, so it wasn't queued.
        return error_redirect(f"/campaigns/{safe}", "submit_failed")
    # job.id is a server-generated uuid4 — never user input (CodeQL-safe redirect).
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.post("/{slug}/recap", response_class=HTMLResponse)
async def campaign_recap(request: Request, slug: str,
                         sessions: Annotated[str, Form()] = "1") -> RedirectResponse:
    """Submit a "Previously on…" recap job and redirect to its progress page."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    from wisper_transcribe.campaign_digest import clamp_recap_sessions
    try:
        count = clamp_recap_sessions(int(sessions))
    except (TypeError, ValueError):
        count = 1
    try:
        job = get_queue(request).submit_campaign_recap(
            safe, sessions=count, name=f"Recap: {campaign.display_name}")
    except Exception:
        # The job's history row couldn't be written, so it wasn't queued.
        return error_redirect(f"/campaigns/{safe}", "submit_failed")
    # job.id is a server-generated uuid4 — never user input (CodeQL-safe redirect).
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.get("/{slug}/summaries/{file_id:int}", response_class=HTMLResponse)
async def campaign_digest_view(request: Request, slug: str, file_id: int) -> HTMLResponse:
    """Render a combined summary or recap by its ``files.id``, or 404.

    The digest is resolved from the database by an integer id and must belong to
    this campaign; no user-supplied name reaches a path.
    """
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    from wisper_transcribe.campaign_digest import digest_for_file
    from .transcripts import _sanitize_html

    digest = digest_for_file(safe, file_id)
    if digest is None or not digest.path.exists():
        return error_redirect(f"/campaigns/{campaign.slug}", "not_found")

    raw = digest.path.read_text(encoding="utf-8")
    from wisper_transcribe.journal import parse_journal
    meta, body = parse_journal(raw)
    import markdown as _md
    html_body = _sanitize_html(_md.markdown(body, extensions=["nl2br"]))
    return templates.TemplateResponse(
        request,
        "campaign_digest.html",
        {
            "request": request,
            "campaign": campaign,
            "slug": safe,
            "kind": digest.kind,
            "file_id": file_id,
            "meta": meta,
            "html_body": html_body,
            "sessions": digest.sessions,
        },
    )


@router.get("/{slug}/summaries/{file_id:int}/download")
async def campaign_digest_download(slug: str, file_id: int) -> Response:
    """Download a combined summary or recap by its ``files.id``, or 404."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    from wisper_transcribe.campaign_digest import digest_for_file

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")
    digest = digest_for_file(safe, file_id)
    if digest is None or not digest.path.exists():
        return HTMLResponse(content="Summary not found", status_code=404)
    # Header values are latin-1, and recap names carry an em dash (and display
    # names can be anything): an ASCII fallback from the db slug and id, plus
    # the real name as RFC 5987 filename*.
    from urllib.parse import quote
    fallback = f"{campaign.slug}-{digest.kind.replace('_', '-')}-{file_id}.md"
    return Response(
        content=digest.path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(digest.path.name)}"},
    )


@router.post("/{slug}/relabel", response_class=HTMLResponse)
async def campaign_relabel(request: Request, slug: str) -> RedirectResponse:
    """Submit a campaign-wide speaker re-match job and redirect to its progress page."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    try:
        job = get_queue(request).submit_relabel(
            safe, name=f"Re-match speakers: {campaign.display_name}"
        )
    except Exception:
        # The job's history row couldn't be written, so it wasn't queued.
        return error_redirect(f"/campaigns/{safe}", "submit_failed")
    # job.id is a server-generated uuid4 — never user input (CodeQL-safe redirect).
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.get("/{slug}/journal", response_class=HTMLResponse)
async def campaign_journal_view(request: Request, slug: str) -> HTMLResponse:
    """Render the campaign's rolling journal, or an empty-state page."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    from wisper_transcribe.journal import (
        journal_path, journal_stale_since, journaled_stems, parse_journal, unjournalled_sessions,
    )
    from .transcripts import _sanitize_html

    pending = unjournalled_sessions(safe)  # also syncs file ↔ DB
    jpath = journal_path(safe)
    meta: dict = {}
    html_body = ""
    has_journal = bool(jpath and jpath.exists())
    if has_journal:
        meta, body = parse_journal(jpath.read_text(encoding="utf-8"))
        import markdown as _md
        html_body = _sanitize_html(_md.markdown(body, extensions=["nl2br"]))

    return templates.TemplateResponse(
        request,
        "campaign_journal.html",
        {
            "request": request,
            "campaign": campaign,
            "slug": safe,
            "meta": meta,
            "html_body": html_body,
            "has_journal": has_journal,
            "pending": pending,
            "journaled_count": len(journaled_stems(safe)),
            "journal_stale": journal_stale_since(safe) if has_journal else None,
        },
    )


@router.get("/{slug}/journal/download")
async def campaign_journal_download(slug: str) -> Response:
    """Download journal.md with ``journaled_sessions`` added to its frontmatter
    (the list lives in the database, not in the file)."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    from wisper_transcribe.journal import export_journal

    campaign = load_campaigns().get(safe)
    text = export_journal(campaign.slug) if campaign is not None else None
    if text is None:
        return HTMLResponse(content="Journal not found", status_code=404)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        # campaign.slug comes from the database, not the URL.
        headers={"Content-Disposition": f'attachment; filename="{campaign.slug}-journal.md"'},
    )


@router.post("/{slug}/transcripts/relink", response_class=HTMLResponse)
async def campaign_relink_transcript(
    request: Request,
    slug: str,
    old_id: Annotated[str, Form()],
    new_id: Annotated[str, Form()],
) -> RedirectResponse:
    """Point a missing campaign entry at a transcript file under a new name.

    The entry keeps its position, journal entry, and speakers
    (``transcript_store.relink``). Both ids index the database; neither is
    turned into a path, so no name guard is needed.
    """
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")
    campaign = load_campaigns().get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    old_tid = _parse_transcript_id(old_id)
    new_tid = _parse_transcript_id(new_id)
    if old_tid is None or new_tid is None:
        return RedirectResponse(url=f"/campaigns/{campaign.slug}?error=relink_failed",
                                status_code=303)

    from wisper_transcribe import transcript_store

    old_loc = transcript_store.locate(old_tid)
    if old_loc is None or old_loc.campaign_id != campaign.id:
        return RedirectResponse(url=f"/campaigns/{campaign.slug}?error=relink_failed",
                                status_code=303)
    new_loc = transcript_store.locate(new_tid)
    if new_loc is None:
        return RedirectResponse(url=f"/campaigns/{campaign.slug}?error=relink_failed",
                                status_code=303)
    try:
        kept = transcript_store.relink(old_tid, new_loc.md)
    except (KeyError, ValueError):
        return RedirectResponse(url=f"/campaigns/{campaign.slug}?error=relink_failed", status_code=303)
    # campaign.slug comes from the database, not the URL.
    notice = "?notice=relink_kept_names" if kept else ""
    return RedirectResponse(url=f"/campaigns/{campaign.slug}{notice}", status_code=303)
