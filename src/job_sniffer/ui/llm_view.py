import logging
from typing import Any

import flet as ft

from job_sniffer.config import AppConfig, save_config
from job_sniffer.llm.catalog import AVAILABLE_MODELS, get_model_by_id
from job_sniffer.llm.hardware import detect_gpu, format_gpu_summary
from job_sniffer.services import EvaluationService

logger = logging.getLogger(__name__)


class LlmView:
    """Local AI engine configuration: GPU, engine, model, download and readiness."""

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
        self._gpu_checkbox = gpu_checkbox
        gpu_checkbox.on_change = self._on_gpu_change

        self._engine_dropdown = ft.Dropdown(
            label="Silnik LLM",
            value=config.engine,
            options=[
                ft.dropdown.Option(
                    key="llama_cpp",
                    text="Wbudowany standalone llama-server (llama.cpp) - brak wymogów",
                ),
                ft.dropdown.Option(
                    key="ollama",
                    text="Lokalny Ollama (http://127.0.0.1:11434)",
                    disabled=True,
                ),
            ],
        )
        self._engine_dropdown.on_select = self._on_engine_select

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
        self._model_desc_text = model_desc_text
        self._model_dropdown = model_dropdown
        model_dropdown.on_select = self._on_model_select

        self._models_dir_field = ft.TextField(
            label="Katalog na pobrane modele i silnik",
            value=config.models_dir,
            expand=True,
        )
        pick_dir_button = ft.ElevatedButton(
            "Zmień folder",
            icon=ft.Icons.FOLDER_OPEN,
            on_click=self._on_pick_models_dir,
        )

        auto_eval_checkbox = ft.Checkbox(
            label="Automatycznie oceniaj oferty w tle po zakończeniu skanowania",
            value=config.auto_evaluate,
        )
        self._auto_eval_checkbox = auto_eval_checkbox
        auto_eval_checkbox.on_change = self._on_auto_eval_change

        self._download_progress = ft.ProgressBar(value=0.0, visible=False)
        self._progress_status_text = ft.Text("", size=13)
        self._download_button = ft.ElevatedButton(
            "Pobierz i uruchom model",
            icon=ft.Icons.DOWNLOAD,
        )
        self._download_button.on_click = self._on_download_click
        self._test_conn_button = ft.OutlinedButton("Testuj gotowość LLM", icon=ft.Icons.CHECK)
        self._test_conn_button.on_click = self._on_test_click

        self.control = ft.Column(
            controls=[
                ft.Card(
                    content=ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "Lokalny silnik AI i Model",
                                    size=16,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                gpu_status_row,
                                gpu_checkbox,
                                self._engine_dropdown,
                                ft.Row([model_dropdown], spacing=10),
                                model_desc_text,
                                ft.Row([self._models_dir_field, pick_dir_button], spacing=10),
                                auto_eval_checkbox,
                                ft.Row([self._download_button, self._test_conn_button], spacing=10),
                                self._download_progress,
                                self._progress_status_text,
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

    def _on_gpu_change(self, _: Any) -> None:
        self._config.use_gpu = bool(self._gpu_checkbox.value)
        save_config(self._config)

    def _on_engine_select(self, _: Any) -> None:
        self._config.engine = (
            "ollama" if str(self._engine_dropdown.value or "").startswith("ollama") else "llama_cpp"
        )
        save_config(self._config)
        self._page.update()

    def _on_model_select(self, _: Any) -> None:
        self._config.selected_model_id = str(self._model_dropdown.value or "qwen2.5-3b-instruct")
        save_config(self._config)
        self._model_desc_text.value = get_model_by_id(self._config.selected_model_id).description
        self._page.update()

    async def _on_pick_models_dir(self, _: Any) -> None:
        chosen_dir = await self._file_picker.get_directory_path(
            dialog_title="Wybierz folder zapisu modeli"
        )
        if chosen_dir:
            self._config.models_dir = chosen_dir
            save_config(self._config)
            self._models_dir_field.value = chosen_dir
            self._page.update()

    def _on_auto_eval_change(self, _: Any) -> None:
        self._config.auto_evaluate = bool(self._auto_eval_checkbox.value)
        save_config(self._config)

    async def _on_download_click(self, _: Any) -> None:
        self._download_button.disabled = True
        self._download_progress.visible = True
        self._download_progress.value = None
        self._progress_status_text.value = "Inicjalizacja pobierania..."
        self._page.update()

        def progress_callback(fraction: float, message: str) -> None:
            self._download_progress.value = fraction if fraction > 0 else None
            self._progress_status_text.value = message
            self._page.update()

        try:
            success = await self._evaluation_service.prepare_backend(
                progress_callback=progress_callback
            )
            if success:
                self._progress_status_text.value = "Silnik LLM i model są gotowe do analizy CV!"
                self._progress_status_text.color = ft.Colors.GREEN_700
            else:
                self._progress_status_text.value = "Nie udało się przygotować wybranego modelu."
                self._progress_status_text.color = ft.Colors.RED_700
        except Exception as d_err:
            logger.exception("Model preparation failed")
            self._progress_status_text.value = f"Błąd przygotowywania modelu: {d_err}"
            self._progress_status_text.color = ft.Colors.RED_700
        finally:
            self._download_button.disabled = False
            self._download_progress.visible = False
            self._page.update()

    async def _on_test_click(self, _: Any) -> None:
        self._test_conn_button.disabled = True
        self._progress_status_text.value = "Sprawdzanie połączenia..."
        self._page.update()
        ready = await self._evaluation_service.is_ready()
        if ready:
            self._progress_status_text.value = "Silnik LLM jest aktywny i odpowiada!"
            self._progress_status_text.color = ft.Colors.GREEN_700
        else:
            self._progress_status_text.value = (
                "Silnik LLM nie odpowiada. Kliknij 'Pobierz i uruchom model' lub uruchom Ollama."
            )
            self._progress_status_text.color = ft.Colors.RED_700
        self._test_conn_button.disabled = False
        self._page.update()
