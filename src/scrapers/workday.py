from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
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

# Workday's job-search API is uniform across every tenant it hosts:
# POST https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
# — same shape whether the tenant is a 50-person startup or Intel. `instance`
# is a per-tenant string (wd1, wd5, wd12, ...) with no derivable pattern, and
# `site` is a per-tenant career-site name (e.g. "External", "external_experienced") —
# both only discoverable by resolving the tenant's actual careers URL, not
# computable from the company name alone. Both are folded into the single
# `CompanyLookup.slug` column discovery already provides (§7.2, §3.5), as
# "{tenant}.{instance}/{site}" — e.g. "intel.wd1/External" — mirroring the
# tenant.instance hostname prefix and the site path segment directly.
_JOBS_API_URL = "https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
_CAREER_SITE_URL = "https://{tenant}.{instance}.myworkdayjobs.com/{site}"
# Word-boundary on "intern" (unlike greenhouse.py/lever.py's plain `intern|co-?op`)
# because Workday's own loose upstream search made the substring-match risk
# real rather than theoretical: querying "intern" live against Adobe's board
# returned "Internal Product Manager 5" in the results, which a bare `intern`
# regex would also match as a false positive.
_INTERN_RE = re.compile(r"\bintern(?:s|ship)?\b|\bco-?op\b", re.IGNORECASE)
_RATE_LIMIT_SECONDS = 5.0
_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"

# Workday's own searchText match is loose (e.g. "intern" matches "Internal
# Product Manager", live-observed against Adobe's board; "co-op" matches
# "Co-Val", live-observed against Intel's) — it's a coarse pre-filter, not a
# real relevance filter, so both queries below are still filtered client-side
# by _INTERN_RE against the title, same as greenhouse.py/lever.py.
_SEARCH_QUERIES = ["intern", "co-op"]

# Some Workday deployments 400 above this (live-observed: limit=100 against
# Intel's board); 20 is the widely-supported page size. Only the first page
# per query is fetched — a company with more open intern/co-op roles than
# fit across both queries' first pages is a known, documented gap rather than
# a silent one (§3.5 rollout tracks broader coverage separately).
_PAGE_LIMIT = 20

_POSTED_TODAY_RE = re.compile(r"posted\s+today", re.IGNORECASE)
_POSTED_YESTERDAY_RE = re.compile(r"posted\s+yesterday", re.IGNORECASE)
_POSTED_DAYS_AGO_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def _is_internship(title: str) -> bool:
    return bool(_INTERN_RE.search(title))


def _parse_slug(slug: str) -> tuple[str, str, str]:
    """Split a "{tenant}.{instance}/{site}" slug into its parts. Raises
    ValueError on any other shape, so a malformed/stale cached slug is
    treated as a per-company failure by the caller, not a crash."""
    host_part, _, site = slug.partition("/")
    tenant, _, instance = host_part.partition(".")
    if not tenant or not instance or not site:
        raise ValueError(f"Malformed Workday slug: {slug!r}")
    return tenant, instance, site


def _parse_posted_at(posted_on: str | None, *, now: datetime | None = None) -> datetime | None:
    """Best-effort parse of Workday's relative `postedOn` string
    ("Posted Today" / "Posted Yesterday" / "Posted N Days Ago" / "Posted
    30+ Days Ago") into an absolute UTC datetime. Returns None — same
    "unreliable/absent, benefit of the doubt" treatment `_is_fresh()` in
    main.py already gives other sources — for anything unrecognized, rather
    than guessing. "30+ Days Ago" is mapped to exactly 30 days ago: it's a
    lower bound, not a true reading, but that's still enough to correctly
    fail the default 7-day freshness window rather than being silently kept
    on a None-is-fresh technicality."""
    if not posted_on:
        return None
    reference = now or datetime.now(UTC).replace(tzinfo=None)
    if _POSTED_TODAY_RE.search(posted_on):
        return reference
    if _POSTED_YESTERDAY_RE.search(posted_on):
        return reference - timedelta(days=1)
    match = _POSTED_DAYS_AGO_RE.search(posted_on)
    if match:
        return reference - timedelta(days=int(match.group(1)))
    return None


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def _fetch_jobs_page(
    tenant: str, instance: str, site: str, search_text: str
) -> list[dict[str, Any]]:
    resp = httpx.post(
        _JOBS_API_URL.format(tenant=tenant, instance=instance, site=site),
        json={"appliedFacets": {}, "limit": _PAGE_LIMIT, "offset": 0, "searchText": search_text},
        headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return list(data.get("jobPostings", []))


def _to_raw_posting(job: dict[str, Any], tenant: str, instance: str, site: str) -> RawPosting:
    location = job.get("locationsText")
    career_site = _CAREER_SITE_URL.format(tenant=tenant, instance=instance, site=site)
    url = career_site + job.get("externalPath", "")

    return RawPosting(
        # externalPath is the only value Workday's search response guarantees
        # is present and unique per posting — there's no bare numeric/string
        # "id" field the way Greenhouse and Lever both provide one.
        source=f"workday:{tenant}/{site}",
        external_id=job.get("externalPath", ""),
        title=job["title"],
        company=tenant,
        location=location,
        is_remote=bool(location and "remote" in location.lower()),
        url=url,
        # The job's own page IS the direct employer application page on
        # Workday — same "ATS page is the direct link" reasoning already
        # applied to Greenhouse/Lever — so apply_url and url are identical.
        apply_url=url,
        # Workday's job-search response doesn't include the full posting
        # body (only title/location/postedOn) — fetching it would require a
        # second request per posting, doubling request volume for every
        # company on every poll. Left as a known gap rather than built now;
        # the AI scorer still has the title to work from.
        description=None,
        posted_at=_parse_posted_at(job.get("postedOn")),
    )


def fetch(slug: str) -> list[RawPosting]:
    """Fetch and filter internship/co-op postings for a single Workday
    tenant/site, identified by a "{tenant}.{instance}/{site}" slug."""
    tenant, instance, site = _parse_slug(slug)

    by_external_id: dict[str, RawPosting] = {}
    for query in _SEARCH_QUERIES:
        jobs = _fetch_jobs_page(tenant, instance, site, query)
        for job in jobs:
            if not _is_internship(job.get("title", "")):
                continue
            posting = _to_raw_posting(job, tenant, instance, site)
            by_external_id[posting.external_id] = posting
    return list(by_external_id.values())


def fetch_for_resolved_companies(session: Session) -> list[RawPosting]:
    """
    Fetch postings for every company resolved to a Workday tenant in the DB.
    Respects a 1 req/5s rate limit between companies.
    """
    slugs = list(
        session.execute(
            select(CompanyLookup.slug).where(
                CompanyLookup.ats_type == "workday",
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
        except (httpx.HTTPError, RetryError, ValueError):
            # RetryError: fetch()'s underlying @retry wraps a repeated
            # httpx.HTTPError once stop_after_attempt(3) is exhausted, same
            # failure mode already fixed for greenhouse.py/lever.py.
            # ValueError: a malformed cached slug (_parse_slug). Either way,
            # one bad company must not abort every remaining company in this
            # cycle.
            continue
    return postings
