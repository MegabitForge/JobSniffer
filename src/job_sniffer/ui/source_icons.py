from importlib import resources

import flet as ft

_ICON_CACHE: dict[str, bytes | None] = {}


def source_icon_image(source_key: str, *, size: int = 20) -> ft.Image | None:
    """Return a logo image for the source, or None when no icon is bundled."""
    icon_bytes = _load_icon(source_key)
    if icon_bytes is None:
        return None
    return ft.Image(
        src=icon_bytes,
        width=size,
        height=size,
        fit=ft.BoxFit.CONTAIN,
    )


def _load_icon(source_key: str) -> bytes | None:
    if source_key in _ICON_CACHE:
        return _ICON_CACHE[source_key]
    try:
        icon_bytes = (
            resources.files("job_sniffer")
            .joinpath("assets", "icons", f"{source_key}.png")
            .read_bytes()
        )
    except OSError:
        icon_bytes = None
    _ICON_CACHE[source_key] = icon_bytes
    return icon_bytes
