"""Unit tests for the cross-source deduplication pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db import Base
from src.deduplicator import (
    find_fuzzy_duplicate,
    find_source_duplicate,
    find_url_duplicate,
    normalize_url,
    upsert_deduped,
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


def _posting(**overrides):
    data = {
        "source": "greenhouse:stripe",
        "external_id": "12345",
        "title": "Software Engineering Intern",
        "company": "Stripe",
        "location": "Remote",
        "is_remote": True,
        "url": "https://boards.greenhouse.io/stripe/jobs/12345",
        "apply_url": "https://boards.greenhouse.io/stripe/jobs/12345",
        "description": "Work on payments infrastructure using Python and Go.",
        "posted_at": None,
    }
    data.update(overrides)
    return data


# ── normalize_url ─────────────────────────────────────────────────────────────


def test_normalize_url_strips_utm_params():
    url = "https://boards.greenhouse.io/stripe/jobs/12345?utm_source=linkedin&utm_medium=social"
    assert normalize_url(url) == "https://boards.greenhouse.io/stripe/jobs/12345"


def test_normalize_url_strips_trailing_slash():
    assert normalize_url("https://jobs.lever.co/linear/abc/") == "https://jobs.lever.co/linear/abc"


def test_normalize_url_lowercases_scheme_and_host():
    assert normalize_url("HTTPS://Boards.Greenhouse.IO/stripe/jobs/1") == (
        "https://boards.greenhouse.io/stripe/jobs/1"
    )


def test_normalize_url_keeps_non_tracking_query_params():
    url = "https://example.com/jobs?role=intern&utm_campaign=x"
    assert normalize_url(url) == "https://example.com/jobs?role=intern"


def test_normalize_url_strips_fragment():
    assert normalize_url("https://example.com/jobs/1#apply") == "https://example.com/jobs/1"


def test_normalize_url_param_order_does_not_matter():
    a = normalize_url("https://example.com/jobs?b=2&a=1")
    b = normalize_url("https://example.com/jobs?a=1&b=2")
    assert a == b


# ── find_source_duplicate ─────────────────────────────────────────────────────


def test_find_source_duplicate_none_when_empty(session):
    assert find_source_duplicate(session, "greenhouse:stripe", "12345") is None


def test_find_source_duplicate_finds_exact_match(session):
    upsert_deduped(session, _posting())
    dup = find_source_duplicate(session, "greenhouse:stripe", "12345")
    assert dup is not None
    assert dup.external_id == "12345"


# ── find_url_duplicate ────────────────────────────────────────────────────────


def test_find_url_duplicate_none_when_empty(session):
    assert find_url_duplicate(session, "https://boards.greenhouse.io/stripe/jobs/12345") is None


def test_find_url_duplicate_finds_match(session):
    upsert_deduped(session, _posting())
    dup = find_url_duplicate(session, "https://boards.greenhouse.io/stripe/jobs/12345")
    assert dup is not None


def test_find_url_duplicate_none_for_empty_string(session):
    assert find_url_duplicate(session, None) is None


# ── find_fuzzy_duplicate ──────────────────────────────────────────────────────


def test_find_fuzzy_duplicate_matches_same_company_title_similar_description(session):
    upsert_deduped(session, _posting())
    dup = find_fuzzy_duplicate(
        session,
        "stripe",
        "software engineering intern",
        "Work on payments infrastructure using Python and Go.",
    )
    assert dup is not None


def test_find_fuzzy_duplicate_ignores_different_descriptions(session):
    upsert_deduped(session, _posting())
    dup = find_fuzzy_duplicate(
        session,
        "stripe",
        "software engineering intern",
        "Totally unrelated description about marketing analytics dashboards for retail clients.",
    )
    assert dup is None


def test_find_fuzzy_duplicate_respects_time_window(session):
    upsert_deduped(session, _posting())
    old_cutoff_check_time = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=8)
    dup = find_fuzzy_duplicate(
        session,
        "stripe",
        "software engineering intern",
        "Work on payments infrastructure using Python and Go.",
        now=old_cutoff_check_time,
    )
    assert dup is None


def test_find_fuzzy_duplicate_falls_back_to_match_when_no_description(session):
    upsert_deduped(session, _posting(description=None))
    dup = find_fuzzy_duplicate(session, "stripe", "software engineering intern", None)
    assert dup is not None


# ── upsert_deduped: end-to-end cross-source scenario ─────────────────────────


def test_upsert_deduped_inserts_new_posting(session):
    posting, was_new, reason = upsert_deduped(session, _posting())
    assert was_new is True
    assert reason == "new"
    assert posting.id is not None


def test_upsert_deduped_same_source_id_is_duplicate(session):
    upsert_deduped(session, _posting())
    posting, was_new, reason = upsert_deduped(session, _posting())
    assert was_new is False
    assert reason == "duplicate_source_id"


def test_upsert_deduped_cross_source_same_url_is_duplicate(session):
    """The design doc's canonical scenario: a Stripe posting found via JSearch/Adzuna
    AND via the Greenhouse direct monitor should collapse to a single row."""
    upsert_deduped(session, _posting(source="greenhouse:stripe", external_id="12345"))

    posting, was_new, reason = upsert_deduped(
        session,
        _posting(
            source="adzuna",
            external_id="adzuna-999",
            apply_url="https://boards.greenhouse.io/stripe/jobs/12345?utm_source=adzuna",
        ),
    )
    assert was_new is False
    assert reason == "duplicate_url"
    assert posting.source == "greenhouse:stripe"


def test_upsert_deduped_cross_source_fuzzy_match_when_urls_differ(session):
    """LinkedIn-style redirect URL vs. the company's direct ATS URL: same job,
    different URL shape, caught by the fuzzy fallback instead."""
    upsert_deduped(session, _posting(source="greenhouse:stripe", external_id="12345"))

    posting, was_new, reason = upsert_deduped(
        session,
        _posting(
            source="indeed",
            external_id="indeed-abc",
            apply_url="https://www.indeed.com/rc/clk?jk=abc123",
        ),
    )
    assert was_new is False
    assert reason == "duplicate_fuzzy"

    assert session.query(type(posting)).count() == 1


def test_upsert_deduped_keeps_two_genuinely_different_roles_same_title(session):
    """Two legitimately different roles with the same title at the same company —
    kept apart because their descriptions differ significantly."""
    upsert_deduped(
        session,
        _posting(
            source="greenhouse:stripe",
            external_id="1",
            apply_url="https://boards.greenhouse.io/stripe/jobs/1",
            description="Backend infra internship focused on Kafka and distributed systems.",
        ),
    )

    posting, was_new, reason = upsert_deduped(
        session,
        _posting(
            source="lever:stripe",
            external_id="2",
            apply_url="https://jobs.lever.co/stripe/2",
            description="Frontend internship building React dashboards for merchants.",
        ),
    )
    assert was_new is True
    assert reason == "new"
    assert posting.external_id == "2"
