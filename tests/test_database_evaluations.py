from __future__ import annotations

from pathlib import Path

from job_sniffer.database import (
    connect,
    delete_evaluation,
    delete_offer,
    get_evaluation,
    get_offer_by_id,
    init_db,
    list_offers_with_evaluations,
    list_unevaluated_offers,
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


def test_list_unevaluated_offers(tmp_path: Path) -> None:
    db_file = tmp_path / "test2.sqlite"
    with connect(db_file) as session:
        init_db(session)

        offer1 = JobOffer(
            external_id="ext-1",
            title="Dev 1",
            company="Comp 1",
            source="olx",
            url="https://olx.pl/1",
            salary=None,
            location="Wrocław",
            posted_at=None,
            description_text="Desc 1",
            raw={},
        )
        offer2 = JobOffer(
            external_id="ext-2",
            title="Dev 2",
            company="Comp 2",
            source="olx",
            url="https://olx.pl/2",
            salary=None,
            location="Kraków",
            posted_at=None,
            description_text="Desc 2",
            raw={},
        )
        id1 = save_offer(session, offer1)
        id2 = save_offer(session, offer2)
        assert id1 is not None and id2 is not None

        unevaluated = list_unevaluated_offers(session)
        assert len(unevaluated) == 2

        # Evaluate only offer1
        save_evaluation(
            session,
            JobOfferEvaluation(
                offer_id=id1,
                fit_score=75,
                verdict="Dobra szansa",
                summary="Sum",
                strengths=[],
                weaknesses=[],
                raw_response={},
            ),
        )

        unevaluated_after = list_unevaluated_offers(session)
        assert len(unevaluated_after) == 1
        assert unevaluated_after[0].id == id2


def test_delete_offer_and_cascade_evaluation(tmp_path: Path) -> None:
    db_file = tmp_path / "test3.sqlite"
    with connect(db_file) as session:
        init_db(session)

        offer = JobOffer(
            external_id="ext-del",
            title="Dev Delete",
            company="Comp Del",
            source="pracuj",
            url="https://pracuj.pl/del",
            salary=None,
            location="Poznań",
            posted_at=None,
            description_text="Desc Del",
            raw={},
        )
        oid = save_offer(session, offer)
        assert oid is not None

        save_evaluation(
            session,
            JobOfferEvaluation(
                offer_id=oid,
                fit_score=80,
                verdict="OK",
                summary="Sum",
                strengths=[],
                weaknesses=[],
                raw_response={},
            ),
        )

        assert get_offer_by_id(session, oid) is not None
        assert get_evaluation(session, oid) is not None

        # Delete offer
        success = delete_offer(session, oid)
        assert success is True
        assert get_offer_by_id(session, oid) is None
        assert get_evaluation(session, oid) is None


def test_delete_evaluation_only(tmp_path: Path) -> None:
    db_file = tmp_path / "test4.sqlite"
    with connect(db_file) as session:
        init_db(session)

        offer = JobOffer(
            external_id="ext-eval-del",
            title="Dev Retest",
            company="Comp Retest",
            source="pracuj",
            url="https://pracuj.pl/retest",
            salary=None,
            location="Gdańsk",
            posted_at=None,
            description_text="Desc",
            raw={},
        )
        oid = save_offer(session, offer)
        assert oid is not None

        save_evaluation(
            session,
            JobOfferEvaluation(
                offer_id=oid,
                fit_score=50,
                verdict="Średnia",
                summary="Sum",
                strengths=[],
                weaknesses=[],
                raw_response={},
            ),
        )

        assert get_evaluation(session, oid) is not None

        # Delete evaluation only
        success = delete_evaluation(session, oid)
        assert success is True
        assert get_evaluation(session, oid) is None
        # Offer itself remains intact
        assert get_offer_by_id(session, oid) is not None
