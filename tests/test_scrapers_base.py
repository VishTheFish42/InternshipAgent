"""Unit tests for scrapers.base — the shared RawPosting type and query list."""

from __future__ import annotations

from src.scrapers.base import build_queries


def test_build_queries_includes_core_software_terms():
    queries = build_queries()
    assert "software engineering intern" in queries
    assert "backend intern" in queries


def test_build_queries_includes_coop_terms():
    queries = build_queries()
    assert "software engineering co-op" in queries


def test_build_queries_includes_ai_and_data_engineering_terms():
    """Extended beyond pure software engineering per explicit request —
    AI/data-titled roles were being missed by the original term list."""
    queries = build_queries()
    assert "AI engineer intern" in queries
    assert "data engineer intern" in queries
    assert "machine learning research intern" in queries
    assert "AI engineer co-op" in queries
    assert "data engineer co-op" in queries


def test_build_queries_returns_no_duplicates():
    queries = build_queries()
    assert len(queries) == len(set(queries))
