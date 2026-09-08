"""Job source contracts."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from threading import Event
from typing import Protocol

from job_sniffer.models import JobOffer, JobSearch

DuplicateChecker = Callable[[JobOffer], bool]
EnrichedOfferHandler = Callable[[JobOffer], bool]


def dedupe_listing_offers(offers: Iterable[JobOffer]) -> list[JobOffer]:
    """Keep the first listing occurrence for each source-local offer identity."""
    result: list[JobOffer] = []
    seen: set[tuple[str, str]] = set()
    for offer in offers:
        key = offer_listing_key(offer)
        if key in seen:
            continue
        seen.add(key)
        result.append(offer)
    return result


def filter_new_offers(
    offers: list[JobOffer],
    duplicate_checker: DuplicateChecker | None,
) -> list[JobOffer]:
    """Return offers that are not known duplicates."""
    if duplicate_checker is None:
        return offers
    return [offer for offer in offers if not duplicate_checker(offer)]


def limit_offers(offers: list[JobOffer], limit: int | None) -> list[JobOffer]:
    """Apply an optional search limit."""
    return offers if limit is None else offers[:limit]


def wait_before_next_page(
    page: int,
    delay_seconds: tuple[float, float],
    stop_event: Event | None,
) -> float | None:
    """Wait before paginated listing pages after the first one."""
    if page <= 1:
        return None
    delay = random.uniform(*delay_seconds)
    if stop_event is not None:
        stop_event.wait(delay)
    else:
        time.sleep(delay)
    return delay


def offer_listing_key(offer: JobOffer) -> tuple[str, str]:
    """Return a stable key suitable for de-duplicating paginated listing rows."""
    if offer.external_id:
        return (offer.source, f"id:{offer.external_id}")
    if offer.url:
        return (offer.source, f"url:{offer.url}")
    location = offer.location.casefold() if offer.location else ""
    return (
        offer.source,
        f"title-company-location:{offer.title.casefold()}|{offer.company.casefold()}|{location}",
    )


def process_enriched_offers(
    offers: Iterable[JobOffer],
    enrich_offer: Callable[[JobOffer], JobOffer | None],
    enriched_offer_handler: EnrichedOfferHandler | None,
) -> list[JobOffer]:
    """Enrich offers sequentially and optionally handle each one immediately."""
    result: list[JobOffer] = []
    for offer in offers:
        enriched_offer = enrich_offer(offer)
        if enriched_offer is None:
            continue
        if enriched_offer_handler is not None and not enriched_offer_handler(enriched_offer):
            continue
        result.append(enriched_offer)
    return result


class JobSource(Protocol):
    """Contract implemented by all job source adapters."""

    source_name: str

    def search(self, search: JobSearch) -> Iterable[JobOffer]:
        """Return offers matching the search criteria."""
        ...
