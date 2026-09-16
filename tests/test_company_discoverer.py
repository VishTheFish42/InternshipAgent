"""Unit tests for company_discoverer — all web calls are mocked."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.company_discoverer import (
    RetryResult,
    SeedResult,
    _guess_greenhouse_or_lever,
    _is_usable,
    _normalize,
    _RateLimiter,
    _serpapi,
    _slug_candidates,
    _verify_greenhouse_slug,
    _verify_lever_slug,
    discover,
    retry_unresolved,
    seed_companies,
)
from src.db import Base, CompanyLookup

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


@pytest.fixture(autouse=True)
def _no_real_network_guessing():
    """Guess-and-verify (T-3xx) makes real HTTP calls to Greenhouse/Lever's
    public APIs, unconditionally, even without a search_api_key — patched
    here to a safe default (no match) so every existing test keeps
    exercising discover()'s bundled-table/search logic deterministically,
    without hitting the network. Tests that actually want to exercise the
    guess step override this patch explicitly."""
    with patch("src.company_discoverer._guess_greenhouse_or_lever", return_value=None):
        yield


# ── Normalisation ─────────────────────────────────────────────────────────────


def test_normalize_lowercases():
    assert _normalize("Stripe") == "stripe"


def test_normalize_strips_whitespace():
    assert _normalize("  Scale AI  ") == "scale ai"


def test_normalize_collapses_internal_spaces():
    assert _normalize("Palo  Alto  Networks") == "palo alto networks"


def test_normalize_preserves_dots_and_ampersands():
    assert _normalize("Monday.com") == "monday.com"
    assert _normalize("Weights & Biases") == "weights & biases"


# ── _is_usable ────────────────────────────────────────────────────────────────


def test_is_usable_true_for_greenhouse_with_slug():
    assert _is_usable("greenhouse", "stripe", None) is True


def test_is_usable_false_for_legacy_type():
    assert _is_usable("custom", None, "https://careers.example.com") is False


def test_is_usable_false_for_workday_missing_slug():
    assert _is_usable("workday", None, None) is False


def test_is_usable_true_for_llm_scrape_with_url():
    assert _is_usable("llm_scrape", None, "https://careers.example.com") is True


def test_is_usable_false_for_llm_scrape_missing_url():
    assert _is_usable("llm_scrape", None, None) is False


def test_is_usable_false_for_none_ats_type():
    assert _is_usable(None, "some-slug", None) is False


# ── _RateLimiter ──────────────────────────────────────────────────────────────


def test_rate_limiter_does_not_wait_on_first_call():
    limiter = _RateLimiter(min_interval_seconds=1.0)
    with (
        patch("src.company_discoverer.time.monotonic", side_effect=[100.0, 100.0]),
        patch("src.company_discoverer.time.sleep") as mock_sleep,
    ):
        limiter.wait()
    mock_sleep.assert_not_called()


def test_rate_limiter_waits_out_the_remaining_interval():
    limiter = _RateLimiter(min_interval_seconds=1.0)
    with (
        patch(
            "src.company_discoverer.time.monotonic",
            side_effect=[100.0, 100.0, 100.3, 100.3],
        ),
        patch("src.company_discoverer.time.sleep") as mock_sleep,
    ):
        limiter.wait()
        limiter.wait()
    mock_sleep.assert_called_once_with(pytest.approx(0.7))


def test_rate_limiter_does_not_wait_once_interval_has_elapsed():
    limiter = _RateLimiter(min_interval_seconds=1.0)
    with (
        patch(
            "src.company_discoverer.time.monotonic",
            side_effect=[100.0, 100.0, 102.0, 102.0],
        ),
        patch("src.company_discoverer.time.sleep") as mock_sleep,
    ):
        limiter.wait()
        limiter.wait()
    mock_sleep.assert_not_called()


# ── _serpapi: logging and rate limiting ──────────────────────────────────────


def test_serpapi_calls_the_rate_limiter():
    with (
        patch("src.company_discoverer._serpapi_rate_limiter") as mock_limiter,
        patch("src.company_discoverer.httpx.get") as mock_get,
    ):
        mock_get.return_value.json.return_value = {"organic_results": []}
        _serpapi("query", "fake-key")
    mock_limiter.wait.assert_called_once()


def test_serpapi_logs_warning_on_http_error(caplog):
    fake_response = MagicMock(status_code=429, text='{"error": "quota exhausted"}')
    fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "429", request=MagicMock(), response=fake_response
    )

    with (
        patch("src.company_discoverer._serpapi_rate_limiter.wait"),
        patch("src.company_discoverer.httpx.get", return_value=fake_response),
        caplog.at_level(logging.WARNING, logger="src.company_discoverer"),
    ):
        result = _serpapi("query", "fake-key")

    assert result == []
    assert "429" in caplog.text
    assert "quota exhausted" in caplog.text


def test_serpapi_logs_warning_on_generic_exception(caplog):
    with (
        patch("src.company_discoverer._serpapi_rate_limiter.wait"),
        patch("src.company_discoverer.httpx.get", side_effect=httpx.ConnectError("boom")),
        caplog.at_level(logging.WARNING, logger="src.company_discoverer"),
    ):
        result = _serpapi("query", "fake-key")

    assert result == []
    assert "SerpAPI request failed" in caplog.text


# ── Step 1: Bundled table hits ────────────────────────────────────────────────


def test_bundled_table_greenhouse(session):
    record = discover("Stripe", session)
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"
    assert record.slug == "stripe"
    assert record.source == "bundled_table"


def test_bundled_table_lever(session):
    record = discover("Linear", session)
    assert record.status == "resolved"
    assert record.ats_type == "lever"
    assert record.slug == "linear"


def test_bundled_table_legacy_ats_type_is_not_trusted_as_final(session):
    """Google's bundled entry is {"ats": "custom"} — a legacy, unscraped
    value (T-309/T-310). Without a search key to try anything better, this
    must come back 'unresolved', not silently 'resolved' — the exact class
    of bug that caused the missed Nvidia posting (design.md §3.5)."""
    record = discover("Google", session)
    assert record.status == "unresolved"


def test_bundled_table_legacy_ats_type_falls_through_to_llm_scrape(session):
    """With a search key, a bundled 'custom' entry now gets a real chance
    to resolve via Step 3 (generic careers search) instead of the bundled
    table being the unconditional final word (T-310)."""

    def fake_serpapi(query: str, api_key: str, num: int = 5):
        if "greenhouse" in query or "lever" in query or "myworkdayjobs" in query:
            return []
        return [{"link": "https://careers.google.com/jobs/results/"}]

    with patch("src.company_discoverer._serpapi", side_effect=fake_serpapi):
        record = discover("Google", session, search_api_key="k")
    assert record.status == "resolved"
    assert record.ats_type == "llm_scrape"
    assert record.url == "https://careers.google.com/jobs/results/"


def test_bundled_table_workday_missing_slug_skips_straight_to_workday_search(session):
    """A bundled entry already tagged ats: "workday" but with no slug
    (e.g. Cisco/Qualcomm before hand-correction, T-310) should re-attempt
    only the Workday search, not waste queries re-checking Greenhouse/Lever
    for a company already known not to be there."""
    with (
        patch(
            "src.company_discoverer._load_ats_map",
            return_value={"acme corp": {"ats": "workday"}},
        ),
        patch("src.company_discoverer._guess_greenhouse_or_lever") as mock_guess,
        patch("src.company_discoverer._search_ats_combined") as mock_combined,
        patch(
            "src.company_discoverer._search_workday",
            return_value=("acmecorp", "wd3", "External"),
        ) as mock_workday,
    ):
        record = discover("Acme Corp", session, search_api_key="k")

    mock_guess.assert_not_called()
    mock_combined.assert_not_called()
    mock_workday.assert_called_once()
    assert record.status == "resolved"
    assert record.ats_type == "workday"
    assert record.slug == "acmecorp.wd3/External"


def test_bundled_table_workday(session):
    record = discover("Salesforce", session)
    assert record.status == "resolved"
    assert record.ats_type == "workday"
    # Verified live against Salesforce's real Workday tenant (2026-08-20,
    # see docs/tasks.md T-470) — previously this bundled entry had no slug
    # at all, meaning "resolved" didn't actually mean "fetchable" (the same
    # dead-end class of bug the Nvidia miss surfaced, design.md §3.5).
    assert record.slug == "salesforce.wd12/External_Career_Site"


def test_bundled_table_case_insensitive(session):
    record = discover("STRIPE", session)
    assert record.status == "resolved"
    assert record.slug == "stripe"


def test_bundled_table_multiword(session):
    record = discover("Palo Alto Networks", session)
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"


# ── Step 0: DB cache ──────────────────────────────────────────────────────────


def test_db_cache_returns_existing_resolved(session):
    # First call populates the DB
    r1 = discover("Stripe", session)
    assert r1.source == "bundled_table"

    # Second call should hit the DB cache without re-reading the ATS map
    with patch("src.company_discoverer._load_ats_map") as mock_map:
        r2 = discover("Stripe", session)
    mock_map.assert_not_called()
    assert r2.status == "resolved"
    assert r2.slug == "stripe"


def test_force_bypasses_cache(session):
    discover("Stripe", session)
    session.commit()

    with patch(
        "src.company_discoverer._load_ats_map",
        return_value={"stripe": {"ats": "greenhouse", "slug": "stripe-new"}},
    ) as mock_map:
        r = discover("Stripe", session, force=True)
    mock_map.assert_called_once()
    assert r.slug == "stripe-new"


# ── Step 3: Direct guess-and-verify (Greenhouse/Lever) ───────────────────────


def test_slug_candidates_concatenates_and_hyphenates():
    assert _slug_candidates("Cadence Design Systems") == [
        "cadencedesignsystems",
        "cadence-design-systems",
    ]


def test_slug_candidates_single_word_has_no_duplicate():
    assert _slug_candidates("Stripe") == ["stripe"]


def test_slug_candidates_strips_punctuation():
    assert _slug_candidates("Monday.com") == ["mondaycom", "monday-com"]


def test_slug_candidates_empty_for_no_alnum_chars():
    assert _slug_candidates("!!!") == []


def test_verify_greenhouse_slug_true_on_valid_board():
    fake_response = MagicMock(status_code=200)
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"jobs": []}
    with (
        patch("src.company_discoverer._guess_rate_limiter.wait"),
        patch("src.company_discoverer.httpx.get", return_value=fake_response),
    ):
        assert _verify_greenhouse_slug("acmecorp") is True


def test_verify_greenhouse_slug_false_on_404():
    fake_response = MagicMock(status_code=404)
    fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=fake_response
    )
    with (
        patch("src.company_discoverer._guess_rate_limiter.wait"),
        patch("src.company_discoverer.httpx.get", return_value=fake_response),
    ):
        assert _verify_greenhouse_slug("nonexistent") is False


def test_verify_lever_slug_true_on_valid_board():
    fake_response = MagicMock(status_code=200)
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = []
    with (
        patch("src.company_discoverer._guess_rate_limiter.wait"),
        patch("src.company_discoverer.httpx.get", return_value=fake_response),
    ):
        assert _verify_lever_slug("acmecorp") is True


def test_verify_lever_slug_false_on_network_error():
    with (
        patch("src.company_discoverer._guess_rate_limiter.wait"),
        patch("src.company_discoverer.httpx.get", side_effect=httpx.ConnectError("boom")),
    ):
        assert _verify_lever_slug("acmecorp") is False


def test_guess_greenhouse_or_lever_hits_greenhouse_first_candidate():
    with (
        patch("src.company_discoverer._verify_greenhouse_slug", return_value=True),
        patch("src.company_discoverer._verify_lever_slug") as mock_lever,
    ):
        result = _guess_greenhouse_or_lever("Acme Corp")
    assert result == ("greenhouse", "acmecorp")
    mock_lever.assert_not_called()


def test_guess_greenhouse_or_lever_falls_back_to_lever():
    with (
        patch("src.company_discoverer._verify_greenhouse_slug", return_value=False),
        patch(
            "src.company_discoverer._verify_lever_slug",
            side_effect=lambda slug: slug == "acme-corp",
        ),
    ):
        result = _guess_greenhouse_or_lever("Acme Corp")
    assert result == ("lever", "acme-corp")


def test_guess_greenhouse_or_lever_returns_none_when_both_miss():
    with (
        patch("src.company_discoverer._verify_greenhouse_slug", return_value=False),
        patch("src.company_discoverer._verify_lever_slug", return_value=False),
    ):
        assert _guess_greenhouse_or_lever("Acme Corp") is None


def test_discover_guess_hit_resolves_without_any_search_call(session):
    with (
        patch(
            "src.company_discoverer._guess_greenhouse_or_lever",
            return_value=("greenhouse", "acmecorp"),
        ),
        patch("src.company_discoverer._serpapi") as mock_serpapi,
    ):
        record = discover("Acme Corp", session, search_api_key="k")
    mock_serpapi.assert_not_called()
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"
    assert record.slug == "acmecorp"
    assert record.source == "guessed_verified"


def test_discover_guess_works_without_a_search_api_key(session):
    # The entire point of this step: it needs no SerpAPI key at all.
    with patch(
        "src.company_discoverer._guess_greenhouse_or_lever",
        return_value=("lever", "acmecorp"),
    ):
        record = discover("Acme Corp", session, search_api_key=None)
    assert record.status == "resolved"
    assert record.ats_type == "lever"
    assert record.source == "guessed_verified"


# ── Step 4: Combined ATS web search (Greenhouse/Lever/Workday, one query) ────


def test_combined_search_greenhouse_hit(session):
    fake_results = [{"link": "https://boards.greenhouse.io/acmecorp/jobs/123"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results) as mock_serpapi:
        record = discover("Acme Corp", session, search_api_key="fake-key")
    assert record.status == "resolved"
    assert record.ats_type == "greenhouse"
    assert record.slug == "acmecorp"
    assert record.source == "web_search"
    mock_serpapi.assert_called_once()  # one combined query, not three


def test_combined_search_greenhouse_extracts_slug_only(session):
    fake_results = [{"link": "https://boards.greenhouse.io/widgetsinc/jobs/9999?gh_src=xyz"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        record = discover("Widgets Inc", session, search_api_key="k")
    assert record.slug == "widgetsinc"


def test_combined_search_lever_hit(session):
    fake_results = [{"link": "https://jobs.lever.co/acmecorp/abc-123"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        record = discover("Acme Corp", session, search_api_key="k")
    assert record.ats_type == "lever"
    assert record.slug == "acmecorp"


def test_combined_search_workday_hit(session):
    fake_results = [{"link": "https://acmecorp.wd1.myworkdayjobs.com/External"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        record = discover("Acme Corp", session, search_api_key="k")
    assert record.ats_type == "workday"
    assert record.slug == "acmecorp.wd1/External"
    assert record.source == "web_search"


def test_combined_search_workday_extracts_site_past_locale_prefix_and_job_path(session):
    # Real-world shape live-observed against Salesforce's actual board
    # (docs/tasks.md T-470): a locale segment before the site, and further
    # path (a specific job listing) after it.
    fake_results = [
        {
            "link": "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site"
            "/job/AI-Builder--Emerging-Talent_JR341276-1"
        }
    ]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        record = discover("Salesforce", session, search_api_key="k")
    assert record.ats_type == "workday"
    assert record.slug == "salesforce.wd12/External_Career_Site"


def test_combined_search_miss_falls_through_to_careers_search(session):
    # Exactly 2 SerpAPI calls total (combined ATS query, then careers) even
    # though this used to take up to 4 (Greenhouse, Lever, Workday, careers)
    # — the concrete regression test for the quota-usage reduction itself.
    with patch(
        "src.company_discoverer._serpapi",
        side_effect=[[], [{"link": "https://careers.acmecorp.com/internships"}]],
    ) as mock_serpapi:
        record = discover("Acme Corp", session, search_api_key="k")
    assert record.ats_type == "llm_scrape"
    assert record.url == "https://careers.acmecorp.com/internships"
    assert mock_serpapi.call_count == 2


# ── Step 5: Generic careers search ───────────────────────────────────────────


def test_web_search_careers_hit(session):
    with patch(
        "src.company_discoverer._serpapi",
        side_effect=[[], [{"link": "https://careers.acmecorp.com/internships"}]],
    ):
        record = discover("Acme Corp", session, search_api_key="k")
    # ats_type="llm_scrape" (design.md §3.5, T-309) — not the old "custom",
    # which had no scraper behind it (the dead end that caused the missed
    # Nvidia posting). Only this newly-discovered-via-search path changed;
    # bundled_table entries hardcoded as "custom" are untested here since
    # discover() never reaches this step for them (see
    # test_bundled_table_custom_has_scraper above).
    assert record.ats_type == "llm_scrape"
    assert record.url == "https://careers.acmecorp.com/internships"
    assert record.slug is None


# ── Step 4: Unresolved ────────────────────────────────────────────────────────


def test_unresolved_when_all_searches_fail(session):
    with patch("src.company_discoverer._serpapi", return_value=[]):
        record = discover("Unknown Corp XYZ", session, search_api_key="k")
    assert record.status == "unresolved"
    assert record.source == "web_search"


def test_unresolved_when_no_api_key(session):
    record = discover("Unknown Corp XYZ", session, search_api_key=None)
    assert record.status == "unresolved"
    assert record.source is None


# ── DB persistence ────────────────────────────────────────────────────────────


def test_result_persisted_to_db(session):
    discover("Stripe", session)
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Stripe")
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "resolved"
    assert row.slug == "stripe"
    assert row.resolved_at is not None


def test_unresolved_persisted_to_db(session):
    with patch("src.company_discoverer._serpapi", return_value=[]):
        discover("Ghost Company", session, search_api_key="k")
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Ghost Company")
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "unresolved"
    assert row.resolved_at is None


def test_second_call_updates_existing_row(session):
    with patch("src.company_discoverer._serpapi", return_value=[]):
        discover("Ghost Company", session, search_api_key="k")
    session.commit()

    fake_gh = [{"link": "https://boards.greenhouse.io/ghostco/jobs/1"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_gh):
        discover("Ghost Company", session, search_api_key="k", force=True)
    session.commit()

    rows = list(
        session.execute(
            select(CompanyLookup).where(CompanyLookup.name_raw == "Ghost Company")
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].status == "resolved"
    assert rows[0].slug == "ghostco"


# ── retry_unresolved ──────────────────────────────────────────────────────────


def _unresolved_row(name: str, last_attempted: datetime | None = None) -> CompanyLookup:
    return CompanyLookup(
        name_raw=name,
        name_normalized=name.lower(),
        status="unresolved",
        last_attempted=last_attempted,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def test_retry_no_candidates(session):
    result = retry_unresolved(session)
    assert result == RetryResult(attempted=0, resolved=0, still_unresolved=0)


def test_retry_skips_resolved_companies(session):
    discover("Stripe", session)
    session.commit()
    result = retry_unresolved(session, default_min_age_hours=0)
    assert result.attempted == 0


def test_retry_targets_resolved_but_stuck_legacy_company(session):
    """T-310: a company resolved to a legacy ats_type ("custom") is stuck
    exactly the same way an unresolved one is — nothing polls it — so it
    must also be picked up by the retry job, not just genuinely-unresolved
    rows."""
    session.add(
        CompanyLookup(
            name_raw="Stuck Corp",
            name_normalized="stuck corp",
            ats_type="custom",
            slug=None,
            url=None,
            status="resolved",
        )
    )
    session.commit()

    fake_results = [{"link": "https://careers.stuckcorp.com/jobs"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        result = retry_unresolved(session, search_api_key="k", default_min_age_hours=0)

    assert result.attempted == 1
    assert result.resolved == 1
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Stuck Corp")
    ).scalar_one()
    assert row.ats_type == "llm_scrape"
    assert row.url == "https://careers.stuckcorp.com/jobs"


def test_retry_targets_resolved_workday_missing_slug(session):
    session.add(
        CompanyLookup(
            name_raw="Cisco",
            name_normalized="cisco",
            ats_type="workday",
            slug=None,
            url=None,
            status="resolved",
        )
    )
    session.commit()

    with patch(
        "src.company_discoverer._search_workday",
        return_value=("cisco", "wd1", "External"),
    ):
        result = retry_unresolved(session, search_api_key="k", default_min_age_hours=0)

    assert result.attempted == 1
    assert result.resolved == 1
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Cisco")
    ).scalar_one()
    assert row.slug == "cisco.wd1/External"


def test_retry_does_not_double_count_a_still_stuck_company_as_resolved(session):
    """A retried company that still can't be improved on (search finds
    nothing) must not inflate RetryResult.resolved just because it was
    already status="resolved" going in."""
    session.add(
        CompanyLookup(
            name_raw="Still Stuck Corp",
            name_normalized="still stuck corp",
            ats_type="custom",
            slug=None,
            url=None,
            status="resolved",
        )
    )
    session.commit()

    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k", default_min_age_hours=0)

    assert result.attempted == 1
    assert result.resolved == 0


def test_retry_skips_recently_attempted(session):
    session.add(_unresolved_row("Fresh Corp", last_attempted=_now()))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]) as mock_search:
        result = retry_unresolved(session, search_api_key="k", default_min_age_hours=1)
    mock_search.assert_not_called()
    assert result.attempted == 0


def test_retry_attempts_stale_company(session):
    stale_time = _now() - timedelta(hours=2)
    session.add(_unresolved_row("Stale Corp", last_attempted=stale_time))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k", default_min_age_hours=1)
    assert result.attempted == 1
    assert result.resolved == 0
    assert result.still_unresolved == 1


def test_retry_attempts_null_last_attempted(session):
    session.add(_unresolved_row("Unknown Corp"))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k")
    assert result.attempted == 1


def test_retry_resolves_on_success(session):
    session.add(_unresolved_row("Lucky Corp"))
    session.commit()
    fake_results = [{"link": "https://boards.greenhouse.io/luckycorp/jobs/1"}]
    with patch("src.company_discoverer._serpapi", return_value=fake_results):
        result = retry_unresolved(session, search_api_key="k")
    assert result.attempted == 1
    assert result.resolved == 1
    assert result.still_unresolved == 0
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Lucky Corp")
    ).scalar_one()
    assert row.status == "resolved"
    assert row.slug == "luckycorp"


def test_retry_updates_last_attempted_even_on_failure(session):
    session.add(_unresolved_row("Bad Corp"))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        retry_unresolved(session, search_api_key="k")
    session.commit()
    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Bad Corp")
    ).scalar_one()
    assert row.last_attempted is not None
    assert row.last_attempted >= _now() - timedelta(seconds=5)


def test_retry_mixed_results(session):
    session.add(_unresolved_row("Lucky Corp"))
    session.add(_unresolved_row("Bad Corp"))
    session.commit()

    def fake_serpapi(query: str, api_key: str, num: int = 5) -> list:
        if "Lucky Corp" in query and "greenhouse" in query:
            return [{"link": "https://boards.greenhouse.io/luckycorp/jobs/1"}]
        return []

    with patch("src.company_discoverer._serpapi", side_effect=fake_serpapi):
        result = retry_unresolved(session, search_api_key="k")
    assert result.attempted == 2
    assert result.resolved == 1
    assert result.still_unresolved == 1


def test_retry_default_min_age_hours_zero_includes_all_unresolved(session):
    session.add(_unresolved_row("Corp A", last_attempted=_now()))
    session.add(_unresolved_row("Corp B", last_attempted=_now() - timedelta(minutes=30)))
    session.commit()
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(session, search_api_key="k", default_min_age_hours=0)
    assert result.attempted == 2


# ── retry_unresolved: priority cooldown + attempt cap (T-312) ───────────────


def test_retry_priority_company_gets_tighter_cooldown(session):
    """A priority company past its (tighter) priority cooldown, but not
    yet past the (looser) default cooldown, must still be retried — that's
    the entire point of a separate, tighter interval for it."""
    attempted_5h_ago = _now() - timedelta(hours=5)
    session.add(_unresolved_row("Important Corp", last_attempted=attempted_5h_ago))
    session.add(_unresolved_row("Regular Corp", last_attempted=attempted_5h_ago))
    session.commit()

    with patch("src.company_discoverer._serpapi", return_value=[]) as mock_search:
        result = retry_unresolved(
            session,
            search_api_key="k",
            priority_names={"Important Corp"},
            priority_min_age_hours=4,
            default_min_age_hours=24,
        )

    # Only Important Corp (5h > 4h priority cooldown) is eligible — Regular
    # Corp (5h < 24h default cooldown) is not.
    assert result.attempted == 1
    mock_search.assert_called()
    for call in mock_search.call_args_list:
        assert "Regular Corp" not in call.args[0]


def test_retry_priority_name_matching_is_normalized(session):
    """priority_names is matched via the same _normalize() every other
    company-name comparison in this module uses — case/whitespace
    shouldn't matter."""
    attempted_5h_ago = _now() - timedelta(hours=5)
    session.add(_unresolved_row("Important Corp", last_attempted=attempted_5h_ago))
    session.commit()

    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(
            session,
            search_api_key="k",
            priority_names={"  IMPORTANT   corp  "},
            priority_min_age_hours=4,
            default_min_age_hours=24,
        )

    assert result.attempted == 1


def test_retry_caps_attempts_per_call(session):
    session.add_all([_unresolved_row(f"Corp {i}") for i in range(25)])
    session.commit()

    with patch("src.company_discoverer._serpapi", return_value=[]) as mock_search:
        result = retry_unresolved(session, search_api_key="k", max_attempts_per_call=20)

    assert result.attempted == 20
    assert mock_search.call_count > 0


def test_retry_priority_companies_are_never_starved_by_the_cap(session):
    session.add_all([_unresolved_row(f"Priority {i}") for i in range(5)])
    session.add_all([_unresolved_row(f"Regular {i}") for i in range(20)])
    session.commit()

    priority_names = {f"Priority {i}" for i in range(5)}
    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = retry_unresolved(
            session,
            search_api_key="k",
            priority_names=priority_names,
            max_attempts_per_call=5,
        )

    assert result.attempted == 5
    rows = session.execute(
        select(CompanyLookup).where(CompanyLookup.last_attempted.is_not(None))
    ).scalars()
    attempted_names = {row.name_raw for row in rows}
    assert attempted_names == priority_names


# ── seed_companies ─────────────────────────────────────────────────────────────


def test_seed_companies_empty_list(session):
    result = seed_companies(session, [])
    assert result == SeedResult(total_configured=0, newly_attempted=0, already_known=0)


def test_seed_companies_attempts_brand_new_company(session):
    with patch("src.company_discoverer._serpapi", return_value=[]) as mock_search:
        result = seed_companies(session, ["Stripe"], search_api_key="k")
    # "Stripe" hits the bundled table (step 1), so no web search is needed —
    # but it must still count as newly attempted since it had no prior row.
    mock_search.assert_not_called()
    assert result == SeedResult(total_configured=1, newly_attempted=1, already_known=0)

    row = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == "Stripe")
    ).scalar_one()
    assert row.status == "resolved"


def test_seed_companies_skips_already_resolved(session):
    discover("Stripe", session)
    session.commit()

    with patch("src.company_discoverer.discover") as mock_discover:
        result = seed_companies(session, ["Stripe"])

    mock_discover.assert_not_called()
    assert result == SeedResult(total_configured=1, newly_attempted=0, already_known=1)


def test_seed_companies_skips_already_unresolved():
    """An unresolved company must NOT be re-attempted on every seed call —
    that's retry_unresolved()'s job, on a 7-day cadence, to avoid burning
    search API quota every single cycle."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(_unresolved_row("Unknown Startup"))
        session.commit()

        with patch("src.company_discoverer.discover") as mock_discover:
            result = seed_companies(session, ["Unknown Startup"])

        mock_discover.assert_not_called()
        assert result == SeedResult(total_configured=1, newly_attempted=0, already_known=1)


def test_seed_companies_mixed_new_and_known(session):
    discover("Stripe", session)
    session.commit()

    with patch("src.company_discoverer._serpapi", return_value=[]):
        result = seed_companies(session, ["Stripe", "Some New Co"], search_api_key="k")

    assert result == SeedResult(total_configured=2, newly_attempted=1, already_known=1)
