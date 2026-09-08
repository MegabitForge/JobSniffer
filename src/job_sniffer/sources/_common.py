from __future__ import annotations

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
)


def is_poland_location(location: str) -> bool:
    """Return true when a search location means all of Poland."""
    normalized = location.casefold().strip()
    return not normalized or normalized in {"poland", "polska"}
