from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.db import CompanyLookup
from src.scrapers.base import RawPosting

_API_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
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
def _fetch_postings_for_slug(slug: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        _API_URL.format(slug=slug),
        headers={"User-Agent": _USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json()
    return data


def _to_raw_posting(job: dict[str, Any], slug: str) -> RawPosting:
    categories = job.get("categories", {}) or {}
    location = categories.get("location")

    posted_at: datetime | None = None
    created_at = job.get("createdAt")
    if created_at:
        try:
            posted_at = datetime.fromtimestamp(int(created_at) / 1000, tz=UTC).replace(tzinfo=None)
        except (ValueError, TypeError):
            posted_at = None

    apply_url = job.get("applyUrl") or job.get("hostedUrl", "")
    return RawPosting(
        source=f"lever:{slug}",
        external_id=str(job["id"]),
        title=job["text"],
        company=slug,
        location=location,
        is_remote=bool(location and "remote" in location.lower()),
        url=job.get("hostedUrl", apply_url),
        apply_url=apply_url,
        description=job.get("descriptionPlain") or job.get("description"),
        posted_at=posted_at,
    )


def fetch(slug: str) -> list[RawPosting]:
    """Fetch and filter internship/co-op postings for a single Lever board."""
    jobs = _fetch_postings_for_slug(slug)
    return [_to_raw_posting(j, slug) for j in jobs if _is_internship(j.get("text", ""))]


def fetch_for_resolved_companies(session: Session) -> list[RawPosting]:
    """
    Fetch postings for every company resolved to a Lever board in the DB.
    Respects a 1 req/5s rate limit between companies.
    """
    slugs = list(
        session.execute(
            select(CompanyLookup.slug).where(
                CompanyLookup.ats_type == "lever",
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
        except httpx.HTTPError:
            continue
    return postings
