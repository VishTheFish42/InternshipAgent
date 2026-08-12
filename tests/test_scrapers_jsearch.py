"""Unit tests for scrapers.jsearch — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from src.scrapers.jsearch import _to_raw_posting, fetch

_FAKE_RESULT = {
    "job_id": "abc123",
    "job_title": "Software Engineering Intern",
    "employer_name": "Stripe",
    "job_city": "San Francisco",
    "job_state": "CA",
    "job_is_remote": False,
    "job_apply_link": "https://www.linkedin.com/jobs/view/abc123",
    "job_description": "Great backend role.",
    "job_posted_at_datetime_utc": "2026-06-01T12:00:00.000Z",
}

_FAKE_RESULT_REMOTE_NO_LOCATION = {
    "job_id": "def456",
    "job_title": "Data Science Intern",
    "employer_name": "Anthropic",
    "job_is_remote": True,
    "job_apply_link": "https://www.linkedin.com/jobs/view/def456",
    "job_description": "Great ML role.",
    "job_posted_at_datetime_utc": "2026-06-02T12:00:00.000Z",
}


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_RESULT)
    assert posting.source == "jsearch"
    assert posting.external_id == "abc123"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "Stripe"
    assert posting.location == "San Francisco, CA"
    assert posting.is_remote is False
    assert posting.apply_url == "https://www.linkedin.com/jobs/view/abc123"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_handles_remote_and_missing_location():
    posting = _to_raw_posting(_FAKE_RESULT_REMOTE_NO_LOCATION)
    assert posting.location is None
    assert posting.is_remote is True


def test_to_raw_posting_handles_missing_posted_date():
    result = dict(_FAKE_RESULT)
    del result["job_posted_at_datetime_utc"]
    posting = _to_raw_posting(result)
    assert posting.posted_at is None


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_parses_results_and_sends_correct_params_and_headers():
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": [_FAKE_RESULT, _FAKE_RESULT_REMOTE_NO_LOCATION]}

    with patch("src.scrapers.jsearch.httpx.get", return_value=mock_response) as mock_get:
        postings = fetch("rapidapi-key-123")

    assert {p.external_id for p in postings} == {"abc123", "def456"}

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["employment_types"] == "INTERN"
    assert kwargs["headers"]["X-RapidAPI-Key"] == "rapidapi-key-123"
    assert kwargs["headers"]["X-RapidAPI-Host"] == "jsearch.p.rapidapi.com"


def test_fetch_makes_exactly_one_call_regardless_of_query_breadth():
    """JSearch meters every request against a monthly quota — this must
    never loop per keyword like Indeed RSS does."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}

    with patch("src.scrapers.jsearch.httpx.get", return_value=mock_response) as mock_get:
        fetch("rapidapi-key")

    mock_get.assert_called_once()


def test_fetch_returns_empty_list_when_no_results():
    mock_response = MagicMock()
    mock_response.json.return_value = {"data": []}

    with patch("src.scrapers.jsearch.httpx.get", return_value=mock_response):
        postings = fetch("rapidapi-key")

    assert postings == []
