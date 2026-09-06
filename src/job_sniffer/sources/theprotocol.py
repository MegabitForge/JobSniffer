from __future__ import annotations

import logging
import random
import re
import threading
import unicodedata
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urljoin

import undetected_chromedriver as uc  # type: ignore[import-untyped]

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources._common import is_poland_location
from job_sniffer.sources._next_data import extract_next_data
from job_sniffer.sources._text import (
    clean_text,
    dedupe_casefold,
    format_bullet_sections,
    join_unique,
)
from job_sniffer.sources.base import DuplicateChecker, filter_new_offers
from job_sniffer.sources.browser import (
    BrowserFetchError,
    fetch_with_existing_browser,
    start_undetected_chrome,
)

logger = logging.getLogger(__name__)


class TheProtocolJobSource:
    source_name = "theprotocol"

    def __init__(
        self,
        *,
        chrome_user_data_dir: str = ".theprotocol-profile",
        timeout_seconds: float = 90.0,
        detail_delay_seconds: tuple[float, float] = (2.0, 4.0),
        stop_event: threading.Event | None = None,
        duplicate_checker: DuplicateChecker | None = None,
    ) -> None:
        self.chrome_user_data_dir = chrome_user_data_dir
        self.timeout_seconds = timeout_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.stop_event = stop_event
        self.duplicate_checker = duplicate_checker

    def search(self, search: JobSearch) -> list[JobOffer]:
        logger.info(
            "Starting TheProtocol search: keywords=%r location=%r limit=%s",
            search.keywords,
            search.location,
            search.limit,
        )
        url = build_search_url(search)
        logger.info("Built TheProtocol search URL: %s", url)
        return self._collect_offers_with_browser(url, search=search)

    def _collect_offers_with_browser(self, url: str, *, search: JobSearch) -> list[JobOffer]:
        logger.info("Starting one TheProtocol browser session for listing and detail pages")
        driver = start_undetected_chrome(self.chrome_user_data_dir)
        try:
            self._raise_if_stopped()
            html_text = self._fetch_html(driver, url)
            offers = parse_theprotocol_offers(html_text)
            new_offers = filter_new_offers(offers, self.duplicate_checker)
            logger.info("Parsed %s TheProtocol offers, %s were new", len(offers), len(new_offers))
            limited_offers = new_offers if search.limit is None else new_offers[: search.limit]
            return [self._enrich_offer_from_detail(driver, offer) for offer in limited_offers]
        finally:
            logger.info("Closing TheProtocol browser session")
            driver.quit()

    def _fetch_html(self, driver: uc.Chrome, url: str) -> str:
        logger.info("Fetching TheProtocol page with existing browser: %s", url)
        html_text = fetch_with_existing_browser(
            driver,
            url,
            timeout_seconds=self.timeout_seconds,
            wait_until=lambda browser: (
                self._is_stopped() or "__NEXT_DATA__" in str(browser.page_source)
            ),
        )
        self._raise_if_stopped()
        return html_text

    def _enrich_offer_from_detail(self, driver: uc.Chrome, offer: JobOffer) -> JobOffer:
        if not offer.url:
            return offer

        self._raise_if_stopped()

        delay = random.uniform(*self.detail_delay_seconds)
        logger.info("Waiting %.1fs before TheProtocol detail fetch: %s", delay, offer.url)
        if self.stop_event and self.stop_event.wait(delay):
            self._raise_if_stopped()
        try:
            html_text = self._fetch_html(driver, offer.url)
            detail = parse_theprotocol_offer_detail(html_text)
        except BrowserFetchError, TypeError, ValueError:
            logger.exception("Could not parse TheProtocol detail page: %s", offer.url)
            return offer

        description_text = detail.get("description_text")
        if not isinstance(description_text, str) or not description_text.strip():
            return offer

        raw_detail = detail.get("raw")
        return replace(
            offer,
            description_text=description_text,
            raw={**offer.raw, "detail": raw_detail if isinstance(raw_detail, dict) else {}},
        )

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _raise_if_stopped(self) -> None:
        if self._is_stopped():
            raise BrowserFetchError(
                "TheProtocol scan cancelled because the application is closing."
            )


def build_search_url(search: JobSearch) -> str:
    keyword = quote(search.keywords.strip())
    if is_poland_location(search.location):
        if not keyword:
            return "https://theprotocol.it/praca"
        return f"https://theprotocol.it/praca?kw={keyword}"
    location = _slugify_location(search.location)
    if not keyword:
        return f"https://theprotocol.it/filtry/{location};wp"
    return f"https://theprotocol.it/filtry/{location};wp?kw={keyword}"


def parse_theprotocol_offers(html_text: str) -> list[JobOffer]:
    data = _extract_next_data(html_text)
    offers = data.get("props", {}).get("pageProps", {}).get("offersResponse", {}).get("offers", [])
    if not isinstance(offers, list):
        raise TypeError("TheProtocol Next.js payload has unexpected offers shape")
    return [_map_offer(offer) for offer in offers if isinstance(offer, dict)]


def parse_theprotocol_offer_detail(html_text: str) -> dict[str, Any]:
    data = _extract_next_data(html_text)
    offer = data.get("props", {}).get("pageProps", {}).get("offer", {})
    if not isinstance(offer, dict):
        raise TypeError("TheProtocol detail payload has unexpected offer shape")
    return {
        "description_text": _format_detail_description(offer),
        "raw": offer,
    }


def _extract_next_data(html_text: str) -> dict[str, Any]:
    return extract_next_data(
        html_text,
        missing_message="TheProtocol page does not contain __NEXT_DATA__",
        type_message="TheProtocol __NEXT_DATA__ is not an object",
    )


def _map_offer(offer: dict[str, Any]) -> JobOffer:
    return JobOffer(
        source=TheProtocolJobSource.source_name,
        external_id=_clean(offer.get("id")) or _clean(offer.get("groupId")),
        title=_clean(offer.get("title")) or "Untitled TheProtocol offer",
        company=_clean(offer.get("employer")) or "Unknown company",
        location=_format_location(offer),
        url=_format_url(offer),
        salary=_format_salary(offer),
        description_text=_format_description(offer),
        posted_at=_clean(offer.get("publicationDateUtc")),
        raw=offer,
    )


def _format_url(offer: dict[str, Any]) -> str | None:
    url_name = _clean(offer.get("offerUrlName"))
    if not url_name:
        return None
    return urljoin("https://theprotocol.it", f"/szczegoly/praca/{url_name}")


def _slugify_location(location: str) -> str:
    value = location.strip().casefold().translate(str.maketrans("ł", "l"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "polska"


def _format_location(offer: dict[str, Any]) -> str | None:
    values: list[str | None] = []
    workplaces = offer.get("workplace")
    if isinstance(workplaces, list):
        for workplace in workplaces:
            if not isinstance(workplace, dict):
                continue
            values.extend(
                [
                    _clean(workplace.get("location")),
                    _clean(workplace.get("city")),
                    _clean(workplace.get("region")),
                ]
            )
    work_modes = offer.get("workModes")
    if isinstance(work_modes, list):
        for mode in work_modes:
            if isinstance(mode, dict):
                values.append(_clean(mode.get("name") or mode.get("value")))
            else:
                values.append(_clean(mode))
    return join_unique(values)


def _format_salary(offer: dict[str, Any]) -> str | None:
    contracts = offer.get("typesOfContracts")
    if not isinstance(contracts, list):
        return None
    parts: list[str] = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        salary_data = contract.get("salary")
        if not isinstance(salary_data, dict):
            continue
        salary_from = salary_data.get("from")
        salary_to = salary_data.get("to")
        if salary_from is None and salary_to is None:
            continue
        amount = str(salary_from) if salary_from is not None else str(salary_to)
        if salary_to is not None:
            amount = f"{amount} - {salary_to}" if salary_from is not None else amount
        currency = _clean(salary_data.get("currencySymbol")) or ""
        time_unit = salary_data.get("timeUnit")
        time_unit_text = None
        if isinstance(time_unit, dict):
            time_unit_text = _clean(time_unit.get("shortForm"))
        amount_with_currency = " ".join(part for part in [amount, currency] if part)
        if time_unit_text:
            amount_with_currency = f"{amount_with_currency} / {time_unit_text}"
        salary_kind = _clean(salary_data.get("kindName"))
        if salary_kind:
            amount_with_currency = f"{amount_with_currency} {salary_kind}"
        parts.append(amount_with_currency)
    return "; ".join(parts) or None


def _format_description(offer: dict[str, Any]) -> str | None:
    sections: list[tuple[str, list[str]]] = []
    about_project = offer.get("aboutProject")
    if isinstance(about_project, list):
        sections.extend(_split_about_project(about_project))
    technologies = offer.get("technologies")
    if isinstance(technologies, list):
        names: list[str | None] = []
        for item in technologies:
            if isinstance(item, dict):
                names.append(_clean(item.get("name") or item.get("value")))
            else:
                names.append(_clean(item))
        if joined := dedupe_casefold(names):
            sections.append(("Technologie", joined))
    return format_bullet_sections(sections)


def _format_detail_description(offer: dict[str, Any]) -> str | None:
    sections = offer.get("jsonSections")
    if not isinstance(sections, list):
        return _format_description(offer)

    formatted_sections: list[tuple[str, list[str]]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        formatted_sections.extend(_extract_detail_section(section, parent_title=None))

    return format_bullet_sections(_sort_detail_sections(formatted_sections))


def _extract_detail_section(
    section: dict[str, Any], *, parent_title: str | None
) -> list[tuple[str, list[str]]]:
    title = _detail_section_title(section, parent_title=parent_title)
    items = _detail_model_items(section.get("model"))
    result: list[tuple[str, list[str]]] = []
    if title and items:
        result.append((title, items))

    subsections = section.get("subSections")
    if isinstance(subsections, list):
        for subsection in subsections:
            if isinstance(subsection, dict):
                result.extend(_extract_detail_section(subsection, parent_title=title))
    return result


def _detail_section_title(section: dict[str, Any], *, parent_title: str | None) -> str | None:
    section_type = _clean(section.get("sectionType"))
    title = _clean(section.get("title"))
    if section_type == "technologies-expected":
        return "Technologie wymagane"
    if section_type == "requirements-expected":
        return "Nasze wymagania"
    if section_type == "requirements-optional":
        return "Mile widziane"
    if section_type == "responsibilities":
        return "Twój zakres obowiązków"
    if section_type == "about-project":
        return "O projekcie"
    if section_type == "benefits":
        return "Benefity"
    if title and parent_title != title:
        return title
    return None


def _detail_model_items(model: Any) -> list[str]:
    if not isinstance(model, dict):
        return []

    values: list[str | None] = []
    for key in ("bullets", "paragraphs", "customItems", "items"):
        value = model.get(key)
        if isinstance(value, list):
            values.extend(_detail_list_items(value))
    return dedupe_casefold(values)


def _detail_list_items(values: list[Any]) -> list[str | None]:
    items: list[str | None] = []
    for value in values:
        if isinstance(value, dict):
            items.append(_clean(value.get("name") or value.get("text") or value.get("value")))
        else:
            items.append(_clean(value))
    return items


def _sort_detail_sections(
    sections: Sequence[tuple[str, Sequence[str]]],
) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {}
    for title, items in sections:
        grouped.setdefault(title, []).extend(item for item in items if item)
    return [
        (title, dedupe_casefold(items))
        for title, items in sorted(grouped.items(), key=lambda item: _section_order(item[0]))
    ]


def _section_order(title: str) -> tuple[int, str]:
    order = {
        "Technologie wymagane": 0,
        "Nasze wymagania": 1,
        "Mile widziane": 2,
        "Twój zakres obowiązków": 3,
        "O projekcie": 4,
        "Benefity": 5,
    }
    return (order.get(title, 100), title.casefold())


def _split_about_project(values: list[Any]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_title = "O projekcie"
    current_items: list[str] = []

    for value in values:
        text = _clean(value)
        if not text:
            continue
        if _is_description_heading(text):
            if current_items:
                sections.append((current_title, current_items))
            current_title = text.rstrip(":")
            current_items = []
            continue
        current_items.append(text)

    if current_items:
        sections.append((current_title, current_items))
    return sections


def _is_description_heading(value: str) -> bool:
    headings = {
        "czym będziesz się zajmować",
        "czym bedziesz sie zajmowac",
        "twój zakres obowiązków",
        "twoj zakres obowiazkow",
        "nasze wymagania",
        "mile widziane",
        "to oferujemy",
        "benefity",
        "o projekcie",
        "your responsibilities",
        "requirements",
        "nice to have",
        "we offer",
    }
    return value.casefold().rstrip(":") in headings


def _clean(value: Any) -> str | None:
    return clean_text(value)
