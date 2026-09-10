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
    init_db,
    list_offers_with_evaluations,
    offer_exists,
    save_offer,
)
from job_sniffer.llm.catalog import AVAILABLE_MODELS, get_model_by_id
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
    """Build the application UI shell with Scanner, LLM/CV Configuration, and Sources tabs."""
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

    def on_source_change(_: Any) -> None:
        bulldogjob_selected = source.value == "bulldogjob"
        keywords.disabled = bulldogjob_selected
        keywords.hint_text = "Niewspierane przez Bulldogjob" if bulldogjob_selected else None
        page.update()

    source.on_select = on_source_change

    # Offer card rendering
    def build_offer_card(
        title: str,
        company: str,
        offer_location: str | None,
        salary: str | None,
        url: str | None,
        source_name: str | None = None,
        evaluation: JobOfferEvaluation | None = None,
    ) -> ft.Card:
        details_row = ft.Row(
            controls=[
                ft.Text(f"🏢 {company}", weight=ft.FontWeight.BOLD),
                ft.Text(f"📍 {offer_location or 'Zdalnie / Nie podano'}"),
                ft.Text(f"💰 {salary or 'Nie podano widełek'}", color=ft.Colors.BLUE_GREY_700),
            ],
            wrap=True,
            spacing=16,
        )

        url_button: ft.Control = ft.Container()
        if url:
            url_button = ft.TextButton(
                "Otwórz ofertę w przeglądarce 🔗",
                url=url,
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
                            f"Szansa: {evaluation.fit_score}% | {evaluation.verdict}",
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
                                "Mocne strony kandydata względem oferty:",
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
                                "Luki / brakujące kompetencje:",
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
        else:
            eval_controls.append(
                ft.Text(
                    "Brak oceny dopasowania (Upewnij się, że CV i silnik LLM są skonfigurowane w zakładce 'Konfiguracja LLM & CV').",
                    size=12,
                    color=ft.Colors.GREY_600,
                    italic=True,
                )
            )

        return ft.Card(
            content=ft.Container(
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
                        url_button,
                        ft.Divider(),
                        *eval_controls,
                    ],
                    spacing=8,
                ),
                padding=14,
            ),
        )

    def load_saved_offers_into_ui() -> None:
        """Load recent saved offers and evaluations from SQLite into the results list."""
        try:
            with connect(Path("job_sniffer.sqlite")) as session:
                init_db(session)
                rows = list_offers_with_evaluations(session, limit=25)
                offers_column.controls.clear()
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
                    offers_column.controls.append(
                        build_offer_card(
                            title=offer_rec.title,
                            company=offer_rec.company,
                            offer_location=offer_rec.location,
                            salary=offer_rec.salary,
                            url=offer_rec.url,
                            source_name=offer_rec.source,
                            evaluation=evaluation,
                        )
                    )
        except (SQLAlchemyError, OSError) as err:
            logger.warning("Failed to preload existing offers: %s", err)

    load_saved_offers_into_ui()

    # Scanning logic
    def _scan_source(
        source_key: str, search: JobSearch
    ) -> tuple[list[tuple[JobOffer, JobOfferEvaluation | None]], SaveStats]:
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

            saved_results: list[tuple[JobOffer, JobOfferEvaluation | None]] = []
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
                    evaluation: JobOfferEvaluation | None = None
                    if config.auto_evaluate:
                        try:
                            # Evaluate offer against candidate CV using LLM
                            evaluation = asyncio.run(
                                evaluation_service.evaluate_offer(offer, inserted_id, session)
                            )
                        except (SQLAlchemyError, RuntimeError, ValueError) as e_err:
                            logger.warning("Automatic evaluation failed: %s", e_err)

                    saved_results.append((offer, evaluation))
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
        offers_column.controls.clear()
        page.update()

        search = JobSearch(keywords=keywords_value, location=location_value, limit=limit_value)
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
            offers_column.controls.clear()
            for offer_obj, eval_obj in results:
                offers_column.controls.append(
                    build_offer_card(
                        title=offer_obj.title,
                        company=offer_obj.company,
                        offer_location=offer_obj.location,
                        salary=offer_obj.salary,
                        url=offer_obj.url,
                        source_name=offer_obj.source,
                        evaluation=eval_obj,
                    )
                )
            if not offers_column.controls:
                offers_column.controls.append(ft.Text("Nie znaleziono żadnych nowych ofert."))
        finally:
            scan_button.disabled = False
            page.update()

    scan_button.on_click = on_scan_click

    def on_refresh_click(_: Any) -> None:
        load_saved_offers_into_ui()
        page.update()

    refresh_button = ft.OutlinedButton(
        "Odśwież zapisane oferty",
        icon=ft.Icons.REFRESH,
        on_click=on_refresh_click,
    )

    # Scanner View
    scanner_view = ft.Column(
        controls=[
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row([keywords, location], spacing=12),
                        ft.Row([limit, source], spacing=12),
                        ft.Row([scan_button, refresh_button, scan_status], spacing=12),
                    ],
                    spacing=10,
                ),
                border_radius=10,
                padding=12,
            ),
            ft.Text("Oferty pracy i ocena dopasowania CV:", size=16, weight=ft.FontWeight.BOLD),
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
        label="Automatycznie oceniaj oferty w tle podczas skanowania",
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

    tab_scanner_btn = ft.FilledButton("Skaner & Oferty", icon=ft.Icons.SEARCH)
    tab_llm_btn = ft.OutlinedButton("Konfiguracja LLM & CV", icon=ft.Icons.PSYCHOLOGY)
    tab_sources_btn = ft.OutlinedButton("Źródła", icon=ft.Icons.LANGUAGE)

    def switch_to_scanner(_: Any) -> None:
        content_area.content = scanner_view
        page.update()

    def switch_to_llm(_: Any) -> None:
        content_area.content = llm_settings_view
        page.update()

    def switch_to_sources(_: Any) -> None:
        content_area.content = sources_view
        page.update()

    tab_scanner_btn.on_click = switch_to_scanner
    tab_llm_btn.on_click = switch_to_llm
    tab_sources_btn.on_click = switch_to_sources

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
