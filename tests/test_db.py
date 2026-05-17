"""Unit tests for src/db.py helpers — all run against in-memory SQLite."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import (
    CompanyLookup,
    DbStats,
    JobPosting,
    Notification,
    RunLog,
    get_stats,
    get_unnotified_above_threshold,
    get_unresolved_companies,
    get_unscored_postings,
    init_db,
    log_run,
    mark_notified,
    session_scope,
    upsert_posting,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine() -> Engine:
    return init_db("sqlite://")


def _posting_data(**overrides: object) -> dict:
    base: dict = {
        "source": "indeed",
        "external_id": "job-001",
        "title": "Software Engineering Intern",
        "company": "Stripe",
        "url": "https://stripe.com/jobs/1",
    }
    base.update(overrides)
    return base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── init_db ───────────────────────────────────────────────────────────────────

def test_init_db_creates_tables(engine: Engine) -> None:
    from sqlalchemy import inspect
    tables = set(inspect(engine).get_table_names())
    assert tables == {"job_postings", "company_lookup", "notifications", "run_log"}


def test_init_db_idempotent() -> None:
    """Calling init_db twice on the same URL must not raise."""
    eng = init_db("sqlite:///:memory:")
    init_db("sqlite:///:memory:")  # second call — tables already exist
    assert eng is not None


# ── session_scope ─────────────────────────────────────────────────────────────

def test_session_scope_commits_on_success(engine: Engine) -> None:
    with session_scope(engine) as s:
        s.add(CompanyLookup(name_raw="Acme", status="unresolved"))

    with session_scope(engine) as s:
        count = s.execute(select(CompanyLookup)).scalars().all()
    assert len(count) == 1


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    with pytest.raises(RuntimeError):
        with session_scope(engine) as s:
            s.add(CompanyLookup(name_raw="Acme", status="unresolved"))
            raise RuntimeError("intentional")

    with session_scope(engine) as s:
        rows = s.execute(select(CompanyLookup)).scalars().all()
    assert rows == []


# ── upsert_posting ────────────────────────────────────────────────────────────

def test_upsert_new_posting_returns_was_new_true(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, was_new = upsert_posting(s, _posting_data())
    assert was_new is True
    assert p.id is not None


def test_upsert_sets_normalized_fields(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data(
            company="Stripe, Inc.", title="Software Engineering Intern!"
        ))
    assert p.company_normalized == "stripe inc"
    assert p.title_normalized == "software engineering intern"


def test_upsert_sets_found_at_automatically(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
    assert p.found_at is not None
    assert isinstance(p.found_at, datetime)


def test_upsert_respects_caller_found_at(engine: Engine) -> None:
    explicit_time = datetime(2025, 1, 1, 12, 0, 0)
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data(found_at=explicit_time))
    assert p.found_at == explicit_time


def test_upsert_duplicate_returns_was_new_false(engine: Engine) -> None:
    with session_scope(engine) as s:
        p1, _ = upsert_posting(s, _posting_data())

    with session_scope(engine) as s:
        p2, was_new = upsert_posting(s, _posting_data())
    assert was_new is False
    assert p2.id == p1.id


def test_upsert_same_external_id_different_source_inserts_both(engine: Engine) -> None:
    with session_scope(engine) as s:
        p1, new1 = upsert_posting(s, _posting_data(source="indeed"))
        p2, new2 = upsert_posting(s, _posting_data(source="adzuna"))
    assert new1 is True
    assert new2 is True
    assert p1.id != p2.id


def test_upsert_stores_optional_fields(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data(
            location="Remote",
            is_remote=True,
            apply_url="https://apply.stripe.com/1",
            description="Build payment infrastructure.",
        ))
    assert p.location == "Remote"
    assert p.is_remote is True
    assert p.apply_url == "https://apply.stripe.com/1"


# ── get_unscored_postings ─────────────────────────────────────────────────────

def test_get_unscored_returns_null_score_rows(engine: Engine) -> None:
    with session_scope(engine) as s:
        upsert_posting(s, _posting_data(external_id="j1"))
        upsert_posting(s, _posting_data(external_id="j2"))

    with session_scope(engine) as s:
        rows = get_unscored_postings(s)
    assert len(rows) == 2


def test_get_unscored_excludes_scored_rows(engine: Engine) -> None:
    with session_scope(engine) as s:
        upsert_posting(s, _posting_data(external_id="j1"))
        p2, _ = upsert_posting(s, _posting_data(external_id="j2"))
        p2.match_score = 80

    with session_scope(engine) as s:
        rows = get_unscored_postings(s)
    assert len(rows) == 1
    assert rows[0].external_id == "j1"


# ── get_unnotified_above_threshold ────────────────────────────────────────────

def test_threshold_excludes_below_score(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 50

    with session_scope(engine) as s:
        rows = get_unnotified_above_threshold(s, threshold=70)
    assert rows == []


def test_threshold_includes_at_boundary(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 70

    with session_scope(engine) as s:
        rows = get_unnotified_above_threshold(s, threshold=70)
    assert len(rows) == 1


def test_threshold_excludes_already_notified(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 90
        p.notified = True

    with session_scope(engine) as s:
        rows = get_unnotified_above_threshold(s, threshold=70)
    assert rows == []


def test_threshold_results_sorted_by_score_desc(engine: Engine) -> None:
    with session_scope(engine) as s:
        p1, _ = upsert_posting(s, _posting_data(external_id="j1"))
        p2, _ = upsert_posting(s, _posting_data(external_id="j2"))
        p3, _ = upsert_posting(s, _posting_data(external_id="j3"))
        p1.match_score = 75
        p2.match_score = 95
        p3.match_score = 85

    with session_scope(engine) as s:
        rows = get_unnotified_above_threshold(s, threshold=70)
    scores = [r.match_score for r in rows]
    assert scores == [95, 85, 75]


# ── mark_notified ─────────────────────────────────────────────────────────────

def test_mark_notified_sets_flags(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        mark_notified(s, pid, "+15550001234", "Test SMS")

    with session_scope(engine) as s:
        posting = s.get(JobPosting, pid)
        assert posting is not None
        assert posting.notified is True
        assert posting.notified_at is not None


def test_mark_notified_creates_notification_record(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        notif = mark_notified(s, pid, "+15550001234", "Test SMS", twilio_sid="SM999")

    assert notif.twilio_sid == "SM999"
    assert notif.delivery_status == "sent"
    assert notif.job_posting_id == pid

    with session_scope(engine) as s:
        rows = s.execute(select(Notification)).scalars().all()
    assert len(rows) == 1


def test_mark_notified_raises_for_missing_posting(engine: Engine) -> None:
    with pytest.raises(ValueError, match="not found"):
        with session_scope(engine) as s:
            mark_notified(s, 99999, "+15550001234", "Test SMS")


def test_mark_notified_phone_stored_as_given(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        notif = mark_notified(s, pid, "+15559876543", "msg")

    assert notif.phone_number == "+15559876543"


# ── log_run ───────────────────────────────────────────────────────────────────

def test_log_run_inserts_and_returns_entry(engine: Engine) -> None:
    now = _now()
    with session_scope(engine) as s:
        run = log_run(s, {
            "started_at": now,
            "finished_at": now,
            "sources_polled": ["indeed", "adzuna"],
            "postings_found": 20,
            "postings_new": 5,
            "postings_scored": 5,
            "alerts_sent": 2,
            "estimated_cost_usd": 0.04,
        })
    assert run.id is not None
    assert run.alerts_sent == 2
    assert run.sources_polled == ["indeed", "adzuna"]


def test_log_run_nullable_fields_default_to_none(engine: Engine) -> None:
    now = _now()
    with session_scope(engine) as s:
        run = log_run(s, {"started_at": now})
    assert run.finished_at is None
    assert run.errors is None
    assert run.estimated_cost_usd is None


# ── get_unresolved_companies ──────────────────────────────────────────────────

def test_get_unresolved_returns_only_unresolved(engine: Engine) -> None:
    with session_scope(engine) as s:
        s.add(CompanyLookup(name_raw="Acme", status="unresolved"))
        s.add(CompanyLookup(name_raw="Stripe", status="resolved"))
        s.add(CompanyLookup(name_raw="Linear", status="manual"))

    with session_scope(engine) as s:
        rows = get_unresolved_companies(s)
    assert len(rows) == 1
    assert rows[0].name_raw == "Acme"


def test_get_unresolved_empty_when_none(engine: Engine) -> None:
    with session_scope(engine) as s:
        rows = get_unresolved_companies(s)
    assert rows == []


# ── get_stats ─────────────────────────────────────────────────────────────────

def test_get_stats_returns_dbstats(engine: Engine) -> None:
    with session_scope(engine) as s:
        stats = get_stats(s)
    assert isinstance(stats, DbStats)


def test_get_stats_counts_postings_and_alerts(engine: Engine) -> None:
    with session_scope(engine) as s:
        p1, _ = upsert_posting(s, _posting_data(external_id="j1"))
        p2, _ = upsert_posting(s, _posting_data(external_id="j2"))
        mark_notified(s, p1.id, "+15550001234", "msg1")

    with session_scope(engine) as s:
        stats = get_stats(s)
    assert stats.total_postings == 2
    assert stats.alerts_sent == 1


def test_get_stats_lists_unresolved_company_names(engine: Engine) -> None:
    with session_scope(engine) as s:
        s.add(CompanyLookup(name_raw="Acme Corp", status="unresolved"))
        s.add(CompanyLookup(name_raw="Widgets Inc", status="unresolved"))
        s.add(CompanyLookup(name_raw="Stripe", status="resolved"))

    with session_scope(engine) as s:
        stats = get_stats(s)
    assert set(stats.unresolved_companies) == {"Acme Corp", "Widgets Inc"}


def test_get_stats_sums_estimated_cost(engine: Engine) -> None:
    now = _now()
    with session_scope(engine) as s:
        log_run(s, {"started_at": now, "estimated_cost_usd": 0.05})
        log_run(s, {"started_at": now, "estimated_cost_usd": 0.03})

    with session_scope(engine) as s:
        stats = get_stats(s)
    assert abs(stats.estimated_cost_usd - 0.08) < 1e-9


def test_get_stats_cost_zero_when_no_runs(engine: Engine) -> None:
    with session_scope(engine) as s:
        stats = get_stats(s)
    assert stats.estimated_cost_usd == 0.0
