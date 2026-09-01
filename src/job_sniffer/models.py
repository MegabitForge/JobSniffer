"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobSearch:
    """Search criteria supported by job sources."""

    keywords: str
    location: str
    limit: int = 25


@dataclass(frozen=True)
class JobOffer:
    """Canonical job offer saved by the application."""

    source: str
    external_id: str | None
    title: str
    company: str
    location: str | None
    url: str | None
    apply_url: str | None
    salary: str | None
    applicant_count: str | None
    description_text: str | None
    posted_at: str | None
    raw: dict[str, Any]
