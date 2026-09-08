from __future__ import annotations

import logging
import random
import re
import threading
import time
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import undetected_chromedriver as uc  # type: ignore[import-untyped]
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources._common import is_poland_location
from job_sniffer.sources._next_data import extract_next_data
from job_sniffer.sources._text import (
    clean_html_text,
    clean_multiline,
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
from job_sniffer.sources.browser import (
    BrowserClosedError,
    BrowserFetchError,
    accept_cookies_if_visible,
    is_browser_closed_error,
    start_undetected_chrome,
)

logger = logging.getLogger(__name__)

DETAIL_SECTION_ORDER = (
    "O projekcie",
    "Twój zakres obowiązków",
    "Nasze wymagania",
    "Wymagania pracodawcy",
    "Mile widziane",
    "To oferujemy",
    "Pracodawca oferuje",
    "Benefity",
    "Etapy rekrutacji",
    "Your responsibilities",
    "Our requirements",
    "Optional",
    "What we offer",
    "Benefits",
)


class PracujBlockedError(RuntimeError):
    """Raised when Pracuj.pl cannot be loaded or parsed."""


class PracujJobSource:
    source_name = "pracuj"

    def __init__(
        self,
        *,
        chrome_user_data_dir: str = ".pracuj-profile",
        selenium_wait_seconds: float = 120.0,
        cloudflare_wait_seconds: float = 45.0,
        detail_delay_seconds: tuple[float, float] = (2.0, 4.0),
        pagination_delay_seconds: tuple[float, float] = (2.0, 4.0),
        max_pages: int = 20,
        stop_event: threading.Event | None = None,
        duplicate_checker: DuplicateChecker | None = None,
        enriched_offer_handler: EnrichedOfferHandler | None = None,
    ) -> None:
        self.chrome_user_data_dir = chrome_user_data_dir
        self.selenium_wait_seconds = selenium_wait_seconds
        self.cloudflare_wait_seconds = cloudflare_wait_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.pagination_delay_seconds = pagination_delay_seconds
        self.max_pages = max_pages
        self.stop_event = stop_event
        self.duplicate_checker = duplicate_checker
        self.enriched_offer_handler = enriched_offer_handler

    def search(self, search: JobSearch) -> list[JobOffer]:
        logger.info(
            "Starting Pracuj.pl search: keywords=%r location=%r limit=%s",
            search.keywords,
            search.location,
            search.limit,
        )
        url = build_search_url(search)
        logger.info("Built Pracuj.pl search URL: %s", url)
        return self._collect_offers_with_selenium(
            url,
            location=search.location,
            limit=search.limit,
        )

    def _collect_offers_with_selenium(
        self,
        url: str,
        *,
        location: str,
        limit: int | None,
    ) -> list[JobOffer]:
        try:
            driver = start_undetected_chrome(self.chrome_user_data_dir)
        except BrowserFetchError as error:
            raise PracujBlockedError(str(error)) from error

        try:
            self._raise_if_stopped()
            logger.info("Opening Pracuj.pl in undetected Chrome")
            time.sleep(random.uniform(0.8, 1.5))
            self._raise_if_stopped()
            offers = self._collect_listing_pages(driver, url)
            matching_offers = _filter_by_location(offers, location)
            new_offers = filter_new_offers(matching_offers, self.duplicate_checker)
            limited_offers = limit_offers(new_offers, limit)
            logger.info(
                "Parsed %s Pracuj.pl offers, %s matched location, %s were new, enriching %s detail pages",
                len(offers),
                len(matching_offers),
                len(new_offers),
                len(limited_offers),
            )
            return process_enriched_offers(
                limited_offers,
                lambda offer: self._enrich_offer_from_detail(driver, offer),
                self.enriched_offer_handler,
            )
        except WebDriverException as error:
            logger.exception("Selenium could not load Pracuj.pl")
            if is_browser_closed_error(error):
                raise BrowserClosedError(
                    "Browser was closed during scan. Stopping current scan."
                ) from error
            raise PracujBlockedError(f"Selenium could not load Pracuj.pl: {error}") from error
        finally:
            logger.info("Closing undetected Chrome")
            try:
                driver.quit()
            except WebDriverException:
                logger.info("Pracuj.pl browser session was already closed")

    def _collect_listing_pages(self, driver: uc.Chrome, url: str) -> list[JobOffer]:
        offers: list[JobOffer] = []
        seen_keys: set[tuple[str, str]] = set()

        for page in range(1, self.max_pages + 1):
            self._raise_if_stopped()
            self._wait_before_listing_page(page)
            page_url = _with_page(url, page)
            logger.info("Opening Pracuj.pl listing page %s: %s", page, page_url)
            driver.get(page_url)
            accept_cookies_if_visible(driver)
            try:
                self._wait_for_next_data_payload(driver, page_label=f"listing page {page}")
            except TimeoutException as error:
                logger.exception("Timed out waiting for Pracuj.pl listing payload")
                raise PracujBlockedError(
                    "Pracuj.pl did not expose the listing payload in Selenium before timeout. "
                    "Cloudflare may be blocking the automated browser."
                ) from error

            page_offers = parse_pracuj_offers(str(driver.page_source), source_name=self.source_name)
            page_new = [offer for offer in page_offers if offer_listing_key(offer) not in seen_keys]
            logger.info(
                "Parsed %s Pracuj.pl offers from listing page %s, %s were new in this scan",
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
            logger.warning("Stopped Pracuj.pl pagination after max_pages=%s", self.max_pages)

        return dedupe_listing_offers(offers)

    def _wait_before_listing_page(self, page: int) -> None:
        delay = wait_before_next_page(page, self.pagination_delay_seconds, self.stop_event)
        if delay is not None:
            logger.info("Waited %.1fs before Pracuj.pl listing page %s", delay, page)
        self._raise_if_stopped()

    def _enrich_offer_from_detail(self, driver: uc.Chrome, offer: JobOffer) -> JobOffer | None:
        if not offer.url:
            logger.info("Skipping Pracuj.pl detail page because offer has no URL: %s", offer.title)
            return None

        delay = random.uniform(*self.detail_delay_seconds)
        logger.info(
            "Waiting %.1f seconds before opening Pracuj.pl detail page: %s",
            delay,
            offer.url,
        )
        if self.stop_event and self.stop_event.wait(delay):
            self._raise_if_stopped()

        try:
            self._raise_if_stopped()
            logger.info("Opening Pracuj.pl detail page: %s", offer.url)
            driver.get(offer.url)
            accept_cookies_if_visible(driver)
            self._wait_for_next_data_payload(driver, page_label="detail")
            detail = _extract_pracuj_offer_detail_from_dom(driver)
            if not _clean(detail.get("description_text")):
                logger.info(
                    "Could not extract Pracuj.pl detail sections from DOM, falling back to payload"
                )
                detail = parse_pracuj_offer_detail(str(driver.page_source))
        except WebDriverException as error:
            if is_browser_closed_error(error):
                raise BrowserClosedError(
                    "Browser was closed during scan. Stopping current scan."
                ) from error
            logger.exception("Could not enrich Pracuj.pl offer from detail page: %s", offer.url)
            return None
        except (
            PracujBlockedError,
            TimeoutException,
            ValueError,
            TypeError,
        ):
            logger.exception("Could not enrich Pracuj.pl offer from detail page: %s", offer.url)
            return None

        description_text = _clean_multiline(detail.get("description_text"))
        if not description_text:
            logger.warning("No description found on Pracuj.pl detail page: %s", offer.url)
            return None
        salary = _clean(detail.get("salary")) or offer.salary
        logger.info(
            "Enriched Pracuj.pl offer detail: title=%r description_chars=%s",
            offer.title,
            len(description_text or ""),
        )
        return replace(
            offer,
            salary=salary,
            description_text=description_text,
            raw={**offer.raw, "detail": detail.get("raw", {})},
        )

    def _wait_for_next_data_payload(self, driver: uc.Chrome, *, page_label: str) -> None:
        logger.info(
            "Waiting up to %.0f seconds for Pracuj.pl %s payload",
            self.selenium_wait_seconds,
            page_label,
        )
        challenge_started_at: float | None = None

        def has_payload_or_block(browser: uc.Chrome) -> bool:
            nonlocal challenge_started_at
            if self._is_stopped():
                return True

            page_source = browser.page_source
            if "__NEXT_DATA__" in page_source:
                return True

            if _has_cloudflare_challenge(page_source):
                if challenge_started_at is None:
                    challenge_started_at = time.monotonic()
                    logger.warning("Pracuj.pl is showing a Cloudflare challenge")
                elif time.monotonic() - challenge_started_at >= self.cloudflare_wait_seconds:
                    raise PracujBlockedError(
                        "Pracuj.pl keeps showing Cloudflare checks in undetected Chrome. "
                    )
            else:
                challenge_started_at = None

            return False

        WebDriverWait(driver, self.selenium_wait_seconds).until(has_payload_or_block)
        self._raise_if_stopped()

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _raise_if_stopped(self) -> None:
        if self._is_stopped():
            raise PracujBlockedError("Pracuj.pl scan cancelled because the application is closing.")


def build_search_url(search: JobSearch) -> str:
    location = _slugify_location(search.location)
    keywords = quote(search.keywords.strip(), safe="")
    if is_poland_location(location):
        return (
            f"https://www.pracuj.pl/praca/{keywords};kw"
            if keywords
            else "https://www.pracuj.pl/praca"
        )
    location_part = f"{quote(location, safe='')};wp"
    if not keywords:
        return f"https://www.pracuj.pl/praca/{location_part}"
    return f"https://www.pracuj.pl/praca/{keywords};kw/{location_part}"


def _with_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["pn"] = str(page)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def parse_pracuj_offers(html_text: str, *, source_name: str = "pracuj") -> list[JobOffer]:
    """Parse Pracuj.pl offers from the Next.js payload embedded in HTML."""
    payload = _extract_next_data(html_text)
    grouped_offers = _iter_grouped_offers(payload)
    logger.info("Found %s grouped Pracuj.pl offers in page payload", len(grouped_offers))
    offers: list[JobOffer] = []

    for group in grouped_offers:
        nested_offers = group.get("offers")
        if not isinstance(nested_offers, list):
            nested_offers = [None]

        for nested_offer in nested_offers:
            if nested_offer is not None and not isinstance(nested_offer, dict):
                continue
            offers.append(_map_offer(group, nested_offer, source_name=source_name))

    return offers


def parse_pracuj_offer_detail(html_text: str) -> dict[str, Any]:
    """Parse richer offer data from a Pracuj.pl detail page payload."""
    payload = _extract_next_data(html_text)
    detail_fields = _extract_detail_fields(payload)
    description_text = _build_detail_description(detail_fields)
    logger.info(
        "Parsed Pracuj.pl detail payload: fields=%s description_chars=%s",
        len(detail_fields),
        len(description_text or ""),
    )
    return {
        "description_text": description_text,
        "salary": _clean_salary(_first_detail_value(detail_fields, ("salaryDisplayText",))),
        "raw": {
            "detail_fields": detail_fields,
        },
    }


def _extract_next_data(html_text: str) -> dict[str, Any]:
    return extract_next_data(
        html_text,
        missing_message="Pracuj.pl page does not contain __NEXT_DATA__ payload",
        type_message="Pracuj.pl __NEXT_DATA__ payload is not a JSON object",
    )


def _iter_grouped_offers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    queries = (
        payload.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
    )
    if not isinstance(queries, list):
        return []

    grouped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for query in queries:
        if not isinstance(query, dict) or not _is_offer_query(query):
            continue

        query_offers = query.get("state", {}).get("data", {}).get("groupedOffers", [])
        if not isinstance(query_offers, list):
            continue

        for offer in query_offers:
            if not isinstance(offer, dict):
                continue
            group_id = _clean(offer.get("groupId")) or _clean(offer.get("commonOfferId"))
            if group_id and group_id in seen_ids:
                continue
            if group_id:
                seen_ids.add(group_id)
            grouped.append(offer)

    return grouped


def _is_offer_query(query: dict[str, Any]) -> bool:
    query_key = query.get("queryKey")
    if not isinstance(query_key, list) or not query_key:
        return False
    return query_key[0] in {"jobOffers", "positionedJobOffers"}


def _map_offer(
    group: dict[str, Any],
    nested_offer: dict[str, Any] | None,
    *,
    source_name: str,
) -> JobOffer:
    nested_offer = nested_offer or {}
    url = _normalize_url(_clean(nested_offer.get("offerAbsoluteUri")))
    external_id = _clean(nested_offer.get("partitionId")) or _clean(group.get("commonOfferId"))
    location = _clean(nested_offer.get("displayWorkplace"))

    raw = {
        "group": group,
        "offer": nested_offer,
    }

    return JobOffer(
        source=source_name,
        external_id=external_id,
        title=_clean(group.get("jobTitle")) or "Untitled Pracuj.pl job",
        company=_clean(group.get("companyName")) or "Unknown company",
        location=location,
        url=url,
        salary=_clean(group.get("salaryDisplayText")),
        description_text=_clean(group.get("jobDescription")),
        posted_at=_clean(group.get("lastPublicated")),
        raw=raw,
    )


def _filter_by_location(offers: list[JobOffer], location: str) -> list[JobOffer]:
    if is_poland_location(location):
        return offers
    return [
        offer
        for offer in offers
        if not _is_multi_location_variant(offer) or _location_matches(offer.location, location)
    ]


def _is_multi_location_variant(offer: JobOffer) -> bool:
    group = offer.raw.get("group")
    if not isinstance(group, dict):
        return False
    nested_offers = group.get("offers")
    return isinstance(nested_offers, list) and len(nested_offers) > 1


def _location_matches(offer_location: str | None, requested_location: str) -> bool:
    offer_key = _location_key(offer_location)
    requested_key = _location_key(requested_location)
    if not offer_key or not requested_key:
        return False
    return requested_key in offer_key


def _location_key(location: str | None) -> str:
    if not location:
        return ""
    value = location.casefold().translate(str.maketrans("ł", "l"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def _extract_detail_fields(payload: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            _collect_section_like_dict(value, path, fields)
            for key, nested_value in value.items():
                key_text = str(key)
                nested_path = f"{path}.{key_text}" if path else key_text
                if _is_detail_text_key(key_text) and not _is_section_like_dict(nested_value):
                    field_text = _text_from_detail_value(nested_value)
                    if field_text:
                        fields.setdefault(nested_path, field_text)
                visit(nested_value, nested_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(payload, "")
    return fields


def _collect_section_like_dict(
    value: dict[str, Any],
    path: str,
    fields: dict[str, str],
) -> None:
    title = _clean(value.get("title") or value.get("header") or value.get("name"))
    if not title or _looks_like_noise(title) or _looks_like_seo_text(title):
        return

    content = _text_from_detail_value(
        value.get("items")
        or value.get("children")
        or value.get("content")
        or value.get("text")
        or value.get("description")
    )
    if content:
        fields.setdefault(f"{path}.{title}" if path else title, f"{title}: {content}")


def _is_section_like_dict(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    has_title = any(key in value for key in ("title", "header", "name"))
    has_content = any(
        key in value for key in ("items", "children", "content", "text", "description")
    )
    return has_title and has_content


def _is_detail_text_key(key: str) -> bool:
    lowered = key.casefold()
    if any(
        blocked in lowered
        for blocked in ("url", "uri", "link", "logo", "image", "banner", "id", "seo")
    ):
        return False
    markers = (
        "about",
        "benefit",
        "description",
        "dutie",
        "expectation",
        "project",
        "requirement",
        "responsibilit",
        "salary",
        "task",
        "technolog",
    )
    return any(marker in lowered for marker in markers)


def _text_from_detail_value(value: Any) -> str | None:
    pieces: list[str] = []

    def collect(item: Any, *, key: str | None = None) -> None:
        if key and _is_noise_key(key):
            return
        if isinstance(item, str):
            text = _clean(item)
            if text and not _looks_like_noise(text) and not _looks_like_seo_text(text):
                pieces.append(text)
        elif isinstance(item, dict):
            for nested_key, nested_value in item.items():
                collect(nested_value, key=str(nested_key))
        elif isinstance(item, list):
            for nested_item in item:
                collect(nested_item)

    collect(value)
    return _join_unique_text(pieces)


def _build_detail_description(detail_fields: dict[str, str]) -> str | None:
    descriptive_values = [
        value
        for key, value in detail_fields.items()
        if "salary" not in key.casefold() and len(value) >= 20 and not _looks_like_seo_text(value)
    ]
    return _join_unique_text(descriptive_values)


def _first_detail_value(detail_fields: dict[str, str], keys: tuple[str, ...]) -> str | None:
    key_markers = tuple(key.casefold() for key in keys)
    for key, value in detail_fields.items():
        lowered = key.casefold()
        if any(marker in lowered for marker in key_markers):
            return value
    return None


def _join_unique_text(values: list[str]) -> str | None:
    return join_unique((_clean(value) for value in values), separator="\n\n")


def _looks_like_noise(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith(("http://", "https://")):
        return True
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lowered):
        return True
    return lowered in {"true", "false", "null", "undefined"}


def _looks_like_seo_text(value: str) -> bool:
    lowered = value.casefold()
    return lowered.startswith(("oferta pracy ", "praca "))


def _is_noise_key(key: str) -> bool:
    lowered = key.casefold()
    return any(
        marker in lowered
        for marker in ("id", "url", "uri", "link", "logo", "image", "banner", "seo")
    )


def _clean_salary(value: str | None) -> str | None:
    cleaned = _clean(value)
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    return cleaned


def _normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    split_url = urlsplit(url)
    return urlunsplit((split_url.scheme, split_url.netloc, split_url.path, "", ""))


def _slugify_location(location: str) -> str:
    value = location.strip().casefold().translate(str.maketrans("ł", "l"))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = " ".join(value.split())
    return value or "polska"


def _clean(value: object) -> str | None:
    return clean_html_text(value)


def _clean_multiline(value: object) -> str | None:
    return clean_multiline(value)


def _has_cloudflare_challenge(page_source: str) -> bool:
    challenge_markers = (
        "cf-challenge",
        "challenge-platform",
        "checking your browser",
        "verify you are human",
        "sprawdzamy, czy jeste",
    )
    lowered = page_source.casefold()
    return any(marker in lowered for marker in challenge_markers)


def _extract_pracuj_offer_detail_from_dom(driver: uc.Chrome) -> dict[str, Any]:
    sections = driver.execute_script(
        r"""
const sectionTitles = [
  'Twój zakres obowiązków',
  'Twoj zakres obowiazkow',
  'Nasze wymagania',
  'Wymagania pracodawcy',
  'Mile widziane',
  'To oferujemy',
  'Pracodawca oferuje',
  'Benefity',
  'Etapy rekrutacji',
  'O projekcie',
  'Your responsibilities',
  'Our requirements',
  'Optional',
  'What we offer',
  'Benefits',
];

const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
const titleByLower = new Map(sectionTitles.map((title) => [title.toLowerCase(), title]));
const isVisible = (element) => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
};
const isSectionTitle = (value) => titleByLower.has(normalize(value).replace(/:$/, '').toLowerCase());
const canonicalTitle = (value) => titleByLower.get(normalize(value).replace(/:$/, '').toLowerCase());

const readSiblingText = (titleElement) => {
  const parts = [];
  let sibling = titleElement.nextElementSibling;
  let guard = 0;
  while (sibling && guard < 12) {
    const text = normalize(sibling.innerText || sibling.textContent);
    if (isSectionTitle(text)) break;
    if (text) parts.push(text);
    sibling = sibling.nextElementSibling;
    guard += 1;
  }
  return parts;
};

const readListItems = (titleElement) => {
  let container = titleElement.parentElement;
  let depth = 0;
  while (container && depth < 4) {
    const titlesInside = Array.from(container.querySelectorAll('*')).filter((element) => {
      return element !== titleElement && isVisible(element) && isSectionTitle(element.innerText || element.textContent);
    });
    const items = Array.from(container.querySelectorAll('li'))
      .map((element) => normalize(element.innerText || element.textContent))
      .filter(Boolean);
    if (items.length > 0 && titlesInside.length <= 1) return items;
    container = container.parentElement;
    depth += 1;
  }
  return [];
};

const elements = Array.from(document.body.querySelectorAll('h1,h2,h3,h4,p,span,div'));
const seenTitles = new Set();
const sections = [];

for (const element of elements) {
  if (!isVisible(element)) continue;
  const title = canonicalTitle(element.innerText || element.textContent);
  if (!title || seenTitles.has(title)) continue;

  const items = readListItems(element);
  const siblingParts = items.length === 0 ? readSiblingText(element) : [];
  const content = items.length > 0 ? items : siblingParts;
  if (content.length === 0) continue;

  seenTitles.add(title);
  sections.push({ title, items: content });
}

return sections;
        """
    )
    if not isinstance(sections, list):
        return {"description_text": None, "raw": {"dom_sections": []}}

    raw_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        title = _clean(section.get("title"))
        items = section.get("items")
        if not title or not isinstance(items, list):
            continue
        clean_items = [clean_item for item in items if (clean_item := _clean(item))]
        if not clean_items:
            continue
        raw_sections.append({"title": title, "items": clean_items})

    normalized_sections = _normalize_dom_sections(raw_sections)
    description_text = _format_description_sections(normalized_sections)
    logger.info(
        "Extracted %s Pracuj.pl detail sections from rendered DOM",
        len(normalized_sections),
    )
    return {
        "description_text": description_text,
        "raw": {"dom_sections": normalized_sections},
    }


def _normalize_dom_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    title_by_key: dict[str, str] = {}

    for section in sections:
        title = _clean(section.get("title"))
        items = section.get("items")
        if not title or not isinstance(items, list):
            continue

        for item in items:
            item_text = _clean(item)
            if not item_text:
                continue
            for target_title, target_items in _split_embedded_sections(title, item_text):
                key = _section_key(target_title)
                title_by_key.setdefault(key, target_title)
                grouped.setdefault(key, [])
                grouped[key].extend(target_items)

    normalized_sections = [
        {"title": title_by_key[key], "items": _dedupe_items(items)}
        for key, items in grouped.items()
    ]
    return sorted(
        normalized_sections,
        key=lambda section: _section_order_index(str(section["title"])),
    )


def _split_embedded_sections(title: str, item: str) -> list[tuple[str, list[str]]]:
    matches = [
        (match.start(), match.end(), section_title)
        for section_title in DETAIL_SECTION_ORDER
        if _section_key(section_title) != _section_key(title)
        if (match := re.search(rf"\b{re.escape(section_title)}\b", item, flags=re.IGNORECASE))
    ]
    if not matches:
        return [(title, _split_description_item(item))]

    matches.sort(key=lambda match: match[0])
    result: list[tuple[str, list[str]]] = []

    first_start = matches[0][0]
    before_first = item[:first_start]
    if before_first.strip():
        result.append((title, _split_description_item(before_first)))

    for index, (_, title_end, section_title) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(item)
        section_text = item[title_end:next_start]
        section_items = _split_description_item(section_text)
        if section_items:
            result.append((section_title, section_items))

    return result or [(title, _split_description_item(item))]


def _split_description_item(item: str) -> list[str]:
    cleaned = _clean(item.strip(" ,.;:-"))
    if not cleaned:
        return []

    parts = [part.strip(" ,;:-") for part in re.split(r"\.\s+", cleaned) if part.strip(" ,;:-")]
    return parts if len(parts) > 1 else [cleaned]


def _format_description_sections(sections: list[dict[str, Any]]) -> str | None:
    section_tuples: list[tuple[str, list[str]]] = []
    for section in sections:
        title = _clean(section.get("title"))
        items = section.get("items")
        if not title or not isinstance(items, list):
            continue
        clean_items = [clean_item for item in items if (clean_item := _clean(item))]
        if not clean_items:
            continue
        section_tuples.append((title, clean_items))
    return format_bullet_sections(section_tuples)


def _section_key(title: str) -> str:
    return _slugify_location(title)


def _section_order_index(title: str) -> int:
    key = _section_key(title)
    for index, section_title in enumerate(DETAIL_SECTION_ORDER):
        if _section_key(section_title) == key:
            return index
    return len(DETAIL_SECTION_ORDER)


def _dedupe_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_items: list[str] = []
    for item in items:
        cleaned = _clean(item)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(cleaned)
    return unique_items
