"""Candidate CV management plus scan profiles with per-source filters."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import flet as ft
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_sniffer.config import AppConfig, save_config
from job_sniffer.database import (
    DB_PATH,
    ProfileRecord,
    ProfileSourceConfig,
    connect,
    create_profile,
    delete_profile,
    get_profile,
    get_profile_source_settings,
    init_db,
    list_profiles,
    save_profile_source_settings,
    update_profile,
)
from job_sniffer.services import EvaluationService
from job_sniffer.sources.filters import SourceFilter, get_source_filter_schema
from job_sniffer.sources.registry import SOURCE_DEFINITIONS, SourceDefinition
from job_sniffer.ui.source_icons import source_icon_image

ACTIVE_SOURCES = tuple(
    source_def for source_def in SOURCE_DEFINITIONS if source_def.status == "active"
)

_MAX_SUMMARY_LENGTH = 80


class _DbError(Exception):
    """Internal marker for a failed database operation."""


class ProfilesView:
    """Candidate profile management: global CV plus scan profiles."""

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
        self.on_profiles_changed: Callable[[], None] | None = None

        self._selected_profile_id: int | None = None
        self._source_switches: dict[str, ft.Switch] = {}
        self._source_text_fields: dict[str, dict[str, ft.TextField]] = {}
        self._source_chips: dict[str, dict[str, list[ft.Chip]]] = {}
        self._name_field: ft.TextField | None = None
        self._limit_field: ft.TextField | None = None

        self._status_text = ft.Text("")
        self._profiles_container = ft.Container()

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

        self._render(update=False)

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
                ft.Card(
                    content=ft.Container(
                        content=ft.Column(
                            controls=[self._profiles_container, self._status_text],
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

    @contextmanager
    def _session(self) -> Iterator[Session]:
        try:
            session = connect(DB_PATH)
        except SQLAlchemyError as error:
            self._set_status(f"Błąd bazy danych: {error}", error=True)
            raise _DbError from error
        try:
            init_db(session)
            yield session
        except SQLAlchemyError as error:
            session.rollback()
            self._set_status(f"Błąd bazy danych: {error}", error=True)
            raise _DbError from error
        finally:
            session.close()

    def _render(self, *, update: bool = True) -> None:
        try:
            with self._session() as session:
                profile = (
                    get_profile(session, self._selected_profile_id)
                    if self._selected_profile_id is not None
                    else None
                )
                if profile is None:
                    self._selected_profile_id = None
                    profiles = [
                        (record, get_profile_source_settings(session, record.id))
                        for record in list_profiles(session)
                    ]
                    content = self._build_list_content(profiles)
                else:
                    settings = get_profile_source_settings(session, profile.id)
                    content = self._build_detail_content(profile, settings)
        except _DbError:
            content = ft.Text(
                "Nie udało się wczytać profili z bazy danych.",
                color=ft.Colors.RED_700,
            )
        self._profiles_container.content = content
        if update:
            self._page.update()

    def _build_list_content(
        self,
        profiles: list[tuple[ProfileRecord, dict[str, ProfileSourceConfig]]],
    ) -> ft.Control:
        if profiles:
            body: ft.Control = ft.Column(
                controls=[self._profile_card(profile, settings) for profile, settings in profiles],
                spacing=8,
            )
        else:
            body = ft.Text("Brak profili. Dodaj pierwszy profil, aby skonfigurować filtry źródeł.")
        return ft.Column(
            controls=[
                ft.Row(
                    [
                        ft.Text(
                            "Profile skanowania",
                            size=16,
                            weight=ft.FontWeight.BOLD,
                            expand=True,
                        ),
                        ft.ElevatedButton(
                            "Dodaj profil",
                            icon=ft.Icons.ADD,
                            on_click=self._on_add_profile_click,
                        ),
                    ],
                    spacing=10,
                ),
                body,
            ],
            spacing=10,
        )

    def _build_detail_content(
        self, profile: ProfileRecord, settings: dict[str, ProfileSourceConfig]
    ) -> ft.Control:
        self._name_field = ft.TextField(label="Nazwa profilu", value=profile.name)
        self._limit_field = ft.TextField(
            label="Limit ofert",
            value=str(profile.offer_limit) if profile.offer_limit is not None else "",
            keyboard_type=ft.KeyboardType.NUMBER,
            hint_text="Puste = brak limitu",
        )
        self._source_switches = {}
        self._source_text_fields = {}
        self._source_chips = {}

        return ft.Column(
            controls=[
                ft.OutlinedButton(
                    "← Wróć do listy profili",
                    icon=ft.Icons.ARROW_BACK,
                    on_click=self._on_back_click,
                ),
                self._name_field,
                self._limit_field,
                ft.Text("Źródła i filtry", size=16, weight=ft.FontWeight.BOLD),
                *[
                    self._build_source_card(source_def, settings.get(source_def.key))
                    for source_def in ACTIVE_SOURCES
                ],
                ft.ElevatedButton(
                    "Zapisz zmiany",
                    icon=ft.Icons.SAVE,
                    on_click=self._on_save_click,
                ),
            ],
            spacing=10,
        )

    def _build_source_card(
        self, source_def: SourceDefinition, stored: ProfileSourceConfig | None
    ) -> ft.Card:
        switch = ft.Switch(value=stored.enabled if stored else True)
        self._source_switches[source_def.key] = switch
        self._source_text_fields[source_def.key] = {}
        self._source_chips[source_def.key] = {}

        schema = get_source_filter_schema(source_def.key)
        filter_controls: list[ft.Control] = []
        for source_filter in schema:
            filter_controls.append(
                self._build_filter_control(source_def.key, source_filter, stored)
            )

        return ft.Card(
            content=ft.ExpansionTile(
                leading=source_icon_image(source_def.key) or ft.Icon(ft.Icons.LANGUAGE),
                title=ft.Row(
                    [
                        ft.Text(source_def.name, weight=ft.FontWeight.BOLD),
                        ft.Container(expand=True),
                        switch,
                    ],
                    spacing=10,
                ),
                subtitle=ft.Text(
                    _filters_summary(stored, schema),
                    size=12,
                ),
                controls=[
                    ft.Column(
                        controls=filter_controls,
                        spacing=10,
                    ),
                ],
                maintain_state=True,
                controls_padding=ft.Padding(left=8, top=4, right=8, bottom=8),
            )
        )

    def _build_filter_control(
        self,
        source_key: str,
        source_filter: SourceFilter,
        stored: ProfileSourceConfig | None,
    ) -> ft.Control:
        stored_value = stored.filters.get(source_filter.key) if stored else None
        if source_filter.kind in ("text", "multi_text"):
            if isinstance(stored_value, list):
                value = ", ".join(stored_value)
            elif isinstance(stored_value, str):
                value = stored_value
            else:
                value = ""
            field = ft.TextField(
                value=value,
                hint_text=source_filter.hint or None,
            )
            self._source_text_fields[source_key][source_filter.key] = field
            control: ft.Control = field
        else:
            checked = stored_value if isinstance(stored_value, list) else []
            chips = [
                ft.Chip(
                    label=option.label,
                    selected=option.value in checked,
                    data=option.value,
                    on_select=lambda _e: None,
                )
                for option in source_filter.options
            ]
            self._source_chips[source_key][source_filter.key] = chips
            control = ft.Row(
                controls=cast("list[ft.Control]", chips),
                wrap=True,
                spacing=10,
                run_spacing=4,
            )
        return ft.Column(
            controls=[
                ft.Text(source_filter.label, size=13, weight=ft.FontWeight.BOLD),
                control,
            ],
            spacing=4,
        )

    def _profile_card(
        self, profile: ProfileRecord, settings: dict[str, ProfileSourceConfig]
    ) -> ft.Card:
        enabled_names = [
            source_def.name
            for source_def in ACTIVE_SOURCES
            if _source_enabled(settings, source_def.key)
        ]
        subtitle = ", ".join(enabled_names) if enabled_names else "brak włączonych źródeł"
        return ft.Card(
            content=ft.ListTile(
                leading=ft.Icon(ft.Icons.PERSON_OUTLINE),
                title=ft.Text(profile.name, weight=ft.FontWeight.BOLD),
                subtitle=ft.Text(f"Źródła: {subtitle}", size=12),
                trailing=ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE,
                    tooltip="Usuń profil",
                    on_click=lambda _e, profile=profile: self._on_delete_profile_click(profile),
                ),
                on_click=lambda _e, profile=profile: self._open_profile(profile.id),
            )
        )

    def _on_add_profile_click(self, _: Any) -> None:
        name_field = ft.TextField(label="Nazwa profilu", autofocus=True)
        dialog = ft.AlertDialog(
            title=ft.Text("Dodaj profil"),
            content=name_field,
            actions=[
                ft.TextButton("Anuluj", on_click=lambda _e: self._page.pop_dialog()),
                ft.FilledButton(
                    "Dodaj",
                    on_click=lambda _e: self._create_profile_from_dialog(name_field.value or ""),
                ),
            ],
        )
        self._page.show_dialog(dialog)

    def _create_profile_from_dialog(self, name: str) -> None:
        self._page.pop_dialog()
        try:
            with self._session() as session:
                created = create_profile(session, name)
        except _DbError:
            return
        if created is None:
            if name.strip():
                self._set_status("Profil o tej nazwie już istnieje", error=True)
            else:
                self._set_status("Nazwa profilu nie może być pusta", error=True)
        else:
            self._set_status("Dodano profil")
        self._render()
        self._notify_profiles_changed()

    def _on_delete_profile_click(self, profile: ProfileRecord) -> None:
        dialog = ft.AlertDialog(
            title=ft.Text(f"Usunąć profil „{profile.name}”?"),
            actions=[
                ft.TextButton("Anuluj", on_click=lambda _e: self._page.pop_dialog()),
                ft.FilledButton(
                    "Usuń",
                    on_click=lambda _e: self._delete_profile(profile.id),
                ),
            ],
        )
        self._page.show_dialog(dialog)

    def _delete_profile(self, profile_id: int) -> None:
        self._page.pop_dialog()
        try:
            with self._session() as session:
                delete_profile(session, profile_id)
        except _DbError:
            return
        if self._selected_profile_id == profile_id:
            self._selected_profile_id = None
        self._set_status("Usunięto profil")
        self._render()
        self._notify_profiles_changed()

    def _open_profile(self, profile_id: int) -> None:
        self._selected_profile_id = profile_id
        self._render()

    def _on_back_click(self, _: Any) -> None:
        self._selected_profile_id = None
        self._render()

    def _on_save_click(self, _: Any) -> None:
        profile_id = self._selected_profile_id
        name_field = self._name_field
        limit_field = self._limit_field
        if profile_id is None or name_field is None or limit_field is None:
            return
        name = name_field.value or ""
        limit_text = (limit_field.value or "").strip()
        offer_limit: int | None = None
        if limit_text:
            try:
                offer_limit = int(limit_text)
            except ValueError:
                self._set_status("Limit ofert musi być liczbą całkowitą", error=True)
                self._page.update()
                return
        settings = self._collect_source_settings()
        try:
            with self._session() as session:
                updated = update_profile(session, profile_id, name, offer_limit)
                if not updated:
                    if name.strip():
                        self._set_status("Profil o tej nazwie już istnieje", error=True)
                    else:
                        self._set_status("Nazwa profilu nie może być pusta", error=True)
                    self._page.update()
                    return
                save_profile_source_settings(session, profile_id, settings)
        except _DbError:
            self._page.update()
            return
        self._set_status("Zapisano zmiany profilu")
        self._render()
        self._notify_profiles_changed()

    def _collect_source_settings(self) -> list[ProfileSourceConfig]:
        configs: list[ProfileSourceConfig] = []
        for source_def in ACTIVE_SOURCES:
            source_key = source_def.key
            switch = self._source_switches.get(source_key)
            if switch is None:
                continue
            filters: dict[str, str | list[str]] = {}
            for source_filter in get_source_filter_schema(source_key):
                if source_filter.kind in ("text", "multi_text"):
                    field = self._source_text_fields.get(source_key, {}).get(source_filter.key)
                    if field is None or field.value is None:
                        continue
                    stripped = field.value.strip()
                    if not stripped:
                        continue
                    if source_filter.kind == "text":
                        filters[source_filter.key] = stripped
                    else:
                        values = [part.strip() for part in stripped.split(",") if part.strip()]
                        if values:
                            filters[source_filter.key] = values
                else:
                    chips = self._source_chips.get(source_key, {}).get(source_filter.key, [])
                    values = [str(chip.data) for chip in chips if chip.selected and chip.data]
                    if values:
                        filters[source_filter.key] = values
            configs.append(
                ProfileSourceConfig(
                    source_key=source_key,
                    enabled=bool(switch.value),
                    filters=filters,
                )
            )
        return configs

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self._status_text.value = message
        self._status_text.color = ft.Colors.RED_700 if error else None

    def _notify_profiles_changed(self) -> None:
        if self.on_profiles_changed is not None:
            self.on_profiles_changed()


def _source_enabled(settings: dict[str, ProfileSourceConfig], source_key: str) -> bool:
    stored = settings.get(source_key)
    return stored is None or stored.enabled


def _filters_summary(stored: ProfileSourceConfig | None, schema: tuple[SourceFilter, ...]) -> str:
    if stored is None or not stored.filters:
        return "Brak filtrów"
    parts: list[str] = []
    for source_filter in schema:
        value = stored.filters.get(source_filter.key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            parts.append(f"{source_filter.label}: {value.strip()}")
        elif isinstance(value, list) and value:
            display = ", ".join(value)
            parts.append(f"{source_filter.label}: {display}")
    if not parts:
        return "Brak filtrów"
    summary = " · ".join(parts)
    if len(summary) > _MAX_SUMMARY_LENGTH:
        return summary[: _MAX_SUMMARY_LENGTH - 1] + "…"
    return summary
