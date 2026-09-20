from typing import Any

import flet as ft

from job_sniffer.config import AppConfig, save_config
from job_sniffer.services import EvaluationService


class ProfilesView:
    """Candidate profile management (CV file selection)."""

    def __init__(
        self,
        page: ft.Page,
        config: AppConfig,
        evaluation_service: EvaluationService,
        file_picker: ft.FilePicker,
    ) -> None:
        self._page = page
        self._config = config
        self._evaluation_service = evaluation_service
        self._file_picker = file_picker

        self._cv_path_field = ft.TextField(
            label="Ścieżka do pliku CV",
            value=config.cv_path or "",
            read_only=True,
            expand=True,
        )
        self._cv_status_text = ft.Text(
            "Załadowano CV" if config.cv_path else "Brak wskazanego pliku CV",
            color=ft.Colors.GREEN_700 if config.cv_path else ft.Colors.ORANGE_800,
        )

        pick_cv_button = ft.ElevatedButton(
            "Wybierz plik CV (.pdf, .txt, .md)",
            icon=ft.Icons.ATTACH_FILE,
            on_click=self._on_pick_cv_click,
        )

        self.control = ft.Column(
            controls=[
                ft.Card(
                    content=ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "Twój profil kandydata (CV)",
                                    size=16,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Row([self._cv_path_field, pick_cv_button], spacing=10),
                                self._cv_status_text,
                            ],
                            spacing=8,
                        ),
                        padding=16,
                    )
                ),
            ],
            spacing=16,
            scroll=ft.ScrollMode.ADAPTIVE,
            expand=True,
        )

    async def _on_pick_cv_click(self, _: Any) -> None:
        files = await self._file_picker.pick_files(
            dialog_title="Wybierz plik CV",
            allowed_extensions=["pdf", "txt", "md"],
        )
        if files and files[0].path:
            self._config.cv_path = files[0].path
            save_config(self._config)
            self._cv_path_field.value = self._config.cv_path
            text = self._evaluation_service.get_cv_text(reload=True)
            if text:
                self._cv_status_text.value = f"Załadowano pomyślnie ({len(text)} znaków)"
                self._cv_status_text.color = ft.Colors.GREEN_700
            else:
                self._cv_status_text.value = "Nie udało się odczytać treści pliku CV"
                self._cv_status_text.color = ft.Colors.RED_700
            self._page.update()
