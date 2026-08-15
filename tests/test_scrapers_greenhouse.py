"""Unit tests for scrapers.greenhouse — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db import Base, CompanyLookup
from src.scrapers.greenhouse import (
    _is_internship,
    _to_raw_posting,
    fetch,
    fetch_for_resolved_companies,
)

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


_FAKE_JOBS = [
    {
        "id": 111,
        "title": "Software Engineering Intern",
        "location": {"name": "Remote"},
        "absolute_url": "https://boards.greenhouse.io/stripe/jobs/111",
        "content": "<p>Great role</p>",
        "updated_at": "2026-06-01T12:00:00-00:00",
    },
    {
        "id": 222,
        "title": "Data Engineering Co-op",
        "location": {"name": "San Francisco, CA"},
        "absolute_url": "https://boards.greenhouse.io/stripe/jobs/222",
        "content": "<p>Another role</p>",
        "updated_at": "2026-06-02T12:00:00-00:00",
    },
    {
        "id": 333,
        "title": "Senior Staff Software Engineer",
        "location": {"name": "New York, NY"},
        "absolute_url": "https://boards.greenhouse.io/stripe/jobs/333",
        "content": "<p>Not an internship</p>",
        "updated_at": "2026-06-03T12:00:00-00:00",
    },
]


# ── _is_internship ───────────────────────────────────────────────────────────


def test_is_internship_matches_intern():
    assert _is_internship("Software Engineering Intern")


def test_is_internship_matches_coop_with_hyphen():
    assert _is_internship("Data Co-op")


def test_is_internship_matches_coop_without_hyphen():
    assert _is_internship("Data Coop")


def test_is_internship_rejects_senior_role():
    assert not _is_internship("Senior Staff Software Engineer")


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_JOBS[0], "stripe")
    assert posting.source == "greenhouse:stripe"
    assert posting.external_id == "111"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "stripe"
    assert posting.is_remote is True
    assert posting.apply_url == "https://boards.greenhouse.io/stripe/jobs/111"
    assert posting.posted_at == datetime.fromisoformat("2026-06-01T12:00:00+00:00")


def test_to_raw_posting_detects_non_remote():
    posting = _to_raw_posting(_FAKE_JOBS[1], "stripe")
    assert posting.is_remote is False
    assert posting.location == "San Francisco, CA"


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_filters_to_internships_only():
    with patch("src.scrapers.greenhouse._fetch_jobs_for_slug", return_value=_FAKE_JOBS):
        postings = fetch("stripe")
    assert len(postings) == 2
    assert {p.external_id for p in postings} == {"111", "222"}


def test_fetch_returns_empty_when_no_matches():
    non_intern_jobs = [_FAKE_JOBS[2]]
    with patch("src.scrapers.greenhouse._fetch_jobs_for_slug", return_value=non_intern_jobs):
        postings = fetch("stripe")
    assert postings == []


# ── fetch_for_resolved_companies ─────────────────────────────────────────────


def test_fetch_for_resolved_companies_only_queries_resolved_greenhouse(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(name_raw="Linear", ats_type="lever", slug="linear", status="resolved"),
            CompanyLookup(name_raw="Acme", ats_type="greenhouse", slug="acme", status="unresolved"),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.greenhouse.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.greenhouse.time.sleep") as mock_sleep,
    ):
        fetch_for_resolved_companies(session)

    mock_fetch.assert_called_once_with("stripe")
    mock_sleep.assert_not_called()


def test_fetch_for_resolved_companies_rate_limits_between_calls(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(
                name_raw="Airbnb", ats_type="greenhouse", slug="airbnb", status="resolved"
            ),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.greenhouse.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.greenhouse.time.sleep") as mock_sleep,
    ):
        fetch_for_resolved_companies(session)

    assert mock_fetch.call_count == 2
    mock_sleep.assert_called_once_with(5.0)


def test_fetch_for_resolved_companies_skips_source_on_http_error(session):
    import httpx

    session.add_all(
        [
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(
                name_raw="Airbnb", ats_type="greenhouse", slug="airbnb", status="resolved"
            ),
        ]
    )
    session.commit()

    def fake_fetch(slug: str) -> list:
        if slug == "stripe":
            raise httpx.HTTPError("boom")
        return ["airbnb-posting"]  # type: ignore[list-item]

    with (
        patch("src.scrapers.greenhouse.fetch", side_effect=fake_fetch) as mock_fetch,
        patch("src.scrapers.greenhouse.time.sleep"),
    ):
        postings = fetch_for_resolved_companies(session)

    # airbnb must still be fetched (and its posting returned) despite stripe's
    # failure — a bad company earlier in the list must not abort the rest.
    mock_fetch.assert_any_call("stripe")
    mock_fetch.assert_any_call("airbnb")
    assert len(postings) == 1


def test_fetch_for_resolved_companies_continues_past_exhausted_retry(session):
    """The real failure mode: _fetch_jobs_for_slug's @retry wraps a repeated
    httpx.HTTPError in tenacity.RetryError once stop_after_attempt(3) is
    exhausted, not the original HTTPError — so RetryError, not just
    HTTPError, must be caught per-company or one stale/incorrect cached ATS
    slug silently aborts every remaining company in this loop for the whole
    cycle (found via a live --run-once against a ~350-company list)."""
    from tenacity import RetryError

    session.add_all(
        [
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(
                name_raw="Airbnb", ats_type="greenhouse", slug="airbnb", status="resolved"
            ),
        ]
    )
    session.commit()

    def fake_fetch(slug: str) -> list:
        if slug == "stripe":
            raise RetryError(last_attempt=None)  # type: ignore[arg-type]
        return ["airbnb-posting"]  # type: ignore[list-item]

    with (
        patch("src.scrapers.greenhouse.fetch", side_effect=fake_fetch) as mock_fetch,
        patch("src.scrapers.greenhouse.time.sleep"),
    ):
        postings = fetch_for_resolved_companies(session)

    mock_fetch.assert_any_call("airbnb")
    assert len(postings) == 1
