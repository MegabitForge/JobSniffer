from collections.abc import Callable
from dataclasses import dataclass

import flet as ft


@dataclass(frozen=True)
class TabDefinition:
    """Describes one tab visible in the sidebar navigation."""

    key: str
    icon: ft.IconData
    label: str


TAB_DEFINITIONS: tuple[TabDefinition, ...] = (
    TabDefinition(key="scan", icon=ft.Icons.RADAR, label="Scan"),
    TabDefinition(key="offers", icon=ft.Icons.WORK, label="Offers"),
    TabDefinition(key="profiles", icon=ft.Icons.PERSON, label="Profiles"),
    TabDefinition(key="sources", icon=ft.Icons.LANGUAGE, label="Sources"),
    TabDefinition(key="llm", icon=ft.Icons.PSYCHOLOGY, label="LLM"),
)


class Sidebar:
    """Left sidebar with full-button tab navigation and active-item highlight."""

    def __init__(self, page: ft.Page) -> None:
        self._page = page
        self._on_tab_change: Callable[[int], None] = lambda _index: None

        self._buttons: list[ft.Container] = []
        self._icons: list[ft.Icon] = []
        self._texts: list[ft.Text] = []

        nav_column = ft.Column(
            controls=[
                ft.Container(
                    content=ft.Text("Job Sniffer", size=20, weight=ft.FontWeight.BOLD),
                    padding=ft.Padding(14, 12, 14, 12),
                ),
                *[self._make_nav_item(index, tab) for index, tab in enumerate(TAB_DEFINITIONS)],
            ],
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        self.control = ft.Container(content=nav_column, width=200, padding=ft.Padding(4, 8, 4, 8))
        self._update_highlight(0)

    def set_on_tab_change(self, callback: Callable[[int], None]) -> None:
        """Register the callback fired with the tab index after each selection."""
        self._on_tab_change = callback

    def select_tab(self, index: int) -> None:
        """Select a tab programmatically or from a click."""
        index = max(0, min(index, len(TAB_DEFINITIONS) - 1))
        self._update_highlight(index)
        self._on_tab_change(index)
        self._page.update()

    def _make_nav_item(self, index: int, tab: TabDefinition) -> ft.Container:
        """Create one full-width clickable tab button with icon and label."""
        nav_icon = ft.Icon(tab.icon, size=22)
        nav_text = ft.Text(tab.label, size=14)
        button = ft.Container(
            content=ft.Row(controls=[nav_icon, nav_text], spacing=12),
            padding=ft.Padding(14, 10, 14, 10),
            border_radius=10,
            ink=True,
            on_click=lambda _, item_index=index: self.select_tab(item_index),
        )
        self._buttons.append(button)
        self._icons.append(nav_icon)
        self._texts.append(nav_text)
        return button

    def _update_highlight(self, active_index: int) -> None:
        """Highlight the whole active tab button and de-emphasize the others."""
        for item_index, button in enumerate(self._buttons):
            is_active = item_index == active_index
            button.bgcolor = ft.Colors.SECONDARY_CONTAINER if is_active else ft.Colors.TRANSPARENT
            self._icons[item_index].color = (
                ft.Colors.PRIMARY if is_active else ft.Colors.ON_SURFACE_VARIANT
            )
            self._texts[item_index].weight = (
                ft.FontWeight.BOLD if is_active else ft.FontWeight.NORMAL
            )
