"""Unit tests for scripts/update_ats_map.py — pure parsing/merge logic, no live network."""

from __future__ import annotations

import json

from scripts.update_ats_map import (
    build_new_entries,
    classify_url,
    extract_rows,
    merge,
    serialize,
)

# ── Fixture HTML, modeled on SimplifyJobs' actual table row structure ────────

_ROW_GREENHOUSE = """
<tr>
<td><strong><a href="https://simplify.jobs/c/Atoms?utm_source=GHList&utm_medium=company">Atoms</a></strong></td>
<td>Machine Learning Intern</td>
<td>Seattle, WA</td>
<td><div align="center"><a href="https://job-boards.greenhouse.io/cssmerge/jobs/8693034002?utm_source=Simplify"><img src="x" alt="Apply"></a> <a href="https://simplify.jobs/p/abc?utm_source=GHList"><img src="y" alt="Simplify"></a></div></td>
<td>0d</td>
</tr>
"""

_ROW_LEVER = """
<tr>
<td><strong><a href="https://simplify.jobs/c/Palantir?utm_source=GHList&utm_medium=company">Palantir</a></strong></td>
<td>Software Engineer Intern</td>
<td>NYC</td>
<td><div align="center"><a href="https://jobs.lever.co/palantir/xyz?utm_source=Simplify"><img src="x" alt="Apply"></a></div></td>
<td>0d</td>
</tr>
"""

_ROW_WORKDAY = """
<tr>
<td><strong><a href="https://simplify.jobs/c/Boeing?utm_source=GHList&utm_medium=company">Boeing</a></strong></td>
<td>Intern</td>
<td>Everett, WA</td>
<td><div align="center"><a href="https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS/job/1?utm_source=Simplify"><img src="x" alt="Apply"></a></div></td>
<td>0d</td>
</tr>
"""

_ROW_UNRECOGNIZED = """
<tr>
<td><strong><a href="https://simplify.jobs/c/TikTok?utm_source=GHList&utm_medium=company">TikTok</a></strong></td>
<td>Intern</td>
<td>Seattle, WA</td>
<td><div align="center"><a href="https://lifeattiktok.com/search/123?utm_source=Simplify"><img src="x" alt="Apply"></a></div></td>
<td>0d</td>
</tr>
"""

_ROW_MISSING_COMPANY_LINK = """
<tr>
<td>↳</td>
<td>Another Role</td>
<td>Remote</td>
<td><div align="center"><a href="https://job-boards.greenhouse.io/cssmerge/jobs/999?utm_source=Simplify"><img src="x" alt="Apply"></a></div></td>
<td>0d</td>
</tr>
"""


# ── classify_url ──────────────────────────────────────────────────────────────


def test_classify_url_greenhouse_job_boards_subdomain():
    assert classify_url("https://job-boards.greenhouse.io/cssmerge/jobs/1") == (
        "greenhouse",
        "cssmerge",
    )


def test_classify_url_greenhouse_classic_subdomain():
    assert classify_url("https://boards.greenhouse.io/stripe/jobs/1") == ("greenhouse", "stripe")


def test_classify_url_greenhouse_eu_subdomain():
    assert classify_url("https://job-boards.eu.greenhouse.io/imc/jobs/1") == ("greenhouse", "imc")


def test_classify_url_lever():
    assert classify_url("https://jobs.lever.co/palantir/xyz-123") == ("lever", "palantir")


def test_classify_url_workday_has_no_slug():
    assert classify_url("https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS/job/1") == (
        "workday",
        None,
    )


def test_classify_url_unrecognized_returns_none():
    assert classify_url("https://careers.example.com/job/1") is None


# ── extract_rows ──────────────────────────────────────────────────────────────


def test_extract_rows_parses_company_and_apply_url():
    html = f"<table><tbody>{_ROW_GREENHOUSE}</tbody></table>"
    rows = extract_rows(html)
    assert rows == [
        ("Atoms", "https://job-boards.greenhouse.io/cssmerge/jobs/8693034002?utm_source=Simplify")
    ]


def test_extract_rows_skips_rows_without_company_link():
    html = f"<table><tbody>{_ROW_MISSING_COMPANY_LINK}</tbody></table>"
    assert extract_rows(html) == []


def test_extract_rows_parses_multiple_rows():
    html = f"<table><tbody>{_ROW_GREENHOUSE}{_ROW_LEVER}{_ROW_WORKDAY}</tbody></table>"
    rows = extract_rows(html)
    assert len(rows) == 3
    assert rows[0][0] == "Atoms"
    assert rows[1][0] == "Palantir"
    assert rows[2][0] == "Boeing"


# ── build_new_entries ─────────────────────────────────────────────────────────


def test_build_new_entries_classifies_each_company():
    rows = extract_rows(
        f"<table><tbody>{_ROW_GREENHOUSE}{_ROW_LEVER}{_ROW_WORKDAY}</tbody></table>"
    )
    entries = build_new_entries(rows)
    assert entries["atoms"] == {"ats": "greenhouse", "slug": "cssmerge"}
    assert entries["palantir"] == {"ats": "lever", "slug": "palantir"}
    assert entries["boeing"] == {"ats": "workday"}


def test_build_new_entries_drops_unrecognized_urls():
    rows = extract_rows(f"<table><tbody>{_ROW_UNRECOGNIZED}</tbody></table>")
    entries = build_new_entries(rows)
    assert entries == {}


def test_build_new_entries_first_occurrence_wins_on_duplicate_company():
    rows = [
        ("Atoms", "https://job-boards.greenhouse.io/first/jobs/1"),
        ("Atoms", "https://job-boards.greenhouse.io/second/jobs/2"),
    ]
    entries = build_new_entries(rows)
    assert entries["atoms"]["slug"] == "first"


# ── merge ─────────────────────────────────────────────────────────────────────


def test_merge_adds_new_keys_only():
    existing = {"stripe": {"ats": "greenhouse", "slug": "stripe"}}
    new_entries = {"atoms": {"ats": "greenhouse", "slug": "cssmerge"}}
    merged, added = merge(existing, new_entries)
    assert added == 1
    assert merged["atoms"] == {"ats": "greenhouse", "slug": "cssmerge"}
    assert merged["stripe"] == {"ats": "greenhouse", "slug": "stripe"}


def test_merge_never_overwrites_existing_entry():
    existing = {"stripe": {"ats": "custom", "slug": "hand-curated"}}
    new_entries = {"stripe": {"ats": "greenhouse", "slug": "stripe"}}
    merged, added = merge(existing, new_entries)
    assert added == 0
    assert merged["stripe"] == {"ats": "custom", "slug": "hand-curated"}


def test_merge_empty_new_entries_is_a_noop():
    existing = {"stripe": {"ats": "greenhouse", "slug": "stripe"}}
    merged, added = merge(existing, {})
    assert added == 0
    assert merged == existing


# ── serialize ─────────────────────────────────────────────────────────────────


def test_serialize_produces_valid_json():
    entries = {"stripe": {"ats": "greenhouse", "slug": "stripe"}, "boeing": {"ats": "workday"}}
    text = serialize(entries)
    assert json.loads(text) == entries


def test_serialize_sorts_keys():
    entries = {"zeta": {"ats": "workday"}, "alpha": {"ats": "workday"}}
    text = serialize(entries)
    assert text.index('"alpha"') < text.index('"zeta"')


def test_serialize_one_line_per_company():
    entries = {"stripe": {"ats": "greenhouse", "slug": "stripe"}, "boeing": {"ats": "workday"}}
    text = serialize(entries)
    assert (
        text
        == '{\n  "boeing": {"ats": "workday"},\n  "stripe": {"ats": "greenhouse", "slug": "stripe"}\n}\n'
    )


def test_serialize_is_idempotent_on_the_real_bundled_map():
    """serialize() always sorts, so it isn't guaranteed to byte-match the
    existing file (which isn't fully sorted) — but re-serializing its own
    output must be a no-op, so a second run of the script never produces a
    spurious diff."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "src" / "data" / "company_ats_map.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    once = serialize(entries)
    twice = serialize(json.loads(once))
    assert once == twice
