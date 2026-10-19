"""Helpers for schema.org JobPosting JSON-LD embedded in offer detail pages."""

import json
import logging
from typing import Any

from selectolax.parser import HTMLParser

from job_sniffer.sources._text import clean_text

logger = logging.getLogger(__name__)


def extract_job_posting(html_text: str, *, source_label: str) -> dict[str, Any]:
    """Return the first JobPosting object found in the page's JSON-LD scripts."""
    tree = HTMLParser(html_text)
    for node in tree.css('script[type="application/ld+json"]'):
        for item in _iter_json_ld_objects(node.text(), source_label):
            posting = _find_job_posting(item)
            if posting is not None:
                return posting
    raise ValueError(f"{source_label} detail page does not contain JobPosting JSON-LD")


def format_hiring_organization(value: Any) -> str | None:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return clean_text(value)


def format_schema_salary(value: Any) -> str | None:
    """Format a JobPosting ``baseSalary`` as e.g. ``10 000 - 15 000 zł / mies.``."""
    if not isinstance(value, dict):
        return clean_text(value)
    currency = clean_text(value.get("currency")) or clean_text(value.get("salaryCurrency")) or ""
    salary_value = value.get("value")
    if isinstance(salary_value, dict):
        salary_from = salary_value.get("minValue") or salary_value.get("value")
        salary_to = salary_value.get("maxValue")
        unit = clean_text(salary_value.get("unitText")) or clean_text(value.get("unitText")) or ""
    else:
        salary_from = value.get("minValue") or value.get("value")
        salary_to = value.get("maxValue")
        unit = clean_text(value.get("unitText")) or ""
    if salary_from is None and salary_to is None:
        return None
    amount = _format_amount(salary_from) if salary_from is not None else _format_amount(salary_to)
    if salary_from is not None and salary_to is not None:
        amount = f"{_format_amount(salary_from)} - {_format_amount(salary_to)}"
    return " ".join(
        part
        for part in [amount, format_salary_currency(currency), _format_salary_unit(unit)]
        if part
    )


def format_salary_currency(value: str | None) -> str | None:
    if not value:
        return None
    currencies = {"PLN": "zł"}
    return currencies.get(value.upper(), value)


def has_ancestor_tag(node: Any, tag: str) -> bool:
    """Whether a node of a parsed description is nested inside ``tag``."""
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False


def _iter_json_ld_objects(value: str, source_label: str) -> list[Any]:
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.exception("Could not decode %s JSON-LD payload", source_label)
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def _find_job_posting(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        node_type = value.get("@type")
        if node_type == "JobPosting" or (isinstance(node_type, list) and "JobPosting" in node_type):
            return value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if posting := _find_job_posting(item):
                    return posting
    elif isinstance(value, list):
        for item in value:
            if posting := _find_job_posting(item):
                return posting
    return None


def _format_amount(value: Any) -> str:
    if isinstance(value, int | float):
        if float(value).is_integer() and abs(value) >= 1000:
            return f"{int(value):,}".replace(",", " ")
        return f"{value:g}"
    return str(value)


def _format_salary_unit(value: str | None) -> str | None:
    if not value:
        return None
    units = {
        "MONTH": "/ mies.",
        "HOUR": "/ godz.",
        "YEAR": "/ rok",
    }
    return units.get(value.upper(), value)
