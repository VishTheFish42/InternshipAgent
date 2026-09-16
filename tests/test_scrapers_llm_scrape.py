"""Unit tests for scrapers.llm_scrape — all HTTP and Claude calls are mocked."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.config import Settings
from src.db import Base, CompanyLookup
from src.scrapers.llm_scrape import (
    _extract_postings,
    _is_internship,
    _parse_posted_date,
    _slugify,
    fetch_for_resolved_companies,
    html_to_linked_text,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


def _settings(**overrides: object) -> Settings:
    base: dict = {"anthropic_api_key": "test-key"}
    base.update(overrides)
    return Settings(**base)


def _fake_message(text: str, input_tokens: int = 100, output_tokens: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# ── _is_internship ───────────────────────────────────────────────────────────


def test_is_internship_matches_intern():
    assert _is_internship("Software Engineering Intern")


def test_is_internship_matches_coop():
    assert _is_internship("Design Co-op")


def test_is_internship_rejects_internal_false_positive():
    assert not _is_internship("Internal Product Manager 5")


# ── _slugify ──────────────────────────────────────────────────────────────────


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Acme Corp, Inc.") == "acme-corp-inc"


def test_slugify_falls_back_when_nothing_alphanumeric():
    assert _slugify("!!!") == "company"


# ── html_to_linked_text ───────────────────────────────────────────────────────


def test_html_to_linked_text_renders_anchors_as_markdown_links():
    html = """
    <html><body>
      <nav>Home | About</nav>
      <div class="listing">
        <a href="/jobs/123">Software Engineering Intern</a>
      </div>
    </body></html>
    """
    text = html_to_linked_text(html, "https://acme.example.com/careers")
    assert "[Software Engineering Intern](https://acme.example.com/jobs/123)" in text
    assert "Home" in text and "About" in text


def test_html_to_linked_text_strips_script_and_style_content():
    html = """
    <html><body>
      <script>var jobs = [{"title": "leaked from JS, not real content"}];</script>
      <style>.job { color: red; }</style>
      <p>Real visible text</p>
    </body></html>
    """
    text = html_to_linked_text(html, "https://acme.example.com/careers")
    assert "leaked from JS" not in text
    assert "color: red" not in text
    assert "Real visible text" in text


def test_html_to_linked_text_resolves_relative_hrefs_against_base_url():
    html = '<a href="jobs/456">Data Science Intern</a>'
    text = html_to_linked_text(html, "https://acme.example.com/careers/")
    assert "https://acme.example.com/careers/jobs/456" in text


# ── _parse_posted_date ────────────────────────────────────────────────────────


def test_parse_posted_date_valid_iso():
    assert _parse_posted_date("2026-08-18") == datetime(2026, 8, 18)


def test_parse_posted_date_none_when_absent():
    assert _parse_posted_date(None) is None


def test_parse_posted_date_none_when_unparseable():
    assert _parse_posted_date("last week sometime") is None


# ── _extract_postings ─────────────────────────────────────────────────────────


def test_extract_postings_maps_fields():
    raw = (
        '[{"title": "Software Engineering Intern", '
        '"url": "https://acme.example.com/jobs/1", '
        '"location": "Remote", "posted_date": "2026-08-18", '
        '"description": "Great role"}]'
    )
    client = MagicMock()
    client.messages.create.return_value = _fake_message(raw)

    result = _extract_postings(
        client, "system prompt", "claude-haiku-4-5-20251001", "Acme", "page text"
    )

    assert len(result.postings) == 1
    posting = result.postings[0]
    assert posting.source == "llm_scrape:acme"
    assert posting.external_id == "https://acme.example.com/jobs/1"
    assert posting.title == "Software Engineering Intern"
    assert posting.company == "Acme"
    assert posting.is_remote is True
    assert posting.apply_url == "https://acme.example.com/jobs/1"
    assert posting.description == "Great role"
    assert posting.posted_at == datetime(2026, 8, 18)
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_extract_postings_filters_non_internship_titles():
    raw = (
        '[{"title": "Senior Staff Engineer", "url": "https://acme.example.com/jobs/2", '
        '"location": null, "posted_date": null, "description": null}]'
    )
    client = MagicMock()
    client.messages.create.return_value = _fake_message(raw)

    result = _extract_postings(
        client, "system prompt", "claude-haiku-4-5-20251001", "Acme", "page text"
    )
    assert result.postings == []


def test_extract_postings_skips_items_missing_url_or_title():
    raw = (
        "["
        '{"title": "Intern", "url": null, "location": null, "posted_date": null, "description": null},'
        '{"title": null, "url": "https://acme.example.com/jobs/3", "location": null, '
        '"posted_date": null, "description": null}'
        "]"
    )
    client = MagicMock()
    client.messages.create.return_value = _fake_message(raw)

    result = _extract_postings(
        client, "system prompt", "claude-haiku-4-5-20251001", "Acme", "page text"
    )
    assert result.postings == []


def test_extract_postings_handles_empty_array():
    client = MagicMock()
    client.messages.create.return_value = _fake_message("[]")

    result = _extract_postings(
        client, "system prompt", "claude-haiku-4-5-20251001", "Acme", "page text"
    )
    assert result.postings == []


def test_extract_postings_strips_markdown_fences():
    raw = (
        '```json\n[{"title": "Intern", "url": "https://acme.example.com/jobs/4", '
        '"location": null, "posted_date": null, "description": null}]\n```'
    )
    client = MagicMock()
    client.messages.create.return_value = _fake_message(raw)

    result = _extract_postings(
        client, "system prompt", "claude-haiku-4-5-20251001", "Acme", "page text"
    )
    assert len(result.postings) == 1


# ── fetch_for_resolved_companies ─────────────────────────────────────────────


def test_fetch_for_resolved_companies_only_queries_resolved_llm_scrape(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="Acme",
                ats_type="llm_scrape",
                url="https://acme.example.com/careers",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Stripe", ats_type="greenhouse", slug="stripe", status="resolved"
            ),
            CompanyLookup(
                name_raw="Widget Co",
                ats_type="llm_scrape",
                url="https://widgetco.example.com/careers",
                status="unresolved",
            ),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.llm_scrape._fetch_html", return_value="<html></html>") as mock_fetch,
        patch("src.scrapers.llm_scrape.Anthropic") as mock_anthropic_cls,
        patch("src.scrapers.llm_scrape.time.sleep") as mock_sleep,
    ):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_message("[]")
        mock_anthropic_cls.return_value = mock_client

        result = fetch_for_resolved_companies(session, _settings())

    mock_fetch.assert_called_once_with("https://acme.example.com/careers")
    mock_sleep.assert_not_called()
    assert result.companies_polled == 1


def test_fetch_for_resolved_companies_rate_limits_between_calls(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="Acme",
                ats_type="llm_scrape",
                url="https://acme.example.com/careers",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Widget Co",
                ats_type="llm_scrape",
                url="https://widgetco.example.com/careers",
                status="resolved",
            ),
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.llm_scrape._fetch_html", return_value="<html></html>") as mock_fetch,
        patch("src.scrapers.llm_scrape.Anthropic") as mock_anthropic_cls,
        patch("src.scrapers.llm_scrape.time.sleep") as mock_sleep,
    ):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_message("[]")
        mock_anthropic_cls.return_value = mock_client

        fetch_for_resolved_companies(session, _settings())

    assert mock_fetch.call_count == 2
    mock_sleep.assert_called_once_with(5.0)


def test_fetch_for_resolved_companies_skips_company_on_fetch_failure(session):
    import httpx

    session.add_all(
        [
            CompanyLookup(
                name_raw="Broken",
                ats_type="llm_scrape",
                url="https://broken.example.com/careers",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Acme",
                ats_type="llm_scrape",
                url="https://acme.example.com/careers",
                status="resolved",
            ),
        ]
    )
    session.commit()

    def fake_fetch_html(url: str) -> str:
        if "broken" in url:
            raise httpx.HTTPError("boom")
        return "<html><a href='/jobs/1'>Software Intern</a></html>"

    with (
        patch("src.scrapers.llm_scrape._fetch_html", side_effect=fake_fetch_html),
        patch("src.scrapers.llm_scrape.Anthropic") as mock_anthropic_cls,
        patch("src.scrapers.llm_scrape.time.sleep"),
    ):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_message(
            '[{"title": "Software Intern", "url": "https://acme.example.com/jobs/1", '
            '"location": null, "posted_date": null, "description": null}]'
        )
        mock_anthropic_cls.return_value = mock_client

        result = fetch_for_resolved_companies(session, _settings())

    # Acme must still be processed despite Broken's fetch failure — a bad
    # company earlier in the list must not abort the rest.
    assert len(result.postings) == 1
    assert result.postings[0].company == "Acme"


def test_fetch_for_resolved_companies_skips_company_on_extraction_failure(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw="BadJson",
                ats_type="llm_scrape",
                url="https://badjson.example.com/careers",
                status="resolved",
            ),
            CompanyLookup(
                name_raw="Acme",
                ats_type="llm_scrape",
                url="https://acme.example.com/careers",
                status="resolved",
            ),
        ]
    )
    session.commit()

    call_count = {"n": 0}

    def fake_create(**kwargs: object) -> SimpleNamespace:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _fake_message("not valid json")
        return _fake_message(
            '[{"title": "Software Intern", "url": "https://acme.example.com/jobs/1", '
            '"location": null, "posted_date": null, "description": null}]'
        )

    with (
        patch("src.scrapers.llm_scrape._fetch_html", return_value="<html></html>"),
        patch("src.scrapers.llm_scrape.Anthropic") as mock_anthropic_cls,
        patch("src.scrapers.llm_scrape.time.sleep"),
    ):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = fake_create
        mock_anthropic_cls.return_value = mock_client

        result = fetch_for_resolved_companies(session, _settings())

    assert len(result.postings) == 1
    assert result.postings[0].company == "Acme"


def test_fetch_for_resolved_companies_returns_empty_result_without_touching_claude(session):
    with (
        patch("src.scrapers.llm_scrape.Anthropic") as mock_anthropic_cls,
    ):
        result = fetch_for_resolved_companies(session, _settings())

    mock_anthropic_cls.assert_not_called()
    assert result.postings == []
    assert result.companies_polled == 0
    assert result.estimated_cost_usd == 0.0


def test_fetch_for_resolved_companies_caps_at_max_companies_per_poll(session):
    session.add_all(
        [
            CompanyLookup(
                name_raw=f"Company{i}",
                ats_type="llm_scrape",
                url=f"https://company{i}.example.com/careers",
                status="resolved",
            )
            for i in range(105)
        ]
    )
    session.commit()

    with (
        patch("src.scrapers.llm_scrape._fetch_html", return_value="<html></html>") as mock_fetch,
        patch("src.scrapers.llm_scrape.Anthropic") as mock_anthropic_cls,
        patch("src.scrapers.llm_scrape.time.sleep"),
    ):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_message("[]")
        mock_anthropic_cls.return_value = mock_client

        result = fetch_for_resolved_companies(session, _settings())

    assert mock_fetch.call_count == 100
    assert result.companies_polled == 100
