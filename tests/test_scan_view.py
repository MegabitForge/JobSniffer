import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from job_sniffer.config import AppConfig
from job_sniffer.database import (
    DB_PATH,
    ProfileSourceConfig,
    create_profile,
    db_session,
    list_offers_with_evaluations,
    save_profile_source_settings,
)
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources.base import EnrichedOfferHandler
from job_sniffer.sources.registry import ACTIVE_SOURCES
from job_sniffer.ui import scan_view
from job_sniffer.ui.scan_view import ScanView

MakeView = Callable[[set[str]], ScanView]


class _FakeSource:
    """Scraper stand-in that saves one offer through the real handler, or fails."""

    def __init__(
        self, source_key: str, error: Exception | None, handler: EnrichedOfferHandler
    ) -> None:
        self.source_name = source_key
        self._error = error
        self._handler = handler

    def search(self, search: JobSearch) -> list[JobOffer]:
        if self._error is not None:
            raise self._error
        offer = JobOffer(
            source=self.source_name,
            external_id=f"{self.source_name}-1",
            title="Python Developer",
            company="Acme",
            location="Wrocław",
            url=f"https://example.invalid/{self.source_name}/1",
            salary=None,
            description_text="Opis",
            posted_at=None,
            raw={},
        )
        return [offer] if self._handler(offer) else []


@dataclass
class _Scrapers:
    errors: dict[str, Exception] = field(default_factory=dict)
    scanned: list[str] = field(default_factory=list)


@pytest.fixture
def scrapers(monkeypatch: pytest.MonkeyPatch) -> _Scrapers:
    """Replace the browser/network scraper factory; the DB and scan flow stay real."""
    scrapers = _Scrapers()

    def fake_create_source(source_key: str, **kwargs: Any) -> _FakeSource:
        scrapers.scanned.append(source_key)
        return _FakeSource(
            source_key, scrapers.errors.get(source_key), kwargs["enriched_offer_handler"]
        )

    monkeypatch.setattr(scan_view, "_create_source", fake_create_source)
    return scrapers


@pytest.fixture
def status_bar() -> MagicMock:
    return MagicMock()


@pytest.fixture
def offers_view() -> MagicMock:
    return MagicMock()


@pytest.fixture
def make_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_bar: MagicMock,
    offers_view: MagicMock,
) -> Iterator[MakeView]:
    """Build a ScanView over a fresh CWD-relative app DB holding one "Backend" profile."""
    monkeypatch.chdir(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1)

    def make(enabled_sources: set[str]) -> ScanView:
        with db_session(DB_PATH) as session:
            profile = create_profile(session, "Backend")
            assert profile is not None
            save_profile_source_settings(
                session,
                profile.id,
                [
                    ProfileSourceConfig(
                        source_key=source.key, enabled=source.key in enabled_sources, filters={}
                    )
                    for source in ACTIVE_SOURCES
                ],
            )
        return ScanView(
            MagicMock(),
            AppConfig(auto_evaluate=False),
            MagicMock(),
            threading.Event(),
            executor,
            offers_view,
            status_bar,
            on_scan_finished=MagicMock(),
        )

    with executor:
        yield make


def _saved_offer_sources() -> list[str]:
    with db_session(DB_PATH) as session:
        return sorted(offer.source for offer, _ in list_offers_with_evaluations(session))


def _last_status(status_bar: MagicMock) -> str:
    return str(status_bar.set_scan.call_args.args[0])


def test_failing_source_does_not_stop_the_remaining_sources(
    make_view: MakeView, scrapers: _Scrapers, status_bar: MagicMock
) -> None:
    view = make_view({"pracuj", "olx"})
    scrapers.errors["pracuj"] = KeyError("nieoczekiwany format strony")

    asyncio.run(view._on_scan_click(None))

    assert scrapers.scanned == ["pracuj", "olx"]
    assert _saved_offer_sources() == ["olx"]
    assert "OLX: zapisano 1/1" in _last_status(status_bar)
    assert "Błędy: Pracuj.pl:" in _last_status(status_bar)


def test_scan_button_is_enabled_again_after_a_failing_source(
    make_view: MakeView, scrapers: _Scrapers
) -> None:
    view = make_view({"pracuj"})
    scrapers.errors["pracuj"] = KeyError("nieoczekiwany format strony")

    asyncio.run(view._on_scan_click(None))

    assert view._scan_button.disabled is False


def test_scan_button_is_enabled_again_after_an_unexpected_ui_error(
    make_view: MakeView, scrapers: _Scrapers, offers_view: MagicMock
) -> None:
    view = make_view({"olx"})
    offers_view.add_new_offers.side_effect = RuntimeError("błąd widoku ofert")

    with pytest.raises(RuntimeError, match="błąd widoku ofert"):
        asyncio.run(view._on_scan_click(None))

    assert view._scan_button.disabled is False
    assert view._progress_bar.value == 0.0


def test_source_disabled_in_profile_is_not_scanned_when_selected(
    make_view: MakeView, scrapers: _Scrapers, status_bar: MagicMock
) -> None:
    view = make_view({"olx"})
    view._source_dropdown.value = "pracuj"

    asyncio.run(view._on_scan_click(None))

    assert scrapers.scanned == []
    assert _last_status(status_bar) == "Źródło Pracuj.pl jest wyłączone w profilu „Backend”."
