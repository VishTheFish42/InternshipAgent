# Requirements

## 1. Functional Requirements

### 1.1 Job Discovery

| ID | Requirement |
|---|---|
| FR-01 | The system SHALL search using broad domain-level keyword queries — not exact job titles. Queries SHALL cover the domains: software engineering, data science, machine learning, and AI — including title variants beyond pure "software engineering" wording (e.g., `"AI engineer intern"`, `"data engineer intern"`, `"machine learning research intern"`) — and SHALL include co-op variants for each (e.g., `"software engineering co-op"`, `"data science co-op"`, `"AI engineer co-op"`, `"data engineer co-op"`). The system SHALL NOT filter by internship term (summer/fall/spring/co-op) at the search layer — all terms are included and term context is surfaced by the AI scorer. |
| FR-02 | The system SHALL poll the following Tier 2 sources, wired into the poll schedule: Indeed (via public RSS feeds), HackerNews "Who's Hiring", and RemoteOK on the main cycle; Adzuna on its own weekly job. For LinkedIn coverage specifically — not reachable via any free source — the system SHALL support JSearch (RapidAPI) as a metered aggregator, active when `JSEARCH_API_KEY` is configured, polled on its own job at a cadence sized to the configured plan's quota. Wellfound (AngelList), Y Combinator Work at a Startup, and Dice are **not currently implemented**: Wellfound has no scraper at all; YC and Dice scrapers exist in `src/scrapers/` but are not wired into any scheduled job. Planned for a later phase. None of these fetch-layer filters are software-scoped by themselves — RemoteOK matches on generic job-board tags (`intern`/`internship` only, after a fix that removed a bare `junior` tag match pulling in non-software roles), and HN matches the word "intern" anywhere in a hiring-thread comment. Domain relevance is enforced downstream by the AI scorer (FR-26, FR-29), not by these sources. |
| FR-03 | The system SHALL also monitor individual company career pages directly, based on company names listed in `config/companies.yaml`. |
| FR-04 | The system SHALL filter all results to United States locations (on-site, hybrid, or remote). |
| FR-05 | The system SHALL exclude postings containing any keyword listed in `profile.yaml → preferences.keywords_excluded`. This filter is applied before AI scoring to avoid unnecessary API calls. |
| FR-06 | The system SHALL support adding new data sources by implementing a common scraper interface without modifying core orchestration logic. |

### 1.2 Company Discovery

| ID | Requirement |
|---|---|
| FR-07 | The user SHALL only need to provide company names in `config/companies.yaml` (e.g., `- Stripe`). The system SHALL automatically determine the company's career page and ATS type without requiring the user to supply URLs or slugs. `config/companies.yaml` SHALL be gitignored so the user's target list is never committed to the repository. `config/companies.example.yaml` SHALL be committed as a format reference. |
| FR-08 | The company discovery pipeline SHALL resolve company names to career pages by checking, in order: (1) a bundled lookup table of known companies, (2) a web search for the company's Greenhouse or Lever board, (3) a web search for the company's generic careers page. |
| FR-09 | Resolved company → ATS mappings SHALL be cached in the database so that web search is only performed once per company. |
| FR-10 | If a company cannot be resolved after all discovery steps, the system SHALL mark it as `unresolved` in the database and log a warning. It SHALL NOT send an error message for each individual unresolved company. |
| FR-11 | Unresolved companies SHALL be surfaced to the user via the `python -m src.db stats` command (count + names). (There used to also be an hourly digest message repeating this — removed per FR-32/FR-33, below.) |
| FR-12 | The system SHALL re-attempt discovery for unresolved companies once per week in case the company has since added a public career page. |

### 1.3 Resume-Based Profile

| ID | Requirement |
|---|---|
| FR-13 | The system SHALL accept one or more PDF resume files placed in the `/resumes` directory as the sole source of the user's qualifications. The user SHALL NOT need to manually enter skills, languages, tools, or projects anywhere. |
| FR-14 | The system SHALL extract structured profile data from all resumes using Claude, including: programming languages, frameworks, tools, project descriptions, experience level, and graduation year. |
| FR-15 | When multiple resumes are provided, the system SHALL merge all extracted data into a single unified profile, deduplicating repeated skills, languages, and tools across resume versions. |
| FR-16 | The merged profile SHALL be written to `profile.cache.json` locally (auto-generated, not user-edited). This file SHALL be gitignored — the repository is public and the file contains personal skills and project data. |
| FR-16a | The cloud-deployed agent SHALL read the profile from a `PROFILE_CACHE` environment variable (base64-encoded JSON). Running `--rebuild-profile` SHALL print the Railway CLI command needed to update this variable. |
| FR-17 | Both resume PDFs and `profile.cache.json` SHALL be gitignored and never committed to the repository. |
| FR-18 | The system SHALL detect when any file in `/resumes` has been added or modified (via SHA-256 hash comparison) and log a prominent warning prompting the user to run `--rebuild-profile`. |
| FR-19 | Running `python -m src.main --rebuild-profile` SHALL re-extract and re-merge all resumes, overwrite `profile.cache.json`, and trigger re-scoring of all stored postings against the updated profile on the next run. |
| FR-20 | `profile.yaml` SHALL contain only user preferences — target locations, excluded keywords, posting freshness window. It SHALL NOT contain skills or experience. |

### 1.4 Deduplication

| ID | Requirement |
|---|---|
| FR-21 | The system SHALL assign a stable unique identifier to each posting based on source + external job ID. |
| FR-22 | The system SHALL never send more than one notification for the same posting. |
| FR-23 | Seen postings SHALL be persisted in the database across restarts. |
| FR-24 | The system SHALL store full posting metadata for all postings regardless of match score, enabling future re-scoring without re-fetching. |

### 1.5 AI Scoring

| ID | Requirement |
|---|---|
| FR-25 | The system SHALL score each new posting 0–100 against `profile.cache.json` using Claude. |
| FR-26 | The system SHALL send an alert for every scored posting at or above `profile.yaml → matching.min_relevance_score` (default 15) — a domain-relevance floor, not a fit-quality gate. Every genuine SWE/DS/ML/AI posting SHALL be alerted regardless of how weak the personal fit is; the floor exists only to cut postings the AI scorer identifies as outside those domains entirely (some sources aren't software-scoped at the fetch layer — see FR-02's RemoteOK note). Each alert SHALL include the score and Claude's identified `missing_qualifications` so the user can judge fit themselves. |
| FR-27 | The match score and a brief Claude reasoning summary SHALL be stored in the database for every scored posting. |
| FR-28 | When `profile.cache.json` changes (new hash), the system SHALL re-score all previously stored postings where `notified = FALSE` and send new alerts for those that now qualify. |
| FR-29 | The scoring prompt SHALL instruct Claude to evaluate holistic relevance across all four target domains (SWE, DS, ML, AI) — not just title keyword matching. It SHALL also apply a hard domain gate: postings outside SWE/DS/ML/AI entirely SHALL score 0-5 regardless of any other factor, since not every source's fetch-layer filter is software-scoped (FR-02). |
| FR-29a | For every scored posting, Claude SHALL identify `missing_qualifications`: specific skills, tools, or requirements named in the posting that the candidate's profile does not show. An empty list SHALL indicate no gaps (or the posting isn't a plausible fit at all). |

### 1.6 Notifications

| ID | Requirement |
|---|---|
| FR-30 | The system SHALL send a Telegram message to the configured chat for every scored posting at or above the domain-relevance floor (FR-26) — there is no personal-fit match threshold. |
| FR-30a | If the number of scored postings in a single polling cycle is below `BURST_THRESHOLD` (default: 20), the system SHALL send one individual message per posting, each containing a direct application link, paced to respect Telegram's per-chat rate limit. If at or above the threshold, it SHALL send a single batched summary message listing all postings ranked by score (each with its own application link), with a note to check `db stats` for details. |
| FR-31a | Every individual and batched message SHALL contain a direct application URL per posting. |
| FR-31 | Each individual message SHALL contain: company name, role title, location/remote status, match score, missing qualifications, the source board the posting was found on, and direct application URL. |
| FR-32 | **Removed.** Previously: the system sent a digest message on a configurable interval (default: every hour) containing new-postings/alerts-sent counts and unresolved company names. Removed per explicit request — it mostly repeated information already visible in the individual real-time alerts (FR-30, FR-31). Unresolved companies are still retried hourly in the background (FR-12) and remain visible via `db stats` (FR-11). |
| FR-33 | **Removed** along with FR-32 — there is no longer a digest message, so the "in addition to real-time alerts" distinction no longer applies. |
| FR-34 | All sent notifications SHALL be logged with timestamp and Telegram message ID. |
| FR-35a | The system SHALL exclude postings older than `profile.yaml → matching.max_posting_age_days` (default 7) from being stored, scored, or notified, based on `posted_at`. Postings with no reliable `posted_at` SHALL NOT be excluded on this basis. |
| FR-36a | Every individual-mode alert SHALL include an inline "Mark Applied" button. Tapping it SHALL mark the posting as applied and permanently exclude it from all future notification, regardless of subsequent rescoring. |
| FR-37a | The system SHALL support `/pause` and `/resume` Telegram commands that toggle a master notification switch, independent of Telegram's own per-chat mute. While paused, the fetch/dedupe/score pipeline SHALL continue running normally; matching postings SHALL remain unnotified until `/resume` is issued, at which point they SHALL be sent normally on the next cycle — no matches SHALL be silently dropped due to a pause. |
| FR-37b | Inbound Telegram commands and button taps SHALL only be honored if they originate from the configured `TELEGRAM_CHAT_ID`; updates from any other chat SHALL be silently ignored. |

### 1.7 Scheduling & CLI

| ID | Requirement |
|---|---|
| FR-35 | The system SHALL poll all sources on a configurable interval (default: 120 minutes). |
| FR-36 | `--run-once`: execute a single full cycle and exit. |
| FR-37 | `--dry-run`: full pipeline without sending any notification. |
| FR-38 | `--rebuild-profile`: re-extract profile from all resume PDFs and exit. |
| FR-39 | `--rescore`: re-score all stored postings against current profile; send new alerts for newly qualifying ones. |
| FR-40 | `python -m src.db stats`: print summary of total postings found/scored/alerted, unresolved companies (with names), and estimated API cost to date. |
| FR-41 | The system SHALL emit a structured JSON log line for every run summarizing: sources polled, postings found, postings new, postings scored, alerts sent, errors, and estimated cost. |

### 1.8 Multi-Tenant Accounts & Auth — Planned (v2)

**Not currently implemented.** The system today (FR-01–FR-41) is a single-user personal tool: one Telegram chat ID, one profile, one `companies.yaml`, all configured via local files/env vars on one deployment. The requirements below (FR-50 onward) describe a planned pivot to a shared multi-user product, decided through user brainstorming on 2026-08-18. They supersede nothing in FR-01–FR-41 yet — v1 keeps running as-is until this is built.

| ID | Requirement |
|---|---|
| FR-50 | The system SHALL support multiple independent user accounts on one shared deployment, each with fully isolated data: profile, resumes, company watchlist, posting match/notification state, and notification preferences. |
| FR-51 | Authentication SHALL be handled by a managed third-party auth provider (e.g. Clerk, Auth0, or Supabase Auth) rather than custom-built password/session handling, supporting at minimum email+password and Google OAuth sign-in. |
| FR-52 | Every user-scoped database table SHALL be keyed by a `user_id` foreign key. No query path SHALL be able to return one user's data to another user. |

### 1.9 Resume Management — Planned (v2)

| ID | Requirement |
|---|---|
| FR-53 | Users SHALL upload one or more resume PDFs through the web app (not a local `/resumes` directory). Uploaded resumes SHALL be stored per-user in cloud object storage, isolated from other users' files. |
| FR-54 | Users SHALL be able to delete any previously uploaded resume version. |
| FR-55 | Users SHALL be able to mark each uploaded resume version as active or inactive. Only resumes marked active SHALL be merged into that user's scoring profile (the per-user equivalent of today's `profile.cache.json`, FR-16). An inactive resume stays on file but does not affect scoring. |
| FR-56 | Adding a resume, deleting a resume, or toggling a resume's active state SHALL trigger the same re-extract/re-merge/re-score pipeline that `--rebuild-profile` triggers today (FR-19), scoped to that user only — other users' profiles and re-scoring are unaffected. |

### 1.10 Per-User Company Watchlist — Planned (v2)

| ID | Requirement |
|---|---|
| FR-57 | Each user SHALL maintain their own company watchlist through the web app — the per-user equivalent of today's `config/companies.yaml` (FR-07), editable via add/remove in the UI rather than a YAML file. |
| FR-58 | Every new user account SHALL start with an empty watchlist. No user's personal watchlist (including the developer's own) SHALL be used as a seed or default for any other user's account. |
| FR-59 | Company → ATS resolution (FR-08, FR-09) SHALL remain a single shared, app-wide cache keyed by normalized company name, reused across every user watching that company — this is impersonal metadata (which ATS a company uses), not personal data, so sharing it introduces no cross-user data leak. |

### 1.11 Notification Channels — Planned (v2)

| ID | Requirement |
|---|---|
| FR-60 | Email SHALL be the default notification channel for every account, sent to the address used at signup, with no additional setup required. |
| FR-61 | Users MAY additionally link a Telegram chat to their account, via a one-time deep-link/bot-start flow, to also receive the existing richer interactive Telegram experience (individual/burst alerts, the "Mark Applied" button, `/pause`/`/resume`) on top of email. Telegram SHALL remain fully optional, never required to use the product. |
| FR-62 | A user who has not linked Telegram SHALL still receive every notification via email, using a non-interactive equivalent of the individual/burst message formats (FR-30a, FR-31) — e.g. a "Mark Applied" link to an authenticated endpoint in place of Telegram's inline button. |

### 1.12 Shared Fetch / Per-User Score Pipeline — Planned (v2)

| ID | Requirement |
|---|---|
| FR-63 | Tier 2 job-board sources (Indeed, JSearch, Adzuna, HN, RemoteOK) SHALL be fetched once app-wide on the shared schedule, not once per user, and stored in a shared postings pool — these sources return the same public postings regardless of which user is asking. |
| FR-64 | Each Tier 1 company SHALL be fetched at most once per polling cycle app-wide if it appears on at least one active user's watchlist (union across all users), not once per user watching it. |
| FR-65 | Every posting reachable by a given user (the full Tier 2 pool, plus their own Tier 1 watchlist companies) SHALL be scored independently against that user's own profile — the same posting can carry a different score, reasoning, and `missing_qualifications` per user. This is the main driver of Claude API cost scaling with user count (see FR-67). |

### 1.13 Deployment Scale & Billing — Planned (v2)

| ID | Requirement |
|---|---|
| FR-66 | The scheduler for the initial multi-tenant beta SHALL remain a simple in-process loop over active users (no distributed job queue), sized for a small beta — dozens of users, not hundreds or more. Revisit if/when scale exceeds that. |
| FR-67 | **Deferred — explicitly the last thing to build.** Billing/subscription to cover per-user Claude API costs at scale is out of scope for the initial multi-tenant build. Per explicit user decision (2026-08-18): the beta SHALL run at the developer's own cost/testing budget with no payment collection until every other multi-tenant requirement in §1.8–§1.12 is implemented and working. |

### 1.14 Company Scraper Coverage Guarantee — Implemented (FR-72 pending the v2 web UI)

Root-caused from a real missed posting: an Nvidia SWE internship posted 2026-08-19 never generated a Telegram alert because Nvidia resolved to `ats_type="custom"` (FR-08) with no scraper behind it — a state the existing discovery pipeline treats identically to a fully-working one. The same dead end affects 26 companies in a real `companies.yaml`, including Google, Meta, Microsoft, Apple, Amazon, Netflix, Tesla, Intel, and AMD. This applies to the current single-user tool, not only the planned multi-tenant product (§1.8–§1.13) — but it is the load-bearing requirement for the "add any company you care about" pitch of the general product, since a company a stranger adds is exactly as likely to land in this dead end as Nvidia did. FR-68–FR-71 and FR-73 are now all implemented (`docs/design.md` §3.3/§3.5, `docs/tasks.md` T-308/T-309/T-310/T-311/T-312/T-470–T-475) — closing the coverage gap for genuinely any company, not just ones on a known ATS, and (FR-73) making sure a company the user actually cares about isn't starved of the scarce search-API quota by the rest of the watchlist. FR-72 remains not implemented (depends on the not-yet-built v2 web UI).

| ID | Requirement |
|---|---|
| FR-68 | **Implemented** (T-309, T-310, 2026-08-20). Company discovery SHALL NOT mark a company `resolved` (FR-08, FR-10) unless its resolved `ats_type` maps to a registered, working scraper. `ats_type="custom"` (resolved, unscraped) SHALL be retired in favor of `ats_type="llm_scrape"` (FR-70), which always has a scraper behind it. `discover()`'s generic-careers-page fallback now resolves to `ats_type="llm_scrape"` for newly-discovered companies (T-309). Bundled `company_ats_map.json` entries already stuck at a legacy or incomplete value (Google, Meta, Microsoft, Apple, Amazon, and ~20 more) now self-heal via the existing hourly retry job (T-310) — discovery's bundled-table step no longer trusts an entry unless it's genuinely usable (a live `ats_type` *and* the identifier its scraper needs), and `retry_unresolved()` was broadened to target these `status="resolved"`-but-stuck rows, not just genuinely `unresolved` ones. |
| FR-69 | **Implemented** (`docs/tasks.md` T-470/T-471/T-308, 2026-08-20). The system includes a generic Workday scraper (`src/scrapers/workday.py`, `ats_type="workday"`), driven by a `"{tenant}.{instance}/{site}"` slug, using Workday's uniform per-tenant JSON job-search API (`POST /wday/cxs/{tenant}/{site}/jobs`) — requiring no per-company code, the same principle as the existing Greenhouse and Lever scrapers. Live-verified against Nvidia, Intel, Adobe, and Salesforce's real Workday boards, including pulling Nvidia's actual "Software Engineering Intern, NCCL" posting (posted 2026-08-18) that motivated this section. Automated discovery of new Workday tenants — resolving companies not already in the bundled table, FR-08's step-3 extension (`docs/design.md` §3.1) — is also implemented (T-308); confirmed live it correctly resolves *only* companies genuinely hosted on Workday (Google/Meta/Microsoft/Apple/Amazon/Netflix/Tesla/Uber are not, verified) and correctly falls through to the still-unbuilt FR-70 path for everything else. |
| FR-70 | **Implemented** (`docs/tasks.md` T-472/T-474, 2026-08-20). For a company that does not resolve to a known JSON-API ATS, the system fetches its careers page (`src/scrapers/llm_scrape.py`) and uses Claude to extract structured postings (title, url, location, posted date where available) from the page content (`ats_type="llm_scrape"`). HTML is converted to Markdown-style linked text (a stdlib `html.parser.HTMLParser`, not a new dependency) so extracted postings keep their real URL, not just visible text. Live-verified against real, unfavorable pages (not just mocked tests) rather than assumed safe: fed jobs.netflix.com's JS-rendered SPA shell, a hidden non-`<script>` data blob on a second Netflix domain, and a Goldman Sachs marketing overview page with no actual listings — all three correctly returned zero fabricated postings rather than hallucinating fake ones, confirming the critical safety property (graceful failure, never invented data) holds under real adversarial conditions, not just the happy path. |
| FR-71 | **Implemented** (T-473). `llm_scrape`-tier companies are polled on their own 6-hourly job (`run_llm_scrape_poll` in `src/main.py`), separate from the main cycle — the same cost-bounding pattern already used for Adzuna and JSearch (§10.1) — since each poll costs a real page fetch and LLM call rather than a free JSON request. |
| FR-72 | **Planned (v2), depends on §1.10's web UI.** A company on a user's watchlist that resolves to the `llm_scrape` tier, or remains `unresolved`, SHALL be visibly indicated as such in the web app (e.g. "covered, best-effort" / "not yet covered, retrying") rather than presented identically to a fully-covered Tier A company — so a user can tell the difference between a company they can trust is fully monitored and one that isn't yet, instead of discovering the gap only after a posting is missed. |
| FR-73 | **Implemented** (T-312, 2026-08-20). Users SHALL be able to designate a subset of their watchlist as higher-priority (`companies.yaml`'s optional `priority_companies:` list). Unresolved or not-yet-usable companies in that subset SHALL be retried on a shorter cooldown than the rest of the watchlist, so that a company the user explicitly cares about gets first claim on a scarce, shared search-API quota (NFR-23) rather than being retried on the same schedule as, and potentially starved by, a much larger general backlog. |

---

## 2. Non-Functional Requirements

### 2.1 Reliability

| ID | Requirement |
|---|---|
| NFR-01 | A failure in one data source SHALL NOT prevent other sources from completing in the same run. |
| NFR-02 | Failed API calls SHALL be retried up to 3 times with exponential back-off before marking the source as failed for that run. |
| NFR-03 | **Not currently implemented.** Previously specified surfacing a source in the next digest message after 3+ consecutive failures; the digest message itself was removed (FR-32). Repeated source failures are currently only visible via application logs, not pushed to the user — and until NFR-23 (2026-08-20), SerpAPI failures specifically weren't even visible in logs, since `_serpapi()` swallowed every exception silently. |
| NFR-04 | The system SHALL resume correctly after process restart without sending duplicate notifications. |
| NFR-21 | **Implemented** (T-910, `tests/test_coverage_guarantee.py`). A unit test asserts that every `ats_type` value company discovery can produce has a corresponding registered scraper in the fetch dispatch table, so a company can never again be marked `resolved` with no scraper behind it — the regression guard for the class of bug that caused the missed Nvidia posting (§1.14). Found a real gap on its first run: Oracle's bundled entry, `{"ats": "taleo"}`, a third unscraped legacy value distinct from `custom`. |

### 2.2 Performance

| ID | Requirement |
|---|---|
| NFR-05 | A full polling cycle SHALL complete within 5 minutes under normal conditions. |
| NFR-06 | Resume extraction (`--rebuild-profile`) SHALL complete within 60 seconds for up to 5 PDFs. |
| NFR-07 | The system SHALL batch-score postings in groups of up to 10 per Claude call to reduce API overhead. |
| NFR-08 | The system SHALL not exceed 100 Claude API calls per polling cycle. |

### 2.3 Cost

| ID | Requirement |
|---|---|
| NFR-09 | Monthly operating cost SHALL be under $10 USD (hosting + APIs; Telegram is free) at steady state. |
| NFR-10 | The system SHALL log estimated Claude API cost per run based on token counts in the API response. |
| NFR-11 | The profile system prompt SHALL use Anthropic prompt caching to reduce repeat token costs on every scoring call. |
| NFR-22 | **Implemented** (T-475). Claude calls made by the `llm_scrape` tier's page-extraction step are included in the per-run cost estimate — `run_llm_scrape_poll` writes its own `RunLog` row with a real `estimated_cost_usd`, which `get_stats()`'s existing cumulative sum (NFR-10) picks up automatically. Companies-per-poll is hard-capped at 100 (`_MAX_COMPANIES_PER_POLL`), enforcing the same ceiling NFR-08 sets for the main cycle's scoring calls. |
| NFR-23 | **Implemented** (T-311, 2026-08-20). Every SerpAPI request SHALL be rate-limited to a configurable minimum interval (default 1/second) regardless of which code path issues it — `seed_companies()`'s and `retry_unresolved()`'s loops over companies, and `discover()`'s own internal Greenhouse/Lever/Workday/careers cascade, all funnel through one centralized limiter in `_serpapi()`. Every SerpAPI failure SHALL be logged, including the HTTP status code and response body when available — previously a bare `except Exception: return []` made a full SerpAPI outage or exhausted quota completely invisible in application logs (found live, not hypothetical: this was the actual cause of the coverage gap investigated 2026-08-20). `retry_unresolved_companies()`'s hourly summary log escalates from INFO to WARNING specifically when a non-trivial batch resolves nothing at all, the signature shape of a sustained outage. |
| NFR-24 | **Implemented.** Company discovery SHALL minimize actual demand on the (fixed, free-tier) search API quota, not just schedule around it. T-311/T-312's rate limiting and cooldown tiering weren't sufficient on their own — the account was found fully exhausted again a month after its 2026-09-09 reset. `discover()` now tries a free, unauthenticated direct guess against Greenhouse's and Lever's own public APIs before ever spending a search (`_guess_greenhouse_or_lever()`, needs no API key), and combines what used to be 3 separate Greenhouse/Lever/Workday `site:` searches into 1 (`_search_ats_combined()`) for companies the guess step misses — cutting the worst-case per-company search cost from up to 4 calls to at most 2, and removing the need for search entirely for most companies. `retry_unresolved()`'s `default_min_age_hours` (FR-73) changed from 24 to 168 (weekly) as a backstop, since the remaining demand can still exceed a week's worth of the ~8/day budget on its own. |

### 2.4 Security

| ID | Requirement |
|---|---|
| NFR-12 | API keys SHALL never appear in logs, committed files, or error messages. |
| NFR-13 | `.env` and the `/resumes` directory SHALL be in `.gitignore`. |
| NFR-14 | Resume PDF content SHALL be processed locally and never transmitted to third-party services other than the Anthropic API. |
| NFR-15 | The Telegram chat ID SHALL be redacted in all log output (show only last 4 characters). |

### 2.5 Maintainability

| ID | Requirement |
|---|---|
| NFR-16 | Each scraper SHALL implement a shared interface so adding/removing sources requires no changes to orchestration. |
| NFR-17 | The codebase SHALL have type annotations throughout and pass `mypy --strict`. |
| NFR-18 | Unit tests SHALL cover the resume extractor, matcher, deduplicator, notifier, and company discoverer. |
| NFR-19 | The system SHALL be deployable to Railway with a single `railway up` after initial setup. |

### 2.6 Multi-Tenant Isolation — Planned (v2)

| ID | Requirement |
|---|---|
| NFR-20 | **Not currently implemented** (§1.8–§1.13). Every user-scoped read/write path SHALL be scoped by `user_id` such that no user can read, modify, or receive a notification about another user's resumes, profile, watchlist, or posting-match state, even under application bugs in unrelated code paths (e.g. via a DB-level constraint or a shared query-scoping helper, not ad-hoc per-endpoint checks alone). |

**Note on NFR-09 (cost budget):** the $10/month figure applies to the original single-user personal deployment described in FR-01–FR-41. It does not apply to the multi-tenant beta (§1.8–§1.13), which is explicitly developer-funded/uncapped for testing purposes until billing (FR-67) is built.

---

## 3. Constraints

- **Legal / scraping**: Prefer official ATS APIs (Greenhouse, Lever) and aggregator APIs (JSearch, Adzuna) over direct HTML scraping. All direct scrapers must respect `robots.txt` and rate limits.
- **LinkedIn**: Direct scraping is not permitted — LinkedIn actively blocks automated access and prohibits it in their ToS. LinkedIn coverage comes exclusively through JSearch, confirmed live: `job_publisher: "LinkedIn"` postings are returned by real test queries.
- **Handshake**: Requires university SSO; direct access is not feasible. Not currently implemented — JSearch's aggregation has not been verified to include Handshake-exclusive postings, unlike the confirmed LinkedIn coverage above.
- **Resume privacy**: PDFs never leave the local machine or Anthropic API. They are never committed to git or uploaded to Railway.
- **Budget**: Under $10/month constrains model choice (Haiku preferred for scoring) and polling frequency.

---

## 4. Out of Scope (v1)

- Auto-submitting applications.
- ~~Email or SMS notifications (Telegram only).~~ **Superseded by §1.11 (planned v2)**: email becomes the default channel, Telegram an optional add-on.
- ~~A web UI or dashboard.~~ **Superseded by §1.8–§1.13 (planned v2)**: a web app is required for account signup, resume upload/management, and watchlist editing.
- Tracking application status post-submission.
- International internships outside the United States.
- Calendar or deadline integration.
- Automated resume tailoring per posting.
- Billing/subscriptions — still out of scope for the initial multi-tenant build too; see FR-67.
