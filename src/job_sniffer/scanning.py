"""Profile scans: run the selected sources and store new offers, independently of the UI."""

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from job_sniffer.database import (
    DB_PATH,
    ProfileRecord,
    ProfileSourceConfig,
    db_session,
    get_profile,
    get_profile_source_settings,
    offer_exists,
    save_offer,
)
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources.base import ProgressHandler, ScanHooks, ScanReport
from job_sniffer.sources.registry import SourceDefinition

logger = logging.getLogger(__name__)

SavedOffer = tuple[JobOffer, int]


@dataclass(frozen=True)
class ScanPlan:
    """A profile together with its per-source settings."""

    profile: ProfileRecord
    settings: dict[str, ProfileSourceConfig]

    def search_for(self, source_key: str) -> JobSearch:
        stored = self.settings.get(source_key)
        if stored is None:
            return JobSearch(filters={})
        return JobSearch(filters=stored.filters, limit=stored.offer_limit)


def load_scan_plan(profile_id: int, db_path: Path = DB_PATH) -> ScanPlan | None:
    """Load a profile with its source settings; None when the profile does not exist."""
    with db_session(db_path) as session:
        profile = get_profile(session, profile_id)
        if profile is None:
            return None
        return ScanPlan(profile=profile, settings=get_profile_source_settings(session, profile_id))


@dataclass(frozen=True)
class ScanCallbacks:
    """Hooks for reporting a running scan; all are called from the scan worker thread."""

    on_source_started: Callable[[SourceDefinition], None]
    on_offer_saved: Callable[[SourceDefinition, JobOffer], None]
    on_progress: ProgressHandler


@dataclass(frozen=True)
class SourceScanOutcome:
    """Result of scanning one source; a failed scan may still have saved some offers."""

    source: SourceDefinition
    saved: list[SavedOffer] = field(default_factory=list)
    report: ScanReport | None = None
    error: Exception | None = None

    def summary(self) -> str:
        """Polish one-line summary for the status bar."""
        if self.report is None:
            if self.saved:
                return f"{self.source.name}: przerwane, zapisano {len(self.saved)}"
            return f"{self.source.name}: {self.error}"
        details = [
            f"{label}: {count}"
            for label, count in (("znane", self.report.known), ("nieudane", self.report.failed))
            if count
        ]
        if self.report.truncated:
            details.append(f"niepełny — limit {self.report.pages} stron")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{self.source.name}: zapisano {self.report.saved}{suffix}"


def run_scan(
    plan: ScanPlan,
    sources: Sequence[SourceDefinition],
    *,
    stop_event: threading.Event,
    callbacks: ScanCallbacks,
    db_path: Path = DB_PATH,
) -> list[SourceScanOutcome]:
    """Scan sources one after another; a failing source does not stop the remaining ones."""
    outcomes: list[SourceScanOutcome] = []
    for source in sources:
        if stop_event.is_set():
            break
        callbacks.on_source_started(source)
        outcomes.append(
            _scan_source(source, plan.search_for(source.key), stop_event, callbacks, db_path)
        )
    return outcomes


def format_scan_message(outcomes: Sequence[SourceScanOutcome]) -> str:
    """Polish status bar message summarising a whole profile scan."""
    summaries = [outcome.summary() for outcome in outcomes if outcome.report or outcome.saved]
    errors = [outcome.summary() for outcome in outcomes if not (outcome.report or outcome.saved)]
    message = "Skanowanie profilu zakończone."
    if summaries:
        message += " " + "; ".join(summaries) + "."
    if errors:
        message += " Błędy: " + "; ".join(errors) + "."
    return message


def _scan_source(
    source: SourceDefinition,
    search: JobSearch,
    stop_event: threading.Event,
    callbacks: ScanCallbacks,
    db_path: Path,
) -> SourceScanOutcome:
    logger.info("Scanning source=%s filters=%s", source.key, search.filters)
    saved: list[SavedOffer] = []
    try:
        if source.create is None:
            raise ValueError(f"Source {source.key} cannot be scanned")
        with db_session(db_path) as session:

            def save_enriched_offer(offer: JobOffer) -> bool:
                inserted_id = save_offer(session, offer)
                if inserted_id is None:
                    logger.info(
                        "Skipped duplicate after enrich: %s - %s", offer.title, offer.company
                    )
                    return False
                logger.info("Saved offer: %s - %s (id=%s)", offer.title, offer.company, inserted_id)
                saved.append((offer, inserted_id))
                callbacks.on_offer_saved(source, offer)
                return True

            hooks = ScanHooks(
                stop_event=stop_event,
                duplicate_checker=lambda offer: offer_exists(session, offer),
                enriched_offer_handler=save_enriched_offer,
                progress_handler=callbacks.on_progress,
            )
            hooks.raise_if_stopped()
            report = source.create(hooks).search(search)
    except Exception as error:
        # One failing source must not abort the remaining ones.
        logger.exception("Scan failed: source=%s, saved before failure=%s", source.key, len(saved))
        return SourceScanOutcome(source=source, saved=saved, error=error)
    return SourceScanOutcome(source=source, saved=saved, report=report)
