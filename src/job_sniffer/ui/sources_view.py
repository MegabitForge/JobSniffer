import flet as ft

from job_sniffer.sources.registry import SOURCE_DEFINITIONS
from job_sniffer.ui.source_icons import source_icon_image


class SourcesView:
    """Static list of supported job sources and their status."""

    def __init__(self) -> None:
        self.control = ft.Column(
            controls=[
                ft.Text(
                    "Wspierane portale ogłoszeń o pracę",
                    size=18,
                    weight=ft.FontWeight.BOLD,
                ),
                *[
                    ft.Card(
                        content=ft.Container(
                            content=ft.Row(
                                [
                                    source_icon_image(source_def.key, size=24)
                                    or ft.Icon(
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
                                    ft.Container(expand=True),
                                    ft.Icon(
                                        ft.Icons.CHECK_CIRCLE
                                        if source_def.status == "active"
                                        else ft.Icons.HOURGLASS_EMPTY,
                                        color=ft.Colors.GREEN
                                        if source_def.status == "active"
                                        else ft.Colors.GREY,
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
