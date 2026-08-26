"""Main application shell."""

import flet as ft


def build_shell() -> ft.Control:
    """Build the initial application view."""
    return ft.Column(
        controls=[
            ft.Text("Job Sniffer", size=32, weight=ft.FontWeight.BOLD),
            ft.Text("The application skeleton is ready."),
        ],
        spacing=8,
    )
