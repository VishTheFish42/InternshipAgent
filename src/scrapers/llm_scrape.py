"""Tier B fallback scraper (design.md §3.5, FR-70/FR-71): for a company with
a bespoke in-house careers page and no known JSON-API ATS (Greenhouse,
Lever, Workday), fetch the page's HTML and use Claude to extract structured
postings from it. This is what makes "add any company" actually true for a
general product — Tier A only covers companies on one of a handful of known
platforms; this tier has no such ceiling, at the cost of a real page fetch
plus one LLM call per company per poll.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx
from anthropic import Anthropic
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Settings, get_settings
from src.db import CompanyLookup
from src.scrapers.base import RawPosting

_log = logging.getLogger(__name__)

_USER_AGENT = "InternshipAgent/1.0 (+personal internship alert tool)"
_RATE_LIMIT_SECONDS = 5.0

# A company with more open roles on one listing page than fit in this many
# characters gets a truncated extraction — a documented gap, not a silent
# one, same trade-off Workday's single-page-per-query limit already carries
# (workday.py).
_MAX_PAGE_TEXT_CHARS = 20_000

# NFR-08 ("not exceed 100 Claude API calls per polling cycle") applied to
# this tier's own poll: one Claude call per company, so this is also a cap
# on companies-per-poll. Real watchlists are expected to stay far under
# this; it exists as a hard guarantee, not just an assumption (NFR-22).
_MAX_COMPANIES_PER_POLL = 100

# Same word-boundary filter workday.py uses, and for the identical reason:
# an LLM extraction is even less constrained than Workday's own search, so
# trusting the prompt's "internship or co-op only" instruction alone isn't
# enough — this is the deterministic backstop.
_INTERN_RE = re.compile(r"\bintern(?:s|ship)?\b|\bco-?op\b", re.IGNORECASE)

# Duplicated from matcher.py rather than imported — keeps this scraper's
# only real dependency on matcher.py's internals at zero; NFR-22 just needs
# a comparable estimate, not the exact same code path.
_MODEL_PRICING_PER_MTOK = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}
_DEFAULT_PRICING = {"input": 3.00, "output": 15.00}

_SYSTEM_PROMPT_TEMPLATE = """\
You are extracting internship and co-op job postings from the text of a \
company's careers page. HTML tags have been stripped from the text below, \
but links are preserved as [visible text](absolute URL).

Today's date is {today}. If a posting states a relative or partial date \
(e.g. "Posted 3 days ago", "Posted Today", "June 2026"), resolve it to an \
absolute ISO 8601 date (YYYY-MM-DD) yourself using today's date as the \
reference point. If no date is stated at all, use null — do not guess.

Return ONLY a valid JSON array, no extra text, no markdown fences. One \
object per internship or co-op posting found (ignore every other kind of \
job posting on the page):

[{{"title": "...", "url": "absolute URL", "location": "..." or null, \
"posted_date": "YYYY-MM-DD" or null, "description": "..." or null}}]

If the page has no internship or co-op postings — including if the text \
below looks empty, truncated, or like a JavaScript application shell \
rather than real page content — return an empty array: []
"""


def _is_internship(title: str) -> bool:
    return bool(_INTERN_RE.search(title))


def _slugify(name: str) -> str:
    """Lowercase, hyphen-joined identifier for the `source` compound value
    — the same role a resolved ATS slug plays for greenhouse.py/lever.py/
    workday.py, but this tier has no such slug, only a company name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "company"


class _LinkedTextExtractor(HTMLParser):
    """Strips HTML down to visible text, rendering anchor tags as
    "[text](absolute url)" so Claude can still recover each posting's link —
    plain tag-stripping would discard hrefs entirely, since they live in
    attributes, not visible text. No BeautifulSoup/lxml dependency: this
    project has already chosen plain stdlib over a library/SDK for a simple
    integration twice before (Telegram in notifier.py, Supabase in
    src/web/supabase_auth.py); this is the same call for HTML."""

    _SKIP_TAGS = {"script", "style", "noscript", "head", "svg"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._parts: list[str] = []
        self._skip_depth = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a" and self._skip_depth == 0:
            href = dict(attrs).get("href")
            self._link_href = urljoin(self._base_url, href) if href else None
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._skip_depth == 0:
            text = "".join(self._link_text).strip()
            if text and self._link_href:
                self._parts.append(f"[{text}]({self._link_href})")
            elif text:
                self._parts.append(text)
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link_text.append(data)
        else:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._parts)


def html_to_linked_text(html_content: str, base_url: str) -> str:
    parser = _LinkedTextExtractor(base_url)
    parser.feed(html_content)
    return parser.get_text()[:_MAX_PAGE_TEXT_CHARS]


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences if the model wraps the JSON anyway —
    same helper as matcher.py's, duplicated for the same zero-coupling
    reason as the pricing table above."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        stripped = "\n".join(inner).strip()
    return stripped


def _parse_posted_date(value: str | None) -> datetime | None:
    """Claude is asked to already resolve relative dates into YYYY-MM-DD
    itself (unlike workday.py's fixed handful of relative-date strings,
    arbitrary career pages use unpredictable wording, which is exactly the
    kind of unstructured-language task worth delegating to the model rather
    than hand-rolling a regex for). This just parses that ISO date — or
    returns None (benefit of the doubt, same as every other source's
    unreliable/absent posted_at) if the model didn't return one or returned
    something unparseable."""
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    except ValueError:
        return None


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = _MODEL_PRICING_PER_MTOK.get(model, _DEFAULT_PRICING)
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def _fetch_html(url: str) -> str:
    resp = httpx.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=15.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


@dataclass
class _ExtractionResult:
    postings: list[RawPosting]
    input_tokens: int
    output_tokens: int


def _extract_postings(
    client: Anthropic,
    system_prompt: str,
    model: str,
    company_name: str,
    page_text: str,
) -> _ExtractionResult:
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": page_text or "(page had no readable text)"}],
    )
    block = message.content[0]
    text = getattr(block, "text", None)
    if text is None:
        raise RuntimeError(f"Unexpected response block type from Claude: {type(block)}")

    items: list[dict[str, Any]] = json.loads(_strip_fences(text))
    slug = _slugify(company_name)
    postings: list[RawPosting] = []
    for item in items:
        url = item.get("url")
        title = item.get("title")
        if not url or not title or not _is_internship(title):
            continue
        location = item.get("location")
        postings.append(
            RawPosting(
                source=f"llm_scrape:{slug}",
                external_id=url,
                title=title,
                company=company_name,
                location=location,
                is_remote=bool(location and "remote" in location.lower()),
                url=url,
                apply_url=url,
                description=item.get("description"),
                posted_at=_parse_posted_date(item.get("posted_date")),
            )
        )

    return _ExtractionResult(
        postings=postings,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )


@dataclass
class LlmScrapeResult:
    postings: list[RawPosting] = field(default_factory=list)
    companies_polled: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


def fetch_for_resolved_companies(
    session: Session, settings: Settings | None = None
) -> LlmScrapeResult:
    """
    Fetch and extract postings for every company resolved to the llm_scrape
    tier in the DB. Respects a 1 req/5s rate limit between companies, same
    as greenhouse.py/lever.py/workday.py — polite to arbitrary third-party
    sites, not just ATS APIs. One Claude call per company; the system
    prompt is built once and cache_control-shared across all of them within
    a single poll, same pattern matcher.py uses for its own repeated calls.
    """
    settings = settings or get_settings()
    rows = list(
        session.execute(
            select(CompanyLookup.name_raw, CompanyLookup.url).where(
                CompanyLookup.ats_type == "llm_scrape",
                CompanyLookup.status == "resolved",
                CompanyLookup.url.is_not(None),
            )
        ).all()
    )
    if len(rows) > _MAX_COMPANIES_PER_POLL:
        _log.warning(
            "llm_scrape: %d resolved companies exceeds the %d-per-poll cap (NFR-08) — "
            "polling the first %d only this cycle",
            len(rows),
            _MAX_COMPANIES_PER_POLL,
            _MAX_COMPANIES_PER_POLL,
        )
        rows = rows[:_MAX_COMPANIES_PER_POLL]

    if not rows:
        return LlmScrapeResult()

    client = Anthropic(api_key=settings.anthropic_api_key)
    model = settings.claude_extraction_model
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(today=datetime.now(UTC).date().isoformat())

    postings: list[RawPosting] = []
    total_input = 0
    total_output = 0
    for i, (name_raw, url) in enumerate(rows):
        if url is None:
            continue
        if i > 0:
            time.sleep(_RATE_LIMIT_SECONDS)
        try:
            html_content = _fetch_html(url)
        except (httpx.HTTPError, RetryError) as exc:
            _log.warning("llm_scrape: failed to fetch %s (%s): %s", name_raw, url, exc)
            continue

        page_text = html_to_linked_text(html_content, url)
        try:
            result = _extract_postings(client, system_prompt, model, name_raw, page_text)
        except Exception as exc:
            # Broad on purpose: a malformed/unexpected Claude response
            # (bad JSON, an unrecognized block type) must not abort every
            # remaining company in this loop, same "one bad company can't
            # take down the cycle" guarantee greenhouse.py/lever.py/
            # workday.py already enforce for their own failure modes.
            _log.warning("llm_scrape: extraction failed for %s: %s", name_raw, exc)
            continue

        postings.extend(result.postings)
        total_input += result.input_tokens
        total_output += result.output_tokens

    return LlmScrapeResult(
        postings=postings,
        companies_polled=len(rows),
        input_tokens=total_input,
        output_tokens=total_output,
        estimated_cost_usd=_estimate_cost(model, total_input, total_output),
    )
