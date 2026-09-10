"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class JobSearch:
    """Search criteria supported by job sources."""

    keywords: str
    location: str
    limit: int | None = None


@dataclass(frozen=True)
class JobOffer:
    """Canonical job offer saved by the application."""

    source: str
    external_id: str | None
    title: str
    company: str
    location: str | None
    url: str | None
    salary: str | None
    description_text: str | None
    posted_at: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class JobOfferEvaluation:
    """Evaluation of a job offer against a candidate's CV."""

    offer_id: int | None
    fit_score: int
    verdict: str
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    raw_response: dict[str, Any] | None = None
    evaluated_at: datetime | None = None
