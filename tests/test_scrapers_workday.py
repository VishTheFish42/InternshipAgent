"""Unit tests for scrapers.workday — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db import Base, CompanyLookup
from src.scrapers.workday import (
    _is_internship,
    _parse_posted_at,
    _parse_slug,
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
        "title": "Software Engineering Intern, NCCL - 2026",
        "externalPath": "/job/US-Santa-Clara/Software-Engineering-Intern_JR12345",
        "locationsText": "US, Remote",
        "postedOn": "Posted 2 Days Ago",
    },
    {
        "title": "Design Co-op",
        "externalPath": "/job/US-Santa-Clara/Design-Co-op_JR12346",
        "locationsText": "Santa Clara, CA",
        "postedOn": "Posted Today",
    },
    {
        "title": "Internal Product Manager 5",
        "externalPath": "/job/US-Santa-Clara/Internal-PM_JR12347",
        "locationsText": "Santa Clara, CA",
        "postedOn": "Posted 30+ Days Ago",
    },
]


# ── _is_internship ───────────────────────────────────────────────────────────


def test_is_internship_matches_intern():
    assert _is_internship("Software Engineering Intern")


def test_is_internship_matches_coop():
    assert _is_internship("Design Co-op")


def test_is_internship_rejects_internal_false_positive():
    # Workday's own searchText match is loose enough to return "Internal
    # Product Manager" for a query of "intern" (live-observed against
    # Adobe's board) — the title-level regex is what actually filters it.
    assert not _is_internship("Internal Product Manager 5")


# ── _parse_slug ───────────────────────────────────────────────────────────────


def test_parse_slug_splits_tenant_instance_site():
    assert _parse_slug("intel.wd1/External") == ("intel", "wd1", "External")


def test_parse_slug_rejects_missing_instance():
    with pytest.raises(ValueError, match="Malformed"):
        _parse_slug("intel/External")


def test_parse_slug_rejects_missing_site():
    with pytest.raises(ValueError, match="Malformed"):
        _parse_slug("intel.wd1")


# ── _parse_posted_at ─────────────────────────────────────────────────────────

_NOW = datetime(2026, 8, 20, 12, 0, 0)


def test_parse_posted_at_today():
    assert _parse_posted_at("Posted Today", now=_NOW) == _NOW


def test_parse_posted_at_yesterday():
    assert _parse_posted_at("Posted Yesterday", now=_NOW) == datetime(2026, 8, 19, 12, 0, 0)


def test_parse_posted_at_days_ago():
    assert _parse_posted_at("Posted 2 Days Ago", now=_NOW) == datetime(2026, 8, 18, 12, 0, 0)


def test_parse_posted_at_thirty_plus_days_ago_is_a_lower_bound():
    assert _parse_posted_at("Posted 30+ Days Ago", now=_NOW) == datetime(2026, 7, 21, 12, 0, 0)


def test_parse_posted_at_none_when_absent():
    assert _parse_posted_at(None, now=_NOW) is None


def test_parse_posted_at_none_when_unrecognized():
    assert _parse_posted_at("Some new format Workday invents later", now=_NOW) is None


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_FAKE_JOBS[0], "nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert posting.source == "workday:nvidia/NVIDIAExternalCareerSite"
    assert posting.external_id == "/job/US-Santa-Clara/Software-Engineering-Intern_JR12345"
    assert posting.title == "Software Engineering Intern, NCCL - 2026"
    assert posting.company == "nvidia"
    assert posting.is_remote is True
    assert posting.description is None
    assert (
        posting.url == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        "/job/US-Santa-Clara/Software-Engineering-Intern_JR12345"
    )
    assert posting.apply_url == posting.url


def test_to_raw_posting_detects_non_remote():
    posting = _to_raw_posting(_FAKE_JOBS[1], "nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert posting.is_remote is False
    assert posting.location == "Santa Clara, CA"


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_filters_to_internships_only():
    with patch("src.scrapers.workday._fetch_jobs_page", return_value=_FAKE_JOBS) as mock_page:
        postings = fetch("nvidia.wd5/NVIDIAExternalCareerSite")

    # Called once per query term ("intern", "co-op"), same slug each time.
    assert mock_page.call_count == 2
    for call in mock_page.call_args_list:
        assert call.args[:3] == ("nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert {p.title for p in postings} == {
        "Software Engineering Intern, NCCL - 2026",
        "Design Co-op",
    }


def test_fetch_dedupes_a_job_matched_by_both_queries():
    with patch("src.scrapers.workday._fetch_jobs_page", return_value=[_FAKE_JOBS[0]]):
        postings = fetch("nvidia.wd5/NVIDIAExternalCareerSite")

    # _FAKE_JOBS[0] is returned identically for both the "intern" and
    # "co-op" query in this test — must collapse to one posting, not two.
    assert len(postings) == 1


def test_fetch_returns_empty_when_no_matches():
    with patch("src.scrapers.workday._fetch_jobs_page", return_value=[_FAKE_JOBS[2]]):
        postings = fetch("nvidia.wd5/NVIDIAExternalCareerSite")
    assert postings == []


def test_fetch_raises_on_malformed_slug():
    with pytest.raises(ValueError, match="Malformed"):
        fetch("not-a-valid-slug")


# ── fetch_for_resolved_companies ─────────────────────────────────────────────


def test_fetch_for_resolved_companies_only_queries_resolved_workday(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="Nvidia",
                ats_type="workday",
                slug="nvidia.wd5/NVIDIAExternalCareerSite",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(
                name_raw="Acme",
                ats_type="workday",
                slug="acme.wd1/External",
                status="unresolved",
            ),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.workday.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.workday.time.sleep") as mock_sleep,
    ):
        fetch_for_resolved_companies(session)

    mock_fetch.assert_called_once_with("nvidia.wd5/NVIDIAExternalCareerSite")
    mock_sleep.assert_not_called()


def test_fetch_for_resolved_companies_rate_limits_between_calls(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="Nvidia",
                ats_type="workday",
                slug="nvidia.wd5/NVIDIAExternalCareerSite",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Intel", ats_type="workday", slug="intel.wd1/External", status="resolved"
            ),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.workday.fetch", return_value=[]) as mock_fetch,
        patch("src.scrapers.workday.time.sleep") as mock_sleep,
    ):
        fetch_for_resolved_companies(session)

    assert mock_fetch.call_count == 2
    mock_sleep.assert_called_once_with(5.0)


def test_fetch_for_resolved_companies_skips_source_on_http_error(session):
    import httpx

    session.add_all(
        [
            CompanyLookup(
                name_raw="Nvidia",
                ats_type="workday",
                slug="nvidia.wd5/NVIDIAExternalCareerSite",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Intel", ats_type="workday", slug="intel.wd1/External", status="resolved"
            ),
        ]
    )
    session.commit()

    def fake_fetch(slug: str) -> list:
        if slug == "nvidia.wd5/NVIDIAExternalCareerSite":
            raise httpx.HTTPError("boom")
        return ["intel-posting"]  # type: ignore[list-item]

    with (
        patch("src.scrapers.workday.fetch", side_effect=fake_fetch) as mock_fetch,
        patch("src.scrapers.workday.time.sleep"),
    ):
        postings = fetch_for_resolved_companies(session)

    mock_fetch.assert_any_call("nvidia.wd5/NVIDIAExternalCareerSite")
    mock_fetch.assert_any_call("intel.wd1/External")
    assert len(postings) == 1


def test_fetch_for_resolved_companies_continues_past_exhausted_retry(session):
    """Same failure mode already fixed for greenhouse.py/lever.py: fetch()'s
    underlying @retry wraps a repeated httpx.HTTPError in tenacity.RetryError
    once stop_after_attempt(3) is exhausted, not the original HTTPError."""
    from tenacity import RetryError

    session.add_all(
        [
            CompanyLookup(
                name_raw="Nvidia",
                ats_type="workday",
                slug="nvidia.wd5/NVIDIAExternalCareerSite",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Intel", ats_type="workday", slug="intel.wd1/External", status="resolved"
            ),
        ]
    )
    session.commit()

    def fake_fetch(slug: str) -> list:
        if slug == "nvidia.wd5/NVIDIAExternalCareerSite":
            raise RetryError(last_attempt=None)  # type: ignore[arg-type]
        return ["intel-posting"]  # type: ignore[list-item]

    with (
        patch("src.scrapers.workday.fetch", side_effect=fake_fetch) as mock_fetch,
        patch("src.scrapers.workday.time.sleep"),
    ):
        postings = fetch_for_resolved_companies(session)

    mock_fetch.assert_any_call("intel.wd1/External")
    assert len(postings) == 1


def test_fetch_for_resolved_companies_skips_malformed_cached_slug(session):
    """A stale/manually-entered CompanyLookup row with a slug that doesn't
    match "{tenant}.{instance}/{site}" must not abort the rest of the loop
    — same "one bad company can't take down the cycle" guarantee as the
    HTTPError/RetryError cases above."""
    session.add_all(
        [
            CompanyLookup(
                name_raw="Broken", ats_type="workday", slug="not-a-valid-slug", status="resolved"
            ),
            CompanyLookup(
                name_raw="Intel", ats_type="workday", slug="intel.wd1/External", status="resolved"
            ),
        ]
    )
    session.commit()

    def fake_fetch(slug: str) -> list:
        if slug == "not-a-valid-slug":
            raise ValueError(f"Malformed Workday slug: {slug!r}")
        return ["intel-posting"]  # type: ignore[list-item]

    with (
        patch("src.scrapers.workday.fetch", side_effect=fake_fetch) as mock_fetch,
        patch("src.scrapers.workday.time.sleep"),
    ):
        postings = fetch_for_resolved_companies(session)

    mock_fetch.assert_any_call("intel.wd1/External")
    assert len(postings) == 1
