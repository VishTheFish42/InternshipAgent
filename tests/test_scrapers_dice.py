"""Unit tests for scrapers.dice — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from src.scrapers.dice import _to_raw_posting, fetch

_FAKE_JOB = {
    "id": "5566",
    "title": "Software Engineering Intern",
    "companyName": "Defense Corp",
    "jobLocation": {"displayName": "Remote"},
    "isRemote": True,
    "detailsPageUrl": "https://www.dice.com/jobs/detail/5566",
    "summary": "Great backend role.",
    "postedDate": "2026-06-01T12:00:00Z",
}


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_JOB)
    assert posting.source == "dice"
    assert posting.external_id == "5566"
    assert posting.company == "Defense Corp"
    assert posting.location == "Remote"
    assert posting.is_remote is True
    assert posting.apply_url == "https://www.dice.com/jobs/detail/5566"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_handles_missing_location():
    job = dict(_FAKE_JOB)
    job["jobLocation"] = None
    job["isRemote"] = False
    posting = _to_raw_posting(job)
    assert posting.location is None
    assert posting.is_remote is False


def test_to_raw_posting_handles_missing_date():
    job = dict(_FAKE_JOB)
    del job["postedDate"]
    posting = _to_raw_posting(job)
    assert posting.posted_at is None


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_parses_results():
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [_FAKE_JOB]}

    with patch("src.scrapers.dice.httpx.get", return_value=mock_response) as mock_get:
        postings = fetch("software engineering intern", api_key="test-key")

    assert len(postings) == 1
    assert postings[0].external_id == "5566"

    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["x-api-key"] == "test-key"
    assert kwargs["params"]["employmentType"] == "INTERN"


def test_fetch_omits_api_key_header_when_none():
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}

    with patch("src.scrapers.dice.httpx.get", return_value=mock_response) as mock_get:
        fetch("internship")

    _, kwargs = mock_get.call_args
    assert "x-api-key" not in kwargs["headers"]


def test_fetch_returns_empty_when_no_results():
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}

    with patch("src.scrapers.dice.httpx.get", return_value=mock_response):
        postings = fetch("internship")

    assert postings == []
