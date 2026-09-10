from __future__ import annotations

from pathlib import Path

from job_sniffer.database import (
    connect,
    get_evaluation,
    init_db,
    list_offers_with_evaluations,
    save_evaluation,
    save_offer,
)
from job_sniffer.models import JobOffer, JobOfferEvaluation


def test_save_and_retrieve_evaluation(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    with connect(db_file) as session:
        init_db(session)

        offer = JobOffer(
            external_id="test-123",
            title="Senior Python Engineer",
            company="Acme IT",
            source="nofluffjobs",
            url="https://nofluffjobs.com/job/senior-python",
            salary="20 000 - 25 000 PLN",
            location="Warszawa",
            posted_at=None,
            description_text="Firma Acme poszukuje seniora.",
            raw={},
        )
        offer_id = save_offer(session, offer)
        assert offer_id is not None

        eval_data = JobOfferEvaluation(
            offer_id=offer_id,
            fit_score=88,
            verdict="Wysoka szansa na rozmowę",
            summary="Stanowisko skupione na backendzie i mikroserwisach.",
            strengths=["5+ lat w Pythonie", "Architektura mikroserwisów"],
            weaknesses=["Brak znajomości GCP"],
            raw_response={"fit_score": 88},
        )
        save_evaluation(session, eval_data)

        retrieved = get_evaluation(session, offer_id)
        assert retrieved is not None
        assert retrieved.fit_score == 88
        assert retrieved.verdict == "Wysoka szansa na rozmowę"
        assert len(retrieved.strengths) == 2
        assert len(retrieved.weaknesses) == 1

        offers_with_eval = list_offers_with_evaluations(session)
        assert len(offers_with_eval) == 1
        retrieved_offer, retrieved_eval = offers_with_eval[0]
        assert retrieved_offer.title == "Senior Python Engineer"
        assert retrieved_eval is not None
        assert retrieved_eval.fit_score == 88
