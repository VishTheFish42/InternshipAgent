from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.db import CompanyLookup

_ATS_MAP_PATH = Path(__file__).parent / "data" / "company_ats_map.json"


@dataclass
class CompanyRecord:
    name_raw: str
    name_normalized: str
    ats_type: str | None  # 'greenhouse' | 'lever' | 'workday' | 'custom' | None
    slug: str | None
    url: str | None
    status: str  # 'resolved' | 'unresolved'
    source: str | None  # 'bundled_table' | 'web_search' | 'manual'


# ── Helpers ───────────────────────────────────────────────────────────────────


def _normalize(name: str) -> str:
    """Lowercase and collapse whitespace — matches keys in company_ats_map.json."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@lru_cache(maxsize=1)
def _load_ats_map() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_ATS_MAP_PATH.read_text(encoding="utf-8"))
    return data


# ── Web search steps ──────────────────────────────────────────────────────────


def _serpapi(query: str, api_key: str, num: int = 5) -> list[dict[str, Any]]:
    """Return SerpAPI organic results, or [] on any failure."""
    try:
        resp = httpx.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": query, "api_key": api_key, "num": num},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("organic_results", [])  # type: ignore[no-any-return]
    except Exception:
        return []


def _search_greenhouse(company: str, api_key: str) -> str | None:
    """Return Greenhouse slug from web search, or None."""
    results = _serpapi(f'"{company}" site:boards.greenhouse.io', api_key)
    for r in results:
        m = re.search(r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)", r.get("link", ""))
        if m:
            return m.group(1)
    return None


def _search_lever(company: str, api_key: str) -> str | None:
    """Return Lever slug from web search, or None."""
    results = _serpapi(f'"{company}" site:jobs.lever.co', api_key)
    for r in results:
        m = re.search(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", r.get("link", ""))
        if m:
            return m.group(1)
    return None


def _search_careers(company: str, api_key: str) -> str | None:
    """Return a generic careers-page URL from web search, or None."""
    results = _serpapi(f'"{company}" internship careers', api_key)
    for r in results:
        link = r.get("link", "")
        if re.search(r"career|job", link, re.IGNORECASE):
            return str(link)
    return None


# ── DB persistence ────────────────────────────────────────────────────────────


def _upsert_company(session: Session, record: CompanyRecord) -> CompanyLookup:
    """Insert or update a CompanyLookup row from a CompanyRecord."""
    now = _now()
    existing = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == record.name_raw)
    ).scalar_one_or_none()

    if existing is not None:
        existing.name_normalized = record.name_normalized
        existing.ats_type = record.ats_type
        existing.slug = record.slug
        existing.url = record.url
        existing.status = record.status
        existing.source = record.source
        existing.last_attempted = now
        if record.status == "resolved" and existing.resolved_at is None:
            existing.resolved_at = now
        session.flush()
        return existing

    row = CompanyLookup(
        name_raw=record.name_raw,
        name_normalized=record.name_normalized,
        ats_type=record.ats_type,
        slug=record.slug,
        url=record.url,
        status=record.status,
        source=record.source,
        last_attempted=now,
        resolved_at=now if record.status == "resolved" else None,
    )
    session.add(row)
    session.flush()
    return row


# ── Public API ────────────────────────────────────────────────────────────────


def discover(
    company_name: str,
    session: Session,
    search_api_key: str | None = None,
    *,
    force: bool = False,
) -> CompanyRecord:
    """
    Resolve a company name to its ATS endpoint via a 4-step pipeline.

      1. DB cache (skip on force=True — used by weekly re-attempt)
      2. Bundled lookup table (company_ats_map.json)
      3. Web search: Greenhouse boards, then Lever jobs
      4. Web search: generic careers page
      → Mark as 'unresolved' if all steps fail

    Always returns a CompanyRecord; check .status == 'resolved' for success.
    Results are persisted to the DB so repeated calls are free.
    Steps 3–4 are skipped when search_api_key is None.
    """
    name_normalized = _normalize(company_name)

    # Step 0: DB cache
    if not force:
        cached = session.execute(
            select(CompanyLookup).where(
                CompanyLookup.name_normalized == name_normalized,
                CompanyLookup.status == "resolved",
            )
        ).scalar_one_or_none()
        if cached is not None:
            return CompanyRecord(
                name_raw=cached.name_raw,
                name_normalized=cached.name_normalized or name_normalized,
                ats_type=cached.ats_type,
                slug=cached.slug,
                url=cached.url,
                status=cached.status,
                source=cached.source,
            )

    # Step 1: Bundled ATS map
    entry = _load_ats_map().get(name_normalized)
    if entry is not None:
        record = CompanyRecord(
            name_raw=company_name,
            name_normalized=name_normalized,
            ats_type=entry.get("ats"),
            slug=entry.get("slug"),
            url=entry.get("url"),
            status="resolved",
            source="bundled_table",
        )
        _upsert_company(session, record)
        return record

    # Steps 2–4 require a search API key
    if search_api_key:
        # Step 2a: Greenhouse
        slug = _search_greenhouse(company_name, search_api_key)
        if slug:
            record = CompanyRecord(
                name_raw=company_name,
                name_normalized=name_normalized,
                ats_type="greenhouse",
                slug=slug,
                url=None,
                status="resolved",
                source="web_search",
            )
            _upsert_company(session, record)
            return record

        # Step 2b: Lever
        slug = _search_lever(company_name, search_api_key)
        if slug:
            record = CompanyRecord(
                name_raw=company_name,
                name_normalized=name_normalized,
                ats_type="lever",
                slug=slug,
                url=None,
                status="resolved",
                source="web_search",
            )
            _upsert_company(session, record)
            return record

        # Step 3: Generic careers page
        url = _search_careers(company_name, search_api_key)
        if url:
            record = CompanyRecord(
                name_raw=company_name,
                name_normalized=name_normalized,
                ats_type="custom",
                slug=None,
                url=url,
                status="resolved",
                source="web_search",
            )
            _upsert_company(session, record)
            return record

    # Step 4: Unresolved
    record = CompanyRecord(
        name_raw=company_name,
        name_normalized=name_normalized,
        ats_type=None,
        slug=None,
        url=None,
        status="unresolved",
        source="web_search" if search_api_key else None,
    )
    _upsert_company(session, record)
    return record


@dataclass
class RetryResult:
    attempted: int
    resolved: int
    still_unresolved: int


def retry_unresolved(
    session: Session,
    search_api_key: str | None = None,
    *,
    min_age_days: int = 7,
) -> RetryResult:
    """
    Re-attempt discovery for all unresolved companies not tried in the last
    min_age_days days. Intended to be called from the weekly digest job (T-703).
    Returns counts of how many were attempted and how many resolved.
    """
    cutoff = _now() - timedelta(days=min_age_days)
    rows = list(
        session.execute(
            select(CompanyLookup).where(
                CompanyLookup.status == "unresolved",
                or_(
                    CompanyLookup.last_attempted.is_(None),
                    CompanyLookup.last_attempted < cutoff,
                ),
            )
        ).scalars()
    )

    resolved_count = 0
    for row in rows:
        result = discover(row.name_raw, session, search_api_key, force=True)
        if result.status == "resolved":
            resolved_count += 1

    return RetryResult(
        attempted=len(rows),
        resolved=resolved_count,
        still_unresolved=len(rows) - resolved_count,
    )
