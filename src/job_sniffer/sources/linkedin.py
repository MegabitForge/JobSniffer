from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from linkedin_jobs_scraper import LinkedinScraper  # type: ignore[import-untyped]
from linkedin_jobs_scraper.events import EventData, Events  # type: ignore[import-untyped]
from linkedin_jobs_scraper.query import Query, QueryOptions  # type: ignore[import-untyped]

from job_sniffer.models import JobOffer, JobSearch


class LinkedInJobSource:
    source_name = "linkedin"

    def __init__(
        self,
        chrome_user_data_dir: Path | None = None,
        *,
        headless: bool = True,
        slow_mo: float = 0.8,
    ) -> None:
        self.chrome_user_data_dir = chrome_user_data_dir
        self.headless = headless
        self.slow_mo = slow_mo

    def search(self, search: JobSearch) -> list[JobOffer]:
        """Run a LinkedIn search and return normalized offers."""
        offers: list[JobOffer] = []
        errors: list[str] = []
        invalid_session = False

        def on_data(data: EventData) -> None:
            offers.append(map_event_data(data))

        def on_error(message: str) -> None:
            errors.append(message)

        def on_invalid_session() -> None:
            nonlocal invalid_session
            invalid_session = True

        scraper_options: dict[str, Any] = {
            "headless": self.headless,
            "max_workers": 1,
            "slow_mo": self.slow_mo,
            "adaptive_slow_mo": True,
        }
        if self.chrome_user_data_dir is not None:
            scraper_options["chrome_user_data_dir"] = str(self.chrome_user_data_dir)

        scraper = LinkedinScraper(**scraper_options)
        scraper.on(Events.DATA, on_data)
        scraper.on(Events.ERROR, on_error)
        scraper.on(Events.INVALID_SESSION, on_invalid_session)

        query = Query(
            query=search.keywords,
            options=QueryOptions(
                locations=[search.location],
                limit=search.limit,
                apply_link=True,
            ),
        )
        scraper.run([query])

        if invalid_session:
            raise RuntimeError(
                "LinkedIn session is invalid. Run: uv run lijs login --chrome-user-data-dir "
                f"{self.chrome_user_data_dir or '.linkedin-profile'}"
            )
        if errors:
            raise RuntimeError(errors[-1])

        return offers


def map_event_data(data: EventData) -> JobOffer:
    """Map linkedin-jobs-scraper event data to the app model."""
    raw = _raw_dict(data)
    description_html = _clean(getattr(data, "description_html", ""))
    description_text = _clean(getattr(data, "description", ""))

    return JobOffer(
        source=LinkedInJobSource.source_name,
        external_id=_clean(getattr(data, "job_id", "")),
        title=_clean(getattr(data, "title", "")) or "Untitled LinkedIn job",
        company=_clean(getattr(data, "company", "")) or "Unknown company",
        location=_clean(getattr(data, "place", "")) or _clean(getattr(data, "location", "")),
        url=_clean(getattr(data, "link", "")),
        apply_url=_clean(getattr(data, "apply_link", "")),
        salary=_clean(getattr(data, "salary", "")),
        applicant_count=_clean(getattr(data, "applicant_count", "")),
        description_text=description_text or html_to_text(description_html),
        posted_at=_clean(getattr(data, "date", "")) or _clean(getattr(data, "date_text", "")),
        raw=raw,
    )


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None

    parser = _LinkedInHTMLTextParser()
    parser.feed(html)
    parser.close()
    text = parser.text()
    return text or None


def _raw_dict(data: EventData) -> dict[str, Any]:
    as_dict = getattr(data, "_asdict", None)
    if callable(as_dict):
        result = as_dict()
        if isinstance(result, Mapping):
            return dict(result)
    return dict(vars(data))


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class _LinkedInHTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "ul", "ol", "h1", "h2", "h3"}:
            self._newline()
        if tag == "li":
            self._parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li", "ul", "ol", "h1", "h2", "h3"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self._parts.append(text)
            self._parts.append(" ")

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _newline(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")
