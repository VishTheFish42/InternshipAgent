"""Unit tests for scrapers.hn — all Algolia API calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from src.scrapers.hn import (
    _extract_company,
    _is_internship_comment,
    _to_raw_posting,
    fetch_current_month_postings,
)

_FAKE_COMMENT_WITH_INTERN = {
    "objectID": "1001",
    "comment_text": "Acme Corp | San Francisco | Remote OK<p>We're hiring a Software Engineering "
    "Intern for the summer. Apply at https://acme.example/careers/42",
    "created_at": "2026-06-01T12:00:00.000Z",
}

_FAKE_COMMENT_NO_INTERN = {
    "objectID": "1002",
    "comment_text": "Widgets Inc | NYC<p>We're hiring senior backend engineers.",
    "created_at": "2026-06-01T13:00:00.000Z",
}

_FAKE_COMMENT_NO_URL = {
    "objectID": "1003",
    "comment_text": "Small Startup<p>Looking for a data science intern, no link provided.",
    "created_at": "2026-06-01T14:00:00.000Z",
}


# ── _is_internship_comment ────────────────────────────────────────────────────


def test_is_internship_comment_matches_intern():
    assert _is_internship_comment("Hiring a software engineering intern")


def test_is_internship_comment_matches_internship():
    assert _is_internship_comment("Summer internship available")


def test_is_internship_comment_rejects_unrelated_text():
    assert not _is_internship_comment("Hiring senior backend engineers")


# ── _extract_company ──────────────────────────────────────────────────────────


def test_extract_company_from_pipe_delimited_line():
    assert _extract_company("Acme Corp | San Francisco | Remote OK\nMore text") == "Acme Corp"


def test_extract_company_falls_back_to_unknown_for_empty_text():
    assert _extract_company("") == "Unknown (HN)"


def test_extract_company_handles_no_delimiter():
    assert (
        _extract_company("Just a plain sentence with no pipes")
        == "Just a plain sentence with no pipes"
    )


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_extracts_url_from_text():
    posting = _to_raw_posting(_FAKE_COMMENT_WITH_INTERN)
    assert posting.source == "hn"
    assert posting.external_id == "1001"
    assert posting.company == "Acme Corp"
    assert posting.apply_url == "https://acme.example/careers/42"
    assert posting.is_remote is True
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_falls_back_to_hn_link_when_no_url():
    posting = _to_raw_posting(_FAKE_COMMENT_NO_URL)
    assert posting.apply_url == "https://news.ycombinator.com/item?id=1003"
    assert posting.url == "https://news.ycombinator.com/item?id=1003"


def test_to_raw_posting_strips_html_tags():
    posting = _to_raw_posting(_FAKE_COMMENT_WITH_INTERN)
    assert "<p>" not in (posting.description or "")


# ── fetch_current_month_postings ──────────────────────────────────────────────


def test_fetch_current_month_postings_filters_to_internships_only():
    with (
        patch("src.scrapers.hn.find_current_thread_id", return_value="thread-1"),
        patch(
            "src.scrapers.hn.fetch_comments",
            return_value=[_FAKE_COMMENT_WITH_INTERN, _FAKE_COMMENT_NO_INTERN, _FAKE_COMMENT_NO_URL],
        ),
    ):
        postings = fetch_current_month_postings()

    assert {p.external_id for p in postings} == {"1001", "1003"}


def test_fetch_current_month_postings_returns_empty_when_no_thread_found():
    with patch("src.scrapers.hn.find_current_thread_id", return_value=None):
        postings = fetch_current_month_postings()
    assert postings == []
