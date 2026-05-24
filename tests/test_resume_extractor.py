"""Unit tests for merge_profiles — all run without PDF files or API calls."""

from __future__ import annotations

import logging

import pytest

from src.resume_extractor import (
    ExtractedProfile,
    MergedProfile,
    Project,
    WorkExperience,
    merge_profiles,
)


def _profile(**kwargs: object) -> ExtractedProfile:
    defaults: dict = {
        "languages": [],
        "frameworks": [],
        "tools": [],
        "experience_level": "junior",
        "graduation_year": 2027,
        "projects": [],
        "work_experience": [],
        "source_file": "test.pdf",
    }
    defaults.update(kwargs)
    return ExtractedProfile(**defaults)


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_merge_empty_list_returns_empty_profile() -> None:
    assert merge_profiles([]) == MergedProfile()


def test_merge_single_profile_passthrough() -> None:
    p = _profile(
        languages=["Python"],
        frameworks=["FastAPI"],
        tools=["Docker"],
        experience_level="sophomore",
        graduation_year=2027,
        projects=[Project("App", "A web app.", ["Python"])],
        work_experience=[WorkExperience("SWE Intern", "Acme", "Built APIs.")],
    )
    result = merge_profiles([p])
    assert result.languages == ["Python"]
    assert result.frameworks == ["FastAPI"]
    assert result.tools == ["Docker"]
    assert result.experience_level == "sophomore"
    assert result.graduation_year == 2027
    assert len(result.projects) == 1
    assert len(result.work_experience) == 1


# ── Language / framework / tool deduplication ─────────────────────────────────


def test_languages_dedup_case_insensitive() -> None:
    p1 = _profile(languages=["Python", "JavaScript"])
    p2 = _profile(languages=["python", "JAVASCRIPT", "Go"])
    result = merge_profiles([p1, p2])
    assert result.languages == ["Python", "JavaScript", "Go"]


def test_frameworks_dedup_case_insensitive() -> None:
    p1 = _profile(frameworks=["React", "FastAPI"])
    p2 = _profile(frameworks=["react", "PyTorch"])
    result = merge_profiles([p1, p2])
    assert result.frameworks == ["React", "FastAPI", "PyTorch"]


def test_tools_dedup_case_insensitive() -> None:
    p1 = _profile(tools=["Docker", "Git"])
    p2 = _profile(tools=["docker", "PostgreSQL"])
    result = merge_profiles([p1, p2])
    assert result.tools == ["Docker", "Git", "PostgreSQL"]


def test_first_seen_casing_is_kept() -> None:
    p1 = _profile(languages=["TypeScript"])
    p2 = _profile(languages=["typescript"])
    result = merge_profiles([p1, p2])
    assert result.languages == ["TypeScript"]


def test_three_profiles_full_union() -> None:
    p1 = _profile(languages=["Python"])
    p2 = _profile(languages=["Java"])
    p3 = _profile(languages=["python", "Go"])
    result = merge_profiles([p1, p2, p3])
    assert result.languages == ["Python", "Java", "Go"]


# ── Project deduplication ─────────────────────────────────────────────────────


def test_projects_longest_description_wins() -> None:
    p1 = _profile(projects=[Project("App", "Short.", ["Python"])])
    p2 = _profile(projects=[Project("App", "Much longer description here.", ["Python", "React"])])
    result = merge_profiles([p1, p2])
    assert len(result.projects) == 1
    assert result.projects[0].description == "Much longer description here."


def test_projects_dedup_name_case_insensitive() -> None:
    p1 = _profile(projects=[Project("image classifier", "Short desc.", [])])
    p2 = _profile(projects=[Project("Image Classifier", "Longer description wins.", [])])
    result = merge_profiles([p1, p2])
    assert len(result.projects) == 1


def test_projects_different_names_both_kept() -> None:
    p1 = _profile(projects=[Project("App A", "Desc A.", [])])
    p2 = _profile(projects=[Project("App B", "Desc B.", [])])
    result = merge_profiles([p1, p2])
    assert len(result.projects) == 2


def test_three_profiles_overlapping_projects() -> None:
    p1 = _profile(
        projects=[
            Project("Proj X", "Short.", []),
            Project("Proj Y", "Only in p1.", []),
        ]
    )
    p2 = _profile(
        projects=[
            Project("Proj X", "Medium length desc.", []),
        ]
    )
    p3 = _profile(
        projects=[
            Project("Proj X", "The longest description of them all.", []),
            Project("Proj Z", "Only in p3.", []),
        ]
    )
    result = merge_profiles([p1, p2, p3])
    assert len(result.projects) == 3
    proj_x = next(p for p in result.projects if "x" in p.name.lower())
    assert proj_x.description == "The longest description of them all."


# ── Work experience deduplication ─────────────────────────────────────────────


def test_work_experience_dedup_longest_description_wins() -> None:
    p1 = _profile(work_experience=[WorkExperience("SWE Intern", "Acme", "Short.")])
    p2 = _profile(work_experience=[WorkExperience("SWE Intern", "Acme", "Longer description.")])
    result = merge_profiles([p1, p2])
    assert len(result.work_experience) == 1
    assert result.work_experience[0].description == "Longer description."


def test_work_experience_different_companies_both_kept() -> None:
    p1 = _profile(work_experience=[WorkExperience("SWE Intern", "Acme", "Desc.")])
    p2 = _profile(work_experience=[WorkExperience("SWE Intern", "Globex", "Desc.")])
    result = merge_profiles([p1, p2])
    assert len(result.work_experience) == 2


def test_work_experience_dedup_title_case_insensitive() -> None:
    p1 = _profile(work_experience=[WorkExperience("swe intern", "Acme", "Short.")])
    p2 = _profile(work_experience=[WorkExperience("SWE Intern", "Acme", "Longer.")])
    result = merge_profiles([p1, p2])
    assert len(result.work_experience) == 1


# ── Scalar conflict resolution ────────────────────────────────────────────────


def test_most_recent_experience_level_wins() -> None:
    p1 = _profile(experience_level="freshman")
    p2 = _profile(experience_level="sophomore")
    assert merge_profiles([p1, p2]).experience_level == "sophomore"


def test_most_recent_graduation_year_wins() -> None:
    p1 = _profile(graduation_year=2026)
    p2 = _profile(graduation_year=2027)
    assert merge_profiles([p1, p2]).graduation_year == 2027


def test_no_conflict_no_warning(caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[name-defined]
    p1 = _profile(experience_level="junior", graduation_year=2027)
    p2 = _profile(experience_level="junior", graduation_year=2027)
    with caplog.at_level(logging.WARNING, logger="src.resume_extractor"):
        merge_profiles([p1, p2])
    assert caplog.records == []


def test_experience_level_conflict_logs_warning(caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[name-defined]
    p1 = _profile(experience_level="freshman")
    p2 = _profile(experience_level="sophomore")
    with caplog.at_level(logging.WARNING, logger="src.resume_extractor"):
        merge_profiles([p1, p2])
    assert any("experience_level" in r.message for r in caplog.records)


def test_graduation_year_conflict_logs_warning(caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[name-defined]
    p1 = _profile(graduation_year=2026)
    p2 = _profile(graduation_year=2027)
    with caplog.at_level(logging.WARNING, logger="src.resume_extractor"):
        merge_profiles([p1, p2])
    assert any("graduation_year" in r.message for r in caplog.records)
