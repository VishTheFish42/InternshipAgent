from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawPosting:
    """A single job posting as returned by any scraper, before dedup or scoring."""

    source: str
    external_id: str
    title: str
    company: str
    location: str | None
    is_remote: bool
    url: str
    apply_url: str | None
    description: str | None
    posted_at: datetime | None


# Core domain terms — every source runs all of these queries.
_CORE_TERMS = [
    "software engineering intern",
    "software intern",
    "data science intern",
    "machine learning intern",
    "AI intern",
    "artificial intelligence intern",
    "research intern computer science",
    "backend intern",
    "frontend intern",
    "full stack intern",
]

# Co-op / extended terms — catches roles that don't use the word "intern".
_COOP_TERMS = [
    "software engineering co-op",
    "software co-op",
    "machine learning co-op",
    "data science co-op",
]


def build_queries() -> list[str]:
    """
    Return the full list of domain-level keyword queries used by every Tier 2
    scraper. Deliberately broad and not filtered by season (summer/fall/spring)
    — the AI scorer handles fine-grained relevance and term filtering.
    """
    return [*_CORE_TERMS, *_COOP_TERMS]
