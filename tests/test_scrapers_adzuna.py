"""Unit tests for scrapers.adzuna — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from src.scrapers.adzuna import _to_raw_posting, fetch

_FAKE_RESULT = {
    "id": "998877",
    "title": "Software Engineering Intern",
    "company": {"display_name": "Stripe"},
    "location": {"display_name": "Remote"},
    "redirect_url": "https://www.adzuna.com/land/ad/998877",
    "description": "Great backend role.",
    "created": "2026-06-01T12:00:00Z",
}

_FAKE_RESULT_NO_LOCATION = {
    "id": "112233",
    "title": "Data Science Intern",
    "company": {"display_name": "Anthropic"},
    "redirect_url": "https://www.adzuna.com/land/ad/112233",
    "description": "Great ML role.",
    "created": "2026-06-02T12:00:00Z",
}


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_RESULT)
    assert posting.source == "adzuna"
    assert posting.external_id == "998877"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "Stripe"
    assert posting.location == "Remote"
    assert posting.is_remote is True
    assert posting.apply_url == "https://www.adzuna.com/land/ad/998877"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_handles_missing_location():
    posting = _to_raw_posting(_FAKE_RESULT_NO_LOCATION)
    assert posting.location is None
    assert posting.is_remote is False


def test_to_raw_posting_handles_missing_created_date():
    result = dict(_FAKE_RESULT)
    del result["created"]
    posting = _to_raw_posting(result)
    assert posting.posted_at is None


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_parses_results_and_sends_correct_params():
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": [_FAKE_RESULT, _FAKE_RESULT_NO_LOCATION]}

    with patch("src.scrapers.adzuna.httpx.get", return_value=mock_response) as mock_get:
        postings = fetch("app-id-123", "app-key-456")

    assert {p.external_id for p in postings} == {"998877", "112233"}

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["app_id"] == "app-id-123"
    assert kwargs["params"]["app_key"] == "app-key-456"
    assert kwargs["params"]["what"] == "internship"
    assert kwargs["params"]["where"] == "us"
    assert kwargs["params"]["full_time"] == 0


def test_fetch_makes_exactly_one_call_regardless_of_query_breadth():
    """Adzuna's free tier is 250 calls/month — this must never loop per keyword."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": []}

    with patch("src.scrapers.adzuna.httpx.get", return_value=mock_response) as mock_get:
        fetch("app-id", "app-key")

    mock_get.assert_called_once()


def test_fetch_returns_empty_list_when_no_results():
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": []}

    with patch("src.scrapers.adzuna.httpx.get", return_value=mock_response):
        postings = fetch("app-id", "app-key")

    assert postings == []
