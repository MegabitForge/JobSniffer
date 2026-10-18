from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_sniffer.database import (
    ProfileSourceConfig,
    ProfileSourceSettingRecord,
    create_profile,
    db_session,
    delete_profile,
    get_profile,
    get_profile_source_settings,
    save_profile_source_settings,
    update_profile,
)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    with db_session(tmp_path / "app.sqlite") as session:
        yield session


@pytest.fixture
def backend_id(session: Session) -> int:
    """Id of a "Backend" profile that shares the database with a "Frontend" profile."""
    backend = create_profile(session, "Backend", offer_limit=5)
    assert backend is not None
    assert create_profile(session, "Frontend") is not None
    return backend.id


OLX_DISABLED = [ProfileSourceConfig(source_key="olx", enabled=False, filters={})]


def test_create_profile_rejects_empty_and_duplicate_names(session: Session) -> None:
    assert create_profile(session, "   ") is None
    assert create_profile(session, "Backend") is not None
    assert create_profile(session, "Backend") is None


def test_saved_source_settings_are_read_back(session: Session, backend_id: int) -> None:
    save_profile_source_settings(
        session,
        backend_id,
        [
            ProfileSourceConfig(
                source_key="theprotocol",
                enabled=True,
                filters={"keywords": "java", "technologies": ["java", ".net"]},
            ),
            ProfileSourceConfig(source_key="olx", enabled=False, filters={"location": "Wrocław"}),
        ],
    )

    assert get_profile_source_settings(session, backend_id) == {
        "theprotocol": ProfileSourceConfig(
            source_key="theprotocol",
            enabled=True,
            filters={"keywords": "java", "technologies": ["java", ".net"]},
        ),
        "olx": ProfileSourceConfig(
            source_key="olx", enabled=False, filters={"location": "Wrocław"}
        ),
    }


def test_saving_settings_again_replaces_the_stored_ones(session: Session, backend_id: int) -> None:
    save_profile_source_settings(
        session,
        backend_id,
        [ProfileSourceConfig(source_key="theprotocol", enabled=True, filters={"levels": ["mid"]})],
    )
    save_profile_source_settings(
        session,
        backend_id,
        [ProfileSourceConfig(source_key="theprotocol", enabled=False, filters={})],
    )

    assert get_profile_source_settings(session, backend_id) == {
        "theprotocol": ProfileSourceConfig(source_key="theprotocol", enabled=False, filters={})
    }


def test_deleting_a_profile_removes_its_source_settings(session: Session, backend_id: int) -> None:
    save_profile_source_settings(session, backend_id, OLX_DISABLED)

    assert delete_profile(session, backend_id) is True
    assert get_profile(session, backend_id) is None
    assert session.scalars(select(ProfileSourceSettingRecord)).all() == []
    assert delete_profile(session, backend_id) is False


def test_update_profile_saves_fields_and_source_settings(session: Session, backend_id: int) -> None:
    assert update_profile(session, backend_id, "Backend Java", 3, OLX_DISABLED) is True

    profile = get_profile(session, backend_id)
    assert profile is not None
    assert (profile.name, profile.offer_limit) == ("Backend Java", 3)
    assert get_profile_source_settings(session, backend_id) == {
        "olx": ProfileSourceConfig(source_key="olx", enabled=False, filters={})
    }


def test_rejected_profile_update_leaves_source_settings_unsaved(
    session: Session, backend_id: int
) -> None:
    assert update_profile(session, backend_id, "Frontend", 3, OLX_DISABLED) is False

    profile = get_profile(session, backend_id)
    assert profile is not None
    assert (profile.name, profile.offer_limit) == ("Backend", 5)
    assert get_profile_source_settings(session, backend_id) == {}
