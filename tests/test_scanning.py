import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from job_sniffer.database import (
    ProfileSourceConfig,
    create_profile,
    db_session,
    list_offers_with_evaluations,
    save_profile_source_settings,
)
from job_sniffer.models import FilterValue, JobOffer, JobSearch
from job_sniffer.scanning import (
    ScanCallbacks,
    ScanPlan,
    SourceScanOutcome,
    format_scan_message,
    load_scan_plan,
    run_scan,
)
from job_sniffer.sources.base import Pacing, ScanHooks, ScanReport, scan_offers
from job_sniffer.sources.registry import SourceDefinition

NO_DELAYS = Pacing(page_delay_seconds=(0, 0), detail_delay_seconds=(0, 0))


@dataclass
class _Board:
    """A job board with one listing page; its pages can be broken to simulate failures."""

    key: str
    name: str
    offer_ids: list[str]
    listing_error: Exception | None = None
    broken_detail: str | None = None
    searches: list[JobSearch] = field(default_factory=list)

    def definition(self) -> SourceDefinition:
        return SourceDefinition(
            key=self.key,
            name=self.name,
            status="active",
            description="",
            create=lambda hooks: _BoardSource(self, hooks),
        )


class _BoardSource:
    def __init__(self, board: _Board, hooks: ScanHooks) -> None:
        self.source_name = board.key
        self._board = board
        self._hooks = hooks

    def search(self, search: JobSearch) -> ScanReport:
        self._board.searches.append(search)
        return scan_offers(
            self._fetch_page,
            self._enrich,
            search=search,
            hooks=self._hooks,
            pacing=NO_DELAYS,
            label=self._board.name,
        )

    def _fetch_page(self, page: int) -> list[JobOffer]:
        if self._board.listing_error is not None:
            raise self._board.listing_error
        if page > 1:
            return []
        return [
            JobOffer(
                source=self._board.key,
                external_id=offer_id,
                title=f"Oferta {offer_id}",
                company="Acme",
                location="Wrocław",
                url=f"https://example.invalid/{self._board.key}/{offer_id}",
                salary=None,
                description_text=None,
                posted_at=None,
                raw={},
            )
            for offer_id in self._board.offer_ids
        ]

    def _enrich(self, offer: JobOffer) -> JobOffer:
        if offer.external_id == self._board.broken_detail:
            raise RuntimeError("przeglądarka została zamknięta")
        return replace(offer, description_text="Opis stanowiska")


MakePlan = Callable[..., ScanPlan]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.sqlite"


@pytest.fixture
def make_plan(db_path: Path) -> MakePlan:
    def make(
        offer_limit: int | None = None, filters: dict[str, dict[str, FilterValue]] | None = None
    ) -> ScanPlan:
        with db_session(db_path) as session:
            profile = create_profile(session, "Backend", offer_limit=offer_limit)
            assert profile is not None
            profile_id = profile.id
            save_profile_source_settings(
                session,
                profile_id,
                [
                    ProfileSourceConfig(source_key=key, enabled=True, filters=source_filters)
                    for key, source_filters in (filters or {}).items()
                ],
            )
        plan = load_scan_plan(profile_id, db_path)
        assert plan is not None
        return plan

    return make


def _scan(
    plan: ScanPlan,
    boards: list[_Board],
    db_path: Path,
    stop_event: threading.Event | None = None,
) -> list[SourceScanOutcome]:
    callbacks = ScanCallbacks(
        on_source_started=lambda source: None,
        on_offer_saved=lambda source, offer: None,
        on_progress=lambda progress: None,
    )
    return run_scan(
        plan,
        [board.definition() for board in boards],
        stop_event=stop_event or threading.Event(),
        callbacks=callbacks,
        db_path=db_path,
    )


def _stored_offers(db_path: Path) -> list[tuple[str, str | None]]:
    with db_session(db_path) as session:
        return sorted(
            (offer.source, offer.external_id) for offer, _ in list_offers_with_evaluations(session)
        )


def test_failing_source_does_not_stop_the_remaining_sources(
    make_plan: MakePlan, db_path: Path
) -> None:
    broken = _Board("alpha", "Alfa", ["1"], listing_error=KeyError("nieznany format strony"))
    working = _Board("beta", "Beta", ["1", "2"])

    outcomes = _scan(make_plan(), [broken, working], db_path)

    assert _stored_offers(db_path) == [("beta", "1"), ("beta", "2")]
    assert format_scan_message(outcomes) == (
        "Skanowanie profilu zakończone. Beta: zapisano 2. Błędy: Alfa: 'nieznany format strony'."
    )


def test_offers_saved_before_a_failure_are_kept_and_reported(
    make_plan: MakePlan, db_path: Path
) -> None:
    board = _Board("alpha", "Alfa", ["1", "2", "3"], broken_detail="2")

    [outcome] = _scan(make_plan(), [board], db_path)

    assert _stored_offers(db_path) == [("alpha", "1")]
    assert [offer.external_id for offer, _ in outcome.saved] == ["1"]
    assert outcome.summary() == "Alfa: przerwane, zapisano 1"


def test_rescanning_skips_offers_already_stored(make_plan: MakePlan, db_path: Path) -> None:
    plan = make_plan()
    _scan(plan, [_Board("alpha", "Alfa", ["1", "2"])], db_path)

    [outcome] = _scan(plan, [_Board("alpha", "Alfa", ["1", "2", "3"])], db_path)

    assert _stored_offers(db_path) == [("alpha", "1"), ("alpha", "2"), ("alpha", "3")]
    assert outcome.summary() == "Alfa: zapisano 1 (znane: 2)"


def test_each_source_is_searched_with_its_profile_filters_and_the_profile_limit(
    make_plan: MakePlan, db_path: Path
) -> None:
    alpha = _Board("alpha", "Alfa", [])
    beta = _Board("beta", "Beta", [])
    plan = make_plan(offer_limit=5, filters={"alpha": {"location": "Wrocław"}})

    _scan(plan, [alpha, beta], db_path)

    assert alpha.searches == [JobSearch(filters={"location": "Wrocław"}, limit=5)]
    assert beta.searches == [JobSearch(filters={}, limit=5)]


def test_summary_lists_known_failed_and_truncated_scans() -> None:
    source = _Board("alpha", "Alfa", []).definition()
    report = ScanReport(pages=20, known=3, failed=1, saved=2, truncated=True)

    assert SourceScanOutcome(source=source, report=report).summary() == (
        "Alfa: zapisano 2 (znane: 3, nieudane: 1, niepełny — limit 20 stron)"
    )
