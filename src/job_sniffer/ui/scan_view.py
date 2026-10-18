import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import flet as ft
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.config import AppConfig
from job_sniffer.database import (
    ProfileRecord,
    ProfileSourceConfig,
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
    JobSource,
    ProgressHandler,
    ScanHooks,
    ScanProgress,
    ScanReport,
)
from job_sniffer.sources.registry import ACTIVE_SOURCES, get_source_definition
from job_sniffer.ui.offers_view import OffersView
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)

ALL_SOURCES_KEY = "__all__"
_MAX_LOG_LINES = 500


class ScanInterruptedError(RuntimeError):
    """Report a failed scan together with offers already saved during it."""

    def __init__(self, cause: Exception, offers: list[tuple[JobOffer, int]]) -> None:
        super().__init__(str(cause))
        self.offers = offers


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
    ) -> tuple[list[tuple[JobOffer, int]], ScanReport]:
        logger.info("Fetching offers: source=%s", source_key)
        source_name = _source_name(source_key)
        saved_results: list[tuple[JobOffer, int]] = []
        with db_session() as session:

            def save_enriched_offer(offer: JobOffer) -> bool:
                inserted_id = save_offer(session, offer)
                if inserted_id is None:
                    logger.info(
                        "Skipped duplicate after enrich: %s - %s", offer.title, offer.company
                    )
                    return False
                logger.info(
                    "Saved enriched offer immediately: %s - %s (id=%s)",
                    offer.title,
                    offer.company,
                    inserted_id,
                )
                saved_results.append((offer, inserted_id))
                self._append_log_line(loop, f"{offer.title} — {offer.company} — {source_name}")
                return True

            hooks = ScanHooks(
                stop_event=self._stop_event,
                duplicate_checker=lambda offer: offer_exists(session, offer),
                enriched_offer_handler=save_enriched_offer,
                progress_handler=self._make_progress_handler(loop),
            )
            try:
                hooks.raise_if_stopped()
                report = _create_source(source_key, hooks).search(search)
            except Exception as error:
                if not saved_results:
                    raise
                raise ScanInterruptedError(error, saved_results) from error

        logger.info("Saved %s enriched offers from %s", len(saved_results), source_key)
        return saved_results, report

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
            if not source_enabled(plan.settings, source_value):
                self._status_bar.set_scan(
                    f"Źródło {_source_name(source_value)} jest wyłączone w profilu "
                    f"„{plan.profile.name}”."
                )
                return None
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
                results, report = await loop.run_in_executor(
                    self._scan_executor, self._scan_source, source_key, search, loop
                )
            except ScanInterruptedError as error:
                logger.exception("Scan stopped after partial success: source=%s", source_key)
                all_results.extend(error.offers)
                summaries.append(
                    f"{_source_name(source_key)}: przerwane, zapisano {len(error.offers)}"
                )
            except Exception as error:
                # One failing source must not abort the remaining ones.
                logger.exception("Scan failed: source=%s", source_key)
                errors.append(f"{_source_name(source_key)}: {error}")
            else:
                all_results.extend(results)
                summaries.append(_format_source_summary(_source_name(source_key), report))

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

        completed = False
        try:
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
            completed = True
        finally:
            # Re-enable scanning even after an unexpected error so the user is never blocked.
            self._scan_button.disabled = False
            self._progress_bar.value = 1.0 if completed else 0.0
            self._page.update()

        if can_eval:
            newly_inserted_ids = [inserted_id for _, inserted_id in reversed(all_results)]
            logger.info("Enqueuing %s offers for background AI evaluation", len(newly_inserted_ids))
            await self._offers_view.enqueue_evaluations(newly_inserted_ids)


def _create_source(source_key: str, hooks: ScanHooks) -> JobSource:
    create = get_source_definition(source_key).create
    if create is None:
        raise ValueError(f"Source {source_key} cannot be scanned")
    return create(hooks)


def _format_source_summary(source_name: str, report: ScanReport) -> str:
    details = [
        f"{label}: {count}"
        for label, count in (("znane", report.known), ("nieudane", report.failed))
        if count
    ]
    if report.truncated:
        details.append(f"niepełny — limit {report.pages} stron")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{source_name}: zapisano {report.saved}{suffix}"


def _source_name(source_key: str) -> str:
    return get_source_definition(source_key).name
