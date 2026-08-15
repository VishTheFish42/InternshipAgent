from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.db import CompanyLookup
from src.scrapers.base import RawPosting

_API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
_INTERN_RE = re.compile(r"intern|co-?op", re.IGNORECASE)
_RATE_LIMIT_SECONDS = 5.0
_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"


def _is_internship(title: str) -> bool:
    return bool(_INTERN_RE.search(title))


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def _fetch_jobs_for_slug(slug: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        _API_URL.format(slug=slug),
        headers={"User-Agent": _USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return list(data.get("jobs", []))


def _to_raw_posting(job: dict[str, Any], slug: str) -> RawPosting:
    location = job.get("location", {}).get("name")
    posted_at: datetime | None = None
    updated = job.get("updated_at")
    if updated:
        try:
            posted_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            posted_at = None

    url = job.get("absolute_url", "")
    return RawPosting(
        source=f"greenhouse:{slug}",
        external_id=str(job["id"]),
        title=job["title"],
        company=slug,
        location=location,
        is_remote=bool(location and "remote" in location.lower()),
        url=url,
        apply_url=url,
        description=job.get("content"),
        posted_at=posted_at,
    )


def fetch(slug: str) -> list[RawPosting]:
    """Fetch and filter internship/co-op postings for a single Greenhouse board."""
    jobs = _fetch_jobs_for_slug(slug)
    return [_to_raw_posting(j, slug) for j in jobs if _is_internship(j.get("title", ""))]


def fetch_for_resolved_companies(session: Session) -> list[RawPosting]:
    """
    Fetch postings for every company resolved to a Greenhouse board in the DB.
    Respects a 1 req/5s rate limit between companies.
    """
    slugs = list(
        session.execute(
            select(CompanyLookup.slug).where(
                CompanyLookup.ats_type == "greenhouse",
                CompanyLookup.status == "resolved",
                CompanyLookup.slug.is_not(None),
            )
        ).scalars()
    )

    postings: list[RawPosting] = []
    for i, slug in enumerate(slugs):
        if slug is None:
            continue
        if i > 0:
            time.sleep(_RATE_LIMIT_SECONDS)
        try:
            postings.extend(fetch(slug))
        except (httpx.HTTPError, RetryError):
            # RetryError is what _fetch_jobs_for_slug's @retry actually raises
            # once stop_after_attempt(3) is exhausted — it wraps the underlying
            # httpx.HTTPError rather than re-raising it, so both must be caught
            # here or one bad slug (e.g. a stale/incorrect cached ATS mapping)
            # aborts every remaining company in this loop for the whole cycle.
            continue
    return postings
