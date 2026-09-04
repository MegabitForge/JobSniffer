"""SQLAlchemy persistence for job offers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Engine, Index, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from job_sniffer.models import JobOffer


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


def save_offer(session: Session, offer: JobOffer) -> bool:
    """Save an offer; return False when it is already present."""
    title_norm = _normalize(offer.title)
    company_norm = _normalize(offer.company)

    if _exists(session, offer, title_norm, company_norm):
        return False

    session.add(
        JobOfferRecord(
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
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def save_offers(session: Session, offers: list[JobOffer]) -> SaveStats:
    """Persist offers and return insert/duplicate counts."""
    inserted = 0
    for offer in offers:
        if save_offer(session, offer):
            inserted += 1

    seen = len(offers)
    return SaveStats(seen=seen, inserted=inserted, duplicates=seen - inserted)


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

    statement = select(JobOfferRecord.id).where(
        JobOfferRecord.title_norm == title_norm,
        JobOfferRecord.company_norm == company_norm,
    )
    return session.execute(statement).first() is not None


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _empty_to_none(value: str | None) -> str | None:
    return value if value else None
