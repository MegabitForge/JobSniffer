from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from job_sniffer.config import AppConfig
from job_sniffer.database import connect, init_db, save_offer
from job_sniffer.llm.base import MatchEvaluationResult
from job_sniffer.models import JobOffer
from job_sniffer.services import EvaluationService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_evaluate_offer_retries_once_and_succeeds(tmp_path: Path) -> None:
    db_file = tmp_path / "test_retry.sqlite"
    cv_file = tmp_path / "cv.txt"
    cv_file.write_text("Doświadczony programista Python, FastAPI, Docker.", encoding="utf-8")

    config = AppConfig(cv_path=str(cv_file))
    service = EvaluationService(lambda: config)

    mock_provider = MagicMock()
    # First attempt raises Exception, second attempt succeeds
    mock_provider.evaluate_match = AsyncMock(
        side_effect=[
            RuntimeError("Simulated LLM network timeout"),
            MatchEvaluationResult(
                fit_score=92,
                explanation="Test explanation",
                    verdict="Świetne dopasowanie",
                summary="Bardzo dobry profil",
                strengths=["Python", "FastAPI"],
                weaknesses=[],
            ),
        ]
    )
    service.get_provider = MagicMock(return_value=mock_provider)  # type: ignore[method-assign]

    with connect(db_file) as session:
        init_db(session)
        offer = JobOffer(
            external_id="retry-1",
            title="Python Developer",
            company="Test Corp",
            source="nofluffjobs",
            url=None,
            salary=None,
            location=None,
            posted_at=None,
            description_text="Szukamy programisty Python.",
            raw={},
        )
        oid = save_offer(session, offer)
        assert oid is not None

        eval_result = await service.evaluate_offer(offer, oid, session)
        assert eval_result is not None
        assert eval_result.fit_score == 92
        assert mock_provider.evaluate_match.call_count == 2


@pytest.mark.anyio
async def test_evaluate_offer_fails_after_retry(tmp_path: Path) -> None:
    db_file = tmp_path / "test_retry_fail.sqlite"
    cv_file = tmp_path / "cv.txt"
    cv_file.write_text("Doświadczony programista Python.", encoding="utf-8")

    config = AppConfig(cv_path=str(cv_file))
    service = EvaluationService(lambda: config)

    mock_provider = MagicMock()
    # Both attempts fail
    mock_provider.evaluate_match = AsyncMock(
        side_effect=[
            RuntimeError("First failure"),
            RuntimeError("Second failure"),
        ]
    )
    service.get_provider = MagicMock(return_value=mock_provider)  # type: ignore[method-assign]

    with connect(db_file) as session:
        init_db(session)
        offer = JobOffer(
            external_id="retry-2",
            title="Python Developer",
            company="Test Corp",
            source="nofluffjobs",
            url=None,
            salary=None,
            location=None,
            posted_at=None,
            description_text="Szukamy programisty Python.",
            raw={},
        )
        oid = save_offer(session, offer)
        assert oid is not None

        eval_result = await service.evaluate_offer(offer, oid, session)
        assert eval_result is None
        assert mock_provider.evaluate_match.call_count == 2
