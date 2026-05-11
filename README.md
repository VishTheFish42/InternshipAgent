# InternshipAgent

An autonomous agent that continuously monitors job boards and company career pages for new internship postings, scores them against your resume using Claude AI, and texts you the moment a strong match appears.

## What it does

1. **Searches broadly** across LinkedIn, Indeed, ZipRecruiter, Handshake, and targeted company career pages using domain-level keywords (software engineering, data science, machine learning, AI) — not exact title matching. The AI handles relevance filtering.
2. **Discovers company career pages automatically** — you provide company names in plain English; the agent finds their ATS (Greenhouse, Lever, Workday, etc.) and monitors their job boards directly.
3. **Reads your resume** — drop a PDF (or multiple) into the `/resumes` folder; Claude extracts your skills, experience, and projects into a unified profile automatically. No manual YAML editing.
4. **Deduplicates** postings so you never get the same alert twice.
5. **Scores** each new posting 0–100 against your extracted profile and only alerts when the match exceeds your threshold.
6. **Texts you** via SMS with company name, role, match score, and a direct application link.

## Architecture at a glance

```
/resumes/*.pdf  ──► Resume Extractor (Claude) ──► profile.cache.json (local)
                                                          │
                                             encoded into PROFILE_CACHE env var
                                                          │ (Railway reads this)
                                                          ▼
config/companies.yaml ──► ATS Discoverer ──► per-company ATS scrapers (Tier 1)
                                                          │
  Indeed RSS · Adzuna · Wellfound · YC ─────────────────── ┤  (Tier 2)
  HackerNews · RemoteOK · Dice                            │
                                                          ▼
                                              Cross-source Deduplicator
                                                          │
                                                   AI Scorer (Claude)
                                                          │
                                          1 match → individual SMS
                                          5+ matches → batched summary SMS
                                                          │
                                                   Twilio ──► your phone
```

Full design in [docs/design.md](docs/design.md).

## Prerequisites

- Python 3.11+
- A [Twilio](https://www.twilio.com) account (free trial is enough to start)
- An [Anthropic](https://console.anthropic.com) API key
- An [Adzuna](https://developer.adzuna.com) API key (free)
- A [Wellfound](https://wellfound.com) developer account (free)
- A [SerpAPI](https://serpapi.com) key (free tier: 100 searches/month — used only for company discovery, not job searching)

## Quick start

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd InternshipAgent

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
playwright install chromium    # for custom scrapers

# 4. Copy and fill in credentials
cp .env.example .env
# Edit .env with your API keys and phone number

# 5. Add your resume
mkdir resumes
cp ~/path/to/your-resume.pdf resumes/

# 6. Extract your profile from the resume
python -m src.main --rebuild-profile
# This generates profile.cache.json — commit this file (not the PDF)

# 7. Edit your preferences (location, threshold, excluded keywords)
# See profile.yaml — this is the only file you manually edit
nano profile.yaml

# 8. Initialize the database
python -m src.db init

# 9. Run once to verify everything works
python -m src.main --run-once --dry-run

# 10. Start the scheduler (runs every 30 minutes)
python -m src.main
```

## Adding your resume

Drop any number of PDF resumes into the `/resumes` folder:

```
resumes/
├── resume-swe.pdf
├── resume-ml.pdf
└── resume-general.pdf
```

Then run:

```bash
python -m src.main --rebuild-profile
```

Claude reads all PDFs, extracts skills/projects/experience, deduplicates across versions, and writes `profile.cache.json` locally. Because the repo is public, this file is gitignored — it's pushed to Railway as an environment variable instead.

After running `--rebuild-profile`, the CLI prints the exact Railway command to update your profile on the server:

```
Profile rebuilt. To deploy to Railway:
  railway variables set PROFILE_CACHE="<encoded string>"
```

**When to re-run:** any time you update or add a resume PDF. The agent detects file changes on each run and warns in the logs if it finds a modified PDF that hasn't been rebuilt.

## Configuring preferences

`profile.yaml` is the only file you need to edit manually. It contains preferences — not skills (those come from your resume automatically).

```yaml
preferences:
  locations: ["United States"]
  remote_ok: true
  on_site_ok: true
  # Postings containing these words are always excluded:
  keywords_excluded:
    - "senior"
    - "staff"
    - "principal"
    - "security clearance required"
    - "PhD required"

matching:
  min_score: 70        # 0–100; only alert if Claude scores >= this
  weekly_digest: true  # send a weekly SMS summary (Sunday 9am) in addition to real-time alerts
```

## Adding companies to monitor

Edit `config/companies.yaml` (which is **gitignored** — your targets stay private) with plain company names:

```yaml
companies:
  - Google
  - OpenAI
  - Stripe
  - Figma
```

No URLs, slugs, or ATS knowledge required. The agent discovers each company's career page automatically. A starter list of ~150 companies across big tech, AI, fintech, quant, SaaS, hardware, and defense is pre-populated in your local `companies.yaml`. Add or remove names freely.

`config/companies.example.yaml` is the committed placeholder — it shows the format but contains no real targets.

If a company can't be found automatically it is logged as unresolved. Check with:

```bash
python -m src.db stats
# Shows: postings found/scored/alerted, unresolved companies (by name), estimated API cost
```

Unresolved companies are also included in your weekly digest SMS.

## How the agent covers companies you haven't listed

You don't need to list every company. The agent uses a **two-tier strategy**:

| Tier | Sources | What it covers | Speed |
|---|---|---|---|
| **Tier 1** — Direct monitoring | Greenhouse · Lever · Custom scrapers | ~150 companies in your `companies.yaml`, checked at their career page directly | Fast: within one poll cycle |
| **Tier 2** — Broad search | JSearch · Adzuna · Wellfound · YC · HackerNews · RemoteOK · Dice | Every company posting on LinkedIn, Indeed, ZipRecruiter, Glassdoor, Wellfound, Dice, RemoteOK, or HN | Slightly slower: depends on board indexing |

**Cross-source deduplication** ensures that if the same posting appears on multiple platforms (e.g., a Stripe job found via JSearch *and* via the Greenhouse direct monitor), you get exactly one SMS. The agent deduplicates by canonical application URL, then by company + title as a fallback.

You only need to list companies in Tier 1 where you want the fastest possible alert, or where the company doesn't post publicly on any board (e.g., some quant firms).

## Environment variables

See `.env.example` for all required variables. Key ones:

| Variable | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Your Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Your Twilio Auth Token |
| `TWILIO_FROM_NUMBER` | Your Twilio phone number |
| `ALERT_PHONE_NUMBER` | Your personal phone number to receive SMS |
| `ANTHROPIC_API_KEY` | Claude API key for resume extraction + job scoring |
| `PROFILE_CACHE` | Base64-encoded `profile.cache.json` — set via `--rebuild-profile` output |
| `ADZUNA_APP_ID` | Adzuna API app ID |
| `ADZUNA_APP_KEY` | Adzuna API key |
| `WELLFOUND_API_KEY` | Wellfound (AngelList) API key — startup coverage |
| `SEARCH_API_KEY` | SerpAPI key — used only for company ATS discovery, not job searching |
| `DATABASE_URL` | SQLite path or Postgres URL |
| `RUN_INTERVAL_MINUTES` | How often to poll (default: `30`) |
| `BURST_THRESHOLD` | Min matches in one cycle to trigger a batched SMS instead of individual ones (default: `5`) |

## CLI reference

```bash
python -m src.main                      # start scheduler (runs every 30 min)
python -m src.main --run-once           # single run, then exit
python -m src.main --dry-run            # full pipeline but no SMS sent
python -m src.main --rebuild-profile    # re-extract profile from /resumes PDFs
python -m src.main --rescore            # re-score all stored postings vs. current profile
python -m src.db init                   # initialize database schema
python -m src.db stats                  # print summary: postings, alerts, unresolved companies, cost
```

## Deployment (Railway)

```bash
npm install -g @railway/cli
railway login
railway init
# Set env vars in Railway dashboard (never commit .env)
railway up
```

The agent runs as a persistent worker process on Railway. Your profile is loaded from the `PROFILE_CACHE` environment variable — no PDFs or extracted profiles are ever committed to the public repo.

## Project structure

```
InternshipAgent/
├── README.md
├── profile.yaml                  # preferences only — committed (no personal data)
├── profile.cache.json            # gitignored — generated locally, deployed via env var
├── .env.example
├── requirements.txt
├── Procfile
├── railway.toml
├── resumes/                      # gitignored — add your PDFs here
│   └── your-resume.pdf
├── config/
│   ├── companies.yaml            # gitignored — your private target list (~150 companies)
│   └── companies.example.yaml    # committed — format reference only
├── docs/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
└── src/
    ├── main.py                   # entry point + scheduler
    ├── db.py                     # database models
    ├── config.py                 # env var loading + validation
    ├── resume_extractor.py       # PDF → profile.cache.json via Claude
    ├── company_discoverer.py     # company name → ATS slug/URL
    ├── deduplicator.py           # cross-source dedup logic
    ├── matcher.py                # AI scoring
    ├── notifier.py               # Twilio SMS (individual + burst batching)
    ├── data/
    │   └── company_ats_map.json  # bundled ATS lookup table
    ├── scrapers/
    │   ├── base.py
    │   ├── indeed_rss.py         # Indeed RSS feed (free)
    │   ├── adzuna.py
    │   ├── wellfound.py
    │   ├── yc.py                 # YC Work at a Startup
    │   ├── hn.py                 # HackerNews Who's Hiring
    │   ├── remoteok.py
    │   ├── dice.py
    │   ├── greenhouse.py
    │   ├── lever.py
    │   └── custom/               # Google, Meta, Microsoft scrapers
    └── tests/
```

## Docs

- [Requirements](docs/requirements.md)
- [Design](docs/design.md)
- [Task list](docs/tasks.md)
