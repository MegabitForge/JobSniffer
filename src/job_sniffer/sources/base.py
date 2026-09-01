"""Job source contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from job_sniffer.models import JobOffer, JobSearch


class JobSource(Protocol):
    """Contract implemented by all job source adapters."""

    source_name: str

    def search(self, search: JobSearch) -> Iterable[JobOffer]:
        """Return offers matching the search criteria."""
        ...
