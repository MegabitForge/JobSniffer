"""Domain models."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

FilterValue = str | list[str]


def text_filter_value(stored: FilterValue | None) -> str:
    """Flatten a stored filter value into one string; multiple values are comma-separated."""
    if isinstance(stored, list):
        return ", ".join(stored)
    return stored or ""


@dataclass(frozen=True)
class JobSearch:
    """Search criteria supported by job sources."""

    filters: Mapping[str, FilterValue]
    limit: int | None = None

    def text(self, key: str) -> str:
        """Return a filter as stripped text; an unset filter is an empty string."""
        return text_filter_value(self.filters.get(key)).strip()

    def values(self, key: str) -> list[str]:
        """Return a filter as a list of non-empty values."""
        stored = self.filters.get(key)
        items = [stored] if isinstance(stored, str) else (stored or [])
        return [item.strip() for item in items if item.strip()]


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
    explanation: str
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    raw_response: dict[str, Any] | None = None
    evaluated_at: datetime | None = None
