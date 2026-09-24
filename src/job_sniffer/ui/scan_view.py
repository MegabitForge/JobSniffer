import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import flet as ft
import httpx
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.config import AppConfig
from job_sniffer.database import (
    ProfileRecord,
    ProfileSourceConfig,
    SaveStats,
    db_session,
    get_profile,
    get_profile_source_settings,
    list_profiles,
    offer_exists,
    save_offer,
    source_enabled,
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
from job_sniffer.sources.nofluffjobs import NoFluffJobsSource
from job_sniffer.sources.olx import OlxJobSource
from job_sniffer.sources.pracuj import PracujBlockedError, PracujJobSource
from job_sniffer.sources.registry import ACTIVE_SOURCES, SOURCE_DEFINITIONS
from job_sniffer.sources.theprotocol import TheProtocolJobSource
from job_sniffer.ui.offers_view import OffersView
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class ScanPlan:
    """Resolved inputs for a scan run."""

    profile: ProfileRecord
    settings: dict[str, ProfileSourceConfig]


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
            with db_session() as session:
                self._profiles = list_profiles(session)
        except SQLAlchemyError as error:
            logger.exception("Could not load profiles")
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return

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
        with db_session() as session:
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

    @staticmethod
    def _include_pre_enrich_duplicates(stats: SaveStats, duplicates: int) -> SaveStats:
        if duplicates == 0:
            return stats
        return SaveStats(
            seen=stats.seen + duplicates,
            inserted=stats.inserted,
            duplicates=stats.duplicates + duplicates,
        )

    def _load_profile_for_scan(self, profile_value: str) -> ScanPlan | None:
        """Load the selected profile with its source settings; None reports the problem."""
        try:
            profile_id = int(profile_value)
        except ValueError:
            self._status_bar.set_scan("Wybierz profil skanowania.")
            return None

        profile: ProfileRecord | None
        settings: dict[str, ProfileSourceConfig]
        try:
            with db_session() as session:
                profile = get_profile(session, profile_id)
                settings = (
                    get_profile_source_settings(session, profile_id) if profile is not None else {}
                )
        except SQLAlchemyError as error:
            logger.exception("Could not load profile %s", profile_id)
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return None

        if profile is None:
            self._status_bar.set_scan("Profil nie istnieje.")
            self.reload_profiles()
            return None
        return ScanPlan(profile=profile, settings=settings)

    def _select_source_keys(self, plan: ScanPlan) -> list[str] | None:
        """Resolve which sources to scan; None means the scan cannot start."""
        source_value = self._source_dropdown.value or ALL_SOURCES_KEY
        if source_value != ALL_SOURCES_KEY:
            return [source_value]
        source_keys = [
            source.key for source in ACTIVE_SOURCES if source_enabled(plan.settings, source.key)
        ]
        if not source_keys:
            self._status_bar.set_scan("Profil nie ma włączonych źródeł.")
            return None
        return source_keys

    async def _run_source_scans(
        self, plan: ScanPlan, source_keys: list[str], loop: asyncio.AbstractEventLoop
    ) -> tuple[list[tuple[JobOffer, int]], list[str], list[str]]:
        """Scan every selected source sequentially, collecting offers and result lines."""
        all_results: list[tuple[JobOffer, int]] = []
        summaries: list[str] = []
        errors: list[str] = []

        for source_key in source_keys:
            if self._stop_event.is_set():
                break
            stored = plan.settings.get(source_key)
            search = JobSearch(
                filters=stored.filters if stored else {},
                limit=plan.profile.offer_limit,
            )
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

        return all_results, summaries, errors

    async def _on_scan_click(self, _: Any) -> None:
        logger.info("Scan initiated")
        profile_value = self._profile_dropdown.value
        if not profile_value:
            self._status_bar.set_scan("Wybierz profil skanowania.")
            return
        plan = self._load_profile_for_scan(profile_value)
        if plan is None:
            return
        source_keys = self._select_source_keys(plan)
        if source_keys is None:
            return

        loop = asyncio.get_running_loop()
        self._scan_button.disabled = True
        self._stop_event.clear()
        self._log_column.controls.clear()
        self._progress_bar.value = None
        self._page.update()

        all_results, summaries, errors = await self._run_source_scans(plan, source_keys, loop)

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
