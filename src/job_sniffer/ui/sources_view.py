import flet as ft

from job_sniffer.sources.registry import SOURCE_DEFINITIONS, SourceDefinition
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
                *[_source_card(source_def) for source_def in SOURCE_DEFINITIONS],
            ],
            spacing=10,
            scroll=ft.ScrollMode.ADAPTIVE,
            expand=True,
        )


def _source_card(source_def: SourceDefinition) -> ft.Card:
    leading = source_icon_image(source_def, size=24) or ft.Container(width=24)
    return ft.Card(
        content=ft.Container(
            content=ft.Row(
                [
                    leading,
                    ft.Text(
                        f"{source_def.name} [{source_def.status.upper()}]",
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.Container(expand=True),
                    _status_icon(source_def),
                ],
                spacing=10,
            ),
            padding=12,
        )
    )


def _status_icon(source_def: SourceDefinition) -> ft.Icon:
    if source_def.status == "active":
        return ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN)
    return ft.Icon(ft.Icons.HOURGLASS_EMPTY, color=ft.Colors.GREY)
