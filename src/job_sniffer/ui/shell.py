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

from job_sniffer.config import AppConfig, load_config, save_config
from job_sniffer.database import (
    SaveStats,
    connect,
    delete_evaluation,
    delete_offer,
    get_evaluation,
    get_offer_by_id,
    init_db,
    list_offers_with_evaluations,
    list_unevaluated_offers,
    offer_exists,
    save_offer,
)
from job_sniffer.llm.catalog import AVAILABLE_MODELS, get_model_by_id
from job_sniffer.llm.hardware import detect_gpu, format_gpu_summary
from job_sniffer.models import JobOffer, JobOfferEvaluation, JobSearch
from job_sniffer.services import EvaluationService
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
    """Build the application UI shell with persistent offers, delete/re-evaluate buttons, and dynamic navigation."""
    config: AppConfig = load_config()
    evaluation_service = EvaluationService(config)

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    stop_event = threading.Event()
    scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job-sniffer-scan")

    # Scanner controls
    keywords = ft.TextField(label="Słowa kluczowe (Keywords)", value="")
    location = ft.TextField(label="Lokalizacja (Location)", hint_text="Opcjonalnie", value="")
    limit = ft.TextField(
        label="Limit ofert",
        hint_text="Puste = brak limitu",
        value="5",
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    source = ft.Dropdown(
        label="Źródło portalu",
        value=ACTIVE_SOURCES[0].key if ACTIVE_SOURCES else None,
        options=[ft.dropdown.Option(key=item.key, text=item.name) for item in ACTIVE_SOURCES],
    )
    keywords.disabled = source.value == "bulldogjob"
    if keywords.disabled:
        keywords.hint_text = "Niewspierane przez Bulldogjob"

    scan_status = ft.Text("Gotowy do skanowania.", size=14)
    scan_button = ft.ElevatedButton("Skanuj oferty", icon=ft.Icons.SEARCH)
    offers_column = ft.Column(spacing=12, scroll=ft.ScrollMode.ADAPTIVE, expand=True)

    # Background AI status controls
    ai_status_text = ft.Text("", size=13, weight=ft.FontWeight.W_500, color=ft.Colors.BLUE_800)
    ai_progress_ring = ft.ProgressRing(width=16, height=16, stroke_width=2)
    ai_status_row = ft.Row([ai_progress_ring, ai_status_text], visible=False, spacing=8)

    # Active offer cards mapped by offer_id to allow dynamic non-blocking live updates
    offer_cards_by_id: dict[int, ft.Card] = {}
    force_eval_ids: set[int] = set()
    failed_eval_ids: set[int] = set()

    def on_source_change(_: Any) -> None:
        bulldogjob_selected = source.value == "bulldogjob"
        keywords.disabled = bulldogjob_selected
        keywords.hint_text = "Niewspierane przez Bulldogjob" if bulldogjob_selected else None
        page.update()

    source.on_select = on_source_change

    def handle_delete_offer(offer_id: int) -> None:
        """Delete an offer from database and remove its card from the UI."""
        try:
            with connect(Path("job_sniffer.sqlite")) as session:
                deleted = delete_offer(session, offer_id)
        except (SQLAlchemyError, OSError) as del_err:
            logger.warning("Failed to delete offer %s: %s", offer_id, del_err)
            return

        if deleted:
            card = offer_cards_by_id.pop(offer_id, None)
            if card is not None and card in offers_column.controls:
                offers_column.controls.remove(card)
            scan_status.value = "Oferta została pomyślnie usunięta."
            page.update()

    async def handle_re_evaluate_offer(offer_id: int) -> None:
        """Reset existing AI evaluation and re-run analysis in the background."""
        if not evaluation_service.get_cv_text():
            ai_status_text.value = "Najpierw wybierz plik CV w zakładce 'Konfiguracja LLM & CV'."
            ai_status_row.visible = True
            page.update()
            return

        rec = None
        try:
            with connect(Path("job_sniffer.sqlite")) as session:
                delete_evaluation(session, offer_id)
                rec = get_offer_by_id(session, offer_id)
        except (SQLAlchemyError, OSError) as reset_err:
            logger.warning("Failed to reset evaluation for offer %s: %s", offer_id, reset_err)
            return

        if rec is not None and offer_id in offer_cards_by_id:
            card = offer_cards_by_id[offer_id]
            card.content = build_card_inner(
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

        force_eval_ids.add(offer_id)
        failed_eval_ids.discard(offer_id)
        await trigger_evaluations([offer_id])

    def build_card_inner(
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
                ft.Text(f"🏢 {company}", weight=ft.FontWeight.BOLD),
                ft.Text(f"📍 {offer_location or 'Zdalnie / Nie podano'}"),
                ft.Text(f"💰 {salary or 'Nie podano widełek'}", color=ft.Colors.BLUE_GREY_700),
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
                                handle_re_evaluate_offer(oid)
                            ),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_color=ft.Colors.RED_500,
                            tooltip="Usuń tę ofertę z listy",
                            on_click=lambda _, oid=offer_id: handle_delete_offer(oid),
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
                    "Brak oceny dopasowania (Upewnij się, że CV i silnik LLM są skonfigurowane w zakładce 'Konfiguracja LLM & CV').",
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

    def build_offer_card(
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
        inner = build_card_inner(
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
            offer_cards_by_id[offer_id] = card
        return card

    def load_saved_offers_into_ui() -> None:
        """Load all previous saved offers and evaluations from SQLite into the results list."""
        try:
            with connect(Path("job_sniffer.sqlite")) as session:
                init_db(session)
                rows = list_offers_with_evaluations(session, limit=None)
                offers_column.controls.clear()
                offer_cards_by_id.clear()
                for offer_rec, eval_rec in rows:
                    evaluation: JobOfferEvaluation | None = None
                    if eval_rec is not None:
                        evaluation = JobOfferEvaluation(
                            offer_id=eval_rec.offer_id,
                            fit_score=eval_rec.fit_score,
                            verdict=eval_rec.verdict,
                            summary=eval_rec.summary,
                            strengths=eval_rec.strengths,
                            weaknesses=eval_rec.weaknesses,
                            raw_response=eval_rec.raw_response,
                            evaluated_at=eval_rec.evaluated_at,
                        )
                    card = build_offer_card(
                        title=offer_rec.title,
                        company=offer_rec.company,
                        offer_location=offer_rec.location,
                        salary=offer_rec.salary,
                        url=offer_rec.url,
                        source_name=offer_rec.source,
                        evaluation=evaluation,
                        offer_id=offer_rec.id,
                        pending_evaluation=False,
                    )
                    offers_column.controls.append(card)
        except (SQLAlchemyError, OSError) as err:
            logger.warning("Failed to preload existing offers: %s", err)

    load_saved_offers_into_ui()

    # Background async AI evaluation worker
    eval_queue: asyncio.Queue[int] = asyncio.Queue()
    eval_in_progress = False

    async def run_eval_worker() -> None:
        nonlocal eval_in_progress
        if eval_in_progress:
            return
        eval_in_progress = True
        ai_status_row.visible = True
        ai_progress_ring.visible = True
        page.update()

        try:
            while not eval_queue.empty():
                if stop_event.is_set():
                    break
                offer_id = await eval_queue.get()
                remaining = eval_queue.qsize() + 1
                ai_status_text.value = f"AI ocenia oferty w tle (pozostało: {remaining})..."
                page.update()

                session = connect(Path("job_sniffer.sqlite"))
                offer_record = None
                try:
                    offer_record = get_offer_by_id(session, offer_id)
                    if offer_record is None:
                        continue

                    existing_eval = get_evaluation(session, offer_id)
                    if existing_eval is not None and offer_id not in force_eval_ids:
                        if offer_id in offer_cards_by_id:
                            card = offer_cards_by_id[offer_id]
                            card.content = build_card_inner(
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

                    force_eval_ids.discard(offer_id)
                    evaluation = await evaluation_service.evaluate_offer(
                        offer_record, offer_id, session
                    )
                    is_failed = evaluation is None
                    if is_failed:
                        failed_eval_ids.add(offer_id)
                    else:
                        failed_eval_ids.discard(offer_id)

                    if offer_id in offer_cards_by_id:
                        card = offer_cards_by_id[offer_id]
                        card.content = build_card_inner(
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
                    failed_eval_ids.add(offer_id)
                    if offer_id in offer_cards_by_id and offer_record is not None:
                        card = offer_cards_by_id[offer_id]
                        card.content = build_card_inner(
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

            ai_status_text.value = "AI: Wszystkie oczekujące oferty zostały ocenione."
            ai_progress_ring.visible = False
            page.update()
            await asyncio.sleep(3.5)
            if eval_queue.empty():
                ai_status_row.visible = False
                ai_progress_ring.visible = True
                page.update()
        finally:
            eval_in_progress = False

    async def trigger_evaluations(offer_ids: list[int]) -> None:
        """Enqueue offer IDs for background evaluation without blocking searches."""
        if not offer_ids:
            return
        for oid in offer_ids:
            await eval_queue.put(oid)
        if not eval_in_progress:
            asyncio.create_task(run_eval_worker())

    # Scanning logic (strictly non-blocking: saves immediately, never calls LLM synchronously)
    def _scan_source(
        source_key: str, search: JobSearch
    ) -> tuple[list[tuple[JobOffer, int]], SaveStats]:
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

            saved_results: list[tuple[JobOffer, int]] = []
            save_seen = 0
            save_duplicates = 0

            def save_enriched_offer(offer: JobOffer) -> bool:
                nonlocal save_seen, save_duplicates
                if stop_event.is_set():
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
                    stop_event=stop_event,
                    duplicate_checker=duplicate_checker,
                    enriched_offer_handler=save_enriched_offer,
                ).search(search)
            except Exception as error:
                if not saved_results:
                    raise
                stats = _include_pre_enrich_duplicates(
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

            if stop_event.is_set():
                raise RuntimeError("Scan cancelled because the application is closing.")

            logger.info("Saved %s enriched offers from %s", len(saved_results), source_key)
            stats = SaveStats(
                seen=save_seen,
                inserted=len(saved_results),
                duplicates=save_duplicates,
            )
            return saved_results, _include_pre_enrich_duplicates(stats, pre_enrich_duplicates)
        finally:
            logger.info("Closing SQLite session")
            session.close()

    def _include_pre_enrich_duplicates(stats: SaveStats, duplicates: int) -> SaveStats:
        if duplicates == 0:
            return stats
        return SaveStats(
            seen=stats.seen + duplicates,
            inserted=stats.inserted,
            duplicates=stats.duplicates + duplicates,
        )

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

        if not source_key or (limit_value is not None and limit_value < 1):
            scan_status.value = "Wybierz źródło i podaj poprawny limit (liczba dodatnia)."
            page.update()
            return

        scan_button.disabled = True
        stop_event.clear()
        scan_status.value = f"Skanowanie {_source_name(source_key)}..."
        page.update()

        search = JobSearch(keywords=keywords_value, location=location_value, limit=limit_value)
        newly_inserted_ids: list[int] = []

        try:
            loop = asyncio.get_running_loop()
            results, stats = await loop.run_in_executor(
                scan_executor,
                _scan_source,
                source_key,
                search,
            )
        except ScanInterruptedError as error:
            logger.exception("Scan stopped after partial success: source=%s", source_key)
            scan_status.value = (
                f"Skanowanie {_source_name(source_key)} przerwane po zapisaniu "
                f"{error.stats.inserted} ofert. Błąd: {error}"
            )
            load_saved_offers_into_ui()
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
            scan_status.value = f"Błąd skanowania {_source_name(source_key)}: {error}"
        else:
            scan_status.value = (
                f"Pobrano {stats.seen} ofert z {_source_name(source_key)}. "
                f"Zapisano {stats.inserted}, pominięto duplikaty: {stats.duplicates}."
            )
            can_eval = config.auto_evaluate and bool(evaluation_service.get_cv_text())

            # Prepend new offers to the existing list so previous offers are always preserved
            for offer_obj, inserted_id in reversed(results):
                newly_inserted_ids.append(inserted_id)
                card = build_offer_card(
                    title=offer_obj.title,
                    company=offer_obj.company,
                    offer_location=offer_obj.location,
                    salary=offer_obj.salary,
                    url=offer_obj.url,
                    source_name=offer_obj.source,
                    evaluation=None,
                    offer_id=inserted_id,
                    pending_evaluation=can_eval,
                )
                offers_column.controls.insert(0, card)

            if not offers_column.controls:
                offers_column.controls.append(ft.Text("Brak zapisanych ofert pracy."))
        finally:
            # Re-enable scanning immediately so user is never blocked
            scan_button.disabled = False
            page.update()

        # Trigger AI evaluation on the newly saved offers at the very end in the background
        if config.auto_evaluate and newly_inserted_ids and evaluation_service.get_cv_text():
            logger.info("Enqueuing %s offers for background AI evaluation", len(newly_inserted_ids))
            await trigger_evaluations(newly_inserted_ids)

    scan_button.on_click = on_scan_click

    def on_refresh_click(_: Any) -> None:
        load_saved_offers_into_ui()
        page.update()

    refresh_button = ft.OutlinedButton(
        "Odśwież listę",
        icon=ft.Icons.REFRESH,
        on_click=on_refresh_click,
    )

    async def on_eval_pending_click(_: Any) -> None:
        if not evaluation_service.get_cv_text():
            ai_status_text.value = "Najpierw wybierz plik CV w zakładce 'Konfiguracja LLM & CV'."
            ai_status_row.visible = True
            page.update()
            return

        with connect(Path("job_sniffer.sqlite")) as session:
            unevaluated = list_unevaluated_offers(session, limit=None)
            pending_ids = [item.id for item in unevaluated]

        # Dołącz również oferty, w których ocena zakończyła się wcześniej błędem
        for fid in list(failed_eval_ids):
            if fid not in pending_ids:
                pending_ids.append(fid)

        if not pending_ids:
            ai_status_text.value = "Wszystkie oferty w bazie posiadają już ocenę."
            ai_status_row.visible = True
            page.update()
            await asyncio.sleep(2.5)
            ai_status_row.visible = False
            page.update()
            return

        # Mark controls as pending and force re-evaluation
        for pid in pending_ids:
            force_eval_ids.add(pid)
            failed_eval_ids.discard(pid)
            if pid in offer_cards_by_id:
                c = offer_cards_by_id[pid]
                with connect(Path("job_sniffer.sqlite")) as session:
                    rec = get_offer_by_id(session, pid)
                    if rec:
                        c.content = build_card_inner(
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
                        c.update()

        ai_status_text.value = f"AI: Rozpoczęto ocenę {len(pending_ids)} nieocenionych ofert..."
        ai_status_row.visible = True
        page.update()
        await trigger_evaluations(pending_ids)

    eval_pending_btn = ft.OutlinedButton(
        "Oceń nie ocenione",
        icon=ft.Icons.AUTO_AWESOME,
        tooltip="Przeanalizuj wszystkie oferty, które nie mają oceny lub których ocena zakończyła się błędem",
        on_click=on_eval_pending_click,
    )

    # Scanner View
    scanner_view = ft.Column(
        controls=[
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row([keywords, location], spacing=12),
                        ft.Row([limit, source], spacing=12),
                        ft.Row(
                            [scan_button, refresh_button, eval_pending_btn, scan_status],
                            spacing=12,
                            wrap=True,
                        ),
                        ai_status_row,
                    ],
                    spacing=10,
                ),
                border_radius=10,
                padding=12,
            ),
            ft.Text(
                "Zapisane oferty pracy i ocena dopasowania CV:", size=16, weight=ft.FontWeight.BOLD
            ),
            offers_column,
        ],
        spacing=12,
        expand=True,
    )

    # LLM & CV Configuration View
    cv_path_field = ft.TextField(
        label="Ścieżka do pliku CV",
        value=config.cv_path or "",
        read_only=True,
        expand=True,
    )
    cv_status_text = ft.Text(
        "Załadowano CV" if config.cv_path else "Brak wskazanego pliku CV",
        color=ft.Colors.GREEN_700 if config.cv_path else ft.Colors.ORANGE_800,
    )

    async def on_pick_cv_click(_: Any) -> None:
        files = await file_picker.pick_files(
            dialog_title="Wybierz plik CV",
            allowed_extensions=["pdf", "txt", "md"],
        )
        if files and files[0].path:
            config.cv_path = files[0].path
            save_config(config)
            cv_path_field.value = config.cv_path
            text = evaluation_service.get_cv_text(reload=True)
            if text:
                cv_status_text.value = f"Załadowano pomyślnie ({len(text)} znaków)"
                cv_status_text.color = ft.Colors.GREEN_700
            else:
                cv_status_text.value = "Nie udało się odczytać treści pliku CV"
                cv_status_text.color = ft.Colors.RED_700
            page.update()

    pick_cv_button = ft.ElevatedButton(
        "Wybierz plik CV (.pdf, .txt, .md)",
        icon=ft.Icons.ATTACH_FILE,
        on_click=on_pick_cv_click,
    )

    # Hardware / GPU detection
    gpu_info = detect_gpu()
    gpu_status_icon = ft.Icon(
        ft.Icons.MEMORY,
        color=ft.Colors.GREEN_700 if gpu_info.has_gpu else ft.Colors.GREY_600,
        size=22,
    )
    gpu_status_text = ft.Text(
        format_gpu_summary(gpu_info),
        size=13,
        weight=ft.FontWeight.W_500,
    )
    gpu_status_row = ft.Row([gpu_status_icon, gpu_status_text], spacing=8)

    gpu_checkbox = ft.Checkbox(
        label="Używaj akceleracji karty graficznej (GPU - znacznie szybsza analiza)",
        value=config.use_gpu,
        disabled=not gpu_info.has_gpu,
    )

    def on_gpu_change(_: Any) -> None:
        config.use_gpu = bool(gpu_checkbox.value)
        save_config(config)

    gpu_checkbox.on_change = on_gpu_change

    # LLM settings
    engine_dropdown = ft.Dropdown(
        label="Silnik LLM",
        value=config.engine,
        options=[
            ft.dropdown.Option(
                key="llama_cpp", text="Wbudowany standalone llama-server (llama.cpp) - brak wymogów"
            ),
            ft.dropdown.Option(key="ollama", text="Lokalny Ollama (http://127.0.0.1:11434)"),
        ],
    )

    model_dropdown = ft.Dropdown(
        label="Wybierz model AI",
        value=config.selected_model_id,
        options=[
            ft.dropdown.Option(key=m.id, text=f"{m.name} ({m.size_mb} MB)")
            for m in AVAILABLE_MODELS
        ],
        expand=True,
    )

    model_desc_text = ft.Text(
        get_model_by_id(config.selected_model_id).description,
        size=12,
        italic=True,
        color=ft.Colors.BLUE_GREY_700,
    )

    def on_model_select(_: Any) -> None:
        config.selected_model_id = str(model_dropdown.value or "qwen2.5-3b-instruct")
        save_config(config)
        model_desc_text.value = get_model_by_id(config.selected_model_id).description
        page.update()

    model_dropdown.on_select = on_model_select

    def on_engine_select(_: Any) -> None:
        config.engine = (
            "ollama" if str(engine_dropdown.value or "").startswith("ollama") else "llama_cpp"
        )
        save_config(config)
        page.update()

    engine_dropdown.on_select = on_engine_select

    models_dir_field = ft.TextField(
        label="Katalog na pobrane modele i silnik",
        value=config.models_dir,
        expand=True,
    )

    async def on_pick_models_dir(_: Any) -> None:
        chosen_dir = await file_picker.get_directory_path(
            dialog_title="Wybierz folder zapisu modeli"
        )
        if chosen_dir:
            config.models_dir = chosen_dir
            save_config(config)
            models_dir_field.value = chosen_dir
            page.update()

    pick_dir_button = ft.ElevatedButton(
        "Zmień folder",
        icon=ft.Icons.FOLDER_OPEN,
        on_click=on_pick_models_dir,
    )

    auto_eval_checkbox = ft.Checkbox(
        label="Automatycznie oceniaj oferty w tle po zakończeniu skanowania",
        value=config.auto_evaluate,
    )

    def on_auto_eval_change(_: Any) -> None:
        config.auto_evaluate = bool(auto_eval_checkbox.value)
        save_config(config)

    auto_eval_checkbox.on_change = on_auto_eval_change

    download_progress = ft.ProgressBar(value=0.0, visible=False)
    progress_status_text = ft.Text("", size=13)
    download_button = ft.ElevatedButton(
        "Pobierz i uruchom model",
        icon=ft.Icons.DOWNLOAD,
    )

    async def on_download_click(_: Any) -> None:
        download_button.disabled = True
        download_progress.visible = True
        download_progress.value = None
        progress_status_text.value = "Inicjalizacja pobierania..."
        page.update()

        def progress_callback(fraction: float, message: str) -> None:
            download_progress.value = fraction if fraction > 0 else None
            progress_status_text.value = message
            page.update()

        try:
            success = await evaluation_service.prepare_backend(progress_callback=progress_callback)
            if success:
                progress_status_text.value = "Silnik LLM i model są gotowe do analizy CV!"
                progress_status_text.color = ft.Colors.GREEN_700
            else:
                progress_status_text.value = "Nie udało się przygotować wybranego modelu."
                progress_status_text.color = ft.Colors.RED_700
        except Exception as d_err:
            logger.exception("Model preparation failed")
            progress_status_text.value = f"Błąd przygotowywania modelu: {d_err}"
            progress_status_text.color = ft.Colors.RED_700
        finally:
            download_button.disabled = False
            download_progress.visible = False
            page.update()

    download_button.on_click = on_download_click

    test_conn_button = ft.OutlinedButton("Testuj gotowość LLM", icon=ft.Icons.CHECK)

    async def on_test_click(_: Any) -> None:
        test_conn_button.disabled = True
        progress_status_text.value = "Sprawdzanie połączenia..."
        page.update()
        ready = await evaluation_service.is_ready()
        if ready:
            progress_status_text.value = "Silnik LLM jest aktywny i odpowiada!"
            progress_status_text.color = ft.Colors.GREEN_700
        else:
            progress_status_text.value = (
                "Silnik LLM nie odpowiada. Kliknij 'Pobierz i uruchom model' lub uruchom Ollama."
            )
            progress_status_text.color = ft.Colors.RED_700
        test_conn_button.disabled = False
        page.update()

    test_conn_button.on_click = on_test_click

    llm_settings_view = ft.Column(
        controls=[
            ft.Card(
                content=ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "1. Twój profil kandydata (CV)",
                                size=16,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Row([cv_path_field, pick_cv_button], spacing=10),
                            cv_status_text,
                        ],
                        spacing=8,
                    ),
                    padding=16,
                )
            ),
            ft.Card(
                content=ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "2. Lokalny silnik AI i Model",
                                size=16,
                                weight=ft.FontWeight.BOLD,
                            ),
                            gpu_status_row,
                            gpu_checkbox,
                            engine_dropdown,
                            ft.Row([model_dropdown], spacing=10),
                            model_desc_text,
                            ft.Row([models_dir_field, pick_dir_button], spacing=10),
                            auto_eval_checkbox,
                            ft.Row([download_button, test_conn_button], spacing=10),
                            download_progress,
                            progress_status_text,
                        ],
                        spacing=10,
                    ),
                    padding=16,
                )
            ),
        ],
        spacing=16,
        scroll=ft.ScrollMode.ADAPTIVE,
        expand=True,
    )

    # Sources View
    sources_view = ft.Column(
        controls=[
            ft.Text("Wspierane portale ogłoszeń o pracę", size=18, weight=ft.FontWeight.BOLD),
            *[
                ft.Card(
                    content=ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.CHECK_CIRCLE
                                    if source_def.status == "active"
                                    else ft.Icons.HOURGLASS_EMPTY,
                                    color=ft.Colors.GREEN
                                    if source_def.status == "active"
                                    else ft.Colors.GREY,
                                ),
                                ft.Text(
                                    f"{source_def.name} [{source_def.status.upper()}]",
                                    weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=10,
                        ),
                        padding=12,
                    )
                )
                for source_def in SOURCE_DEFINITIONS
            ],
        ],
        spacing=10,
        scroll=ft.ScrollMode.ADAPTIVE,
        expand=True,
    )

    content_area = ft.Container(content=scanner_view, expand=True)

    # Navigation buttons with dynamic active state highlighting
    tab_scanner_btn = ft.ElevatedButton("Skaner & Oferty", icon=ft.Icons.SEARCH)
    tab_llm_btn = ft.ElevatedButton("Konfiguracja LLM & CV", icon=ft.Icons.PSYCHOLOGY)
    tab_sources_btn = ft.ElevatedButton("Źródła", icon=ft.Icons.LANGUAGE)

    def set_active_tab(tab_name: str) -> None:
        """Switch views and update header button colors to highlight the active tab."""
        is_scanner = tab_name == "scanner"
        is_llm = tab_name == "llm"
        is_sources = tab_name == "sources"

        tab_scanner_btn.bgcolor = ft.Colors.BLUE_700 if is_scanner else ft.Colors.GREY_200
        tab_scanner_btn.color = ft.Colors.WHITE if is_scanner else ft.Colors.BLACK87
        tab_scanner_btn.elevation = 3 if is_scanner else 0

        tab_llm_btn.bgcolor = ft.Colors.BLUE_700 if is_llm else ft.Colors.GREY_200
        tab_llm_btn.color = ft.Colors.WHITE if is_llm else ft.Colors.BLACK87
        tab_llm_btn.elevation = 3 if is_llm else 0

        tab_sources_btn.bgcolor = ft.Colors.BLUE_700 if is_sources else ft.Colors.GREY_200
        tab_sources_btn.color = ft.Colors.WHITE if is_sources else ft.Colors.BLACK87
        tab_sources_btn.elevation = 3 if is_sources else 0

        if is_scanner:
            content_area.content = scanner_view
        elif is_llm:
            content_area.content = llm_settings_view
        elif is_sources:
            content_area.content = sources_view

        page.update()

    tab_scanner_btn.on_click = lambda _: set_active_tab("scanner")
    tab_llm_btn.on_click = lambda _: set_active_tab("llm")
    tab_sources_btn.on_click = lambda _: set_active_tab("sources")

    # Initialize active styling on Scanner tab
    set_active_tab("scanner")

    def on_page_close(_: Any) -> None:
        logger.info("Application is closing, shutting down workers and server")
        stop_event.set()
        scan_executor.shutdown(wait=False, cancel_futures=True)
        evaluation_service.shutdown()

    page.on_close = on_page_close
    page.on_disconnect = on_page_close

    return ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Text("Job Sniffer", size=26, weight=ft.FontWeight.BOLD),
                    ft.Row([tab_scanner_btn, tab_llm_btn, tab_sources_btn], spacing=10),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Divider(),
            content_area,
        ],
        spacing=10,
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
