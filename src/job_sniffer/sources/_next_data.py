from __future__ import annotations

import html
import json
from html.parser import HTMLParser
from typing import Any


def extract_next_data(
    html_text: str,
    *,
    missing_message: str,
    type_message: str,
) -> dict[str, Any]:
    """Extract and decode a Next.js __NEXT_DATA__ payload."""
    parser = _NextDataParser()
    parser.feed(html_text)
    if not parser.payload:
        raise ValueError(missing_message)
    parsed = json.loads(html.unescape(parser.payload))
    if not isinstance(parsed, dict):
        raise TypeError(type_message)
    return parsed


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.payload = ""
        self._capture = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attributes = dict(attrs)
        self._capture = attributes.get("id") == "__NEXT_DATA__"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.payload += data
