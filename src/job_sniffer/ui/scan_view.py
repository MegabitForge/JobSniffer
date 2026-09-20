import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import flet as ft
import httpx
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.config import AppConfig
from job_sniffer.database import SaveStats, connect, init_db, offer_exists, save_offer
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.services import EvaluationService
from job_sniffer.sources.base import DuplicateChecker, EnrichedOfferHandler, JobSource
from job_sniffer.sources.browser import BrowserFetchError
from job_sniffer.sources.bulldogjob import BulldogjobSource
from job_sniffer.sources.nofluffjobs import NoFluffJobsSource
from job_sniffer.sources.olx import OlxJobSource
from job_sniffer.sources.pracuj import PracujBlockedError, PracujJobSource
from job_sniffer.sources.registry import SOURCE_DEFINITIONS
from job_sniffer.sources.theprotocol import TheProtocolJobSource
from job_sniffer.ui.offers_view import DB_PATH, OffersView
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)

ACTIVE_SOURCES = tuple(source for source in SOURCE_DEFINITIONS if source.status == "active")
KEYWORD_UNSUPPORTED_SOURCES = ("bulldogjob", "nofluffjobs")


class ScanInterruptedError(RuntimeError):
    """Report a failed scan together with offers already saved during it."""

    def __init__(self, cause: Exception, offers: list[JobOffer], stats: SaveStats) -> None:
        super().__init__(str(cause))
        self.offers = offers
        self.stats = stats


class ScanView:
    """Search form with non-blocking scanning logic."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        evaluation_service: EvaluationService,
        stop_event: threading.Event,
        scan_executor: ThreadPoolExecutor,
        offers_view: OffersView,
        status_bar: StatusBar,
        on_scan_finished: Callable[[], None],
    ) -> None:
        self._page = page
        self._config = config
        self._evaluation_service = evaluation_service
        self._stop_event = stop_event
        self._scan_executor = scan_executor
        self._offers_view = offers_view
        self._status_bar = status_bar
        self._on_scan_finished = on_scan_finished

        self._keywords = ft.TextField(label="Słowa kluczowe (Keywords)", value="")
        self._location = ft.TextField(
            label="Lokalizacja (Location)", hint_text="Opcjonalnie", value=""
        )
        self._limit = ft.TextField(
            label="Limit ofert",
            hint_text="Puste = brak limitu",
            value="5",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        self._source = ft.Dropdown(
            label="Źródło",
            value=ACTIVE_SOURCES[0].key if ACTIVE_SOURCES else None,
            options=[ft.dropdown.Option(key=item.key, text=item.name) for item in ACTIVE_SOURCES],
        )
        keywords_disabled = self._source.value in KEYWORD_UNSUPPORTED_SOURCES
        self._keywords.disabled = keywords_disabled
        if keywords_disabled:
            self._keywords.hint_text = (
                "Wybrane źródło nie obsługuje wyszukiwania po słowach kluczowych"
            )

        self._source.on_select = self._on_source_change

        self._scan_button = ft.ElevatedButton("Skanuj oferty", icon=ft.Icons.SEARCH)
        self._scan_button.on_click = self._on_scan_click

        self.control = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row([self._keywords, self._location], spacing=12),
                            ft.Row([self._limit, self._source], spacing=12),
                            ft.Row([self._scan_button], spacing=12, wrap=True),
                        ],
                        spacing=10,
                    ),
                    border_radius=10,
                    padding=12,
                ),
            ],
            spacing=12,
            expand=True,
        )

    def _on_source_change(self, _: Any) -> None:
        keywords_disabled = self._source.value in KEYWORD_UNSUPPORTED_SOURCES
        self._keywords.disabled = keywords_disabled
        self._keywords.hint_text = (
            "Wybrane źródło nie obsługuje wyszukiwania po słowach kluczowych"
            if keywords_disabled
            else None
        )
        self._page.update()

    def _scan_source(
        self, source_key: str, search: JobSearch
    ) -> tuple[list[tuple[JobOffer, int]], SaveStats]:
        logger.info("Fetching offers: source=%s", source_key)
        if self._stop_event.is_set():
            raise RuntimeError("Scan cancelled because the application is closing.")

        session = connect(DB_PATH)
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

            saved_results: list[tuple[JobOffer, int]] = []
            save_seen = 0
            save_duplicates = 0

            def save_enriched_offer(offer: JobOffer) -> bool:
                nonlocal save_seen, save_duplicates
                if self._stop_event.is_set():
                    raise RuntimeError("Scan cancelled because the application is closing.")
                save_seen += 1
                inserted_id = save_offer(session, offer)
                if inserted_id is not None:
                    logger.info(
                        "Saved enriched offer immediately: %s - %s (id=%s)",
                        offer.title,
                        offer.company,
                        inserted_id,
                    )
                    saved_results.append((offer, inserted_id))
                    return True

                save_duplicates += 1
                logger.info(
                    "Skipped duplicate after enrich: %s - %s",
                    offer.title,
                    offer.company,
                )
                return False

            try:
                _create_source(
                    source_key,
                    stop_event=self._stop_event,
                    duplicate_checker=duplicate_checker,
                    enriched_offer_handler=save_enriched_offer,
                ).search(search)
            except Exception as error:
                if not saved_results:
                    raise
                stats = self._include_pre_enrich_duplicates(
                    SaveStats(
                        seen=save_seen,
                        inserted=len(saved_results),
                        duplicates=save_duplicates,
                    ),
                    pre_enrich_duplicates,
                )
                raise ScanInterruptedError(
                    error, [item[0] for item in saved_results], stats
                ) from error

            if self._stop_event.is_set():
                raise RuntimeError("Scan cancelled because the application is closing.")

            logger.info("Saved %s enriched offers from %s", len(saved_results), source_key)
            stats = SaveStats(
                seen=save_seen,
                inserted=len(saved_results),
                duplicates=save_duplicates,
            )
            return saved_results, self._include_pre_enrich_duplicates(stats, pre_enrich_duplicates)
        finally:
            logger.info("Closing SQLite session")
            session.close()

    @staticmethod
    def _include_pre_enrich_duplicates(stats: SaveStats, duplicates: int) -> SaveStats:
        if duplicates == 0:
            return stats
        return SaveStats(
            seen=stats.seen + duplicates,
            inserted=stats.inserted,
            duplicates=stats.duplicates + duplicates,
        )

    async def _on_scan_click(self, _: Any) -> None:
        logger.info("Scan initiated")
        keywords_value = (self._keywords.value or "").strip()
        location_value = (self._location.value or "").strip()
        source_key = (self._source.value or "").strip()
        if source_key in KEYWORD_UNSUPPORTED_SOURCES:
            keywords_value = ""

        limit_text = (self._limit.value or "").strip()
        limit_value: int | None = None
        if limit_text:
            try:
                limit_value = int(limit_text)
            except ValueError:
                limit_value = 0

        if not source_key or (limit_value is not None and limit_value < 1):
            self._status_bar.set_scan("Wybierz źródło i podaj poprawny limit (liczba dodatnia).")
            return

        self._scan_button.disabled = True
        self._stop_event.clear()
        self._status_bar.set_scan(f"Skanowanie {_source_name(source_key)}...")

        search = JobSearch(keywords=keywords_value, location=location_value, limit=limit_value)
        newly_inserted_ids: list[int] = []

        try:
            loop = asyncio.get_running_loop()
            results, stats = await loop.run_in_executor(
                self._scan_executor,
                self._scan_source,
                source_key,
                search,
            )
        except ScanInterruptedError as error:
            logger.exception("Scan stopped after partial success: source=%s", source_key)
            self._status_bar.set_scan(
                f"Skanowanie {_source_name(source_key)} przerwane po zapisaniu "
                f"{error.stats.inserted} ofert. Błąd: {error}"
            )
            self._offers_view.reload_offers()
            self._page.update()
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
            self._status_bar.set_scan(f"Błąd skanowania {_source_name(source_key)}: {error}")
        else:
            self._status_bar.set_scan(
                f"Pobrano {stats.seen} ofert z {_source_name(source_key)}. "
                f"Zapisano {stats.inserted}, pominięto duplikaty: {stats.duplicates}."
            )
            can_eval = self._config.auto_evaluate and bool(self._evaluation_service.get_cv_text())
            self._offers_view.add_new_offers(results, pending_evaluation=can_eval)
            newly_inserted_ids = [inserted_id for _, inserted_id in reversed(results)]
            if self._on_scan_finished is not None:
                self._on_scan_finished()
        finally:
            # Re-enable scanning immediately so user is never blocked
            self._scan_button.disabled = False
            self._page.update()

        # Trigger AI evaluation on the newly saved offers at the very end in the background
        if (
            self._config.auto_evaluate
            and newly_inserted_ids
            and self._evaluation_service.get_cv_text()
        ):
            logger.info("Enqueuing %s offers for background AI evaluation", len(newly_inserted_ids))
            await self._offers_view.enqueue_evaluations(newly_inserted_ids)


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
