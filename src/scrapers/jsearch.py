"""JSearch (RapidAPI) scraper — the only source that reaches LinkedIn postings.
Requires JSEARCH_API_KEY.

Every other Tier 2 source is free but doesn't cover LinkedIn — see the
"LinkedIn" constraint in docs/requirements.md. JSearch re-sells aggregated
listings (including LinkedIn's) through a metered API instead.

Endpoint is /search-v2, not the more commonly documented /search — confirmed
against a live subscription; RapidAPI's "JSearch" listings vary in which
endpoints they actually expose. Results are nested under data.jobs, not data
directly. Each result carries job_publisher (e.g. "LinkedIn", "ZipRecruiter",
"Indeed", "BeBee") naming the exact board it was aggregated from — encoded
into `source` as "jsearch:{publisher}", the same compound-source convention
greenhouse.py/lever.py use for company slugs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from src.scrapers.base import RawPosting

_SEARCH_URL = "https://jsearch.p.rapidapi.com/search-v2"
_HOST = "jsearch.p.rapidapi.com"

# Job-board re-posters, not employers — confirmed live: JSearch marks these
# is_direct: true even though the link goes to the aggregator's own listing
# page, not the company's application system. Excluded regardless of what
# is_direct says, per explicit user request after seeing them show up as the
# "Apply" link.
_BLOCKED_APPLY_DOMAINS = {"bebee.com", "jobleads.com"}


def _is_blocked_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith(f".{d}") for d in _BLOCKED_APPLY_DOMAINS)

# Two query variants, alternated per poll by the caller (main.py) rather than
# both queried every poll — a second query every 4 hours would double the
# ~180/month request volume to ~360/month, over a 200/month free-tier quota.
INTERN_QUERY = "software engineering, AI, or data engineering intern in the United States"
COOP_QUERY = "software engineering, AI, or data engineering co-op in the United States"


def _direct_apply_url(result: dict[str, Any]) -> str:
    """Prefer the employer's own application page over the aggregator's
    (LinkedIn/BeBee/JobLeads/etc.) re-listing. JSearch's job_apply_link is
    often a re-poster, not the employer — confirmed in a live test where
    job_apply_link pointed to BeBee with job_apply_is_direct: false.
    apply_options carries every known application route with an is_direct
    flag per option; use the first direct one if any exists, otherwise fall
    back to job_apply_link.

    _BLOCKED_APPLY_DOMAINS (BeBee, JobLeads) are excluded at every step
    regardless of is_direct — JSearch's own flag has been observed marking
    them direct even though they're just another re-poster, not the
    employer. The full priority chain is built first and then filtered,
    so a blocked domain never displaces a genuinely better candidate
    further down the list; only if literally nothing else is available
    does a blocked link get returned, rather than leaving the posting
    with no apply link at all."""
    candidates: list[str] = []

    if result.get("job_apply_is_direct"):
        link = result.get("job_apply_link")
        if link:
            candidates.append(str(link))

    apply_options = result.get("apply_options") or []
    for option in apply_options:
        if option.get("is_direct") and option.get("apply_link"):
            candidates.append(str(option["apply_link"]))

    fallback = result.get("job_apply_link")
    if fallback:
        candidates.append(str(fallback))

    for option in apply_options:
        link = option.get("apply_link")
        if link:
            candidates.append(str(link))

    for candidate in candidates:
        if not _is_blocked_domain(candidate):
            return candidate

    return candidates[0] if candidates else ""


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
    apply_url = _direct_apply_url(result)
    publisher = result.get("job_publisher") or "Unknown"

    return RawPosting(
        source=f"jsearch:{publisher}",
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


def fetch(api_key: str, *, query: str = INTERN_QUERY) -> list[RawPosting]:
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
            "num_pages": "1",
            "country": "us",
            "date_posted": "week",
            "employment_types": "INTERN",
        },
        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": _HOST},
        timeout=10.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    jobs: list[dict[str, Any]] = (data.get("data") or {}).get("jobs") or []
    return [_to_raw_posting(r) for r in jobs]
