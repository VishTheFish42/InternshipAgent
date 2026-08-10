"""Dice tech job board scraper.

NOTE: Dice does not offer a stable free public API or RSS feed anymore — this
implementation follows the shape described in docs/design.md §4.7 as a
best-effort placeholder. Verify (or replace) the endpoint against Dice's
current terms before relying on it; it may require a commercial API key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from src.scrapers.base import RawPosting

_SEARCH_URL = "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"
_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"


def _fetch_jobs(query: str, api_key: str | None = None) -> list[dict[str, Any]]:
    headers = {"User-Agent": _USER_AGENT}
    if api_key:
        headers["x-api-key"] = api_key

    resp = httpx.get(
        _SEARCH_URL,
        params={"q": query, "employmentType": "INTERN", "countryCode2": "US"},
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return list(data.get("data", []))


def _to_raw_posting(job: dict[str, Any]) -> RawPosting:
    posted_at = None
    posted_date = job.get("postedDate")
    if posted_date:
        try:
            posted_at = (
                datetime.fromisoformat(str(posted_date).replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except ValueError:
            posted_at = None

    url = job.get("detailsPageUrl", "")
    location = job.get("jobLocation", {}).get("displayName") if job.get("jobLocation") else None

    return RawPosting(
        source="dice",
        external_id=str(job.get("id")),
        title=job.get("title", ""),
        company=job.get("companyName", ""),
        location=location,
        is_remote=bool(job.get("isRemote")) or bool(location and "remote" in location.lower()),
        url=url,
        apply_url=url,
        description=job.get("summary"),
        posted_at=posted_at,
    )


def fetch(query: str = "internship", api_key: str | None = None) -> list[RawPosting]:
    """Fetch internship postings from Dice for a single broad query."""
    jobs = _fetch_jobs(query, api_key)
    return [_to_raw_posting(j) for j in jobs]
