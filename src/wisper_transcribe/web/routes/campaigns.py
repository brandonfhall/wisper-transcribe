"""Campaigns route — manage per-campaign speaker rosters."""
from __future__ import annotations

import os
from typing import Annotated, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import templates
from wisper_transcribe.campaign_manager import (
    _validate_campaign_slug,
    add_member,
    create_campaign,
    delete_campaign,
    load_campaigns,
    remove_member,
)
from wisper_transcribe.speaker_manager import load_profiles

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
        return RedirectResponse(url="/campaigns?error=invalid_name", status_code=303)

    try:
        campaign = create_campaign(display_name)
    except ValueError:
        return RedirectResponse(url="/campaigns?error=create_failed", status_code=303)

    # Use the server-generated slug (from uuid4-like derivation), not the raw form value.
    safe = _validate_campaign_slug(campaign.slug)
    if safe is None:
        return RedirectResponse(url="/campaigns?error=create_failed", status_code=303)
    return RedirectResponse(url=f"/campaigns/{safe}", status_code=303)


@router.get("/{slug}", response_class=HTMLResponse)
async def campaign_detail(request: Request, slug: str) -> HTMLResponse:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return HTMLResponse(content="Invalid campaign slug", status_code=400)

    campaigns = load_campaigns()
    campaign = campaigns.get(safe)
    if campaign is None:
        return RedirectResponse(url="/campaigns?error=not_found", status_code=303)

    profiles = load_profiles()
    # Profiles not yet in this campaign (for the add-member dropdown)
    unenrolled = {k: v for k, v in profiles.items() if k not in campaign.members}

    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {
            "request": request,
            "campaigns": campaigns,
            "profiles": profiles,
            "active_campaign": campaign,
            "unenrolled": unenrolled,
        },
    )


@router.post("/{slug}/delete", response_class=HTMLResponse)
async def campaign_delete(request: Request, slug: str) -> RedirectResponse:
    safe = _validate_campaign_slug(slug)
    if safe is None:
        return HTMLResponse(content="Invalid campaign slug", status_code=400)

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
        return HTMLResponse(content="Invalid campaign slug", status_code=400)

    # Validate profile_key by checking it exists in the global profile store.
    # We do NOT use profile_key in path construction — membership check only.
    profiles = load_profiles()
    if profile_key not in profiles:
        return RedirectResponse(
            url=f"/campaigns/{safe}?error=unknown_profile", status_code=303
        )

    try:
        add_member(safe, profile_key, role=role, character=character)
    except KeyError:
        return RedirectResponse(
            url=f"/campaigns/{safe}?error=not_found", status_code=303
        )

    return RedirectResponse(url=f"/campaigns/{safe}", status_code=303)


@router.post("/{slug}/members/{profile_key}/remove", response_class=HTMLResponse)
async def campaign_remove_member(
    request: Request,
    slug: str,
    profile_key: str,
) -> RedirectResponse:
    safe_slug = _validate_campaign_slug(slug)
    if safe_slug is None:
        return HTMLResponse(content="Invalid campaign slug", status_code=400)

    # Validate profile_key with same two-layer pattern.
    if not profile_key or "\x00" in profile_key:
        return HTMLResponse(content="Invalid profile key", status_code=400)
    import re as _re
    safe_key = os.path.basename(profile_key)
    if safe_key != profile_key or not _re.match(r"^[\w\-]+$", safe_key):
        return HTMLResponse(content="Invalid profile key", status_code=400)

    _guard_base = os.path.abspath("_guard")
    if not _guard_base.endswith(os.sep):
        _guard_base += os.sep
    _guard_path = os.path.abspath(os.path.join(_guard_base, safe_key))
    if not _guard_path.startswith(_guard_base):
        return HTMLResponse(content="Invalid profile key", status_code=400)
    clean_key = os.path.basename(_guard_path)

    try:
        remove_member(safe_slug, clean_key)
    except KeyError:
        pass  # Campaign gone — redirect silently

    return RedirectResponse(url=f"/campaigns/{safe_slug}", status_code=303)
