from __future__ import annotations

import json
import logging
import random
import re
import threading
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources._common import DEFAULT_USER_AGENT, is_poland_location
from job_sniffer.sources._next_data import extract_next_data
from job_sniffer.sources._text import (
    clean_multiline,
    clean_text,
    format_bullet_sections,
    join_unique,
)
from job_sniffer.sources.base import (
    DuplicateChecker,
    EnrichedOfferHandler,
    dedupe_listing_offers,
    filter_new_offers,
    limit_offers,
    offer_listing_key,
    process_enriched_offers,
    wait_before_next_page,
)

logger = logging.getLogger(__name__)


class BulldogjobSource:
    source_name = "bulldogjob"

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        detail_delay_seconds: tuple[float, float] = (2.0, 4.0),
        pagination_delay_seconds: tuple[float, float] = (2.0, 4.0),
        max_pages: int = 20,
        stop_event: threading.Event | None = None,
        duplicate_checker: DuplicateChecker | None = None,
        enriched_offer_handler: EnrichedOfferHandler | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.pagination_delay_seconds = pagination_delay_seconds
        self.max_pages = max_pages
        self.stop_event = stop_event
        self.duplicate_checker = duplicate_checker
        self.enriched_offer_handler = enriched_offer_handler

    def search(self, search: JobSearch) -> list[JobOffer]:
        logger.info(
            "Starting Bulldogjob search: keywords=%r location=%r limit=%s",
            search.keywords,
            search.location,
            search.limit,
        )
        url = build_search_url(search)
        logger.info("Built Bulldogjob search URL: %s", url)
        offers = self._collect_listing_pages(url)
        new_offers = filter_new_offers(offers, self.duplicate_checker)
        logger.info("Parsed %s Bulldogjob offers, %s were new", len(offers), len(new_offers))
        limited_offers = limit_offers(new_offers, search.limit)
        return process_enriched_offers(
            limited_offers,
            self._enrich_offer_from_detail,
            self.enriched_offer_handler,
        )

    def _collect_listing_pages(self, url: str) -> list[JobOffer]:
        offers: list[JobOffer] = []
        seen_keys: set[tuple[str, str]] = set()

        for page in range(1, self.max_pages + 1):
            self._raise_if_stopped()
            self._wait_before_listing_page(page)
            page_url = _with_page(url, page)
            logger.info("Fetching Bulldogjob listing page %s: %s", page, page_url)
            response = httpx.get(
                page_url,
                headers={"Accept": "text/html", "User-Agent": DEFAULT_USER_AGENT},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            page_offers = parse_bulldogjob_offers(response.text)
            page_new = [offer for offer in page_offers if offer_listing_key(offer) not in seen_keys]
            logger.info(
                "Parsed %s Bulldogjob offers from listing page %s, %s were new in this scan",
                len(page_offers),
                page,
                len(page_new),
            )
            if not page_offers or not page_new:
                break
            for offer in page_new:
                seen_keys.add(offer_listing_key(offer))
            offers.extend(page_new)
        else:
            logger.warning("Stopped Bulldogjob pagination after max_pages=%s", self.max_pages)

        return dedupe_listing_offers(offers)

    def _wait_before_listing_page(self, page: int) -> None:
        delay = wait_before_next_page(page, self.pagination_delay_seconds, self.stop_event)
        if delay is not None:
            logger.info("Waited %.1fs before Bulldogjob listing page %s", delay, page)
        self._raise_if_stopped()

    def _enrich_offer_from_detail(self, offer: JobOffer) -> JobOffer | None:
        if not offer.url:
            logger.warning("Skipping Bulldogjob offer without a detail URL: %s", offer.title)
            return None

        delay = random.uniform(*self.detail_delay_seconds)
        logger.info("Waiting %.1fs before Bulldogjob detail fetch: %s", delay, offer.url)
        if self.stop_event and self.stop_event.wait(delay):
            self._raise_if_stopped()

        self._raise_if_stopped()
        logger.info("Fetching Bulldogjob detail page: %s", offer.url)
        try:
            response = httpx.get(
                offer.url,
                headers={"Accept": "text/html", "User-Agent": DEFAULT_USER_AGENT},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            detail = parse_bulldogjob_offer_detail(response.text)
        except httpx.HTTPError, TypeError, ValueError:
            logger.exception("Could not enrich Bulldogjob offer from detail page: %s", offer.url)
            return None

        raw_detail = detail.get("raw")
        description_text = detail.get("description_text")
        cleaned_description = (
            clean_multiline(description_text) if isinstance(description_text, str) else None
        )
        if not cleaned_description:
            logger.warning("No description found on Bulldogjob detail page: %s", offer.url)
            return None
        return replace(
            offer,
            title=_clean(detail.get("title")) or offer.title,
            company=_clean(detail.get("company")) or offer.company,
            location=_clean(detail.get("location")) or offer.location,
            salary=_clean(detail.get("salary")) or offer.salary,
            description_text=cleaned_description,
            posted_at=_clean(detail.get("posted_at")) or offer.posted_at,
            raw={**offer.raw, "detail": raw_detail if isinstance(raw_detail, dict) else {}},
        )

    def _raise_if_stopped(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("Bulldogjob scan cancelled because the application is closing.")


def build_search_url(search: JobSearch) -> str:
    filters: list[str] = []
    if not is_poland_location(search.location):
        filters.append(f"city,{_slugify_location(search.location)}")

    if not filters:
        return "https://bulldogjob.pl/companies/jobs/s/page,1"
    return "https://bulldogjob.pl/companies/jobs/s/" + "/".join(filters)


def _with_page(url: str, page: int) -> str:
    page_segment = f"page,{page}"
    if re.search(r"(?:^|/)page,\d+(?:/|$)", url):
        return re.sub(r"(?:^|/)page,\d+(?=/|$)", f"/{page_segment}", url)
    if "/s/" in url:
        return f"{url.rstrip('/')}/{page_segment}"
    return f"{url.rstrip('/')}/s/{page_segment}"


def parse_bulldogjob_offers(html_text: str) -> list[JobOffer]:
    payload = _extract_next_data(html_text)
    jobs = payload.get("props", {}).get("pageProps", {}).get("jobs", [])
    if not isinstance(jobs, list):
        raise TypeError("Bulldogjob Next.js payload has unexpected jobs shape")
    return [_map_job(job) for job in jobs if isinstance(job, dict)]


def parse_bulldogjob_offer_detail(html_text: str) -> dict[str, Any]:
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


def _extract_next_data(html_text: str) -> dict[str, Any]:
    return extract_next_data(
        html_text,
        missing_message="Bulldogjob page does not contain __NEXT_DATA__ payload",
        type_message="Bulldogjob __NEXT_DATA__ payload is not a JSON object",
    )


def _map_job(job: dict[str, Any]) -> JobOffer:
    company = job.get("company")
    company_name = _clean(company.get("name")) if isinstance(company, dict) else None
    return JobOffer(
        source=BulldogjobSource.source_name,
        external_id=_clean(job.get("id")),
        title=_clean(job.get("position")) or "Untitled Bulldogjob offer",
        company=company_name or "Unknown company",
        location=_format_listing_location(job),
        url=_format_job_url(job),
        salary=_format_listing_salary(job.get("denominatedSalaryLong")),
        description_text=_format_listing_description(job),
        posted_at=_clean(job.get("publishedAt")) or _clean(job.get("createdAt")),
        raw=job,
    )


def _format_job_url(job: dict[str, Any]) -> str | None:
    job_id = _clean(job.get("id"))
    if not job_id:
        return None
    return urljoin("https://bulldogjob.pl", f"/companies/jobs/{job_id}")


def _format_listing_location(job: dict[str, Any]) -> str | None:
    values = [_clean(job.get("city"))]
    if job.get("remote") is True:
        values.append("Remote")
    environment = job.get("environment")
    if isinstance(environment, dict) and environment.get("remotePossible") is True:
        values.append("Remote possible")
    return join_unique(values)


def _format_listing_salary(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("hidden") is True:
        return None
    money = _clean(value.get("money"))
    currency = _clean(value.get("currency"))
    return join_unique([money, _format_salary_currency(currency)])


def _format_listing_description(job: dict[str, Any]) -> str | None:
    tags = job.get("technologyTags")
    if not isinstance(tags, list):
        return None
    values = [_clean(tag) for tag in tags]
    return format_bullet_sections([("Technologie", values)])


def _extract_job_posting(html_text: str) -> dict[str, Any]:
    tree = HTMLParser(html_text)
    for node in tree.css('script[type="application/ld+json"]'):
        for item in _iter_json_ld_objects(node.text()):
            posting = _find_job_posting(item)
            if posting is not None:
                return posting
    raise ValueError("Bulldogjob detail page does not contain JobPosting JSON-LD")


def _iter_json_ld_objects(value: str) -> list[Any]:
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        logger.exception("Could not decode Bulldogjob JSON-LD payload")
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
    return _clean(address.get("addressLocality"))


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
    return format_bullet_sections(sections) if sections else clean_text(value)


def _html_description_sections(value: str) -> list[tuple[str, list[str]]]:
    heading_parts = re.split(r"<h[1-6][^>]*>(.*?)</h[1-6]>", value, flags=re.IGNORECASE | re.DOTALL)
    if len(heading_parts) > 1:
        sections: list[tuple[str, list[str]]] = []
        intro_items = _html_fragment_items(heading_parts[0])
        if intro_items:
            sections.append(("Opis", intro_items))

        for index in range(1, len(heading_parts), 2):
            title = _clean(HTMLParser(heading_parts[index]).text(separator=" ", strip=True))
            body = heading_parts[index + 1] if index + 1 < len(heading_parts) else ""
            items = _html_fragment_items(body)
            if title and items:
                sections.append((title.rstrip(":"), items))
        if sections:
            return sections

    tree = HTMLParser(value)
    fallback_sections: list[tuple[str, list[str]]] = []
    current_title = "Opis"
    current_items: list[str] = []

    def flush() -> None:
        nonlocal current_items
        if current_items:
            fallback_sections.append((current_title, current_items))
            current_items = []

    for node in tree.css("h1,h2,h3,h4,h5,h6,p,li"):
        if _has_ancestor_tag(node, "li") and node.tag != "li":
            continue
        text = _clean(node.text(separator=" ", strip=True))
        if not text:
            continue
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            flush()
            current_title = text.rstrip(":").strip()
            continue
        current_items.append(text)

    flush()
    return fallback_sections


def _html_fragment_items(value: str) -> list[str]:
    if not value.strip():
        return []
    tree = HTMLParser(value)
    items = [_clean(node.text(separator=" ", strip=True)) for node in tree.css("li")]
    clean_items = [item for item in items if item]
    if clean_items:
        return clean_items
    text = _clean(tree.text(separator=" ", strip=True))
    return [text] if text else []


def _has_ancestor_tag(node: Any, tag: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.parent
    return False


def _format_amount(value: Any) -> str:
    if isinstance(value, int | float):
        if float(value).is_integer() and abs(value) >= 1000:
            return f"{int(value):,}".replace(",", " ")
        return f"{value:g}"
    return str(value)


def _format_salary_currency(value: str | None) -> str | None:
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


def _slugify_location(location: str) -> str:
    value = location.strip().casefold().translate(str.maketrans("ł", "l"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def _clean(value: Any) -> str | None:
    return clean_text(value)
