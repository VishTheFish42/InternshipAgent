"""InternshipAgent entry point: CLI, run_cycle, and scheduler orchestration."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.company_discoverer import retry_unresolved, seed_companies
from src.config import Settings, get_settings
from src.db import (
    JobPosting,
    Notification,
    get_last_run_log,
    get_unnotified_above_threshold,
    get_unnotified_partial_matches,
    get_unnotified_postings,
    get_unresolved_companies,
    get_unscored_postings,
    init_db,
    log_run,
    mark_notified,
    mark_partial_notified,
    record_score,
    session_scope,
)
from src.deduplicator import upsert_deduped
from src.matcher import ScoringResult, hash_profile, score_postings
from src.notifier import (
    MatchInfo,
    PartialMatchInfo,
    format_burst_message,
    format_digest,
    format_individual_message,
    format_partial_match_message,
    notify_matches,
    send_message_with_retry,
)
from src.resume_extractor import rebuild_profile
from src.scrapers import adzuna, greenhouse, hn, indeed_rss, lever, remoteok
from src.scrapers.base import RawPosting

_RESUMES_DIR = Path("resumes")
_PROFILE_YAML = Path("profile.yaml")
_COMPANIES_YAML = Path("config/companies.yaml")
_log = logging.getLogger(__name__)

# Every registered Tier 1/2 source polled on the *main* cycle. Adzuna is
# deliberately excluded here and polled on its own weekly job instead
# (run_adzuna_poll, below) — its free tier is 250 calls/month, which the main
# cycle's hourly interval would exhaust in under two weeks. YC and Dice are
# not yet wired into either schedule.
_SOURCES: list[tuple[str, Callable[[Session], list[RawPosting]]]] = [
    ("greenhouse", greenhouse.fetch_for_resolved_companies),
    ("lever", lever.fetch_for_resolved_companies),
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


def _load_company_names(path: Path = _COMPANIES_YAML) -> list[str]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("companies", []))


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


def _dedupe_and_store(session: Session, raw_postings: list[RawPosting]) -> int:
    """Insert postings through the dedup pipeline. Returns count of new rows."""
    new_count = 0
    for rp in raw_postings:
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
        apply_url=posting.apply_url or posting.url,
    )


def _notify_and_mark(session: Session, matches: list[JobPosting], settings: Settings) -> int:
    """Send alerts for matches (individual or burst mode) and mark each as
    notified. Returns the number of postings successfully notified."""
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
        result = results[0]
        if result.success:
            body = format_burst_message(match_infos)
            for posting in matches:
                mark_notified(session, posting.id, chat_id, body, str(result.message_id))
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
                    info.apply_url,
                )
                mark_notified(session, posting.id, chat_id, body, str(result.message_id))
                alerts_sent += 1

    return alerts_sent


def _to_partial_match_info(posting: JobPosting) -> PartialMatchInfo:
    return PartialMatchInfo(
        company=posting.company,
        title=posting.title,
        score=posting.match_score or 0,
        missing_qualifications=list(posting.missing_qualifications or []),
        apply_url=posting.apply_url or posting.url,
    )


def _notify_partial_matches_and_mark(
    session: Session, partial_matches: list[JobPosting], settings: Settings
) -> int:
    """Send one batched message covering all of this cycle's partial matches
    and mark each as partial_notified. Returns the count successfully sent."""
    if not partial_matches:
        return 0

    bot_token = settings.telegram_bot_token or ""
    chat_id = settings.telegram_chat_id or ""
    infos = [_to_partial_match_info(p) for p in partial_matches]
    body = format_partial_match_message(infos)
    result = send_message_with_retry(bot_token, chat_id, body)

    if not result.success:
        return 0

    for posting in partial_matches:
        mark_partial_notified(session, posting.id, chat_id, body, str(result.message_id))
    return len(partial_matches)


# ── Run cycle ─────────────────────────────────────────────────────────────────


def run_cycle(session: Session, settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """Full pipeline: fetch → dedupe → score → notify → log. Returns the
    run_log data that was recorded."""
    started_at = _now()

    company_names = _load_company_names()
    if company_names:
        seed_result = seed_companies(session, company_names, settings.search_api_key)
        if seed_result.newly_attempted:
            _log.info(
                "Company seeding: %d newly attempted, %d already known",
                seed_result.newly_attempted,
                seed_result.already_known,
            )

    raw_postings, sources_polled, errors = _fetch_all_postings(session)
    postings_found = len(raw_postings)
    postings_new = _dedupe_and_store(session, raw_postings)

    profile = settings.load_profile()
    config = _load_yaml_config()
    preferences = config.get("preferences", {})
    matching = config.get("matching", {})
    min_score = matching.get("min_score", 70)
    partial_match_min_score = matching.get("partial_match_min_score", 50)
    max_missing_qualifications = matching.get("max_missing_qualifications", 5)
    profile_hash = hash_profile(profile)

    last_run = get_last_run_log(session)
    profile_changed = last_run is not None and last_run.profile_hash != profile_hash
    to_score = get_unscored_postings(session)
    if profile_changed:
        merged = {p.id: p for p in [*to_score, *get_unnotified_postings(session)]}
        to_score = list(merged.values())

    scoring_result = _score_and_record(
        session, to_score, profile, preferences, profile_hash, settings
    )

    matches = get_unnotified_above_threshold(session, min_score)
    partial_matches = get_unnotified_partial_matches(
        session, partial_match_min_score, min_score, max_missing_qualifications
    )
    if dry_run:
        _log.info("[dry-run] Would send alerts for %d match(es)", len(matches))
        _log.info("[dry-run] Would send %d partial match(es)", len(partial_matches))
        alerts_sent = 0
    else:
        alerts_sent = _notify_and_mark(session, matches, settings)
        partial_alerts_sent = _notify_partial_matches_and_mark(session, partial_matches, settings)
        if partial_alerts_sent:
            _log.info("Sent partial-match digest covering %d posting(s)", partial_alerts_sent)

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
    return run_data


def _get_top_match_this_week(session: Session) -> JobPosting | None:
    cutoff = _now() - timedelta(days=7)
    return session.execute(
        select(JobPosting)
        .where(JobPosting.found_at >= cutoff, JobPosting.match_score.is_not(None))
        .order_by(JobPosting.match_score.desc())
        .limit(1)
    ).scalar_one_or_none()


def _get_period_stats(session: Session, since: datetime) -> tuple[int, int]:
    """Postings found and alerts sent since `since` — used for the digest's
    per-period counts, distinct from get_stats()'s all-time totals."""
    postings_found = session.execute(
        select(func.count()).select_from(JobPosting).where(JobPosting.found_at >= since)
    ).scalar_one()
    alerts_sent = session.execute(
        select(func.count()).select_from(Notification).where(Notification.sent_at >= since)
    ).scalar_one()
    return postings_found, alerts_sent


def run_digest(session: Session, settings: Settings) -> None:
    """Re-attempt unresolved companies and send the periodic summary message."""
    config = _load_yaml_config()
    if not config.get("matching", {}).get("digest_enabled", True):
        return

    retry_result = retry_unresolved(session, settings.search_api_key, min_age_hours=1)
    _log.info(
        "Digest re-attempt: %d attempted, %d resolved",
        retry_result.attempted,
        retry_result.resolved,
    )

    postings_found, alerts_sent = _get_period_stats(session, _now() - timedelta(hours=1))
    unresolved = [c.name_raw for c in get_unresolved_companies(session)]
    top_posting = _get_top_match_this_week(session)
    top_match = _to_match_info(top_posting) if top_posting else None

    body = format_digest(
        _now().strftime("%a %b %-d %I:%M%p"), postings_found, alerts_sent, top_match, unresolved
    )

    if settings.telegram_bot_token and settings.telegram_chat_id:
        send_message_with_retry(settings.telegram_bot_token, settings.telegram_chat_id, body)


def run_adzuna_poll(session: Session, settings: Settings) -> None:
    """
    Poll Adzuna on its own weekly job, deliberately separate from the main
    cycle — its free tier is 250 calls/month, and this makes one call per
    week (~4/month), leaving headroom. New postings are stored unscored;
    the next main cycle picks them up for scoring like any other source.
    """
    if not (settings.adzuna_app_id and settings.adzuna_app_key):
        _log.info("Adzuna poll skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not configured")
        return

    postings = adzuna.fetch(settings.adzuna_app_id, settings.adzuna_app_key)
    new_count = _dedupe_and_store(session, postings)
    _log.info("Adzuna poll: %d found, %d new", len(postings), new_count)


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


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            '{"time": "%(asctime)s", "level": "%(levelname)s", '
            '"logger": "%(name)s", "message": "%(message)s"}'
        ),
    )


def main() -> None:
    _configure_logging()
    args = _parse_args()

    if args.rebuild_profile:
        rebuild_profile(_RESUMES_DIR)
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
            run_cycle(session, settings, dry_run=args.dry_run)

    def _scheduled_digest() -> None:
        with session_scope(engine) as session:
            run_digest(session, settings)

    def _scheduled_adzuna() -> None:
        with session_scope(engine) as session:
            run_adzuna_poll(session, settings)

    scheduler.add_job(
        _scheduled_cycle,
        IntervalTrigger(minutes=settings.run_interval_minutes),
        max_instances=1,
    )
    scheduler.add_job(_scheduled_digest, IntervalTrigger(hours=1), max_instances=1)
    scheduler.add_job(_scheduled_adzuna, IntervalTrigger(weeks=1), max_instances=1)

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        _log.info("Received signal %d — shutting down after current cycle", signum)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _log.info("Starting scheduler: every %d minutes", settings.run_interval_minutes)
    scheduler.start()


if __name__ == "__main__":
    main()
