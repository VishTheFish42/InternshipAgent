"""RemoteOK public API scraper (no auth, single bulk feed — no per-query polling)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from src.scrapers.base import RawPosting

_API_URL = "https://remoteok.com/api"
_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"
_INTERN_TAGS = {"intern", "internship"}
# Deliberately excludes "junior" — RemoteOK tags junior roles across every field
# (marketing, design, sales, ...), not just software; that bare tag was letting
# through postings with no software relevance at all.


def _fetch_jobs() -> list[dict[str, Any]]:
    resp = httpx.get(_API_URL, headers={"User-Agent": _USER_AGENT}, timeout=10.0)
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json()
    # The first element of RemoteOK's feed is a legal notice, not a job.
    return [item for item in data if "id" in item]


def _is_internship(job: dict[str, Any]) -> bool:
    tags = {str(t).lower() for t in job.get("tags", [])}
    if tags & _INTERN_TAGS:
        return True
    return "intern" in (job.get("position") or "").lower()


def _to_raw_posting(job: dict[str, Any]) -> RawPosting:
    posted_at = None
    date_str = job.get("date")
    if date_str:
        try:
            posted_at = (
                datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except ValueError:
            posted_at = None

    url = job.get("url", "")
    return RawPosting(
        source="remoteok",
        external_id=str(job.get("id")),
        title=job.get("position", ""),
        company=job.get("company", ""),
        location=job.get("location") or "Remote",
        is_remote=True,
        url=url,
        apply_url=job.get("apply_url") or url,
        description=job.get("description"),
        posted_at=posted_at,
    )


def fetch() -> list[RawPosting]:
    """Fetch the full RemoteOK feed and filter to internship/junior-tagged roles."""
    jobs = _fetch_jobs()
    return [_to_raw_posting(j) for j in jobs if _is_internship(j)]
