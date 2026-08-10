"""Unit tests for scrapers.lever — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db import Base, CompanyLookup
from src.scrapers.lever import (
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


_FAKE_POSTINGS = [
    {
        "id": "abc-123",
        "text": "Software Engineering Intern",
        "categories": {"location": "Remote"},
        "hostedUrl": "https://jobs.lever.co/linear/abc-123",
        "applyUrl": "https://jobs.lever.co/linear/abc-123/apply",
        "descriptionPlain": "Great role",
        "createdAt": 1748000000000,
    },
    {
        "id": "def-456",
        "text": "Design Co-op",
        "categories": {"location": "New York, NY"},
        "hostedUrl": "https://jobs.lever.co/linear/def-456",
        "applyUrl": "https://jobs.lever.co/linear/def-456/apply",
        "descriptionPlain": "Another role",
        "createdAt": 1748100000000,
    },
    {
        "id": "ghi-789",
        "text": "Staff Software Engineer",
        "categories": {"location": "New York, NY"},
        "hostedUrl": "https://jobs.lever.co/linear/ghi-789",
        "applyUrl": "https://jobs.lever.co/linear/ghi-789/apply",
        "descriptionPlain": "Not an internship",
        "createdAt": 1748200000000,
    },
]


# ── _is_internship ───────────────────────────────────────────────────────────


def test_is_internship_matches_intern():
    assert _is_internship("Software Engineering Intern")


def test_is_internship_matches_coop():
    assert _is_internship("Design Co-op")


def test_is_internship_rejects_staff_role():
    assert not _is_internship("Staff Software Engineer")


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_POSTINGS[0], "linear")
    assert posting.source == "lever:linear"
    assert posting.external_id == "abc-123"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "linear"
    assert posting.is_remote is True
    assert posting.apply_url == "https://jobs.lever.co/linear/abc-123/apply"
    assert posting.posted_at == datetime(2025, 5, 23, 11, 33, 20)


def test_to_raw_posting_detects_non_remote():
    posting = _to_raw_posting(_FAKE_POSTINGS[1], "linear")
    assert posting.is_remote is False
    assert posting.location == "New York, NY"


def test_to_raw_posting_falls_back_to_hosted_url_when_no_apply_url():
    job = dict(_FAKE_POSTINGS[0])
    del job["applyUrl"]
    posting = _to_raw_posting(job, "linear")
    assert posting.apply_url == "https://jobs.lever.co/linear/abc-123"


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_filters_to_internships_only():
    with patch("src.scrapers.lever._fetch_postings_for_slug", return_value=_FAKE_POSTINGS):
        postings = fetch("linear")
    assert len(postings) == 2
    assert {p.external_id for p in postings} == {"abc-123", "def-456"}


def test_fetch_returns_empty_when_no_matches():
    with patch("src.scrapers.lever._fetch_postings_for_slug", return_value=[_FAKE_POSTINGS[2]]):
        postings = fetch("linear")
    assert postings == []


# ── fetch_for_resolved_companies ─────────────────────────────────────────────


def test_fetch_for_resolved_companies_only_queries_resolved_lever(session):
    session.add_all(
        [
            CompanyLookup(name_raw="Linear", ats_type="lever", slug="linear", status="resolved"),
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(name_raw="Acme", ats_type="lever", slug="acme", status="unresolved"),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.lever.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.lever.time.sleep") as mock_sleep,
    ):
        fetch_for_resolved_companies(session)

    mock_fetch.assert_called_once_with("linear")
    mock_sleep.assert_not_called()


def test_fetch_for_resolved_companies_rate_limits_between_calls(session):
    session.add_all(
        [
            CompanyLookup(name_raw="Linear", ats_type="lever", slug="linear", status="resolved"),
            CompanyLookup(name_raw="Notion", ats_type="lever", slug="notionhq", status="resolved"),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.lever.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.lever.time.sleep") as mock_sleep,
    ):
        fetch_for_resolved_companies(session)

    assert mock_fetch.call_count == 2
    mock_sleep.assert_called_once_with(5.0)


def test_fetch_for_resolved_companies_skips_source_on_http_error(session):
    import httpx

    session.add_all(
        [
            CompanyLookup(name_raw="Linear", ats_type="lever", slug="linear", status="resolved"),
            CompanyLookup(name_raw="Notion", ats_type="lever", slug="notionhq", status="resolved"),
        ]
    )
    session.commit()

    def fake_fetch(slug: str) -> list:
        if slug == "linear":
            raise httpx.HTTPError("boom")
        return []

    with (
        patch("src.scrapers.lever.fetch", side_effect=fake_fetch),
        patch("src.scrapers.lever.time.sleep"),
    ):
        postings = fetch_for_resolved_companies(session)

    assert postings == []
