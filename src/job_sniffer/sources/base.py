"""Job source contracts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from job_sniffer.models import JobOffer, JobSearch

DuplicateChecker = Callable[[JobOffer], bool]


def filter_new_offers(
    offers: list[JobOffer],
    duplicate_checker: DuplicateChecker | None,
) -> list[JobOffer]:
    """Return offers that are not known duplicates."""
    if duplicate_checker is None:
        return offers
    return [offer for offer in offers if not duplicate_checker(offer)]


class JobSource(Protocol):
    """Contract implemented by all job source adapters."""

    source_name: str

    def search(self, search: JobSearch) -> Iterable[JobOffer]:
        """Return offers matching the search criteria."""
        ...
