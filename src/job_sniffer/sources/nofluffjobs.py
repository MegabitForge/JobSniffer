from __future__ import annotations

import html
import json
import logging
import random
import re
import threading
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources._common import DEFAULT_USER_AGENT, is_poland_location
from job_sniffer.sources._text import clean_text, format_bullet_sections, join_unique
from job_sniffer.sources.base import DuplicateChecker, filter_new_offers

logger = logging.getLogger(__name__)


class NoFluffJobsSource:
    source_name = "nofluffjobs"

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        detail_delay_seconds: tuple[float, float] = (2.0, 4.0),
        stop_event: threading.Event | None = None,
        duplicate_checker: DuplicateChecker | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.stop_event = stop_event
        self.duplicate_checker = duplicate_checker

    def search(self, search: JobSearch) -> list[JobOffer]:
        logger.info(
            "Starting NoFluffJobs search: keywords=%r location=%r limit=%s",
            search.keywords,
            search.location,
            search.limit,
        )
        data = self._fetch_listing_data(search)
        postings = data.get("postings", [])
        if not isinstance(postings, list):
            raise TypeError("NoFluffJobs API returned an unexpected postings payload")

        offers = [_map_posting(posting) for posting in postings if isinstance(posting, dict)]
        new_offers = filter_new_offers(offers, self.duplicate_checker)
        logger.info("NoFluffJobs returned %s offers, %s were new", len(offers), len(new_offers))
        limited_offers = new_offers if search.limit is None else new_offers[: search.limit]
        return [self._enrich_offer_from_detail(offer) for offer in limited_offers]

    def _fetch_listing_data(self, search: JobSearch) -> dict[str, Any]:
        if is_poland_location(search.location):
            response = httpx.get(
                "https://nofluffjobs.com/api/joboffers/main",
                params={
                    "salaryCurrency": "PLN",
                    "salaryPeriod": "MONTH",
                    "criteria": search.keywords,
                },
                headers={"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise TypeError("NoFluffJobs API returned an unexpected listing payload")
            return data

        url = build_search_url(search)
        logger.info("Fetching NoFluffJobs location-filtered page: %s", url)
        response = httpx.get(
            url,
            headers={"Accept": "text/html", "User-Agent": DEFAULT_USER_AGENT},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return _extract_listing_data_from_state(response.text)

    def _enrich_offer_from_detail(self, offer: JobOffer) -> JobOffer:
        if not offer.url:
            return offer

        delay = random.uniform(*self.detail_delay_seconds)
        logger.info("Waiting %.1fs before NoFluffJobs detail fetch: %s", delay, offer.url)
        if self.stop_event and self.stop_event.wait(delay):
            self._raise_if_stopped()

        self._raise_if_stopped()
        logger.info("Fetching NoFluffJobs detail page: %s", offer.url)
        try:
            response = httpx.get(
                offer.url,
                headers={"Accept": "text/html", "User-Agent": DEFAULT_USER_AGENT},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            detail = parse_nofluffjobs_offer_detail(response.text)
        except httpx.HTTPError, TypeError, ValueError:
            logger.exception("Could not enrich NoFluffJobs offer from detail page: %s", offer.url)
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

    def _raise_if_stopped(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("NoFluffJobs scan cancelled because the application is closing.")


def _map_posting(posting: dict[str, Any]) -> JobOffer:
    title = _clean(posting.get("title")) or "Untitled NoFluffJobs offer"
    company = _clean(posting.get("name")) or "Unknown company"
    url_slug = _clean(posting.get("url")) or _clean(posting.get("id"))
    salary = _format_salary(posting.get("salary"))
    location = _format_location(posting.get("location"))
    tags = _format_tiles(posting.get("tiles"))
    description_text = tags or None

    return JobOffer(
        source=NoFluffJobsSource.source_name,
        external_id=_clean(posting.get("id")) or _clean(posting.get("reference")),
        title=title,
        company=company,
        location=location,
        url=f"https://nofluffjobs.com/job/{quote(url_slug, safe='/')}" if url_slug else None,
        salary=salary,
        description_text=description_text,
        posted_at=_clean(posting.get("posted")) or _clean(posting.get("renewed")),
        raw=posting,
    )


def build_search_url(search: JobSearch) -> str:
    if is_poland_location(search.location):
        return "https://nofluffjobs.com/pl"
    location = _slugify_location(search.location)
    keyword = quote(search.keywords.strip())
    if not keyword:
        return f"https://nofluffjobs.com/pl/{location}"
    return f"https://nofluffjobs.com/pl/{location}?criteria={keyword}"


def _extract_listing_data_from_state(html_text: str) -> dict[str, Any]:
    payload = _extract_server_app_state(HTMLParser(html_text))
    if payload is None:
        raise ValueError("NoFluffJobs page does not contain serverApp-state payload")
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("postings"), list):
            return value
    raise ValueError("NoFluffJobs serverApp-state does not contain listing results")


def parse_nofluffjobs_offer_detail(html_text: str) -> dict[str, Any]:
    tree = HTMLParser(html_text)
    payload_sections = _extract_detail_sections_from_state(tree)
    if payload_sections:
        return {
            "description_text": format_bullet_sections(payload_sections),
            "raw": {
                "sections": [{"title": title, "items": items} for title, items in payload_sections]
            },
        }

    sections: list[tuple[str, list[str]]] = []
    raw_sections: list[dict[str, Any]] = []

    for heading in tree.css("h2,h3,h4"):
        title = _canonical_section_title(heading.text(strip=True))
        if not title or heading.parent is None:
            continue
        items = _extract_section_items(heading.parent, title)
        if not items:
            continue
        sections.append((title, items))
        raw_sections.append({"title": title, "items": items})

    return {
        "description_text": format_bullet_sections(sections),
        "raw": {"sections": raw_sections},
    }


def _extract_detail_sections_from_state(tree: HTMLParser) -> list[tuple[str, list[str]]]:
    payload = _extract_server_app_state(tree)
    if payload is None:
        return []
    posting = _extract_posting_payload(payload)
    if posting is None:
        return []

    sections: list[tuple[str, list[str]]] = []
    requirements = posting.get("requirements")
    if isinstance(requirements, dict):
        musts = _requirement_items(requirements.get("musts"))
        musts.extend(_language_items(requirements.get("languages"), language_type="MUST"))
        if musts:
            sections.append(("Must have", musts))

        nices = _requirement_items(requirements.get("nices"))
        nices.extend(_language_items(requirements.get("languages"), language_type="NICE"))
        if nices:
            sections.append(("Nice to have", nices))

        if description_items := _html_items(requirements.get("description")):
            sections.append(("Requirements description", description_items))

    specs = posting.get("specs")
    if isinstance(specs, dict) and (tasks := _string_items(specs.get("dailyTasks"))):
        sections.append(("Your responsibilities", tasks))

    details = posting.get("details")
    if isinstance(details, dict) and (description := _html_items(details.get("description"))):
        sections.append(("Offer description", description))

    if job_details := _job_detail_items(posting):
        sections.append(("Job details", job_details))

    return sections


def _extract_server_app_state(tree: HTMLParser) -> dict[str, Any] | None:
    node = tree.css_first("script#serverApp-state")
    if node is None:
        return None
    try:
        payload = json.loads(node.text())
    except json.JSONDecodeError:
        logger.exception("Could not decode NoFluffJobs serverApp-state payload")
        return None
    return payload if isinstance(payload, dict) else None


def _extract_posting_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key, value in payload.items():
        if key.startswith("/posting/") and isinstance(value, dict):
            return value
    return None


def _requirement_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item for entry in value if isinstance(entry, dict) and (item := _clean(entry.get("value")))
    ]


def _language_items(value: Any, *, language_type: str) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item
        for entry in value
        if isinstance(entry, dict)
        and _clean(entry.get("type")) == language_type
        and (item := _format_language(entry))
    ]


def _format_language(value: dict[str, Any]) -> str | None:
    code = _clean(value.get("code"))
    level = _clean(value.get("level"))
    if not code:
        return None
    language = {
        "en": "English",
        "pl": "Polish",
    }.get(code.casefold(), code.upper())
    return f"{language} ({level})" if level else language


def _html_items(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = html.unescape(text).split("\n")
    return [item for line in lines if (item := _clean(line)) and not _is_detail_noise(item)]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for entry in value if (item := _clean(entry))]


def _job_detail_items(posting: dict[str, Any]) -> list[str]:
    items: list[str] = []
    recruitment = posting.get("recruitment")
    if isinstance(recruitment, dict) and recruitment.get("onlineInterviewAvailable"):
        items.append("Online recruitment")

    essentials = posting.get("essentials")
    if isinstance(essentials, dict):
        contract = essentials.get("contract")
        if isinstance(contract, dict) and (start := _clean(contract.get("start"))):
            items.append(f"Start {start}")

    location = posting.get("location")
    if isinstance(location, dict) and (remote_text := _format_remote_work(location.get("remote"))):
        items.append(remote_text)

    specs = posting.get("specs")
    details = specs.get("details") if isinstance(specs, dict) else None
    if isinstance(details, dict) and details.get("flexibleHours"):
        items.append("Flexible hours")
    custom_details = details.get("custom") if isinstance(details, dict) else None
    if isinstance(custom_details, list):
        items.extend(_string_items(custom_details))

    return items


def _format_remote_work(value: Any) -> str | None:
    if value is True:
        return "Fully remote"
    if isinstance(value, int):
        if value <= 0:
            return None
        return f"Remote for {value} {'day' if value == 1 else 'days'} a week"
    return None


def _slugify_location(location: str) -> str:
    value = location.strip().casefold().translate(str.maketrans("ł", "l"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "polska"


def _canonical_section_title(value: str) -> str | None:
    title = _clean(value)
    if not title:
        return None
    normalized = title.casefold()
    titles = {
        "must have": "Must have",
        "requirements description": "Requirements description",
        "your responsibilities": "Your responsibilities",
        "offer description": "Offer description",
        "job details": "Job details",
    }
    return titles.get(normalized)


def _extract_section_items(section: Any, title: str) -> list[str]:
    list_items = [_clean(node.text(separator=" ", strip=True)) for node in section.css("li")]
    items = [item for item in list_items if item and not _is_detail_noise(item)]
    if items:
        return items

    lines = [_clean(line) for line in section.text(separator="\n", strip=True).split("\n")]
    return [line for line in lines if line and line != title and not _is_detail_noise(line)]


def _is_detail_noise(value: str) -> bool:
    normalized = value.casefold().strip()
    return normalized in {
        "original text.",
        "show translation",
        "show all",
    } or bool(re.fullmatch(r"\(\d+\)", normalized))


def _format_location(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    if value.get("fullyRemote"):
        parts.append("Remote")
    places = value.get("places")
    if isinstance(places, list):
        parts.extend(
            city
            for place in places
            if isinstance(place, dict) and (city := _clean(place.get("city")))
        )
    if hybrid := _clean(value.get("hybridDesc")):
        parts.append(hybrid)
    return join_unique(parts)


def _format_salary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    salary_from = value.get("from")
    salary_to = value.get("to")
    currency = _clean(value.get("currency")) or ""
    period = _clean(value.get("period")) or ""
    contract = _clean(value.get("type")) or ""
    if salary_from is None and salary_to is None:
        return None
    amount = f"{salary_from:g}" if isinstance(salary_from, float | int) else str(salary_from)
    if salary_to is not None:
        amount_to = f"{salary_to:g}" if isinstance(salary_to, float | int) else str(salary_to)
        amount = f"{amount} - {amount_to}"
    return join_unique([amount, currency, period, contract])


def _format_tiles(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    values = value.get("values")
    if not isinstance(values, list):
        return None
    tags = [item.get("value") for item in values if isinstance(item, dict)]
    joined = join_unique([_clean(tag) for tag in tags])
    return f"Tags: {joined}" if joined else None


def _clean(value: Any) -> str | None:
    return clean_text(value)
