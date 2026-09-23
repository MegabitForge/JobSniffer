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
from job_sniffer.database import (
    DB_PATH,
    ProfileRecord,
    ProfileSourceConfig,
    SaveStats,
    connect,
    get_profile,
    get_profile_source_settings,
    init_db,
    list_profiles,
    offer_exists,
    save_offer,
)
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.services import EvaluationService
from job_sniffer.sources.base import (
    DuplicateChecker,
    EnrichedOfferHandler,
    JobSource,
    ProgressHandler,
    ScanProgress,
)
from job_sniffer.sources.browser import BrowserFetchError
from job_sniffer.sources.bulldogjob import BulldogjobSource
from job_sniffer.sources.filters import get_source_filter_schema
from job_sniffer.sources.nofluffjobs import NoFluffJobsSource
from job_sniffer.sources.olx import OlxJobSource
from job_sniffer.sources.pracuj import PracujBlockedError, PracujJobSource
from job_sniffer.sources.registry import SOURCE_DEFINITIONS
from job_sniffer.sources.theprotocol import TheProtocolJobSource
from job_sniffer.ui.offers_view import OffersView
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)

ACTIVE_SOURCES = tuple(source for source in SOURCE_DEFINITIONS if source.status == "active")
ALL_SOURCES_KEY = "__all__"
_MAX_LOG_LINES = 500


class ScanInterruptedError(RuntimeError):
    """Report a failed scan together with offers already saved during it."""

    def __init__(
        self, cause: Exception, offers: list[tuple[JobOffer, int]], stats: SaveStats
    ) -> None:
        super().__init__(str(cause))
        self.offers = offers
        self.stats = stats


class ScanView:
    """Profile-driven scan form with progress and a minimal offer log."""

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

        self._profiles: list[ProfileRecord] = []

        self._profile_dropdown = ft.Dropdown(label="Profil", options=[])
        self._source_dropdown = ft.Dropdown(
            label="Źródło",
            value=ALL_SOURCES_KEY,
            options=[
                ft.dropdown.Option(key=ALL_SOURCES_KEY, text="Wszystkie (włączone w profilu)"),
                *[ft.dropdown.Option(key=item.key, text=item.name) for item in ACTIVE_SOURCES],
            ],
        )

        self._scan_button = ft.ElevatedButton("Skanuj oferty", icon=ft.Icons.SEARCH)
        self._scan_button.on_click = self._on_scan_click

        self._progress_bar = ft.ProgressBar(value=0.0)
        self._hint_text = ft.Text(
            "Brak profili — dodaj profil w zakładce „Profiles”.",
            color=ft.Colors.ORANGE_800,
            visible=False,
        )
        self._log_column = ft.Column(spacing=2, scroll=ft.ScrollMode.ADAPTIVE, expand=True)

        self.reload_profiles(update=False)

        self.control = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row([self._profile_dropdown, self._source_dropdown], spacing=12),
                            ft.Row([self._scan_button], spacing=12, wrap=True),
                            self._progress_bar,
                            self._hint_text,
                            self._log_column,
                        ],
                        spacing=10,
                        expand=True,
                    ),
                    border_radius=10,
                    padding=12,
                ),
            ],
            spacing=12,
            expand=True,
        )

    def reload_profiles(self, *, update: bool = True) -> None:
        """Refresh the profile dropdown after profiles change."""
        try:
            session = connect(DB_PATH)
        except SQLAlchemyError as error:
            logger.exception("Could not open database while loading profiles")
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return
        try:
            init_db(session)
            self._profiles = list_profiles(session)
        except SQLAlchemyError as error:
            logger.exception("Could not load profiles")
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return
        finally:
            session.close()

        previous = self._profile_dropdown.value
        options = [
            ft.dropdown.Option(key=str(profile.id), text=profile.name) for profile in self._profiles
        ]
        self._profile_dropdown.options = options
        if previous and any(option.key == previous for option in options):
            self._profile_dropdown.value = previous
        elif self._profiles:
            self._profile_dropdown.value = str(self._profiles[0].id)
        else:
            self._profile_dropdown.value = None

        has_profiles = bool(self._profiles)
        self._scan_button.disabled = not has_profiles
        self._hint_text.visible = not has_profiles
        if update:
            self._page.update()

    def _make_progress_handler(self, loop: asyncio.AbstractEventLoop) -> ProgressHandler:
        def handle_progress(progress: ScanProgress) -> None:
            def apply() -> None:
                if progress.stage == "listing":
                    self._progress_bar.value = None
                else:
                    total = progress.total or 0
                    self._progress_bar.value = progress.current / total if total else 0.0
                self._page.update()

            loop.call_soon_threadsafe(apply)

        return handle_progress

    def _append_log_line(self, loop: asyncio.AbstractEventLoop, text: str) -> None:
        def apply() -> None:
            self._log_column.controls.append(ft.Text(text, size=12))
            while len(self._log_column.controls) > _MAX_LOG_LINES:
                self._log_column.controls.pop(0)
            self._page.update()

        loop.call_soon_threadsafe(apply)

    def _scan_source(
        self, source_key: str, search: JobSearch, loop: asyncio.AbstractEventLoop
    ) -> tuple[list[tuple[JobOffer, int]], SaveStats]:
        logger.info("Fetching offers: source=%s", source_key)
        if self._stop_event.is_set():
            raise RuntimeError("Scan cancelled because the application is closing.")

        source_name = _source_name(source_key)
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
                    self._append_log_line(loop, f"{offer.title} — {offer.company} — {source_name}")
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
                    progress_handler=self._make_progress_handler(loop),
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
                raise ScanInterruptedError(error, saved_results, stats) from error

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

    @staticmethod
    def _build_search(
        source_key: str,
        settings: dict[str, ProfileSourceConfig],
        offer_limit: int | None,
    ) -> JobSearch:
        stored = settings.get(source_key)
        filters = stored.filters if stored else {}
        schema_keys = {source_filter.key for source_filter in get_source_filter_schema(source_key)}

        keywords = ""
        location = ""
        source_filters: dict[str, str | list[str]] = {}
        for key, value in filters.items():
            if key == "keywords":
                keywords = value if isinstance(value, str) else ", ".join(value)
            elif key == "location":
                location = value if isinstance(value, str) else ", ".join(value)
            elif key in schema_keys:
                source_filters[key] = value
        return JobSearch(
            keywords=keywords,
            location=location,
            limit=offer_limit,
            source_filters=source_filters or None,
        )

    async def _on_scan_click(self, _: Any) -> None:
        logger.info("Scan initiated")
        profile_value = self._profile_dropdown.value
        if not profile_value:
            self._status_bar.set_scan("Wybierz profil skanowania.")
            return
        try:
            profile_id = int(profile_value)
        except ValueError:
            self._status_bar.set_scan("Wybierz profil skanowania.")
            return

        try:
            session = connect(DB_PATH)
        except SQLAlchemyError as error:
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return
        try:
            init_db(session)
            profile = get_profile(session, profile_id)
            settings = (
                get_profile_source_settings(session, profile_id) if profile is not None else {}
            )
        except SQLAlchemyError as error:
            logger.exception("Could not load profile %s", profile_id)
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return
        finally:
            session.close()

        if profile is None:
            self._status_bar.set_scan("Profil nie istnieje.")
            self.reload_profiles()
            return

        source_value = self._source_dropdown.value or ALL_SOURCES_KEY
        if source_value == ALL_SOURCES_KEY:
            source_keys = [
                source.key for source in ACTIVE_SOURCES if _source_enabled(settings, source.key)
            ]
            if not source_keys:
                self._status_bar.set_scan("Profil nie ma włączonych źródeł.")
                return
        else:
            source_keys = [source_value]

        loop = asyncio.get_running_loop()
        self._scan_button.disabled = True
        self._stop_event.clear()
        self._log_column.controls.clear()
        self._progress_bar.value = None
        self._page.update()

        all_results: list[tuple[JobOffer, int]] = []
        summaries: list[str] = []
        errors: list[str] = []

        for source_key in source_keys:
            if self._stop_event.is_set():
                break
            search = self._build_search(source_key, settings, profile.offer_limit)
            self._status_bar.set_scan(f"Skanowanie {_source_name(source_key)}...")
            try:
                results, stats = await loop.run_in_executor(
                    self._scan_executor, self._scan_source, source_key, search, loop
                )
            except ScanInterruptedError as error:
                logger.exception("Scan stopped after partial success: source=%s", source_key)
                all_results.extend(error.offers)
                summaries.append(
                    f"{_source_name(source_key)}: przerwane, zapisano {error.stats.inserted}"
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
                errors.append(f"{_source_name(source_key)}: {error}")
            else:
                all_results.extend(results)
                summaries.append(
                    f"{_source_name(source_key)}: zapisano {stats.inserted}/{stats.seen}"
                )

        can_eval = (
            bool(all_results)
            and self._config.auto_evaluate
            and bool(self._evaluation_service.get_cv_text())
        )
        if all_results:
            self._offers_view.add_new_offers(all_results, pending_evaluation=can_eval)
            if self._on_scan_finished is not None:
                self._on_scan_finished()

        message = "Skanowanie profilu zakończone."
        if summaries:
            message += " " + "; ".join(summaries) + "."
        if errors:
            message += " Błędy: " + "; ".join(errors) + "."
        self._status_bar.set_scan(message)

        self._scan_button.disabled = False
        self._progress_bar.value = 1.0
        self._page.update()

        if can_eval:
            newly_inserted_ids = [inserted_id for _, inserted_id in reversed(all_results)]
            logger.info("Enqueuing %s offers for background AI evaluation", len(newly_inserted_ids))
            await self._offers_view.enqueue_evaluations(newly_inserted_ids)


def _create_source(
    source_key: str,
    *,
    stop_event: threading.Event,
    duplicate_checker: DuplicateChecker | None = None,
    enriched_offer_handler: EnrichedOfferHandler | None = None,
    progress_handler: ProgressHandler | None = None,
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
            progress_handler=progress_handler,
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


def _source_enabled(settings: dict[str, ProfileSourceConfig], source_key: str) -> bool:
    stored = settings.get(source_key)
    return stored is None or stored.enabled
