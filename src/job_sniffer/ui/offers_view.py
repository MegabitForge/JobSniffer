import asyncio
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any

import flet as ft
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.config import AppConfig
from job_sniffer.database import (
    DB_PATH,
    connect,
    delete_evaluation,
    delete_offer,
    get_evaluation,
    get_offer_by_id,
    init_db,
    list_offers_with_evaluations,
    list_unevaluated_offers,
)
from job_sniffer.models import JobOffer, JobOfferEvaluation
from job_sniffer.services import EvaluationService
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)

PAGE_SIZE = 50


@dataclass(frozen=True)
class OfferRow:
    """Plain snapshot of one saved offer, safe to use after the DB session closes."""

    offer_id: int | None
    title: str
    company: str
    location: str | None
    salary: str | None
    url: str | None
    source: str | None
    evaluation: JobOfferEvaluation | None


class OffersView:
    """Saved offers list plus the background AI evaluation worker."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        evaluation_service: EvaluationService,
        status_bar: StatusBar,
        stop_event: threading.Event,
    ) -> None:
        self._page = page
        self._config = config
        self._evaluation_service = evaluation_service
        self._status_bar = status_bar
        self._stop_event = stop_event

        # Active offer cards mapped by offer_id to allow dynamic non-blocking live updates
        self._offer_cards_by_id: dict[int, ft.Card] = {}
        self._force_eval_ids: set[int] = set()
        self._failed_eval_ids: set[int] = set()
        self._pending_eval_ids: set[int] = set()
        self._eval_queue: asyncio.Queue[int] = asyncio.Queue()
        self._eval_in_progress = False

        self._current_page = 0
        self._total_pages = 1

        self.offers_column = ft.Column(spacing=12, scroll=ft.ScrollMode.ADAPTIVE, expand=True)

        refresh_button = ft.OutlinedButton(
            "Odśwież listę",
            icon=ft.Icons.REFRESH,
            on_click=self._on_refresh_click,
        )
        eval_pending_button = ft.OutlinedButton(
            "Oceń nie ocenione",
            icon=ft.Icons.AUTO_AWESOME,
            tooltip="Przeanalizuj wszystkie oferty, które nie mają oceny lub których ocena zakończyła się błędem",
            on_click=self._on_eval_pending_click,
        )

        self._prev_page_button = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT,
            tooltip="Poprzednia strona",
            on_click=self._on_previous_page_click,
        )
        self._next_page_button = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT,
            tooltip="Następna strona",
            on_click=self._on_next_page_click,
        )
        self._page_label = ft.Text("", size=13)
        self._pagination_row = ft.Row(
            controls=[self._prev_page_button, self._page_label, self._next_page_button],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
            visible=False,
        )

        self.control = ft.Column(
            controls=[
                ft.Text(
                    "Zapisane oferty pracy i ocena dopasowania CV:",
                    size=16,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Row([refresh_button, eval_pending_button], spacing=12, wrap=True),
                self.offers_column,
                self._pagination_row,
            ],
            spacing=12,
            expand=True,
        )

        self.reload_offers()

    def _handle_delete_offer(self, offer_id: int) -> None:
        """Delete an offer from database and remove its card from the UI."""
        try:
            with connect(DB_PATH) as session:
                deleted = delete_offer(session, offer_id)
        except (SQLAlchemyError, OSError) as del_err:
            logger.warning("Failed to delete offer %s: %s", offer_id, del_err)
            return

        if deleted:
            self._offer_cards_by_id.pop(offer_id, None)
            self._pending_eval_ids.discard(offer_id)
            self._render_page()
            self._status_bar.set_scan("Oferta została pomyślnie usunięta.")

    async def _handle_re_evaluate_offer(self, offer_id: int) -> None:
        """Reset existing AI evaluation and re-run analysis in the background."""
        if not self._evaluation_service.get_cv_text():
            self._status_bar.show_ai("Najpierw wybierz plik CV w zakładce 'Profiles'.")
            return

        rec = None
        try:
            with connect(DB_PATH) as session:
                delete_evaluation(session, offer_id)
                rec = get_offer_by_id(session, offer_id)
        except (SQLAlchemyError, OSError) as reset_err:
            logger.warning("Failed to reset evaluation for offer %s: %s", offer_id, reset_err)
            return

        if rec is not None and offer_id in self._offer_cards_by_id:
            card = self._offer_cards_by_id[offer_id]
            card.content = self._build_card_inner(
                title=rec.title,
                company=rec.company,
                offer_location=rec.location,
                salary=rec.salary,
                url=rec.url,
                source_name=rec.source,
                evaluation=None,
                offer_id=offer_id,
                pending_evaluation=True,
            )
            card.update()

        self._force_eval_ids.add(offer_id)
        self._failed_eval_ids.discard(offer_id)
        self._pending_eval_ids.add(offer_id)
        await self.enqueue_evaluations([offer_id])

    def _build_card_inner(
        self,
        title: str,
        company: str,
        offer_location: str | None,
        salary: str | None,
        url: str | None,
        source_name: str | None = None,
        evaluation: JobOfferEvaluation | None = None,
        offer_id: int | None = None,
        pending_evaluation: bool = False,
        evaluation_failed: bool = False,
    ) -> ft.Container:
        details_row = ft.Row(
            controls=[
                ft.Text(f"{company}", weight=ft.FontWeight.BOLD),
                ft.Text(f"{offer_location or 'Nie podano'}"),
                ft.Text(f"{salary or 'Nie podano widełek'}", color=ft.Colors.BLUE_GREY_700),
            ],
            wrap=True,
            spacing=16,
        )

        action_controls: list[ft.Control] = []
        if url:
            action_controls.append(
                ft.TextButton(
                    "Otwórz ofertę w przeglądarce 🔗",
                    url=url,
                )
            )
        else:
            action_controls.append(ft.Container())

        if offer_id is not None:
            action_controls.append(
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.REPLAY,
                            icon_color=ft.Colors.BLUE_700,
                            tooltip="Zresetuj ocenę AI i przeanalizuj tę ofertę ponownie",
                            on_click=lambda _, oid=offer_id: asyncio.create_task(
                                self._handle_re_evaluate_offer(oid)
                            ),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_color=ft.Colors.RED_500,
                            tooltip="Usuń tę ofertę z listy",
                            on_click=lambda _, oid=offer_id: self._handle_delete_offer(oid),
                        ),
                    ],
                    spacing=2,
                    tight=True,
                )
            )

        actions_row = ft.Row(
            controls=action_controls,
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

        eval_controls: list[ft.Control] = []
        if evaluation:
            badge_color = (
                ft.Colors.GREEN_700
                if evaluation.fit_score >= 70
                else (ft.Colors.AMBER_700 if evaluation.fit_score >= 45 else ft.Colors.RED_700)
            )
            badge = ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.AUTO_AWESOME, color=ft.Colors.WHITE, size=18),
                        ft.Text(
                            f"Szansa na odpowiedź: {evaluation.fit_score}% | {evaluation.verdict}",
                            color=ft.Colors.WHITE,
                            weight=ft.FontWeight.BOLD,
                            size=13,
                        ),
                    ],
                    tight=True,
                    spacing=6,
                ),
                bgcolor=badge_color,
                border_radius=8,
                padding=ft.Padding(10, 6, 10, 6),
            )
            eval_controls.append(badge)

            if getattr(evaluation, "explanation", ""):
                eval_controls.append(
                    ft.Text(
                        f"Uzasadnienie: {evaluation.explanation}",
                        italic=True,
                        size=13,
                        color=ft.Colors.GREY_400,
                    )
                )

            if evaluation.summary:
                eval_controls.append(
                    ft.Text(
                        f"Opis i profil: {evaluation.summary}",
                        italic=True,
                        size=13,
                    )
                )

            if evaluation.strengths:
                eval_controls.append(
                    ft.Column(
                        controls=[
                            ft.Text(
                                "Mocne strony kandydata (pokrycie wymagań z oferty):",
                                weight=ft.FontWeight.BOLD,
                                size=13,
                                color=ft.Colors.GREEN_800,
                            ),
                            *[
                                ft.Row(
                                    [
                                        ft.Icon(
                                            ft.Icons.CHECK_CIRCLE,
                                            color=ft.Colors.GREEN,
                                            size=16,
                                        ),
                                        ft.Text(item, size=12, expand=True),
                                    ],
                                    vertical_alignment=ft.CrossAxisAlignment.START,
                                )
                                for item in evaluation.strengths
                            ],
                        ],
                        spacing=3,
                    )
                )

            if evaluation.weaknesses:
                eval_controls.append(
                    ft.Column(
                        controls=[
                            ft.Text(
                                "Luki i braki względem faktycznych wymagań oferty:",
                                weight=ft.FontWeight.BOLD,
                                size=13,
                                color=ft.Colors.RED_800,
                            ),
                            *[
                                ft.Row(
                                    [
                                        ft.Icon(
                                            ft.Icons.REMOVE_CIRCLE_OUTLINE,
                                            color=ft.Colors.RED,
                                            size=16,
                                        ),
                                        ft.Text(item, size=12, expand=True),
                                    ],
                                    vertical_alignment=ft.CrossAxisAlignment.START,
                                )
                                for item in evaluation.weaknesses
                            ],
                        ],
                        spacing=3,
                    )
                )
        elif pending_evaluation:
            eval_controls.append(
                ft.Row(
                    [
                        ft.ProgressRing(width=16, height=16, stroke_width=2),
                        ft.Text(
                            "Oczekiwanie na analizę dopasowania przez AI w tle...",
                            size=12,
                            color=ft.Colors.BLUE_700,
                            italic=True,
                        ),
                    ],
                    spacing=8,
                )
            )
        elif evaluation_failed:
            eval_controls.append(
                ft.Row(
                    [
                        ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.AMBER_800, size=16),
                        ft.Text(
                            "Ocena AI nie powiodła się. Kliknij 'Oceń nie ocenione' lub ikonę powtórzenia, aby spróbować ponownie.",
                            size=12,
                            color=ft.Colors.AMBER_900,
                            italic=True,
                        ),
                    ],
                    spacing=6,
                )
            )
        else:
            eval_controls.append(
                ft.Text(
                    "Brak oceny dopasowania (Upewnij się, że CV jest wybrane w zakładce 'Profiles', "
                    "a silnik LLM skonfigurowany w zakładce 'LLM').",
                    size=12,
                    color=ft.Colors.GREY_600,
                    italic=True,
                )
            )

        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text(title, size=16, weight=ft.FontWeight.BOLD, expand=True),
                            ft.Text(f"[{source_name}]" if source_name else "", size=12),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    details_row,
                    actions_row,
                    ft.Divider(),
                    *eval_controls,
                ],
                spacing=8,
            ),
            padding=14,
        )

    def _build_offer_card(
        self,
        title: str,
        company: str,
        offer_location: str | None,
        salary: str | None,
        url: str | None,
        source_name: str | None = None,
        evaluation: JobOfferEvaluation | None = None,
        offer_id: int | None = None,
        pending_evaluation: bool = False,
        evaluation_failed: bool = False,
    ) -> ft.Card:
        inner = self._build_card_inner(
            title=title,
            company=company,
            offer_location=offer_location,
            salary=salary,
            url=url,
            source_name=source_name,
            evaluation=evaluation,
            offer_id=offer_id,
            pending_evaluation=pending_evaluation,
            evaluation_failed=evaluation_failed,
        )
        card = ft.Card(content=inner)
        if offer_id is not None:
            self._offer_cards_by_id[offer_id] = card
        return card

    def _to_evaluation(self, eval_rec: Any) -> JobOfferEvaluation:
        """Convert a database evaluation record into the domain model."""
        return JobOfferEvaluation(
            offer_id=eval_rec.offer_id,
            fit_score=eval_rec.fit_score,
            verdict=eval_rec.verdict,
            explanation=getattr(eval_rec, "explanation", ""),
            summary=eval_rec.summary,
            strengths=eval_rec.strengths,
            weaknesses=eval_rec.weaknesses,
            raw_response=eval_rec.raw_response,
            evaluated_at=eval_rec.evaluated_at,
        )

    def _fetch_rows(self) -> list[OfferRow]:
        """Read all saved offers with evaluations as plain snapshots (newest first)."""
        with connect(DB_PATH) as session:
            init_db(session)
            records = list_offers_with_evaluations(session, limit=None)
            return [
                OfferRow(
                    offer_id=offer_rec.id,
                    title=offer_rec.title,
                    company=offer_rec.company,
                    location=offer_rec.location,
                    salary=offer_rec.salary,
                    url=offer_rec.url,
                    source=offer_rec.source,
                    evaluation=(self._to_evaluation(eval_rec) if eval_rec is not None else None),
                )
                for offer_rec, eval_rec in records
            ]

    def _update_pagination(self, total_offers: int) -> None:
        """Sync pagination controls with the current page state."""
        has_multiple_pages = self._total_pages > 1
        self._pagination_row.visible = has_multiple_pages
        self._page_label.value = (
            f"Strona {self._current_page + 1} z {self._total_pages} ({total_offers} ofert)"
        )
        self._prev_page_button.disabled = self._current_page == 0
        self._next_page_button.disabled = self._current_page >= self._total_pages - 1

    def _render_page(self) -> None:
        """Rebuild the offers column with cards only for the currently visible page."""
        try:
            rows = self._fetch_rows()
        except (SQLAlchemyError, OSError) as err:
            logger.warning("Failed to load saved offers: %s", err)
            rows = []

        self._total_pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
        self._current_page = min(self._current_page, self._total_pages - 1)
        start = self._current_page * PAGE_SIZE
        page_rows = rows[start : start + PAGE_SIZE]

        self._offer_cards_by_id.clear()
        self.offers_column.controls.clear()

        if not rows:
            self.offers_column.controls.append(ft.Text("Brak zapisanych ofert pracy."))

        for row in page_rows:
            self.offers_column.controls.append(
                self._build_offer_card(
                    title=row.title,
                    company=row.company,
                    offer_location=row.location,
                    salary=row.salary,
                    url=row.url,
                    source_name=row.source,
                    evaluation=row.evaluation,
                    offer_id=row.offer_id,
                    pending_evaluation=row.offer_id in self._pending_eval_ids,
                )
            )

        self._update_pagination(len(rows))
        self._page.update()

    def _on_previous_page_click(self, _: Any) -> None:
        if self._current_page > 0:
            self._current_page -= 1
            self._render_page()

    def _on_next_page_click(self, _: Any) -> None:
        if self._current_page < self._total_pages - 1:
            self._current_page += 1
            self._render_page()

    def reload_offers(self) -> None:
        """Reload the current page of saved offers and evaluations from SQLite."""
        self._render_page()

    def add_new_offers(
        self, offers_with_ids: list[tuple[JobOffer, int]], pending_evaluation: bool
    ) -> None:
        """Prepare freshly scanned offers to be shown on the first page."""
        if pending_evaluation:
            self._pending_eval_ids.update(offer_id for _, offer_id in offers_with_ids)
        self._current_page = 0

    async def enqueue_evaluations(self, offer_ids: list[int]) -> None:
        """Enqueue offer IDs for background evaluation without blocking searches."""
        if not offer_ids:
            return
        for oid in offer_ids:
            await self._eval_queue.put(oid)
        if not self._eval_in_progress:
            asyncio.create_task(self._run_eval_worker())

    async def _run_eval_worker(self) -> None:
        self._eval_in_progress = True
        self._status_bar.show_ai()

        try:
            while not self._eval_queue.empty():
                if self._stop_event.is_set():
                    break
                offer_id = await self._eval_queue.get()
                remaining = self._eval_queue.qsize() + 1
                self._status_bar.set_ai(f"AI ocenia oferty w tle (pozostało: {remaining})...")

                session = connect(DB_PATH)
                offer_record = None
                try:
                    offer_record = get_offer_by_id(session, offer_id)
                    if offer_record is None:
                        self._pending_eval_ids.discard(offer_id)
                        continue

                    existing_eval = get_evaluation(session, offer_id)
                    if existing_eval is not None and offer_id not in self._force_eval_ids:
                        self._pending_eval_ids.discard(offer_id)
                        if offer_id in self._offer_cards_by_id:
                            card = self._offer_cards_by_id[offer_id]
                            card.content = self._build_card_inner(
                                title=offer_record.title,
                                company=offer_record.company,
                                offer_location=offer_record.location,
                                salary=offer_record.salary,
                                url=offer_record.url,
                                source_name=offer_record.source,
                                evaluation=existing_eval,
                                offer_id=offer_id,
                                pending_evaluation=False,
                            )
                            card.update()
                        continue

                    self._force_eval_ids.discard(offer_id)
                    evaluation = await self._evaluation_service.evaluate_offer(
                        offer_record, offer_id, session
                    )
                    is_failed = evaluation is None
                    self._pending_eval_ids.discard(offer_id)
                    if is_failed:
                        self._failed_eval_ids.add(offer_id)
                    else:
                        self._failed_eval_ids.discard(offer_id)

                    if offer_id in self._offer_cards_by_id:
                        card = self._offer_cards_by_id[offer_id]
                        card.content = self._build_card_inner(
                            title=offer_record.title,
                            company=offer_record.company,
                            offer_location=offer_record.location,
                            salary=offer_record.salary,
                            url=offer_record.url,
                            source_name=offer_record.source,
                            evaluation=evaluation,
                            offer_id=offer_id,
                            pending_evaluation=False,
                            evaluation_failed=is_failed,
                        )
                        card.update()
                except (SQLAlchemyError, RuntimeError, ValueError, TypeError, OSError) as eval_err:
                    logger.warning("Error evaluating offer %s: %s", offer_id, eval_err)
                    self._failed_eval_ids.add(offer_id)
                    self._pending_eval_ids.discard(offer_id)
                    if offer_id in self._offer_cards_by_id and offer_record is not None:
                        card = self._offer_cards_by_id[offer_id]
                        card.content = self._build_card_inner(
                            title=offer_record.title,
                            company=offer_record.company,
                            offer_location=offer_record.location,
                            salary=offer_record.salary,
                            url=offer_record.url,
                            source_name=offer_record.source,
                            evaluation=None,
                            offer_id=offer_id,
                            pending_evaluation=False,
                            evaluation_failed=True,
                        )
                        card.update()
                finally:
                    session.close()

            self._status_bar.finish_ai("AI: Wszystkie oczekujące oferty zostały ocenione.")
            await asyncio.sleep(3.5)
            if self._eval_queue.empty():
                self._status_bar.hide_ai()
        finally:
            self._eval_in_progress = False

    def _on_refresh_click(self, _: Any) -> None:
        self.reload_offers()
        self._page.update()

    async def _on_eval_pending_click(self, _: Any) -> None:
        if not self._evaluation_service.get_cv_text():
            self._status_bar.show_ai("Najpierw wybierz plik CV w zakładce 'Profiles'.")
            return

        with connect(DB_PATH) as session:
            unevaluated = list_unevaluated_offers(session, limit=None)
            pending_ids = [item.id for item in unevaluated]

        for fid in list(self._failed_eval_ids):
            if fid not in pending_ids:
                pending_ids.append(fid)

        if not pending_ids:
            self._status_bar.show_ai("Wszystkie oferty w bazie posiadają już ocenę.")
            await asyncio.sleep(2.5)
            self._status_bar.hide_ai()
            return

        # Mark controls as pending and force re-evaluation
        self._pending_eval_ids.update(pending_ids)
        for pid in pending_ids:
            self._force_eval_ids.add(pid)
            self._failed_eval_ids.discard(pid)
            if pid in self._offer_cards_by_id:
                card = self._offer_cards_by_id[pid]
                with connect(DB_PATH) as session:
                    rec = get_offer_by_id(session, pid)
                    if rec:
                        card.content = self._build_card_inner(
                            title=rec.title,
                            company=rec.company,
                            offer_location=rec.location,
                            salary=rec.salary,
                            url=rec.url,
                            source_name=rec.source,
                            evaluation=None,
                            offer_id=pid,
                            pending_evaluation=True,
                            evaluation_failed=False,
                        )
                        card.update()

        self._status_bar.set_ai(f"AI: Rozpoczęto ocenę {len(pending_ids)} nieocenionych ofert...")
        await self.enqueue_evaluations(pending_ids)
