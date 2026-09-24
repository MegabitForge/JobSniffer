from importlib import resources

import flet as ft

from job_sniffer.sources.registry import SourceDefinition

_ICON_CACHE: dict[str, bytes | None] = {}


def source_icon_image(source_def: SourceDefinition, *, size: int = 20) -> ft.Image | None:
    """Return a logo image for the source, or None when no icon is bundled."""
    icon_bytes = _load_icon(source_def.icon)
    if icon_bytes is None:
        return None
    return ft.Image(
        src=icon_bytes,
        width=size,
        height=size,
        fit=ft.BoxFit.CONTAIN,
    )


def _load_icon(icon_name: str) -> bytes | None:
    if icon_name in _ICON_CACHE:
        return _ICON_CACHE[icon_name]
    icon_bytes = None
    if icon_name:
        try:
            icon_bytes = (
                resources.files("job_sniffer").joinpath("assets", "icons", icon_name).read_bytes()
            )
        except OSError:
            icon_bytes = None
    _ICON_CACHE[icon_name] = icon_bytes
    return icon_bytes
