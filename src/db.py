from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_source_external_id"),
        Index("idx_apply_url_normalized", "apply_url_normalized"),
        Index("idx_company_title_dedup", "company_normalized", "title_normalized", "found_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    company_normalized: Mapped[str | None] = mapped_column(String)
    title_normalized: Mapped[str | None] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    apply_url: Mapped[str | None] = mapped_column(String)
    apply_url_normalized: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    found_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    match_score: Mapped[int | None] = mapped_column(Integer)
    match_reasoning: Mapped[str | None] = mapped_column(Text)
    profile_hash: Mapped[str | None] = mapped_column(String)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    notifications: Mapped[list[Notification]] = relationship(
        "Notification", back_populates="job_posting"
    )


class CompanyLookup(Base):
    __tablename__ = "company_lookup"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name_raw: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name_normalized: Mapped[str | None] = mapped_column(String)
    ats_type: Mapped[str | None] = mapped_column(
        String
    )  # 'greenhouse' | 'lever' | 'workday' | 'custom'
    slug: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'resolved' | 'unresolved' | 'manual'
    last_attempted: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str | None] = mapped_column(String)  # 'bundled_table' | 'web_search' | 'manual'


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_posting_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_postings.id"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    phone_number: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    twilio_sid: Mapped[str | None] = mapped_column(String)
    delivery_status: Mapped[str | None] = mapped_column(String)  # 'sent' | 'delivered' | 'failed'

    job_posting: Mapped[JobPosting] = relationship("JobPosting", back_populates="notifications")


class RunLog(Base):
    __tablename__ = "run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    sources_polled: Mapped[list[Any] | None] = mapped_column(JSON)
    postings_found: Mapped[int] = mapped_column(Integer, default=0)
    postings_new: Mapped[int] = mapped_column(Integer, default=0)
    postings_scored: Mapped[int] = mapped_column(Integer, default=0)
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[Any] | None] = mapped_column(JSON)
    profile_hash: Mapped[str | None] = mapped_column(String)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)


# ── Schema init ───────────────────────────────────────────────────────────────


def init_db(database_url: str) -> Engine:
    """Create all tables (no-op if they already exist) and return the engine."""
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    return engine


# ── Session management ────────────────────────────────────────────────────────


@contextmanager
def session_scope(engine: Engine) -> Generator[Session, None, None]:
    """Yield a Session that auto-commits on clean exit and rolls back on error."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Internal helpers ──────────────────────────────────────────────────────────


def _now() -> datetime:
    """Current UTC time as a naive datetime (consistent with SQLAlchemy DateTime columns)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_text(s: str | None) -> str | None:
    """Lowercase and strip punctuation for dedup index columns."""
    if s is None:
        return None
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


# ── DB helpers ────────────────────────────────────────────────────────────────


def upsert_posting(session: Session, data: dict[str, Any]) -> tuple[JobPosting, bool]:
    """
    Insert a posting keyed by (source, external_id).

    Returns (posting, was_new). If the posting already exists the existing row
    is returned unchanged (was_new=False). Phase 4 will add URL and fuzzy dedup
    on top of this primary key check.
    """
    existing = session.execute(
        select(JobPosting).where(
            JobPosting.source == data["source"],
            JobPosting.external_id == data["external_id"],
        )
    ).scalar_one_or_none()

    if existing is not None:
        return existing, False

    kwargs = dict(data)
    kwargs.setdefault("found_at", _now())
    kwargs.setdefault("company_normalized", _normalize_text(kwargs.get("company")))
    kwargs.setdefault("title_normalized", _normalize_text(kwargs.get("title")))
    posting = JobPosting(**kwargs)
    session.add(posting)
    session.flush()
    return posting, True


def get_unscored_postings(session: Session) -> list[JobPosting]:
    """Return all postings that have not yet been scored (match_score IS NULL)."""
    return list(
        session.execute(select(JobPosting).where(JobPosting.match_score.is_(None))).scalars()
    )


def get_unnotified_above_threshold(session: Session, threshold: int) -> list[JobPosting]:
    """Return scored postings at or above threshold that haven't triggered an SMS yet."""
    return list(
        session.execute(
            select(JobPosting)
            .where(
                JobPosting.match_score >= threshold,
                JobPosting.notified == False,  # noqa: E712
            )
            .order_by(JobPosting.match_score.desc())
        ).scalars()
    )


def mark_notified(
    session: Session,
    posting_id: int,
    phone_number: str,
    message: str,
    twilio_sid: str | None = None,
) -> Notification:
    """Mark a posting as notified and record the outbound SMS."""
    now = _now()
    posting = session.get(JobPosting, posting_id)
    if posting is None:
        raise ValueError(f"JobPosting {posting_id} not found")
    posting.notified = True
    posting.notified_at = now

    notif = Notification(
        job_posting_id=posting_id,
        sent_at=now,
        phone_number=phone_number,
        message=message,
        twilio_sid=twilio_sid,
        delivery_status="sent",
    )
    session.add(notif)
    session.flush()
    return notif


def log_run(session: Session, data: dict[str, Any]) -> RunLog:
    """Insert a run_log entry and return it."""
    entry = RunLog(**data)
    session.add(entry)
    session.flush()
    return entry


def get_unresolved_companies(session: Session) -> list[CompanyLookup]:
    """Return all company_lookup rows whose career page could not be resolved."""
    return list(
        session.execute(select(CompanyLookup).where(CompanyLookup.status == "unresolved")).scalars()
    )


@dataclass
class DbStats:
    total_postings: int
    alerts_sent: int
    unresolved_companies: list[str] = field(default_factory=list)
    estimated_cost_usd: float = 0.0


def get_stats(session: Session) -> DbStats:
    """Aggregate stats used by `python -m src.db stats`."""
    total_postings = session.execute(select(func.count()).select_from(JobPosting)).scalar_one()

    alerts_sent = session.execute(select(func.count()).select_from(Notification)).scalar_one()

    unresolved = get_unresolved_companies(session)

    raw_cost = session.execute(select(func.sum(RunLog.estimated_cost_usd))).scalar_one()

    return DbStats(
        total_postings=total_postings,
        alerts_sent=alerts_sent,
        unresolved_companies=[c.name_raw for c in unresolved],
        estimated_cost_usd=raw_cost or 0.0,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    from pydantic_settings import BaseSettings, SettingsConfigDict

    class _CliSettings(BaseSettings):
        """Minimal settings for the DB CLI — only DATABASE_URL is needed."""

        model_config = SettingsConfigDict(
            env_file=".env", env_file_encoding="utf-8", extra="ignore"
        )
        database_url: str = "sqlite:///./internship_agent.db"

    parser = argparse.ArgumentParser(prog="python -m src.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create all tables (safe to run multiple times).")
    sub.add_parser("stats", help="Show posting counts, alerts, costs, and unresolved companies.")
    args = parser.parse_args()

    db_url = _CliSettings().database_url

    if args.command == "init":
        init_db(db_url)
        print(f"Database initialised: {db_url}")
        sys.exit(0)

    if args.command == "stats":
        engine = init_db(db_url)
        with session_scope(engine) as s:
            st = get_stats(s)

        sep = "─" * 52
        print(f"── InternshipAgent DB Stats {sep[27:]}")
        print(f"  Total postings:      {st.total_postings}")
        print(f"  Alerts sent:         {st.alerts_sent}")
        print(f"  Estimated API cost:  ${st.estimated_cost_usd:.4f}")

        if st.unresolved_companies:
            names = ", ".join(st.unresolved_companies)
            print(f"\n  Unresolved companies ({len(st.unresolved_companies)}): {names}")
        else:
            print("\n  Unresolved companies: none")

        sys.exit(0)
