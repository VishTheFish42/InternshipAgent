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
- [x] **T-008** Create `profile.yaml` with default preferences (empty `keywords_excluded`, `min_score: 70`, `digest_enabled: true`)
- [x] **T-009** Create `config/companies.example.yaml` (committed format reference); verify `config/companies.yaml` (pre-populated with ~150 companies) is in `.gitignore` and not committed

---

## Phase 1 — Core Infrastructure

- [x] **T-101** Implement `src/config.py`: load and validate all env vars using `pydantic-settings`; raise descriptive errors on missing required vars; include `BURST_THRESHOLD` (default 20 — see T-611) and `PROFILE_CACHE` (base64 profile, optional — falls back to local `profile.cache.json` for local dev)
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

- [x] **T-301** Build `src/data/company_ats_map.json`: initial mapping of ~446 well-known tech companies to ATS type + slug. *Correction: no evidence this was actually scraped from SimplifyJobs — there's no fetch script or cached source data from that original session, just the hand-compiled JSON. Likely LLM-knowledge-generated from well-known public ATS mappings. `scripts/update_ats_map.py` (T-1006) now does a real, verified live fetch from SimplifyJobs going forward.*
- [x] **T-302** Implement `src/company_discoverer.py`: `discover(company_name: str) -> CompanyRecord | None` following the 4-step pipeline (bundled table → Greenhouse web search → Lever web search → generic careers search → unresolved)
- [x] **T-303** Implement web search step using SerpAPI or Google Custom Search; extract ATS slug from first matching URL using regex
- [x] **T-304** Implement DB caching for resolved companies: never re-query a company that has already been resolved; store resolution source (`bundled_table`, `web_search`, `manual`)
- [x] **T-305** Implement hourly re-attempt for unresolved companies (in the digest job; 1-hour per-company cooldown via `retry_unresolved(min_age_hours=1)`)
- [x] **T-306** Write unit tests for company discoverer: mock web search; test each discovery step in isolation; test caching behavior
- [x] **T-307** *(added)* `seed_companies()` in `src/company_discoverer.py`, wired into `run_cycle()` in `src/main.py`: reads `config/companies.yaml` and calls `discover()` once for any company with no existing `company_lookup` row. This was a real gap found during a code walkthrough — `discover()`/`retry_unresolved()` existed and were tested, but nothing ever actually read the user's plain-English company list and triggered discovery on it, so Tier 1 monitoring silently did nothing regardless of what was in `companies.yaml`.

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

- [x] **T-421** Adzuna developer account created; `app_id`/`app_key` in `.env`
- [x] **T-422** Implement `src/scrapers/adzuna.py`: single broad query (not looped per keyword — free tier is 250 calls/month); filter `where=us`, `sort_by=date`. Wired in via `run_adzuna_poll()` in `src/main.py`, scheduled on its own `IntervalTrigger(weeks=1)` — separate from the main `_SOURCES` cycle, deliberately, to stay in quota (~4 calls/month vs. a 250/month cap). New postings are stored unscored and picked up by the next main cycle like any other source.
- [x] **T-423** Unit tests with mocked HTTP responses, plus `run_adzuna_poll()` orchestration tests in `test_main.py`

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
- [x] **T-509** *(added 2026-08-11)* Scoring prompt/response extended to return `missing_qualifications: list[str]` per posting — specific gaps vs. the profile, empty list if none. `ScoredPosting` dataclass and `_parse_batch_response()` updated; `record_score()` in `db.py` persists it to a new `job_postings.missing_qualifications` JSON column.

---

## Phase 6 — Notification System

**Pivoted from Twilio SMS to the Telegram Bot API on 2026-08-11.** Twilio's toll-free verification was rejected (reason code 30445: business information could not be verified) — their A2P/toll-free review process is built to vet registered businesses, and a personal single-recipient tool doesn't fit that mold. Retrying with corrected info would risk hitting the same wall for the same underlying reason. Telegram's Bot API requires no business verification of any kind and delivers the same real-time push-notification experience. `src/db.py`'s `notifications` table was also updated pre-launch (no real data existed yet): `phone_number` → `recipient_id`, `twilio_sid` → `telegram_message_id`.

- [x] **T-601** ~~Create a Twilio account~~ — Superseded. Create a Telegram bot via `@BotFather` instead (free, instant, no verification) — *user action*
- [x] **T-602** Implement `src/notifier.py`: `notify_matches()` routes to individual or burst mode based on `BURST_THRESHOLD`
- [x] **T-603** Individual message formatter: ≤4096 chars (Telegram's limit), full URL always included, reasoning truncated with an ellipsis
- [x] **T-603a** Burst message formatter: ranked by score, caps at 10 lines + overflow count, ends with a `db stats` pointer. *Updated 2026-08-11: now includes an apply link per item (see T-611).*
- [x] **T-604** Digest message formatter: last-hour stats, top match, unresolved companies
- [x] **T-605** ~~Twilio delivery-status polling~~ — N/A for Telegram: `sendMessage` returning `ok: true` with a `message_id` is itself the delivery confirmation; there's no separate polling endpoint to check status against, unlike Twilio's `MessageSid` lookup.
- [x] **T-606** `send_message_with_retry()`: one retry after a 5-minute wait, permanent failure logged
- [x] **T-607** Chat IDs redacted to last 4 characters (`_redact_chat_id`) in every log call
- [x] **T-608** Unit tests: mocked `httpx.post` against Telegram's response shape, message formatting, chat ID redaction in logs
- [x] **T-609** Manual end-to-end test: real bot token/chat ID set, `notifier.send_message()` confirmed delivered to phone on 2026-08-11
- [x] **T-610** *(added 2026-08-11)* Partial-match notifications: postings scoring in `[partial_match_min_score, min_score)` with `<= max_missing_qualifications` gaps are surfaced (see T-611 for individual/burst routing), listing company/title/score/missing quals, ranked by score. New `job_postings.partial_notified` column (separate from `notified`) tracks delivery without blocking a future full-match alert if a rescore pushes the score up. New DB helpers: `get_unnotified_partial_matches()`, `mark_partial_notified()`. Wired into `run_cycle()` in `src/main.py` right after full-match notification. Required a real Alembic migration (`7b8bf99b2722_partial_match_tracking.py`) applied to the live Railway DB — see Phase 8 update below.
- [x] **T-611** *(added 2026-08-11, same day)* Per explicit request — individual mode is now the default for both full and partial matches, not just a fallback for small counts. `BURST_THRESHOLD` default raised 5 → 20 (individual is normal; batching is now a safety net for genuinely high-volume cycles, not the common case). Added apply links to `format_burst_message()` and `format_partial_match_message()` (previously batched messages omitted them). Added `format_partial_match_individual_message()` and `notify_partial_matches()` (mirrors `notify_matches()`) — partial matches previously had no individual mode at all, always one batched message regardless of count. Individual sends in both `notify_matches()` and `notify_partial_matches()` are now paced 1 second apart (`_INDIVIDUAL_SEND_DELAY_SECONDS`) to respect Telegram's per-chat rate limit. *Caught during testing:* `_notify_partial_matches_and_mark()`'s tests originally patched `src.main.send_message_with_retry`, correct for the old always-batched implementation but wrong once it routes through `notify_partial_matches()` in `notifier.py` — individual mode's unpatched real `send_message_with_retry` call hit a fake endpoint and triggered its real 300-second retry sleep, hanging the test suite. Fixed by patching the router (`src.main.notify_partial_matches`) instead, matching the pattern already used for full-match tests.
- [x] **T-612** *(added 2026-08-11, later)* "Mark Applied" tracking: every individual-mode alert (full or partial) now carries an inline Telegram keyboard button (`applied_button()` in `src/notifier.py`) with `callback_data="applied:<posting_id>"`. Tapping it fires a `callback_query`, handled by the new `process_telegram_updates()` in `src/main.py`, which calls the new `mark_applied()` DB helper and acknowledges via `answer_callback_query()`. New `job_postings.applied`/`applied_at` columns; `get_unnotified_above_threshold()` and `get_unnotified_partial_matches()` now exclude `applied=True` rows so a posting the user already applied to is never re-surfaced even if a later profile change pushes its score up. Deliberately individual-mode-only — burst-mode digests skip the button (one per line in a 20-item batch wasn't worth the complexity).
- [x] **T-613** *(added 2026-08-11, later)* Master notifications pause/resume via Telegram commands `/pause` and `/resume`, handled by the same `process_telegram_updates()`. New singleton `bot_state` table (`get_bot_state()`, `set_notifications_paused()`) holds the flag plus the `getUpdates` polling cursor (`last_update_id`, advanced via `update_last_telegram_update_id()` — never moves backward). When paused, `run_cycle()` and `run_digest()` skip sending but the rest of the pipeline (fetch/dedupe/score) still runs — matches stay unnotified and get sent for real once `/resume` is issued, so nothing is lost, only delayed. Both `getUpdates` and the "Mark Applied" callback check the update's `chat.id` against the configured `TELEGRAM_CHAT_ID` and silently ignore anything from another chat, since the bot would otherwise accept commands from anyone who discovers its username. Polled via a new dedicated scheduler job (`IntervalTrigger(minutes=2)`) decoupled from the hourly main cycle so pause/resume and mark-applied feel responsive.
- [x] **T-614** *(added 2026-08-11, later)* Posting freshness filter: `matching.max_posting_age_days` (default 7) in `profile.yaml`. Applied in `_dedupe_and_store()` — postings older than the window (by `posted_at`) are skipped before insertion at all, so they're never scored or stored, not just never notified. Postings with no reliable `posted_at` (`None`) are kept rather than silently dropped, since not every source populates it consistently.

Schema change for T-612/T-613 required a second real Alembic migration against the live production DB (`1f7647697c13_applied_tracking_and_bot_state.py`) — see Phase 8 update below.

---

## Phase 7 — Orchestration

- [x] **T-701** `src/main.py`: `--run-once`, `--dry-run`, `--rebuild-profile`, `--rescore` CLI flags
- [x] **T-702** `run_cycle()`: fetch → dedupe → score → notify → log. *Sources are polled sequentially, not in parallel — acceptable at the current source count; revisit with `concurrent.futures` if the source list grows.*
- [x] **T-703** APScheduler: `IntervalTrigger(minutes=RUN_INTERVAL_MINUTES)` (default 60) for the main cycle, `IntervalTrigger(hours=1)` for the digest — changed from the original weekly `CronTrigger(day_of_week="sun", hour=9)` per explicit request (2026-08-11)
- [x] **T-704** Graceful shutdown on `SIGTERM`/`SIGINT` via `scheduler.shutdown(wait=True)`
- [x] **T-705** `--rescore` mode: re-scores all `notified=FALSE` postings against the current profile
- [x] **T-706** Profile-change detection triggers a re-score of unnotified postings on the next cycle (see T-505)
- [x] **T-707** JSON-formatted log lines via a custom `logging.basicConfig` format string. *Uses stdlib `logging`, not the `structlog` dependency listed in the design doc — kept consistent with every other module in the codebase, none of which use structlog either.*

---

## Phase 8 — Cloud Deployment

- [x] **T-801** `Procfile`: `worker: python -m src.main`
- [x] **T-802** `railway.toml` with nixpacks build config and restart policy
- [x] **T-803** Railway project `InternshipAgent` created, service linked directly to the `VishTheFish42/InternshipAgent` GitHub repo (`main` branch) — pushes auto-redeploy
- [x] **T-804** All 12 env vars set on the service (Anthropic, Telegram, Adzuna, SerpAPI, `DATABASE_URL`, scheduler/model config), including `PROFILE_CACHE` from the existing local `profile.cache.json`
- [x] **T-805** Deployed; status `● Online`. Verified via deploy logs: volume mounted, all 3 scheduled jobs registered (`_scheduled_cycle`, `_scheduled_digest`, `_scheduled_adzuna`), scheduler started
- [x] **T-806** Telegram delivery itself already verified separately (`notifier.send_message()` direct test, message confirmed received). A `--run-once` executed directly inside the live container via `railway ssh` completed the full pipeline — 4/5 sources returned data, 2 new postings found and scored via Claude ($0.0017), 0 alerts sent because neither posting cleared the score threshold (correct filtering behavior, not a failure — the send path itself is proven, just not exercised by this particular run's data)
- [x] **T-807** Persistent volume (`internshipagent-volume`, 5GB) attached at `/data`; `DATABASE_URL=sqlite:////data/internship_agent.db`. Confirmed working — the `--run-once` SSH test above wrote real rows to it
- [ ] **T-808** Restart-on-crash policy is configured (`ON_FAILURE`, max 10 retries) but not stress-tested — didn't intentionally crash the live service to verify. Low-risk to leave unverified given `restartPolicyType` is a Railway platform guarantee, not custom code
- [ ] **T-809** `--rebuild-profile` → commit → push → auto-redeploy workflow not yet tested end-to-end — needs an actual resume update to exercise for real

**Notes from the live deploy:**
- Indeed's RSS feed returned `403 Forbidden` on the live run — likely bot/rate-limit detection on Railway's IP range. Source failure isolation worked exactly as designed (logged as an error, other 4 sources completed normally, run didn't crash) — but Indeed coverage may need revisiting (rotating User-Agent, a delay, or accepting it as a known gap) if it's consistently blocked from Railway's network.
- Hit two real Railway CLI issues during setup: `railway volume add` panics (`unwrap() on None`) when passing service/environment by *name* — works fine with explicit IDs instead. `railway ssh` requires an unlocked (non-passphrase-protected) SSH key registered with Railway, and the SSH session's `PATH` does not include the app's Nixpacks virtualenv (`/opt/venv/bin`) — must invoke `/opt/venv/bin/python` explicitly, not bare `python`.

---

## Phase 9 — Testing & Hardening

- [x] **T-901** Unit test coverage across `src/` is 85% (`pytest --cov=src`), above the 80% target
- [x] **T-902** Test failure isolation: `test_fetch_all_postings_continues_after_source_failure` / `_fetch_all_postings` in `src/main.py`
- [x] **T-903** Covered indirectly: `upsert_deduped` primary-key check makes a second identical run a no-op (`test_upsert_deduped_same_source_id_is_duplicate`)
- [x] **T-909** Cross-source dedup covered by `test_upsert_deduped_cross_source_same_url_is_duplicate` and `..._fuzzy_match_when_urls_differ`
- [x] **T-904** Profile-change → re-score flow covered by `run_cycle`'s `profile_changed` branch (unit-level, not a full `--rebuild-profile` integration run)
- [x] **T-905** `test_run_cycle_dry_run_never_sends_notifications` — dry-run completes fetch/dedupe/score but sends nothing
- [x] **T-906** Covered in `test_company_discoverer.py` (unresolved-company path)
- [ ] **T-907** Nightly GitHub Actions live-API integration job — not added
- [x] **T-908** Chat ID redaction verified by `test_send_message_does_not_leak_chat_id_in_logs`; no other secrets are logged anywhere in the new code

---

## Phase 10 — Ongoing Maintenance

- [ ] **T-1001** When you update your resume, run `python -m src.main --rebuild-profile`, then commit and push `profile.cache.json`
- [ ] **T-1002** Review `python -m src.db stats` monthly; check for unresolved companies and investigate if any are important targets
- [ ] **T-1003** Update `config/companies.yaml` as you discover new target companies (just add the name — no URLs needed)
- [ ] **T-1004** Adjust `profile.yaml → matching.min_relevance_score` if off-domain noise creeps back in (raise) or genuine postings seem to be getting filtered out (lower) — it's a domain-relevance floor, not a fit-quality bar (§8.2a, design.md)
- [ ] **T-1005** Monitor Railway logs weekly for source errors or unusual API cost spikes
- [x] **T-1006** `scripts/update_ats_map.py` fetches SimplifyJobs' live README, extracts (company, apply URL) pairs, classifies against known Greenhouse/Lever/Workday URL patterns, and adds any company not already in the bundled map (never overwrites existing entries). Run periodically: `python scripts/update_ats_map.py` (`--dry-run` to preview). Verified against the live repo: 170 postings parsed, 56 companies classified, 48 new entries available beyond the existing 446 as of this writing.
- [ ] **T-1007** Rotate API keys every 6 months (Telegram bot token, RapidAPI, Anthropic)
- [ ] **T-1008** Review and update custom scrapers (Google, Meta, Microsoft) if career page structure changes

---

## Summary

| Phase | Task range | Description |
|---|---|---|
| 0 | T-001 – T-009 | Project bootstrap & CI |
| 1 | T-101 – T-106 | Core infrastructure (DB, config) |
| 2 | T-201 – T-208 | Resume ingestion pipeline |
| 3 | T-301 – T-307 | Company discovery |
| 4 | T-401 – T-469 | Data ingestion (9 sources + cross-source dedup) |
| 5 | T-501 – T-508 | AI scoring (Claude) |
| 6 | T-601 – T-609 | Notifications (Telegram Bot API) |
| 7 | T-701 – T-707 | Orchestration & CLI |
| 8 | T-801 – T-809 | Cloud deployment (Railway) |
| 9 | T-901 – T-908 | Testing & hardening |
| 10 | T-1001 – T-1008 | Ongoing maintenance |

**Total**: ~90 tasks across 10 phases. Phases 0–8 are the v1 build. Phase 9 is pre-launch hardening. Phase 10 is recurring.

**Status as of 2026-08-09**: Phases 0–3 and 5–7 are code-complete and tested. Phase 4 is done except Wellfound (skipped — API access closed) and the three custom Playwright scrapers (Google/Meta/Microsoft — not started). Phase 6 is code-complete; the Twilio account itself and a live manual test are still open (user action). Phase 8's config files exist; the actual Railway account/deploy steps are open (user action). Phase 9 is mostly covered by unit tests; the nightly live-API CI job is not added. 253 tests passing, 85% coverage on `src/`, mypy/ruff clean.

**Update 2026-08-10**: Closed a real gap found during interview-prep code review — `config/companies.yaml` was never actually connected to `discover()`, so Tier 1 company monitoring was inert regardless of what was configured (T-307, above). Also built `scripts/update_ats_map.py` (T-1006) as a real, live-verified alternative to the never-actually-scraped SimplifyJobs claim in T-301. 282 tests passing.

**Update 2026-08-11**: Pivoted notifications from Twilio SMS to the Telegram Bot API (Phase 6, above) after Twilio rejected toll-free verification — the business-verification requirement doesn't fit a personal single-recipient tool. `src/notifier.py`, `src/config.py`, `src/main.py`, and the `notifications` table schema were all updated; `SMS_CONSENT.md` was removed (it was written specifically for Twilio A2P compliance and no longer applies). Verified end-to-end: a real Telegram message was sent and confirmed delivered. 282 tests passing, `twilio` dependency fully removed.

**Update 2026-08-11 (later same day)**: Per explicit request — main poll cycle changed from every 30 to every 60 minutes (`RUN_INTERVAL_MINUTES` default); the digest job changed from a weekly `CronTrigger(day_of_week="sun", hour=9)` to an hourly `IntervalTrigger(hours=1)`, and `retry_unresolved()`'s per-company cooldown changed from `min_age_days=7` to `min_age_hours=1` to match (T-305, T-604, T-703, above; `format_weekly_digest`→`format_digest`, `run_weekly_digest`→`run_digest`, `weekly_digest`→`digest_enabled` config key). Digest stats are now scoped to the last hour instead of `get_stats()`'s all-time totals, via a new `_get_period_stats()` helper. Also wired Adzuna into the scheduler on its own `IntervalTrigger(weeks=1)` job (`run_adzuna_poll()`, T-421–T-423, above) — separate from the main cycle to respect its 250-calls/month free tier. 286 tests passing, 85% coverage, mypy/ruff clean.

**Update 2026-08-11 (Railway deployment)**: Phase 8 is done except T-808 (restart-on-crash not stress-tested) and T-809 (resume-update redeploy workflow not yet exercised) — see Phase 8 above for full detail and the two Railway CLI issues hit along the way. The agent is genuinely live: project created, linked to GitHub for auto-deploy on push, all env vars set, persistent volume attached, and a real `--run-once` executed successfully inside the deployed container via `railway ssh` (4/5 sources returned data; Indeed 403'd, isolated cleanly per NFR-01; 2 postings found and scored for $0.0017; 0 alerts because none cleared threshold). This is the first point at which the project is actually running in the cloud rather than only locally.

**Update 2026-08-11 (partial-match feature + first live schema migration)**: Added partial-match notifications (T-509, T-610, above). This was the first schema change made *after* the app was already live on Railway with real data, so — unlike every prior schema change this project — it went through a real Alembic migration rather than a baseline edit. Process: (1) wrote and locally verified the migration (upgrade + downgrade) against a scratch DB seeded with a fake pre-migration row, confirming it survives intact; (2) discovered the production DB had no `alembic_version` table at all (it was created via `Base.metadata.create_all()`, not `alembic upgrade`), so it first had to be `alembic stamp`-ed at the baseline revision before an incremental upgrade would apply cleanly; (3) deployed the new code first (the migration file has to exist in the container before `alembic upgrade` can run it), then SSH'd in and ran the migration immediately after, ahead of the next scheduled cycle; (4) verified via `sqlite3`-equivalent Python queries that the new columns exist with correct types/defaults and the 2 existing production rows survived untouched; (5) ran a fresh `--run-once` against the live container post-migration to confirm no regression. 314 tests passing, 85.5% coverage, mypy/ruff clean.

**Update 2026-08-11 (mark-applied, pause/resume, freshness filter)**: Added the three post-launch requests from real usage (T-612–T-614, above): a "Mark Applied" inline button on individual-mode alerts that permanently excludes a posting from future notification regardless of rescoring; `/pause` and `/resume` Telegram commands as a master kill switch distinct from Telegram's own per-chat mute (the pipeline keeps running underneath, it just holds sends until resumed); and a `max_posting_age_days` (default 7) freshness filter that skips stale postings before they're ever stored or scored. The first two required the bot to receive input for the first time — previously `src/notifier.py` only ever sent. Added `get_updates()`/`answer_callback_query()` wrappers around Telegram's `getUpdates`/`answerCallbackQuery` endpoints and a new `process_telegram_updates()` orchestrator in `src/main.py`, polled by its own `IntervalTrigger(minutes=2)` scheduler job — deliberately decoupled from the hourly main cycle so control feels responsive from the phone. Both inbound paths verify the update's `chat.id` against the configured chat before acting, since the bot token alone doesn't restrict who can message the bot. Second real production Alembic migration (`1f7647697c13`), verified with the same stamp/upgrade/downgrade-round-trip process established in T-610's migration. 354 tests passing, 87% coverage, mypy/ruff clean.

**Deploy note (same day)**: This migration hit a new failure mode T-610 didn't: it creates a brand-new table (`bot_state`), and `init_db()` calls `Base.metadata.create_all()` on every process start, which *does* auto-create missing tables (unlike columns on existing tables, which it leaves alone). The redeployed container's own startup created `bot_state` before the operator-run `alembic upgrade head` reached that same step, so the migration failed on `table bot_state already exists` — with the `job_postings.applied`/`applied_at` `ADD COLUMN` statements ahead of it in the same migration having already committed (SQLite migrations run non-transactionally). Fixed by stamping straight to the new revision (`alembic stamp 1f7647697c13`) instead of re-running the DDL, after confirming via direct schema inspection that the live state already matched the target exactly. Separately, the scheduler's hourly jobs fired twice (05:13 and 06:13 UTC) before the SSH session reached the migration — the interval clock starts from deploy time, not from when the operator gets around to migrating — so `_scheduled_cycle` and `_scheduled_digest` both threw `no such column: job_postings.applied` and were skipped for that hour. Self-healing and lossless (isolated exceptions, scheduler kept running, a manual `--run-once` after the fix caught up on all postings missed during the gap, 0 alerts were pending anyway) but confirms the real lesson from T-610 more sharply: for any migration that adds a new *table*, run the migration immediately on deploy, don't leave any gap — the app's own boot-time `create_all()` and the scheduler's first job firing both race ahead of a manual SSH step.

**Update 2026-08-11 (individual-by-default notifications + apply links, T-611)**: Per explicit request — individual messages are now the default for both full and partial matches (previously partial matches had no individual mode at all; full matches batched at just 5). `BURST_THRESHOLD` default raised to 20, and every message type (individual, burst, partial) now includes a per-item apply link. Individual sends are paced 1s apart for Telegram's rate limit. 324 tests passing. Notable bug caught during this change: two partial-match tests were mocking the wrong function (`send_message_with_retry` instead of the new `notify_partial_matches` router) — the mismatch let a real, unmocked network call with a real 300-second retry sleep run during the test suite, hanging it. Fixed by mocking at the same layer the equivalent full-match tests already did.

**Update 2026-08-12 (companies.yaml deploy gap fixed; no-floor notifications)**: Investigated real missed postings (Figma, PathAI) reported by the user. Root cause: `config/companies.yaml` is gitignored and was never actually reaching the deployed container — confirmed by zero Greenhouse/Lever HTTP calls across the full captured production log window, despite Figma being listed locally. Company-direct monitoring (Tier 1) had been silently inert in production since the very first deploy (see the 2026-08-11 Railway deployment note above — "0 alerts because none cleared threshold" was itself a symptom of the same underlying threshold-gating design, not just this gap). Fixed with a `COMPANIES_CONFIG` env var (base64 `companies.yaml`, mirroring the existing `PROFILE_CACHE` pattern) as the production source of truth, local file as dev fallback, and a new `--sync-companies` CLI flag to regenerate it after edits — same rhythm as `--rebuild-profile`. Added PathAI to the watchlist.

Separately, per explicit request: removed the `min_score`/`partial_match_min_score`/`max_missing_qualifications` gating entirely. There is no longer a minimum-score floor — every scored posting gets exactly one Telegram message (individual or burst, same routing as before) carrying its score, reasoning, and missing qualifications; the full/partial split collapsed into a single path (`get_unnotified_scored_postings`, `MatchInfo.missing_qualifications`). `partial_notified` stays in the schema, unused, rather than pulling a migration for a column drop — not worth the risk given the deploy-race incident two entries up. 331 tests passing, 87% coverage, mypy/ruff clean. Also rebuilt `profile.cache.json` from the current `resumes/VISHWA PRAKASH.pdf` — its hash didn't match the cache's `source_hash`, meaning the deployed profile had been stale relative to the resume already sitting in the repo.

**Update 2026-08-12 (JSearch added for LinkedIn coverage)**: Per explicit request. `docs/requirements.md`'s original constraint — "LinkedIn: Direct scraping is not permitted. LinkedIn coverage comes exclusively through JSearch" — was designed in from day one but never actually built; the project shipped with free Indeed RSS instead (`docs/design.md` §4.1, "replaces JSearch") and accepted the LinkedIn gap to stay budget-conscious. Declined to build a direct LinkedIn scraper now too — LinkedIn actively blocks automated access and prohibits it in their ToS, and it'd require holding a logged-in session's credentials in the agent. Added `src/scrapers/jsearch.py` instead (RapidAPI JSearch — a legitimate aggregator API that re-sells LinkedIn/Glassdoor/ZipRecruiter listings), wired in as `run_jsearch_poll()` on its own `IntervalTrigger(weeks=1)` job, same treatment as Adzuna and for the same reason: metered per-request quota, one broad query per poll rather than looping the domain keyword list. Requires `JSEARCH_API_KEY`; silently skipped (like Adzuna without credentials) if unset. 339 tests passing, mypy/ruff clean. Noted in passing while touching the README's coverage table: the YC and Dice scrapers exist in `src/scrapers/` but were never actually wired into `_SOURCES` — they're dormant, a pre-existing gap unrelated to this change.

**Update 2026-08-12 (JSearch cadence tuned to a 200/month free-tier plan, same day)**: User was evaluating specific RapidAPI JSearch plans. Sized the poll interval to the actual constraint instead of the initial weekly default: at one broad query per poll, every 2 hours is ~360 requests/month (over a 200/month free tier); every 4 hours is ~180/month (fits with margin). Changed `_scheduled_jsearch` from `IntervalTrigger(weeks=1)` to `IntervalTrigger(hours=4)`. Worth remembering the arithmetic pattern (`queries_per_poll × polls_per_day × 30`) next time this needs re-tuning for a different plan tier.

**Update 2026-08-12 (JSearch endpoint corrected against the user's live subscription, same day)**: The initial `src/scrapers/jsearch.py` was written from general knowledge of "the" JSearch API and was wrong in two ways once tested against the user's actual RapidAPI key: (1) the endpoint is `/search-v2`, not `/search` — the latter 404s with `"Endpoint '/search' does not exist"` on this subscription, confirming RapidAPI's "JSearch" listings aren't all identical; (2) the response nests results under `data.jobs`, not `data` directly. Both found by live-testing against the real key rather than trusting the original assumption — worth remembering next time a scraper is written against a remembered-but-unverified API shape instead of a confirmed live response. Also added `job_publisher` (e.g. "LinkedIn", "ZipRecruiter", "BeBee") to the mapping, encoded into `source` as `jsearch:{publisher}` — same compound-source convention `greenhouse.py`/`lever.py` already use for company slugs. Verified live: 10 postings returned for a test query, 4 explicitly `job_publisher: LinkedIn`.

Per explicit request, also surfaced *which board* found each posting — previously invisible in the outbound message despite being tracked in `JobPosting.source` all along. Added `MatchInfo.source`, a `_friendly_source()` mapper in `notifier.py` (`"greenhouse:stripe"` → `"Greenhouse"`, `"jsearch:LinkedIn"` → `"LinkedIn"` — the jsearch prefix's suffix is itself the human-readable label, everything else's suffix is an internal slug not worth showing), and a `Source:` line in both individual and burst messages. 351 tests passing, mypy/ruff clean.

**Update 2026-08-12 (docs/requirements.md and docs/design.md brought current, same day)**: User asked directly whether the specs were still accurate. They weren't — audited both against the actual code rather than assuming. Fixed everything from today's changes (JSearch now active per FR-02; no-score-floor per FR-26/FR-30; removed FR-34a–d, the entire partial-match tier requirement; FR-31 now lists missing_qualifications and source board). Also found and fixed several inconsistencies that predate today, per explicit instruction to mark them "not currently implemented" rather than build or remove them: Wellfound has no scraper at all despite FR-02 listing it as required; YC and Dice scrapers exist but were never wired into any scheduled job. Also caught while auditing, unrelated to any of the above: `keywords_excluded` was never actually a pre-scoring filter (FR-05's stated intent) — it's only rendered into the Claude prompt as a hint in `matcher.py`, so excluded postings still get scored and billed; `detect_resume_changes()` is dead code, never called from `main.py`, so the documented "warns on resume file changes at startup" behavior doesn't happen; `design.md`'s own resume-update workflow told the user to `git commit profile.cache.json`, contradicting its own §2.4 two sections earlier (it's gitignored); the `bot_state` table was entirely missing from the data model section despite existing in the real schema since the mark-applied/pause-resume migration; `structlog` is a listed dependency nothing imports. None of these were fixed in code — documented as known gaps per the user's "implement later" instruction. Doc-only change, no test/deploy impact.

**Update 2026-08-12 (domain-relevance floor reintroduced after real off-domain postings reached Telegram, same day)**: User reported random non-software postings on Telegram — not the no-score-floor design working as intended, an actual bug: `remoteok.py`'s `_INTERN_TAGS` matched the bare `"junior"` tag, which RemoteOK applies across every field (marketing, design, sales), not just software. `hn.py`'s regex matches "intern" anywhere in a hiring-thread comment, no domain check at all. Previously the old `min_score=70` silently absorbed this noise as a side effect; removing it (two entries up) made it visible as real messages. Three-part fix, all per explicit user confirmation: (1) `remoteok.py` no longer matches the bare `junior` tag; (2) `matcher.py`'s scoring prompt gained a hard domain gate — postings outside SWE/DS/ML/AI score 0-5 regardless of other factors, since not every source's fetch filter is software-scoped; (3) reintroduced `profile.yaml → matching.min_relevance_score` (default 15) in `get_unnotified_scored_postings()` — deliberately named and framed differently from the old `min_score` to avoid the misconception that it's back to being a "good personal fit" bar. It's a domain-relevance floor only; a weak-but-genuine software posting still clears it easily. `requirements.md`, `design.md`, and `README.md` updated to match. 354 tests passing, mypy/ruff clean.

**Update 2026-08-12 (co-op coverage audited and fixed across all sources, same day)**: User asked directly whether co-op postings specifically were covered. Checked each source's actual filter rather than assuming: Greenhouse/Lever and Indeed RSS always matched co-op wording; HN, RemoteOK, Adzuna, and JSearch did not — each only matched "intern"/"internship", so a co-op-only listing that never says "intern" would be silently missed on those four. Fixed all four per explicit user request ("fix all four, including Adzuna/JSearch"): `hn.py` and `remoteok.py` now also match `\bco-?op\b` (word-boundary regex on both, specifically to avoid false-matching "cooperative" — caught this risk before shipping and added test cases for it in both `tests/test_scrapers_hn.py` and `tests/test_scrapers_remoteok.py`). Adzuna now queries both `what="internship"` and `what="co-op"` every weekly poll — trivial extra cost (~8/month total vs. the 250/month quota), so no need to trade off coverage lag. JSearch alternates between `INTERN_QUERY` and `COOP_QUERY` by UTC hour (`(hour // 4) % 2` in `run_jsearch_poll`) rather than querying both every poll, since doubling would exceed its tighter 200/month-sized quota — each variant still polls roughly every 8 hours. `design.md` §5.2 also corrected a mistake from two entries up: it previously implied HN and RemoteOK loop the same `build_queries()` keyword list Indeed RSS does: they don't, and never did — each has always used its own independent fetch+filter mechanism. 362 tests passing, mypy/ruff clean.

**Update 2026-08-13 (direct apply links for JSearch; visual confirmation on Mark Applied)**: Two explicit user requests. (1) Apply links should point to the employer, not the job board — audited every source's `apply_url` field rather than assuming: Greenhouse/Lever were already direct (the ATS *is* the employer's application system), RemoteOK/HN already best-effort direct. JSearch was the real gap — `job_apply_link` is frequently a re-poster (a captured live response pointed to BeBee, `job_apply_is_direct: false`), while JSearch separately hands us `apply_options[].is_direct` and was simply never used. Added `_direct_apply_url()` in `jsearch.py`: prefers the first `is_direct: true` option, falls back to `job_apply_link` only if nothing in `apply_options` is marked direct. Indeed RSS and Adzuna have no equivalent "this one's direct" signal in their public APIs — flagged as a known limitation (would require following redirect chains per posting, a real latency/reliability trade-off for a small share of coverage) rather than silently left unaddressed.

(2) "Mark Applied" only ever produced a transient `answerCallbackQuery` toast — no lasting confirmation once you scrolled past it. Added `get_latest_notification_for_posting()` (`db.py`) to look up the original alert's `telegram_message_id`, and `edit_message_mark_applied()` (`notifier.py`, Telegram's `editMessageText`) to append "✅ Applied" to that message and remove the now-redundant button. Wired into the existing `applied:{id}` callback handler in `process_telegram_updates()`, after `mark_applied()` and before the toast. Never raises — Telegram's "message is not modified" (e.g. a near-simultaneous double-tap racing the edit) is treated as a harmless no-op, same pattern as every other outbound Telegram call in this codebase. 370 tests passing, mypy/ruff clean.

**Update 2026-08-14 (HTML-formatted messages, digest removed, faster Mark-Applied response, broader AI/data-engineer terms)**: Several explicit requests bundled together. (1) Individual and burst Telegram messages now use `parse_mode="HTML"` instead of plain text — bold company/title header, a 🟢/🟡/🔴 score-tier emoji (visual triage only, not a filter — the real floor is still `min_relevance_score`), and a clickable `Apply` link instead of a bare URL; every substituted value goes through `html.escape()` first since it's all either AI-generated or scraped third-party content. `edit_message_mark_applied()` now also passes `parse_mode="HTML"`, since the stored original message is itself HTML. (2) The hourly digest message was removed entirely per explicit request — `run_digest()` → `retry_unresolved_companies()`, which still retries unresolved companies hourly but sends nothing; `format_digest()`, `_get_period_stats()`, `_get_top_match_this_week()`, and `profile.yaml → matching.digest_enabled` were all deleted. (3) The Telegram command-poll job (`/pause`, `/resume`, "Mark Applied") changed from `IntervalTrigger(minutes=2)` to `IntervalTrigger(seconds=5)` so button taps feel close to instant — `getUpdates` is a cheap short-poll with no meaningful rate-limit risk at that cadence. (4) `RUN_INTERVAL_MINUTES` default doubled from 60 to 120 per explicit request. (5) `build_queries()` and JSearch's `INTERN_QUERY`/`COOP_QUERY` broadened beyond pure "software engineering" wording to also catch `"AI engineer"`/`"data engineer"`/`"machine learning research"` intern and co-op titles. New `tests/test_scrapers_base.py` covers the expanded term list. `docs/requirements.md` and `docs/design.md` updated to match (FR-01, FR-11, FR-20, FR-32/FR-33 marked removed, FR-35, FR-37a, NFR-03; design.md §3.4, §4.1a, §5.1, §5.2, §8.2/§8.2a, §9, §10). 369 tests passing, mypy/ruff clean.

Also caught while touching `docs/design.md`: the working copy had regressed to a stale pre-2026-08-12/08-13 version in several spots — the co-op-coverage-by-source table, the JSearch direct-apply-link bullet, the hard domain-gate description, and the Mark-Applied message-edit flow had all been silently dropped from the doc even though the underlying code (`hn.py`, `remoteok.py`, `jsearch.py`'s `_direct_apply_url()`, `matcher.py`'s domain gate, `edit_message_mark_applied()`) was all still present and unchanged. Restored those sections rather than losing previously-verified documentation. Root cause not identified — flagging in case it recurs.

**Update 2026-08-15 (companies.yaml replaced with a ~350-company list; found and fixed a per-company fetch-abort bug in Greenhouse/Lever)**: Per explicit request, replaced `config/companies.yaml` with a new user-supplied 349-company list spanning big tech, gaming, AI labs/infra, data/cloud infra, security, fintech, quant trading, semiconductors/EDA, SaaS, and aerospace/defense/robotics — organized into 22 category sections, synced to Railway via `COMPANIES_CONFIG` per the established `--sync-companies` workflow (T-1003). Verified resolution with a local `--run-once --dry-run`: 281/349 resolved; the other 68 hit SerpAPI `429 Too Many Requests` partway through (349 companies × up to 3 queries each, fired back-to-back with no client-side throttling, exceeded the plan's rate limit) — not missing companies, just deferred to the next hourly unresolved-company retry.

That same verification run surfaced a real, pre-existing bug, invisible at the old company-count scale: `greenhouse.py` and `lever.py`'s `fetch_for_resolved_companies()` loops catch `except httpx.HTTPError: continue` per company, but the actual per-company `fetch()` call is wrapped in `@retry(stop_after_attempt(3))` (tenacity) — once all 3 attempts fail, tenacity raises its own `RetryError`, not the underlying `HTTPError`, so the per-company `except` never fired. One bad slug (live-observed: a stale `spotify` Greenhouse mapping, and a `arraylabs` Lever mapping) aborted the fetch loop for every company after it in that source, for that whole cycle — meaning most of a large company list was likely never actually being checked in production. Also, the two existing tests named `..._skips_source_on_http_error` never actually caught this: they asserted `postings == []`, which passes whether or not the second company was fetched at all. Fixed by also catching `tenacity.RetryError` in both files' per-company loops, and strengthened both tests to assert the second company's `fetch()` call happens (`mock_fetch.assert_any_call(...)`) rather than just checking the aggregate return value. Added a new `..._continues_past_exhausted_retry` test per file reproducing the exact `RetryError` failure mode. 371 tests passing, mypy/ruff clean.

Separately noticed while reading the run's logs, not yet addressed: `SEARCH_API_KEY` (SerpAPI) appears in cleartext in every `httpx` request log line for company discovery (`GET https://serpapi.com/search?...&api_key=...`), unlike the deliberate chat-ID redaction already in place for Telegram logs (T-908). Worth fixing at some point — flagged to the user, not yet fixed.
