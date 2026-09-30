"""Campaigns route — manage per-campaign speaker rosters."""
from __future__ import annotations

import os
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import get_queue, templates
from wisper_transcribe.campaign_manager import (
    _validate_campaign_slug,
    _validate_profile_key,
    add_member,
    bind_discord_id,
    create_campaign,
    delete_campaign,
    load_campaigns,
    remove_member,
    remove_transcript_from_campaign,
    reorder_campaign_transcript,
)
from wisper_transcribe.speaker_manager import load_profiles
from wisper_transcribe.web._responses import error_redirect, invalid_input_response

router = APIRouter(prefix="/campaigns")


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
    except ValueError:
        return error_redirect("/campaigns", "create_failed")

    # Use the server-generated slug (from uuid4-like derivation), not the raw form value.
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
    from wisper_transcribe.path_utils import get_output_dir
    transcript_store.reconcile(get_output_dir())  # flag externally deleted/renamed files

    from wisper_transcribe.journal import journal_path, journal_stale_since, unjournalled_sessions
    journal_pending = len(unjournalled_sessions(safe))  # also syncs file ↔ DB
    jpath = journal_path(safe)
    journal_exists = bool(jpath and jpath.exists())
    journal_stale = journal_stale_since(safe) if journal_exists else None

    # Entries whose transcript is gone (deleted outside wisper, renamed, or
    # on an unmounted drive). Shown as missing with Relink, never pruned: an
    # unavailable output dir would otherwise wipe every assignment.
    base_dir = os.path.abspath(str(get_output_dir()))
    if not base_dir.endswith(os.sep):
        base_dir += os.sep

    def _transcript_missing(stem: str) -> bool:
        # Stems come from the database; same basename + abspath guard as routes.
        safe = os.path.basename(stem)
        candidate = os.path.abspath(os.path.join(base_dir, safe + ".md"))
        return safe != stem or not candidate.startswith(base_dir) or not os.path.exists(candidate)

    missing = {stem for stem in campaign.transcripts if _transcript_missing(stem)}
    # For the rebuild confirmation's LLM-call count: sessions without a summary.
    summarized = sum(
        1 for stem in campaign.transcripts
        if os.path.exists(os.path.join(base_dir, f"{os.path.basename(stem)}.summary.md"))
    )

    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {
            "request": request,
            "campaigns": campaigns,
            "profiles": profiles,
            "active_campaign": campaign,
            "unenrolled": unenrolled,
            "journal_exists": journal_exists,
            "journal_pending": journal_pending,
            "journal_stale": journal_stale,
            "sessions_needing_summary": len(campaign.transcripts) - summarized,
            "missing_transcripts": missing,
            "relink_candidates": transcript_store.relink_candidates() if missing else [],
        },
    )


@router.post("/{slug}/delete", response_class=HTMLResponse)
async def campaign_delete(request: Request, slug: str) -> RedirectResponse:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    try:
        delete_campaign(safe)
    except KeyError:
        pass  # Already gone — redirect silently

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
    stem: Annotated[str, Form()],
) -> RedirectResponse:
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")

    # Stem validation: no null bytes, no path separators, not empty, not a dot path.
    # The stem only selects a campaign_transcripts row — no file paths are
    # constructed from it — but we still reject traversal-style payloads.
    if (
        not stem
        or "\x00" in stem
        or os.sep in stem
        or "/" in stem
        or "\\" in stem
        or stem.strip(".") == ""
        or len(stem) > 512
    ):
        return invalid_input_response("Invalid transcript stem")

    campaigns = load_campaigns()
    campaign = campaigns.get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    if stem in campaign.transcripts:
        remove_transcript_from_campaign(stem)

    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)


@router.post("/{slug}/transcripts/reorder", response_class=HTMLResponse)
async def campaign_reorder_transcript(
    request: Request,
    slug: str,
    stem: Annotated[str, Form()],
    direction: Annotated[str, Form()],
) -> RedirectResponse:
    """Move one transcript one position up/down in the campaign's transcript
    order — the order sessions get folded into the rolling journal in.
    """
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")

    # Same stem-validation as /transcripts/remove: never used in a file path
    # (only a campaign_transcripts row), but still reject traversal-style
    # payloads defensively.
    if (
        not stem
        or "\x00" in stem
        or os.sep in stem
        or "/" in stem
        or "\\" in stem
        or stem.strip(".") == ""
        or len(stem) > 512
    ):
        return invalid_input_response("Invalid transcript stem")

    if direction not in ("up", "down"):
        return invalid_input_response("Invalid direction")

    campaign = load_campaigns().get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    try:
        reorder_campaign_transcript(safe_slug, stem, direction)
    except ValueError:
        pass  # stem not in this campaign (stale form) — no-op, just redirect back

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
    # job.id is a server-generated uuid4 — never user input (CodeQL-safe redirect).
    return RedirectResponse(url=f"/transcribe/jobs/{job.id}", status_code=303)


@router.post("/{slug}/relabel", response_class=HTMLResponse)
async def campaign_relabel(request: Request, slug: str) -> RedirectResponse:
    """Submit a campaign-wide speaker re-match job and redirect to its progress page."""
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return invalid_input_response("Invalid campaign slug")

    campaign = load_campaigns().get(safe)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    job = get_queue(request).submit_relabel(
        safe, name=f"Re-match speakers: {campaign.display_name}"
    )
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
    old_stem: Annotated[str, Form()],
    new_stem: Annotated[str, Form()],
) -> RedirectResponse:
    """Point a missing campaign entry at a transcript file under a new name.

    The entry keeps its position, journal entry, and speakers
    (``transcript_store.relink``). Both stems are path-guarded here and again
    in the store.
    """
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return invalid_input_response("Invalid campaign slug")
    campaign = load_campaigns().get(safe_slug)
    if campaign is None:
        return error_redirect("/campaigns", "not_found")

    from wisper_transcribe import transcript_store
    from wisper_transcribe.path_utils import get_output_dir

    out_dir = get_output_dir()
    old_md = transcript_store.safe_path(old_stem, ".md", out_dir)
    new_md = transcript_store.safe_path(new_stem, ".md", out_dir)
    if old_md is None or new_md is None or old_md.stem not in campaign.transcripts:
        return RedirectResponse(url=f"/campaigns/{campaign.slug}?error=relink_failed", status_code=303)
    try:
        transcript_store.relink(old_md.stem, new_md.stem, output_dir=out_dir)
    except (KeyError, ValueError):
        return RedirectResponse(url=f"/campaigns/{campaign.slug}?error=relink_failed", status_code=303)
    # campaign.slug comes from the database, not the URL.
    return RedirectResponse(url=f"/campaigns/{campaign.slug}", status_code=303)
