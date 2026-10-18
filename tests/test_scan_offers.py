import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest

from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources.base import (
    Pacing,
    ScanCancelledError,
    ScanHooks,
    ScanReport,
    scan_offers,
)
from job_sniffer.sources.bulldogjob import BulldogjobSource

NO_DELAYS = Pacing(page_delay_seconds=(0, 0), detail_delay_seconds=(0, 0))


def _offer(offer_id: str) -> JobOffer:
    return JobOffer(
        source="board",
        external_id=offer_id,
        title=f"Oferta {offer_id}",
        company="Acme",
        location="Wrocław",
        url=f"https://example.invalid/{offer_id}",
        salary=None,
        description_text=None,
        posted_at=None,
        raw={},
    )


@dataclass
class _Board:
    """A job board serving listing pages of offer ids; detail pages add a description."""

    pages: dict[int, list[str]]
    known: set[str] = field(default_factory=set)
    broken_details: set[str] = field(default_factory=set)
    fetched_pages: list[int] = field(default_factory=list)
    fetched_details: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    on_save: Callable[[JobOffer], None] = lambda offer: None

    def fetch_page(self, page: int) -> list[JobOffer]:
        self.fetched_pages.append(page)
        return [_offer(offer_id) for offer_id in self.pages.get(page, [])]

    def enrich(self, offer: JobOffer) -> JobOffer | None:
        assert offer.external_id is not None
        self.fetched_details.append(offer.external_id)
        if offer.external_id in self.broken_details:
            return None
        return replace(offer, description_text="Opis stanowiska")

    def save(self, offer: JobOffer) -> bool:
        assert offer.external_id is not None
        if offer.external_id in self.known:
            return False
        self.saved.append(offer.external_id)
        self.on_save(offer)
        return True

    def scan(
        self,
        *,
        limit: int | None = None,
        max_pages: int = 20,
        accept: Callable[[JobOffer], bool] | None = None,
        known_before_listing: Iterable[str] | None = None,
    ) -> ScanReport:
        listing_known = self.known if known_before_listing is None else set(known_before_listing)
        return scan_offers(
            self.fetch_page,
            self.enrich,
            search=JobSearch(filters={}, limit=limit),
            hooks=ScanHooks(
                stop_event=self.stop_event,
                duplicate_checker=lambda offer: offer.external_id in listing_known,
                enriched_offer_handler=self.save,
            ),
            pacing=replace(NO_DELAYS, max_pages=max_pages),
            label="board",
            accept=accept,
        )


def test_scan_without_limit_reads_all_pages_and_saves_only_new_offers() -> None:
    board = _Board(pages={1: ["1", "2", "3"], 2: ["4", "5"]}, known={"1", "4"})

    report = board.scan()

    assert board.fetched_pages == [1, 2, 3]
    assert board.fetched_details == ["2", "3", "5"]
    assert board.saved == ["2", "3", "5"]
    assert report == ScanReport(pages=3, listed=5, known=2, saved=3)


def test_board_repeating_its_last_page_ends_pagination() -> None:
    last_page = ["3", "4"]
    board = _Board(pages={1: ["1", "2"], **{page: last_page for page in range(2, 21)}})

    report = board.scan()

    assert board.fetched_pages == [1, 2, 3]
    assert board.saved == ["1", "2", "3", "4"]
    assert report.truncated is False


def test_offer_repeated_on_one_page_is_saved_once() -> None:
    board = _Board(pages={1: ["1", "1", "2"]})

    report = board.scan()

    assert board.saved == ["1", "2"]
    assert report.listed == 2


def test_offers_without_a_usable_detail_page_are_reported_as_failed() -> None:
    board = _Board(pages={1: ["1", "2", "3"]}, broken_details={"2"})

    report = board.scan()

    assert board.saved == ["1", "3"]
    assert (report.saved, report.failed) == (2, 1)


def test_reaching_max_pages_marks_the_scan_as_truncated() -> None:
    board = _Board(pages={page: [str(page)] for page in range(1, 10)})

    report = board.scan(max_pages=3)

    assert board.fetched_pages == [1, 2, 3]
    assert board.saved == ["1", "2", "3"]
    assert (report.pages, report.truncated) == (3, True)


def test_limit_counts_only_offers_not_stored_yet() -> None:
    board = _Board(pages={1: ["1", "2", "3"], 2: ["4", "5", "6"]}, known={"1", "2"})

    report = board.scan(limit=3)

    assert board.fetched_pages == [1, 2]
    assert board.saved == ["3", "4", "5"]
    assert report.truncated is False


def test_offers_rejected_by_the_source_filter_are_skipped_without_detail_fetch() -> None:
    board = _Board(pages={1: ["1", "2", "3"]})

    report = board.scan(accept=lambda offer: offer.external_id != "2")

    assert board.fetched_details == ["1", "3"]
    assert (report.filtered_out, report.saved) == (1, 2)


def test_duplicate_detected_only_when_saving_is_reported_as_known() -> None:
    board = _Board(pages={1: ["1", "2"]}, known={"2"})

    report = board.scan(known_before_listing=set())

    assert board.saved == ["1"]
    assert (report.known, report.saved) == (1, 1)


def test_stopping_the_scan_skips_the_remaining_detail_pages() -> None:
    board = _Board(pages={1: ["1", "2", "3"]})
    board.on_save = lambda offer: board.stop_event.set()

    with pytest.raises(ScanCancelledError):
        board.scan()

    assert board.fetched_details == ["1"]
    assert board.saved == ["1"]


def _bulldogjob_listing(job_ids: list[str]) -> str:
    payload = {"props": {"pageProps": {"jobs": [{"id": job_id} for job_id in job_ids]}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def _bulldogjob_detail(title: str) -> str:
    posting = {"@type": "JobPosting", "title": title, "description": "<p>Opis stanowiska</p>"}
    return f'<script type="application/ld+json">{json.dumps(posting)}</script>'


def test_bulldogjob_scan_saves_new_offers_from_every_listing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {1: ["1", "2"], 2: ["3"]}

    def fake_get(url: str, **_: Any) -> httpx.Response:
        if "/s/" in url:
            page = int(url.rsplit("page,", 1)[1])
            body = _bulldogjob_listing(pages.get(page, []))
        else:
            body = _bulldogjob_detail(f"Oferta {url.rsplit('/', 1)[1]}")
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    saved: list[JobOffer] = []

    def save(offer: JobOffer) -> bool:
        saved.append(offer)
        return True

    hooks = ScanHooks(
        duplicate_checker=lambda offer: offer.external_id == "2", enriched_offer_handler=save
    )

    report = BulldogjobSource(hooks, pacing=NO_DELAYS).search(JobSearch(filters={}))

    assert [offer.title for offer in saved] == ["Oferta 1", "Oferta 3"]
    assert all("Opis stanowiska" in (offer.description_text or "") for offer in saved)
    assert (report.known, report.saved) == (1, 2)
