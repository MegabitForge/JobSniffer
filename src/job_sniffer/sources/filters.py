from dataclasses import dataclass
from typing import Literal

FilterKind = Literal["text", "multi_text", "multi_select"]


@dataclass(frozen=True)
class FilterOption:
    value: str
    label: str


@dataclass(frozen=True)
class SourceFilter:
    key: str
    label: str
    kind: FilterKind
    hint: str = ""
    options: tuple[FilterOption, ...] = ()

    @property
    def is_text(self) -> bool:
        """Whether the filter is edited as free text (single or comma-separated values)."""
        return self.kind in ("text", "multi_text")


KEYWORDS_FILTER = SourceFilter(key="keywords", label="Słowa kluczowe", kind="text")
LOCATION_FILTER = SourceFilter(key="location", label="Lokalizacja", kind="text")


def parse_text_filter_value(kind: FilterKind, raw: str) -> str | list[str] | None:
    """Parse field text into a stored filter value; None means "no filter"."""
    stripped = raw.strip()
    if not stripped:
        return None
    if kind == "text":
        return stripped
    values = [part.strip() for part in stripped.split(",") if part.strip()]
    return values or None
