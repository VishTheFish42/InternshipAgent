"""Unit tests for scrapers.indeed_rss — all HTTP/feed parsing is mocked."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.scrapers.indeed_rss import (
    _extract_job_id,
    _parse_title,
    _to_raw_posting,
    fetch,
    fetch_all,
)

_FAKE_ENTRIES = [
    {
        "id": "https://www.indeed.com/rc/clk?jk=abc123def",
        "title": "Software Engineering Intern - Stripe - San Francisco, CA",
        "link": "https://www.indeed.com/rc/clk?jk=abc123def",
        "summary": "Great backend role.",
        "published": "Mon, 01 Jun 2026 12:00:00 GMT",
    },
    {
        "id": "https://www.indeed.com/rc/clk?jk=xyz789",
        "title": "Data Science Intern - Anthropic - Remote",
        "link": "https://www.indeed.com/rc/clk?jk=xyz789",
        "summary": "Great ML role.",
        "published": "Tue, 02 Jun 2026 12:00:00 GMT",
    },
]


# ── _extract_job_id ───────────────────────────────────────────────────────────


def test_extract_job_id_from_jk_param():
    entry = {"id": "https://www.indeed.com/rc/clk?jk=abc123def"}
    assert _extract_job_id(entry) == "abc123def"


def test_extract_job_id_falls_back_to_link_when_no_jk():
    entry = {"id": "", "link": "https://example.com/jobs/1"}
    assert _extract_job_id(entry) == "https://example.com/jobs/1"


# ── _parse_title ──────────────────────────────────────────────────────────────


def test_parse_title_three_part_format():
    title, company, location = _parse_title(
        "Software Engineering Intern - Stripe - San Francisco, CA"
    )
    assert title == "Software Engineering Intern"
    assert company == "Stripe"
    assert location == "San Francisco, CA"


def test_parse_title_two_part_format():
    title, company, location = _parse_title("Software Engineering Intern - Stripe")
    assert title == "Software Engineering Intern"
    assert company == "Stripe"
    assert location is None


def test_parse_title_no_separator():
    title, company, location = _parse_title("Software Engineering Intern")
    assert title == "Software Engineering Intern"
    assert company == "Unknown"
    assert location is None


def test_parse_title_location_with_dash_preserved():
    title, company, location = _parse_title("SWE Intern - Acme - New York - NY")
    assert title == "SWE Intern"
    assert company == "Acme"
    assert location == "New York - NY"


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_ENTRIES[0])
    assert posting.source == "indeed"
    assert posting.external_id == "abc123def"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "Stripe"
    assert posting.location == "San Francisco, CA"
    assert posting.is_remote is False
    assert posting.apply_url == "https://www.indeed.com/rc/clk?jk=abc123def"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_detects_remote():
    posting = _to_raw_posting(_FAKE_ENTRIES[1])
    assert posting.is_remote is True
    assert posting.location == "Remote"


def test_to_raw_posting_handles_missing_published_date():
    entry = dict(_FAKE_ENTRIES[0])
    del entry["published"]
    posting = _to_raw_posting(entry)
    assert posting.posted_at is None


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_returns_all_entries_as_postings():
    fake_feed = SimpleNamespace(entries=_FAKE_ENTRIES)
    with patch("src.scrapers.indeed_rss._fetch_feed", return_value=fake_feed):
        postings = fetch("software engineering intern")
    assert len(postings) == 2
    assert {p.external_id for p in postings} == {"abc123def", "xyz789"}


def test_fetch_empty_feed_returns_empty_list():
    fake_feed = SimpleNamespace(entries=[])
    with patch("src.scrapers.indeed_rss._fetch_feed", return_value=fake_feed):
        postings = fetch("nonexistent query")
    assert postings == []


# ── fetch_all ─────────────────────────────────────────────────────────────────


def test_fetch_all_queries_every_search_term_and_rate_limits():
    with (
        patch("src.scrapers.indeed_rss.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.indeed_rss.time.sleep") as mock_sleep,
    ):
        fetch_all(["query1", "query2", "query3"])

    assert mock_fetch.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(2.0)


def test_fetch_all_uses_default_queries_when_none_given():
    with (
        patch("src.scrapers.indeed_rss.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.indeed_rss.time.sleep"),
    ):
        fetch_all()
    from src.scrapers.base import build_queries

    assert mock_fetch.call_count == len(build_queries())


def test_fetch_all_skips_failing_query_and_continues():
    import httpx

    def fake_fetch(query: str) -> list:
        if query == "bad":
            raise httpx.HTTPError("boom")
        return [_to_raw_posting(_FAKE_ENTRIES[0])]

    with (
        patch("src.scrapers.indeed_rss.fetch", side_effect=fake_fetch),
        patch("src.scrapers.indeed_rss.time.sleep"),
    ):
        postings = fetch_all(["bad", "good"])

    assert len(postings) == 1
