"""Application entry point."""

import flet as ft

from job_sniffer.ui.shell import build_shell


def main(page: ft.Page) -> None:
    """Configure the main page and render the application shell."""
    page.title = "Job Sniffer"
    page.padding = 24
    page.add(build_shell())


def run() -> None:
    """Start the Flet desktop application."""
    ft.run(main)
