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
│   Indeed RSS · Adzuna · Wellfound · YC    Greenhouse · Lever        │
│   HackerNews · RemoteOK · Dice           Custom scrapers            │
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
│                                         │ score >= threshold        │
│                                         ▼                           │
│                              ┌─────────────────────┐               │
│                              │   Notifier          │               │
│                              │   (Twilio SMS)      │               │
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
                           profile.cache.json  ──► committed to git
                                                   (PDFs are not)
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
Step 4: Mark as "unresolved" in DB; include in weekly digest SMS
        Re-attempt weekly
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
2. The weekly digest SMS includes: `⚠ Could not find career pages for: Acme Corp, Widgets Inc`

---

## 4. Data Sources (Tier 2 — Broad Search)

All Tier 2 sources run the same keyword queries in parallel and feed into the shared deduplication layer. A posting that appears on multiple platforms is stored once and triggers one SMS.

### 4.1 Indeed RSS (free, replaces JSearch)

- **Provider**: Indeed public RSS feeds — no API key, no account required
- **Covers**: All jobs posted on Indeed, which aggregates a large portion of US job postings (including many that were previously accessed via JSearch)
- **URL format**: `https://www.indeed.com/rss?q={query}&l=United+States&jt=internship&sort=date`
- **Parse with**: `feedparser` library; each RSS item includes job ID in the `<guid>` field
- **Free tier**: Effectively unlimited — public RSS, no rate limit specified. Polite 1 req/2s between queries.
- **Limitation**: Does not include LinkedIn-exclusive postings. Those are covered by Tier 1 (direct ATS monitoring) for listed companies.
- **Dedup key**: `indeed:{job_id}` (extracted from RSS guid)

### 4.2 Adzuna

- **Provider**: Adzuna developer API (free)
- **Covers**: Indeed, Reed, Totaljobs, and US-specific aggregation not fully in JSearch
- **Auth**: `app_id` + `app_key`
- **Free tier**: 250 calls/month
- **Filter**: `where=us`, `sort_by=date`, `full_time=0`
- **Dedup key**: `adzuna:{id}`

### 4.3 Wellfound / AngelList (startup-specific)

- **Provider**: Wellfound (formerly AngelList Talent)
- **Covers**: YC-backed, VC-backed, and early-stage startups — many post only here
- **Auth**: Wellfound API key (free developer access)
- **Why needed**: Startups that recruit off Wellfound often don't appear in JSearch/Adzuna results at all
- **Dedup key**: `wellfound:{job_id}`

### 4.4 Y Combinator Work at a Startup

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
- **Cadence**: Monthly threads are parsed once on release (first weekday of each month) and re-checked daily for new comments

### 4.6 RemoteOK

- **Provider**: RemoteOK public API (`remoteok.com/api`) — free, no auth, no rate limit specified
- **Covers**: Remote-only internships and entry-level roles across all tech companies; good coverage of distributed/async-first companies that don't post on LinkedIn
- **Filter**: tags include `intern` or `junior`; parsed from JSON feed
- **Dedup key**: `remoteok:{id}`

### 4.7 Dice

- **Provider**: Dice Tech Job Board (API or RSS feed)
- **Covers**: Tech-specific; large US engineering job board that surfaces postings from defense contractors, enterprise software, and mid-size tech companies that LinkedIn under-indexes
- **Filter**: keyword queries same as other sources; `employment_type=internship`
- **Dedup key**: `dice:{id}`

---

## 5. Search Strategy

### 5.1 Two-tier coverage model

```
Tier 1 — Direct company monitoring (companies.yaml)
  └── Fastest alert (within one 30-min poll cycle of posting)
  └── Best for: big tech, quant firms, companies that don't post broadly
  └── Source: Greenhouse / Lever ATS APIs, custom scrapers
  └── Coverage: ~150 companies in your personal list

Tier 2 — Broad job board search (all other companies)
  └── Catches any company posting on any major platform
  └── Best for: startups, mid-size companies, companies you don't know yet
  └── Sources: JSearch · Adzuna · Wellfound · YC · HackerNews · RemoteOK · Dice
  └── Coverage: effectively unlimited
```

The same posting can appear in both tiers (e.g., a Stripe job found via JSearch AND via the Greenhouse direct monitor). Cross-source deduplication (§6) ensures you only get one SMS.

### 5.2 Domain-based keyword search (not title matching)

The agent searches using broad domain-level keyword combinations across all Tier 2 sources. The AI scorer handles fine-grained relevance filtering — the search layer casts a wide net.

**Queries run in parallel across all sources:**
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

**Filters applied at the search layer** (before AI scoring):
- Country: United States
- Date posted: last 24 hours (catches fresh postings only)
- Job type: internship / part-time / contract (exclude full-time)
- Keywords excluded: anything in `profile.yaml → preferences.keywords_excluded`

**Filters applied by AI scorer** (after fetching):
- Relevance to user's actual skill set
- Seniority level appropriateness
- Degree requirements (no "PhD required" unless configured)

### 5.3 Tier 1 company career page search

For direct ATS monitoring, the agent searches each company's board for postings where `title ILIKE '%intern%'` — broad enough to catch "Internship", "Intern", "Co-op", "Student", "New Grad". The AI scorer validates relevance.

### 5.4 Why you don't need to list every company

Between all Tier 2 sources, the agent covers every company posting on LinkedIn, Indeed, ZipRecruiter, Glassdoor, Wellfound, Dice, RemoteOK, or HN. That is the vast majority of tech internship postings worldwide. The Tier 1 list is only needed for companies where you want faster notification or where the company doesn't post publicly on any board.

---

## 6. Cross-Source Deduplication

This is the mechanism that ensures the same job posting found on multiple platforms results in exactly one SMS alert.

### 6.1 The problem

The same Stripe internship might appear as:
- A JSearch result (sourced from LinkedIn)
- An Adzuna result
- A Greenhouse direct result (from `boards.greenhouse.io/stripe/jobs/12345`)

Without cross-source dedup, you'd get 3 SMS alerts for one job.

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
    source              TEXT NOT NULL,      -- 'jsearch', 'adzuna', 'wellfound', 'yc', 'hn', 'remoteok', 'dice', 'greenhouse:stripe', etc.
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
    match_score         INTEGER,            -- 0–100; NULL = not yet scored
    match_reasoning     TEXT,
    profile_hash        TEXT,               -- hash of profile.cache.json at scoring time
    notified            BOOLEAN DEFAULT FALSE,
    notified_at         DATETIME,
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
    phone_number    TEXT NOT NULL,         -- redacted in logs
    message         TEXT NOT NULL,
    twilio_sid      TEXT,
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

Score 0–100. Return JSON array with `external_id`, `score`, `reasoning` (1–2 sentences).

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

### 9.1 Notification modes

The notifier operates in two modes per polling cycle depending on how many matches are found:

**Individual mode** (< `BURST_THRESHOLD` matches, default 5): one SMS per match.

```
[InternAgent] Stripe · Software Engineering Intern · Remote
Match: 88 — Strong Python/backend fit, welcoming undergrads
Apply: https://boards.greenhouse.io/stripe/jobs/123456
```

**Burst mode** (≥ `BURST_THRESHOLD` matches): a single summary SMS listing all matches.

```
[InternAgent] 12 new matches this cycle
 1. Stripe · SWE Intern · Remote (92)
 2. Anthropic · ML Intern · SF (89)
 3. Scale AI · Data Eng Intern (85)
 + 9 more — run `db stats` to see all
```

The full list is always stored in the database. `db stats` shows everything regardless of which mode was used. `BURST_THRESHOLD` is configurable via env var.

### 9.2 Real-time SMS format (individual mode)

```
[InternAgent] Stripe · Software Engineering Intern · Remote
Match: 88 — Strong Python/backend fit, welcoming undergrads
Apply: https://boards.greenhouse.io/stripe/jobs/123456
```

Targets ≤480 chars (3 SMS segments). URL is always included verbatim; reasoning is truncated if needed.

### 9.3 Weekly digest SMS format

```
[InternAgent] Weekly Summary — Sun May 10
This week: 47 new postings, 6 alerts sent
Top match: Stripe SWE Intern (92/100)
⚠ Not found: Acme Corp, Widgets Inc
```

### 9.4 Twilio delivery

- Python `twilio` SDK
- Stores `MessageSid`; polls for delivery status 2 minutes after send
- On failure: retries once after 5 minutes; logs permanent failure
- Phone number redacted to last 4 digits in all logs

---

## 10. Scheduling & Orchestration

### 10.1 Scheduler

- `APScheduler` with `IntervalTrigger(minutes=RUN_INTERVAL_MINUTES)` (default: 30)
- `max_instances=1` prevents overlapping runs
- Weekly digest sent via a separate `CronTrigger(day_of_week='sun', hour=9)`

### 10.2 Run lifecycle

```
startup
  └── load .env
  └── load profile.cache.json + profile.yaml
  └── validate all credentials
  └── check for resume file changes → warn if detected
  └── start APScheduler

each scheduled run:
  └── for each source (parallel):
        └── fetch postings (broad keyword search)
        └── retry on failure (3x, exponential back-off)
  └── deduplicate against DB (insert new, skip known)
  └── batch-score all NULL-score postings (groups of 10 via Claude)
  └── for each score >= min_score AND notified=FALSE:
        └── send SMS
        └── mark notified=TRUE
  └── check if profile hash changed → trigger re-score pass
  └── write run_log entry (JSON)

weekly (Sunday 9am):
  └── compile weekly stats
  └── fetch unresolved companies list
  └── send digest SMS
  └── re-attempt discovery for unresolved companies
```

---

## 11. Deployment (Railway)

```
InternshipAgent on Railway
  ├── Worker process: python -m src.main
  ├── SQLite on Railway persistent volume (or Postgres plugin)
  └── profile.cache.json committed to repo — no PDFs on server
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
3. git add profile.cache.json && git commit -m "update profile" && git push
4. Railway auto-redeploys (~30s)
```

---

## 12. Security

- All secrets in environment variables; `.env` in `.gitignore`
- `/resumes/` in `.gitignore`; PDFs never leave local machine or Anthropic API
- `profile.cache.json` contains only extracted skills/experience — no contact info, no address
- Phone number logged as `****XXXX` only
- Database permissions set to `600` on Linux

---

## 13. Key Dependencies

| Package | Purpose |
|---|---|
| `anthropic` | Claude API (resume extraction + job scoring) |
| `pdfplumber` | PDF text extraction |
| `feedparser` | Indeed RSS feed parsing |
| `httpx` | Async HTTP for API calls |
| `playwright` | Headless browser for custom company scrapers |
| `sqlalchemy` | ORM + DB abstraction |
| `alembic` | Schema migrations |
| `twilio` | SMS |
| `apscheduler` | In-process job scheduler |
| `pydantic` / `pydantic-settings` | Config validation |
| `pyyaml` | profile.yaml parsing |
| `tenacity` | Retry with exponential back-off |
| `structlog` | Structured JSON logging |
| `mypy` | Static type checking |
| `pytest` | Tests |
