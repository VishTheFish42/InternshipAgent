"""Unit tests for main.py orchestration — all external services are mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.engine import Engine

from src.company_discoverer import RetryResult, SeedResult
from src.config import Settings
from src.db import (
    JobPosting,
    init_db,
    mark_notified,
    record_score,
    session_scope,
    upsert_posting,
)
from src.main import (
    _dedupe_and_store,
    _fetch_all_postings,
    _get_top_match_this_week,
    _load_company_names,
    _load_yaml_config,
    _notify_and_mark,
    _notify_partial_matches_and_mark,
    _score_and_record,
    _to_match_info,
    _to_partial_match_info,
    _to_scoring_posting,
    run_adzuna_poll,
    run_cycle,
    run_digest,
)
from src.matcher import ScoredPosting, ScoringResult
from src.notifier import SendResult
from src.scrapers.base import RawPosting

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def engine() -> Engine:
    return init_db("sqlite://")


def _settings(**overrides: object) -> Settings:
    base: dict = {
        "anthropic_api_key": "test-key",
        "telegram_bot_token": "bot-token-test",
        "telegram_chat_id": "123456789",
        "burst_threshold": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _raw_posting(
    source: str = "greenhouse:stripe", external_id: str = "1", **overrides: object
) -> RawPosting:
    base: dict = {
        "source": source,
        "external_id": external_id,
        "title": "Software Engineering Intern",
        "company": "Stripe",
        "location": "Remote",
        "is_remote": True,
        "url": "https://boards.greenhouse.io/stripe/jobs/1",
        "apply_url": "https://boards.greenhouse.io/stripe/jobs/1",
        "description": "Great role.",
        "posted_at": None,
    }
    base.update(overrides)
    return RawPosting(**base)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _stored_posting(session, **overrides: object) -> JobPosting:
    data: dict = {
        "source": "greenhouse:stripe",
        "external_id": "1",
        "title": "Software Engineering Intern",
        "company": "Stripe",
        "url": "https://boards.greenhouse.io/stripe/jobs/1",
        "apply_url": "https://boards.greenhouse.io/stripe/jobs/1",
    }
    data.update(overrides)
    posting, _ = upsert_posting(session, data)
    return posting


# ── _load_yaml_config ─────────────────────────────────────────────────────────


def test_load_yaml_config_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _load_yaml_config(tmp_path / "nope.yaml") == {}


def test_load_yaml_config_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("matching:\n  min_score: 80\n", encoding="utf-8")
    config = _load_yaml_config(path)
    assert config["matching"]["min_score"] == 80


# ── _load_company_names ────────────────────────────────────────────────────────


def test_load_company_names_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _load_company_names(tmp_path / "nope.yaml") == []


def test_load_company_names_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - Stripe\n  - OpenAI\n", encoding="utf-8")
    assert _load_company_names(path) == ["Stripe", "OpenAI"]


def test_load_company_names_empty_companies_key(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text("companies: []\n", encoding="utf-8")
    assert _load_company_names(path) == []


# ── _fetch_all_postings ───────────────────────────────────────────────────────


def test_fetch_all_postings_aggregates_across_sources(engine: Engine) -> None:
    source_a = MagicMock(return_value=[_raw_posting(external_id="a1")])
    source_b = MagicMock(return_value=[_raw_posting(external_id="b1")])

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("a", source_a), ("b", source_b)]),
    ):
        postings, polled, errors = _fetch_all_postings(session)

    assert {p.external_id for p in postings} == {"a1", "b1"}
    assert polled == ["a", "b"]
    assert errors == []


def test_fetch_all_postings_continues_after_source_failure(engine: Engine) -> None:
    failing = MagicMock(side_effect=RuntimeError("boom"))
    working = MagicMock(return_value=[_raw_posting(external_id="ok1")])

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("broken", failing), ("fine", working)]),
    ):
        postings, polled, errors = _fetch_all_postings(session)

    assert {p.external_id for p in postings} == {"ok1"}
    assert polled == ["fine"]
    assert len(errors) == 1
    assert "broken" in errors[0]


# ── _dedupe_and_store ─────────────────────────────────────────────────────────


def test_dedupe_and_store_counts_only_new(engine: Engine) -> None:
    postings = [
        _raw_posting(external_id="1"),
        _raw_posting(external_id="1"),
        _raw_posting(
            external_id="2",
            title="Data Science Intern",
            description="A completely different role in a different team.",
            url="https://boards.greenhouse.io/stripe/jobs/2",
            apply_url="https://boards.greenhouse.io/stripe/jobs/2",
        ),
    ]
    with session_scope(engine) as session:
        new_count = _dedupe_and_store(session, postings)
    assert new_count == 2


# ── _to_scoring_posting ───────────────────────────────────────────────────────


def test_to_scoring_posting_uses_db_id_as_external_id(engine: Engine) -> None:
    with session_scope(engine) as session:
        posting = _stored_posting(session)
        raw = _to_scoring_posting(posting)
    assert raw.external_id == str(posting.id)
    assert raw.title == "Software Engineering Intern"


# ── _score_and_record ─────────────────────────────────────────────────────────


def test_score_and_record_empty_list_skips_claude(engine: Engine) -> None:
    with session_scope(engine) as session, patch("src.main.score_postings") as mock_score:
        result = _score_and_record(session, [], {}, {}, "hash1", _settings())
    mock_score.assert_not_called()
    assert result.scored == []


def test_score_and_record_writes_scores_back_by_db_id(engine: Engine) -> None:
    with session_scope(engine) as session:
        p1 = _stored_posting(session, external_id="1")
        p2 = _stored_posting(
            session, external_id="2", apply_url="https://boards.greenhouse.io/stripe/jobs/2"
        )

        fake_result = ScoringResult(
            scored=[
                ScoredPosting(external_id=str(p1.id), score=90, reasoning="Great fit."),
                ScoredPosting(external_id=str(p2.id), score=40, reasoning="Weak fit."),
            ],
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd=0.001,
        )
        with patch("src.main.score_postings", return_value=fake_result):
            _score_and_record(session, [p1, p2], {}, {}, "hash1", _settings())

    with session_scope(engine) as session:
        rows = {r.external_id: r for r in session.query(JobPosting).all()}
        assert rows["1"].match_score == 90
        assert rows["1"].profile_hash == "hash1"
        assert rows["2"].match_score == 40


# ── _to_match_info ────────────────────────────────────────────────────────────


def test_to_match_info_falls_back_to_url_when_no_apply_url(engine: Engine) -> None:
    with session_scope(engine) as session:
        posting = _stored_posting(session, apply_url=None, match_score=80, match_reasoning="Good.")
        info = _to_match_info(posting)
    assert info.apply_url == posting.url
    assert info.score == 80


# ── _notify_and_mark ──────────────────────────────────────────────────────────


def test_notify_and_mark_empty_list_returns_zero(engine: Engine) -> None:
    with session_scope(engine) as session:
        count = _notify_and_mark(session, [], _settings())
    assert count == 0


def test_notify_and_mark_individual_mode_marks_each_posting(engine: Engine) -> None:
    with session_scope(engine) as session:
        p1 = _stored_posting(session, external_id="1", match_score=80)
        p2 = _stored_posting(
            session,
            external_id="2",
            apply_url="https://boards.greenhouse.io/stripe/jobs/2",
            match_score=85,
        )

        fake_results = [
            SendResult(success=True, message_id=1, error=None),
            SendResult(success=True, message_id=2, error=None),
        ]
        with patch("src.main.notify_matches", return_value=fake_results) as mock_notify:
            count = _notify_and_mark(session, [p1, p2], _settings(burst_threshold=5))

    assert count == 2
    mock_notify.assert_called_once()

    with session_scope(engine) as session:
        rows = {r.external_id: r for r in session.query(JobPosting).all()}
        assert rows["1"].notified is True
        assert rows["2"].notified is True


def test_notify_and_mark_burst_mode_marks_all_from_single_result(engine: Engine) -> None:
    with session_scope(engine) as session:
        postings = [
            _stored_posting(
                session,
                external_id=str(i),
                apply_url=f"https://boards.greenhouse.io/stripe/jobs/{i}",
                match_score=80,
            )
            for i in range(5)
        ]

        with patch(
            "src.main.notify_matches",
            return_value=[SendResult(success=True, message_id=999, error=None)],
        ):
            count = _notify_and_mark(session, postings, _settings(burst_threshold=5))

    assert count == 5
    with session_scope(engine) as session:
        rows = session.query(JobPosting).all()
        assert all(r.notified for r in rows)
        assert all(r.notified_at is not None for r in rows)


def test_notify_and_mark_does_not_mark_on_send_failure(engine: Engine) -> None:
    with session_scope(engine) as session:
        p1 = _stored_posting(session, external_id="1", match_score=80)

        with patch(
            "src.main.notify_matches",
            return_value=[SendResult(success=False, message_id=None, error="failed")],
        ):
            count = _notify_and_mark(session, [p1], _settings(burst_threshold=5))

    assert count == 0
    with session_scope(engine) as session:
        row = session.query(JobPosting).one()
        assert row.notified is False


# ── _to_partial_match_info ────────────────────────────────────────────────────


def test_to_partial_match_info_maps_fields(engine: Engine) -> None:
    with session_scope(engine) as session:
        posting = _stored_posting(session)
        record_score(
            session, posting.id, 62, "Close fit.", "hash", missing_qualifications=["Kubernetes"]
        )

    with session_scope(engine) as session:
        posting = session.query(JobPosting).one()
        info = _to_partial_match_info(posting)
    assert info.score == 62
    assert info.missing_qualifications == ["Kubernetes"]


def test_to_partial_match_info_falls_back_to_url_when_no_apply_url(engine: Engine) -> None:
    with session_scope(engine) as session:
        posting = _stored_posting(session, apply_url=None)
        record_score(session, posting.id, 60, "x", "hash")

    with session_scope(engine) as session:
        posting = session.query(JobPosting).one()
        info = _to_partial_match_info(posting)
    assert info.apply_url == posting.url


# ── _notify_partial_matches_and_mark ──────────────────────────────────────────


def test_notify_partial_matches_and_mark_empty_list_returns_zero(engine: Engine) -> None:
    with session_scope(engine) as session:
        count = _notify_partial_matches_and_mark(session, [], _settings())
    assert count == 0


def test_notify_partial_matches_and_mark_individual_mode_marks_each_posting(engine: Engine) -> None:
    with session_scope(engine) as session:
        postings = []
        for i in range(3):
            p = _stored_posting(
                session,
                external_id=str(i),
                apply_url=f"https://boards.greenhouse.io/stripe/jobs/{i}",
            )
            record_score(session, p.id, 60, "x", "hash", missing_qualifications=["Kubernetes"])
            postings.append(p)

        fake_results = [SendResult(success=True, message_id=i, error=None) for i in range(3)]
        with patch("src.main.notify_partial_matches", return_value=fake_results) as mock_notify:
            count = _notify_partial_matches_and_mark(
                session, postings, _settings(burst_threshold=5)
            )

    assert count == 3
    mock_notify.assert_called_once()

    with session_scope(engine) as session:
        rows = session.query(JobPosting).all()
        assert all(r.partial_notified for r in rows)
        assert all(not r.notified for r in rows)


def test_notify_partial_matches_and_mark_burst_mode_marks_all_from_single_result(
    engine: Engine,
) -> None:
    with session_scope(engine) as session:
        postings = []
        for i in range(5):
            p = _stored_posting(
                session,
                external_id=str(i),
                apply_url=f"https://boards.greenhouse.io/stripe/jobs/{i}",
            )
            record_score(session, p.id, 60, "x", "hash", missing_qualifications=["Kubernetes"])
            postings.append(p)

        with patch(
            "src.main.notify_partial_matches",
            return_value=[SendResult(success=True, message_id=999, error=None)],
        ):
            count = _notify_partial_matches_and_mark(
                session, postings, _settings(burst_threshold=5)
            )

    assert count == 5
    with session_scope(engine) as session:
        rows = session.query(JobPosting).all()
        assert all(r.partial_notified for r in rows)


def test_notify_partial_matches_and_mark_does_not_mark_on_send_failure(engine: Engine) -> None:
    with session_scope(engine) as session:
        p = _stored_posting(session)
        record_score(session, p.id, 60, "x", "hash")

        with patch(
            "src.main.notify_partial_matches",
            return_value=[SendResult(success=False, message_id=None, error="failed")],
        ):
            count = _notify_partial_matches_and_mark(session, [p], _settings(burst_threshold=5))

    assert count == 0
    with session_scope(engine) as session:
        row = session.query(JobPosting).one()
        assert row.partial_notified is False


# ── run_cycle ─────────────────────────────────────────────────────────────────


def test_run_cycle_seeds_companies_from_config(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  min_score: 50\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "companies.yaml").write_text(
        "companies:\n  - Stripe\n  - OpenAI\n", encoding="utf-8"
    )

    fake_score = ScoringResult(scored=[], input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", MagicMock(return_value=[]))]),
        patch("src.main.score_postings", return_value=fake_score),
        patch(
            "src.main.seed_companies",
            return_value=SeedResult(total_configured=2, newly_attempted=2, already_known=0),
        ) as mock_seed,
    ):
        run_cycle(session, _settings(), dry_run=True)

    mock_seed.assert_called_once()
    args, _ = mock_seed.call_args
    assert args[1] == ["Stripe", "OpenAI"]


def test_run_cycle_skips_seeding_when_no_companies_configured(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  min_score: 50\n", encoding="utf-8")
    fake_score = ScoringResult(scored=[], input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", MagicMock(return_value=[]))]),
        patch("src.main.score_postings", return_value=fake_score),
        patch("src.main.seed_companies") as mock_seed,
    ):
        run_cycle(session, _settings(), dry_run=True)

    mock_seed.assert_not_called()


def test_run_cycle_dry_run_never_sends_notifications(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  min_score: 50\n", encoding="utf-8")

    source = MagicMock(return_value=[_raw_posting(external_id="1")])
    fake_score = ScoringResult(scored=[], input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", source)]),
        patch("src.main.score_postings", return_value=fake_score),
        patch("src.main.notify_matches") as mock_notify,
    ):
        summary = run_cycle(session, _settings(), dry_run=True)

    mock_notify.assert_not_called()
    assert summary["postings_new"] == 1
    assert summary["alerts_sent"] == 0


def test_run_cycle_sends_alerts_for_matches_above_threshold(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  min_score: 50\n", encoding="utf-8")

    source = MagicMock(return_value=[_raw_posting(external_id="1")])

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", source)]),
    ):
        # First: fetch + dedupe inserts the posting, unscored.
        run_cycle_postings = _fetch_all_postings(session)[0]
        _dedupe_and_store(session, run_cycle_postings)
        stored = session.query(JobPosting).one()
        db_id = stored.id

    fake_score = ScoringResult(
        scored=[ScoredPosting(external_id=str(db_id), score=90, reasoning="Great fit.")],
        input_tokens=10,
        output_tokens=5,
        estimated_cost_usd=0.0001,
    )

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", MagicMock(return_value=[]))]),
        patch("src.main.score_postings", return_value=fake_score),
        patch(
            "src.main.notify_matches",
            return_value=[SendResult(success=True, message_id=1, error=None)],
        ) as mock_notify,
    ):
        summary = run_cycle(session, _settings(), dry_run=False)

    assert summary["alerts_sent"] == 1
    mock_notify.assert_called_once()


def test_run_cycle_sends_partial_match_digest(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text(
        "matching:\n  min_score: 70\n  partial_match_min_score: 50\n  max_missing_qualifications: 5\n",
        encoding="utf-8",
    )

    source = MagicMock(return_value=[_raw_posting(external_id="1")])

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", source)]),
    ):
        run_cycle_postings = _fetch_all_postings(session)[0]
        _dedupe_and_store(session, run_cycle_postings)
        db_id = session.query(JobPosting).one().id

    fake_score = ScoringResult(
        scored=[
            ScoredPosting(
                external_id=str(db_id),
                score=60,
                reasoning="Close fit.",
                missing_qualifications=["Kubernetes"],
            )
        ],
        input_tokens=10,
        output_tokens=5,
        estimated_cost_usd=0.0001,
    )

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", MagicMock(return_value=[]))]),
        patch("src.main.score_postings", return_value=fake_score),
        patch(
            "src.main.notify_partial_matches",
            return_value=[SendResult(success=True, message_id=1, error=None)],
        ) as mock_notify,
    ):
        run_cycle(session, _settings(), dry_run=False)

    mock_notify.assert_called_once()
    sent_infos = mock_notify.call_args.args[1]
    assert len(sent_infos) == 1
    assert sent_infos[0].missing_qualifications == ["Kubernetes"]

    with session_scope(engine) as session:
        row = session.query(JobPosting).one()
        assert row.partial_notified is True
        assert row.notified is False


def test_run_cycle_dry_run_never_sends_partial_match_digest(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text(
        "matching:\n  min_score: 70\n  partial_match_min_score: 50\n", encoding="utf-8"
    )

    source = MagicMock(return_value=[_raw_posting(external_id="1")])
    fake_score = ScoringResult(
        scored=[ScoredPosting(external_id="1", score=60, reasoning="Close fit.")],
        input_tokens=10,
        output_tokens=5,
        estimated_cost_usd=0.0001,
    )

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", source)]),
        patch("src.main.score_postings", return_value=fake_score),
        patch("src.main.send_message_with_retry") as mock_send,
        patch("src.main.notify_partial_matches") as mock_notify_partial,
    ):
        run_cycle(session, _settings(), dry_run=True)

    mock_send.assert_not_called()
    mock_notify_partial.assert_not_called()


def test_run_cycle_records_errors_from_failed_sources(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  min_score: 70\n", encoding="utf-8")

    failing = MagicMock(side_effect=RuntimeError("API down"))
    fake_score = ScoringResult(scored=[], input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)

    with (
        session_scope(engine) as session,
        patch("src.main._SOURCES", [("greenhouse", failing)]),
        patch("src.main.score_postings", return_value=fake_score),
    ):
        summary = run_cycle(session, _settings(), dry_run=True)

    assert len(summary["errors"]) == 1
    assert "greenhouse" in summary["errors"][0]


# ── _get_top_match_this_week ──────────────────────────────────────────────────


def test_get_top_match_this_week_returns_highest_score(engine: Engine) -> None:
    with session_scope(engine) as session:
        _stored_posting(session, external_id="1", match_score=60, found_at=_now())
        _stored_posting(
            session,
            external_id="2",
            apply_url="https://boards.greenhouse.io/stripe/jobs/2",
            match_score=95,
            found_at=_now(),
        )

    with session_scope(engine) as session:
        top = _get_top_match_this_week(session)
    assert top is not None
    assert top.match_score == 95


def test_get_top_match_this_week_none_when_empty(engine: Engine) -> None:
    with session_scope(engine) as session:
        assert _get_top_match_this_week(session) is None


# ── run_digest ────────────────────────────────────────────────────────────────


def test_run_digest_skipped_when_disabled(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  digest_enabled: false\n", encoding="utf-8")

    with (
        session_scope(engine) as session,
        patch("src.main.retry_unresolved") as mock_retry,
    ):
        run_digest(session, _settings())

    mock_retry.assert_not_called()


def test_run_digest_sends_summary_message(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  digest_enabled: true\n", encoding="utf-8")

    with (
        session_scope(engine) as session,
        patch(
            "src.main.retry_unresolved",
            return_value=RetryResult(attempted=2, resolved=1, still_unresolved=1),
        ),
        patch("src.main.send_message_with_retry") as mock_send,
    ):
        run_digest(session, _settings())

    mock_send.assert_called_once()
    body = mock_send.call_args.args[2]
    assert "Digest" in body


# ── run_adzuna_poll ────────────────────────────────────────────────────────────


def test_run_adzuna_poll_skipped_without_credentials(engine: Engine) -> None:
    with (
        session_scope(engine) as session,
        patch("src.main.adzuna.fetch") as mock_fetch,
    ):
        run_adzuna_poll(session, _settings(adzuna_app_id=None, adzuna_app_key=None))

    mock_fetch.assert_not_called()


def test_run_adzuna_poll_fetches_and_stores(engine: Engine) -> None:
    postings = [_raw_posting(source="adzuna", external_id="1")]

    with (
        session_scope(engine) as session,
        patch("src.main.adzuna.fetch", return_value=postings) as mock_fetch,
    ):
        run_adzuna_poll(session, _settings(adzuna_app_id="app-id", adzuna_app_key="app-key"))

    mock_fetch.assert_called_once_with("app-id", "app-key")
    with session_scope(engine) as session:
        assert session.query(JobPosting).count() == 1


def test_run_digest_counts_only_recent_postings(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest now runs hourly, so its 'new postings' count must be scoped
    to postings found in the last hour, not get_stats()'s all-time total."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  digest_enabled: true\n", encoding="utf-8")

    with session_scope(engine) as session:
        _stored_posting(
            session,
            external_id="old",
            apply_url="https://boards.greenhouse.io/stripe/jobs/old",
            found_at=_now() - timedelta(days=2),
        )
        _stored_posting(
            session,
            external_id="recent",
            apply_url="https://boards.greenhouse.io/stripe/jobs/recent",
            found_at=_now(),
        )

    with (
        session_scope(engine) as session,
        patch(
            "src.main.retry_unresolved",
            return_value=RetryResult(attempted=0, resolved=0, still_unresolved=0),
        ),
        patch("src.main.send_message_with_retry") as mock_send,
    ):
        run_digest(session, _settings())

    body = mock_send.call_args.args[2]
    assert "1 new postings" in body


def test_run_digest_counts_alerts_by_when_sent_not_when_found(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alert for an old posting still counts toward this period's
    alerts-sent total if it was actually sent within the window."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profile.yaml").write_text("matching:\n  digest_enabled: true\n", encoding="utf-8")

    with session_scope(engine) as session:
        old = _stored_posting(
            session,
            external_id="old",
            apply_url="https://boards.greenhouse.io/stripe/jobs/old",
            found_at=_now() - timedelta(days=2),
        )
        mark_notified(session, old.id, "123", "old alert")

    with (
        session_scope(engine) as session,
        patch(
            "src.main.retry_unresolved",
            return_value=RetryResult(attempted=0, resolved=0, still_unresolved=0),
        ),
        patch("src.main.send_message_with_retry") as mock_send,
    ):
        run_digest(session, _settings())

    body = mock_send.call_args.args[2]
    assert "1 alerts sent" in body
