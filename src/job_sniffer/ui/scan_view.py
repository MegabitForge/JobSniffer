import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import flet as ft
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.config import AppConfig
from job_sniffer.database import DB_PATH, ProfileRecord, db_session, list_profiles, source_enabled
from job_sniffer.models import JobOffer
from job_sniffer.scanning import (
    ScanCallbacks,
    ScanPlan,
    format_scan_message,
    load_scan_plan,
    run_scan,
)
from job_sniffer.services import EvaluationService
from job_sniffer.sources.base import ScanProgress
from job_sniffer.sources.registry import ACTIVE_SOURCES, SourceDefinition
from job_sniffer.ui.offers_view import OffersView
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)

ALL_SOURCES_KEY = "__all__"
_MAX_LOG_LINES = 500


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
        *,
        sources: Sequence[SourceDefinition] = ACTIVE_SOURCES,
        db_path: Path = DB_PATH,
    ) -> None:
        self._page = page
        self._config = config
        self._evaluation_service = evaluation_service
        self._stop_event = stop_event
        self._scan_executor = scan_executor
        self._offers_view = offers_view
        self._status_bar = status_bar
        self._on_scan_finished = on_scan_finished
        self._sources = tuple(sources)
        self._db_path = db_path

        self._profiles: list[ProfileRecord] = []

        self._profile_dropdown = ft.Dropdown(label="Profil", options=[])
        self._source_dropdown = ft.Dropdown(
            label="Źródło",
            value=ALL_SOURCES_KEY,
            options=[
                ft.dropdown.Option(key=ALL_SOURCES_KEY, text="Wszystkie (włączone w profilu)"),
                *[ft.dropdown.Option(key=item.key, text=item.name) for item in self._sources],
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
            with db_session(self._db_path) as session:
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

    def _make_callbacks(self, loop: asyncio.AbstractEventLoop) -> ScanCallbacks:
        """Callbacks run on the scan worker thread, so each one hands its UI work to the loop."""

        def on_source_started(source: SourceDefinition) -> None:
            loop.call_soon_threadsafe(self._status_bar.set_scan, f"Skanowanie {source.name}...")

        def on_offer_saved(source: SourceDefinition, offer: JobOffer) -> None:
            loop.call_soon_threadsafe(
                self._append_log_line, f"{offer.title} — {offer.company} — {source.name}"
            )

        def on_progress(progress: ScanProgress) -> None:
            loop.call_soon_threadsafe(self._show_progress, progress)

        return ScanCallbacks(
            on_source_started=on_source_started,
            on_offer_saved=on_offer_saved,
            on_progress=on_progress,
        )

    def _show_progress(self, progress: ScanProgress) -> None:
        if progress.stage == "listing" or not progress.total:
            self._progress_bar.value = None
        else:
            self._progress_bar.value = progress.current / progress.total
        self._page.update()

    def _append_log_line(self, text: str) -> None:
        self._log_column.controls.append(ft.Text(text, size=12))
        while len(self._log_column.controls) > _MAX_LOG_LINES:
            self._log_column.controls.pop(0)
        self._page.update()

    def _load_plan(self) -> ScanPlan | None:
        """Load the selected profile; None means the problem is already shown."""
        try:
            profile_id = int(self._profile_dropdown.value or "")
        except ValueError:
            self._status_bar.set_scan("Wybierz profil skanowania.")
            return None
        try:
            plan = load_scan_plan(profile_id, self._db_path)
        except SQLAlchemyError as error:
            logger.exception("Could not load profile %s", profile_id)
            self._status_bar.set_scan(f"Błąd bazy danych: {error}")
            return None
        if plan is None:
            self._status_bar.set_scan("Profil nie istnieje.")
            self.reload_profiles()
        return plan

    def _select_sources(self, plan: ScanPlan) -> list[SourceDefinition] | None:
        """Resolve which sources to scan; None means the scan cannot start."""
        selected = self._source_dropdown.value or ALL_SOURCES_KEY
        candidates = [
            source for source in self._sources if selected in (ALL_SOURCES_KEY, source.key)
        ]
        sources = [source for source in candidates if source_enabled(plan.settings, source.key)]
        if sources:
            return sources
        if selected == ALL_SOURCES_KEY:
            self._status_bar.set_scan("Profil nie ma włączonych źródeł.")
        else:
            self._status_bar.set_scan(
                f"Źródło {candidates[0].name} jest wyłączone w profilu „{plan.profile.name}”."
            )
        return None

    async def _on_scan_click(self, _: Any) -> None:
        logger.info("Scan initiated")
        plan = self._load_plan()
        if plan is None:
            return
        sources = self._select_sources(plan)
        if sources is None:
            return

        loop = asyncio.get_running_loop()
        self._scan_button.disabled = True
        self._stop_event.clear()
        self._log_column.controls.clear()
        self._progress_bar.value = None
        self._page.update()

        completed = False
        try:
            outcomes = await loop.run_in_executor(
                self._scan_executor,
                lambda: run_scan(
                    plan,
                    sources,
                    stop_event=self._stop_event,
                    callbacks=self._make_callbacks(loop),
                    db_path=self._db_path,
                ),
            )
            saved = [offer for outcome in outcomes for offer in outcome.saved]
            can_eval = (
                bool(saved)
                and self._config.auto_evaluate
                and bool(self._evaluation_service.get_cv_text())
            )
            if saved:
                self._offers_view.add_new_offers(saved, pending_evaluation=can_eval)
                self._on_scan_finished()
            self._status_bar.set_scan(format_scan_message(outcomes))
            completed = True
        finally:
            # Re-enable scanning even after an unexpected error so the user is never blocked.
            self._scan_button.disabled = False
            self._progress_bar.value = 1.0 if completed else 0.0
            self._page.update()

        if can_eval:
            newly_inserted_ids = [inserted_id for _, inserted_id in reversed(saved)]
            logger.info("Enqueuing %s offers for background AI evaluation", len(newly_inserted_ids))
            await self._offers_view.enqueue_evaluations(newly_inserted_ids)
