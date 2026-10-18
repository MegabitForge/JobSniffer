"""Job source contracts and the scan loop shared by all sources."""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Literal, Protocol

from job_sniffer.models import JobOffer, JobSearch

logger = logging.getLogger(__name__)

DuplicateChecker = Callable[[JobOffer], bool]
EnrichedOfferHandler = Callable[[JobOffer], bool]


class ScanCancelledError(RuntimeError):
    """Raised when the application stops a scan in progress."""


@dataclass(frozen=True)
class ScanProgress:
    """Progress report emitted by a source during a scan."""

    stage: Literal["listing", "enrich"]
    current: int
    total: int | None


ProgressHandler = Callable[[ScanProgress], None]


@dataclass(frozen=True)
class ScanHooks:
    """Callbacks connecting a source to the application during one scan.

    ``duplicate_checker`` tells whether a listing offer is already stored, and
    ``enriched_offer_handler`` stores an enriched offer, returning False for a duplicate.
    """

    stop_event: Event | None = None
    duplicate_checker: DuplicateChecker | None = None
    enriched_offer_handler: EnrichedOfferHandler | None = None
    progress_handler: ProgressHandler | None = None

    def is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def raise_if_stopped(self) -> None:
        if self.is_stopped():
            raise ScanCancelledError("Scan cancelled because the application is closing.")

    def wait(self, delay_seconds: tuple[float, float]) -> None:
        """Sleep for a random delay, waking up early when the scan is stopped."""
        delay = random.uniform(*delay_seconds)
        if self.stop_event is not None:
            self.stop_event.wait(delay)
        else:
            time.sleep(delay)
        self.raise_if_stopped()

    def report_progress(self, progress: ScanProgress) -> None:
        if self.progress_handler is not None:
            self.progress_handler(progress)


@dataclass(frozen=True)
class Pacing:
    """Pagination bounds and delays that keep scans polite to the job boards."""

    max_pages: int = 20
    page_delay_seconds: tuple[float, float] = (2.0, 4.0)
    detail_delay_seconds: tuple[float, float] = (2.0, 4.0)


DEFAULT_PACING = Pacing()


@dataclass
class ScanReport:
    """What happened to the offers a source found during one scan."""

    pages: int = 0
    listed: int = 0
    filtered_out: int = 0
    known: int = 0
    failed: int = 0
    saved: int = 0
    truncated: bool = False


def scan_offers(
    fetch_page: Callable[[int], list[JobOffer]],
    enrich_offer: Callable[[JobOffer], JobOffer | None],
    *,
    search: JobSearch,
    hooks: ScanHooks,
    pacing: Pacing,
    label: str,
    accept: Callable[[JobOffer], bool] | None = None,
) -> ScanReport:
    """Collect new offers from paginated listing pages, then enrich and hand over each one.

    Offers already stored (``hooks.duplicate_checker``) or rejected by ``accept`` are
    skipped during pagination, so an optional ``search.limit`` counts only new offers.
    ``enrich_offer`` returns None when the detail page cannot be used; such offers are
    reported as failed.
    """
    report = ScanReport()
    candidates = _collect_new_offers(fetch_page, search.limit, hooks, pacing, label, report, accept)

    for index, offer in enumerate(candidates, start=1):
        hooks.wait(pacing.detail_delay_seconds)
        enriched_offer = enrich_offer(offer)
        hooks.raise_if_stopped()
        hooks.report_progress(ScanProgress(stage="enrich", current=index, total=len(candidates)))
        if enriched_offer is None:
            report.failed += 1
        elif hooks.enriched_offer_handler is None or hooks.enriched_offer_handler(enriched_offer):
            report.saved += 1
        else:
            report.known += 1

    logger.info("Finished %s scan: %s", label, report)
    return report


def _collect_new_offers(
    fetch_page: Callable[[int], list[JobOffer]],
    limit: int | None,
    hooks: ScanHooks,
    pacing: Pacing,
    label: str,
    report: ScanReport,
    accept: Callable[[JobOffer], bool] | None,
) -> list[JobOffer]:
    candidates: list[JobOffer] = []
    seen_keys: set[tuple[str, str]] = set()

    for page in range(1, pacing.max_pages + 1):
        hooks.raise_if_stopped()
        if page > 1:
            hooks.wait(pacing.page_delay_seconds)
        page_offers = fetch_page(page)
        hooks.raise_if_stopped()
        report.pages = page
        hooks.report_progress(ScanProgress(stage="listing", current=page, total=None))

        unseen = 0
        for offer in page_offers:
            key = offer_listing_key(offer)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unseen += 1
            report.listed += 1
            if accept is not None and not accept(offer):
                report.filtered_out += 1
            elif hooks.duplicate_checker is not None and hooks.duplicate_checker(offer):
                report.known += 1
            else:
                candidates.append(offer)
                if limit is not None and len(candidates) >= limit:
                    logger.info("Reached the limit of %s new %s offers", limit, label)
                    return candidates

        logger.info(
            "Parsed %s %s offers from listing page %s, %s unseen in this scan",
            len(page_offers),
            label,
            page,
            unseen,
        )
        # Boards answer an out-of-range page with the last one, so a page without
        # unseen offers marks the end of the listing.
        if not unseen:
            return candidates

    report.truncated = True
    logger.warning("Stopped %s pagination after max_pages=%s", label, pacing.max_pages)
    return candidates


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


class JobSource(Protocol):
    """Contract implemented by all job source adapters."""

    source_name: str

    def search(self, search: JobSearch) -> ScanReport:
        """Scan the board for offers matching the search, handing each one to the hooks."""
        ...
