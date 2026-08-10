"""Y Combinator 'Work at a Startup' scraper (no auth).

NOTE: workatastartup.com's job search endpoint is an internal, undocumented
API used by their own frontend — this implementation follows the shape
described in docs/design.md §4.4 but has not been verified against a live
response. Confirm the request/response schema before relying on it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from src.scrapers.base import RawPosting

_SEARCH_URL = "https://www.workatastartup.com/company_filters/search_startup_jobs"
_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"
_DOMAIN_KEYWORDS = (
    "software",
    "engineer",
    "data",
    "machine learning",
    "artificial intelligence",
    " ai ",
    "backend",
    "frontend",
    "full stack",
)


def _fetch_jobs(role_type: str = "intern") -> list[dict[str, Any]]:
    resp = httpx.post(
        _SEARCH_URL,
        json={"role_types": [role_type], "job_location": "US"},
        headers={"User-Agent": _USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return list(data.get("jobs", []))


def _matches_domain(job: dict[str, Any]) -> bool:
    text = f" {job.get('title', '')} {job.get('description', '')} ".lower()
    return any(keyword in text for keyword in _DOMAIN_KEYWORDS)


def _to_raw_posting(job: dict[str, Any]) -> RawPosting:
    posted_at = None
    created = job.get("created_at") or job.get("live_at")
    if created:
        try:
            posted_at = (
                datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except ValueError:
            posted_at = None

    company = job.get("company_name", "")
    url = job.get("url") or job.get("job_url", "")
    location = job.get("location")

    return RawPosting(
        source="yc",
        external_id=str(job.get("id")),
        title=job.get("title", ""),
        company=company,
        location=location,
        is_remote=bool(job.get("remote")) or bool(location and "remote" in str(location).lower()),
        url=url,
        apply_url=url,
        description=job.get("description"),
        posted_at=posted_at,
    )


def fetch() -> list[RawPosting]:
    """Fetch YC-batch company internship postings, filtered to SWE/DS/ML/AI roles."""
    jobs = _fetch_jobs()
    return [_to_raw_posting(j) for j in jobs if _matches_domain(j)]
