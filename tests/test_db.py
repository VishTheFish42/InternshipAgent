"""Unit tests for src/db.py helpers — all run against in-memory SQLite."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import (
    CompanyLookup,
    DbStats,
    JobPosting,
    Notification,
    get_bot_state,
    get_last_run_log,
    get_latest_notification_for_posting,
    get_stats,
    get_unnotified_postings,
    get_unnotified_scored_postings,
    get_unresolved_companies,
    get_unscored_postings,
    init_db,
    log_run,
    mark_applied,
    mark_notified,
    record_score,
    session_scope,
    set_notifications_paused,
    update_last_telegram_update_id,
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
    return datetime.now(UTC).replace(tzinfo=None)


# ── init_db ───────────────────────────────────────────────────────────────────


def test_init_db_creates_tables(engine: Engine) -> None:
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    assert tables == {
        "job_postings",
        "company_lookup",
        "notifications",
        "run_log",
        "bot_state",
    }


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
    with pytest.raises(RuntimeError), session_scope(engine) as s:
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
        p, _ = upsert_posting(
            s, _posting_data(company="Stripe, Inc.", title="Software Engineering Intern!")
        )
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
        p, _ = upsert_posting(
            s,
            _posting_data(
                location="Remote",
                is_remote=True,
                apply_url="https://apply.stripe.com/1",
                description="Build payment infrastructure.",
            ),
        )
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


# ── record_score ──────────────────────────────────────────────────────────────


def test_record_score_sets_fields(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data(external_id="j1"))
        posting_id = p.id

    with session_scope(engine) as s:
        record_score(s, posting_id, 88, "Strong fit.", "abc123")

    with session_scope(engine) as s:
        row = s.get(JobPosting, posting_id)
        assert row is not None
        assert row.match_score == 88
        assert row.match_reasoning == "Strong fit."
        assert row.profile_hash == "abc123"


def test_record_score_raises_for_missing_posting(engine: Engine) -> None:
    with session_scope(engine) as s, pytest.raises(ValueError):
        record_score(s, 9999, 50, "x", "hash")


def test_record_score_stores_missing_qualifications(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data(external_id="j1"))
        posting_id = p.id

    with session_scope(engine) as s:
        record_score(
            s,
            posting_id,
            60,
            "Close fit.",
            "abc123",
            missing_qualifications=["Kubernetes", "GraphQL"],
        )

    with session_scope(engine) as s:
        row = s.get(JobPosting, posting_id)
        assert row is not None
        assert row.missing_qualifications == ["Kubernetes", "GraphQL"]


def test_record_score_defaults_missing_qualifications_to_empty_list(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data(external_id="j1"))
        posting_id = p.id

    with session_scope(engine) as s:
        record_score(s, posting_id, 90, "Great fit.", "abc123")

    with session_scope(engine) as s:
        row = s.get(JobPosting, posting_id)
        assert row is not None
        assert row.missing_qualifications == []


# ── get_unnotified_postings ───────────────────────────────────────────────────


def test_get_unnotified_postings_includes_unscored_and_scored(engine: Engine) -> None:
    with session_scope(engine) as s:
        upsert_posting(s, _posting_data(external_id="j1"))
        p2, _ = upsert_posting(s, _posting_data(external_id="j2"))
        p2.match_score = 40

    with session_scope(engine) as s:
        rows = get_unnotified_postings(s)
    assert {r.external_id for r in rows} == {"j1", "j2"}


def test_get_unnotified_postings_excludes_already_notified(engine: Engine) -> None:
    with session_scope(engine) as s:
        p1, _ = upsert_posting(s, _posting_data(external_id="j1"))
        upsert_posting(s, _posting_data(external_id="j2"))
        p1.match_score = 90
        p1.notified = True

    with session_scope(engine) as s:
        rows = get_unnotified_postings(s)
    assert {r.external_id for r in rows} == {"j2"}


# ── get_last_run_log ──────────────────────────────────────────────────────────


def test_get_last_run_log_none_when_empty(engine: Engine) -> None:
    with session_scope(engine) as s:
        assert get_last_run_log(s) is None


def test_get_last_run_log_returns_most_recent(engine: Engine) -> None:
    with session_scope(engine) as s:
        log_run(s, {"started_at": _now(), "profile_hash": "first"})
        log_run(s, {"started_at": _now(), "profile_hash": "second"})

    with session_scope(engine) as s:
        latest = get_last_run_log(s)
    assert latest is not None
    assert latest.profile_hash == "second"


# ── get_unnotified_scored_postings ────────────────────────────────────────────


def test_get_unnotified_scored_postings_defaults_to_no_floor(engine: Engine) -> None:
    """Calling without min_relevance_score returns everything, even a very low score."""
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 5

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s)
    assert len(rows) == 1


def test_get_unnotified_scored_postings_excludes_below_min_relevance_score(
    engine: Engine,
) -> None:
    """min_relevance_score is a domain-relevance floor, not a fit-quality gate —
    postings below it (flagged off-field by the scoring prompt) are excluded."""
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 5

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s, min_relevance_score=15)
    assert rows == []


def test_get_unnotified_scored_postings_includes_at_min_relevance_score(
    engine: Engine,
) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 15

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s, min_relevance_score=15)
    assert len(rows) == 1


def test_get_unnotified_scored_postings_excludes_unscored(engine: Engine) -> None:
    with session_scope(engine) as s:
        upsert_posting(s, _posting_data())

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s)
    assert rows == []


def test_get_unnotified_scored_postings_excludes_already_notified(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 90
        p.notified = True

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s)
    assert rows == []


def test_get_unnotified_scored_postings_sorted_by_score_desc(engine: Engine) -> None:
    with session_scope(engine) as s:
        p1, _ = upsert_posting(s, _posting_data(external_id="j1"))
        p2, _ = upsert_posting(s, _posting_data(external_id="j2"))
        p3, _ = upsert_posting(s, _posting_data(external_id="j3"))
        p1.match_score = 15
        p2.match_score = 95
        p3.match_score = 55

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s)
    scores = [r.match_score for r in rows]
    assert scores == [95, 55, 15]


def test_get_unnotified_scored_postings_excludes_applied(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        p.match_score = 90
        p.applied = True

    with session_scope(engine) as s:
        rows = get_unnotified_scored_postings(s)
    assert rows == []


# ── mark_notified ─────────────────────────────────────────────────────────────


def test_mark_notified_sets_flags(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        mark_notified(s, pid, "123456789", "Test message")

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
        notif = mark_notified(s, pid, "123456789", "Test message", telegram_message_id="999")

    assert notif.telegram_message_id == "999"
    assert notif.delivery_status == "sent"
    assert notif.job_posting_id == pid

    with session_scope(engine) as s:
        rows = s.execute(select(Notification)).scalars().all()
    assert len(rows) == 1


def test_mark_notified_raises_for_missing_posting(engine: Engine) -> None:
    with pytest.raises(ValueError, match="not found"), session_scope(engine) as s:
        mark_notified(s, 99999, "123456789", "Test message")


def test_mark_notified_recipient_id_stored_as_given(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        notif = mark_notified(s, pid, "987654321", "msg")

    assert notif.recipient_id == "987654321"


# ── get_latest_notification_for_posting ─────────────────────────────────────────


def test_get_latest_notification_for_posting_returns_most_recent(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id
        mark_notified(s, pid, "123", "first message", telegram_message_id="111")

    with session_scope(engine) as s:
        mark_notified(s, pid, "123", "second message", telegram_message_id="222")

    with session_scope(engine) as s:
        notif = get_latest_notification_for_posting(s, pid)

    assert notif is not None
    assert notif.message == "second message"
    assert notif.telegram_message_id == "222"


def test_get_latest_notification_for_posting_returns_none_when_never_notified(
    engine: Engine,
) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        assert get_latest_notification_for_posting(s, pid) is None


# ── log_run ───────────────────────────────────────────────────────────────────


def test_log_run_inserts_and_returns_entry(engine: Engine) -> None:
    now = _now()
    with session_scope(engine) as s:
        run = log_run(
            s,
            {
                "started_at": now,
                "finished_at": now,
                "sources_polled": ["indeed", "adzuna"],
                "postings_found": 20,
                "postings_new": 5,
                "postings_scored": 5,
                "alerts_sent": 2,
                "estimated_cost_usd": 0.04,
            },
        )
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


# ── mark_applied ──────────────────────────────────────────────────────────────


def test_mark_applied_sets_flag_and_timestamp(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        mark_applied(s, pid)

    with session_scope(engine) as s:
        posting = s.get(JobPosting, pid)
        assert posting is not None
        assert posting.applied is True
        assert posting.applied_at is not None


def test_mark_applied_is_idempotent(engine: Engine) -> None:
    with session_scope(engine) as s:
        p, _ = upsert_posting(s, _posting_data())
        pid = p.id

    with session_scope(engine) as s:
        mark_applied(s, pid)
    with session_scope(engine) as s:
        first_applied_at = s.get(JobPosting, pid).applied_at  # type: ignore[union-attr]

    with session_scope(engine) as s:
        mark_applied(s, pid)
    with session_scope(engine) as s:
        posting = s.get(JobPosting, pid)
        assert posting is not None
        assert posting.applied is True
        assert posting.applied_at == first_applied_at


def test_mark_applied_raises_for_missing_posting(engine: Engine) -> None:
    with pytest.raises(ValueError, match="not found"), session_scope(engine) as s:
        mark_applied(s, 99999)


# ── BotState ──────────────────────────────────────────────────────────────────


def test_get_bot_state_creates_singleton_on_first_access(engine: Engine) -> None:
    with session_scope(engine) as s:
        state = get_bot_state(s)
    assert state.id == 1
    assert state.last_update_id is None
    assert state.notifications_paused is False


def test_get_bot_state_returns_same_row_across_calls(engine: Engine) -> None:
    with session_scope(engine) as s:
        get_bot_state(s)
        set_notifications_paused(s, True)

    with session_scope(engine) as s:
        state = get_bot_state(s)
    assert state.notifications_paused is True


def test_set_notifications_paused_toggles_flag(engine: Engine) -> None:
    with session_scope(engine) as s:
        set_notifications_paused(s, True)
    with session_scope(engine) as s:
        assert get_bot_state(s).notifications_paused is True

    with session_scope(engine) as s:
        set_notifications_paused(s, False)
    with session_scope(engine) as s:
        assert get_bot_state(s).notifications_paused is False


def test_update_last_telegram_update_id_advances_cursor(engine: Engine) -> None:
    with session_scope(engine) as s:
        update_last_telegram_update_id(s, 100)
    with session_scope(engine) as s:
        assert get_bot_state(s).last_update_id == 100


def test_update_last_telegram_update_id_never_moves_backward(engine: Engine) -> None:
    with session_scope(engine) as s:
        update_last_telegram_update_id(s, 100)
    with session_scope(engine) as s:
        update_last_telegram_update_id(s, 50)
    with session_scope(engine) as s:
        assert get_bot_state(s).last_update_id == 100
