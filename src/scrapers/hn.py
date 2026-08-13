"""HackerNews 'Who is Hiring?' scraper via the Algolia HN Search API (no auth)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from src.scrapers.base import RawPosting

_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
_INTERN_RE = re.compile(r"\bintern(ship)?\b|\bco-?op\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text or "").strip()


def _is_internship_comment(text: str) -> bool:
    return bool(_INTERN_RE.search(text))


def find_current_thread_id() -> str | None:
    """Find the current 'Ask HN: Who is hiring?' thread by searching the
    official whoishiring account's stories, most recent first."""
    resp = httpx.get(
        _SEARCH_URL,
        params={"tags": "story,author_whoishiring", "query": "Who is Hiring"},
        timeout=10.0,
    )
    resp.raise_for_status()
    hits: list[dict[str, Any]] = resp.json().get("hits", [])
    for hit in hits:
        if "who is hiring" in hit.get("title", "").lower():
            return str(hit["objectID"])
    return None


def fetch_comments(story_id: str) -> list[dict[str, Any]]:
    """Fetch all comments on a thread, newest first (re-run daily to catch new replies)."""
    resp = httpx.get(
        _SEARCH_URL,
        params={"tags": f"comment,story_{story_id}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    hits: list[dict[str, Any]] = resp.json().get("hits", [])
    return hits


def _extract_company(text: str) -> str:
    """HN hiring comments conventionally lead with 'Company | Location | ...'."""
    first_line = text.split("\n")[0]
    parts = [p.strip() for p in re.split(r"[|–—]", first_line) if p.strip()]
    return parts[0][:80] if parts else "Unknown (HN)"


def _to_raw_posting(comment: dict[str, Any]) -> RawPosting:
    text = _strip_html(comment.get("comment_text") or "")
    object_id = str(comment.get("objectID"))
    hn_link = f"https://news.ycombinator.com/item?id={object_id}"

    url_match = _URL_RE.search(text)
    apply_url = url_match.group(0).rstrip(").,") if url_match else hn_link

    posted_at = None
    created_at = comment.get("created_at")
    if created_at:
        try:
            posted_at = (
                datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                .astimezone(UTC)
                .replace(tzinfo=None)
            )
        except ValueError:
            posted_at = None

    return RawPosting(
        source="hn",
        external_id=object_id,
        title="Internship (via HN Who's Hiring)",
        company=_extract_company(text),
        location=None,
        is_remote="remote" in text.lower(),
        url=hn_link,
        apply_url=apply_url,
        description=text[:2000],
        posted_at=posted_at,
    )


def fetch_current_month_postings() -> list[RawPosting]:
    """Find this month's thread and extract every comment that mentions an internship or co-op."""
    thread_id = find_current_thread_id()
    if thread_id is None:
        return []

    comments = fetch_comments(thread_id)
    intern_comments = [
        c for c in comments if _is_internship_comment(_strip_html(c.get("comment_text") or ""))
    ]
    return [_to_raw_posting(c) for c in intern_comments]
