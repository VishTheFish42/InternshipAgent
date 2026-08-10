# Task List

Check off tasks as they are completed. Phases are ordered by dependency — complete earlier phases before starting later ones.

---

## Phase 0 — Project Bootstrap

- [x] **T-001** Initialize git repository and push to GitHub
- [x] **T-002** Create Python project structure (`src/`, `src/scrapers/`, `src/scrapers/custom/`, `src/data/`, `config/`, `docs/`, `tests/`, `resumes/`)
- [x] **T-003** Create `requirements.txt` with all pinned dependencies (see design doc §11)
- [x] **T-004** Create `.env.example` with all required variable names and descriptions (no values)
- [x] **T-005** Add `.gitignore`: `.env`, `resumes/`, `profile.cache.json`, `config/companies.yaml`, `*.db`, `__pycache__`, `.venv`, `playwright/`
- [x] **T-006** Set up `pyproject.toml` with `mypy`, `pytest`, and `ruff` config
- [x] **T-007** Set up GitHub Actions CI: run `mypy`, `ruff`, and `pytest` on every push to main
- [x] **T-008** Create `profile.yaml` with default preferences (empty `keywords_excluded`, `min_score: 70`, `weekly_digest: true`)
- [x] **T-009** Create `config/companies.example.yaml` (committed format reference); verify `config/companies.yaml` (pre-populated with ~150 companies) is in `.gitignore` and not committed

---

## Phase 1 — Core Infrastructure

- [x] **T-101** Implement `src/config.py`: load and validate all env vars using `pydantic-settings`; raise descriptive errors on missing required vars; include `BURST_THRESHOLD` (default 5) and `PROFILE_CACHE` (base64 profile, optional — falls back to local `profile.cache.json` for local dev)
- [x] **T-102** Implement `src/db.py`: SQLAlchemy models for `job_postings`, `company_lookup`, `notifications`, `run_log`; `init_db()` to create schema
- [x] **T-103** Write Alembic migration baseline from current schema
- [x] **T-104** Implement `src/db.py` helpers: `upsert_posting()`, `get_unscored_postings()`, `get_unnotified_above_threshold()`, `mark_notified()`, `log_run()`, `get_unresolved_companies()`, `get_stats()`
- [x] **T-105** Write unit tests for all DB helpers using in-memory SQLite
- [x] **T-106** Implement `python -m src.db init` and `python -m src.db stats` subcommands; `stats` must show total postings, alerts sent, unresolved companies (with names), and cumulative estimated API cost

---

## Phase 2 — Resume Ingestion

- [x] **T-201** Implement `src/resume_extractor.py`: `extract_from_pdf(path: Path) -> ExtractedProfile` using `pdfplumber` for text extraction and Claude Sonnet for structured parsing; return typed `ExtractedProfile` dataclass
- [x] **T-202** Design and implement the Claude extraction prompt: ask for languages, frameworks, tools, experience level, graduation year, projects (name + description + technologies), and work experience; return JSON
- [x] **T-203** Implement `merge_profiles(profiles: list[ExtractedProfile]) -> MergedProfile`: union of all skills/languages/tools with case-insensitive deduplication; longest project description wins on conflict; most recent file's experience level and graduation year wins
- [x] **T-204** Implement `src/resume_extractor.py`: `rebuild_profile(resumes_dir: Path) -> MergedProfile`; hash all PDFs; write result + hash to `profile.cache.json` locally; print `railway variables set PROFILE_CACHE="<base64>"` command to stdout
- [x] **T-205** Implement `src/resume_extractor.py`: `detect_resume_changes(resumes_dir: Path, cache: MergedProfile) -> bool`; compare current PDF hash to `_source_hash` in cache
- [x] **T-206** Implement the `--rebuild-profile` CLI flag in `src/main.py` that calls `rebuild_profile()` and exits
- [x] **T-207** Write unit tests for merge logic: test deduplication, conflict resolution, and edge cases (empty resumes dir, single PDF, 3 PDFs with overlapping projects)
- [x] **T-208** Write integration test: feed a sample PDF resume; verify `profile.cache.json` is produced with expected fields

---

## Phase 3 — Company Discovery

- [x] **T-301** Build `src/data/company_ats_map.json`: initial mapping of ~500 well-known tech companies to ATS type + slug, sourced from SimplifyJobs and other open-source internship trackers
- [x] **T-302** Implement `src/company_discoverer.py`: `discover(company_name: str) -> CompanyRecord | None` following the 4-step pipeline (bundled table → Greenhouse web search → Lever web search → generic careers search → unresolved)
- [x] **T-303** Implement web search step using SerpAPI or Google Custom Search; extract ATS slug from first matching URL using regex
- [x] **T-304** Implement DB caching for resolved companies: never re-query a company that has already been resolved; store resolution source (`bundled_table`, `web_search`, `manual`)
- [x] **T-305** Implement weekly re-attempt for unresolved companies (in the weekly digest job)
- [x] **T-306** Write unit tests for company discoverer: mock web search; test each discovery step in isolation; test caching behavior

---

## Phase 4 — Data Ingestion (Scrapers)

### 4A — Scraper interface

- [x] **T-401** Define `src/scrapers/base.py`: `RawPosting` dataclass (source, external_id, title, company, location, is_remote, url, apply_url, description, posted_at). *Implemented as sync functions per-source rather than an async `BaseScraper` ABC, to match the rest of the codebase (no other module uses asyncio).*
- [x] **T-402** Implement keyword query builder `build_queries()` in `src/scrapers/base.py`: 10 core + 4 co-op domain queries

### 4B — Indeed RSS (free, primary broad source)

- [x] **T-411** Implement `src/scrapers/indeed_rss.py`: fetch Indeed RSS feeds per keyword query via `feedparser`; parse into `RawPosting` list
- [x] **T-412** Extract job ID (`jk` param) from the RSS guid/link; dedup key is `source="indeed"` + that ID; apply_url set to the raw link
- [x] **T-413** Rate-limit to 1 req/2s between queries; descriptive `User-Agent` header
- [x] **T-414** Retry logic (3 attempts, exponential back-off) via `tenacity`
- [x] **T-415** Unit tests with mocked feed responses (integration test against the live endpoint not added)

### 4C — Adzuna

- [ ] **T-421** Create an Adzuna developer account; obtain `app_id` and `app_key` — *user action, keys not yet in `.env`*
- [x] **T-422** Implement `src/scrapers/adzuna.py`: single broad query (not looped per keyword — free tier is 250 calls/month); filter `where=us`, `sort_by=date`. *Not wired into `main.py`'s `_SOURCES` yet — needs a slower poll cadence than the 30-min main cycle to stay in quota; decide on scheduling before enabling.*
- [x] **T-423** Unit tests with mocked HTTP responses

### 4D — Wellfound / AngelList (startup coverage)

- [ ] **T-431 – T-433** Skipped. Wellfound's public API has been effectively closed to new developer signups; not worth blocking on. Revisit if access is ever granted.

### 4E — Y Combinator Work at a Startup

- [x] **T-434** Implement `src/scrapers/yc.py`. *⚠ workatastartup.com's job search endpoint is undocumented/internal — this follows the design doc's assumed shape but has not been verified against a live response. Confirm before relying on it.*
- [x] **T-435** Filter by title/description containing SWE/ML/AI/DS keywords
- [x] **T-436** Unit tests with mocked HTTP responses

### 4F — HackerNews "Who's Hiring"

- [x] **T-437** Implement `src/scrapers/hn.py`: find the current month's thread via the Algolia HN API by searching `author_whoishiring` stories; fetch all comments containing "intern"
- [x] **T-438** Extract company name (leading `Company | Location | ...` convention) and URL via regex. *Uses regex extraction, not a Claude call per comment — the downstream AI scorer already judges relevance from the full comment text, so a second LLM pass here wasn't worth the added cost/latency.*
- [x] **T-439** Unit tests with mocked Algolia API responses

### 4G — RemoteOK

- [x] **T-450** Implement `src/scrapers/remoteok.py`: fetch the bulk feed, filter by `intern`/`junior` tags or title
- [x] **T-451** Unit tests with mocked HTTP responses

### 4H — Dice

- [x] **T-452** Implement `src/scrapers/dice.py`. *⚠ Dice no longer offers a stable free public API/RSS feed — this is a best-effort placeholder against their internal search endpoint shape; likely needs a commercial API key or replacement before going live.*
- [x] **T-453** Unit tests with mocked HTTP responses

### 4I — Cross-Source Deduplication

- [x] **T-454** Implement `src/deduplicator.py`: `normalize_url()` strips tracking params, trailing slashes, fragments
- [x] **T-455** Secondary dedup: `find_url_duplicate()` checks `apply_url_normalized` before insert
- [x] **T-456** Tertiary fuzzy dedup: `find_fuzzy_duplicate()` — same `(company_normalized, title_normalized)` within 7 days, confirmed by description similarity (`difflib`, 0.8 threshold)
- [x] **T-457** Unit tests covering the canonical cross-source scenario and the "two different roles, same title" false-positive case

### 4J — Greenhouse ATS (Tier 1)

- [x] **T-461** Implement `src/scrapers/greenhouse.py`: per resolved company, call the public Greenhouse boards API; filter intern/co-op titles
- [x] **T-462** 1 req/5s rate limit; descriptive `User-Agent` header
- [x] **T-463** Unit tests with mocked HTTP responses (live-fixture integration test not added)

### 4K — Lever ATS (Tier 1)

- [x] **T-464** Implement `src/scrapers/lever.py`: per resolved company, call the public Lever postings API; filter intern/co-op titles
- [x] **T-465** Unit tests with mocked HTTP responses

### 4L — Custom scrapers (Tier 1, major companies not on standard ATS)

- [ ] **T-466 – T-469** Not started. Lowest priority of the scraper set — Playwright-based, most brittle to maintain, and most of these companies' internship postings are also reachable via Tier 2 broad search in the meantime.

---

## Phase 5 — AI Scoring

- [x] **T-501** Implement `src/matcher.py`: `score_postings()` scores a list of `RawPosting` against the profile + preferences
- [x] **T-502** Scoring system prompt with the 4-factor rubric; returns JSON array with `external_id`, `score`, `reasoning`
- [x] **T-503** Batching: groups of 10 postings per Claude call
- [x] **T-504** Prompt caching on the system prompt via `cache_control: {"type": "ephemeral"}`
- [x] **T-505** Profile change detection: `hash_profile()` + comparison against the last `run_log` entry, wired into `run_cycle()` in `src/main.py`
- [x] **T-506** Cost estimate (`estimated_cost_usd`) computed from token usage and logged per run
- [x] **T-507** Unit tests: mocked `Anthropic` client, batching, JSON parsing, score clamping
- [x] **T-508** Covered by unit tests with mixed strong/weak/failing-batch scoring scenarios (not a live-API end-to-end test)

---

## Phase 6 — Notification System

- [ ] **T-601** Create a Twilio account; obtain account SID, auth token, and a phone number — *user action; SID/token/number already present in `.env`, unverified live*
- [x] **T-602** Implement `src/notifier.py`: `notify_matches()` routes to individual or burst mode based on `BURST_THRESHOLD`
- [x] **T-603** Individual SMS formatter: ≤480 chars, full URL always included, reasoning truncated with an ellipsis
- [x] **T-603a** Burst SMS formatter: ranked by score, caps at 10 lines + overflow count, ends with a `db stats` pointer
- [x] **T-604** Weekly digest SMS formatter: stats, top match, unresolved companies
- [x] **T-605** `check_delivery_status()` implemented (fetches Twilio status by SID); not yet wired into a scheduled "check 2 minutes later" job
- [x] **T-606** `send_sms_with_retry()`: one retry after a 5-minute wait, permanent failure logged
- [x] **T-607** Phone numbers redacted to last 4 digits (`_redact_phone`) in every log call
- [x] **T-608** Unit tests: mocked Twilio client, message formatting, phone redaction in logs
- [ ] **T-609** Manual end-to-end test (`--dry-run` then a real SMS) — *requires a live Twilio account, user action*

---

## Phase 7 — Orchestration

- [x] **T-701** `src/main.py`: `--run-once`, `--dry-run`, `--rebuild-profile`, `--rescore` CLI flags
- [x] **T-702** `run_cycle()`: fetch → dedupe → score → notify → log. *Sources are polled sequentially, not in parallel — acceptable at the current source count; revisit with `concurrent.futures` if the source list grows.*
- [x] **T-703** APScheduler: `IntervalTrigger` for the main cycle, `CronTrigger(day_of_week="sun", hour=9)` for the weekly digest
- [x] **T-704** Graceful shutdown on `SIGTERM`/`SIGINT` via `scheduler.shutdown(wait=True)`
- [x] **T-705** `--rescore` mode: re-scores all `notified=FALSE` postings against the current profile
- [x] **T-706** Profile-change detection triggers a re-score of unnotified postings on the next cycle (see T-505)
- [x] **T-707** JSON-formatted log lines via a custom `logging.basicConfig` format string. *Uses stdlib `logging`, not the `structlog` dependency listed in the design doc — kept consistent with every other module in the codebase, none of which use structlog either.*

---

## Phase 8 — Cloud Deployment

- [x] **T-801** `Procfile`: `worker: python -m src.main`
- [x] **T-802** `railway.toml` with nixpacks build config and restart policy
- [ ] **T-803** Create Railway account; link to GitHub repository — *user action*
- [ ] **T-804** Set all environment variables in Railway dashboard — *user action*
- [ ] **T-805** Deploy to Railway; verify first run completes and logs look correct — *user action*
- [ ] **T-806** Verify an SMS is received on your phone after the first live run — *user action*
- [ ] **T-807** Set up Railway persistent volume for SQLite (or switch to Postgres plugin) — *user action*
- [ ] **T-808** Confirm Railway restarts the worker automatically after a crash — *user action*
- [ ] **T-809** Test the `--rebuild-profile` → commit → push → Railway redeploy workflow end-to-end — *user action*

---

## Phase 9 — Testing & Hardening

- [x] **T-901** Unit test coverage across `src/` is 85% (`pytest --cov=src`), above the 80% target
- [x] **T-902** Test failure isolation: `test_fetch_all_postings_continues_after_source_failure` / `_fetch_all_postings` in `src/main.py`
- [x] **T-903** Covered indirectly: `upsert_deduped` primary-key check makes a second identical run a no-op (`test_upsert_deduped_same_source_id_is_duplicate`)
- [x] **T-909** Cross-source dedup covered by `test_upsert_deduped_cross_source_same_url_is_duplicate` and `..._fuzzy_match_when_urls_differ`
- [x] **T-904** Profile-change → re-score flow covered by `run_cycle`'s `profile_changed` branch (unit-level, not a full `--rebuild-profile` integration run)
- [x] **T-905** `test_run_cycle_dry_run_never_calls_twilio` — dry-run completes fetch/dedupe/score but sends nothing
- [x] **T-906** Covered in `test_company_discoverer.py` (unresolved-company path)
- [ ] **T-907** Nightly GitHub Actions live-API integration job — not added
- [x] **T-908** Phone redaction verified by `test_send_sms_does_not_leak_phone_number_in_logs`; no other secrets are logged anywhere in the new code

---

## Phase 10 — Ongoing Maintenance

- [ ] **T-1001** When you update your resume, run `python -m src.main --rebuild-profile`, then commit and push `profile.cache.json`
- [ ] **T-1002** Review `python -m src.db stats` monthly; check for unresolved companies and investigate if any are important targets
- [ ] **T-1003** Update `config/companies.yaml` as you discover new target companies (just add the name — no URLs needed)
- [ ] **T-1004** Adjust `profile.yaml → matching.min_score` if you're getting too many alerts (raise) or too few (lower)
- [ ] **T-1005** Monitor Railway logs weekly for source errors or unusual API cost spikes
- [ ] **T-1006** Update `src/data/company_ats_map.json` periodically from the SimplifyJobs repo (run the update script)
- [ ] **T-1007** Rotate API keys every 6 months (Twilio, RapidAPI, Anthropic)
- [ ] **T-1008** Review and update custom scrapers (Google, Meta, Microsoft) if career page structure changes

---

## Summary

| Phase | Task range | Description |
|---|---|---|
| 0 | T-001 – T-009 | Project bootstrap & CI |
| 1 | T-101 – T-106 | Core infrastructure (DB, config) |
| 2 | T-201 – T-208 | Resume ingestion pipeline |
| 3 | T-301 – T-306 | Company discovery |
| 4 | T-401 – T-469 | Data ingestion (9 sources + cross-source dedup) |
| 5 | T-501 – T-508 | AI scoring (Claude) |
| 6 | T-601 – T-609 | SMS notifications (Twilio) |
| 7 | T-701 – T-707 | Orchestration & CLI |
| 8 | T-801 – T-809 | Cloud deployment (Railway) |
| 9 | T-901 – T-908 | Testing & hardening |
| 10 | T-1001 – T-1008 | Ongoing maintenance |

**Total**: ~90 tasks across 10 phases. Phases 0–8 are the v1 build. Phase 9 is pre-launch hardening. Phase 10 is recurring.

**Status as of 2026-08-09**: Phases 0–3 and 5–7 are code-complete and tested. Phase 4 is done except Wellfound (skipped — API access closed) and the three custom Playwright scrapers (Google/Meta/Microsoft — not started). Phase 6 is code-complete; the Twilio account itself and a live manual test are still open (user action). Phase 8's config files exist; the actual Railway account/deploy steps are open (user action). Phase 9 is mostly covered by unit tests; the nightly live-API CI job is not added. 253 tests passing, 85% coverage on `src/`, mypy/ruff clean.
