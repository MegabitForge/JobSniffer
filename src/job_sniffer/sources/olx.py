from __future__ import annotations

import json
import logging
import random
import re
import threading
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urljoin

import undetected_chromedriver as uc  # type: ignore[import-untyped]
from selectolax.parser import HTMLParser
from selenium.common.exceptions import WebDriverException

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources._common import is_poland_location
from job_sniffer.sources._text import (
    clean_multiline,
    clean_text,
    format_bullet_sections,
    html_to_text,
    join_unique,
)
from job_sniffer.sources.browser import (
    BrowserFetchError,
    fetch_with_existing_browser,
    start_undetected_chrome,
)

logger = logging.getLogger(__name__)


class OlxJobSource:
    source_name = "olx"

    def __init__(
        self,
        *,
        chrome_user_data_dir: str = ".olx-profile",
        timeout_seconds: float = 90.0,
        page_load_timeout_seconds: float = 5.0,
        detail_delay_seconds: tuple[float, float] = (2.0, 4.0),
        stop_event: threading.Event | None = None,
    ) -> None:
        self.chrome_user_data_dir = chrome_user_data_dir
        self.timeout_seconds = timeout_seconds
        self.page_load_timeout_seconds = page_load_timeout_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.stop_event = stop_event

    def search(self, search: JobSearch) -> list[JobOffer]:
        logger.info(
            "Starting OLX search: keywords=%r location=%r limit=%s",
            search.keywords,
            search.location,
            search.limit,
        )
        url = build_search_url(search)
        logger.info("Built OLX search URL: %s", url)
        return self._collect_offers_with_browser(url, limit=search.limit)

    def _collect_offers_with_browser(self, url: str, *, limit: int) -> list[JobOffer]:
        driver = start_undetected_chrome(self.chrome_user_data_dir)
        try:
            html_text = self._fetch_listing_html(driver, url)
            offers = parse_olx_offers(html_text)
            logger.info("Parsed %s OLX offers", len(offers))
            return [self._enrich_offer_from_detail(driver, offer) for offer in offers[:limit]]
        finally:
            logger.info("Closing OLX browser session")
            driver.quit()

    def _fetch_listing_html(self, driver: uc.Chrome, url: str) -> str:
        return fetch_with_existing_browser(
            driver,
            url,
            timeout_seconds=self.timeout_seconds,
            page_load_timeout_seconds=self.page_load_timeout_seconds,
            return_partial_on_timeout=True,
            wait_until=lambda browser: self._is_stopped() or _browser_has_olx_offers(browser),
        )

    def _fetch_detail_html(self, driver: uc.Chrome, url: str) -> str:
        return fetch_with_existing_browser(
            driver,
            url,
            timeout_seconds=self.timeout_seconds,
            page_load_timeout_seconds=self.page_load_timeout_seconds,
            return_partial_on_timeout=True,
            wait_until=lambda browser: self._is_stopped() or _browser_has_olx_detail(browser),
        )

    def _enrich_offer_from_detail(self, driver: uc.Chrome, offer: JobOffer) -> JobOffer:
        if not offer.url:
            return offer

        delay = random.uniform(*self.detail_delay_seconds)
        logger.info("Waiting %.1fs before OLX detail fetch: %s", delay, offer.url)
        if self.stop_event and self.stop_event.wait(delay):
            return offer

        try:
            detail = parse_olx_offer_detail(self._fetch_detail_html(driver, offer.url))
        except BrowserFetchError, TypeError, ValueError, WebDriverException:
            logger.exception("Could not enrich OLX offer from detail page: %s", offer.url)
            return offer

        raw_detail = detail.get("raw")
        description_text = detail.get("description_text")
        return replace(
            offer,
            title=_clean(detail.get("title")) or offer.title,
            company=_clean(detail.get("company")) or offer.company,
            location=_clean(detail.get("location")) or offer.location,
            salary=offer.salary or _clean(detail.get("salary")),
            description_text=(
                clean_multiline(description_text)
                if isinstance(description_text, str)
                else offer.description_text
            ),
            posted_at=_clean(detail.get("posted_at")) or offer.posted_at,
            raw={**offer.raw, "detail": raw_detail if isinstance(raw_detail, dict) else {}},
        )

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()


def build_search_url(search: JobSearch) -> str:
    keyword = quote("-".join(search.keywords.casefold().split()))
    base = f"https://www.olx.pl/praca/q-{keyword}/"
    if not is_poland_location(search.location):
        location = _slugify_location(search.location)
        return f"https://www.olx.pl/praca/{quote(location)}/q-{keyword}/"
    return base


def _slugify_location(location: str) -> str:
    value = location.strip().casefold().translate(str.maketrans("ł", "l"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "polska"


def parse_olx_offers(html_text: str) -> list[JobOffer]:
    try:
        state = _extract_prerendered_state(html_text)
    except ValueError:
        logger.info("OLX prerendered state not found, parsing rendered offer cards")
        return _parse_offer_cards(html_text)
    ads = state.get("listing", {}).get("listing", {}).get("ads", [])
    if not isinstance(ads, list):
        raise TypeError("OLX prerendered state has unexpected ads payload")
    return [_map_ad(ad) for ad in ads if isinstance(ad, dict) and ad.get("isJob") is not False]


def parse_olx_offer_detail(html_text: str) -> dict[str, Any]:
    posting = _extract_job_posting(html_text)
    return {
        "title": _clean(posting.get("title")),
        "company": _format_hiring_organization(posting.get("hiringOrganization")),
        "location": _format_job_location(posting.get("jobLocation")),
        "salary": _format_schema_salary(posting.get("baseSalary")),
        "description_text": _format_schema_description(posting.get("description")),
        "posted_at": _clean(posting.get("datePosted")),
        "raw": {"job_posting": posting},
    }


def _page_has_olx_offers(html_text: str) -> bool:
    return "__PRERENDERED_STATE__" in html_text or 'data-testid="l-card"' in html_text


def _browser_has_olx_offers(driver: Any) -> bool:
    try:
        has_cards = driver.execute_script(
            'return Boolean(document.querySelector(\'[data-testid="l-card"], [data-cy="l-card"]\'));'
        )
    except WebDriverException:
        logger.exception("Could not inspect OLX rendered offer cards")
        return _page_has_olx_offers(str(driver.page_source))
    return bool(has_cards) or "__PRERENDERED_STATE__" in str(driver.page_source)


def _browser_has_olx_detail(driver: Any) -> bool:
    try:
        has_detail = driver.execute_script(
            """
return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
  .some((node) => (node.textContent || '').includes('JobPosting'));
            """
        )
    except WebDriverException:
        logger.exception("Could not inspect OLX detail page")
        return "JobPosting" in str(driver.page_source)
    return bool(has_detail) or "JobPosting" in str(driver.page_source)


def _parse_offer_cards(html_text: str) -> list[JobOffer]:
    tree = HTMLParser(html_text)
    cards = tree.css('[data-testid="l-card"]') or tree.css('[data-cy="l-card"]')
    offers = [_map_card(card) for card in cards]
    return [offer for offer in offers if offer is not None]


def _map_card(card: Any) -> JobOffer | None:
    title = _clean(_node_text(card.css_first("h4")))
    title_link = card.css_first('[data-testid="card-title-link"]')
    url = _normalize_url(_clean(title_link.attributes.get("href")) if title_link else None)
    if not title and not url:
        return None

    card_id = _clean(card.attributes.get("id"))
    location = _clean(_node_text(card.css_first("span")))
    paragraphs = [_clean(_node_text(node)) for node in card.css("p")]
    salary = next((item for item in paragraphs if item and "zł" in item.casefold()), None)
    posted_at = next(
        (item for item in paragraphs if item and item.casefold().startswith("odświeżono")),
        None,
    )
    return JobOffer(
        source=OlxJobSource.source_name,
        external_id=card_id,
        title=title or "Untitled OLX offer",
        company="Unknown company",
        location=location,
        url=url,
        salary=salary,
        description_text=None,
        posted_at=posted_at,
        raw={"card_id": card_id, "html_source": "rendered_card"},
    )


def _node_text(node: Any) -> str | None:
    if node is None:
        return None
    return str(node.text(separator=" ", strip=True))


def _extract_prerendered_state(html_text: str) -> dict[str, Any]:
    match = re.search(r"window\.__PRERENDERED_STATE__\s*=\s*(\".*?\")\s*;", html_text, re.DOTALL)
    if not match:
        raise ValueError("OLX page does not contain window.__PRERENDERED_STATE__")
    decoded = json.loads(match.group(1))
    result = json.loads(decoded)
    if not isinstance(result, dict):
        raise TypeError("OLX prerendered state is not an object")
    return result


def _extract_job_posting(html_text: str) -> dict[str, Any]:
    tree = HTMLParser(html_text)
    for node in tree.css('script[type="application/ld+json"]'):
        for item in _iter_json_ld_objects(node.text()):
            posting = _find_job_posting(item)
            if posting is not None:
                return posting
    raise ValueError("OLX detail page does not contain JobPosting JSON-LD")


def _iter_json_ld_objects(value: str) -> list[Any]:
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.exception("Could not decode OLX JSON-LD payload")
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


def _format_hiring_organization(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean(value.get("name"))
    return _clean(value)


def _format_job_location(value: Any) -> str | None:
    if isinstance(value, list):
        return join_unique([_format_job_location(item) for item in value])
    if not isinstance(value, dict):
        return _clean(value)
    address = value.get("address")
    if not isinstance(address, dict):
        return _clean(value.get("name"))
    return join_unique(
        [
            _clean(address.get("addressLocality")),
            _clean(address.get("addressRegion")),
            _clean(address.get("streetAddress")),
        ]
    )


def _format_schema_salary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return _clean(value)
    currency = _clean(value.get("currency")) or _clean(value.get("salaryCurrency")) or ""
    salary_value = value.get("value")
    if isinstance(salary_value, dict):
        salary_from = salary_value.get("minValue") or salary_value.get("value")
        salary_to = salary_value.get("maxValue")
        unit = _clean(salary_value.get("unitText")) or _clean(value.get("unitText")) or ""
    else:
        salary_from = value.get("minValue") or value.get("value")
        salary_to = value.get("maxValue")
        unit = _clean(value.get("unitText")) or ""
    if salary_from is None and salary_to is None:
        return None
    amount = _format_amount(salary_from) if salary_from is not None else _format_amount(salary_to)
    if salary_from is not None and salary_to is not None:
        amount = f"{_format_amount(salary_from)} - {_format_amount(salary_to)}"
    return " ".join(
        part
        for part in [amount, _format_salary_currency(currency), _format_salary_unit(unit)]
        if part
    )


def _format_schema_description(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    sections = _html_description_sections(value)
    return format_bullet_sections(sections) if sections else html_to_text(value)


def _html_description_sections(value: str) -> list[tuple[str, list[str]]]:
    tree = HTMLParser(value)
    sections: list[tuple[str, list[str]]] = [("Opis", [])]

    for node in tree.css("p,li,h1,h2,h3,h4,h5,h6"):
        if node.tag == "li":
            _append_description_item(sections, node.text(separator=" ", strip=True))
            continue
        if _has_ancestor_tag(node, "li"):
            continue

        text = _clean(node.text(separator=" ", strip=True))
        if not text:
            continue
        if heading := _description_heading(node, text):
            sections.append((heading, []))
        else:
            _append_description_item(sections, text)

    return [(title, items) for title, items in sections if items]


def _append_description_item(sections: list[tuple[str, list[str]]], value: str | None) -> None:
    item = _clean(value)
    if item:
        sections[-1][1].append(item)


def _description_heading(node: Any, text: str) -> str | None:
    if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return _strip_heading_colon(text)

    strong = node.css_first("strong") or node.css_first("b")
    if strong is None:
        return None
    strong_text = _clean(strong.text(separator=" ", strip=True))
    if not strong_text:
        return None
    if _strip_heading_colon(text) != _strip_heading_colon(strong_text):
        return None
    if not _ends_with_heading_colon(text) and len(text) > 80:
        return None
    return _strip_heading_colon(text)


def _strip_heading_colon(value: str) -> str:
    return re.sub(r"\s+:", ":", value).rstrip(":").strip()


def _ends_with_heading_colon(value: str) -> bool:
    return re.sub(r"\s+:", ":", value).endswith(":")


def _has_ancestor_tag(node: Any, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False


def _format_amount(value: Any) -> str:
    if isinstance(value, int | float):
        formatted = f"{value:g}"
        if float(value).is_integer() and abs(value) >= 1000:
            return f"{int(value):,}".replace(",", " ")
        return formatted
    return str(value)


def _format_salary_currency(value: str) -> str | None:
    if not value:
        return None
    currencies = {"PLN": "zł"}
    return currencies.get(value.upper(), value)


def _format_salary_unit(value: str | None) -> str | None:
    if not value:
        return None
    units = {
        "MONTH": "/ mies.",
        "HOUR": "/ godz.",
        "YEAR": "/ rok",
    }
    return units.get(value.upper(), value)


def _map_ad(ad: dict[str, Any]) -> JobOffer:
    raw_user = ad.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    return JobOffer(
        source=OlxJobSource.source_name,
        external_id=_clean(ad.get("id")),
        title=_clean(ad.get("title")) or "Untitled OLX offer",
        company=_clean(user.get("company_name")) or _clean(user.get("name")) or "Unknown company",
        location=_format_location(ad.get("location")),
        url=_normalize_url(_clean(ad.get("url")) or _clean(ad.get("urlPath"))),
        salary=_format_salary(ad.get("salary")) or _format_salary(ad.get("price")),
        description_text=html_to_text(_clean(ad.get("description"))),
        posted_at=_clean(ad.get("createdTime")) or _clean(ad.get("lastRefreshTime")),
        raw=ad,
    )


def _format_location(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return join_unique(
        [
            _clean(value.get("cityName")),
            _clean(value.get("districtName")),
            _clean(value.get("regionName")),
            _clean(value.get("pathName")),
        ]
    )


def _format_salary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return _clean(value)
    if display := _clean(value.get("displayValue")):
        return display
    salary_from = value.get("from") or value.get("value")
    salary_to = value.get("to")
    currency = _clean(value.get("currencySymbol")) or _clean(value.get("currencyCode")) or ""
    period = _clean(value.get("period")) or ""
    if salary_from is None and salary_to is None:
        return None
    amount = str(salary_from)
    if salary_to is not None:
        amount = f"{amount} - {salary_to}"
    return join_unique([amount, currency, period])


def _normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    return urljoin("https://www.olx.pl", value)


def _clean(value: Any) -> str | None:
    return clean_text(value)
