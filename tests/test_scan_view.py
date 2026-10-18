import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from job_sniffer.config import AppConfig
from job_sniffer.database import (
    ProfileSourceConfig,
    create_profile,
    db_session,
    save_profile_source_settings,
)
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources.base import ScanHooks, ScanReport
from job_sniffer.sources.registry import SourceDefinition
from job_sniffer.ui.scan_view import ScanView

MakeView = Callable[[set[str]], ScanView]


@dataclass
class _Scrapers:
    """Scraper stand-ins: each source saves one offer, or raises its configured error."""

    errors: dict[str, Exception] = field(default_factory=dict)
    scanned: list[str] = field(default_factory=list)

    def definition(self, key: str, name: str) -> SourceDefinition:
        return SourceDefinition(
            key=key,
            name=name,
            status="active",
            description="",
            create=lambda hooks: _FakeSource(self, key, hooks),
        )


class _FakeSource:
    def __init__(self, scrapers: _Scrapers, key: str, hooks: ScanHooks) -> None:
        self.source_name = key
        self._scrapers = scrapers
        self._hooks = hooks

    def search(self, search: JobSearch) -> ScanReport:
        self._scrapers.scanned.append(self.source_name)
        if error := self._scrapers.errors.get(self.source_name):
            raise error
        offer = JobOffer(
            source=self.source_name,
            external_id="1",
            title="Python Developer",
            company="Acme",
            location="Wrocław",
            url=f"https://example.invalid/{self.source_name}/1",
            salary=None,
            description_text="Opis",
            posted_at=None,
            raw={},
        )
        assert self._hooks.enriched_offer_handler is not None
        self._hooks.enriched_offer_handler(offer)
        return ScanReport(pages=1, listed=1, saved=1)


@pytest.fixture
def scrapers() -> _Scrapers:
    return _Scrapers()


@pytest.fixture
def status_bar() -> MagicMock:
    return MagicMock()


@pytest.fixture
def offers_view() -> MagicMock:
    return MagicMock()


@pytest.fixture
def make_view(
    tmp_path: Path, scrapers: _Scrapers, status_bar: MagicMock, offers_view: MagicMock
) -> Iterator[MakeView]:
    """Build a ScanView over a fresh DB holding one "Backend" profile with the given sources."""
    db_path = tmp_path / "app.sqlite"
    sources = [scrapers.definition("alpha", "Alfa"), scrapers.definition("beta", "Beta")]
    executor = ThreadPoolExecutor(max_workers=1)

    def make(enabled_sources: set[str]) -> ScanView:
        with db_session(db_path) as session:
            profile = create_profile(session, "Backend")
            assert profile is not None
            save_profile_source_settings(
                session,
                profile.id,
                [
                    ProfileSourceConfig(
                        source_key=source.key, enabled=source.key in enabled_sources, filters={}
                    )
                    for source in sources
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
            sources=sources,
            db_path=db_path,
        )

    with executor:
        yield make


def _last_status(status_bar: MagicMock) -> str:
    return str(status_bar.set_scan.call_args.args[0])


def test_scan_reports_saved_offers_and_failing_sources(
    make_view: MakeView, scrapers: _Scrapers, status_bar: MagicMock, offers_view: MagicMock
) -> None:
    view = make_view({"alpha", "beta"})
    scrapers.errors["alpha"] = KeyError("nieznany format strony")

    asyncio.run(view._on_scan_click(None))

    assert scrapers.scanned == ["alpha", "beta"]
    saved_offers = offers_view.add_new_offers.call_args.args[0]
    assert [offer.source for offer, _ in saved_offers] == ["beta"]
    assert _last_status(status_bar) == (
        "Skanowanie profilu zakończone. Beta: zapisano 1. Błędy: Alfa: 'nieznany format strony'."
    )


def test_scan_button_is_enabled_again_after_an_unexpected_ui_error(
    make_view: MakeView, offers_view: MagicMock
) -> None:
    view = make_view({"alpha"})
    offers_view.add_new_offers.side_effect = RuntimeError("błąd widoku ofert")

    with pytest.raises(RuntimeError, match="błąd widoku ofert"):
        asyncio.run(view._on_scan_click(None))

    assert view._scan_button.disabled is False
    assert view._progress_bar.value == 0.0


def test_source_disabled_in_profile_is_not_scanned_when_selected(
    make_view: MakeView, scrapers: _Scrapers, status_bar: MagicMock
) -> None:
    view = make_view({"beta"})
    view._source_dropdown.value = "alpha"

    asyncio.run(view._on_scan_click(None))

    assert scrapers.scanned == []
    assert _last_status(status_bar) == "Źródło Alfa jest wyłączone w profilu „Backend”."
