"""Search route — full-text search over transcripts and session summaries."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from wisper_transcribe import search_index
from wisper_transcribe.campaign_manager import load_campaigns

from . import templates

router = APIRouter()

MAX_PAGE = 500
MAX_QUERY_CHARS = 500


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    campaign: str = "",
    speaker: str = "",
    kind: str = "",
    page: int = 1,
) -> HTMLResponse:
    """Search results grouped by transcript; filters are exact values from
    the dropdowns, anything else is ignored."""
    q = q.strip()[:MAX_QUERY_CHARS]
    campaigns = load_campaigns()
    speakers = search_index.speakers()
    campaign_filter = campaign if campaign in campaigns else ""
    speaker_filter = speaker if speaker in speakers else ""
    kind_filter = kind if kind in search_index.KINDS else ""
    page = min(max(1, page), MAX_PAGE)

    results = None
    if q:
        results = search_index.search(
            q, campaign=campaign_filter or None, speaker=speaker_filter or None,
            kind=kind_filter or None, page=page, per_page=search_index.PAGE_SIZE,
        )
    indexed, total = search_index.progress()
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "request": request,
            "q": q,
            "results": results,
            "campaigns": campaigns,
            "speakers": speakers,
            "campaign_filter": campaign_filter,
            "speaker_filter": speaker_filter,
            "kind_filter": kind_filter,
            "page": page,
            "indexed": indexed,
            "total": total,
        },
    )
