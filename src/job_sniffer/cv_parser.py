"""Resume parsing boundary."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf4llm  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class CVParseError(Exception):
    """Raised when parsing a resume file fails."""


def parse_cv_file(path: Path | str) -> str:
    """Parse resume content from a PDF, Markdown, or plain text file.

    Returns the parsed resume text in markdown format.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise CVParseError(f"CV file not found: {file_path}")

    suffix = file_path.suffix.lower()
    logger.info("Parsing CV file: %s (type: %s)", file_path.name, suffix)

    try:
        if suffix == ".pdf":
            text = str(pymupdf4llm.to_markdown(str(file_path)))
            if not text.strip():
                raise CVParseError("Parsed PDF CV is empty or contains only images/scans.")
            return text
        if suffix in (".txt", ".md"):
            return file_path.read_text(encoding="utf-8", errors="replace")
        raise CVParseError(
            f"Unsupported CV file extension: '{suffix}'. Supported formats: .pdf, .txt, .md"
        )
    except CVParseError:
        raise
    except Exception as error:
        logger.exception("Failed to parse CV file: %s", file_path)
        raise CVParseError(f"Error reading CV file: {error}") from error
