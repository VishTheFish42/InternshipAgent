"""Unit tests for scrapers.remoteok — all HTTP calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from src.scrapers.remoteok import _is_internship, _to_raw_posting, fetch

_LEGAL_NOTICE = {"legal": "https://remoteok.com/legal"}

_INTERN_JOB = {
    "id": "111222",
    "position": "Software Engineering Intern",
    "company": "Acme Remote",
    "location": "Worldwide",
    "tags": ["intern", "python"],
    "url": "https://remoteok.com/remote-jobs/111222",
    "apply_url": "https://acme.example/apply/111222",
    "description": "Great remote internship.",
    "date": "2026-06-01T12:00:00+00:00",
}

_JUNIOR_JOB = {
    "id": "333444",
    "position": "Junior Backend Engineer",
    "company": "Widgets Co",
    "location": "Worldwide",
    "tags": ["junior", "go"],
    "url": "https://remoteok.com/remote-jobs/333444",
    "date": "2026-06-02T12:00:00+00:00",
}

_SENIOR_JOB = {
    "id": "555666",
    "position": "Staff Software Engineer",
    "company": "BigCo",
    "location": "Worldwide",
    "tags": ["senior", "rust"],
    "url": "https://remoteok.com/remote-jobs/555666",
    "date": "2026-06-03T12:00:00+00:00",
}


# ── _is_internship ────────────────────────────────────────────────────────────


def test_is_internship_matches_intern_tag():
    assert _is_internship(_INTERN_JOB)


def test_is_internship_rejects_bare_junior_tag():
    """The bare 'junior' tag is deliberately excluded — RemoteOK tags junior
    roles across every field, not just software, and letting it through was
    sending non-software postings with no domain relevance at all."""
    assert not _is_internship(_JUNIOR_JOB)


def test_is_internship_matches_coop_tag():
    assert _is_internship({"position": "Backend Co-op", "tags": ["co-op", "python"]})


def test_is_internship_matches_coop_in_position_without_tag():
    job = {"position": "Software Engineering Co-op - Fall 2026", "tags": ["python"]}
    assert _is_internship(job)


def test_is_internship_rejects_cooperative_as_false_positive():
    job = {"position": "Cooperative Partnerships Manager", "tags": ["sales"]}
    assert not _is_internship(job)


def test_is_internship_matches_intern_in_position_without_tag():
    job = {"position": "Data Science Intern", "tags": ["python"]}
    assert _is_internship(job)


def test_is_internship_rejects_senior_role():
    assert not _is_internship(_SENIOR_JOB)


# ── _to_raw_posting ───────────────────────────────────────────────────────────


def test_to_raw_posting_maps_fields():
    posting = _to_raw_posting(_INTERN_JOB)
    assert posting.source == "remoteok"
    assert posting.external_id == "111222"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "Acme Remote"
    assert posting.is_remote is True
    assert posting.apply_url == "https://acme.example/apply/111222"
    assert posting.posted_at == datetime(2026, 6, 1, 12, 0, 0)


def test_to_raw_posting_falls_back_to_url_when_no_apply_url():
    posting = _to_raw_posting(_JUNIOR_JOB)
    assert posting.apply_url == "https://remoteok.com/remote-jobs/333444"


def test_to_raw_posting_defaults_location_to_remote():
    job = dict(_INTERN_JOB)
    del job["location"]
    posting = _to_raw_posting(job)
    assert posting.location == "Remote"


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_filters_legal_notice_and_non_intern_jobs():
    with patch(
        "src.scrapers.remoteok._fetch_jobs",
        return_value=[_INTERN_JOB, _JUNIOR_JOB],
    ):
        postings = fetch()
    assert {p.external_id for p in postings} == {"111222"}


def test_fetch_excludes_senior_roles():
    with patch("src.scrapers.remoteok._fetch_jobs", return_value=[_SENIOR_JOB]):
        postings = fetch()
    assert postings == []


def test_fetch_jobs_strips_legal_notice_entry():
    from src.scrapers.remoteok import _fetch_jobs

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return [_LEGAL_NOTICE, _INTERN_JOB]

    with patch("src.scrapers.remoteok.httpx.get", return_value=_FakeResponse()):
        jobs = _fetch_jobs()

    assert len(jobs) == 1
    assert jobs[0]["id"] == "111222"
