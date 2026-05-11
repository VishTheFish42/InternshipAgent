# Task List

Check off tasks as they are completed. Phases are ordered by dependency — complete earlier phases before starting later ones.

---

## Phase 0 — Project Bootstrap

- [x] **T-001** Initialize git repository and push to GitHub
- [ ] **T-002** Create Python project structure (`src/`, `src/scrapers/`, `src/scrapers/custom/`, `src/data/`, `config/`, `docs/`, `tests/`, `resumes/`)
- [ ] **T-003** Create `requirements.txt` with all pinned dependencies (see design doc §11)
- [ ] **T-004** Create `.env.example` with all required variable names and descriptions (no values)
- [x] **T-005** Add `.gitignore`: `.env`, `resumes/`, `profile.cache.json`, `config/companies.yaml`, `*.db`, `__pycache__`, `.venv`, `playwright/`
- [ ] **T-006** Set up `pyproject.toml` with `mypy`, `pytest`, and `ruff` config
- [ ] **T-007** Set up GitHub Actions CI: run `mypy`, `ruff`, and `pytest` on every push to main
- [ ] **T-008** Create `profile.yaml` with default preferences (empty `keywords_excluded`, `min_score: 70`, `weekly_digest: true`)
- [ ] **T-009** Create `config/companies.example.yaml` (committed format reference); verify `config/companies.yaml` (pre-populated with ~150 companies) is in `.gitignore` and not committed

---

## Phase 1 — Core Infrastructure

- [ ] **T-101** Implement `src/config.py`: load and validate all env vars using `pydantic-settings`; raise descriptive errors on missing required vars; include `BURST_THRESHOLD` (default 5) and `PROFILE_CACHE` (base64 profile, optional — falls back to local `profile.cache.json` for local dev)
- [ ] **T-102** Implement `src/db.py`: SQLAlchemy models for `job_postings`, `company_lookup`, `notifications`, `run_log`; `init_db()` to create schema
- [ ] **T-103** Write Alembic migration baseline from current schema
- [ ] **T-104** Implement `src/db.py` helpers: `upsert_posting()`, `get_unscored_postings()`, `get_unnotified_above_threshold()`, `mark_notified()`, `log_run()`, `get_unresolved_companies()`, `get_stats()`
- [ ] **T-105** Write unit tests for all DB helpers using in-memory SQLite
- [ ] **T-106** Implement `python -m src.db init` and `python -m src.db stats` subcommands; `stats` must show total postings, alerts sent, unresolved companies (with names), and cumulative estimated API cost

---

## Phase 2 — Resume Ingestion

- [ ] **T-201** Implement `src/resume_extractor.py`: `extract_from_pdf(path: Path) -> ExtractedProfile` using `pdfplumber` for text extraction and Claude Sonnet for structured parsing; return typed `ExtractedProfile` dataclass
- [ ] **T-202** Design and implement the Claude extraction prompt: ask for languages, frameworks, tools, experience level, graduation year, projects (name + description + technologies), and work experience; return JSON
- [ ] **T-203** Implement `merge_profiles(profiles: list[ExtractedProfile]) -> MergedProfile`: union of all skills/languages/tools with case-insensitive deduplication; longest project description wins on conflict; most recent file's experience level and graduation year wins
- [ ] **T-204** Implement `src/resume_extractor.py`: `rebuild_profile(resumes_dir: Path) -> MergedProfile`; hash all PDFs; write result + hash to `profile.cache.json` locally; print `railway variables set PROFILE_CACHE="<base64>"` command to stdout
- [ ] **T-205** Implement `src/resume_extractor.py`: `detect_resume_changes(resumes_dir: Path, cache: MergedProfile) -> bool`; compare current PDF hash to `_source_hash` in cache
- [ ] **T-206** Implement the `--rebuild-profile` CLI flag in `src/main.py` that calls `rebuild_profile()` and exits
- [ ] **T-207** Write unit tests for merge logic: test deduplication, conflict resolution, and edge cases (empty resumes dir, single PDF, 3 PDFs with overlapping projects)
- [ ] **T-208** Write integration test: feed a sample PDF resume; verify `profile.cache.json` is produced with expected fields

---

## Phase 3 — Company Discovery

- [ ] **T-301** Build `src/data/company_ats_map.json`: initial mapping of ~500 well-known tech companies to ATS type + slug, sourced from SimplifyJobs and other open-source internship trackers
- [ ] **T-302** Implement `src/company_discoverer.py`: `discover(company_name: str) -> CompanyRecord | None` following the 4-step pipeline (bundled table → Greenhouse web search → Lever web search → generic careers search → unresolved)
- [ ] **T-303** Implement web search step using SerpAPI or Google Custom Search; extract ATS slug from first matching URL using regex
- [ ] **T-304** Implement DB caching for resolved companies: never re-query a company that has already been resolved; store resolution source (`bundled_table`, `web_search`, `manual`)
- [ ] **T-305** Implement weekly re-attempt for unresolved companies (in the weekly digest job)
- [ ] **T-306** Write unit tests for company discoverer: mock web search; test each discovery step in isolation; test caching behavior

---

## Phase 4 — Data Ingestion (Scrapers)

### 4A — Scraper interface

- [ ] **T-401** Define `src/scrapers/base.py`: `BaseScraper` abstract class with `async def fetch(queries: list[str]) -> list[RawPosting]`; define `RawPosting` dataclass (source, external_id, title, company, location, is_remote, url, apply_url, description, posted_at)
- [ ] **T-402** Implement keyword query builder in `src/scrapers/base.py`: generates the full list of domain keyword queries including co-op variants (software engineering intern, software engineering co-op, data science intern, data science co-op, ML intern, ML co-op, AI intern, backend intern, frontend intern, full stack intern)

### 4B — Indeed RSS (free, primary broad source)

- [ ] **T-411** Implement `src/scrapers/indeed_rss.py`: fetch Indeed RSS feeds for each keyword query using `feedparser`; URL format `https://www.indeed.com/rss?q={query}&l=United+States&jt=internship&sort=date`; parse into `RawPosting` list
- [ ] **T-412** Extract job ID from RSS `<guid>` field; set dedup key `indeed:{job_id}`; set apply_url to the cleaned link (strip tracking params)
- [ ] **T-413** Rate-limit to 1 req/2s between queries; add descriptive `User-Agent` header
- [ ] **T-414** Add retry logic (3 attempts, exponential back-off) via `tenacity`
- [ ] **T-415** Write integration test: fetch one query, verify at least one `RawPosting` is returned with all required fields

### 4C — Adzuna

- [ ] **T-421** Create an Adzuna developer account; obtain `app_id` and `app_key`
- [ ] **T-422** Implement `src/scrapers/adzuna.py`: run keyword queries; filter by `country=us`, sort by `date`
- [ ] **T-423** Write integration test for Adzuna

### 4D — Wellfound / AngelList (startup coverage)

- [ ] **T-431** Create a Wellfound developer account; obtain API key
- [ ] **T-432** Implement `src/scrapers/wellfound.py`: query Wellfound for internship roles in CS/SWE/ML/AI; filter by US location; parse into `RawPosting` list
- [ ] **T-433** Write integration test for Wellfound scraper

### 4E — Y Combinator Work at a Startup

- [ ] **T-434** Implement `src/scrapers/yc.py`: POST to `https://www.workatastartup.com/company_filters/search_startup_jobs` with role_type=intern; no auth required; parse JSON into `RawPosting` list
- [ ] **T-435** Filter results by US location and description containing SWE/ML/AI/DS keywords
- [ ] **T-436** Write integration test for YC scraper

### 4F — HackerNews "Who's Hiring"

- [ ] **T-437** Implement `src/scrapers/hn.py`: search Algolia HN API for the current month's "Ask HN: Who is hiring?" thread (search by title, not fixed date — thread sometimes posts late); fetch all comments containing "intern"; extract company name and URL via regex
- [ ] **T-438** Use Claude to parse ambiguous/unstructured HN comments into structured `RawPosting` objects; store `hn:{comment_id}` as dedup key; re-poll daily for new comments added to an existing thread
- [ ] **T-439** Write unit test with mock Algolia API response

### 4G — RemoteOK

- [ ] **T-450** Implement `src/scrapers/remoteok.py`: fetch `https://remoteok.com/api`; filter by tags containing "intern" or "junior" plus SWE/ML/AI/DS tags; no auth required
- [ ] **T-451** Write integration test for RemoteOK scraper

### 4H — Dice

- [ ] **T-452** Implement `src/scrapers/dice.py`: Dice job search API or RSS feed; filter by `employment_type=INTERN` and US location; run same keyword queries
- [ ] **T-453** Write integration test for Dice scraper

### 4I — Cross-Source Deduplication

- [ ] **T-454** Implement `src/deduplicator.py`: `normalize_url(url: str) -> str` that strips UTM params, tracking tokens, and trailing slashes
- [ ] **T-455** Implement secondary dedup in `upsert_posting()`: before insert, query `apply_url_normalized` index; skip insert and log if a match exists from a different source
- [ ] **T-456** Implement tertiary (fuzzy) dedup: normalize company name and title; query DB for same `(company_normalized, title_normalized)` within last 7 days; skip if descriptions are >80% similar
- [ ] **T-457** Write unit tests: verify a Stripe posting found via both Indeed RSS and Greenhouse results in one DB row and one SMS; verify two legitimately different roles at the same company are both kept

### 4J — Greenhouse ATS (Tier 1)

- [ ] **T-461** Implement `src/scrapers/greenhouse.py`: for each resolved Greenhouse company from DB, call `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs`; filter titles containing "intern" or "co-op" case-insensitively
- [ ] **T-462** Respect 1 req/5s rate limit; add descriptive `User-Agent` header
- [ ] **T-463** Write integration test using Stripe's public Greenhouse board as a fixture

### 4K — Lever ATS (Tier 1)

- [ ] **T-464** Implement `src/scrapers/lever.py`: for each resolved Lever company from DB, call `https://api.lever.co/v0/postings/{slug}?mode=json`; filter by title containing "intern" or "co-op"
- [ ] **T-465** Write integration test for Lever

### 4L — Custom scrapers (Tier 1, major companies not on standard ATS)

- [ ] **T-466** Install Playwright; implement `src/scrapers/custom/base_custom.py` with robots.txt checking, minimum 3s page delay, and Playwright page loading
- [ ] **T-467** Implement `src/scrapers/custom/google.py` for Google Careers internship listings
- [ ] **T-468** Implement `src/scrapers/custom/meta.py` for Meta Careers
- [ ] **T-469** Implement `src/scrapers/custom/microsoft.py` for Microsoft Careers

---

## Phase 5 — AI Scoring

- [ ] **T-501** Implement `src/matcher.py`: `score_postings(postings: list[RawPosting], profile: MergedProfile, prefs: Preferences) -> list[ScoredPosting]`
- [ ] **T-502** Design scoring system prompt: include full profile summary; define the 4-factor rubric (skills alignment 40%, domain relevance 30%, seniority fit 20%, role accessibility 10%); instruct Claude to return JSON array with `external_id`, `score`, `reasoning`
- [ ] **T-503** Implement batching: group postings into lists of 10; call Claude once per batch
- [ ] **T-504** Implement prompt caching on the system prompt using Anthropic `cache_control` headers
- [ ] **T-505** Implement profile change detection: compare `profile.cache.json` hash to the hash stored in the last `run_log`; if changed, queue all `notified=FALSE` postings for re-scoring
- [ ] **T-506** Log estimated Claude API cost per run using token counts from the API response
- [ ] **T-507** Write unit tests for matcher: mock `anthropic.Anthropic`; verify batching logic, JSON parsing, and score boundary handling
- [ ] **T-508** Write end-to-end test: 3 sample postings (one strong match, one weak, one irrelevant); verify output scores and reasoning

---

## Phase 6 — Notification System

- [ ] **T-601** Create a Twilio account; obtain account SID, auth token, and a phone number
- [ ] **T-602** Implement `src/notifier.py`: `notify_matches(matches: list[ScoredPosting], threshold: int) -> None`; routes to individual or burst mode based on `BURST_THRESHOLD`
- [ ] **T-603** Implement individual SMS formatter: target ≤480 chars; always include full URL; truncate reasoning if needed
- [ ] **T-603a** Implement burst SMS formatter: list all matches ranked by score with company, title, score; include count of additional matches beyond what fits; always end with "run db stats for full list"
- [ ] **T-604** Implement weekly digest SMS formatter: include stats (postings found, alerts sent), top match, and list of unresolved companies
- [ ] **T-605** Implement delivery status polling: check Twilio message status 2 minutes after send; log result
- [ ] **T-606** Implement retry: if send fails, retry once after 5 minutes; log permanent failure
- [ ] **T-607** Redact phone number to last 4 digits in all log output
- [ ] **T-608** Write unit tests for notifier: mock Twilio client; verify message format for short and long descriptions; verify phone redaction in logs
- [ ] **T-609** Manually test end-to-end: trigger a `--dry-run`, then a real alert to your phone; verify content and format

---

## Phase 7 — Orchestration

- [ ] **T-701** Implement `src/main.py`: CLI arg parsing (`--run-once`, `--dry-run`, `--rebuild-profile`, `--rescore`); startup validation
- [ ] **T-702** Implement `run_cycle()`: full pipeline — fetch (parallel) → deduplicate → score → notify → log
- [ ] **T-703** Configure APScheduler: `IntervalTrigger` for main cycle; `CronTrigger` for weekly digest
- [ ] **T-704** Implement graceful shutdown on `SIGTERM`/`SIGINT`: let current cycle finish, then exit cleanly
- [ ] **T-705** Implement `--rescore` mode: re-score all `notified=FALSE` postings against current profile; send new alerts
- [ ] **T-706** Implement startup resume-change detection: warn in logs if resume files differ from cache hash
- [ ] **T-707** Write structured JSON logs for every run lifecycle event using `structlog`

---

## Phase 8 — Cloud Deployment

- [ ] **T-801** Create `Procfile`: `worker: python -m src.main`
- [ ] **T-802** Create `railway.toml` with nixpacks build config and restart policy
- [ ] **T-803** Create Railway account; link to GitHub repository
- [ ] **T-804** Set all environment variables in Railway dashboard (verify `.env` is not committed)
- [ ] **T-805** Deploy to Railway; verify first run completes and logs look correct
- [ ] **T-806** Verify an SMS is received on your phone after the first live run
- [ ] **T-807** Set up Railway persistent volume for SQLite (or switch to Railway Postgres plugin)
- [ ] **T-808** Confirm Railway restarts the worker automatically after a crash
- [ ] **T-809** Test the `--rebuild-profile` → commit → push → Railway redeploy workflow end-to-end

---

## Phase 9 — Testing & Hardening

- [ ] **T-901** Achieve >80% unit test coverage across `src/` (`pytest-cov`)
- [ ] **T-902** Test failure isolation: simulate JSearch API failure; verify other sources still complete
- [ ] **T-903** Test deduplication: run the agent twice back-to-back; verify no duplicate SMS sent
- [ ] **T-909** Test cross-source dedup: mock a posting returned by both JSearch and Greenhouse with the same apply_url; verify only one SMS is sent
- [ ] **T-904** Test profile change flow: run `--rebuild-profile` with a modified resume; verify re-scoring triggers on next run
- [ ] **T-905** Test `--dry-run`: verify no SMS sent but all other pipeline stages complete
- [ ] **T-906** Test company discovery: add an unknown company; verify it appears in `db stats` as unresolved
- [ ] **T-907** Add nightly GitHub Actions integration test job using secrets (runs against live APIs)
- [ ] **T-908** Review all log output to confirm no secrets or full phone numbers appear

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
