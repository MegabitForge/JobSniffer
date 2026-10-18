from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_sniffer.database import (
    ProfileSourceConfig,
    ProfileSourceSettingRecord,
    connect,
    create_profile,
    db_session,
    delete_profile,
    get_profile,
    get_profile_source_settings,
    init_db,
    list_profiles,
    save_profile_source_settings,
    update_profile,
)


def test_create_and_list_profiles_ordered_by_name(tmp_path: Path) -> None:
    with connect(tmp_path / "test.sqlite") as session:
        init_db(session)

        junior = create_profile(session, "Junior Java")
        mid = create_profile(session, "Mid Python")
        assert junior is not None
        assert mid is not None
        assert junior.id != mid.id

        names = [profile.name for profile in list_profiles(session)]
        assert names == ["Junior Java", "Mid Python"]


def test_create_profile_rejects_empty_and_duplicate_names(tmp_path: Path) -> None:
    with connect(tmp_path / "test2.sqlite") as session:
        init_db(session)

        assert create_profile(session, "   ") is None
        assert create_profile(session, "Backend") is not None
        assert create_profile(session, "Backend") is None


def test_update_profile_name_and_limit(tmp_path: Path) -> None:
    with connect(tmp_path / "test3.sqlite") as session:
        init_db(session)

        profile = create_profile(session, "Old", offer_limit=5)
        assert profile is not None

        assert update_profile(session, profile.id, "New", 10) is True
        updated = get_profile(session, profile.id)
        assert updated is not None
        assert updated.name == "New"
        assert updated.offer_limit == 10

        assert update_profile(session, profile.id, "  ", None) is False
        other = create_profile(session, "Other")
        assert other is not None
        assert update_profile(session, profile.id, "Other", None) is False
        assert update_profile(session, 999, "None", None) is False


def test_new_profile_has_no_source_settings(tmp_path: Path) -> None:
    with connect(tmp_path / "test4.sqlite") as session:
        init_db(session)

        profile = create_profile(session, "Java Dev")
        assert profile is not None
        assert get_profile_source_settings(session, profile.id) == {}


def test_save_and_read_source_settings(tmp_path: Path) -> None:
    with connect(tmp_path / "test5.sqlite") as session:
        init_db(session)

        profile = create_profile(session, "Java Dev")
        assert profile is not None

        save_profile_source_settings(
            session,
            profile.id,
            [
                ProfileSourceConfig(
                    source_key="theprotocol",
                    enabled=True,
                    filters={"keywords": "java", "technologies": ["java", ".net"]},
                ),
                ProfileSourceConfig(
                    source_key="olx",
                    enabled=False,
                    filters={"location": "Wrocław"},
                ),
            ],
        )

        settings = get_profile_source_settings(session, profile.id)
        assert set(settings) == {"theprotocol", "olx"}
        theprotocol = settings["theprotocol"]
        assert theprotocol.enabled is True
        assert theprotocol.filters["keywords"] == "java"
        assert theprotocol.filters["technologies"] == ["java", ".net"]
        assert settings["olx"].enabled is False
        assert settings["olx"].filters["location"] == "Wrocław"


def test_saving_settings_upserts_existing_rows(tmp_path: Path) -> None:
    with connect(tmp_path / "test6.sqlite") as session:
        init_db(session)

        profile = create_profile(session, "DevOps")
        assert profile is not None

        save_profile_source_settings(
            session,
            profile.id,
            [
                ProfileSourceConfig(
                    source_key="theprotocol",
                    enabled=True,
                    filters={"levels": ["mid"]},
                ),
            ],
        )
        save_profile_source_settings(
            session,
            profile.id,
            [ProfileSourceConfig(source_key="theprotocol", enabled=False, filters={})],
        )

        settings = get_profile_source_settings(session, profile.id)
        assert list(settings) == ["theprotocol"]
        assert settings["theprotocol"].enabled is False
        assert settings["theprotocol"].filters == {}


def test_delete_profile_cascades_to_source_settings(tmp_path: Path) -> None:
    with connect(tmp_path / "test7.sqlite") as session:
        init_db(session)

        profile = create_profile(session, "To be deleted")
        assert profile is not None
        save_profile_source_settings(
            session,
            profile.id,
            [ProfileSourceConfig(source_key="olx", enabled=True, filters={})],
        )

        assert delete_profile(session, profile.id) is True
        assert get_profile(session, profile.id) is None
        assert session.scalars(select(ProfileSourceSettingRecord)).all() == []
        assert delete_profile(session, profile.id) is False


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
