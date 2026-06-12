"""Unit tests for company_discoverer — all web calls are mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.company_discoverer import CompanyRecord, RetryResult, _normalize, discover, retry_unresolved
from src.db import Base, CompanyLookup


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


# ── Normalisation ─────────────────────────────────────────────────────────────


def test_normalize_lowercases():
    assert _normalize("Stripe") == "stripe"


def test_normalize_strips_whitespace():
    assert _normalize("  Scale AI  ") == "scale ai"


def test_normalize_collapses_internal_spaces():
    assert _normalize("Palo  Alto  Networks") == "palo alto networks"


def test_normalize_preserves_dots_and_ampersands():
    assert _normalize("Monday.com") == "monday.com"
    assert _normalize("Weights & Biases") == "weights & biases"


# ── Step 1: Bundled table hits ────────────────────────────────────────────────


def test_bundled_table_greenhouse(session):
    record = discover("Stripe", session)
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"
    assert record.slug == "stripe"
    assert record.source == "bundled_table"


def test_bundled_table_lever(session):
    record = discover("Linear", session)
    assert record.status == "resolved"
    assert record.ats_type == "lever"
    assert record.slug == "linear"


def test_bundled_table_custom_has_scraper(session):
    record = discover("Google", session)
    assert record.status == "resolved"
    assert record.ats_type == "custom"


def test_bundled_table_workday(session):
    record = discover("Salesforce", session)
    assert record.status == "resolved"
    assert record.ats_type == "workday"
    assert record.slug is None


def test_bundled_table_case_insensitive(session):
    record = discover("STRIPE", session)
    assert record.status == "resolved"
    assert record.slug == "stripe"


def test_bundled_table_multiword(session):
    record = discover("Palo Alto Networks", session)
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"


# ── Step 0: DB cache ──────────────────────────────────────────────────────────


def test_db_cache_returns_existing_resolved(session):
    # First call populates the DB
    r1 = discover("Stripe", session)
    assert r1.source == "bundled_table"

    # Second call should hit the DB cache without re-reading the ATS map
    with patch("src.company_discoverer._load_ats_map") as mock_map:
        r2 = discover("Stripe", session)
    mock_map.assert_not_called()
    assert r2.status == "resolved"
    assert r2.slug == "stripe"


def test_force_bypasses_cache(session):
    discover("Stripe", session)
    session.commit()

    with patch("src.company_discoverer._load_ats_map", return_value={"stripe": {"ats": "greenhouse", "slug": "stripe-new"}}) as mock_map:
        r = discover("Stripe", session, force=True)
    mock_map.assert_called_once()
    assert r.slug == "stripe-new"


# ── Step 2a: Greenhouse web search ────────────────────────────────────────────


def test_web_search_greenhouse_hit(session):
    fake_results = [{"link": "https://boards.greenhouse.io/acmecorp/jobs/123"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        record = discover("Acme Corp", session, search_api_key="fake-key")
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"
    assert record.slug == "acmecorp"
    assert record.source == "web_search"


def test_web_search_greenhouse_extracts_slug_only(session):
    fake_results = [{"link": "https://boards.greenhouse.io/widgetsinc/jobs/9999?gh_src=xyz"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        record = discover("Widgets Inc", session, search_api_key="k")
    assert record.slug == "widgetsinc"


# ── Step 2b: Lever web search ─────────────────────────────────────────────────


def test_web_search_lever_hit(session):
    def fake_serpapi(query: str, api_key: str, num: int = 5):
        if "greenhouse" in query:
            return []
        return [{"link": "https://jobs.lever.co/acmecorp/abc-123"}]

    with patch("src.company_discoverer._serpapi", side_effect=fake_serpapi):
        record = discover("Acme Corp", session, search_api_key="k")
    assert record.ats_type == "lever"
    assert record.slug == "acmecorp"


# ── Step 3: Generic careers search ───────────────────────────────────────────


def test_web_search_careers_hit(session):
    def fake_serpapi(query: str, api_key: str, num: int = 5):
        if "greenhouse" in query or "lever" in query:
            return []
        return [{"link": "https://careers.acmecorp.com/internships"}]

    with patch("src.company_discoverer._serpapi", side_effect=fake_serpapi):
        record = discover("Acme Corp", session, search_api_key="k")
    assert record.ats_type == "custom"
    assert record.url == "https://careers.acmecorp.com/internships"
    assert record.slug is None


# ── Step 4: Unresolved ────────────────────────────────────────────────────────


def test_unresolved_when_all_searches_fail(session):
    with patch("src.company_discoverer._serpapi", return_value=[]):
        record = discover("Unknown Corp XYZ", session, search_api_key="k")
    assert record.status == "unresolved"
    assert record.source == "web_search"


def test_unresolved_when_no_api_key(session):
    record = discover("Unknown Corp XYZ", session, search_api_key=None)
    assert record.status == "unresolved"
    assert record.source is None


# ── DB persistence ────────────────────────────────────────────────────────────


def test_result_persisted_to_db(session):
    discover("Stripe", session)
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Stripe")
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "resolved"
    assert row.slug == "stripe"
    assert row.resolved_at is not None


def test_unresolved_persisted_to_db(session):
    with patch("src.company_discoverer._serpapi", return_value=[]):
        discover("Ghost Company", session, search_api_key="k")
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Ghost Company")
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "unresolved"
    assert row.resolved_at is None


def test_second_call_updates_existing_row(session):
    with patch("src.company_discoverer._serpapi", return_value=[]):
        discover("Ghost Company", session, search_api_key="k")
    session.commit()

    fake_gh = [{"link": "https://boards.greenhouse.io/ghostco/jobs/1"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_gh):
        discover("Ghost Company", session, search_api_key="k", force=True)
    session.commit()

    rows = list(session.execute(select(CompanyLookup).where(CompanyLookup.name_raw == "Ghost Company")).scalars())
    assert len(rows) == 1
    assert rows[0].status == "resolved"
    assert rows[0].slug == "ghostco"


# ── retry_unresolved ──────────────────────────────────────────────────────────


def _unresolved_row(name: str, last_attempted: datetime | None = None) -> CompanyLookup:
    return CompanyLookup(
        name_raw=name,
        name_normalized=name.lower(),
        status="unresolved",
        last_attempted=last_attempted,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def test_retry_no_candidates(session):
    result = retry_unresolved(session)
    assert result == RetryResult(attempted=0, resolved=0, still_unresolved=0)


def test_retry_skips_resolved_companies(session):
    discover("Stripe", session)
    session.commit()
    result = retry_unresolved(session, min_age_days=0)
    assert result.attempted == 0


def test_retry_skips_recently_attempted(session):
    session.add(_unresolved_row("Fresh Corp", last_attempted=_now()))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]) as mock_search:
        result = retry_unresolved(session, search_api_key="k", min_age_days=7)
    mock_search.assert_not_called()
    assert result.attempted == 0


def test_retry_attempts_stale_company(session):
    stale_time = _now() - timedelta(days=8)
    session.add(_unresolved_row("Stale Corp", last_attempted=stale_time))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k", min_age_days=7)
    assert result.attempted == 1
    assert result.resolved == 0
    assert result.still_unresolved == 1


def test_retry_attempts_null_last_attempted(session):
    session.add(_unresolved_row("Unknown Corp"))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k")
    assert result.attempted == 1


def test_retry_resolves_on_success(session):
    session.add(_unresolved_row("Lucky Corp"))
    session.commit()
    fake_results = [{"link": "https://boards.greenhouse.io/luckycorp/jobs/1"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        result = retry_unresolved(session, search_api_key="k")
    assert result.attempted == 1
    assert result.resolved == 1
    assert result.still_unresolved == 0
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Lucky Corp")
    ).scalar_one()
    assert row.status == "resolved"
    assert row.slug == "luckycorp"


def test_retry_updates_last_attempted_even_on_failure(session):
    session.add(_unresolved_row("Bad Corp"))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        retry_unresolved(session, search_api_key="k")
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Bad Corp")
    ).scalar_one()
    assert row.last_attempted is not None
    assert row.last_attempted >= _now() - timedelta(seconds=5)


def test_retry_mixed_results(session):
    session.add(_unresolved_row("Lucky Corp"))
    session.add(_unresolved_row("Bad Corp"))
    session.commit()

    def fake_serpapi(query: str, api_key: str, num: int = 5) -> list:
        if "Lucky Corp" in query and "greenhouse" in query:
            return [{"link": "https://boards.greenhouse.io/luckycorp/jobs/1"}]
        return []

    with patch("src.company_discoverer._serpapi", side_effect=fake_serpapi):
        result = retry_unresolved(session, search_api_key="k")
    assert result.attempted == 2
    assert result.resolved == 1
    assert result.still_unresolved == 1


def test_retry_min_age_days_zero_includes_all_unresolved(session):
    session.add(_unresolved_row("Corp A", last_attempted=_now()))
    session.add(_unresolved_row("Corp B", last_attempted=_now() - timedelta(days=1)))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k", min_age_days=0)
    assert result.attempted == 2
