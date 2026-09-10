from __future__ import annotations

from pathlib import Path

import pytest

from job_sniffer.cv_parser import CVParseError, parse_cv_file


def test_parse_text_cv(tmp_path: Path) -> None:
    cv_file = tmp_path / "cv.txt"
    cv_file.write_text("Jan Kowalski\nPython Developer\nDoświadczenie: 4 lata", encoding="utf-8")
    content = parse_cv_file(cv_file)
    assert "Jan Kowalski" in content
    assert "Python Developer" in content


def test_parse_markdown_cv(tmp_path: Path) -> None:
    cv_file = tmp_path / "cv.md"
    cv_file.write_text("# Jan Kowalski\n## Umiejętności\n- Python\n- Docker", encoding="utf-8")
    content = parse_cv_file(cv_file)
    assert "# Jan Kowalski" in content
    assert "Docker" in content


def test_parse_non_existent_cv(tmp_path: Path) -> None:
    cv_file = tmp_path / "non_existent.pdf"
    with pytest.raises(CVParseError, match="CV file not found"):
        parse_cv_file(cv_file)


def test_parse_unsupported_format(tmp_path: Path) -> None:
    cv_file = tmp_path / "cv.docx"
    cv_file.write_text("docx content", encoding="utf-8")
    with pytest.raises(CVParseError, match="Unsupported CV file extension"):
        parse_cv_file(cv_file)
