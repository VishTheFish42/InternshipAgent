from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db import JobPosting, upsert_posting

_TRACKING_PARAM_RE = re.compile(
    r"^(utm_|gh_src|gh_jid|lever[-_]?source|ref|source|trk|fbclid|gclid|mc_[a-z]+)",
    re.IGNORECASE,
)
_FUZZY_WINDOW_DAYS = 7
_DESCRIPTION_SIMILARITY_THRESHOLD = 0.8


def normalize_url(url: str) -> str:
    """Strip tracking params, trailing slashes, and fragments so equivalent URLs compare equal."""
    parsed = urlparse(url)
    kept_params = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not _TRACKING_PARAM_RE.match(k)
    )
    path = parsed.path.rstrip("/") or "/"
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=path,
        query=urlencode(kept_params),
        fragment="",
    )
    return urlunparse(normalized)


def _normalize_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ── Dedup layers ──────────────────────────────────────────────────────────────


def find_source_duplicate(session: Session, source: str, external_id: str) -> JobPosting | None:
    """Layer 1: exact (source, external_id) match — the DB's UNIQUE constraint."""
    return session.execute(
        select(JobPosting).where(
            JobPosting.source == source,
            JobPosting.external_id == external_id,
        )
    ).scalar_one_or_none()


def find_url_duplicate(session: Session, apply_url_normalized: str | None) -> JobPosting | None:
    """Layer 2: same normalized application URL found via a different source."""
    if not apply_url_normalized:
        return None
    return (
        session.execute(
            select(JobPosting).where(JobPosting.apply_url_normalized == apply_url_normalized)
        )
        .scalars()
        .first()
    )


def _description_similar_enough(a: str | None, b: str | None) -> bool:
    if not a or not b:
        # Nothing to compare on one side — fall back to trusting the
        # company+title match alone as evidence of a duplicate.
        return True
    return SequenceMatcher(None, a, b).ratio() >= _DESCRIPTION_SIMILARITY_THRESHOLD


def find_fuzzy_duplicate(
    session: Session,
    company_normalized: str,
    title_normalized: str,
    description: str | None = None,
    *,
    now: datetime | None = None,
) -> JobPosting | None:
    """
    Layer 3: same normalized (company, title) within a rolling window, confirmed
    by description similarity. Two genuinely different roles with the same title
    at the same company (e.g. different locations) are kept apart when their
    descriptions diverge significantly.
    """
    if not company_normalized or not title_normalized:
        return None
    cutoff = (now or _now()) - timedelta(days=_FUZZY_WINDOW_DAYS)
    candidates = (
        session.execute(
            select(JobPosting).where(
                JobPosting.company_normalized == company_normalized,
                JobPosting.title_normalized == title_normalized,
                JobPosting.found_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if _description_similar_enough(description, candidate.description):
            return candidate
    return None


# ── Public entrypoint ─────────────────────────────────────────────────────────


def upsert_deduped(session: Session, data: dict[str, Any]) -> tuple[JobPosting, bool, str]:
    """
    Insert a posting through the full three-layer dedup pipeline:
      1. (source, external_id) exact match
      2. Normalized apply_url match across sources
      3. Fuzzy (company, title, recency) match confirmed by description similarity

    Returns (posting, was_new, reason). reason is one of:
    'new', 'duplicate_source_id', 'duplicate_url', 'duplicate_fuzzy'.
    """
    source_dup = find_source_duplicate(session, data["source"], data["external_id"])
    if source_dup is not None:
        return source_dup, False, "duplicate_source_id"

    apply_url = data.get("apply_url")
    apply_url_normalized = normalize_url(apply_url) if apply_url else None

    url_dup = find_url_duplicate(session, apply_url_normalized)
    if url_dup is not None:
        return url_dup, False, "duplicate_url"

    company_normalized = _normalize_text(data.get("company"))
    title_normalized = _normalize_text(data.get("title"))

    fuzzy_dup = find_fuzzy_duplicate(
        session, company_normalized, title_normalized, data.get("description")
    )
    if fuzzy_dup is not None:
        return fuzzy_dup, False, "duplicate_fuzzy"

    kwargs = dict(data)
    kwargs["apply_url_normalized"] = apply_url_normalized
    kwargs.setdefault("company_normalized", company_normalized)
    kwargs.setdefault("title_normalized", title_normalized)
    posting, was_new = upsert_posting(session, kwargs)
    return posting, was_new, "new"
