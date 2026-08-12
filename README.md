# InternshipAgent

An autonomous agent that continuously monitors job boards and company career pages for new internship postings, scores them against your resume using Claude AI, and messages you on Telegram the moment a strong match appears.

## What it does

1. **Searches broadly** across Indeed, RemoteOK, HackerNews, Adzuna, LinkedIn/Glassdoor/ZipRecruiter (via JSearch, if configured), and targeted company career pages using domain-level keywords (software engineering, data science, machine learning, AI) — not exact title matching. The AI handles relevance filtering.
2. **Discovers company career pages automatically** — you provide company names in plain English; the agent finds their ATS (Greenhouse, Lever, Workday, etc.) and monitors their job boards directly.
3. **Reads your resume** — drop a PDF (or multiple) into the `/resumes` folder; Claude extracts your skills, experience, and projects into a unified profile automatically. No manual YAML editing.
4. **Deduplicates** postings so you never get the same alert twice.
5. **Scores** each new posting 0–100 against your extracted profile — there is no minimum-score gate.
6. **Messages you** on Telegram for every scored posting, with company name, role, match score, missing qualifications, and a direct application link — so you never have to guess what didn't make the cut.

## Architecture at a glance

```
/resumes/*.pdf  ──► Resume Extractor (Claude) ──► profile.cache.json (local)
                                                          │
                                             encoded into PROFILE_CACHE env var
                                                          │ (Railway reads this)
                                                          ▼
config/companies.yaml ──► ATS Discoverer ──► per-company ATS scrapers (Tier 1)
                                                          │
      Indeed RSS · Adzuna · YC ────────────────────────── ┤  (Tier 2)
      HackerNews · RemoteOK · Dice · JSearch (LinkedIn)   │
                                                          ▼
                                              Cross-source Deduplicator
                                                          │
                                                   AI Scorer (Claude)
                                                          │
                                    < BURST_THRESHOLD matches → individual messages
                                    >= BURST_THRESHOLD matches → batched summary message
                                                          │
                                                Telegram Bot API ──► your phone
```

Full design in [docs/design.md](docs/design.md).

## Prerequisites

- Python 3.11+
- A [Telegram](https://telegram.org) account and a bot token from [@BotFather](https://t.me/BotFather) (free, no business verification required)
- An [Anthropic](https://console.anthropic.com) API key
- An [Adzuna](https://developer.adzuna.com) API key (free)
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
# Edit .env with your API keys and Telegram bot token/chat ID

# 5. Add your resume
mkdir resumes
cp ~/path/to/your-resume.pdf resumes/

# 6. Extract your profile from the resume
python -m src.main --rebuild-profile
# This generates profile.cache.json — commit this file (not the PDF)

# 7. Edit your preferences (location, excluded keywords)
# See profile.yaml — this is the only file you manually edit
nano profile.yaml

# 8. Initialize the database
python -m src.db init

# 9. Run once to verify everything works
python -m src.main --run-once --dry-run

# 10. Start the scheduler (runs every 60 minutes)
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
  digest_enabled: true # send a summary message every hour in addition to real-time alerts
  max_posting_age_days: 7  # skip postings older than this (by posted_at); no reliable posted_at → kept
```

**No minimum score:** every posting the scrapers pick up in the software/AI/data categories gets a Telegram message with its score, the AI's reasoning, and any `missing_qualifications` (specific skills/tools/requirements the posting names that your profile doesn't show) — so you decide what's worth applying to, rather than the agent silently dropping anything it scores as a weak fit. A 15/100 posting and a 95/100 posting both get a message; only the content differs. This trades message volume for completeness — if it gets noisy, `BURST_THRESHOLD` (see below) automatically switches a busy cycle to one batched digest message instead of one-per-posting.

## Marking postings as applied, and pausing notifications

Every individual-mode alert carries a **"✅ Mark Applied" button**. Tap it and the posting is permanently excluded from future alerts, even if a later profile update would otherwise change its score — a belt-and-suspenders safety net on top of the agent's own deduplication.

Send **`/pause`** to the bot at any time to stop receiving messages, and **`/resume`** to turn them back on. This is a real kill switch, not Telegram's own per-chat mute: while paused, the agent keeps polling, deduping, and scoring underneath — matches just wait unsent until you resume, so nothing found while paused is lost. Both the button and these commands only work from your configured `TELEGRAM_CHAT_ID`; messages from any other chat are ignored.

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

**Important:** because `companies.yaml` is gitignored, it never reaches the deployed container on its own — a `git push` alone will not update the companies the live agent watches. After editing it, run:

```bash
python -m src.main --sync-companies
# prints: railway variables set COMPANIES_CONFIG="<base64>"
```

and run the printed command to push the updated list to Railway (mirrors how `--rebuild-profile` pushes `PROFILE_CACHE`). Locally, `python -m src.main` reads `config/companies.yaml` directly and this step isn't needed.

If a company can't be found automatically it is logged as unresolved. Check with:

```bash
python -m src.db stats
# Shows: postings found/scored/alerted, unresolved companies (by name), estimated API cost
```

Unresolved companies are also included in your hourly digest message.

## How the agent covers companies you haven't listed

You don't need to list every company. The agent uses a **two-tier strategy**:

| Tier | Sources | What it covers | Speed |
|---|---|---|---|
| **Tier 1** — Direct monitoring | Greenhouse · Lever · Custom scrapers | ~150 companies in your `companies.yaml`, checked at their career page directly | Fast: within one poll cycle |
| **Tier 2** — Broad search | Indeed RSS · Adzuna · HackerNews · RemoteOK · JSearch | Every company posting on Indeed, RemoteOK, HN's monthly hiring thread, or — via JSearch — LinkedIn, Glassdoor, and ZipRecruiter | Slightly slower: depends on board indexing |

**LinkedIn coverage:** direct LinkedIn scraping isn't done — LinkedIn actively blocks it and prohibits it in their Terms of Service. **JSearch** (RapidAPI) is the only source that reaches LinkedIn postings, via a legitimate metered API rather than scraping. It requires a `JSEARCH_API_KEY` (see Environment variables below) and polls on its own job every 4 hours (~180 requests/month, sized for a 200/month free-tier plan — adjust `IntervalTrigger(hours=4)` in `main.py` if your plan's quota differs). Without a key set, this source is silently skipped — everything else still runs.

**Note:** the YC and Dice scrapers exist in `src/scrapers/` but aren't wired into the poll schedule yet (`_SOURCES` in `main.py`) — they're dormant, not actively polling, despite appearing in earlier design docs.

**Cross-source deduplication** ensures that if the same posting appears on multiple platforms (e.g., a Stripe job found via Indeed RSS *and* via the Greenhouse direct monitor), you get exactly one alert. The agent deduplicates by canonical application URL, then by company + title as a fallback.

You only need to list companies in Tier 1 where you want the fastest possible alert, or where the company doesn't post publicly on any board (e.g., some quant firms).

## Environment variables

See `.env.example` for all required variables. Key ones:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot's token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID to receive alerts (find via `getUpdates`, see `.env.example`) |
| `ANTHROPIC_API_KEY` | Claude API key for resume extraction + job scoring |
| `PROFILE_CACHE` | Base64-encoded `profile.cache.json` — set via `--rebuild-profile` output |
| `COMPANIES_CONFIG` | Base64-encoded `config/companies.yaml` — set via `--sync-companies` output; required for Tier 1 monitoring in production, since `companies.yaml` is gitignored |
| `ADZUNA_APP_ID` | Adzuna API app ID |
| `ADZUNA_APP_KEY` | Adzuna API key |
| `JSEARCH_API_KEY` | RapidAPI JSearch key — the only source that reaches LinkedIn postings |
| `SEARCH_API_KEY` | SerpAPI key — used only for company ATS discovery, not job searching |
| `DATABASE_URL` | SQLite path or Postgres URL |
| `RUN_INTERVAL_MINUTES` | How often to poll (default: `60`) |
| `BURST_THRESHOLD` | Min postings in one cycle to trigger a batched message instead of individual ones (default: `20`) |

## CLI reference

```bash
python -m src.main                      # start scheduler (runs every 60 min)
python -m src.main --run-once           # single run, then exit
python -m src.main --dry-run            # full pipeline but no message sent
python -m src.main --rebuild-profile    # re-extract profile from /resumes PDFs
python -m src.main --sync-companies     # re-encode config/companies.yaml for Railway
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
    ├── notifier.py               # Telegram Bot API (individual + burst batching)
    ├── data/
    │   └── company_ats_map.json  # bundled ATS lookup table
    ├── scrapers/
    │   ├── base.py
    │   ├── indeed_rss.py         # Indeed RSS feed (free)
    │   ├── adzuna.py
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
