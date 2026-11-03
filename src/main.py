"""InternshipAgent entry point: CLI, run_cycle, and scheduler orchestration."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import httpx
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from src.company_discoverer import retry_unresolved, seed_companies
from src.config import Settings, get_settings
from src.db import (
    JobPosting,
    get_bot_state,
    get_last_run_log,
    get_latest_notification_for_posting,
    get_unnotified_postings,
    get_unnotified_scored_postings,
    get_unscored_postings,
    init_db,
    log_run,
    mark_applied,
    mark_notified,
    record_score,
    session_scope,
    set_notifications_paused,
    update_last_telegram_update_id,
)
from src.deduplicator import upsert_deduped
from src.matcher import ScoringResult, hash_profile, score_postings
from src.notifier import (
    MatchInfo,
    answer_callback_query,
    bot_token_is_rejected,
    edit_message_mark_applied,
    format_burst_messages,
    format_individual_message,
    get_updates,
    notify_matches,
    send_message_with_retry,
)
from src.resume_extractor import rebuild_profile
from src.scrapers import (
    adzuna,
    greenhouse,
    hn,
    indeed_rss,
    jsearch,
    lever,
    llm_scrape,
    remoteok,
    workday,
)
from src.scrapers.base import RawPosting

_RESUMES_DIR = Path("resumes")
_PROFILE_YAML = Path("profile.yaml")
_COMPANIES_YAML = Path("config/companies.yaml")
_log = logging.getLogger(__name__)

# Every registered Tier 1/2 source polled on the *main* cycle. Adzuna and
# JSearch are deliberately excluded here and polled on their own slower jobs
# instead (run_adzuna_poll/run_jsearch_poll, below) — both are metered per
# month, which the main cycle's interval would exhaust quickly. llm_scrape
# (Tier B, design.md §3.5) is excluded for a different reason — it costs a
# real Claude call per company per poll, not quota — and also runs on its
# own slower job (run_llm_scrape_poll). YC and Dice are not yet wired into
# any schedule.
_SOURCES: list[tuple[str, Callable[[Session], list[RawPosting]]]] = [
    ("greenhouse", greenhouse.fetch_for_resolved_companies),
    ("lever", lever.fetch_for_resolved_companies),
    ("workday", workday.fetch_for_resolved_companies),
    ("indeed", lambda _session: indeed_rss.fetch_all()),
    ("hn", lambda _session: hn.fetch_current_month_postings()),
    ("remoteok", lambda _session: remoteok.fetch()),
]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _load_yaml_config(path: Path = _PROFILE_YAML) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_companies_yaml_list(
    settings: Settings, key: str, path: Path = _COMPANIES_YAML
) -> list[str]:
    """Prefer COMPANIES_CONFIG (base64 companies.yaml, set for the deployed
    container since config/companies.yaml is gitignored); fall back to the
    local file for dev. Shared by _load_company_names() (key="companies")
    and _load_priority_company_names() (key="priority_companies", T-312)
    — same file, same env-var-vs-local-file precedence, different key."""
    if settings.companies_config:
        raw = base64.b64decode(settings.companies_config).decode("utf-8")
        data = yaml.safe_load(raw) or {}
        return list(data.get(key, []))
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get(key, []))


def _load_company_names(settings: Settings, path: Path = _COMPANIES_YAML) -> list[str]:
    return _load_companies_yaml_list(settings, "companies", path)


def _load_priority_company_names(settings: Settings, path: Path = _COMPANIES_YAML) -> list[str]:
    """Companies retried on the tighter cooldown (T-312, design.md §3.3) —
    an optional, opt-in subset via companies.yaml's `priority_companies:`
    list. Empty by default; not required. Doesn't need to be a subset of
    `companies:` — retry_unresolved() only ever acts on companies already
    in company_lookup, so a name here that was never seeded is simply a
    no-op, not an error."""
    return _load_companies_yaml_list(settings, "priority_companies", path)


# ── Fetch & dedupe ────────────────────────────────────────────────────────────


def _fetch_all_postings(session: Session) -> tuple[list[RawPosting], list[str], list[str]]:
    """Poll every registered source. A source failure is logged and skipped,
    never fatal to the run."""
    postings: list[RawPosting] = []
    polled: list[str] = []
    errors: list[str] = []
    for name, fetch_fn in _SOURCES:
        try:
            postings.extend(fetch_fn(session))
            polled.append(name)
        except Exception as exc:
            _log.warning("Source %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")
    return postings, polled, errors


def _is_fresh(posted_at: datetime | None, max_age_days: int) -> bool:
    """True if within the freshness window, or if posted_at is unreliable/absent
    for this source (benefit of the doubt rather than silently dropping it).

    Most scrapers build posted_at via datetime.fromisoformat(...+00:00), which
    is timezone-aware; only lever.py and indeed_rss.py normalize it to naive
    UTC themselves. _now() is always naive, so comparing directly raised
    TypeError: can't compare offset-naive and offset-aware datetimes whenever
    any posting from one of the other 7 sources had a real posted_at — which
    aborted the entire cycle (fetch/dedupe/score/notify, all of it) before a
    single alert could be sent. Normalizing here, at the one place the
    comparison happens, fixes it for every source at once rather than
    special-casing each scraper's own datetime construction."""
    if posted_at is None:
        return True
    if posted_at.tzinfo is not None:
        posted_at = posted_at.astimezone(UTC).replace(tzinfo=None)
    return posted_at >= _now() - timedelta(days=max_age_days)


def _still_fresh(posting: JobPosting, max_age_days: int) -> bool:
    """_is_fresh() for a stored posting. Falls back to found_at because many
    sources give no posted_at, and without it a posting would stay "fresh"
    forever, however long an outage kept it waiting."""
    return _is_fresh(posting.posted_at or posting.found_at, max_age_days)


def _dedupe_and_store(
    session: Session, raw_postings: list[RawPosting], max_posting_age_days: int
) -> int:
    """Insert postings through the dedup pipeline, skipping anything older than
    max_posting_age_days. Returns count of new rows."""
    new_count = 0
    for rp in raw_postings:
        if not _is_fresh(rp.posted_at, max_posting_age_days):
            continue
        data = {
            "source": rp.source,
            "external_id": rp.external_id,
            "title": rp.title,
            "company": rp.company,
            "location": rp.location,
            "is_remote": rp.is_remote,
            "url": rp.url,
            "apply_url": rp.apply_url,
            "description": rp.description,
            "posted_at": rp.posted_at,
        }
        _, was_new, _reason = upsert_deduped(session, data)
        if was_new:
            new_count += 1
    return new_count


# ── Scoring ───────────────────────────────────────────────────────────────────


def _to_scoring_posting(posting: JobPosting) -> RawPosting:
    """Adapt a stored JobPosting into a RawPosting for the matcher, using the
    DB row id (not the source's external_id) as the round-trip key — two
    different sources can otherwise share the same external_id."""
    return RawPosting(
        source=posting.source,
        external_id=str(posting.id),
        title=posting.title,
        company=posting.company,
        location=posting.location,
        is_remote=posting.is_remote,
        url=posting.url,
        apply_url=posting.apply_url,
        description=posting.description,
        posted_at=posting.posted_at,
    )


def _score_and_record(
    session: Session,
    postings: list[JobPosting],
    profile: dict[str, Any],
    preferences: dict[str, Any],
    profile_hash: str,
    settings: Settings,
) -> ScoringResult:
    if not postings:
        return ScoringResult(scored=[], input_tokens=0, output_tokens=0, estimated_cost_usd=0.0)

    raw = [_to_scoring_posting(p) for p in postings]
    result = score_postings(raw, profile, preferences, settings=settings)

    by_id = {p.id: p for p in postings}
    for scored in result.scored:
        posting = by_id.get(int(scored.external_id))
        if posting is not None and posting.id is not None:
            record_score(
                session,
                posting.id,
                scored.score,
                scored.reasoning,
                profile_hash,
                missing_qualifications=scored.missing_qualifications,
            )
    return result


# ── Notification ──────────────────────────────────────────────────────────────


def _to_match_info(posting: JobPosting) -> MatchInfo:
    return MatchInfo(
        company=posting.company,
        title=posting.title,
        location=posting.location,
        score=posting.match_score or 0,
        reasoning=posting.match_reasoning or "",
        missing_qualifications=list(posting.missing_qualifications or []),
        apply_url=posting.apply_url or posting.url,
        posting_id=posting.id,
        source=posting.source,
    )


def _notify_and_mark(session: Session, matches: list[JobPosting], settings: Settings) -> int:
    """Send alerts for every scored posting (individual or burst mode) and
    mark each as notified. Returns the number of postings successfully
    notified. There is no minimum-score gate — every posting passed in gets
    an alert."""
    if not matches:
        return 0

    bot_token = settings.telegram_bot_token or ""
    chat_id = settings.telegram_chat_id or ""
    match_infos = [_to_match_info(m) for m in matches]
    results = notify_matches(
        bot_token,
        match_infos,
        chat_id=chat_id,
        burst_threshold=settings.burst_threshold,
    )

    alerts_sent = 0
    is_burst = len(matches) >= settings.burst_threshold
    if is_burst:
        parts = format_burst_messages(match_infos)
        for part, result in zip(parts, results, strict=True):
            if result.success:
                for info in part.matches:
                    mark_notified(
                        session, info.posting_id, chat_id, part.body, str(result.message_id)
                    )
                    alerts_sent += 1
    else:
        for posting, info, result in zip(matches, match_infos, results, strict=True):
            if result.success:
                body = format_individual_message(
                    info.company,
                    info.title,
                    info.location,
                    info.score,
                    info.reasoning,
                    info.missing_qualifications,
                    info.apply_url,
                    info.source,
                )
                mark_notified(session, posting.id, chat_id, body, str(result.message_id))
                alerts_sent += 1

    return alerts_sent


# ── Inbound Telegram commands ────────────────────────────────────────────────


def _chat_id_matches(update_chat_id: Any, configured_chat_id: str) -> bool:
    return str(update_chat_id) == str(configured_chat_id)


def process_telegram_updates(session: Session, settings: Settings) -> None:
    """
    Short-poll Telegram for anything sent since the last cursor: "Mark Applied"
    button taps (callback_query, data="applied:<posting_id>") and /pause,
    /resume commands. Ignores updates from any chat other than the configured
    one — the bot token is private, but this keeps a stray discovery of the
    bot username from being able to toggle notifications.
    """
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not bot_token or not chat_id:
        return

    state = get_bot_state(session)
    offset = state.last_update_id + 1 if state.last_update_id is not None else None
    updates = get_updates(bot_token, offset=offset)

    # Taken from this batch alone, not compared with the stored cursor, which
    # may belong to a previous bot token (see update_last_telegram_update_id).
    max_update_id: int | None = None
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None and (max_update_id is None or update_id > max_update_id):
            max_update_id = update_id

        callback = update.get("callback_query")
        if callback is not None:
            cb_chat = callback.get("message", {}).get("chat", {})
            if not _chat_id_matches(cb_chat.get("id"), chat_id):
                continue
            data = callback.get("data", "")
            if data.startswith("applied:"):
                try:
                    posting_id = int(data.split(":", 1)[1])
                    mark_applied(session, posting_id)
                    notif = get_latest_notification_for_posting(session, posting_id)
                    if notif is not None and notif.telegram_message_id:
                        edit_message_mark_applied(
                            bot_token, chat_id, notif.telegram_message_id, notif.message
                        )
                    answer_callback_query(bot_token, callback["id"], text="Marked applied ✅")
                except (ValueError, LookupError) as exc:
                    _log.warning("Failed to process applied callback %r: %s", data, exc)
                    answer_callback_query(
                        bot_token, callback["id"], text="Couldn't mark that — try again"
                    )
            continue

        message = update.get("message")
        if message is None:
            continue
        if not _chat_id_matches(message.get("chat", {}).get("id"), chat_id):
            continue
        text = (message.get("text") or "").strip().lower()
        if text == "/pause":
            set_notifications_paused(session, True)
            send_message_with_retry(
                bot_token, chat_id, "Notifications paused. Send /resume to turn them back on."
            )
        elif text == "/resume":
            set_notifications_paused(session, False)
            send_message_with_retry(bot_token, chat_id, "Notifications resumed.")

    if max_update_id is not None:
        update_last_telegram_update_id(session, max_update_id)


# ── Run cycle ─────────────────────────────────────────────────────────────────


def run_cycle(session: Session, settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """Full pipeline: fetch → dedupe → score → notify → log. Returns the
    run_log data that was recorded."""
    started_at = _now()

    company_names = _load_company_names(settings)
    if company_names:
        seed_result = seed_companies(session, company_names, settings.search_api_key)
        if seed_result.newly_attempted:
            _log.info(
                "Company seeding: %d newly attempted, %d already known",
                seed_result.newly_attempted,
                seed_result.already_known,
            )

    config = _load_yaml_config()
    preferences = config.get("preferences", {})
    matching = config.get("matching", {})
    max_posting_age_days = matching.get("max_posting_age_days", 7)
    min_relevance_score = matching.get("min_relevance_score", 15)

    raw_postings, sources_polled, errors = _fetch_all_postings(session)
    postings_found = len(raw_postings)
    postings_new = _dedupe_and_store(session, raw_postings, max_posting_age_days)

    profile = settings.load_profile()
    profile_hash = hash_profile(profile)

    last_run = get_last_run_log(session)
    profile_changed = last_run is not None and last_run.profile_hash != profile_hash
    to_score = get_unscored_postings(session)
    if profile_changed:
        merged = {p.id: p for p in [*to_score, *get_unnotified_postings(session)]}
        to_score = list(merged.values())
    # Re-checked here, not just on insert: postings wait in the DB through an
    # outage, and scoring a stale backlog spends credits on dead listings.
    fresh_to_score = [p for p in to_score if _still_fresh(p, max_posting_age_days)]
    if len(fresh_to_score) < len(to_score):
        _log.info(
            "Skipping %d posting(s) older than %d days",
            len(to_score) - len(fresh_to_score),
            max_posting_age_days,
        )
    to_score = fresh_to_score

    scoring_result = _score_and_record(
        session, to_score, profile, preferences, profile_hash, settings
    )

    matches = [
        p
        for p in get_unnotified_scored_postings(session, min_relevance_score)
        if _still_fresh(p, max_posting_age_days)
    ]
    alerts_attempted = False
    if dry_run:
        _log.info("[dry-run] Would send alerts for %d posting(s)", len(matches))
        alerts_sent = 0
    elif get_bot_state(session).notifications_paused:
        _log.info(
            "Notifications paused — holding %d posting(s) for next cycle",
            len(matches),
        )
        alerts_sent = 0
    else:
        alerts_sent = _notify_and_mark(session, matches, settings)
        alerts_attempted = bool(matches)

    run_data = {
        "started_at": started_at,
        "finished_at": _now(),
        "sources_polled": sources_polled,
        "postings_found": postings_found,
        "postings_new": postings_new,
        "postings_scored": len(scoring_result.scored),
        "alerts_sent": alerts_sent,
        "errors": errors,
        "profile_hash": profile_hash,
        "estimated_cost_usd": scoring_result.estimated_cost_usd,
    }
    log_run(session, run_data)
    # Not RunLog columns, so added only after log_run. Each flags a total
    # failure of that stage, not a partial one: one unparseable batch or
    # undeliverable message is logged but isn't an outage worth an email.
    return {
        **run_data,
        "all_sources_failed": bool(errors) and not sources_polled,
        "all_scoring_failed": (
            scoring_result.batches_attempted > 0
            and scoring_result.batches_failed == scoring_result.batches_attempted
        ),
        "all_alerts_failed": alerts_attempted and alerts_sent == 0,
    }


def _cycle_is_healthy(summary: dict[str, Any], settings: Settings) -> bool:
    """Whether a finished cycle may report in to the dead-man's switch. run_cycle
    swallows source, scoring, and send failures so one bad dependency can't stop
    the rest, so "it returned" says nothing. The token is checked directly
    because a quiet cycle sends no alerts and wouldn't otherwise notice a dead one."""
    problems = [
        key
        for key in ("all_sources_failed", "all_scoring_failed", "all_alerts_failed")
        if summary.get(key)
    ]
    if settings.telegram_bot_token and bot_token_is_rejected(settings.telegram_bot_token):
        problems.append("telegram_token_rejected")
    if problems:
        _log.warning("Cycle unhealthy, skipping health ping: %s", ", ".join(problems))
    return not problems


def _ping_healthcheck(url: str) -> None:
    """Never raises: a monitoring hiccup must not fail a cycle that succeeded."""
    try:
        httpx.get(url, timeout=10.0).raise_for_status()
    except Exception as exc:
        # str(exc) can contain the URL, which is a credential of sorts.
        _log.warning("Health ping failed: %s", type(exc).__name__)


def retry_unresolved_companies(session: Session, settings: Settings) -> None:
    """Re-attempt discovery for unresolved companies, hourly, silently.

    Replaces the old digest job — previously this also sent a periodic
    Telegram summary (postings found, alerts sent, top match, unresolved
    company names), but that message mostly repeated information already
    visible in the individual alerts themselves and was removed per explicit
    request. Unresolved companies are still visible via `python -m src.db
    stats`; this just keeps retrying them in the background without pushing
    a message about it.

    Priority companies (companies.yaml's optional `priority_companies:`
    list, T-312) get retry_unresolved()'s tighter cooldown — the point is
    not missing new postings from companies the user actually cares about,
    even under SerpAPI's tight free-tier quota (confirmed live 2026-08-20:
    250 searches/month, easily exhausted by treating every company
    identically). Everything else gets the looser default cooldown so the
    budget lasts the month instead of being burned on the very first
    eligible batch.

    Logged at WARNING rather than INFO when a non-trivial batch resolves
    nothing at all — that specific shape (N attempted, 0 resolved,
    repeated hour after hour) is the actual, previously-invisible signature
    of a SerpAPI outage or exhausted quota (found live 2026-08-20:
    `_serpapi()` itself logs nothing on failure, so this summary line was
    the only place such an outage could ever surface). A single 0-resolved
    hour is normal noise; this exists so a *sustained* one is at least
    visible to whoever is watching logs, not just theoretically present in
    them (NFR-03 already documents that nothing pushes this to the user).
    """
    priority_names = set(_load_priority_company_names(settings))
    retry_result = retry_unresolved(session, settings.search_api_key, priority_names=priority_names)
    log = _log.warning if retry_result.attempted > 0 and retry_result.resolved == 0 else _log.info
    log(
        "Unresolved-company retry: %d attempted, %d resolved",
        retry_result.attempted,
        retry_result.resolved,
    )


def run_adzuna_poll(session: Session, settings: Settings) -> None:
    """
    Poll Adzuna on its own weekly job, deliberately separate from the main
    cycle — its free tier is 250 calls/month. Runs two queries ("internship"
    and "co-op") every week rather than one, since "internship" alone misses
    co-op-only listings that never use the word "internship" — even doubled,
    that's ~8/month, far under the 250/month quota. New postings are stored
    unscored; the next main cycle picks them up for scoring like any other
    source.
    """
    if not (settings.adzuna_app_id and settings.adzuna_app_key):
        _log.info("Adzuna poll skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not configured")
        return

    max_posting_age_days = _load_yaml_config().get("matching", {}).get("max_posting_age_days", 7)
    postings = adzuna.fetch(settings.adzuna_app_id, settings.adzuna_app_key) + adzuna.fetch(
        settings.adzuna_app_id, settings.adzuna_app_key, what="co-op"
    )
    new_count = _dedupe_and_store(session, postings, max_posting_age_days)
    _log.info("Adzuna poll: %d found, %d new", len(postings), new_count)


def run_jsearch_poll(session: Session, settings: Settings) -> None:
    """
    Poll JSearch (RapidAPI) on its own 4-hourly job, deliberately separate
    from the main cycle — like Adzuna, it's metered per month, and a single
    broad query every 4 hours is ~180 requests/month, comfortably under a
    200/month free-tier quota (every 2 hours would be ~360/month — over it).
    Adjust the interval to match your actual RapidAPI plan's quota if it
    differs. This is the only source that reaches LinkedIn postings (see the
    LinkedIn constraint in docs/requirements.md).

    Alternates between the intern and co-op query each poll (by UTC hour)
    rather than querying both every time — querying "intern" alone misses
    co-op-only listings that never use the word "internship", but querying
    both every poll would double the request volume past the 200/month quota.
    Each variant still gets polled roughly every 8 hours.

    New postings are stored unscored; the next main cycle picks them up for
    scoring like any other source.
    """
    if not settings.jsearch_api_key:
        _log.info("JSearch poll skipped: JSEARCH_API_KEY not configured")
        return

    max_posting_age_days = _load_yaml_config().get("matching", {}).get("max_posting_age_days", 7)
    is_intern_slot = (_now().hour // 4) % 2 == 0
    query = jsearch.INTERN_QUERY if is_intern_slot else jsearch.COOP_QUERY
    postings = jsearch.fetch(settings.jsearch_api_key, query=query)
    new_count = _dedupe_and_store(session, postings, max_posting_age_days)
    _log.info(
        "JSearch poll (%s): %d found, %d new",
        "intern" if is_intern_slot else "co-op",
        len(postings),
        new_count,
    )


def run_llm_scrape_poll(session: Session, settings: Settings) -> None:
    """
    Poll every ats_type="llm_scrape" (Tier B, design.md §3.5) company on its
    own 6-hourly job, deliberately separate from the main cycle — unlike
    every other Tier 1 source (Greenhouse/Lever/Workday, all a free JSON
    GET), this one costs a real Claude call per company per poll, so it
    gets the same slower-cadence treatment Adzuna/JSearch already get for
    their own (quota, not $) cost reasons (FR-71). New postings are stored
    unscored; the next main cycle picks them up for scoring like any other
    source.

    Unlike Adzuna/JSearch, this poll's own cost is real and worth tracking
    on its own — not just folded into the next main cycle's scoring cost —
    so it writes its own RunLog row (NFR-22) rather than only logging a
    summary line. get_stats()'s cumulative estimated_cost_usd already sums
    across every RunLog row, so this needs no new tracking infrastructure,
    just reusing what NFR-10 already built for the main cycle.

    The row's profile_hash is stamped with the *current* profile hash, not
    left None — run_cycle()'s profile-change detection (§8's profile_hash
    comparison) reads whichever RunLog row is most recent, and this job's
    row can easily be that row given it runs on its own schedule. Leaving
    profile_hash None here would make every main cycle immediately after an
    llm_scrape poll spuriously detect a "profile change" and re-score every
    unnotified posting for no reason — stamping the real current hash keeps
    that comparison correct regardless of which job ran last.
    """
    started_at = _now()
    max_posting_age_days = _load_yaml_config().get("matching", {}).get("max_posting_age_days", 7)
    result = llm_scrape.fetch_for_resolved_companies(session, settings)
    new_count = _dedupe_and_store(session, result.postings, max_posting_age_days)
    _log.info(
        "llm_scrape poll: %d companies polled, %d found, %d new, est. cost $%.4f",
        result.companies_polled,
        len(result.postings),
        new_count,
        result.estimated_cost_usd,
    )
    log_run(
        session,
        {
            "started_at": started_at,
            "finished_at": _now(),
            "sources_polled": ["llm_scrape"],
            "postings_found": len(result.postings),
            "postings_new": new_count,
            "postings_scored": 0,
            "alerts_sent": 0,
            "errors": [],
            "profile_hash": hash_profile(settings.load_profile()),
            "estimated_cost_usd": result.estimated_cost_usd,
        },
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="InternshipAgent — monitors job boards and sends SMS alerts.",
    )
    parser.add_argument(
        "--rebuild-profile",
        action="store_true",
        help=(
            "Extract structured profile from all PDFs in /resumes/, write "
            "profile.cache.json, and print the railway variables set command."
        ),
    )
    parser.add_argument(
        "--sync-companies",
        action="store_true",
        help=(
            "Base64-encode config/companies.yaml and print the railway variables "
            "set command for COMPANIES_CONFIG. Run this after editing companies.yaml "
            "— the file is gitignored, so it never reaches the deployed container "
            "any other way."
        ),
    )
    parser.add_argument("--run-once", action="store_true", help="Run a single cycle, then exit.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Run the full pipeline but do not send SMS."
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-score all unnotified postings against the current profile, then exit.",
    )
    return parser.parse_args()


class _SecretRedactingFilter(logging.Filter):
    """httpx logs every request at INFO level with the full URL, including
    query-string credentials (SerpAPI's api_key=...) and path-embedded ones
    (Telegram's /bot<token>:.../...) — unlike the app's own chat-ID redaction
    in notifier.py, which only covers messages it logs itself, this is the
    third-party library logging the raw request URL verbatim on every single
    call. Redacts by fully rendering the message (record.getMessage() applies
    %-style args, which is where httpx puts the URL) and clearing args so the
    redacted text isn't re-interpolated."""

    _PATTERNS = [
        (re.compile(r"(api_key=)[^&\s\"]+"), r"\1***"),
        (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), r"\1***"),
        (re.compile(r"(hc-ping\.com/)[A-Za-z0-9_/-]+"), r"\1***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern, repl in self._PATTERNS:
            msg = pattern.sub(repl, msg)
        record.msg = msg
        record.args = ()
        return True


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            '{"time": "%(asctime)s", "level": "%(levelname)s", '
            '"logger": "%(name)s", "message": "%(message)s"}'
        ),
    )
    logging.getLogger("httpx").addFilter(_SecretRedactingFilter())


def main() -> None:
    _configure_logging()
    args = _parse_args()

    if args.rebuild_profile:
        rebuild_profile(_RESUMES_DIR)
        sys.exit(0)

    if args.sync_companies:
        if not _COMPANIES_YAML.exists():
            sys.exit(f"{_COMPANIES_YAML} not found — nothing to sync.")
        encoded = base64.b64encode(_COMPANIES_YAML.read_bytes()).decode()
        print(f'railway variables set COMPANIES_CONFIG="{encoded}"')
        sys.exit(0)

    settings = get_settings()
    engine = init_db(settings.database_url)

    if args.rescore:
        with session_scope(engine) as session:
            profile = settings.load_profile()
            config = _load_yaml_config()
            postings = get_unnotified_postings(session)
            result = _score_and_record(
                session,
                postings,
                profile,
                config.get("preferences", {}),
                hash_profile(profile),
                settings,
            )
            _log.info("Rescored %d posting(s)", len(result.scored))
        sys.exit(0)

    if args.run_once:
        with session_scope(engine) as session:
            summary = run_cycle(session, settings, dry_run=args.dry_run)
        _log.info("Run complete: %s", json.dumps(summary, default=str))
        sys.exit(0)

    scheduler = BlockingScheduler()

    def _scheduled_cycle() -> None:
        with session_scope(engine) as session:
            summary = run_cycle(session, settings, dry_run=args.dry_run)
        # After the session closes, so a failed commit means no ping.
        if settings.healthcheck_url and not args.dry_run and _cycle_is_healthy(summary, settings):
            _ping_healthcheck(settings.healthcheck_url)

    def _scheduled_company_retry() -> None:
        with session_scope(engine) as session:
            retry_unresolved_companies(session, settings)

    def _scheduled_adzuna() -> None:
        with session_scope(engine) as session:
            run_adzuna_poll(session, settings)

    def _scheduled_jsearch() -> None:
        with session_scope(engine) as session:
            run_jsearch_poll(session, settings)

    def _scheduled_llm_scrape() -> None:
        with session_scope(engine) as session:
            run_llm_scrape_poll(session, settings)

    def _scheduled_telegram_poll() -> None:
        with session_scope(engine) as session:
            process_telegram_updates(session, settings)

    scheduler.add_job(
        _scheduled_cycle,
        IntervalTrigger(minutes=settings.run_interval_minutes),
        max_instances=1,
    )
    scheduler.add_job(_scheduled_company_retry, IntervalTrigger(hours=1), max_instances=1)
    scheduler.add_job(_scheduled_adzuna, IntervalTrigger(weeks=1), max_instances=1)
    scheduler.add_job(_scheduled_jsearch, IntervalTrigger(hours=4), max_instances=1)
    scheduler.add_job(_scheduled_llm_scrape, IntervalTrigger(hours=6), max_instances=1)
    # Polls Telegram for /pause, /resume, and "Mark Applied" taps — decoupled
    # from the hourly cycle and deliberately fast (a few seconds, not minutes)
    # so a button tap feels close to instant rather than sitting unconfirmed
    # for however long the next poll takes to roll around. getUpdates is a
    # cheap short-poll (timeout=0) with no meaningful rate-limit risk at this
    # interval.
    scheduler.add_job(_scheduled_telegram_poll, IntervalTrigger(seconds=5), max_instances=1)

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        _log.info("Received signal %d — shutting down after current cycle", signum)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _log.info("Starting scheduler: every %d minutes", settings.run_interval_minutes)
    scheduler.start()


if __name__ == "__main__":
    main()
