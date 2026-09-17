"""Unit tests for src/web/db.py — the multi-tenant schema (T-1102). All run
against in-memory SQLite, same pattern as tests/test_db.py; no Postgres-specific
types are used in these models, so SQLite is a faithful stand-in for ORM-level
behavior. The real Postgres schema itself is verified separately via
`alembic -c alembic_web.ini upgrade head` against the live Supabase project."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.web.db import (
    Base,
    CompanyLookup,
    JobPosting,
    Resume,
    User,
    UserCompany,
    UserPostingMatch,
    UserProfileCache,
    get_or_create_user,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session(engine: Engine) -> Session:
    return Session(engine)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ── User ──────────────────────────────────────────────────────────────────────


def test_create_user(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    session.add(user)
    session.commit()

    assert user.id is not None
    assert user.notifications_paused is False
    assert user.min_relevance_score == 15


def test_user_auth_provider_id_is_unique(session: Session) -> None:
    session.add(User(auth_provider_id="auth-1", email="a@example.com", created_at=_now()))
    session.commit()

    session.add(User(auth_provider_id="auth-1", email="b@example.com", created_at=_now()))
    with pytest.raises(IntegrityError):
        session.commit()


def test_user_email_is_unique(session: Session) -> None:
    session.add(User(auth_provider_id="auth-1", email="a@example.com", created_at=_now()))
    session.commit()

    session.add(User(auth_provider_id="auth-2", email="a@example.com", created_at=_now()))
    with pytest.raises(IntegrityError):
        session.commit()


# ── get_or_create_user ────────────────────────────────────────────────────────


def test_get_or_create_user_creates_when_missing(session: Session) -> None:
    user = get_or_create_user(session, auth_provider_id="auth-1", email="a@example.com")
    session.commit()

    assert user.id is not None
    assert user.email == "a@example.com"


def test_get_or_create_user_returns_existing_without_duplicating(session: Session) -> None:
    first = get_or_create_user(session, auth_provider_id="auth-1", email="a@example.com")
    session.commit()

    second = get_or_create_user(session, auth_provider_id="auth-1", email="a@example.com")
    session.commit()

    assert first.id == second.id
    assert session.query(User).count() == 1


# ── Resume / UserProfileCache ────────────────────────────────────────────────


def test_create_resume(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    session.add(user)
    session.commit()

    resume = Resume(
        user_id=user.id,
        filename="resume.pdf",
        storage_key="resumes/auth-1/resume.pdf",
        sha256_hash="deadbeef",
        uploaded_at=_now(),
    )
    session.add(resume)
    session.commit()

    assert resume.is_active is True


def test_create_user_profile_cache(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    session.add(user)
    session.commit()

    cache = UserProfileCache(
        user_id=user.id,
        profile_json={"languages": ["Python"]},
        profile_hash="hash-1",
        updated_at=_now(),
    )
    session.add(cache)
    session.commit()

    fetched = session.get(UserProfileCache, user.id)
    assert fetched is not None
    assert fetched.profile_json == {"languages": ["Python"]}


# ── UserCompany ───────────────────────────────────────────────────────────────


def test_user_company_links_to_shared_company_lookup(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    session.add(user)
    session.commit()

    lookup = CompanyLookup(name_raw="Stripe", name_normalized="stripe", status="resolved")
    session.add(lookup)
    session.commit()

    uc = UserCompany(
        user_id=user.id, company_name_raw="Stripe", company_lookup_id=lookup.id, added_at=_now()
    )
    session.add(uc)
    session.commit()

    assert uc.id is not None


def test_user_company_allows_unresolved_lookup(session: Session) -> None:
    """company_lookup_id is nullable — a user can add a company before
    discovery has resolved it at all (or resolution never succeeds)."""
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    session.add(user)
    session.commit()

    uc = UserCompany(user_id=user.id, company_name_raw="Some New Co", added_at=_now())
    session.add(uc)
    session.commit()

    assert uc.company_lookup_id is None


def test_user_company_unique_per_user_and_name(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    session.add(user)
    session.commit()

    session.add(UserCompany(user_id=user.id, company_name_raw="Stripe", added_at=_now()))
    session.commit()

    session.add(UserCompany(user_id=user.id, company_name_raw="Stripe", added_at=_now()))
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_users_can_both_watch_the_same_company_name(session: Session) -> None:
    """The UNIQUE constraint is scoped to (user_id, company_name_raw) — two
    different users watching the same company must not collide."""
    user_a = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    user_b = User(auth_provider_id="auth-2", email="b@example.com", created_at=_now())
    session.add_all([user_a, user_b])
    session.commit()

    session.add(UserCompany(user_id=user_a.id, company_name_raw="Stripe", added_at=_now()))
    session.add(UserCompany(user_id=user_b.id, company_name_raw="Stripe", added_at=_now()))
    session.commit()  # must not raise

    assert session.query(UserCompany).count() == 2


# ── JobPosting / UserPostingMatch ─────────────────────────────────────────────


def test_job_posting_has_no_per_user_columns(session: Session) -> None:
    """Confirms the shared/per-user split from design.md §14.3 — job_postings
    here carries only content fields, no match_score/notified/applied."""
    posting = JobPosting(
        source="greenhouse:stripe",
        external_id="123",
        title="Software Engineering Intern",
        company="Stripe",
        url="https://stripe.com/jobs/123",
        found_at=_now(),
    )
    session.add(posting)
    session.commit()

    assert not hasattr(posting, "match_score")
    assert not hasattr(posting, "notified")


def test_user_posting_match_carries_the_per_user_state(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    posting = JobPosting(
        source="greenhouse:stripe",
        external_id="123",
        title="Software Engineering Intern",
        company="Stripe",
        url="https://stripe.com/jobs/123",
        found_at=_now(),
    )
    session.add_all([user, posting])
    session.commit()

    match = UserPostingMatch(
        user_id=user.id,
        job_posting_id=posting.id,
        match_score=85,
        match_reasoning="Strong fit",
        missing_qualifications=["Go"],
        notified=True,
        notified_at=_now(),
    )
    session.add(match)
    session.commit()

    assert match.applied is False


def test_two_users_can_have_independent_scores_for_the_same_posting(session: Session) -> None:
    user_a = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    user_b = User(auth_provider_id="auth-2", email="b@example.com", created_at=_now())
    posting = JobPosting(
        source="greenhouse:stripe",
        external_id="123",
        title="Software Engineering Intern",
        company="Stripe",
        url="https://stripe.com/jobs/123",
        found_at=_now(),
    )
    session.add_all([user_a, user_b, posting])
    session.commit()

    session.add(UserPostingMatch(user_id=user_a.id, job_posting_id=posting.id, match_score=90))
    session.add(UserPostingMatch(user_id=user_b.id, job_posting_id=posting.id, match_score=40))
    session.commit()  # must not raise — unique constraint is per (user, posting)

    scores = {m.user_id: m.match_score for m in session.query(UserPostingMatch).all()}
    assert scores == {user_a.id: 90, user_b.id: 40}


def test_user_posting_match_unique_per_user_and_posting(session: Session) -> None:
    user = User(auth_provider_id="auth-1", email="a@example.com", created_at=_now())
    posting = JobPosting(
        source="greenhouse:stripe",
        external_id="123",
        title="Software Engineering Intern",
        company="Stripe",
        url="https://stripe.com/jobs/123",
        found_at=_now(),
    )
    session.add_all([user, posting])
    session.commit()

    session.add(UserPostingMatch(user_id=user.id, job_posting_id=posting.id, match_score=90))
    session.commit()

    session.add(UserPostingMatch(user_id=user.id, job_posting_id=posting.id, match_score=95))
    with pytest.raises(IntegrityError):
        session.commit()
