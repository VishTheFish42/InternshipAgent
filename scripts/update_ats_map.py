"""Refresh src/data/company_ats_map.json from SimplifyJobs' internship tracker.

Fetches the live README, extracts (company, application URL) pairs from its
HTML job tables, classifies each URL against known ATS URL patterns
(Greenhouse, Lever, Workday), and adds any company not already present in the
bundled map. Existing entries are never overwritten — some may be
hand-curated or point at custom scrapers, and a scrape shouldn't silently
clobber that.

Usage:
    python scripts/update_ats_map.py
    python scripts/update_ats_map.py --dry-run   # report without writing

NOTE: SimplifyJobs renames its repo each admissions cycle (e.g.
Summer2026-Internships -> Summer2027-Internships). If this script starts
404ing, update _README_URL below to the current cycle's repo.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import httpx

_README_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
_ATS_MAP_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "company_ats_map.json"

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_COMPANY_RE = re.compile(r'simplify\.jobs/c/[^"]*">([^<]+)</a>')
_APPLY_URL_RE = re.compile(r'<a href="([^"]+)"><img[^>]*alt="Apply"')

_GREENHOUSE_RE = re.compile(r"(?:job-)?boards\.(?:[a-z]+\.)?greenhouse\.io/([a-zA-Z0-9_-]+)")
_LEVER_RE = re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)")
_WORKDAY_RE = re.compile(r"https?://([a-zA-Z0-9-]+)\.wd\d+\.myworkdayjobs\.com")


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def fetch_readme(url: str = _README_URL) -> str:
    resp = httpx.get(url, timeout=15.0)
    resp.raise_for_status()
    return resp.text


def extract_rows(html: str) -> list[tuple[str, str]]:
    """Return (company_name, apply_url) for every row with both a recognizable
    company link and an Apply link. Rows missing either — e.g. grouped
    continuation rows for a company's Nth posting — are skipped rather than
    guessed at."""
    pairs: list[tuple[str, str]] = []
    for row in _ROW_RE.findall(html):
        company_match = _COMPANY_RE.search(row)
        apply_match = _APPLY_URL_RE.search(row)
        if company_match and apply_match:
            pairs.append((company_match.group(1), apply_match.group(1)))
    return pairs


def classify_url(url: str) -> tuple[str, str | None] | None:
    """Return (ats_type, slug) if the URL matches a known ATS pattern, else None."""
    m = _GREENHOUSE_RE.search(url)
    if m:
        return "greenhouse", m.group(1)
    m = _LEVER_RE.search(url)
    if m:
        return "lever", m.group(1)
    m = _WORKDAY_RE.search(url)
    if m:
        return "workday", None
    return None


def build_new_entries(rows: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    """Classify every row into a {normalized_name: {ats, slug?}} map. Rows
    whose URL doesn't match a known ATS pattern are dropped — those companies
    are left to the existing web-search fallback in company_discoverer.py."""
    entries: dict[str, dict[str, str]] = {}
    for company, url in rows:
        key = _normalize(company)
        if key in entries:
            continue
        classified = classify_url(url)
        if classified is None:
            continue
        ats_type, slug = classified
        entry: dict[str, str] = {"ats": ats_type}
        if slug:
            entry["slug"] = slug
        entries[key] = entry
    return entries


def merge(
    existing: dict[str, Any], new_entries: dict[str, dict[str, str]]
) -> tuple[dict[str, Any], int]:
    """Add only genuinely new keys — never overwrite an existing entry."""
    merged = dict(existing)
    added = 0
    for key, entry in new_entries.items():
        if key not in merged:
            merged[key] = entry
            added += 1
    return merged, added


def serialize(entries: dict[str, Any]) -> str:
    """Match the bundled map's existing style: one compact line per company,
    sorted by key, so a diff only shows genuinely new entries instead of
    reformatting the whole file."""
    items = sorted(entries.items())
    lines = ["{"]
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, separators=(', ', ': '))}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing the file."
    )
    args = parser.parse_args()

    print(f"Fetching {_README_URL} ...")
    html = fetch_readme()

    rows = extract_rows(html)
    print(f"Parsed {len(rows)} postings with a recognizable company + apply link.")

    new_entries = build_new_entries(rows)
    print(f"Classified {len(new_entries)} unique companies against known ATS patterns.")

    existing = json.loads(_ATS_MAP_PATH.read_text(encoding="utf-8"))
    merged, added = merge(existing, new_entries)

    print(f"{added} new companies would be added (out of {len(existing)} existing).")

    if args.dry_run:
        print("Dry run — not writing.")
        return

    if added == 0:
        print("Nothing to add — leaving file unchanged.")
        return

    _ATS_MAP_PATH.write_text(serialize(merged), encoding="utf-8")
    print(f"Wrote {_ATS_MAP_PATH} ({len(merged)} total entries).")


if __name__ == "__main__":
    main()
