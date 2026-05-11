# Requirements

## 1. Functional Requirements

### 1.1 Job Discovery

| ID | Requirement |
|---|---|
| FR-01 | The system SHALL search using broad domain-level keyword queries — not exact job titles. Queries SHALL cover the domains: software engineering, data science, machine learning, and AI, and SHALL include co-op variants (e.g., `"software engineering co-op"`, `"data science co-op"`). The system SHALL NOT filter by internship term (summer/fall/spring/co-op) at the search layer — all terms are included and term context is surfaced by the AI scorer. |
| FR-02 | The system SHALL poll at least the following Tier 2 sources: Indeed (via public RSS feeds), Adzuna, Wellfound (AngelList), Y Combinator Work at a Startup, HackerNews "Who's Hiring", RemoteOK, and Dice. No paid aggregator API (e.g., JSearch) is required. |
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
| FR-10 | If a company cannot be resolved after all discovery steps, the system SHALL mark it as `unresolved` in the database and log a warning. It SHALL NOT send an error SMS for each individual unresolved company. |
| FR-11 | Unresolved companies SHALL be surfaced to the user via: (a) the `python -m src.db stats` command, and (b) the weekly digest SMS (count + names). |
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
| FR-20 | `profile.yaml` SHALL contain only user preferences — target locations, match threshold, excluded keywords, digest settings. It SHALL NOT contain skills or experience. |

### 1.4 Deduplication

| ID | Requirement |
|---|---|
| FR-21 | The system SHALL assign a stable unique identifier to each posting based on source + external job ID. |
| FR-22 | The system SHALL never send more than one SMS notification for the same posting. |
| FR-23 | Seen postings SHALL be persisted in the database across restarts. |
| FR-24 | The system SHALL store full posting metadata for all postings regardless of match score, enabling future re-scoring without re-fetching. |

### 1.5 AI Scoring

| ID | Requirement |
|---|---|
| FR-25 | The system SHALL score each new posting 0–100 against `profile.cache.json` using Claude. |
| FR-26 | The system SHALL only send an SMS alert for a posting if its score >= `profile.yaml → matching.min_score`. |
| FR-27 | The match score and a brief Claude reasoning summary SHALL be stored in the database for every scored posting. |
| FR-28 | When `profile.cache.json` changes (new hash), the system SHALL re-score all previously stored postings where `notified = FALSE` and send new alerts for those that now qualify. |
| FR-29 | The scoring prompt SHALL instruct Claude to evaluate holistic relevance across all four target domains (SWE, DS, ML, AI) — not just title keyword matching. |

### 1.6 Notifications

| ID | Requirement |
|---|---|
| FR-30 | The system SHALL send an SMS to the configured phone number when a posting meets the match threshold. |
| FR-30a | If the number of qualifying matches in a single polling cycle is below `BURST_THRESHOLD` (default: 5), the system SHALL send one individual SMS per match. If at or above the threshold, it SHALL send a single batched summary SMS listing all matches ranked by score, with a note to check `db stats` for details. |
| FR-31 | Each individual SMS SHALL contain: company name, role title, location/remote status, match score, and direct application URL. |
| FR-32 | The system SHALL send a weekly digest SMS (configurable day/time, default: Sunday 9am) containing: number of new postings found, number of alerts sent, and names of unresolved companies. |
| FR-33 | The digest SMS is in addition to real-time alerts, not a replacement. |
| FR-34 | All sent notifications SHALL be logged with timestamp and Twilio delivery status. |

### 1.7 Scheduling & CLI

| ID | Requirement |
|---|---|
| FR-35 | The system SHALL poll all sources on a configurable interval (default: 30 minutes). |
| FR-36 | `--run-once`: execute a single full cycle and exit. |
| FR-37 | `--dry-run`: full pipeline without sending any SMS. |
| FR-38 | `--rebuild-profile`: re-extract profile from all resume PDFs and exit. |
| FR-39 | `--rescore`: re-score all stored postings against current profile; send new alerts for newly qualifying ones. |
| FR-40 | `python -m src.db stats`: print summary of total postings found/scored/alerted, unresolved companies (with names), and estimated API cost to date. |
| FR-41 | The system SHALL emit a structured JSON log line for every run summarizing: sources polled, postings found, postings new, postings scored, alerts sent, errors, and estimated cost. |

---

## 2. Non-Functional Requirements

### 2.1 Reliability

| ID | Requirement |
|---|---|
| NFR-01 | A failure in one data source SHALL NOT prevent other sources from completing in the same run. |
| NFR-02 | Failed API calls SHALL be retried up to 3 times with exponential back-off before marking the source as failed for that run. |
| NFR-03 | If any source fails for 3 or more consecutive runs, the system SHALL include it in the next weekly digest SMS. |
| NFR-04 | The system SHALL resume correctly after process restart without sending duplicate notifications. |

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
| NFR-09 | Monthly operating cost SHALL be under $10 USD (hosting + APIs + SMS) at steady state. |
| NFR-10 | The system SHALL log estimated Claude API cost per run based on token counts in the API response. |
| NFR-11 | The profile system prompt SHALL use Anthropic prompt caching to reduce repeat token costs on every scoring call. |

### 2.4 Security

| ID | Requirement |
|---|---|
| NFR-12 | API keys SHALL never appear in logs, committed files, or error messages. |
| NFR-13 | `.env` and the `/resumes` directory SHALL be in `.gitignore`. |
| NFR-14 | Resume PDF content SHALL be processed locally and never transmitted to third-party services other than the Anthropic API. |
| NFR-15 | The phone number SHALL be redacted in all log output (show only last 4 digits). |

### 2.5 Maintainability

| ID | Requirement |
|---|---|
| NFR-16 | Each scraper SHALL implement a shared interface so adding/removing sources requires no changes to orchestration. |
| NFR-17 | The codebase SHALL have type annotations throughout and pass `mypy --strict`. |
| NFR-18 | Unit tests SHALL cover the resume extractor, matcher, deduplicator, notifier, and company discoverer. |
| NFR-19 | The system SHALL be deployable to Railway with a single `railway up` after initial setup. |

---

## 3. Constraints

- **Legal / scraping**: Prefer official ATS APIs (Greenhouse, Lever) and aggregator APIs (JSearch, Adzuna) over direct HTML scraping. All direct scrapers must respect `robots.txt` and rate limits.
- **LinkedIn**: Direct scraping is not permitted. LinkedIn coverage comes exclusively through JSearch.
- **Handshake**: Requires university SSO; direct access is not feasible. Covered best-effort through JSearch aggregation.
- **Resume privacy**: PDFs never leave the local machine or Anthropic API. They are never committed to git or uploaded to Railway.
- **Budget**: Under $10/month constrains model choice (Haiku preferred for scoring) and polling frequency.

---

## 4. Out of Scope (v1)

- Auto-submitting applications.
- Email notifications (SMS only).
- A web UI or dashboard.
- Tracking application status post-submission.
- International internships outside the United States.
- Calendar or deadline integration.
- Automated resume tailoring per posting.
