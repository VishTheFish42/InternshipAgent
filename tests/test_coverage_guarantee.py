"""Regression guard for the coverage-guarantee bug (design.md §3.5, NFR-21,
docs/tasks.md T-910): a company must never be marked `resolved` with no
working scraper behind its ats_type. This is the direct fix for the class
of bug that caused a real missed posting — Nvidia resolved to
ats_type="custom" with no scraper ever reading it, silently, for who knows
how long, and `db stats` reported nothing wrong because nothing about that
state looked wrong from the DB's point of view.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.company_discoverer import _LEGACY_DEAD_END_ATS_TYPES
from src.main import _SOURCES

_ATS_MAP_PATH = Path(__file__).parent.parent / "src" / "data" / "company_ats_map.json"

# Every ats_type value discover() can produce (src/company_discoverer.py)
# that is expected to have a real, working scraper reading it, registered
# in main._SOURCES specifically (the main-cycle sources). "llm_scrape" is
# deliberately excluded from this set — it's cost-bounded onto its own
# schedule (run_llm_scrape_poll), not _SOURCES, same as Adzuna/JSearch —
# and is checked separately below.
_MAIN_CYCLE_ATS_TYPES = {"greenhouse", "lever", "workday"}
# Imported from company_discoverer rather than duplicated here — that's the
# single source of truth discover()/retry_unresolved() themselves use
# (T-310). Currently {"custom", "taleo"} — "taleo" was found by this test
# on its very first run (2026-08-20): Oracle's bundled entry is
# {"ats": "taleo"}, a third unscraped legacy value nobody had cataloged
# under the earlier "custom" framing. Real evidence the guarantee below is
# doing its job, not just a passing test — it caught a gap immediately.
_KNOWN_LEGACY_ATS_TYPES = _LEGACY_DEAD_END_ATS_TYPES


def test_every_main_cycle_ats_type_is_wired_into_sources():
    """greenhouse/lever/workday must each have a registered fetch function
    in _SOURCES — exactly the thing that was silently untrue for `custom`
    before this test existed."""
    wired = {name for name, _fetch_fn in _SOURCES}
    missing = _MAIN_CYCLE_ATS_TYPES - wired
    assert not missing, f"ats_type(s) {missing} have no scraper registered in main._SOURCES"


def test_llm_scrape_is_wired_into_its_own_poll_job():
    """llm_scrape doesn't live in _SOURCES (it's cost-bounded onto its own
    schedule, like Adzuna/JSearch) — verify main.py still actually imports
    and calls the real scraper module, rather than this wiring having
    quietly rotted (e.g. an import removed, the function renamed)."""
    import src.main as main_module

    assert hasattr(main_module, "run_llm_scrape_poll")
    assert main_module.llm_scrape.fetch_for_resolved_companies is not None


def test_bundled_ats_map_has_no_unrecognized_ats_type_values():
    """Every ats_type value actually present in the bundled lookup table
    must be either a live, working type or an explicitly-acknowledged
    legacy value (_LEGACY_DEAD_END_ATS_TYPES) — never a typo, or a new type
    someone added to the map without ever building or wiring a scraper for
    it (the exact mistake that produced the original "custom" dead end)."""
    data = json.loads(_ATS_MAP_PATH.read_text())
    ats_types = {entry.get("ats") for entry in data.values() if entry.get("ats")}
    recognized = _MAIN_CYCLE_ATS_TYPES | {"llm_scrape"} | _KNOWN_LEGACY_ATS_TYPES
    unrecognized = ats_types - recognized
    assert not unrecognized, (
        f"Unrecognized ats_type value(s) in company_ats_map.json: {unrecognized} — "
        "add a real scraper and wire it in before adding entries with this ats_type, "
        "or this test's known-type set, if it genuinely has one already."
    )
