"""Adzuna job board scraper — requires ADZUNA_APP_ID / ADZUNA_APP_KEY."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from src.scrapers.base import RawPosting

_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"


def _to_raw_posting(result: dict[str, Any]) -> RawPosting:
    posted_at = None
    created = result.get("created")
    if created:
        try:
            posted_at = (
                datetime.fromisoformat(created.replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except ValueError:
            posted_at = None

    company = (result.get("company") or {}).get("display_name", "")
    location = (result.get("location") or {}).get("display_name")
    url = result.get("redirect_url", "")

    return RawPosting(
        source="adzuna",
        external_id=str(result.get("id")),
        title=result.get("title", ""),
        company=company,
        location=location,
        is_remote=bool(location and "remote" in location.lower()),
        url=url,
        apply_url=url,
        description=result.get("description"),
        posted_at=posted_at,
    )


def fetch(
    app_id: str, app_key: str, *, what: str = "internship", results_per_page: int = 50
) -> list[RawPosting]:
    """
    Single broad query for internship postings — deliberately NOT looped per
    keyword like the other Tier 2 sources. Adzuna's free tier allows only 250
    calls/month, which a 30-minute poll cycle would exhaust in under a day at
    more than ~1 call per cycle. The AI scorer handles relevance filtering
    from this one broad result set, same as every other source.
    """
    resp = httpx.get(
        _SEARCH_URL,
        params={
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": results_per_page,
            "what": what,
            "where": "us",
            "sort_by": "date",
            "full_time": 0,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return [_to_raw_posting(r) for r in data.get("results", [])]
