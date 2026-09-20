import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import flet as ft

from job_sniffer.config import AppConfig, load_config
from job_sniffer.services import EvaluationService
from job_sniffer.ui.llm_view import LlmView
from job_sniffer.ui.offers_view import OffersView
from job_sniffer.ui.profiles_view import ProfilesView
from job_sniffer.ui.scan_view import ScanView
from job_sniffer.ui.sidebar import TAB_DEFINITIONS, Sidebar
from job_sniffer.ui.sources_view import SourcesView
from job_sniffer.ui.status_bar import StatusBar

logger = logging.getLogger(__name__)


def build_shell(page: ft.Page) -> ft.Control:
    """Build the application shell with a sidebar navigation"""
    config: AppConfig = load_config()
    evaluation_service = EvaluationService(load_config)

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    stop_event = threading.Event()
    scan_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job-sniffer-scan")

    status_bar = StatusBar(page)
    sidebar = Sidebar(page)
    offers_view = OffersView(page, config, evaluation_service, status_bar, stop_event)
    profiles_view = ProfilesView(page, config, evaluation_service, file_picker)
    llm_view = LlmView(page, config, evaluation_service, file_picker)
    sources_view = SourcesView()

    def show_offers_after_scan() -> None:
        """Switch to Offers before rebuilding its mounted control tree."""
        sidebar.select_tab(1)
        offers_view.reload_offers()

    scan_view = ScanView(
        page,
        config,
        evaluation_service,
        stop_event,
        scan_executor,
        offers_view,
        status_bar,
        on_scan_finished=show_offers_after_scan,
    )

    views: dict[str, ft.Control] = {
        "scan": scan_view.control,
        "offers": offers_view.control,
        "profiles": profiles_view.control,
        "sources": sources_view.control,
        "llm": llm_view.control,
    }

    content_area = ft.Container(content=views["scan"], expand=True)

    def switch_content(index: int) -> None:
        """Swap the visible view for the selected tab index."""
        content_area.content = views[TAB_DEFINITIONS[index].key]

    sidebar.set_on_tab_change(switch_content)

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
                    sidebar.control,
                    ft.VerticalDivider(width=1),
                    content_area,
                ],
                spacing=8,
                expand=True,
            ),
            status_bar.control,
        ],
        spacing=4,
        expand=True,
    )
