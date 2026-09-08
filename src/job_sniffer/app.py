import logging

import flet as ft

from job_sniffer.ui.shell import build_shell


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def main(page: ft.Page) -> None:
    """Configure the main page and render the application shell."""
    configure_logging()
    page.title = "Job Sniffer"
    page.padding = 24
    page.add(build_shell(page))


def run() -> None:
    """Start the Flet desktop application."""
    ft.run(main)
