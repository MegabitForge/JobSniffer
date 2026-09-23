from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from job_sniffer import database as db
from job_sniffer.database import Base, connect, init_db

EXPECTED_TABLES = {
    "alembic_version",
    "job_offers",
    "job_offer_evaluations",
    "profiles",
    "profile_source_settings",
}


def _head_revision() -> str:
    config = AlembicConfig()
    config.set_main_option("script_location", str(db._ALEMBIC_DIR))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _schema_inventory(engine: Engine) -> dict[str, tuple[Any, ...]]:
    inspector = inspect(engine)
    inventory: dict[str, tuple[Any, ...]] = {}
    for table_name in sorted(inspector.get_table_names()):
        if table_name == "alembic_version":
            continue
        columns = tuple(
            (column["name"], str(column["type"]), column["nullable"], column["primary_key"])
            for column in inspector.get_columns(table_name)
        )
        indexes = {
            index["name"]: (index["unique"], index["column_names"])
            for index in inspector.get_indexes(table_name)
            if index["name"] and not index["name"].startswith("sqlite_autoindex")
        }
        foreign_keys = tuple(
            sorted(
                (foreign_key["referred_table"], tuple(foreign_key["constrained_columns"]))
                for foreign_key in inspector.get_foreign_keys(table_name)
            )
        )
        inventory[table_name] = (columns, indexes, foreign_keys)
    return inventory


def test_fresh_database_contains_all_tables(tmp_path: Path) -> None:
    with connect(tmp_path / "test.sqlite") as session:
        init_db(session)
        engine = session.get_bind()

    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES
    assert "explanation" in {
        column["name"] for column in inspector.get_columns("job_offer_evaluations")
    }
    offer_indexes = {index["name"] for index in inspector.get_indexes("job_offers")}
    assert "ux_job_source_external_id" in offer_indexes
    assert "ux_job_source_url" in offer_indexes
    assert _version(engine) == _head_revision()


def test_alembic_schema_matches_metadata(tmp_path: Path) -> None:
    metadata_engine = create_engine(f"sqlite:///{tmp_path / 'metadata.sqlite'}")
    Base.metadata.create_all(metadata_engine)

    with connect(tmp_path / "migrated.sqlite") as session:
        init_db(session)
        migrated_engine = session.get_bind()

    assert _schema_inventory(migrated_engine) == _schema_inventory(metadata_engine)


def test_legacy_database_is_upgraded(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite"
    legacy_engine = create_engine(f"sqlite:///{legacy_path}")

    config = AlembicConfig()
    config.set_main_option("script_location", str(db._ALEMBIC_DIR))
    with legacy_engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")

    with legacy_engine.begin() as connection:
        connection.execute(text("ALTER TABLE job_offer_evaluations DROP COLUMN explanation"))
        connection.execute(text("DROP TABLE alembic_version"))

    with connect(legacy_path) as session:
        init_db(session)

    inspector = inspect(legacy_engine)
    assert set(inspector.get_table_names()) == EXPECTED_TABLES
    assert "explanation" in {
        column["name"] for column in inspector.get_columns("job_offer_evaluations")
    }
    assert _version(legacy_engine) == _head_revision()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    with connect(tmp_path / "test2.sqlite") as session:
        init_db(session)
        init_db(session)
        engine = session.get_bind()

    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
