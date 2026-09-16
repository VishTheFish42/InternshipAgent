from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from src.db import CompanyLookup

_log = logging.getLogger(__name__)

_ATS_MAP_PATH = Path(__file__).parent / "data" / "company_ats_map.json"

# ats_type values with a real, working scraper behind them (src/main.py's
# _SOURCES for greenhouse/lever/workday; run_llm_scrape_poll for llm_scrape).
_LIVE_ATS_TYPES = {"greenhouse", "lever", "workday", "llm_scrape"}

# ats_type values discover() no longer writes (T-309) but that can still be
# read from old company_lookup rows or un-migrated company_ats_map.json
# entries — 'taleo' found live by tests/test_coverage_guarantee.py's very
# first run (T-910), not assumed; there may be others not yet discovered.
_LEGACY_DEAD_END_ATS_TYPES = {"custom", "taleo"}


def _is_usable(ats_type: str | None, slug: str | None, url: str | None) -> bool:
    """True if (ats_type, slug, url) actually names a scraper that will run
    — not just a live ats_type in the abstract. A 'workday' entry with no
    slug, or an 'llm_scrape' entry with no url, is exactly as dead as a
    'custom' entry: 'resolved' in the DB, but nothing will ever poll it
    (design.md §3.5, T-310)."""
    if ats_type not in _LIVE_ATS_TYPES:
        return False
    if ats_type == "llm_scrape":
        return bool(url)
    return bool(slug)


@dataclass
class CompanyRecord:
    name_raw: str
    name_normalized: str
    ats_type: str | None  # 'greenhouse' | 'lever' | 'workday' | 'llm_scrape' | 'custom' | None
    # 'custom' is a legacy value: still readable from old company_lookup rows
    # and from company_ats_map.json entries not yet migrated (docs/tasks.md
    # T-310), but no longer written by discover() itself (T-309) — it had no
    # scraper behind it. New resolutions use 'llm_scrape' instead, which does.
    slug: str | None
    url: str | None
    status: str  # 'resolved' | 'unresolved'
    source: str | None  # 'bundled_table' | 'guessed_verified' | 'web_search' | 'manual'


# ── Helpers ───────────────────────────────────────────────────────────────────


def _normalize(name: str) -> str:
    """Lowercase and collapse whitespace — matches keys in company_ats_map.json."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@lru_cache(maxsize=1)
def _load_ats_map() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_ATS_MAP_PATH.read_text(encoding="utf-8"))
    return data


# ── Web search steps ──────────────────────────────────────────────────────────


class _RateLimiter:
    """Enforces a minimum interval between successive calls. Centralized
    here rather than threaded through seed_companies()'s and
    retry_unresolved()'s loops separately — one call to .wait() per
    _serpapi() invocation covers every call site at once, including
    discover()'s own internal Greenhouse/Lever/Workday/careers cascade
    (up to 4 back-to-back SerpAPI calls for a single company that misses
    every step). Found needed live, not hypothetical (docs/tasks.md,
    2026-08-15): the original 349-company seed fired queries with zero
    throttling and 429'd SerpAPI's own rate limit — a different failure
    mode from, and possibly a contributor to, the account's separate
    monthly-quota exhaustion found later (2026-08-20)."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._last_call_at = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call_at = time.monotonic()


# 1 request/second is a conservative default, not SerpAPI's documented
# limit for any specific plan — tune to your actual plan tier if it
# differs, same "adjust to your plan" caveat as jsearch.py's poll cadence.
_serpapi_rate_limiter = _RateLimiter(min_interval_seconds=1.0)


def _serpapi(query: str, api_key: str, num: int = 5) -> list[dict[str, Any]]:
    """Return SerpAPI organic results, or [] on any failure — logging a
    warning either way (previously silent, found live 2026-08-20: an
    exhausted SerpAPI quota produced zero application-level signal
    anywhere, only discoverable by querying SerpAPI directly). This is the
    only place a SerpAPI failure is diagnosable from logs at all, since
    every _search_* helper in this module funnels through here."""
    _serpapi_rate_limiter.wait()
    try:
        resp = httpx.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": query, "api_key": api_key, "num": num},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("organic_results", [])  # type: ignore[no-any-return]
    except httpx.HTTPStatusError as exc:
        _log.warning(
            "SerpAPI request failed (HTTP %d): %s",
            exc.response.status_code,
            exc.response.text[:200],
        )
        return []
    except Exception as exc:
        _log.warning("SerpAPI request failed: %s", exc)
        return []


# ── Direct guess-and-verify (Greenhouse/Lever) ───────────────────────────────
#
# Both ATS platforms key a company's board by a plain slug derived from its
# name, and both expose that board via a free, unauthenticated public API.
# Guessing the slug and verifying it directly costs zero SerpAPI quota —
# found worth doing after the account's free tier (250 searches/month)
# turned out to be exhausted every month by a large watchlist (docs/tasks.md,
# 2026-08-20): most companies that resolve at all resolve to Greenhouse or
# Lever, so catching them here before ever reaching search removes most of
# the demand on that quota rather than just spending it more efficiently.

_GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
_LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
_GUESS_TIMEOUT_SECONDS = 5.0
_GUESS_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"

# Shared between every Greenhouse/Lever guess-verification call. These are
# free, unauthenticated public APIs with no documented quota — but guessing
# slugs for a whole not-yet-resolved backlog is a more request-happy pattern
# than the existing per-company job-fetch scrapers (which only ever poll a
# company already confirmed resolved), so still worth throttling rather than
# firing requests as fast as possible.
_guess_rate_limiter = _RateLimiter(min_interval_seconds=0.5)


def _slug_candidates(company_name: str) -> list[str]:
    """Plausible Greenhouse/Lever slug guesses for a company name, most
    likely first. Doesn't need to be exhaustive — a wrong guess just 404s
    against the real API and falls through to search exactly as if this
    step didn't exist, so this only needs to catch the common cases cheaply."""
    words = re.findall(r"[a-z0-9]+", company_name.strip().lower())
    if not words:
        return []
    concatenated = "".join(words)
    hyphenated = "-".join(words)
    return [concatenated] if hyphenated == concatenated else [concatenated, hyphenated]


def _verify_greenhouse_slug(slug: str) -> bool:
    """True if `slug` names a real, currently-live Greenhouse board — a 200
    response with a `jobs` list (even an empty one still means the board
    itself exists; the company just has no open postings right now). Any
    failure (404, timeout, malformed response) means 'not this one', not an
    error — this is a best-effort guess, not a required step."""
    _guess_rate_limiter.wait()
    try:
        resp = httpx.get(
            _GREENHOUSE_JOBS_URL.format(slug=slug),
            headers={"User-Agent": _GUESS_USER_AGENT},
            timeout=_GUESS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return isinstance(resp.json().get("jobs"), list)
    except Exception:
        return False


def _verify_lever_slug(slug: str) -> bool:
    """True if `slug` names a real, currently-live Lever board — a 200
    response with a JSON list (possibly empty)."""
    _guess_rate_limiter.wait()
    try:
        resp = httpx.get(
            _LEVER_POSTINGS_URL.format(slug=slug),
            headers={"User-Agent": _GUESS_USER_AGENT},
            timeout=_GUESS_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return isinstance(resp.json(), list)
    except Exception:
        return False


def _guess_greenhouse_or_lever(company_name: str) -> tuple[str, str] | None:
    """Try every slug candidate against Greenhouse first, then Lever — no
    search, no API key, no quota spent. Greenhouse checked first since it's
    the more common of the two across this project's watchlist history."""
    candidates = _slug_candidates(company_name)
    for slug in candidates:
        if _verify_greenhouse_slug(slug):
            return "greenhouse", slug
    for slug in candidates:
        if _verify_lever_slug(slug):
            return "lever", slug
    return None


# Matches https://{tenant}.{instance}.myworkdayjobs.com/{site} and the same
# URL with a job-listing path or a locale prefix (e.g. /en-US/) appended —
# both real-world shapes, live-observed against Salesforce's actual board
# (docs/tasks.md T-470). `instance` (wd1, wd5, wd12, ...) has no derivable
# pattern per tenant, so it must come from the matched URL itself, not be
# guessed.
_WORKDAY_URL_RE = re.compile(
    r"https?://(?P<tenant>[a-zA-Z0-9_-]+)\.(?P<instance>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[a-zA-Z0-9_-]+)"
)


def _search_workday(company: str, api_key: str) -> tuple[str, str, str] | None:
    """Return (tenant, instance, site) from web search, or None."""
    results = _serpapi(f'"{company}" site:myworkdayjobs.com', api_key)
    for r in results:
        m = _WORKDAY_URL_RE.search(r.get("link", ""))
        if m:
            return m.group("tenant"), m.group("instance"), m.group("site")
    return None


def _search_ats_combined(company: str, api_key: str) -> tuple[str, str] | None:
    """Return (ats_type, slug) from a single combined OR query across
    Greenhouse/Lever/Workday's site: filters — one SerpAPI call instead of
    what used to be up to three separate ones (Greenhouse, then Lever, then
    Workday), each spent even after an earlier one already missed. Cuts
    quota usage for any company that reaches this step at all roughly 3x
    (docs/tasks.md, SerpAPI-usage-reduction entry). Slug format matches
    what each individual ATS branch already produced."""
    results = _serpapi(
        f'"{company}" (site:boards.greenhouse.io OR site:jobs.lever.co OR site:myworkdayjobs.com)',
        api_key,
    )
    for r in results:
        link = r.get("link", "")
        m = re.search(r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)", link)
        if m:
            return "greenhouse", m.group(1)
        m = re.search(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", link)
        if m:
            return "lever", m.group(1)
        m = _WORKDAY_URL_RE.search(link)
        if m:
            return "workday", f"{m.group('tenant')}.{m.group('instance')}/{m.group('site')}"
    return None


def _search_careers(company: str, api_key: str) -> str | None:
    """Return a generic careers-page URL from web search, or None."""
    results = _serpapi(f'"{company}" internship careers', api_key)
    for r in results:
        link = r.get("link", "")
        if re.search(r"career|job", link, re.IGNORECASE):
            return str(link)
    return None


# ── DB persistence ────────────────────────────────────────────────────────────


def _upsert_company(session: Session, record: CompanyRecord) -> CompanyLookup:
    """Insert or update a CompanyLookup row from a CompanyRecord."""
    now = _now()
    existing = session.execute(
        select(CompanyLookup).where(CompanyLookup.name_raw == record.name_raw)
    ).scalar_one_or_none()

    if existing is not None:
        existing.name_normalized = record.name_normalized
        existing.ats_type = record.ats_type
        existing.slug = record.slug
        existing.url = record.url
        existing.status = record.status
        existing.source = record.source
        existing.last_attempted = now
        if record.status == "resolved" and existing.resolved_at is None:
            existing.resolved_at = now
        session.flush()
        return existing

    row = CompanyLookup(
        name_raw=record.name_raw,
        name_normalized=record.name_normalized,
        ats_type=record.ats_type,
        slug=record.slug,
        url=record.url,
        status=record.status,
        source=record.source,
        last_attempted=now,
        resolved_at=now if record.status == "resolved" else None,
    )
    session.add(row)
    session.flush()
    return row


# ── Public API ────────────────────────────────────────────────────────────────


def discover(
    company_name: str,
    session: Session,
    search_api_key: str | None = None,
    *,
    force: bool = False,
) -> CompanyRecord:
    """
    Resolve a company name to its ATS endpoint via a 6-step pipeline.

      1. DB cache (skip on force=True — used by weekly re-attempt)
      2. Bundled lookup table (company_ats_map.json) — only trusted if it
         names a live ats_type AND actually carries the identifier that
         type's scraper needs (T-310); otherwise treated as absent
      3. Direct guess-and-verify against Greenhouse's and Lever's own public
         APIs — free, no search API key needed, spends zero quota. Skipped
         when the bundled table already confirms this company is on Workday.
      4. Web search: one combined query across Greenhouse/Lever/Workday's
         site: filters (or just Workday alone, if the bundled table already
         confirmed this company is on Workday and only the slug is missing)
      5. Web search: generic careers page → ats_type="llm_scrape"
      → Mark as 'unresolved' if all steps fail

    Always returns a CompanyRecord; check .status == 'resolved' for success.
    Results are persisted to the DB so repeated calls are free.
    Steps 4–5 are skipped when search_api_key is None; step 3 is not, since
    it needs no key at all.

    Step 4's Workday sub-step (design.md §3.5) only resolves companies
    actually hosted on Workday — verified live (docs/tasks.md T-308) that
    Google, Meta, Microsoft, Apple, Amazon, Netflix, Tesla, and Uber are
    not, and never will be by this step; those fall through to step 4
    (ats_type="llm_scrape", design.md §3.5 Tier B — implemented, T-472)
    since they run fully bespoke in-house career platforms with no
    discoverable JSON API pattern.
    """
    name_normalized = _normalize(company_name)

    # Step 0: DB cache
    if not force:
        cached = session.execute(
            select(CompanyLookup).where(
                CompanyLookup.name_normalized == name_normalized,
                CompanyLookup.status == "resolved",
            )
        ).scalar_one_or_none()
        if cached is not None:
            return CompanyRecord(
                name_raw=cached.name_raw,
                name_normalized=cached.name_normalized or name_normalized,
                ats_type=cached.ats_type,
                slug=cached.slug,
                url=cached.url,
                status=cached.status,
                source=cached.source,
            )

    # Step 1: Bundled ATS map — only trusted as a final answer when it's
    # actually usable (T-310). The bug this fixes: the old unconditional
    # return here meant a bundled entry stuck at a legacy ats_type, or a
    # live type missing its identifier (e.g. "workday" with no slug — the
    # identical dead end under a more convincing label), could never
    # self-heal via force=True — this step ran before, and always beat,
    # live search, so force=True's retry never actually got a chance to
    # search for a better answer. Falling through here instead lets
    # retry_unresolved() (broadened to also target these rows, below)
    # actually fix them over time.
    entry = _load_ats_map().get(name_normalized)
    if entry is not None and _is_usable(entry.get("ats"), entry.get("slug"), entry.get("url")):
        record = CompanyRecord(
            name_raw=company_name,
            name_normalized=name_normalized,
            ats_type=entry.get("ats"),
            slug=entry.get("slug"),
            url=entry.get("url"),
            status="resolved",
            source="bundled_table",
        )
        _upsert_company(session, record)
        return record

    # Step 3: Direct guess-and-verify against Greenhouse/Lever's own public
    # APIs — free, needs no search_api_key, spends zero SerpAPI quota. This
    # is what actually fixes the quota math (docs/tasks.md,
    # SerpAPI-usage-reduction entry): most companies that resolve at all
    # resolve to Greenhouse or Lever, so this step catches nearly all of
    # them before a single search is ever needed. Skipped when the bundled
    # table already confirms this company is on Workday — guessing an ATS
    # already known to be wrong just wastes two real HTTP calls.
    if entry is None or entry.get("ats") != "workday":
        guess = _guess_greenhouse_or_lever(company_name)
        if guess:
            ats_type, slug = guess
            record = CompanyRecord(
                name_raw=company_name,
                name_normalized=name_normalized,
                ats_type=ats_type,
                slug=slug,
                url=None,
                status="resolved",
                source="guessed_verified",
            )
            _upsert_company(session, record)
            return record

    # Steps 4–5 require a search API key
    if search_api_key:
        if entry is not None and entry.get("ats") == "workday":
            # The bundled entry already confirms this company is on
            # Workday, just missing its slug — go straight to re-attempting
            # the Workday search rather than wasting queries re-checking
            # Greenhouse/Lever for a company already known not to be there.
            workday_match = _search_workday(company_name, search_api_key)
            if workday_match:
                tenant, instance, site = workday_match
                record = CompanyRecord(
                    name_raw=company_name,
                    name_normalized=name_normalized,
                    ats_type="workday",
                    slug=f"{tenant}.{instance}/{site}",
                    url=None,
                    status="resolved",
                    source="web_search",
                )
                _upsert_company(session, record)
                return record
        else:
            # Step 4: one combined query across Greenhouse/Lever/Workday's
            # site: filters instead of up to three separate ones — cuts
            # quota usage for any company that reaches this step at all
            # roughly 3x (docs/tasks.md, SerpAPI-usage-reduction entry).
            combined = _search_ats_combined(company_name, search_api_key)
            if combined:
                ats_type, slug = combined
                record = CompanyRecord(
                    name_raw=company_name,
                    name_normalized=name_normalized,
                    ats_type=ats_type,
                    slug=slug,
                    url=None,
                    status="resolved",
                    source="web_search",
                )
                _upsert_company(session, record)
                return record

        # Step 5: Generic careers page — resolves to the Tier B llm_scrape
        # fallback (design.md §3.5, src/scrapers/llm_scrape.py), not the old
        # ats_type="custom", which had no scraper behind it at all (the
        # dead end that caused the missed Nvidia posting, T-309). Reached
        # for genuinely new companies AND for a bundled entry stuck at a
        # legacy ats_type that the branches above couldn't improve on —
        # this is what actually lets a company like Google or Meta pick up
        # real coverage now, instead of the bundled table's stale "custom"
        # tag being the final word forever.
        url = _search_careers(company_name, search_api_key)
        if url:
            record = CompanyRecord(
                name_raw=company_name,
                name_normalized=name_normalized,
                ats_type="llm_scrape",
                slug=None,
                url=url,
                status="resolved",
                source="web_search",
            )
            _upsert_company(session, record)
            return record

    # Step 6: Unresolved
    record = CompanyRecord(
        name_raw=company_name,
        name_normalized=name_normalized,
        ats_type=None,
        slug=None,
        url=None,
        status="unresolved",
        source="web_search" if search_api_key else None,
    )
    _upsert_company(session, record)
    return record


@dataclass
class RetryResult:
    attempted: int
    resolved: int
    still_unresolved: int


def retry_unresolved(
    session: Session,
    search_api_key: str | None = None,
    *,
    priority_names: set[str] | None = None,
    priority_min_age_hours: int = 4,
    default_min_age_hours: int = 168,
    max_attempts_per_call: int = 20,
) -> RetryResult:
    """
    Re-attempt discovery for every unresolved company, plus every company
    marked 'resolved' but not actually usable (T-310) — stuck at a legacy
    ats_type, or a live ats_type missing its identifier (e.g. "workday"
    with no slug). Both are the same kind of stuck; `status == "resolved"`
    just hid the second one. Intended to be called hourly (T-703) — but
    each candidate is only actually re-attempted once past its own cooldown.

    Two cooldowns, not one (T-312): companies in `priority_names` (matched
    via _normalize(), same as everywhere else in this module) get
    `priority_min_age_hours` (default 4h); everything else gets the far
    looser `default_min_age_hours` (default 168h/weekly). This exists
    because the old single 1-hour cooldown assumed cheap, unlimited
    retries — false for SerpAPI's free tier (250 searches/month, confirmed
    live 2026-08-20, and re-confirmed fully exhausted again a month later):
    at ~8 searches/day of actual budget, even a day-scale cooldown against
    a 100+ company unresolved backlog is still mathematically guaranteed to
    exhaust the month's quota in the first few days — most of discover()'s
    demand on that quota is now removed by the guess-and-verify and
    combined-query changes above, but the backlog can still be larger than
    a week's worth of budget on its own, so the default cooldown needs to
    be week-scale, not day-scale, for the budget to actually last the
    month. A several-hour priority cooldown still gives the companies that
    matter most a real shot at resolving same-day once any quota exists.

    `max_attempts_per_call` hard-caps how many companies get a fresh
    `discover()` call in a single invocation, regardless of how many are
    past their cooldown — priority companies are selected first, so a
    large non-priority backlog can never crowd them out, and a single call
    can never itself exhaust the whole monthly budget in one shot the way
    the uncapped hourly retry used to.

    Returns counts of how many were attempted and how many actually
    resolved to a usable ats_type — not just `status == "resolved"`, which
    a legacy/missing-identifier row already was before this call.
    """
    priority_normalized = {_normalize(name) for name in (priority_names or set())}
    now = _now()
    stuck_resolved = and_(
        CompanyLookup.status == "resolved",
        or_(
            CompanyLookup.ats_type.in_(_LEGACY_DEAD_END_ATS_TYPES),
            CompanyLookup.ats_type.is_(None),
            and_(CompanyLookup.ats_type == "workday", CompanyLookup.slug.is_(None)),
            and_(CompanyLookup.ats_type == "llm_scrape", CompanyLookup.url.is_(None)),
        ),
    )
    candidates = list(
        session.execute(
            select(CompanyLookup).where(or_(CompanyLookup.status == "unresolved", stuck_resolved))
        ).scalars()
    )

    eligible: list[tuple[bool, CompanyLookup]] = []
    for row in candidates:
        is_priority = row.name_normalized in priority_normalized
        cooldown = timedelta(hours=priority_min_age_hours if is_priority else default_min_age_hours)
        if row.last_attempted is None or row.last_attempted < now - cooldown:
            eligible.append((is_priority, row))

    # Priority first, so a capped batch never starves them behind a large
    # non-priority backlog.
    eligible.sort(key=lambda pair: not pair[0])
    rows = [row for _is_priority, row in eligible[:max_attempts_per_call]]

    resolved_count = 0
    for row in rows:
        result = discover(row.name_raw, session, search_api_key, force=True)
        if result.status == "resolved" and _is_usable(result.ats_type, result.slug, result.url):
            resolved_count += 1

    return RetryResult(
        attempted=len(rows),
        resolved=resolved_count,
        still_unresolved=len(rows) - resolved_count,
    )


@dataclass
class SeedResult:
    total_configured: int
    newly_attempted: int
    already_known: int


def seed_companies(
    session: Session,
    company_names: list[str],
    search_api_key: str | None = None,
) -> SeedResult:
    """
    For each name in company_names, run discover() once if — and only if —
    the company has never been seen before (no row in company_lookup at all).
    Companies already known are left alone here, regardless of status:
    resolved ones don't need re-resolving, and unresolved ones are retried on
    a schedule by retry_unresolved(), not on every seed call. Without that
    distinction, an unresolved company would burn a fresh discovery attempt
    (and search API quota) on every single run.
    """
    already_known = 0
    newly_attempted = 0

    for name in company_names:
        normalized = _normalize(name)
        existing = session.execute(
            select(CompanyLookup).where(CompanyLookup.name_normalized == normalized)
        ).scalar_one_or_none()
        if existing is not None:
            already_known += 1
            continue
        discover(name, session, search_api_key)
        newly_attempted += 1

    return SeedResult(
        total_configured=len(company_names),
        newly_attempted=newly_attempted,
        already_known=already_known,
    )
