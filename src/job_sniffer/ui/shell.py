from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import flet as ft
import httpx
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.database import SaveStats, connect, init_db, offer_exists, save_offer
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources.base import DuplicateChecker, EnrichedOfferHandler, JobSource
from job_sniffer.sources.browser import BrowserFetchError
from job_sniffer.sources.bulldogjob import BulldogjobSource
from job_sniffer.sources.nofluffjobs import NoFluffJobsSource
from job_sniffer.sources.olx import OlxJobSource
from job_sniffer.sources.pracuj import PracujBlockedError, PracujJobSource
from job_sniffer.sources.registry import SOURCE_DEFINITIONS
from job_sniffer.sources.theprotocol import TheProtocolJobSource

logger = logging.getLogger(__name__)

ACTIVE_SOURCES = tuple(source for source in SOURCE_DEFINITIONS if source.status == "active")


class ScanInterruptedError(RuntimeError):
    """Report a failed scan together with offers already saved during it."""

    def __init__(self, cause: Exception, offers: list[JobOffer], stats: SaveStats) -> None:
        super().__init__(str(cause))
        self.offers = offers
        self.stats = stats


def build_shell(page: ft.Page) -> ft.Control:
    """Build the initial application view."""
    stop_event = threading.Event()
    scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job-sniffer-scan")

    keywords = ft.TextField(
        label="Keywords",
        value="",
    )
    location = ft.TextField(
        label="Location",
        hint_text="Optional",
        value="",
    )
    limit = ft.TextField(
        label="Limit",
        hint_text="Optional; empty means all offers from the source",
        value="5",
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    source = ft.Dropdown(
        label="Source",
        value=ACTIVE_SOURCES[0].key if ACTIVE_SOURCES else None,
        options=[ft.dropdown.Option(key=item.key, text=item.name) for item in ACTIVE_SOURCES],
    )
    keywords.disabled = source.value == "bulldogjob"
    if keywords.disabled:
        keywords.hint_text = "Not supported by Bulldogjob"
    status = ft.Text(
        "Ready to scan.",
    )
    results = ft.Column(spacing=8)
    source_notes = ft.Column(
        controls=[
            ft.Text("Sources", size=18, weight=ft.FontWeight.BOLD),
            *[
                ft.Text(
                    f"{source.name} [{source.status}]",
                )
                for source in SOURCE_DEFINITIONS
            ],
        ],
        spacing=6,
    )
    scan_button = ft.ElevatedButton(
        "Scan",
    )

    def on_source_change(_: Any) -> None:
        bulldogjob_selected = source.value == "bulldogjob"
        keywords.disabled = bulldogjob_selected
        keywords.hint_text = "Not supported by Bulldogjob" if bulldogjob_selected else None
        page.update()

    async def on_scan_click(_: Any) -> None:
        logger.info("Scan initiated")
        keywords_value = (keywords.value or "").strip()
        location_value = (location.value or "").strip()
        source_key = (source.value or "").strip()
        if source_key == "bulldogjob":
            keywords_value = ""

        limit_text = (limit.value or "").strip()
        limit_value: int | None = None
        if limit_text:
            try:
                limit_value = int(limit_text)
            except ValueError:
                limit_value = 0

        if not source_key or limit_value is not None and limit_value < 1:
            logger.warning(
                "Invalid scan form values: source=%r keywords=%r location=%r limit=%r",
                source.value,
                keywords.value,
                location.value,
                limit.value,
            )
            status.value = "Choose a source and optionally provide a positive limit."
            page.update()
            return

        scan_button.disabled = True
        stop_event.clear()
        status.value = f"Scanning {_source_name(source_key)}..."
        results.controls.clear()
        page.update()

        search = JobSearch(keywords=keywords_value, location=location_value, limit=limit_value)
        logger.info("Starting scan in background: source=%s", source_key)
        try:
            loop = asyncio.get_running_loop()
            inserted_offers, stats = await loop.run_in_executor(
                scan_executor,
                _scan_source,
                source_key,
                search,
            )
        except ScanInterruptedError as error:
            logger.exception("Scan stopped after partial success: source=%s", source_key)
            _show_offer_list(error.offers)
            status.value = (
                f"{_source_name(source_key)} scan stopped after saving "
                f"{error.stats.inserted} offers. Reason: {error}"
            )
        except (
            BrowserFetchError,
            httpx.HTTPError,
            PracujBlockedError,
            SQLAlchemyError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as error:
            logger.exception("Scan failed: source=%s", source_key)
            status.value = f"{_source_name(source_key)} scan failed: {error}"
        else:
            logger.info(
                "Scan finished: source=%s seen=%s inserted=%s duplicates=%s",
                source_key,
                stats.seen,
                stats.inserted,
                stats.duplicates,
            )
            _show_results(inserted_offers, stats, source_key=source_key, action="Fetched")
        finally:
            scan_button.disabled = False
            page.update()

    def _scan_source(source_key: str, search: JobSearch) -> tuple[list[JobOffer], SaveStats]:
        logger.info("Fetching offers: source=%s", source_key)
        if stop_event.is_set():
            raise RuntimeError("Scan cancelled because the application is closing.")

        session = connect(Path("job_sniffer.sqlite"))
        try:
            logger.info("Initializing SQLite schema")
            init_db(session)

            pre_enrich_duplicates = 0

            def duplicate_checker(offer: JobOffer) -> bool:
                nonlocal pre_enrich_duplicates
                exists = offer_exists(session, offer)
                if exists:
                    pre_enrich_duplicates += 1
                return exists

            inserted_offers: list[JobOffer] = []
            save_seen = 0
            save_duplicates = 0

            def save_enriched_offer(offer: JobOffer) -> bool:
                nonlocal save_seen, save_duplicates
                if stop_event.is_set():
                    raise RuntimeError("Scan cancelled because the application is closing.")
                save_seen += 1
                inserted = save_offer(session, offer)
                if inserted:
                    inserted_offers.append(offer)
                    logger.info(
                        "Saved enriched offer immediately: %s - %s", offer.title, offer.company
                    )
                else:
                    save_duplicates += 1
                    logger.info(
                        "Skipped duplicate after enrich: %s - %s",
                        offer.title,
                        offer.company,
                    )
                return inserted

            try:
                _create_source(
                    source_key,
                    stop_event=stop_event,
                    duplicate_checker=duplicate_checker,
                    enriched_offer_handler=save_enriched_offer,
                ).search(search)
            except Exception as error:
                if not inserted_offers:
                    raise
                stats = _include_pre_enrich_duplicates(
                    SaveStats(
                        seen=save_seen,
                        inserted=len(inserted_offers),
                        duplicates=save_duplicates,
                    ),
                    pre_enrich_duplicates,
                )
                raise ScanInterruptedError(error, inserted_offers.copy(), stats) from error
            if stop_event.is_set():
                raise RuntimeError("Scan cancelled because the application is closing.")
            logger.info("Saved %s enriched offers from %s", len(inserted_offers), source_key)
            stats = SaveStats(
                seen=save_seen,
                inserted=len(inserted_offers),
                duplicates=save_duplicates,
            )
            return inserted_offers, _include_pre_enrich_duplicates(
                stats,
                pre_enrich_duplicates,
            )
        finally:
            logger.info("Closing SQLite session")
            session.close()

    def _include_pre_enrich_duplicates(
        stats: SaveStats,
        duplicates: int,
    ) -> SaveStats:
        if duplicates == 0:
            return stats
        return SaveStats(
            seen=stats.seen + duplicates,
            inserted=stats.inserted,
            duplicates=stats.duplicates + duplicates,
        )

    def _show_results(
        offers: list[JobOffer],
        stats: SaveStats,
        *,
        source_key: str,
        action: str,
    ) -> None:
        status.value = (
            f"{action} {stats.seen} offers from {_source_name(source_key)}. "
            f"Inserted {stats.inserted}, skipped duplicates {stats.duplicates}."
        )
        _show_offer_list(offers)

    def _show_offer_list(offers: list[JobOffer]) -> None:
        results.controls[:] = [
            ft.Text(
                f"{offer.title} - {offer.company}",
            )
            for offer in offers[:10]
        ]
        if not results.controls:
            results.controls.append(ft.Text("No new offers found."))

    scan_button.on_click = on_scan_click
    source.on_select = on_source_change

    def on_page_close(_: Any) -> None:
        logger.info("Application is closing, requesting scan shutdown")
        stop_event.set()
        scan_executor.shutdown(wait=False, cancel_futures=True)

    page.on_close = on_page_close
    page.on_disconnect = on_page_close

    return ft.Column(
        controls=[
            ft.Text("Job Sniffer", size=32, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=ft.Column(
                    controls=[
                        keywords,
                        location,
                        limit,
                        source,
                        scan_button,
                        status,
                        results,
                    ],
                    spacing=12,
                ),
                border_radius=12,
                padding=16,
            ),
            source_notes,
        ],
        spacing=12,
        expand=True,
    )


def _create_source(
    source_key: str,
    *,
    stop_event: threading.Event,
    duplicate_checker: DuplicateChecker | None = None,
    enriched_offer_handler: EnrichedOfferHandler | None = None,
) -> JobSource:
    if source_key == "pracuj":
        return PracujJobSource(
            stop_event=stop_event,
            duplicate_checker=duplicate_checker,
            enriched_offer_handler=enriched_offer_handler,
        )
    if source_key == "olx":
        return OlxJobSource(
            stop_event=stop_event,
            duplicate_checker=duplicate_checker,
            enriched_offer_handler=enriched_offer_handler,
        )
    if source_key == "theprotocol":
        return TheProtocolJobSource(
            stop_event=stop_event,
            duplicate_checker=duplicate_checker,
            enriched_offer_handler=enriched_offer_handler,
        )
    if source_key == "nofluffjobs":
        return NoFluffJobsSource(
            stop_event=stop_event,
            duplicate_checker=duplicate_checker,
            enriched_offer_handler=enriched_offer_handler,
        )
    if source_key == "bulldogjob":
        return BulldogjobSource(
            stop_event=stop_event,
            duplicate_checker=duplicate_checker,
            enriched_offer_handler=enriched_offer_handler,
        )
    raise ValueError(f"Unsupported source: {source_key}")


def _source_name(source_key: str) -> str:
    for item in SOURCE_DEFINITIONS:
        if item.key == source_key:
            return item.name
    return source_key
