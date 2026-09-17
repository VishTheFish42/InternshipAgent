"""Multi-tenant DB schema (T-1102) for the web app — a separate database and
DeclarativeBase from src/db.py's single-user worker schema, deliberately. The
worker keeps running unchanged against its own SQLite DB; this schema lives in
the Supabase project's Postgres instance (SUPABASE_DB_URL) and is migrated by
its own Alembic environment (alembic_web.ini / alembic_web/), not the worker's.
See docs/design.md §14.3 for the full data-model rationale.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Supabase's own user UUID (AuthUser.id) — the external identity this row
    # is keyed on; never generated locally.
    auth_provider_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String)
    telegram_link_token: Mapped[str | None] = mapped_column(String)
    notifications_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    min_relevance_score: Mapped[int] = mapped_column(Integer, default=15)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class UserProfileCache(Base):
    __tablename__ = "user_profile_cache"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    profile_hash: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CompanyLookup(Base):
    """Shared, app-wide company -> ATS resolution cache (design.md §14.5) — the
    same shape as src/db.py's CompanyLookup, but a separate table in this DB.
    Empty until T-1106/T-1107 wire in the shared-fetch rework; not populated
    by this task."""

    __tablename__ = "company_lookup"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_raw: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name_normalized: Mapped[str | None] = mapped_column(String)
    ats_type: Mapped[str | None] = mapped_column(String)
    slug: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    last_attempted: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str | None] = mapped_column(String)


class UserCompany(Base):
    __tablename__ = "user_companies"
    __table_args__ = (UniqueConstraint("user_id", "company_name_raw", name="uq_user_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    company_name_raw: Mapped[str] = mapped_column(String, nullable=False)
    company_lookup_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("company_lookup.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class JobPosting(Base):
    """Shared posting content only — no per-user scoring/notification state.
    Empty until T-1106/T-1107 wire in the shared-fetch rework; not populated
    by this task."""

    __tablename__ = "job_postings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    url: Mapped[str] = mapped_column(String, nullable=False)
    apply_url: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    found_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class UserPostingMatch(Base):
    __tablename__ = "user_posting_matches"
    __table_args__ = (UniqueConstraint("user_id", "job_posting_id", name="uq_user_posting_match"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_postings.id"), nullable=False
    )
    match_score: Mapped[int | None] = mapped_column(Integer)
    match_reasoning: Mapped[str | None] = mapped_column(Text)
    missing_qualifications: Mapped[list[Any] | None] = mapped_column(JSON)
    profile_hash: Mapped[str | None] = mapped_column(String)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)


# ── Schema init / session helpers ────────────────────────────────────────────


def init_db(database_url: str) -> Engine:
    """Return an engine for `database_url`. Unlike src/db.py's init_db(), this
    does NOT call Base.metadata.create_all() — schema changes here go through
    alembic_web's migrations from the start, to avoid the exact create_all/
    migration race documented in docs/tasks.md (2026-08-11) for the worker's
    original DB. Tests create tables directly via Base.metadata.create_all()
    against a throwaway in-memory SQLite engine instead of calling this."""
    return create_engine(database_url)


@contextmanager
def session_scope(engine: Engine) -> Generator[Session, None, None]:
    """Context manager yielding a Session that commits on success, rolls back
    on exception — same pattern as src/db.py's session_scope()."""
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_user(session: Session, auth_provider_id: str, email: str) -> User:
    """Look up a User by Supabase's auth_provider_id, creating one if this is
    the first time we've seen it — closes the T-1101 loop ("no app-side users
    row is created on signup") for both a fresh signup and a sign-in from an
    account that predates this schema (e.g. a test account created during
    T-1101's live verification, before this table existed)."""
    existing = session.execute(
        select(User).where(User.auth_provider_id == auth_provider_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(
        auth_provider_id=auth_provider_id,
        email=email,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(user)
    session.flush()
    return user
