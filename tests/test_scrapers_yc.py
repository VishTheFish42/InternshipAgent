"""Unit tests for scrapers.yc — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from src.scrapers.yc import _matches_domain, _to_raw_posting, fetch

_SWE_JOB = {
    "id": 1001,
    "title": "Software Engineering Intern",
    "company_name": "Acme YC Co",
    "location": "Remote",
    "remote": True,
    "url": "https://workatastartup.com/jobs/1001",
    "description": "Build backend services.",
    "created_at": "2026-06-01T12:00:00Z",
}

_NON_DOMAIN_JOB = {
    "id": 1002,
    "title": "Marketing Intern",
    "company_name": "Widgets Inc",
    "location": "San Francisco, CA",
    "remote": False,
    "url": "https://workatastartup.com/jobs/1002",
    "description": "Help run social media campaigns.",
    "created_at": "2026-06-02T12:00:00Z",
}


# ── _matches_domain ───────────────────────────────────────────────────────────


def test_matches_domain_true_for_software_role():
    assert _matches_domain(_SWE_JOB)


def test_matches_domain_false_for_marketing_role():
    assert not _matches_domain(_NON_DOMAIN_JOB)


def test_matches_domain_checks_description_too():
    job = {"title": "Generalist Intern", "description": "Work on our machine learning pipeline."}
    assert _matches_domain(job)


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_SWE_JOB)
    assert posting.source == "yc"
    assert posting.external_id == "1001"
    assert posting.company == "Acme YC Co"
    assert posting.is_remote is True
    assert posting.apply_url == "https://workatastartup.com/jobs/1001"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_handles_missing_date():
    job = dict(_SWE_JOB)
    del job["created_at"]
    posting = _to_raw_posting(job)
    assert posting.posted_at is None


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_filters_to_domain_relevant_jobs():
    mock_response = MagicMock()
    mock_response.json.return_value = {"jobs": [_SWE_JOB, _NON_DOMAIN_JOB]}

    with patch("src.scrapers.yc.httpx.post", return_value=mock_response) as mock_post:
        postings = fetch()

    assert {p.external_id for p in postings} == {"1001"}
    mock_post.assert_called_once()


def test_fetch_returns_empty_when_no_jobs():
    mock_response = MagicMock()
    mock_response.json.return_value = {"jobs": []}

    with patch("src.scrapers.yc.httpx.post", return_value=mock_response):
        postings = fetch()

    assert postings == []
