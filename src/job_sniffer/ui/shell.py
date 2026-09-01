from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import flet as ft
from sqlalchemy.exc import SQLAlchemyError

from job_sniffer.database import SaveStats, connect, init_db, save_offer
from job_sniffer.models import JobOffer, JobSearch
from job_sniffer.sources.pracuj import PracujBlockedError, PracujJobSource
from job_sniffer.sources.registry import SOURCE_DEFINITIONS

logger = logging.getLogger(__name__)


def build_shell(page: ft.Page) -> ft.Control:
    """Build the initial application view."""
    keywords = ft.TextField(
        label="Keywords",
        value="python developer",
    )
    location = ft.TextField(
        label="Location",
        value="Poland",
    )
    limit = ft.TextField(
        label="Limit",
        value="5",
        keyboard_type=ft.KeyboardType.NUMBER,
    )
    status = ft.Text(
        "Ready to scan Pracuj.pl.",
    )
    results = ft.Column(spacing=8)
    source_notes = ft.Column(
        controls=[
            ft.Text("Sources", size=18, weight=ft.FontWeight.BOLD),
            *[
                ft.Text(
                    f"{source.name} [{source.status}]",
                )
                for source in SOURCE_DEFINITIONS
            ],
        ],
        spacing=6,
    )
    scan_button = ft.ElevatedButton(
        "Scan Pracuj.pl",
    )

    async def on_scan_click(_: Any) -> None:
        logger.info("Scan initiated")
        keywords_value = (keywords.value or "").strip()
        location_value = (location.value or "").strip()

        try:
            limit_value = int(limit.value or "0")
        except ValueError:
            limit_value = 0

        if not keywords_value or not location_value or limit_value < 1:
            logger.warning(
                "Invalid scan form values: keywords=%r location=%r limit=%r",
                keywords.value,
                location.value,
                limit.value,
            )
            status.value = "Provide keywords, location, and a positive limit."
            page.update()
            return

        scan_button.disabled = True
        status.value = "Scanning Pracuj.pl..."
        results.controls.clear()
        page.update()

        search = JobSearch(keywords=keywords_value, location=location_value, limit=limit_value)
        logger.info("Starting Pracuj.pl scan in background")
        try:
            inserted_offers, stats = await asyncio.to_thread(_scan_pracuj, search)
        except (
            PracujBlockedError,
            SQLAlchemyError,
            ValueError,
            TypeError,
        ) as error:
            logger.exception("Pracuj.pl scan failed")
            status.value = f"Pracuj.pl scan failed: {error}"
        else:
            logger.info(
                "Pracuj.pl scan finished: seen=%s inserted=%s duplicates=%s",
                stats.seen,
                stats.inserted,
                stats.duplicates,
            )
            _show_results(inserted_offers, stats, action="Fetched")
        finally:
            scan_button.disabled = False
            page.update()

    def _scan_pracuj(search: JobSearch) -> tuple[list[JobOffer], SaveStats]:
        logger.info("Fetching offers from Pracuj.pl")
        offers = PracujJobSource().search(search)
        logger.info("Fetched %s offers from Pracuj.pl, saving to db", len(offers))
        inserted_offers, stats = _save_pracuj_offers(offers)
        return inserted_offers, stats

    def _save_pracuj_offers(offers: list[JobOffer]) -> tuple[list[JobOffer], SaveStats]:
        session = connect(Path("job_sniffer.sqlite"))
        try:
            logger.info("Initializing SQLite schema")
            init_db(session)
            logger.info("Saving Pracuj.pl offers")
            inserted_offers = [offer for offer in offers if save_offer(session, offer)]
            seen = len(offers)
            stats = SaveStats(
                seen=seen,
                inserted=len(inserted_offers),
                duplicates=seen - len(inserted_offers),
            )
            return inserted_offers, stats
        finally:
            logger.info("Closing SQLite session")
            session.close()

    def _show_results(offers: list[JobOffer], stats: SaveStats, *, action: str) -> None:
        status.value = (
            f"{action} {stats.seen} offers from Pracuj.pl. "
            f"Inserted {stats.inserted}, skipped duplicates {stats.duplicates}."
        )
        results.controls[:] = [
            ft.Text(
                f"{offer.title} - {offer.company}",
            )
            for offer in offers[:10]
        ]
        if not results.controls:
            results.controls.append(ft.Text("No new offers found."))

    scan_button.on_click = on_scan_click

    return ft.Column(
        controls=[
            ft.Text("Job Sniffer", size=32, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=ft.Column(
                    controls=[
                        keywords,
                        location,
                        limit,
                        scan_button,
                        status,
                        results,
                    ],
                    spacing=12,
                ),
                border_radius=12,
                padding=16,
            ),
            source_notes,
        ],
        spacing=12,
        expand=True,
    )
