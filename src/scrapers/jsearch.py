"""JSearch (RapidAPI) scraper — the only source that reaches LinkedIn (plus
Glassdoor/ZipRecruiter) postings. Requires JSEARCH_API_KEY.

Every other Tier 2 source is free and doesn't cover LinkedIn — see the
"LinkedIn" constraint in docs/requirements.md. JSearch re-sells aggregated
listings (including LinkedIn's) through a metered API instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from src.scrapers.base import RawPosting

_SEARCH_URL = "https://jsearch.p.rapidapi.com/search"
_HOST = "jsearch.p.rapidapi.com"


def _to_raw_posting(result: dict[str, Any]) -> RawPosting:
    posted_at = None
    raw_date = result.get("job_posted_at_datetime_utc")
    if raw_date:
        try:
            posted_at = (
                datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except ValueError:
            posted_at = None

    location = ", ".join(p for p in [result.get("job_city"), result.get("job_state")] if p) or None
    apply_url = result.get("job_apply_link") or ""

    return RawPosting(
        source="jsearch",
        external_id=str(result.get("job_id")),
        title=result.get("job_title", ""),
        company=result.get("employer_name", ""),
        location=location,
        is_remote=bool(result.get("job_is_remote")),
        url=apply_url,
        apply_url=apply_url,
        description=result.get("job_description"),
        posted_at=posted_at,
    )


def fetch(
    api_key: str, *, query: str = "software engineering intern in the United States"
) -> list[RawPosting]:
    """
    Single broad query per poll — deliberately NOT looped per domain keyword
    like Indeed RSS. JSearch meters every request against a monthly quota on
    every RapidAPI plan; check your plan's actual limit on the RapidAPI
    dashboard before raising the poll frequency, widening the query, or
    looping multiple queries per cycle. The AI scorer handles fine-grained
    relevance from this one broad result set, same as Adzuna.
    """
    resp = httpx.get(
        _SEARCH_URL,
        params={
            "query": query,
            "page": "1",
            "num_pages": "1",
            "date_posted": "week",
            "employment_types": "INTERN",
        },
        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": _HOST},
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return [_to_raw_posting(r) for r in data.get("data", [])]
