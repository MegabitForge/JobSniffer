"""SQLAlchemy persistence for job offers and their AI evaluations."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Engine, ForeignKey, Index, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from job_sniffer.models import JobOffer, JobOfferEvaluation


class Base(DeclarativeBase):
    """Base class for ORM records."""


class JobOfferRecord(Base):
    """Persisted job offer record."""

    __tablename__ = "job_offers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    salary: Mapped[str | None] = mapped_column(String)
    description_text: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[str | None] = mapped_column(String)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    title_norm: Mapped[str] = mapped_column(String, nullable=False)
    company_norm: Mapped[str] = mapped_column(String, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC), nullable=False)

    evaluation: Mapped[JobOfferEvaluationRecord | None] = relationship(
        "JobOfferEvaluationRecord",
        back_populates="offer",
        uselist=False,
        cascade="all, delete-orphan",
    )


class JobOfferEvaluationRecord(Base):
    """Persisted evaluation of a job offer against candidate CV."""

    __tablename__ = "job_offer_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    offer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_offers.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    strengths: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    weaknesses: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC), nullable=False
    )

    offer: Mapped[JobOfferRecord] = relationship("JobOfferRecord", back_populates="evaluation")


_ = Index(
    "ux_job_source_external_id",
    JobOfferRecord.source,
    JobOfferRecord.external_id,
    unique=True,
    sqlite_where=(JobOfferRecord.external_id.is_not(None) & (JobOfferRecord.external_id != "")),
)
_ = Index(
    "ux_job_source_url",
    JobOfferRecord.source,
    JobOfferRecord.url,
    unique=True,
    sqlite_where=(JobOfferRecord.url.is_not(None) & (JobOfferRecord.url != "")),
)


@dataclass(frozen=True)
class SaveStats:
    """Summary of a batch save operation."""

    seen: int
    inserted: int
    duplicates: int


def connect(path: Path) -> Session:
    """Open a SQLAlchemy session for a SQLite database file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    return Session(engine)


def init_db(session: Session) -> None:
    """Create the current baseline schema when it does not exist."""
    bind = session.get_bind()
    if not isinstance(bind, Engine):
        raise TypeError("init_db requires a Session bound to an Engine")
    Base.metadata.create_all(bind)


def save_offer(session: Session, offer: JobOffer) -> int | None:
    """Save an offer; return inserted record ID or None if already present."""
    title_norm = _normalize(offer.title)
    company_norm = _normalize(offer.company)

    if _exists(session, offer, title_norm, company_norm):
        return None

    record = JobOfferRecord(
        source=offer.source,
        external_id=_empty_to_none(offer.external_id),
        title=offer.title,
        company=offer.company,
        location=offer.location,
        url=_empty_to_none(offer.url),
        salary=offer.salary,
        description_text=offer.description_text,
        posted_at=offer.posted_at,
        raw_json=offer.raw,
        title_norm=title_norm,
        company_norm=company_norm,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    return record.id


def save_evaluation(session: Session, evaluation: JobOfferEvaluation) -> int | None:
    """Save or update evaluation for an offer."""
    if evaluation.offer_id is None:
        return None

    existing = session.execute(
        select(JobOfferEvaluationRecord).where(
            JobOfferEvaluationRecord.offer_id == evaluation.offer_id
        )
    ).scalar_one_or_none()

    if existing:
        existing.fit_score = evaluation.fit_score
        existing.verdict = evaluation.verdict
        existing.summary = evaluation.summary
        existing.strengths = evaluation.strengths
        existing.weaknesses = evaluation.weaknesses
        existing.raw_response = evaluation.raw_response
        existing.evaluated_at = evaluation.evaluated_at or datetime.now(UTC)
        record = existing
    else:
        record = JobOfferEvaluationRecord(
            offer_id=evaluation.offer_id,
            fit_score=evaluation.fit_score,
            verdict=evaluation.verdict,
            summary=evaluation.summary,
            strengths=evaluation.strengths,
            weaknesses=evaluation.weaknesses,
            raw_response=evaluation.raw_response,
            evaluated_at=evaluation.evaluated_at or datetime.now(UTC),
        )
        session.add(record)

    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    return record.id


def get_evaluation(session: Session, offer_id: int) -> JobOfferEvaluation | None:
    """Retrieve evaluation for a specific offer."""
    record = session.execute(
        select(JobOfferEvaluationRecord).where(JobOfferEvaluationRecord.offer_id == offer_id)
    ).scalar_one_or_none()
    if not record:
        return None
    return JobOfferEvaluation(
        offer_id=record.offer_id,
        fit_score=record.fit_score,
        verdict=record.verdict,
        summary=record.summary,
        strengths=record.strengths,
        weaknesses=record.weaknesses,
        raw_response=record.raw_response,
        evaluated_at=record.evaluated_at,
    )


def list_offers_with_evaluations(
    session: Session, limit: int = 50
) -> list[tuple[JobOfferRecord, JobOfferEvaluationRecord | None]]:
    """Retrieve recent offers with their evaluations."""
    statement = (
        select(JobOfferRecord, JobOfferEvaluationRecord)
        .outerjoin(
            JobOfferEvaluationRecord,
            JobOfferRecord.id == JobOfferEvaluationRecord.offer_id,
        )
        .order_by(JobOfferRecord.id.desc())
        .limit(limit)
    )
    results = session.execute(statement).all()
    return [(row[0], row[1]) for row in results]


def offer_exists(session: Session, offer: JobOffer) -> bool:
    """Return True when an offer would be treated as a duplicate."""
    return _exists(session, offer, _normalize(offer.title), _normalize(offer.company))


def _exists(
    session: Session,
    offer: JobOffer,
    title_norm: str,
    company_norm: str,
) -> bool:
    if offer.external_id:
        statement = select(JobOfferRecord.id).where(
            JobOfferRecord.source == offer.source,
            JobOfferRecord.external_id == offer.external_id,
        )
        if session.execute(statement).first() is not None:
            return True

    if offer.url:
        statement = select(JobOfferRecord.id).where(
            JobOfferRecord.source == offer.source,
            JobOfferRecord.url == offer.url,
        )
        if session.execute(statement).first() is not None:
            return True

    location_candidates = select(JobOfferRecord.source, JobOfferRecord.location).where(
        JobOfferRecord.title_norm == title_norm,
        JobOfferRecord.company_norm == company_norm,
    )
    for existing_source, existing_location in session.execute(location_candidates):
        if _same_location(existing_location, offer.location):
            return True
        if existing_source == offer.source and not existing_location and not offer.location:
            return True
    return False


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _same_location(left: str | None, right: str | None) -> bool:
    left_key = _normalize_location(left)
    right_key = _normalize_location(right)
    return bool(left_key and right_key and left_key == right_key)


def _normalize_location(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.casefold().translate(str.maketrans("ł", "l"))
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in normalized).split())


def _empty_to_none(value: str | None) -> str | None:
    return value if value else None
