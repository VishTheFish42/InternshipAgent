"""Unit tests for scrapers.jsearch — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from src.scrapers.jsearch import _to_raw_posting, fetch

_FAKE_RESULT = {
    "job_id": "abc123",
    "job_title": "Software Engineering Intern",
    "employer_name": "Stripe",
    "job_publisher": "LinkedIn",
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
    "job_publisher": "ZipRecruiter",
    "job_is_remote": True,
    "job_apply_link": "https://www.ziprecruiter.com/jobs/view/def456",
    "job_description": "Great ML role.",
    "job_posted_at_datetime_utc": "2026-06-02T12:00:00.000Z",
}


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_RESULT)
    assert posting.source == "jsearch:LinkedIn"
    assert posting.external_id == "abc123"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "Stripe"
    assert posting.location == "San Francisco, CA"
    assert posting.is_remote is False
    assert posting.apply_url == "https://www.linkedin.com/jobs/view/abc123"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_falls_back_to_job_apply_link_when_not_direct():
    """Matches the real live response captured during development: a single
    non-direct apply_options entry re-hosted by BeBee, not the employer."""
    result = dict(_FAKE_RESULT)
    result["job_apply_is_direct"] = False
    result["apply_options"] = [
        {"apply_link": "https://bebee.com/us/jobs/abc123", "is_direct": False, "publisher": "BeBee"}
    ]
    posting = _to_raw_posting(result)
    assert posting.apply_url == "https://www.linkedin.com/jobs/view/abc123"


def test_to_raw_posting_uses_job_apply_link_when_marked_direct():
    result = dict(_FAKE_RESULT)
    result["job_apply_is_direct"] = True
    posting = _to_raw_posting(result)
    assert posting.apply_url == "https://www.linkedin.com/jobs/view/abc123"


def test_to_raw_posting_prefers_direct_apply_option_over_job_apply_link():
    result = dict(_FAKE_RESULT)
    result["job_apply_is_direct"] = False
    result["apply_options"] = [
        {
            "apply_link": "https://bebee.com/us/jobs/abc123",
            "is_direct": False,
            "publisher": "BeBee",
        },
        {
            "apply_link": "https://stripe.com/jobs/abc123",
            "is_direct": True,
            "publisher": "Stripe",
        },
    ]
    posting = _to_raw_posting(result)
    assert posting.apply_url == "https://stripe.com/jobs/abc123"


def test_to_raw_posting_encodes_publisher_into_source():
    posting = _to_raw_posting(_FAKE_RESULT_REMOTE_NO_LOCATION)
    assert posting.source == "jsearch:ZipRecruiter"


def test_to_raw_posting_handles_remote_and_missing_location():
    posting = _to_raw_posting(_FAKE_RESULT_REMOTE_NO_LOCATION)
    assert posting.location is None
    assert posting.is_remote is True


def test_to_raw_posting_handles_missing_publisher():
    result = dict(_FAKE_RESULT)
    del result["job_publisher"]
    posting = _to_raw_posting(result)
    assert posting.source == "jsearch:Unknown"


def test_to_raw_posting_handles_missing_posted_date():
    result = dict(_FAKE_RESULT)
    del result["job_posted_at_datetime_utc"]
    posting = _to_raw_posting(result)
    assert posting.posted_at is None


# ── fetch ─────────────────────────────────────────────────────────────────────


def _mock_response(jobs: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"status": "OK", "data": {"jobs": jobs}}
    return resp


def test_fetch_parses_nested_jobs_and_sends_correct_params_and_headers():
    mock_response = _mock_response([_FAKE_RESULT, _FAKE_RESULT_REMOTE_NO_LOCATION])

    with patch("src.scrapers.jsearch.httpx.get", return_value=mock_response) as mock_get:
        postings = fetch("rapidapi-key-123")

    assert {p.external_id for p in postings} == {"abc123", "def456"}

    kwargs = mock_get.call_args.kwargs
    assert kwargs["params"]["employment_types"] == "INTERN"
    assert kwargs["params"]["country"] == "us"
    assert "/search-v2" in mock_get.call_args.args[0]
    assert kwargs["headers"]["X-RapidAPI-Key"] == "rapidapi-key-123"
    assert kwargs["headers"]["X-RapidAPI-Host"] == "jsearch.p.rapidapi.com"


def test_fetch_makes_exactly_one_call_regardless_of_query_breadth():
    """JSearch meters every request against a monthly quota — this must
    never loop per keyword like Indeed RSS does."""
    with patch("src.scrapers.jsearch.httpx.get", return_value=_mock_response([])) as mock_get:
        fetch("rapidapi-key")

    mock_get.assert_called_once()


def test_fetch_returns_empty_list_when_no_results():
    with patch("src.scrapers.jsearch.httpx.get", return_value=_mock_response([])):
        postings = fetch("rapidapi-key")

    assert postings == []


def test_fetch_returns_empty_list_when_data_key_missing():
    resp = MagicMock()
    resp.json.return_value = {"status": "OK"}
    with patch("src.scrapers.jsearch.httpx.get", return_value=resp):
        postings = fetch("rapidapi-key")

    assert postings == []
