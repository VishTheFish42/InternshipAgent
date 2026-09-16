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
Step 2: Guess a slug from the company name, verify it directly against
        Greenhouse's and Lever's own public APIs — no search API key
        needed, spends zero SerpAPI quota            [IMPLEMENTED, SerpAPI-usage-reduction]
   │  hit → ats_type="greenhouse"|"lever", source="guessed_verified"
   │  miss ↓
   ▼
Step 3: One combined web search across all three ATS site: filters —
        "{company} (site:boards.greenhouse.io OR site:jobs.lever.co
         OR site:myworkdayjobs.com)"          [IMPLEMENTED, was 3 separate queries]
   │  hit → extract ats_type + slug from the matched URL, cache in DB
   │  miss ↓
   ▼
Step 4: Web search "{company} careers internship site:careers.{company}.com"
   │  hit → ats_type="llm_scrape", store the URL             [IMPLEMENTED 2026-08-20, T-309]
   │  miss ↓
   ▼
Step 5: Mark as "unresolved" in DB; retried automatically — priority
        companies every few hours, everything else weekly (see the
        SerpAPI-usage-reduction entry below) — no message is pushed about
        this, check `python -m src.db stats` for the current list
```

Step 2's guess-and-verify runs even when no `search_api_key` is configured at all — it's the only step in this pipeline that needs neither a bundled-table entry nor a search API key, since it queries Greenhouse/Lever directly. It's skipped only when the bundled table already confirms a company is on Workday (guessing an ATS already known to be wrong wastes two real HTTP calls for nothing).

**Coverage guarantee (§3.5, NOT YET IMPLEMENTED):** today, step 4 (previously step 3) resolves a company to `ats_type="custom"` and stores a URL, but no scraper ever reads that URL — the company is marked `resolved` and then never actually polled again. This is a real bug, not a hypothetical: an NVIDIA SWE internship posted 2026-08-19 never reached Telegram because `Nvidia` resolves to `ats_type="custom"` in the bundled table with no scraper behind it, and 25 other companies in a real `companies.yaml` (Google, Meta, Microsoft, Apple, Amazon, Netflix, Tesla, Intel, AMD, Uber, and more) are in the identical dead-end state. §3.5 fixes this by making resolution and scrapability the same step — see below.

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

The `"scraper"` key above was aspirational — it was never read by any code path, and `ats: "custom"` entries are dead ends today (§3.1, §3.5). Rollout step 5 of §3.5 re-resolves every `custom` entry so most become `workday`; whatever's left gets `ats_type="llm_scrape"` instead, which — unlike `custom` — has a real scraper reading it.

### 3.3 Web search for discovery

Uses the SerpAPI or Google Custom Search API. Query: `"{company name}" internship site:boards.greenhouse.io`. Extracts the slug from the first result URL. Rate-limited to avoid excessive queries; results cached permanently in the `company_lookup` DB table.

**Rate limiting and failure logging — IMPLEMENTED 2026-08-20.** This section's "rate-limited" claim was aspirational, not actually true, until this date — `seed_companies()`'s and `retry_unresolved()`'s loops over companies had no throttling at all between `discover()` calls, and neither did `discover()`'s own internal cascade across up to four sequential SerpAPI calls (Greenhouse, Lever, Workday, careers) for a single company that misses every step. This wasn't hypothetical: the original 349-company seed (`docs/tasks.md`, 2026-08-15) genuinely 429'd SerpAPI's own rate limit from firing queries with zero delay. Fixed centrally in `_serpapi()` (`company_discoverer.py`) via a `_RateLimiter` enforcing a minimum interval (1 request/second, a conservative default — tune to your actual SerpAPI plan) between *every* SerpAPI call regardless of which loop or cascade step it came from — one fix point instead of threading sleeps through multiple call sites separately.

Separately, and more consequentially: `_serpapi()` previously caught every failure with a bare `except Exception: return []` — no logging at all, not even at debug level. A full SerpAPI outage (rate limit, or the account's monthly quota exhausted entirely) was completely invisible in application logs; the only way to notice it was to query SerpAPI directly, which is how it was actually found this session. Now logs a warning on every failure, including the HTTP status code and response body for an `HTTPStatusError` (e.g. `429`), so the failure reason is at least in the logs going forward. `retry_unresolved_companies()` (`main.py`) additionally escalates its own hourly summary line from INFO to WARNING specifically when a non-trivial batch (`attempted > 0`) resolves nothing at all (`resolved == 0`) — that shape, repeated hour after hour, is the actual signature of a sustained outage, distinct from the normal noise of a single 0-resolved hour.

**Two-tier retry cooldown for priority companies — IMPLEMENTED 2026-08-20 (T-312).** Even with rate limiting in place, SerpAPI's free tier is a hard constraint that scheduling alone can't work around: 250 searches/month (confirmed live from the account's own `/account` endpoint — `plan_id: "free"`, `searches_per_month: 250`), and a single company's worst-case discovery cascade costs up to 4 of those. The old single 1-hour cooldown on `retry_unresolved()` assumed retries were nearly free; against a real ~200-company unresolved backlog it could burn a month's entire budget on the very first eligible hourly batch — which is close to what actually happened (the account was already fully exhausted, `plan_searches_left: 0`, when this was investigated).

`retry_unresolved()` now takes two cooldowns instead of one: `priority_min_age_hours` (default 4h) for companies named in `config/companies.yaml`'s new optional `priority_companies:` list, and a far looser `default_min_age_hours` for everything else. The intent isn't more total throughput — the 250/month ceiling is fixed regardless of scheduling — it's *allocation*: a company the user has explicitly flagged as more important gets first claim on whatever quota exists, checked several times a day instead of once every 24 hours, while the long tail of the watchlist backs off to a cadence that actually lets the monthly budget survive the month rather than being spent in the first day. A `max_attempts_per_call` cap (default 20) enforces this concretely: when both priority and non-priority companies are past their respective cooldowns in the same cycle, priority companies are selected into the capped batch first, so a large non-priority backlog can never crowd them out — and no single retry cycle can itself exhaust the whole monthly budget in one shot, the way the old uncapped hourly retry effectively could.

`priority_companies` is opt-in and empty by default — omitting it changes nothing. It doesn't need to be a subset of `companies:`; `retry_unresolved()` only ever acts on companies already seeded into `company_lookup`, so a name here that was never added to the watchlist is simply inert, not an error.

**SerpAPI usage reduction — IMPLEMENTED.** T-312's 24h default cooldown turned out not to be enough: the account was found fully exhausted again a month after its 2026-09-09 reset, confirmed live via `/account.json` (`plan_searches_left: 0`, `plan_renewal_date: 2026-10-09`). At 250 searches/month (~8/day), even a day-scale cooldown against a 100+ company unresolved backlog is mathematically guaranteed to burn the whole month's budget in the first few days — the fix needed to reduce actual demand on the quota, not just reschedule it. Three changes, together:

1. **Direct guess-and-verify (§3.1 Step 2, `_guess_greenhouse_or_lever()`).** Both Greenhouse and Lever key a company's board by a plain slug and expose it via a free, unauthenticated public API (`boards-api.greenhouse.io`, `api.lever.co`) — the same APIs `src/scrapers/greenhouse.py`/`lever.py` already fetch postings from once resolved. `_slug_candidates()` generates 1-2 plausible slugs from the company name (concatenated and hyphenated lowercase forms); each is verified with a direct HTTP call before ever spending a SerpAPI query. This is the single biggest reduction: most companies that resolve at all resolve to Greenhouse or Lever, so this step removes the *need* for search entirely for the common case, rather than making the search cheaper.
2. **Combined single-query search (§3.1 Step 3, `_search_ats_combined()`).** For companies the guess step misses, the three separate `site:` searches (Greenhouse, then Lever, then Workday) are now one query — `"{company}" (site:boards.greenhouse.io OR site:jobs.lever.co OR site:myworkdayjobs.com)` — with the ATS type inferred from which domain the matched result URL belongs to. Cuts the worst-case discovery cascade from up to 4 SerpAPI calls per company down to 2 (this combined query, plus the generic-careers fallback if it also misses).
3. **Week-scale default retry cooldown.** `default_min_age_hours` changed from 24 to 168 (T-312 already added the parameter; this changes its default). Even with 1 and 2 above sharply reducing per-company demand, a backlog can still exceed a week's worth of an 8/day budget on its own — a daily cooldown was still structurally guaranteed to exhaust the month early. `priority_min_age_hours` (4h) is unchanged, so companies the user actually flagged as important still get a same-day shot at resolving.

Together, 1 and 2 mean most companies never spend a search at all, and the ones that do spend at most half of what they used to — the actual quota-usage reduction. 3 is the backstop for whatever demand remains against a fixed, small monthly budget.

### 3.4 Warning surfacing for unresolved companies

Unresolved companies are surfaced via `python -m src.db stats`, which prints a section: `Unresolved companies (N): [list]`. There used to also be a hourly digest message repeating this list; it was removed (§9, §10.1) since it mostly duplicated `db stats` — unresolved companies are still retried automatically in the background, just without a pushed notification about it.

Note this only covers the `unresolved` status. It does not (and, before §3.5, could not) surface the more dangerous state a company like Nvidia was actually in: `resolved`, with a scraper that doesn't exist. `db stats` reported nothing wrong because nothing about that state looked wrong from the DB's point of view.

### 3.5 Scraper tiers and the coverage guarantee — IMPLEMENTED (Tier A + Tier B; Tier C deferred)

Only two of the `ats_type` values a company can resolve to (§7.2) had a scraper actually wired into `_SOURCES` in `main.py` before this section's Tier A landed: `greenhouse` and `lever`. Everything else — `workday`, `custom`, and any company that falls through to a generic careers-page URL — resolved successfully and then was never fetched again. This section defines the fix: every `ats_type` a company can resolve to must correspond to a registered, working scraper, with no third option.

**Tier A — known JSON-API ATS (`greenhouse`, `lever`, `workday`) — IMPLEMENTED 2026-08-20 (`docs/tasks.md` T-470/T-471).** Same shape as Greenhouse/Lever (`src/scrapers/greenhouse.py`, `src/scrapers/lever.py`): one generic scraper per ATS, parameterized by the resolved slug, zero per-company code. `src/scrapers/workday.py` covers every Workday-hosted company at once, driven by a `"{tenant}.{instance}/{site}"` slug (e.g. `"intel.wd1/External"`) stored in `CompanyLookup.slug`, POSTing to Workday's uniform `/wday/cxs/{tenant}/{site}/jobs` search endpoint — same JSON shape across every tenant. Two queries per company per poll (`searchText="intern"` and `searchText="co-op"`), each still filtered client-side by title against a word-boundary `intern`/`co-op` regex — necessary, not just consistent-for-its-own-sake: Workday's own search match is loose enough that querying `"intern"` live against Adobe's board returned "Internal Product Manager 5", and `"co-op"` against Intel's returned "Agentic AI Customer Co-Val CG". Only the first page (20 results) per query is fetched — a company with more open intern/co-op roles than fit across both first pages is a documented gap, not a silent one. `postedOn`'s relative-date strings (`"Posted Today"` / `"Posted N Days Ago"` / `"Posted 30+ Days Ago"`) are parsed to an absolute datetime for the freshness filter (§3.1's `max_posting_age_days`); the response has no full job description, so `RawPosting.description` is `None` for this source — the AI scorer works from the title alone here, a known trade-off against doubling request volume with a second per-posting fetch.

Live-verified against real companies before shipping (not just mocked unit tests): Intel, Adobe, and Salesforce's actual Workday boards, and — directly closing the incident this section exists because of — Nvidia's. `Nvidia` was hand-corrected in `company_ats_map.json` from the dead-end `{"ats": "custom"}` to `{"ats": "workday", "slug": "nvidia.wd5/NVIDIAExternalCareerSite"}`, confirmed live: `nvidia.wd5.myworkdayjobs.com` is a real, working Workday tenant, and a live `fetch_for_resolved_companies()` call against it returned 33 real postings including "Software Engineering Intern, NCCL - 2026," posted 2026-08-18 — one day before the missed alert this whole investigation started from. Adobe and Salesforce were verified the same way and corrected in the bundled table too. This was a hand-verified, four-company spot-fix at the time — the general mechanism, discovery Step 3's Workday web search (§3.1, T-308), is now also implemented, so any *new* company a user adds that happens to be on Workday resolves automatically without needing the same manual `curl`-and-edit treatment.

**A real limit on what discovery-side Workday detection can fix, checked live rather than assumed:** none of Google's, Meta's, Microsoft's, Apple's, Amazon's, Netflix's, Tesla's, or Uber's real career pages redirect to `myworkdayjobs.com` — they're all on fully bespoke in-house platforms, confirmed by following each one's actual careers URL. So Step 3's Workday search correctly finds nothing for any of them and falls through to the generic-careers step every time; it was never going to close that specific part of the original 26-company gap, only Tier B (below) can. What Step 3 *does* reach: the companies already correctly tagged `ats: "workday"` in the bundled table but missing a slug (Cisco, Qualcomm, Broadcom, VMware, PayPal, ServiceNow, DocuSign, Lockheed Martin, Northrop Grumman — T-310 still needs to force-re-resolve these to pick up a real slug) and any company outside the bundled table entirely that turns out to be on Workday. A live end-to-end test against a real, not-yet-mapped Workday company (Micron Technology, confirmed independently via `curl` to be on Workday) hit an external limitation rather than a code issue: the project's `SEARCH_API_KEY` (SerpAPI) account has exhausted its monthly search quota, so the live search call itself couldn't be exercised this session — `discover()` correctly fell through to `unresolved` rather than erroring, which is itself the intended graceful-degradation behavior (NFR-01), but it means true live confirmation is still pending a quota reset.

**Tier B — LLM page-scrape fallback (`ats_type="llm_scrape"`, replaces the dead-end `custom`) — IMPLEMENTED 2026-08-20 (`docs/tasks.md` T-472–T-475, T-309).** For companies with a bespoke in-house careers page and no known JSON API: `src/scrapers/llm_scrape.py` fetches the resolved careers URL's HTML on its own 6-hourly job (T-473, separate from the main cycle — FR-71), converts it to Markdown-style linked text (`[visible text](absolute url)` — a small `html.parser.HTMLParser` subclass, not a new dependency; plain tag-stripping would discard every href, since links live in attributes, not visible text), and sends it to Claude (`settings.claude_extraction_model`, the same higher-fidelity model resume extraction uses, not the cheaper scoring model — this is a generative structured-extraction task over messy real-world text, not batch classification, and a hallucinated posting is worse than a mis-scored real one) with a fixed extraction prompt/schema (title, url, location, posted date where present) to produce structured postings, which then flow through the same dedup/scoring/notify pipeline as any other `RawPosting`. This is what makes "add any company and get real coverage" actually true for a general product — there is no company this tier can't attempt, at the cost of one page fetch + one LLM call per company per poll (cost tracked via its own `RunLog` row, NFR-22). Extracted postings still pass through the same word-boundary `intern`/`co-op` title regex workday.py uses as a deterministic backstop — an LLM extraction is even less constrained than Workday's own loose search, so trusting the prompt's instructions alone isn't enough.

**Live-verified against real, unfavorable pages before trusting it — not just mocked tests, and not assumed safe:** fed the pipeline three real pages chosen specifically because they were expected to be hard, not easy. (1) `jobs.netflix.com` — the classic Tier C case (see below): 135KB of raw HTML collapses to ~1,300 characters of real visible text (a JS-rendered SPA shell, marketing chrome only); Claude correctly returned an empty array rather than inventing postings from nav-menu text, at a cost of 4 output tokens. (2) A second Netflix domain the first page itself linked to (`explore.jobs.netflix.net/careers`) surfaced a *different*, previously unanticipated failure mode: a 20,000-character data blob sitting inside a `<code id="branding-data" style="display: none;">` element — hidden from real users via inline CSS, but not inside a `<script>` tag, so the extractor's tag-name-based skip list didn't catch it and it silently ate the entire per-page text budget. Still safe in outcome — Claude again returned zero postings rather than hallucinating from the JSON-shaped garbage — but it's a real, documented gap: pages that hide non-script "data island" content via inline `style="display:none"` rather than a `<script>` tag can crowd out the real content within the truncation window. A full CSS-visibility engine to catch every such case was deliberately not built — it's a materially bigger scope than a lightweight stdlib HTML-to-text pass is meant to cover, and the failure mode this session found is safe (empty result), not silently wrong (fabricated postings). (3) A Goldman Sachs internship program page returned real, substantial visible text (~8,000 characters) but zero postings — correct behavior, since the resolved URL was a program-overview marketing page, not an actual listings page; a discovery-quality issue (which URL `_search_careers` finds), not an extraction-quality one. Across all three, the one property that actually matters held: **never fabricate a posting that isn't there.**

**Tier C — headless render (`needs_render` flag on Tier B) — deferred.** Some bespoke careers pages are JS-rendered SPAs where a plain GET returns an near-empty shell — confirmed live against `jobs.netflix.com` itself during Tier B's own verification above, not hypothetical. Discovery can detect this (response body mostly script tags, negligible text) and set a `needs_render` flag so the Tier B poller uses a headless browser (e.g. Playwright, already an unused listed dependency from the original, superseded per-company-scraper plan — see Phase 4L in `docs/tasks.md`) instead of a plain HTTP GET for that company only. Worth noting for later: a real headless browser's rendered accessible-text output would also incidentally fix the `display:none` hidden-data-island failure mode found above, since browsers don't include CSS-hidden content in rendered text either — Tier C would likely resolve both gaps at once, not just the JS-shell one. Not needed to close the immediate coverage gap — deferred until real-world usage shows how often Tier B actually hits this rather than a working page.

**Cadence.** Tier A polls every main cycle, same as Greenhouse/Lever today. Tier B polls on its own slower, separate schedule — the same pattern already used for Adzuna and JSearch (§10.1) to bound cost/quota — since it costs a real LLM call per company per poll rather than a free JSON GET.

**The coverage guarantee, enforced — IMPLEMENTED 2026-08-20 (T-910, `tests/test_coverage_guarantee.py`).** Three tests assert: every main-cycle ats_type (`greenhouse`/`lever`/`workday`) has a registered fetch function in `_SOURCES`; `llm_scrape` is wired into its own poll job (`run_llm_scrape_poll`); and every `ats_type` value actually present in `company_ats_map.json` is either a live, working type or an explicitly-acknowledged legacy one — never an unrecognized typo or a new type someone adds without ever building a scraper for it. This is the regression guard for the exact class of bug that caused the missed Nvidia posting: it is asserted, not just conventionally true, that "resolved" always means "will actually be polled." It found a real gap on its very first run, before any legacy-type allowlist existed: Oracle's bundled entry is `{"ats": "taleo"}` — a *third* unscraped legacy value, distinct from `custom`, that hadn't been cataloged under the earlier "26 broken companies" audit because it isn't literally tagged `custom`. Added to the test's known-legacy set alongside `custom`, same status: acknowledged, not fixed.

**Rollout order — all steps complete as of 2026-08-20:** (1) ✅ Workday scraper + dispatch wiring — biggest coverage win, smallest new surface area; (2) ✅ discovery step 3 (Workday site-search, T-308) — landing before the generic-careers fallback; (3) ✅ discovery's generic-careers fallback now resolves to `llm_scrape` instead of the dead-end `custom` (T-309); (4) ✅ the LLM page-scrape scraper itself (T-472), on its own 6-hourly cadence (T-473), with its own cost tracking (T-475); (5) ✅ the coverage-guarantee test (T-910, above); (6) ✅ self-healing re-resolution of companies stuck at a legacy or incomplete bundled entry (T-310) — see below. Only Tier C (§below, deliberately deferred) and FR-72 (v2 web UI) remain unbuilt.

**T-310, and the correctness bug found while first trying to build it (§ discover()'s trust model).** The original plan — "force re-resolution (`force=True`) of every company at `ats_type='custom'` so most flip to `workday` automatically" — turned out not to work at all: `discover()`'s bundled-table step (§3.1 Step 1) ran unconditionally regardless of `force`, and always short-circuited before reaching the web-search steps for any company already present in `company_ats_map.json`. `force=True` could bypass the DB cache (Step 0) but never that. This is exactly why Nvidia/Intel/Adobe/Salesforce needed hand-correction in T-470 rather than an automated retry — that was the only thing that could actually work at the time.

Fixed properly, not worked around: Step 1 now only trusts a bundled entry as final when it's genuinely *usable* — a new `_is_usable(ats_type, slug, url)` check requires a live ats_type (`greenhouse`/`lever`/`workday`/`llm_scrape`) **and** the identifier that type's scraper actually needs (a `slug` for the ATS types, a `url` for `llm_scrape`). A `workday` entry with no slug is exactly as dead as a `custom` entry with no url, just under a more convincing label — both now fall through to live search instead of being returned immediately. One targeted optimization on top: a bundled entry already tagged `workday` (just missing its slug — Cisco, Qualcomm, Broadcom, VMware, PayPal, ServiceNow, DocuSign, Lockheed Martin, Northrop Grumman) skips straight to re-attempting the Workday search rather than wasting queries re-checking Greenhouse/Lever for a company already confirmed not to be there.

`retry_unresolved()` (§3.1 Step 5, the hourly background retry) is broadened to match: previously it only ever queried `status == "unresolved"` rows, meaning nothing would have called `force=True` on a stuck-but-"resolved" company even after the Step 1 fix — the fix alone doesn't help if nothing triggers it. It now also targets `status == "resolved"` rows that aren't `_is_usable` (the same legacy/missing-identifier states as above), on the same per-company cooldown. `RetryResult.resolved` now means "resolved to something *usable*," not just `status == "resolved"` — a stuck row retried and still stuck (search found nothing better) no longer inflates the count just because it started out `status == "resolved"`.

This gives Google, Meta, Microsoft, Apple, Amazon, Netflix, Tesla, Uber, and every other bundled `custom`/`taleo` company a real, automatic path to `llm_scrape` coverage over time via the existing hourly retry job — no more one-company-at-a-time hand-correction required. Confirmed live (T-308) that none of them are actually on Workday, so they'll resolve to Tier B, not Tier A, when they do resolve — consistent with everything already established about this set of companies.

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
- **Query shape**: one broad query per poll, `country=us`, `employment_types=INTERN`, `date_posted=week` — deliberately not looped per domain keyword like Indeed RSS, since every request is metered. **Alternates between two queries by UTC hour** (`(hour // 4) % 2`): `INTERN_QUERY` (`"software engineering, AI, or data engineering intern in the United States"`) on even 4-hour slots, `COOP_QUERY` (`"...co-op..."`) on odd ones — added after discovering the single intern-only query missed co-op-only listings that never say "intern"; both variants were later broadened beyond pure "software engineering" wording to also catch AI/data-engineer-titled roles (§5.2). Each variant polls roughly every 8 hours.
- **Cadence**: its own scheduled job, independent of the main cycle — currently every 4 hours (~180 requests/month total across both query variants), sized to fit a 200/month free-tier plan. Re-tune the interval (`queries_per_poll × polls_per_day × 30 ≤ plan quota`) if the plan differs — querying both variants every poll instead of alternating would double this to ~360/month.
- **Source labeling**: each result includes `job_publisher` (e.g. `"LinkedIn"`, `"ZipRecruiter"`, `"BeBee"`) — the exact board it was aggregated from. Encoded into `source` as `jsearch:{publisher}`, the same compound-source convention `greenhouse.py`/`lever.py` use for company slugs, and surfaced to the user as the message's `Source:` line (§9.2)
- **Apply link prefers the employer's own page over the aggregator's** (`_direct_apply_url()` in `jsearch.py`): `job_apply_link` is frequently a re-poster, not the employer — confirmed live, a captured response had `job_apply_link` pointing to BeBee with `job_apply_is_direct: false`. JSearch separately provides `apply_options`, a list of every known application route each with its own `is_direct` flag; the scraper uses the first direct one if any exists, and only falls back to the possibly-indirect `job_apply_link` if nothing in `apply_options` is marked direct either. **BeBee and JobLeads are hard-excluded** (`_BLOCKED_APPLY_DOMAINS`) regardless of what `is_direct` says — JSearch's own flag has been observed marking these two as direct even though they're just another re-poster, not the employer, found live after they kept showing up as the "Apply" link. The full candidate chain (direct `job_apply_link` → direct `apply_options` entries → fallback `job_apply_link` → any `apply_options` entry) is built first, then filtered for blocked domains, so a blocked link never displaces a genuinely better one further down the chain; it's only returned as an absolute last resort if nothing else exists at all.
- **Verified live**: a test query returned 10 postings, 4 explicitly `job_publisher: LinkedIn`
- **Dedup key**: `(source, external_id) = ("jsearch:{publisher}", job_id)` — unlike other sources, `source` itself is compound here rather than a fixed string

### 4.2 Adzuna

- **Provider**: Adzuna developer API (free)
- **Covers**: Indeed, Reed, Totaljobs, and US-specific aggregation not fully in JSearch
- **Auth**: `app_id` + `app_key`
- **Free tier**: 250 calls/month
- **Filter**: `where=us`, `sort_by=date`, `full_time=0`
- **Query shape**: unlike JSearch, **both** `what="internship"` and `what="co-op"` are queried every weekly poll rather than alternated — two calls/week (~8/month) is trivial against a 250/month quota, so there's no need to trade off coverage lag for quota safety the way JSearch (4-hour cadence, tighter quota) does
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
- **Mechanism**: Parse the current month's thread; extract comments mentioning "intern"/"internship" or "co-op" (word-boundary regex, so it doesn't false-positive on "cooperative"); extract company name and any URL; run through AI scorer like a normal posting
- **Dedup key**: `hn:{comment_id}`
- **Cadence**: Wired into the main poll cycle (`_SOURCES` in `main.py`) — re-fetched every cycle (default hourly), same as Indeed RSS and RemoteOK, not on a separate monthly/daily schedule

### 4.6 RemoteOK

- **Provider**: RemoteOK public API (`remoteok.com/api`) — free, no auth, no rate limit specified
- **Covers**: Remote-only internships and entry-level roles across all tech companies; good coverage of distributed/async-first companies that don't post on LinkedIn
- **Filter**: tags include `intern`, `internship`, `co-op`, or `coop`, OR the position title matches `intern` or a word-boundary `co-?op` regex (same false-positive guard as HN's, so "Cooperative Partnerships Manager" doesn't match); parsed from JSON feed. **Deliberately does not match the bare `junior` tag** — that was matched originally, but RemoteOK tags junior roles across every field (marketing, design, sales, ...), not just software, and it was letting through postings with zero domain relevance. Found and fixed after the no-score-floor change (§8.2a) made this previously-silent noise visible as real Telegram messages.
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
  └── Fastest alert (within one poll cycle of posting — default 120 min)
  └── Best for: big tech, quant firms, companies that don't post broadly
  └── Source: Greenhouse / Lever ATS APIs, custom scrapers
  └── Coverage: ~600 companies in your personal list

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

**Correction (2026-08-12): this section previously implied all Tier 2 sources run the query list below — only Indeed RSS actually does.** HN and RemoteOK fetch their own bulk feed (the current month's thread; the full RemoteOK API dump) and apply an independent regex/tag filter, never touching `build_queries()`. Adzuna and JSearch each run their own one-or-two hand-written broad queries (§4.2, §4.1a), not this list either. `build_queries()` in `src/scrapers/base.py` is Indeed RSS's alone.

**Indeed RSS's full query list** (looped sequentially, one query per `httpx` call, within the main cycle):
```
# Core SWE / DS / ML / AI
"software engineering intern"
"software intern"
"data science intern"
"machine learning intern"
"AI intern"
"artificial intelligence intern"
"AI engineer intern"
"data engineer intern"
"machine learning research intern"
"research intern computer science"
"backend intern"
"frontend intern"
"full stack intern"

# Co-op / extended terms
"software engineering co-op"
"software co-op"
"machine learning co-op"
"data science co-op"
"AI engineer co-op"
"data engineer co-op"
```

The `AI engineer`/`data engineer`/`machine learning research` variants were added after AI/data-titled roles (as opposed to pure "software engineering" wording) were found being missed by the original term list.

The agent does not filter by semester or term — it catches summer, fall, spring, and co-op postings alike. Season filtering would require parsing unstructured job description text and is left to the AI scorer (which can flag term in its reasoning).

**Co-op coverage varies by source** (found and fixed 2026-08-12, after a user question specifically about co-op coverage): HN's and RemoteOK's filters originally matched only `intern`/`internship`, silently missing co-op-only listings (ones that never use the word "intern"). Current state per source:

| Source | Matches co-op? |
|---|---|
| Greenhouse / Lever (Tier 1) | ✅ always did — `intern\|co-?op` title regex |
| Indeed RSS | ✅ always did — explicit co-op terms in `build_queries()` above |
| HN | ✅ fixed — regex now `\bintern(ship)?\b\|\bco-?op\b` |
| RemoteOK | ✅ fixed — tags now include `co-op`/`coop`, position regex matches `co-?op` too |
| Adzuna | ✅ fixed — queries both `what="internship"` and `what="co-op"` every weekly poll |
| JSearch | ✅ fixed — alternates `INTERN_QUERY`/`COOP_QUERY` by UTC hour (§4.1a); not queried simultaneously, to stay under quota |

All the `co-?op` regexes use a `\b` word boundary on both sides specifically to avoid false-matching "cooperative" (e.g. "a cooperative team culture") — confirmed by test cases in `tests/test_scrapers_hn.py` and `tests/test_scrapers_remoteok.py`.

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
    match_score         INTEGER,            -- 0–100; NULL = not yet scored. No fit-quality gate on
                                             -- notification — only a small domain-relevance floor
                                             -- (min_relevance_score, default 15, §8.2a).
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
    ats_type        TEXT,                  -- 'greenhouse' | 'lever' | 'workday' | 'llm_scrape' | NULL —
                                             -- 'custom' is the pre-§3.5 dead-end value: resolved, but with
                                             -- no scraper behind it. Being phased out; see §3.5 rollout.
    slug            TEXT,                  -- ATS-specific slug (Tier A) or workday tenant+site
    url             TEXT,                  -- careers page URL for llm_scrape (Tier B)
    needs_render    BOOLEAN DEFAULT FALSE, -- NOT YET IMPLEMENTED (§3.5 Tier C) — set when a plain GET of
                                             -- `url` returns a near-empty JS-SPA shell; tells the Tier B
                                             -- poller to use a headless browser for this company only
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

**Hard domain gate first**: if the posting isn't SWE/DS/ML/AI at all (marketing, sales, design, ops, ...), Claude scores it 0-5 regardless of anything else and skips the weighted rubric below. This exists because not every fetch-layer filter is software-scoped (§4.5, §4.6) — the gate is the thing doing real filtering work now that there's no downstream fit-quality threshold (§8.2a).

For postings that pass the gate, Claude evaluates:
- **Skills alignment** (40%): does the posting require skills the user has?
- **Domain relevance** (30%): is this a SWE / DS / ML / AI role specifically (vs. adjacent technical fields like IT support or QA)?
- **Seniority fit** (20%): is this appropriate for the user's experience level?
- **Role accessibility** (10%): are there gatekeeping requirements (PhD, clearance, 2+ YOE) the user doesn't meet?

Score 0–100. Claude also identifies `missing_qualifications`: specific skills, tools, or requirements named in the posting that the candidate's profile doesn't show (empty list for a strong match). Return JSON array with `external_id`, `score`, `reasoning` (1–2 sentences), `missing_qualifications` (list of short strings).

### 8.2a No fit-quality gate, but a domain-relevance floor

There is no `min_score` "good personal fit" threshold and no separate "partial match" tier — both existed at one point and were removed. Every scored posting is notified, with its score, reasoning, and `missing_qualifications` all included in the message (§9.2), so the user judges fit themselves rather than the system silently dropping weak matches.

This surfaced a real problem: not every source's fetch-layer filter is software-scoped (§4.5, §4.6 — HN matches the word "intern" anywhere in a comment, RemoteOK matched a generic `junior` tag before that was fixed). With no floor at all, those off-domain postings were sent as real messages too, just with a low score attached — noise the old `min_score=70` had been silently absorbing as a side effect, not by design.

Two-part fix: the scoring prompt (§8.1) now has an explicit hard domain gate instructing Claude to score anything outside SWE/DS/ML/AI at 0-5 regardless of other factors, and `profile.yaml → matching.min_relevance_score` (default 15) filters those out in `get_unnotified_scored_postings()`. This is a "is this even the right field" gate, not a "would I apply to this" gate — a weak-but-genuine software posting still clears it easily. See §9.1 for the unified notification routing.

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

The notifier operates in one of two modes per polling cycle, depending on how many scored postings there are to notify (`notify_matches()`). There is no separate full/partial-match distinction — every posting at or above the domain-relevance floor goes through this same single path (§8.2a).

**Individual mode** (< `BURST_THRESHOLD` postings, default 20): one message per posting, each including its score, reasoning, missing qualifications, source board, and an apply link — direct to the employer where the source makes that possible (Greenhouse, Lever, JSearch; see §4.1a), otherwise the source's own listing (Indeed, Adzuna) — plus a "Mark Applied" button. Individual sends are paced 1 second apart (`_INDIVIDUAL_SEND_DELAY_SECONDS`) to stay under Telegram's per-chat rate limit — a cycle with, say, 15 postings takes ~15 seconds to fully notify, not instant.

```
Stripe — Software Engineering Intern
📍 Remote  ·  🔗 Greenhouse

🟢 88/100
Strong Python/backend fit, welcoming undergrads

Missing: Kubernetes
👉 Apply
```

**Burst mode** (≥ `BURST_THRESHOLD` postings): a single summary message listing all of them, each with its score, source, missing qualifications, and its own apply link, capped at 10 lines shown (`+ N more — run db stats`) to stay within Telegram's message length limit on a genuinely high-volume cycle.

```
25 new postings this cycle
🟢 1. Stripe — SWE Intern · 📍Remote · 🔗Greenhouse (92)
    Missing: none listed · Apply
🟡 2. TikTok — SWE Intern · 📍Seattle · 🔗LinkedIn (78)
    Missing: Kubernetes · Apply

+ 23 more — run db stats to see all
```

The full list is always stored in the database. `db stats` shows everything regardless of which mode was used. `BURST_THRESHOLD` is configurable via env var — the default (20) was chosen deliberately high so individual messages (with apply links) are the normal experience, and batching only kicks in as a safety net on unusually high-volume cycles, not as the default behavior.

### 9.2 Real-time message format (individual mode)

Sent as Telegram HTML (`parse_mode="HTML"`), not plain text — bold company/title header, a score-tier emoji (🟢 ≥70, 🟡 ≥40, 🔴 below — a visual triage cue only, not a filtering mechanism; the actual floor is `min_relevance_score`, §8.2a), and a clickable `Apply` link rather than a bare pasted URL. Every value substituted into the template (company, title, location, reasoning, missing qualifications) is run through `html.escape()` first, since all of it is either AI-generated or scraped from a third-party posting — never safe to trust verbatim inside HTML.

The `🔗` source label is derived from `JobPosting.source` via `_friendly_source()` in `notifier.py` — most sources map to a fixed label (`greenhouse:{slug}` → `"Greenhouse"`); `jsearch:{publisher}` is the one exception where the compound suffix *is* the label (`jsearch:LinkedIn` → `"LinkedIn"`), since that's the actual board the posting was aggregated from. It's omitted entirely if `source` is empty (defensive default, not expected in practice).

Targets ≤4096 chars (Telegram's hard per-message limit — generous compared to SMS's per-segment cost, so truncation is a rare edge case rather than the norm). The apply link is always included verbatim; reasoning (after escaping) is truncated if needed to fit the budget, after accounting for the header, meta line, and missing-qualifications/apply-link suffix.

### 9.3 Telegram delivery

- Plain `httpx` POST to `https://api.telegram.org/bot{token}/sendMessage` — no dedicated SDK needed for a single-endpoint integration this simple
- `parse_mode="HTML"` is opt-in per call, not a default on `send_message()` — only the formatted individual/burst alerts use it (pre-escaped content); plain confirmation texts (`/pause`, `/resume`) have no need for it
- Stores the returned `message_id`
- On failure: retries once after 5 minutes; logs permanent failure
- Chat ID redacted to last 4 characters in all logs (lower sensitivity than a phone number, but kept consistent with the project's log-hygiene practice)

### 9.4 Mark Applied — visual confirmation

Tapping "✅ Mark Applied" used to only produce a transient `answerCallbackQuery` toast, with no lasting trace once scrolled past. `get_latest_notification_for_posting()` (`db.py`) looks up the original alert's `telegram_message_id`, and `edit_message_mark_applied()` (`notifier.py`, Telegram's `editMessageText`) appends "✅ Applied" to that message and removes the button — `parse_mode="HTML"` must be passed here too, since the stored `original_text` is itself HTML (bold header, score emoji, Apply link), or Telegram renders the tags as literal text. Never raises — Telegram's "message is not modified" (e.g. a near-simultaneous double-tap racing the edit) is treated as a harmless no-op, same pattern as every other outbound Telegram call in this codebase.

### 9.5 No separate digest/summary job

There used to be an hourly digest message (stats, top match, unresolved companies) alongside the real-time alerts above. Removed per explicit request — it mostly repeated information already visible in the individual alerts themselves. Unresolved companies are still retried automatically in the background (§3.4, §10.1); check them via `python -m src.db stats`, not a pushed message.

---

## 10. Scheduling & Orchestration

### 10.1 Scheduler

Five independent `APScheduler` jobs, all `max_instances=1` to prevent overlapping runs of the same job (different jobs can still run concurrently with each other):

| Job | Interval | Does |
|---|---|---|
| Main cycle | `IntervalTrigger(minutes=RUN_INTERVAL_MINUTES)` (default 120) | Fetch (Indeed/HN/RemoteOK/Greenhouse/Lever), dedupe, score, notify |
| Unresolved-company retry | `IntervalTrigger(hours=1)` | Retry unresolved companies, silently — replaces the old digest job (§9.5); no message is sent |
| Adzuna | `IntervalTrigger(weeks=1)` | Fetch, store unscored — separate from the main cycle to fit its free-tier quota |
| JSearch | `IntervalTrigger(hours=4)` | Fetch (LinkedIn/Glassdoor/ZipRecruiter via one broad query), store unscored — separate for the same reason, sized to a 200/month plan |
| Telegram command poll | `IntervalTrigger(seconds=5)` | `getUpdates` for `/pause`, `/resume`, and "Mark Applied" taps — decoupled from the main cycle and deliberately fast (a few seconds, not minutes) so a button tap feels close to instant; `getUpdates` is a cheap short-poll (timeout=0) with no meaningful rate-limit risk at this interval |

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
  └── for every scored, unnotified, non-applied posting >= min_relevance_score (default
      15 — a domain-relevance floor, not a fit-quality gate):
        └── send Telegram message (individual or burst, by BURST_THRESHOLD)
        └── mark notified=TRUE
  └── check if profile hash changed → trigger re-score pass on unnotified postings
  └── write run_log entry (JSON)

every hour (unresolved-company retry job, §9.5):
  └── re-attempt discovery for unresolved companies (1-hour per-company cooldown)
  └── no message sent — check `python -m src.db stats` for the current list

every 5 seconds (telegram command poll):
  └── getUpdates since last cursor
  └── callback_query "applied:{id}" → mark_applied, only from TELEGRAM_CHAT_ID
        → look up the posting's Notification row (get_latest_notification_
          for_posting); if one exists, edit that original message
          (edit_message_mark_applied) to append "✅ Applied" and drop the
          button — the answerCallbackQuery toast alone is transient and
          leaves no trace once scrolled past
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

---

## 14. Multi-Tenant Architecture — Planned (v2)

**Not currently implemented.** Everything in §1–§13 describes the live single-user personal tool: one Telegram chat, one `profile.cache.json`, one `config/companies.yaml`, one SQLite DB, all driven by env vars on one Railway deployment. This section documents the planned pivot to a shared multi-user product, decided through user brainstorming on 2026-08-18 (see `docs/tasks.md` for the dated log entry). It's a design direction to build toward, not a description of running code — every subsection below is prefixed accordingly where it matters.

### 14.1 Scope decisions

Four structural decisions were made explicitly, each with real alternatives considered and rejected:

- **Single shared SaaS app**, not a deployable-template-per-user model. One deployment, one (eventually multi-tenant) database, real accounts — not everyone running their own Railway fork with their own API keys.
- **Email is the default notification channel; Telegram is optional.** Telegram's interactive features (Mark Applied button, `/pause`/`/resume`, §9) don't cleanly generalize to a channel everyone has, and requiring an app install is real signup friction for a general audience. Email works for 100% of signups (it's needed for auth anyway) with no added setup; Telegram becomes an enhancement a user can link afterward.
- **Fetch once, score per user.** Tier 2 job-board content (Indeed/JSearch/Adzuna/HN/RemoteOK) is not personal — the same posting exists regardless of who's asking — so it's wasteful to fetch it once per user. AI *scoring* is inherently personal (it's evaluated against each user's own profile), so that part does scale per user. This is the main cost driver at scale (§14.6).
- **Billing is deferred to last**, per explicit instruction. The beta runs at the developer's own testing budget with no payment collection until every other piece here is built and working end-to-end.

Scale target for the initial build: a small beta, dozens of users — not hundreds or thousands. That keeps the scheduler a simple in-process loop over users (§14.6) rather than requiring a distributed job queue; revisit if usage outgrows that.

### 14.2 Accounts & auth

Auth is handled by a managed provider (e.g. Clerk, Auth0, or Supabase Auth) rather than hand-rolled password/session code — far less security-sensitive surface to own (password hashing, verification emails, reset tokens, session/CSRF handling) for a product at this scale. Minimum supported sign-in: email+password and Google OAuth. The provider issues a stable external user ID that the app's own `users` table stores alongside app-specific state (§14.3).

### 14.3 Data model changes

The core structural change: **posting content becomes shared/app-wide; scoring and notification state becomes per-user.** Today's single `job_postings` table (§7.1) conflates both — `match_score`, `match_reasoning`, `missing_qualifications`, `notified`, `applied` are all columns on the posting itself, which only works because there's exactly one user. Splitting these apart:

```sql
-- job_postings loses its per-user columns; becomes purely shared content.
-- (source, external_id, title, company, location, url, apply_url,
--  description, posted_at, found_at — unchanged from §7.1.
--  match_score, match_reasoning, missing_qualifications, profile_hash,
--  notified, notified_at, applied, applied_at all MOVE to the new table below.)

CREATE TABLE users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    auth_provider_id    TEXT NOT NULL UNIQUE,  -- external ID from the auth provider (§14.2)
    email               TEXT NOT NULL UNIQUE,
    telegram_chat_id    TEXT,                  -- NULL until linked (§14.7); optional
    telegram_link_token TEXT,                  -- one-time token for the deep-link flow, cleared once used
    notifications_paused BOOLEAN DEFAULT FALSE, -- per-user equivalent of today's bot_state (§7.5)
    min_relevance_score INTEGER DEFAULT 15,     -- per-user equivalent of profile.yaml (§1.5, FR-26)
    created_at          DATETIME NOT NULL
);

CREATE TABLE resumes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    filename        TEXT NOT NULL,
    storage_key     TEXT NOT NULL,   -- path in cloud object storage, not local disk
    sha256_hash     TEXT NOT NULL,   -- per-file change detection, same idea as today's FR-18
    is_active       BOOLEAN DEFAULT TRUE,  -- only active resumes are merged into the profile (FR-55)
    uploaded_at     DATETIME NOT NULL
);

CREATE TABLE user_profile_cache (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id),  -- one merged profile per user
    profile_json    JSON NOT NULL,   -- the per-user equivalent of profile.cache.json (§2.4)
    profile_hash    TEXT NOT NULL,   -- hash of the active resume set; drives re-score-on-change (FR-56)
    updated_at      DATETIME NOT NULL
);

CREATE TABLE user_companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    company_name_raw    TEXT NOT NULL,     -- as entered by the user (FR-57)
    company_lookup_id   INTEGER REFERENCES company_lookup(id),  -- resolved via the SHARED cache (§14.5)
    added_at            DATETIME NOT NULL,
    UNIQUE(user_id, company_name_raw)
);

CREATE TABLE user_posting_matches (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    job_posting_id          INTEGER NOT NULL REFERENCES job_postings(id),
    match_score             INTEGER,      -- NULL = not yet scored for this user
    match_reasoning         TEXT,
    missing_qualifications  JSON,
    profile_hash            TEXT,         -- this user's profile_hash at scoring time
    notified                BOOLEAN DEFAULT FALSE,
    notified_at             DATETIME,
    applied                 BOOLEAN DEFAULT FALSE,
    applied_at              DATETIME,
    UNIQUE(user_id, job_posting_id)
);
```

`company_lookup` (§7.2) is unchanged and stays shared — it's referenced by `user_companies.company_lookup_id` from every user watching that company, rather than being read from one global YAML file (§14.5). `notifications` (§7.3) gains a `user_id` FK (today's `recipient_id` text field becomes redundant once channel dispatch is per-user, §14.7). `bot_state` (§7.5) is superseded by the per-user columns now on `users` (`notifications_paused`, `telegram_chat_id`) — no more singleton row.

### 14.4 Resume management

Resumes upload through the web app into per-user cloud object storage (not a local `/resumes` directory, FR-53) — isolated per user, never shared. Users can delete any version (FR-54) and toggle each version active/inactive (FR-55); only active resumes are merged into `user_profile_cache`. Any of add / delete / toggle triggers the same extract → merge → re-score pipeline `--rebuild-profile` runs today (FR-19, FR-56), scoped to that one user — `resume_extractor.py`'s extraction and merge logic (§2.1–§2.3) is reused as-is, only the storage layer and the "which resumes count" selection change.

### 14.5 Per-user company watchlist, shared discovery cache

Each user's watchlist lives in `user_companies` and starts empty (FR-58) — no user's list, including the developer's own, seeds any other account. What *is* shared app-wide is company → ATS resolution (`company_lookup`, structurally unchanged from §7.2/§3 — the resolution cascade itself gains the Workday/`llm_scrape` tiering of §3.5, but stays a single shared cache either way): "Stripe uses Greenhouse" is impersonal metadata true for every user, so it's resolved once (§3.1's lookup-table → web-search cascade) and referenced by every `user_companies` row for every user who adds Stripe, rather than re-discovered per user. This is exactly why §3.5's coverage guarantee matters more, not less, once watchlists are user-driven: a dead-end resolution silently fails every user watching that company at once, not just one `companies.yaml`.

### 14.6 Shared fetch / per-user score pipeline

The scheduler (§10.1) restructures around the fetch-once/score-per-user split (§14.1):

```
Tier 2 sources (Indeed/JSearch/Adzuna/HN/RemoteOK):
  fetched once app-wide, same cadence as today (§10.1) → shared job_postings pool
  (identical to today's fetch/dedupe logic, §4 and §6 — nothing user-specific here)

Tier 1 sources (company watchlists):
  each polling cycle, compute the UNION of all active users' user_companies
  → fetch each distinct company's ATS board at most once per cycle
    (a company watched by 40 users still gets ONE Greenhouse/Lever fetch,
    not 40 — FR-64)
  → new postings land in the same shared job_postings pool as Tier 2

Scoring (per user, the actual multi-tenant cost driver — FR-65):
  for each active user:
    for each job_posting reachable by that user (all of Tier 2's shared pool,
    plus their own Tier 1 watchlist companies) with no existing
    user_posting_matches row:
      batch-score against that user's own user_profile_cache (same
      batches-of-10 Claude call as today, §8.1 — just looped per user)
      → insert/update user_posting_matches

Notify (per user):
  for each user's newly-scored user_posting_matches at/above their own
  min_relevance_score (users.min_relevance_score, not a shared profile.yaml
  value anymore):
    send via that user's linked channels (§14.7)
    mark notified=TRUE on the user_posting_matches row
```

This is still a simple in-process loop for the beta scale target (§14.1, FR-66) — no job queue yet. It's the natural place to revisit if user count grows enough that per-cycle wall-clock time or Claude request volume becomes a problem.

### 14.7 Notification channels

Email is the default, always-on channel (FR-60) — sent to the signup address, no linking step required. A user can additionally link Telegram (FR-61) via a one-time deep-link: the app generates a `telegram_link_token`, the user taps a link that opens a chat with the bot pre-filled with `/start <token>`, and the bot's existing update-polling loop (§10.2) resolves the token to a `user_id` and stores the resulting `chat.id` on `users.telegram_chat_id`. From then on, notification dispatch for that user fans out to every linked channel: email always, Telegram additionally if `telegram_chat_id` is set.

Today's individual/burst message formats (§9.1, §9.2) and the "Mark Applied" button (§9.4) carry over unchanged for the Telegram channel. Email needs its own non-interactive equivalent (FR-62) — the same information (company, title, score, reasoning, missing qualifications, apply link), but "Mark Applied" becomes a plain link to an authenticated endpoint instead of an inline Telegram button, since email has no callback mechanism.

### 14.8 Deployment & storage notes

Two infrastructure pieces that don't carry over cleanly from the single-user deployment (§11):

- **SQLite likely doesn't hold up.** The 2026-08-15 WAL/busy-timeout fix (`docs/tasks.md`) was sized for occasional overlap between a handful of independent scheduled jobs, not sustained concurrent writes across dozens of users' scoring/notification cycles. Worth evaluating a managed Postgres before or shortly after the first real multi-user beta, not as an afterthought.
- **Resume storage moves off local disk** to cloud object storage (e.g. S3-compatible), per-user-scoped paths, since `/resumes` (FR-13, gitignored, local-machine-only per NFR-14) doesn't exist in a hosted multi-tenant deployment. `profile.cache.json`'s env-var-sync workaround (§2.4, `PROFILE_CACHE`) also goes away entirely — replaced by `user_profile_cache` rows written directly by the app, no manual Railway CLI step (§11's "Resume update workflow") needed per user.

### 14.9 Billing — deferred

Explicitly the last piece to build (FR-67). Claude API cost scales with active user count (§14.6's per-user scoring loop is the driver) in a way NFR-09's $10/month single-user budget never had to account for. No pricing model, payment collection, or usage metering is designed yet — intentional, per the 2026-08-18 decision to get the rest of the product working first at developer-funded cost.

### 14.10 Coverage visibility for watchlist companies — Planned (v2)

§3.5 fixes the underlying bug (a company can no longer be `resolved` with no scraper behind it), but Tier B (`llm_scrape`) and `unresolved` companies are still meaningfully lower-confidence than Tier A — a bespoke page-scrape can miss a listing a JSON API wouldn't, and an unresolved company isn't being polled at all yet. In the single-user tool that gap is invisible by design (§3.4's `db stats` is opt-in, CLI-only). That doesn't hold for a general product: a user who explicitly adds a company via `user_companies` (§14.5, FR-57) has no other way to know whether it's fully covered, degraded, or not covered yet.

Each row a user's watchlist resolves to (via `company_lookup.ats_type`/`status`) surfaces a coverage indicator in the web UI:

- Tier A (`greenhouse`/`lever`/`workday`) → "Fully covered."
- Tier B (`llm_scrape`) → "Covered (best-effort) — we're still building full coverage for this company," surfaced rather than presented identically to Tier A.
- `unresolved` → "Not yet covered — retrying automatically," rather than the silent background retry (§3.1 step 5) it is today.

This turns exactly the failure mode that caused the missed Nvidia posting — a company that looks the same as any other on the list but silently isn't being watched — into something a user can actually see and account for, instead of discovering it after the fact.
