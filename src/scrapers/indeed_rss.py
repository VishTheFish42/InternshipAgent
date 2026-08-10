from __future__ import annotations

import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.scrapers.base import RawPosting, build_queries

_RATE_LIMIT_SECONDS = 2.0
_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"
_JOB_ID_RE = re.compile(r"jk=([a-zA-Z0-9]+)")


def _extract_job_id(entry: Any) -> str:
    """Pull Indeed's job key ('jk') out of the guid/link, falling back to the raw link."""
    candidate = entry.get("id") or entry.get("link", "")
    m = _JOB_ID_RE.search(candidate)
    return m.group(1) if m else str(candidate)


def _parse_title(title: str) -> tuple[str, str, str | None]:
    """Indeed RSS titles are formatted 'Job Title - Company - Location'."""
    parts = [p.strip() for p in title.split(" - ")]
    if len(parts) >= 3:
        return parts[0], parts[1], " - ".join(parts[2:])
    if len(parts) == 2:
        return parts[0], parts[1], None
    return title, "Unknown", None


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def _fetch_feed(query: str) -> Any:
    resp = httpx.get(
        "https://www.indeed.com/rss",
        params={"q": query, "l": "United States", "jt": "internship", "sort": "date"},
        headers={"User-Agent": _USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def _to_raw_posting(entry: Any) -> RawPosting:
    job_title, company, location = _parse_title(entry.get("title", ""))

    posted_at = None
    published = entry.get("published")
    if published:
        try:
            posted_at = parsedate_to_datetime(published).astimezone(UTC).replace(tzinfo=None)
        except (TypeError, ValueError):
            posted_at = None

    link = entry.get("link", "")
    return RawPosting(
        source="indeed",
        external_id=_extract_job_id(entry),
        title=job_title,
        company=company,
        location=location,
        is_remote=bool(location and "remote" in location.lower()),
        url=link,
        apply_url=link,
        description=entry.get("summary"),
        posted_at=posted_at,
    )


def fetch(query: str) -> list[RawPosting]:
    """Fetch and parse a single Indeed RSS query into RawPostings."""
    feed = _fetch_feed(query)
    return [_to_raw_posting(entry) for entry in feed.entries]


def fetch_all(queries: list[str] | None = None) -> list[RawPosting]:
    """Fetch every domain keyword query, rate-limited at 1 req/2s. A failing
    query is logged (via the caller) and skipped rather than aborting the run."""
    queries = queries if queries is not None else build_queries()
    postings: list[RawPosting] = []
    for i, query in enumerate(queries):
        if i > 0:
            time.sleep(_RATE_LIMIT_SECONDS)
        try:
            postings.extend(fetch(query))
        except httpx.HTTPError:
            continue
    return postings
