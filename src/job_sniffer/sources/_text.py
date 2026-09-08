from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from html.parser import HTMLParser
from typing import Any


def clean_text(value: Any) -> str | None:
    """Collapse whitespace in a value converted to text."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def clean_html_text(value: Any) -> str | None:
    """Unescape HTML entities and collapse whitespace."""
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def clean_multiline(value: Any) -> str | None:
    """Normalize whitespace while preserving meaningful line breaks."""
    if value is None:
        return None
    text = html.unescape(str(value)).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized or None


def dedupe_casefold(values: Iterable[str | None]) -> list[str]:
    """Return non-empty strings deduplicated case-insensitively, preserving order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def join_unique(values: Iterable[str | None], *, separator: str = ", ") -> str | None:
    """Join non-empty strings deduplicated case-insensitively."""
    joined = separator.join(dedupe_casefold(values))
    return joined or None


def format_bullet_sections(sections: Sequence[tuple[str, Sequence[str | None]]]) -> str | None:
    """Format section tuples as Markdown-like headings with bullet lists."""
    formatted_sections: list[str] = []
    for title, items in sections:
        clean_title = clean_text(title)
        unique_items = dedupe_casefold(clean_text(item) for item in items)
        if not clean_title or not unique_items:
            continue
        bullets = [f"- {item}" for item in unique_items]
        formatted_sections.append("\n".join([f"## {clean_title}", "", *bullets]))
    return "\n\n".join(formatted_sections) or None


def html_to_text(value: str | None) -> str | None:
    """Strip simple HTML into collapsed plain text."""
    if not value:
        return None
    parser = _TextParser()
    parser.feed(value)
    return clean_text(parser.text())


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self._parts.append(html.unescape(f"&{name};"))

    def text(self) -> str:
        return " ".join(self._parts)
