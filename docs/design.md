# Design

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          InternshipAgent                            │
│                                                                     │
│  /resumes/*.pdf ──► Resume Extractor ──► profile.cache.json        │
│                       (Claude)                  │                   │
│                                                 │                   │
│  profile.yaml (prefs) ──────────────────────────┤                  │
│                                                 │                   │
│  config/companies.yaml ──► Company Discoverer ──► per-company ATS  │
│                               (lookup + web)          scrapers      │
│                                                         │           │
│                           ┌─────────────────────────────┤          │
│                           │                             │           │
│        Job Board APIs (Tier 2)              ATS APIs (Tier 1)      │
│   Indeed RSS · Adzuna · JSearch          Greenhouse · Lever        │
│   HackerNews · RemoteOK                  Custom scrapers            │
│   (Wellfound/YC/Dice: not wired in)                                 │
│                           │                             │           │
│                           └─────────────┬───────────────┘          │
│                                         ▼                           │
│                              ┌─────────────────────┐               │
│                              │   Deduplicator      │               │
│                              │   (SQLite/Postgres)  │               │
│                              └──────────┬──────────┘               │
│                                         │ new postings only         │
│                                         ▼                           │
│                              ┌─────────────────────┐               │
│                              │   AI Scorer         │               │
│                              │   (Claude Haiku)    │               │
│                              └──────────┬──────────┘               │
│                                         │ every scored posting      │
│                                         ▼                           │
│                              ┌─────────────────────┐               │
│                              │   Notifier          │               │
│                              │   (Telegram)        │               │
│                              └─────────────────────┘               │
│                                                                     │
│                              ┌─────────────────────┐               │
│                              │   SQLite/Postgres   │ ◄── all stages │
│                              └─────────────────────┘               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Resume Ingestion Pipeline

This replaces the manual `profile.yaml` skills section. The user never edits their skills or projects — they just drop PDFs into `/resumes/`.

### 2.1 Extraction flow

```
/resumes/resume-swe.pdf   ─┐
/resumes/resume-ml.pdf    ─┼──► pdfplumber (text extraction)
/resumes/resume-v3.pdf    ─┘          │
                                      ▼
                           Claude: "Extract structured profile from this resume text"
                           (called once per PDF)
                                      │
                           ┌──────────┴──────────┐
                           │  ExtractedProfile   │
                           │  (per PDF)          │
                           └──────────┬──────────┘
                                      │ (all PDFs)
                                      ▼
                           Merger: union of skills, languages,
                           tools, frameworks; deduplicated by
                           normalized name; projects merged
                           keeping unique ones
                                      │
                                      ▼
                           profile.cache.json  ──► gitignored, synced to
                                                   Railway via PROFILE_CACHE
                                                   env var (§2.4) — NOT committed
                                                   (neither are the PDFs)
```

### 2.2 Extraction prompt

Claude is given each resume as plain extracted text and asked to return a JSON object:

```json
{
  "languages": ["Python", "JavaScript", "Java"],
  "frameworks": ["React", "FastAPI", "PyTorch", "scikit-learn"],
  "tools": ["Docker", "Git", "AWS S3", "PostgreSQL"],
  "experience_level": "sophomore",
  "graduation_year": 2027,
  "projects": [
    {
      "name": "Image Classifier",
      "description": "CNN trained on CIFAR-10 achieving 94% top-1 accuracy",
      "technologies": ["Python", "PyTorch", "AWS S3"]
    }
  ],
  "work_experience": [
    {
      "title": "Software Engineering Intern",
      "company": "Acme Corp",
      "description": "Built REST APIs using FastAPI; reduced latency by 40%"
    }
  ]
}
```

### 2.3 Merging across multiple resumes

The merger normalizes strings (`"javascript"` = `"JavaScript"` = `"JS"` → `"JavaScript"`) and computes the union. For projects: identity is based on normalized project name — if two resumes list the same project, the longer description wins. For experience level and graduation year: the values are expected to be consistent; if they differ, the most recent file's value is used and a warning is logged.

### 2.4 Change detection and deployment

Because the repository is public, `profile.cache.json` is gitignored. The cloud agent reads the profile from a `PROFILE_CACHE` environment variable (base64-encoded JSON) set in the Railway dashboard.

**Rebuild workflow:**

```
[local machine]
1. Update your resume PDF in /resumes/
2. python -m src.main --rebuild-profile
   → extracts from all PDFs
   → writes profile.cache.json locally
   → prints the Railway command to update the env var:
     railway variables set PROFILE_CACHE="<base64 string>"
3. Run that command (Railway CLI must be installed and logged in)
4. Railway restarts the worker with the new profile (~30s)
```

**Change detection on the cloud agent:** On each run, the agent hashes the `PROFILE_CACHE` env var value. If the hash differs from the one stored in the last `run_log`, it triggers a re-score pass of all unnotified postings.

**Local development:** When running locally, the agent loads `profile.cache.json` directly from disk (no env var needed). This makes the dev loop fast — rebuild and immediately run `--run-once --dry-run` without touching Railway.

---

## 3. Company Discovery Pipeline

### 3.1 Overview

The user lists company names in `config/companies.yaml`. The agent resolves each name to a monitorable career page endpoint using a multi-step discovery process.

```
"Stripe"
   │
   ▼
Step 1: Check bundled lookup table (500+ major tech companies pre-mapped)
   │  hit → (ats_type="greenhouse", slug="stripe")
   │  miss ↓
   ▼
Step 2: Web search "{company} site:boards.greenhouse.io"
        Web search "{company} site:jobs.lever.co"
   │  hit → extract slug from URL, cache in DB
   │  miss ↓
   ▼
Step 3: Web search "{company} careers internship site:careers.{company}.com"
        Try to identify ATS from page content (Workday, iCIMS, Taleo, etc.)
   │  hit → store custom URL in DB, flag for custom scraper
   │  miss ↓
   ▼
Step 4: Mark as "unresolved" in DB; include in hourly digest message
        Re-attempted every hour (subject to a 1-hour per-company cooldown)
```

### 3.2 Bundled lookup table

Maintained as `src/data/company_ats_map.json` — a curated mapping of ~500 well-known tech companies to their ATS. Sourced initially from the open-source [SimplifyJobs internship tracking repo](https://github.com/SimplifyJobs/Summer2026-Internships) and community databases. Updated periodically.

```json
{
  "stripe": {"ats": "greenhouse", "slug": "stripe"},
  "airbnb": {"ats": "greenhouse", "slug": "airbnb"},
  "linear": {"ats": "lever", "slug": "linear"},
  "notion": {"ats": "lever", "slug": "notionhq"},
  "google": {"ats": "custom", "scraper": "google"},
  "meta": {"ats": "custom", "scraper": "meta"}
}
```

### 3.3 Web search for discovery

Uses the SerpAPI or Google Custom Search API. Query: `"{company name}" internship site:boards.greenhouse.io`. Extracts the slug from the first result URL. Rate-limited to avoid excessive queries; results cached permanently in the `company_lookup` DB table.

### 3.4 Warning surfacing for unresolved companies

Unresolved companies are surfaced in two ways:
1. `python -m src.db stats` prints a section: `Unresolved companies (N): [list]`
2. The hourly digest message includes: `⚠ Could not find career pages for: Acme Corp, Widgets Inc`

---

## 4. Data Sources (Tier 2 — Broad Search)

Indeed RSS, HN, and RemoteOK run the same keyword queries sequentially within the main poll cycle (`_fetch_all_postings()` in `main.py` is a plain `for` loop, not parallelized/threaded). Adzuna and JSearch run one broad query each on their own separate, slower schedules (§10.1) rather than the full keyword list. All of them feed into the same shared deduplication layer regardless of cadence — a posting that appears on multiple platforms is stored once and triggers one alert.

### 4.1 Indeed RSS (free)

- **Provider**: Indeed public RSS feeds — no API key, no account required
- **Covers**: All jobs posted on Indeed, which aggregates a large portion of US job postings
- **URL format**: `https://www.indeed.com/rss?q={query}&l=United+States&jt=internship&sort=date`
- **Parse with**: `feedparser` library; each RSS item includes job ID in the `<guid>` field
- **Free tier**: Effectively unlimited — public RSS, no rate limit specified. Polite 1 req/2s between queries.
- **Limitation**: Does not include LinkedIn-exclusive postings. Those are covered by JSearch (§4.1a) instead.
- **Dedup key**: `indeed:{job_id}` (extracted from RSS guid)

### 4.1a JSearch (RapidAPI) — LinkedIn coverage

- **Provider**: JSearch on RapidAPI — the only source that reaches LinkedIn, Glassdoor, and ZipRecruiter postings, aggregated through a legitimate metered API rather than direct scraping (see the LinkedIn constraint, §3 of `requirements.md`)
- **Endpoint**: `GET https://jsearch.p.rapidapi.com/search-v2` — **not** the more commonly documented `/search`, which 404s on this project's subscription; confirmed by live-testing against the real key, since RapidAPI's "JSearch" listings vary across publishers in which endpoints they actually expose. Results are nested under `data.jobs`, not `data` directly.
- **Auth**: `X-RapidAPI-Key` / `X-RapidAPI-Host` headers; requires `JSEARCH_API_KEY`
- **Query shape**: one broad query per poll (`"software engineering intern in the United States"`), `country=us`, `employment_types=INTERN`, `date_posted=week` — deliberately not looped per domain keyword like Indeed RSS, since every request is metered
- **Cadence**: its own scheduled job, independent of the main cycle — currently every 4 hours (~180 requests/month), sized to fit a 200/month free-tier plan. Re-tune the interval (`queries_per_poll × polls_per_day × 30 ≤ plan quota`) if the plan differs.
- **Source labeling**: each result includes `job_publisher` (e.g. `"LinkedIn"`, `"ZipRecruiter"`, `"BeBee"`) — the exact board it was aggregated from. Encoded into `source` as `jsearch:{publisher}`, the same compound-source convention `greenhouse.py`/`lever.py` use for company slugs, and surfaced to the user as the message's `Source:` line (§9.2)
- **Verified live**: a test query returned 10 postings, 4 explicitly `job_publisher: LinkedIn`
- **Dedup key**: `(source, external_id) = ("jsearch:{publisher}", job_id)` — unlike other sources, `source` itself is compound here rather than a fixed string

### 4.2 Adzuna

- **Provider**: Adzuna developer API (free)
- **Covers**: Indeed, Reed, Totaljobs, and US-specific aggregation not fully in JSearch
- **Auth**: `app_id` + `app_key`
- **Free tier**: 250 calls/month
- **Filter**: `where=us`, `sort_by=date`, `full_time=0`
- **Dedup key**: `adzuna:{id}`

### 4.3 Wellfound / AngelList (startup-specific) — NOT IMPLEMENTED

Designed in but never built — there is no `src/scrapers/wellfound.py`. `WELLFOUND_API_KEY` exists as a `Settings` field with no corresponding scraper reading it. Planned for a later phase; the rest of this subsection describes the original design intent, not current behavior.

- **Provider**: Wellfound (formerly AngelList Talent)
- **Covers (planned)**: YC-backed, VC-backed, and early-stage startups — many post only here
- **Auth**: Wellfound API key (free developer access)
- **Why needed**: Startups that recruit off Wellfound often don't appear in Indeed/Adzuna/JSearch results at all
- **Dedup key (planned)**: `wellfound:{job_id}`

### 4.4 Y Combinator Work at a Startup — NOT WIRED IN

`src/scrapers/yc.py` exists and is implemented, but is not registered in `_SOURCES` or any scheduled job in `main.py` — it's dormant, never actually called. Planned to be wired in later.

- **Provider**: `workatastartup.com` (free public API, no auth required)
- **Covers**: All YC-batch companies exclusively — fills a gap because YC companies often list only here before listing on LinkedIn
- **Endpoint**: `https://www.workatastartup.com/company_filters/search_startup_jobs`
- **Filter**: role type = intern, location = US / remote
- **Dedup key**: `yc:{job_id}`

### 4.5 HackerNews "Who's Hiring" thread

- **Provider**: Algolia HN Search API (free, no auth)
- **Covers**: Monthly HN hiring threads (`Ask HN: Who is hiring?`). Extremely popular among AI labs, dev-tool startups, and research orgs — many post only here
- **Mechanism**: Parse the current month's thread; extract comments mentioning "intern" or "internship"; extract company name and any URL; run through AI scorer like a normal posting
- **Dedup key**: `hn:{comment_id}`
- **Cadence**: Wired into the main poll cycle (`_SOURCES` in `main.py`) — re-fetched every cycle (default hourly), same as Indeed RSS and RemoteOK, not on a separate monthly/daily schedule

### 4.6 RemoteOK

- **Provider**: RemoteOK public API (`remoteok.com/api`) — free, no auth, no rate limit specified
- **Covers**: Remote-only internships and entry-level roles across all tech companies; good coverage of distributed/async-first companies that don't post on LinkedIn
- **Filter**: tags include `intern` or `junior`; parsed from JSON feed
- **Dedup key**: `remoteok:{id}`

### 4.7 Dice — NOT WIRED IN

`src/scrapers/dice.py` exists and is implemented, but — like YC (§4.4) — is not registered in any scheduled job. Dormant.

- **Provider**: Dice Tech Job Board (API or RSS feed)
- **Covers**: Tech-specific; large US engineering job board that surfaces postings from defense contractors, enterprise software, and mid-size tech companies that LinkedIn under-indexes
- **Filter**: keyword queries same as other sources; `employment_type=internship`
- **Dedup key**: `dice:{id}`

---

## 5. Search Strategy

### 5.1 Two-tier coverage model

```
Tier 1 — Direct company monitoring (companies.yaml)
  └── Fastest alert (within one poll cycle of posting — default 60 min)
  └── Best for: big tech, quant firms, companies that don't post broadly
  └── Source: Greenhouse / Lever ATS APIs, custom scrapers
  └── Coverage: ~150 companies in your personal list

Tier 2 — Broad job board search (all other companies)
  └── Catches any company posting on any major platform
  └── Best for: startups, mid-size companies, companies you don't know yet
  └── Active sources: Indeed RSS · Adzuna (weekly) · JSearch (LinkedIn, every 4hrs) ·
      HackerNews · RemoteOK
  └── Not currently implemented/wired in: Wellfound, YC, Dice (§4.3, §4.4, §4.7)
  └── Coverage: broad, but not "effectively unlimited" until the above three ship
```

The same posting can appear in both tiers (e.g., a Stripe job found via Indeed RSS AND via the Greenhouse direct monitor). Cross-source deduplication (§6) ensures you only get one alert.

### 5.2 Domain-based keyword search (not title matching)

The agent searches using broad domain-level keyword combinations. The AI scorer handles fine-grained relevance filtering — the search layer casts a wide net.

**Full query list** (run sequentially against Indeed RSS, HN, and RemoteOK each main cycle — see §4's intro; Adzuna and JSearch use one broad query instead, not this full list):
```
# Core SWE / DS / ML / AI
"software engineering intern"
"software intern"
"data science intern"
"machine learning intern"
"AI intern"
"artificial intelligence intern"
"research intern computer science"
"backend intern"
"frontend intern"
"full stack intern"

# Co-op / extended terms
"software engineering co-op"
"software co-op"
"machine learning co-op"
"data science co-op"

# All terms (agent does not filter by season — catches summer, fall, spring, co-op)
```

The agent does not filter by semester or term — it catches summer, fall, spring, and co-op postings alike. Season filtering would require parsing unstructured job description text and is left to the AI scorer (which can flag term in its reasoning).

**Filters applied at the search layer** (before storage — vary per source, see §4 for each one's exact query params):
- Country: United States, where the source's API supports it (Indeed, Adzuna, JSearch); HN and RemoteOK have no country param and rely entirely on the AI scorer to catch non-US postings
- Job type: internship-flavored, where supported (Indeed's `jt=internship`, JSearch's `employment_types=INTERN`, Adzuna's `full_time=0`); HN and RemoteOK don't filter by employment type at the query level
- Freshness: **not** a per-source "date posted" query param for most sources — enforced uniformly after fetching, at the dedupe/store step, via `max_posting_age_days` (default 7, `profile.yaml → matching.max_posting_age_days`). Postings with no reliable `posted_at` are kept rather than dropped. JSearch is the one exception with an actual API-level filter (`date_posted=week`).

**Correction (found stale while auditing this doc, 2026-08-12): keywords_excluded is NOT a pre-scoring filter**, despite FR-05's original intent. `keywords_excluded` is only ever read in `matcher.py`'s `build_profile_summary()`, where it's rendered into the Claude system prompt as a hint ("Automatically excluded keywords: ...") for the AI scorer to weigh — every posting is still scored (and billed) regardless of whether it contains an excluded keyword. There is no code path that drops a posting before scoring based on this list. Worth fixing later if the token-cost savings FR-05 intended actually matter.

**Filters applied by AI scorer** (after fetching):
- Relevance to user's actual skill set
- Seniority level appropriateness
- Degree requirements (no "PhD required" unless configured)
- `keywords_excluded` (see correction above — advisory to the scorer, not a hard pre-filter)

### 5.3 Tier 1 company career page search

For direct ATS monitoring, the agent searches each company's board for postings where `title ILIKE '%intern%'` — broad enough to catch "Internship", "Intern", "Co-op", "Student", "New Grad". The AI scorer validates relevance.

### 5.4 Why you don't need to list every company

Between the currently active Tier 2 sources, the agent covers every company posting on Indeed, RemoteOK, or HN's monthly thread — and, when `JSEARCH_API_KEY` is configured, LinkedIn, Glassdoor, and ZipRecruiter too. Wellfound and Dice coverage is not yet available (§4.3, §4.7 — not implemented/not wired in). The Tier 1 list is still worth using for companies where you want the fastest possible alert, or where the company doesn't post publicly on any of the above boards.

---

## 6. Cross-Source Deduplication

This is the mechanism that ensures the same job posting found on multiple platforms results in exactly one alert.

### 6.1 The problem

The same Stripe internship might appear as:
- A JSearch result (sourced from LinkedIn)
- An Adzuna result
- A Greenhouse direct result (from `boards.greenhouse.io/stripe/jobs/12345`)

Without cross-source dedup, you'd get 3 alerts for one job.

### 6.2 Primary dedup: source + external ID

Each posting is stored with a `UNIQUE(source, external_id)` constraint. This prevents the same source from inserting the same posting twice across runs.

### 6.3 Secondary dedup: normalized application URL

Every posting has a canonical `apply_url` (the direct link to the application, not a redirect or tracking URL). Before inserting a new posting, the system:

1. Normalizes the URL (strips UTM params, tracking tokens, query strings that don't affect the destination)
2. Checks if any existing posting in the DB has the same normalized URL
3. If a match exists: skip the insert; the posting is already known under a different source key

This catches the Stripe example above — all three sources ultimately link to `https://boards.greenhouse.io/stripe/jobs/12345`, so only the first one found is inserted and scored.

### 6.4 Tertiary dedup: fuzzy identity matching (fallback)

For postings where the URL differs but it's clearly the same job (e.g., LinkedIn's redirect URL vs. the company's direct ATS URL), a lightweight fuzzy check is applied:

- Normalize company name
- Normalize job title (lowercase, strip punctuation)
- Check if a posting exists within the last 7 days with the same (company, normalized_title)
- If matched: log as probable duplicate, skip insert

This is a best-effort fallback; false positives (two legitimately different roles with the same title at the same company, e.g., "Software Engineering Intern — New York" and "Software Engineering Intern — Seattle") are handled by keeping both if their descriptions differ significantly.

---

## 7. Data Model

### 7.1 `job_postings` table

```sql
CREATE TABLE job_postings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,      -- 'indeed', 'adzuna', 'hn', 'remoteok', 'greenhouse:{slug}',
                                             -- 'lever:{slug}', 'jsearch:{publisher}' (e.g. 'jsearch:LinkedIn') —
                                             -- greenhouse/lever/jsearch encode extra context after the colon
    external_id         TEXT NOT NULL,      -- ID from the source system
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    company_normalized  TEXT,               -- lowercase, stripped for fuzzy dedup
    title_normalized    TEXT,               -- lowercase, stripped for fuzzy dedup
    location            TEXT,
    is_remote           BOOLEAN DEFAULT FALSE,
    url                 TEXT NOT NULL,      -- source listing URL
    apply_url           TEXT,               -- canonical application URL (used for cross-source dedup)
    apply_url_normalized TEXT,              -- UTM-stripped apply_url (indexed for dedup lookup)
    description         TEXT,
    posted_at           DATETIME,
    found_at            DATETIME NOT NULL,
    match_score         INTEGER,            -- 0–100; NULL = not yet scored. No minimum-score gate on
                                             -- notification — every scored posting gets an alert.
    match_reasoning     TEXT,
    missing_qualifications JSON,            -- Claude-identified gaps vs. the profile (list[str])
    profile_hash        TEXT,               -- hash of profile.cache.json at scoring time
    notified            BOOLEAN DEFAULT FALSE,
    notified_at         DATETIME,
    partial_notified    BOOLEAN DEFAULT FALSE, -- UNUSED as of the full/partial-match tier removal
                                             -- (§9.1). Left in place rather than migrated out — not
                                             -- worth the risk for a dead column; see the deploy-race
                                             -- note in docs/tasks.md on the cost of schema migrations.
    applied             BOOLEAN DEFAULT FALSE, -- set via the "Mark Applied" Telegram button tap;
                                             -- permanently excludes the posting from future alerts
    applied_at          DATETIME,
    UNIQUE(source, external_id)
);

CREATE INDEX idx_apply_url_normalized ON job_postings(apply_url_normalized);
CREATE INDEX idx_company_title_dedup ON job_postings(company_normalized, title_normalized, found_at);
```

### 7.2 `company_lookup` table

```sql
CREATE TABLE company_lookup (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name_raw        TEXT NOT NULL UNIQUE,  -- as entered in companies.yaml
    name_normalized TEXT,                  -- lowercase, stripped
    ats_type        TEXT,                  -- 'greenhouse' | 'lever' | 'workday' | 'custom' | NULL
    slug            TEXT,                  -- ATS-specific slug
    url             TEXT,                  -- direct URL if not standard ATS
    status          TEXT NOT NULL,         -- 'resolved' | 'unresolved' | 'manual'
    last_attempted  DATETIME,
    resolved_at     DATETIME,
    source          TEXT                   -- 'bundled_table' | 'web_search' | 'manual'
);
```

### 7.3 `notifications` table

```sql
CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_posting_id  INTEGER NOT NULL REFERENCES job_postings(id),
    sent_at         DATETIME NOT NULL,
    recipient_id    TEXT NOT NULL,         -- redacted in logs
    message         TEXT NOT NULL,
    telegram_message_id TEXT,
    delivery_status TEXT                   -- 'sent' | 'delivered' | 'failed'
);
```

### 7.4 `run_log` table

```sql
CREATE TABLE run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      DATETIME NOT NULL,
    finished_at     DATETIME,
    sources_polled  TEXT,                  -- JSON array
    postings_found  INTEGER DEFAULT 0,
    postings_new    INTEGER DEFAULT 0,
    postings_scored INTEGER DEFAULT 0,
    alerts_sent     INTEGER DEFAULT 0,
    errors          TEXT,                  -- JSON array
    profile_hash    TEXT,
    estimated_cost_usd REAL
);
```

### 7.5 `bot_state` table

Singleton row (`id=1`, created lazily on first access) added alongside the "Mark Applied" / `/pause` / `/resume` feature — not present in the original schema design; documented here since it was missing from this section entirely until this audit pass.

```sql
CREATE TABLE bot_state (
    id                    INTEGER PRIMARY KEY,   -- always 1
    last_update_id        INTEGER,                -- Telegram getUpdates cursor; advances past
                                                    -- already-processed updates so they aren't
                                                    -- redelivered on the next poll
    notifications_paused  BOOLEAN DEFAULT FALSE    -- master kill switch set by /pause and /resume
);
```

---

## 8. AI Scoring

### 8.1 Prompt structure

Claude is called with:
- **System prompt** (cached via Anthropic cache-control): the full contents of `profile.cache.json` plus `profile.yaml → preferences`, formatted as a readable profile summary.
- **User message**: a batch of up to 10 job postings (title + company + description).

Prompt caching means the profile is only tokenized once per cache window (~5 min TTL), drastically reducing cost on repeated runs.

### 8.2 Scoring rubric (in the system prompt)

Claude is instructed to evaluate each posting on:
- **Skills alignment** (40%): does the posting require skills the user has?
- **Domain relevance** (30%): is this a SWE / DS / ML / AI role?
- **Seniority fit** (20%): is this appropriate for the user's experience level?
- **Role accessibility** (10%): are there gatekeeping requirements (PhD, clearance, 2+ YOE) the user doesn't meet?

Score 0–100. Claude also identifies `missing_qualifications`: specific skills, tools, or requirements named in the posting that the candidate's profile doesn't show (empty list for a strong match). Return JSON array with `external_id`, `score`, `reasoning` (1–2 sentences), `missing_qualifications` (list of short strings).

### 8.2a No minimum-score gate

There is no `min_score` threshold and no separate "partial match" tier — both existed at one point and were removed. Every scored posting is notified, with its score, reasoning, and `missing_qualifications` all included in the message (§9.2), so the user judges fit themselves rather than the system silently dropping weak matches. See §9.1 for the unified notification routing.

### 8.3 Model selection

| Use case | Model | Why |
|---|---|---|
| Resume extraction | `claude-sonnet-4-6` | Needs strong comprehension of free-form resume text |
| Job scoring (batches) | `claude-haiku-4-5` | Fast and cheap; sufficient for structured scoring |
| Fallback (complex postings) | `claude-sonnet-4-6` | For postings where Haiku returns low-confidence scores |

Override with `CLAUDE_SCORING_MODEL` and `CLAUDE_EXTRACTION_MODEL` env vars.

### 8.4 Cost estimate

At Haiku pricing (~$0.80/MTok input, ~$4.00/MTok output):
- Profile system prompt: ~2,000 tokens (cached after first call)
- Per posting: ~500 tokens input, ~100 tokens output
- 500 postings/day × $0.0004/posting ≈ **$0.20/day → ~$6/month**

Resume extraction (Sonnet): ~$0.01–0.05 per rebuild (rare operation).

Total: well within the $10/month budget.

---

## 9. Notification Design

**Delivery channel:** Telegram Bot API, not SMS/Twilio. The original design used Twilio, but Twilio's toll-free/A2P verification process is built to vet registered businesses — it rejected this project's application (reason: business information could not be verified) because a personal single-recipient tool genuinely isn't a business. Telegram's Bot API requires no business verification at all: create a bot via `@BotFather`, get a token, done. It delivers the same real-time push-notification experience.

### 9.1 Notification modes

The notifier operates in one of two modes per polling cycle, depending on how many scored postings there are to notify (`notify_matches()`). There is no separate full/partial-match distinction — every scored posting goes through this same single path, since the score floor was removed (§8.2a).

**Individual mode** (< `BURST_THRESHOLD` postings, default 20): one message per posting, each including its score, reasoning, missing qualifications, source board, and a direct apply link, plus a "Mark Applied" button. Individual sends are paced 1 second apart (`_INDIVIDUAL_SEND_DELAY_SECONDS`) to stay under Telegram's per-chat rate limit — a cycle with, say, 15 postings takes ~15 seconds to fully notify, not instant.

```
[InternAgent] Stripe · Software Engineering Intern · Remote
Source: Greenhouse
Match: 88 — Strong Python/backend fit, welcoming undergrads
Missing: Kubernetes
Apply: https://boards.greenhouse.io/stripe/jobs/123456
```

**Burst mode** (≥ `BURST_THRESHOLD` postings): a single summary message listing all of them, each with its score, source, missing qualifications, and its own apply link, capped at 10 lines shown (`+ N more — run db stats`) to stay within Telegram's message length limit on a genuinely high-volume cycle.

```
[InternAgent] 25 new postings this cycle
 1. Stripe · SWE Intern · Remote (92) via Greenhouse — missing: none listed
    Apply: https://boards.greenhouse.io/stripe/jobs/123456
 2. TikTok · SWE Intern · Seattle (78) via LinkedIn — missing: Kubernetes
    Apply: https://www.linkedin.com/jobs/view/abc123
 + 23 more — run `db stats` to see all
```

The full list is always stored in the database. `db stats` shows everything regardless of which mode was used. `BURST_THRESHOLD` is configurable via env var — the default (20) was chosen deliberately high so individual messages (with apply links) are the normal experience, and batching only kicks in as a safety net on unusually high-volume cycles, not as the default behavior.

### 9.2 Real-time message format (individual mode)

```
[InternAgent] Stripe · Software Engineering Intern · Remote
Source: Greenhouse
Match: 88 — Strong Python/backend fit, welcoming undergrads
Missing: Kubernetes
Apply: https://boards.greenhouse.io/stripe/jobs/123456
```

The `Source:` line is derived from `JobPosting.source` via `_friendly_source()` in `notifier.py` — most sources map to a fixed label (`greenhouse:{slug}` → `"Greenhouse"`); `jsearch:{publisher}` is the one exception where the compound suffix *is* the label (`jsearch:LinkedIn` → `"LinkedIn"`), since that's the actual board the posting was aggregated from. The line is omitted entirely if `source` is empty (defensive default, not expected in practice).

Targets ≤4096 chars (Telegram's hard per-message limit — generous compared to SMS's per-segment cost, so truncation is a rare edge case rather than the norm). URL is always included verbatim; reasoning is truncated if needed to fit the budget, after accounting for the header, source line, and missing-qualifications/apply-url suffix.

### 9.3 Digest message format

```
[InternAgent] Digest — Sun May 10 3:00PM
In the last hour: 3 new postings, 1 alerts sent
Top match: Stripe SWE Intern (92/100)
⚠ Not found: Acme Corp, Widgets Inc
```

"New postings" and "alerts sent" are scoped to the last hour (matching the digest's own cadence) — not `db stats`' all-time totals. "Top match" still looks back 7 days, independent of digest cadence, since "best match in the last hour" would rarely have anything to show.

### 9.4 Telegram delivery

- Plain `httpx` POST to `https://api.telegram.org/bot{token}/sendMessage` — no dedicated SDK needed for a single-endpoint integration this simple
- Stores the returned `message_id`
- On failure: retries once after 5 minutes; logs permanent failure
- Chat ID redacted to last 4 characters in all logs (lower sensitivity than a phone number, but kept consistent with the project's log-hygiene practice)

---

## 10. Scheduling & Orchestration

### 10.1 Scheduler

Five independent `APScheduler` jobs, all `max_instances=1` to prevent overlapping runs of the same job (different jobs can still run concurrently with each other):

| Job | Interval | Does |
|---|---|---|
| Main cycle | `IntervalTrigger(minutes=RUN_INTERVAL_MINUTES)` (default 60) | Fetch (Indeed/HN/RemoteOK/Greenhouse/Lever), dedupe, score, notify |
| Digest | `IntervalTrigger(hours=1)` | Retry unresolved companies, send summary message |
| Adzuna | `IntervalTrigger(weeks=1)` | Fetch, store unscored — separate from the main cycle to fit its free-tier quota |
| JSearch | `IntervalTrigger(hours=4)` | Fetch (LinkedIn/Glassdoor/ZipRecruiter via one broad query), store unscored — separate for the same reason, sized to a 200/month plan |
| Telegram command poll | `IntervalTrigger(minutes=2)` | `getUpdates` for `/pause`, `/resume`, and "Mark Applied" taps — deliberately frequent and decoupled from the hourly cycle so control feels responsive from the phone |

Postings Adzuna and JSearch store are picked up and scored by whichever main cycle run happens next — they don't score or notify themselves.

### 10.2 Run lifecycle

```
startup
  └── load .env
  └── load profile.cache.json + profile.yaml
  └── validate ANTHROPIC_API_KEY (required; process exits if missing)
        (TELEGRAM_BOT_TOKEN/CHAT_ID and other optional keys are validated
        lazily at send/fetch time, not at startup)
  └── start APScheduler (all 5 jobs from §10.1)

each main-cycle run:
  └── seed/resolve companies from COMPANIES_CONFIG or config/companies.yaml
  └── for each active source (sequential, not parallel — greenhouse/lever/indeed/hn/remoteok):
        └── fetch postings (broad keyword search)
        └── on failure: log and skip that source, don't fail the run (NFR-01)
  └── deduplicate against DB (insert new, skip known; skip anything older than
      max_posting_age_days)
  └── batch-score all NULL-score postings (groups of 10 via Claude)
  └── for every scored, unnotified, non-applied posting — no score floor:
        └── send Telegram message (individual or burst, by BURST_THRESHOLD)
        └── mark notified=TRUE
  └── check if profile hash changed → trigger re-score pass on unnotified postings
  └── write run_log entry (JSON)

every hour (digest job):
  └── re-attempt discovery for unresolved companies (1-hour per-company cooldown)
  └── compile last-hour stats (postings found, alerts sent)
  └── fetch unresolved companies list
  └── send digest message (unconditionally, if digest_enabled and not paused —
      not gated on there being anything new to report)

every 2 minutes (telegram command poll):
  └── getUpdates since last cursor
  └── callback_query "applied:{id}" → mark_applied, only from TELEGRAM_CHAT_ID
  └── message "/pause" or "/resume" → toggle bot_state.notifications_paused,
      only from TELEGRAM_CHAT_ID
```

**Not implemented**: startup does not currently check `/resumes` for file changes and warn — `detect_resume_changes()` exists in `resume_extractor.py` but is never called anywhere in `main.py`. The user must remember to run `--rebuild-profile` manually after updating a resume PDF.

---

## 11. Deployment (Railway)

```
InternshipAgent on Railway
  ├── Worker process: python -m src.main
  ├── SQLite on a Railway persistent volume (mounted at /data; no Postgres in use)
  ├── GitHub-linked: auto-deploys on push to main
  └── profile.cache.json and config/companies.yaml are BOTH gitignored —
      neither reaches the container via git. Each has its own env var
      (PROFILE_CACHE, COMPANIES_CONFIG) that must be synced manually; see
      the two workflows below. This is a real gap that caused company-direct
      monitoring to be silently inert in production for a while — see the
      docs/tasks.md entry on the companies.yaml deploy gap.
```

**Procfile:**
```
worker: python -m src.main
```

**railway.toml:**
```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "python -m src.main"
restartPolicyType = "on_failure"
restartPolicyMaxRetries = 10
```

**Resume update workflow:**
```
1. Update PDF in /resumes/ locally
2. python -m src.main --rebuild-profile
   → writes profile.cache.json locally
   → prints: railway variables set PROFILE_CACHE="<base64 string>"
3. Run that printed command (Railway CLI must be installed and logged in)
4. Setting the variable triggers a Railway redeploy automatically
```

**Companies watchlist update workflow:**
```
1. Edit config/companies.yaml locally
2. python -m src.main --sync-companies
   → prints: railway variables set COMPANIES_CONFIG="<base64 string>"
3. Run that printed command
4. Setting the variable triggers a Railway redeploy automatically
```

Neither `profile.cache.json` nor `config/companies.yaml` is ever committed to git — both stay gitignored, matching the "repo is public, personal data stays out of it" rule in §2.4 and §12.

---

## 12. Security

- All secrets in environment variables; `.env` in `.gitignore`
- `/resumes/` in `.gitignore`; PDFs never leave local machine or Anthropic API
- `profile.cache.json` contains only extracted skills/experience — no contact info, no address
- Telegram chat ID logged as `****XXXX` only
- Database permissions set to `600` on Linux

---

## 13. Key Dependencies

| Package | Purpose |
|---|---|
| `anthropic` | Claude API (resume extraction + job scoring) |
| `pdfplumber` | PDF text extraction |
| `feedparser` | Indeed RSS feed parsing |
| `httpx` | HTTP for every scraper, SerpAPI, and the Telegram Bot API (used synchronously — no other module in the codebase uses asyncio) |
| `playwright` | Headless browser for custom company scrapers |
| `sqlalchemy` | ORM + DB abstraction |
| `alembic` | Schema migrations |
| `apscheduler` | In-process job scheduler |
| `pydantic` / `pydantic-settings` | Config validation |
| `pyyaml` | profile.yaml parsing |
| `tenacity` | Retry with exponential back-off |
| `structlog` | Listed in `requirements.txt` but not actually imported anywhere in `src/` — structured JSON logging is done via stdlib `logging.basicConfig()` with a manual JSON format string (`_configure_logging()` in `main.py`) instead. Dead dependency; worth removing from `requirements.txt` if nothing ends up using it. |
| `mypy` | Static type checking |
| `pytest` | Tests |
