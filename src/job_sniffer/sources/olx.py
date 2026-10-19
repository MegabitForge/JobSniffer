import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import undetected_chromedriver as uc  # type: ignore[import-untyped]
from selectolax.parser import HTMLParser
from selenium.common.exceptions import WebDriverException

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources._common import is_poland_location
from job_sniffer.sources._job_posting import (
    extract_job_posting,
    format_hiring_organization,
    format_schema_salary,
    has_ancestor_tag,
)
from job_sniffer.sources._text import (
    clean_multiline,
    clean_text,
    format_bullet_sections,
    html_to_text,
    join_unique,
)
from job_sniffer.sources.base import (
    DEFAULT_PACING,
    Pacing,
    ScanHooks,
    ScanReport,
    scan_offers,
)
from job_sniffer.sources.browser import (
    BrowserClosedError,
    BrowserFetchError,
    fetch_with_existing_browser,
    start_undetected_chrome,
)
from job_sniffer.sources.filters import KEYWORDS_FILTER, LOCATION_FILTER, SourceFilter

logger = logging.getLogger(__name__)


FILTERS: tuple[SourceFilter, ...] = (KEYWORDS_FILTER, LOCATION_FILTER)


class OlxJobSource:
    source_name = "olx"

    def __init__(
        self,
        hooks: ScanHooks | None = None,
        *,
        pacing: Pacing = DEFAULT_PACING,
        chrome_user_data_dir: str = ".olx-profile",
        timeout_seconds: float = 90.0,
        page_load_timeout_seconds: float = 5.0,
    ) -> None:
        self.hooks = hooks or ScanHooks()
        self.pacing = pacing
        self.chrome_user_data_dir = chrome_user_data_dir
        self.timeout_seconds = timeout_seconds
        self.page_load_timeout_seconds = page_load_timeout_seconds

    def search(self, search: JobSearch) -> ScanReport:
        logger.info("Starting OLX search: filters=%s limit=%s", search.filters, search.limit)
        url = build_search_url(search)
        logger.info("Built OLX search URL: %s", url)
        driver = start_undetected_chrome(self.chrome_user_data_dir)
        try:
            return scan_offers(
                lambda page: parse_olx_offers(
                    self._fetch_html(driver, _with_page(url, page), _browser_has_olx_offers)
                ),
                lambda offer: self._enrich_offer_from_detail(driver, offer),
                search=search,
                hooks=self.hooks,
                pacing=self.pacing,
                label="OLX",
            )
        finally:
            logger.info("Closing OLX browser session")
            try:
                driver.quit()
            except WebDriverException:
                logger.info("OLX browser session was already closed")

    def _fetch_html(
        self, driver: uc.Chrome, url: str, is_ready: Callable[[uc.Chrome], bool]
    ) -> str:
        logger.info("Fetching OLX page: %s", url)
        html_text = fetch_with_existing_browser(
            driver,
            url,
            timeout_seconds=self.timeout_seconds,
            page_load_timeout_seconds=self.page_load_timeout_seconds,
            return_partial_on_timeout=True,
            wait_until=lambda browser: self.hooks.is_stopped() or is_ready(browser),
        )
        # The wait ends early on stop; the partial page must not be parsed.
        self.hooks.raise_if_stopped()
        return html_text

    def _enrich_offer_from_detail(self, driver: uc.Chrome, offer: JobOffer) -> JobOffer | None:
        if not offer.url:
            logger.warning("Skipping OLX offer without a detail URL: %s", offer.title)
            return None

        try:
            detail = parse_olx_offer_detail(
                self._fetch_html(driver, offer.url, _browser_has_olx_detail)
            )
        except BrowserClosedError:
            raise
        except BrowserFetchError, TypeError, ValueError, WebDriverException:
            logger.exception("Could not enrich OLX offer from detail page: %s", offer.url)
            return None

        raw_detail = detail.get("raw")
        description_text = detail.get("description_text")
        cleaned_description = (
            clean_multiline(description_text) if isinstance(description_text, str) else None
        )
        if not cleaned_description:
            logger.warning("No description found on OLX detail page: %s", offer.url)
            return None
        return replace(
            offer,
            title=_clean(detail.get("title")) or offer.title,
            company=_clean(detail.get("company")) or offer.company,
            location=_clean(detail.get("location")) or offer.location,
            salary=offer.salary or _clean(detail.get("salary")),
            description_text=cleaned_description,
            posted_at=_clean(detail.get("posted_at")) or offer.posted_at,
            raw={**offer.raw, "detail": raw_detail if isinstance(raw_detail, dict) else {}},
        )


def build_search_url(search: JobSearch) -> str:
    keywords = search.text("keywords")
    location = search.text("location")
    keyword = quote("-".join(keywords.casefold().split()), safe="")
    if not is_poland_location(location):
        location_slug = _slugify_location(location)
        if not keyword:
            return f"https://www.olx.pl/praca/{quote(location_slug, safe='')}/"
        return f"https://www.olx.pl/praca/{quote(location_slug, safe='')}/q-{keyword}/"
    if not keyword:
        return "https://www.olx.pl/praca/"
    return f"https://www.olx.pl/praca/q-{keyword}/"


def _with_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


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
    posting = extract_job_posting(html_text, source_label="OLX")
    return {
        "title": _clean(posting.get("title")),
        "company": format_hiring_organization(posting.get("hiringOrganization")),
        "location": _format_job_location(posting.get("jobLocation")),
        "salary": format_schema_salary(posting.get("baseSalary")),
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
        if has_ancestor_tag(node, "li"):
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
