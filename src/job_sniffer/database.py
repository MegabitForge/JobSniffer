"""SQLAlchemy persistence for job offers, evaluations, and scan profiles."""

import threading
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    JSON,
    Engine,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from job_sniffer.models import JobOffer, JobOfferEvaluation

DB_PATH = Path("job_sniffer.sqlite")


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
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
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


class ProfileRecord(Base):
    """Persisted scan profile with per-source settings."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    offer_limit: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC), nullable=False)

    source_settings: Mapped[list[ProfileSourceSettingRecord]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
    )


class ProfileSourceSettingRecord(Base):
    """Persisted per-source configuration within a scan profile."""

    __tablename__ = "profile_source_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    profile: Mapped[ProfileRecord] = relationship(back_populates="source_settings")


_ = Index(
    "ux_profile_source_settings_profile_source",
    ProfileSourceSettingRecord.profile_id,
    ProfileSourceSettingRecord.source_key,
    unique=True,
)


@dataclass(frozen=True)
class SaveStats:
    """Summary of a batch save operation."""

    seen: int
    inserted: int
    duplicates: int


@dataclass(frozen=True)
class ProfileSourceConfig:
    """Per-source configuration snapshot stored within a scan profile."""

    source_key: str
    enabled: bool
    filters: dict[str, str | list[str]]


def connect(path: Path) -> Session:
    """Open a SQLAlchemy session for a SQLite database file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    return Session(engine)


_ALEMBIC_DIR = Path(__file__).parent / "alembic"
_BASELINE_REVISION = "0001"
_MIGRATION_LOCK = threading.Lock()


def init_db(session: Session) -> None:
    """Bring the database schema up to the latest Alembic revision."""
    bind = session.get_bind()
    if not isinstance(bind, Engine):
        raise TypeError("init_db requires a Session bound to an Engine")

    with _MIGRATION_LOCK:
        config = AlembicConfig()
        config.set_main_option("script_location", str(_ALEMBIC_DIR))
        with bind.connect() as connection:
            config.attributes["connection"] = connection
            if _is_unversioned_legacy_database(bind):
                _upgrade_legacy_baseline(bind)
                command.stamp(config, _BASELINE_REVISION)
            command.upgrade(config, "head")


def _is_unversioned_legacy_database(bind: Engine) -> bool:
    inspector = inspect(bind)
    return inspector.has_table("job_offers") and not inspector.has_table("alembic_version")


def _upgrade_legacy_baseline(bind: Engine) -> None:
    inspector = inspect(bind)
    if not inspector.has_table("job_offer_evaluations"):
        return
    columns = {column["name"] for column in inspector.get_columns("job_offer_evaluations")}
    if "explanation" in columns:
        return
    with bind.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE job_offer_evaluations ADD COLUMN explanation TEXT NOT NULL DEFAULT '';"
            )
        )


@contextmanager
def db_session(path: Path = DB_PATH) -> Iterator[Session]:
    """Open a session on path, bring the schema up to date, and always close it."""
    session = connect(path)
    try:
        init_db(session)
        yield session
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


def source_enabled(settings: Mapping[str, ProfileSourceConfig], source_key: str) -> bool:
    """Return whether a source is enabled in per-source profile settings."""
    stored = settings.get(source_key)
    return stored is None or stored.enabled


def list_profiles(session: Session) -> list[ProfileRecord]:
    """Return all scan profiles ordered by name."""
    return list(session.scalars(select(ProfileRecord).order_by(ProfileRecord.name.asc())).all())


def create_profile(
    session: Session, name: str, offer_limit: int | None = None
) -> ProfileRecord | None:
    """Create a profile; return None for an empty or duplicate name."""
    stripped = name.strip()
    if not stripped:
        return None
    record = ProfileRecord(name=stripped, offer_limit=offer_limit)
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    return record


def update_profile(session: Session, profile_id: int, name: str, offer_limit: int | None) -> bool:
    """Update profile name and offer limit; return False when not possible."""
    record = session.get(ProfileRecord, profile_id)
    if record is None:
        return False
    stripped = name.strip()
    if not stripped:
        return False
    record.name = stripped
    record.offer_limit = offer_limit
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def delete_profile(session: Session, profile_id: int) -> bool:
    """Delete a profile together with its source settings."""
    record = session.get(ProfileRecord, profile_id)
    if record is None:
        return False
    session.delete(record)
    session.commit()
    return True


def get_profile(session: Session, profile_id: int) -> ProfileRecord | None:
    """Return a single profile by primary key."""
    return session.get(ProfileRecord, profile_id)


def get_profile_source_settings(
    session: Session, profile_id: int
) -> dict[str, ProfileSourceConfig]:
    """Return stored per-source settings for a profile."""
    records = session.scalars(
        select(ProfileSourceSettingRecord).where(
            ProfileSourceSettingRecord.profile_id == profile_id
        )
    ).all()
    return {
        record.source_key: ProfileSourceConfig(
            source_key=record.source_key,
            enabled=record.enabled,
            filters=dict(record.filters or {}),
        )
        for record in records
    }


def save_profile_source_settings(
    session: Session, profile_id: int, settings: Sequence[ProfileSourceConfig]
) -> None:
    """Upsert per-source settings for a profile."""
    existing = {
        record.source_key: record
        for record in session.scalars(
            select(ProfileSourceSettingRecord).where(
                ProfileSourceSettingRecord.profile_id == profile_id
            )
        ).all()
    }
    for setting in settings:
        record = existing.get(setting.source_key)
        if record is None:
            session.add(
                ProfileSourceSettingRecord(
                    profile_id=profile_id,
                    source_key=setting.source_key,
                    enabled=setting.enabled,
                    filters=setting.filters,
                )
            )
        else:
            record.enabled = setting.enabled
            record.filters = setting.filters
    session.commit()


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
        existing.explanation = evaluation.explanation
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
            explanation=evaluation.explanation,
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
        explanation=getattr(record, "explanation", ""),
        summary=record.summary,
        strengths=record.strengths,
        weaknesses=record.weaknesses,
        raw_response=record.raw_response,
        evaluated_at=record.evaluated_at,
    )


def get_offer_by_id(session: Session, offer_id: int) -> JobOfferRecord | None:
    """Retrieve a single job offer by primary key."""
    return session.get(JobOfferRecord, offer_id)


def delete_offer(session: Session, offer_id: int) -> bool:
    """Delete an offer and its associated evaluation."""
    record = session.get(JobOfferRecord, offer_id)
    if record is None:
        return False
    session.delete(record)
    session.commit()
    return True


def delete_evaluation(session: Session, offer_id: int) -> bool:
    """Delete only the AI evaluation for a specific offer, leaving the offer intact."""
    eval_rec = session.execute(
        select(JobOfferEvaluationRecord).where(JobOfferEvaluationRecord.offer_id == offer_id)
    ).scalar_one_or_none()
    if eval_rec is None:
        return False
    session.delete(eval_rec)
    session.commit()
    return True


def list_offers_with_evaluations(
    session: Session, limit: int | None = None
) -> list[tuple[JobOfferRecord, JobOfferEvaluationRecord | None]]:
    """Retrieve recent or all offers with their evaluations."""
    statement = (
        select(JobOfferRecord, JobOfferEvaluationRecord)
        .outerjoin(
            JobOfferEvaluationRecord,
            JobOfferRecord.id == JobOfferEvaluationRecord.offer_id,
        )
        .order_by(JobOfferRecord.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    results = session.execute(statement).all()
    return [(row[0], row[1]) for row in results]


def list_unevaluated_offers(session: Session, limit: int | None = None) -> list[JobOfferRecord]:
    """Retrieve job offers that do not yet have an AI evaluation."""
    statement = (
        select(JobOfferRecord)
        .outerjoin(
            JobOfferEvaluationRecord,
            JobOfferRecord.id == JobOfferEvaluationRecord.offer_id,
        )
        .where(JobOfferEvaluationRecord.offer_id.is_(None))
        .order_by(JobOfferRecord.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement).all())


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
