from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from fire_viewer.core.config import Settings
from fire_viewer.db.sqlite_invariants import SQLITE_CRITICAL_TRIGGERS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRE_NORMALIZATION_REVISION = "f3b8c1d7a920"
NORMALIZATION_REVISION = "a4e9c2f7d610"
PRE_SOURCE_INGESTION_REVISION = "e1c7a9b4d620"
SOURCE_INGESTION_REVISION = "f9c8b7a6d510"
CURRENT_SCHEMA_REVISION = "db7c2e4f9a10"


def test_runtime_and_vercel_expect_the_current_schema_revision() -> None:
    vercel_config = json.loads((PROJECT_ROOT / "vercel.json").read_text(encoding="utf-8"))

    assert Settings().database_schema_revision == CURRENT_SCHEMA_REVISION
    assert "buildCommand" not in vercel_config
    assert vercel_config["env"]["FV_DATABASE_SCHEMA_REVISION"] == CURRENT_SCHEMA_REVISION


def _config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _foreign_key(inspector, table: str, column: str) -> dict:
    return next(
        constraint
        for constraint in inspector.get_foreign_keys(table)
        if constraint["constrained_columns"] == [column]
    )


def test_forward_repair_creates_bulletin_table_for_already_stamped_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-stamped-without-bulletin.db"
    config = _config(database_path)
    command.upgrade(config, "ca3d7e9f2b61")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE incident_bulletin_entry"))
        assert not inspect(engine).has_table("incident_bulletin_entry")
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        repaired = inspect(engine)
        assert repaired.has_table("incident_bulletin_entry")
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == CURRENT_SCHEMA_REVISION
    finally:
        engine.dispose()


def test_normalization_migration_resolves_all_three_autogenerate_drifts(tmp_path: Path) -> None:
    database_path = tmp_path / "schema-drift.db"
    config = _config(database_path)
    command.upgrade(config, PRE_NORMALIZATION_REVISION)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        before = inspect(engine)
        lod = next(
            column for column in before.get_columns("model_asset") if column["name"] == "lod"
        )
        assert lod["type"].length == 7
    finally:
        engine.dispose()

    command.upgrade(config, NORMALIZATION_REVISION)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        after = inspect(engine)
        lod = next(column for column in after.get_columns("model_asset") if column["name"] == "lod")
        assert lod["type"].length == 8
        assert (
            _foreign_key(after, "model_asset", "spatial_package_file_id")["options"]["ondelete"]
            == "RESTRICT"
        )
        assert (
            _foreign_key(after, "manifest_revision", "spatial_package_id")["options"]["ondelete"]
            == "RESTRICT"
        )
        with engine.connect() as connection:
            triggers = set(
                connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                ).scalars()
            )
        assert triggers >= SQLITE_CRITICAL_TRIGGERS
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, PRE_NORMALIZATION_REVISION)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        downgraded = inspect(engine)
        lod = next(
            column for column in downgraded.get_columns("model_asset") if column["name"] == "lod"
        )
        assert lod["type"].length == 7
        with engine.connect() as connection:
            triggers = set(
                connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                ).scalars()
            )
        assert triggers >= SQLITE_CRITICAL_TRIGGERS
    finally:
        engine.dispose()

    command.downgrade(config, "base")


def test_source_ingestion_migration_normalizes_existing_consent_values(tmp_path: Path) -> None:
    database_path = tmp_path / "source-consent.db"
    config = _config(database_path)
    command.upgrade(config, PRE_SOURCE_INGESTION_REVISION)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_media_batch "
                    "(id, batch_id, schema_version, batch_type, priority, state, "
                    "idempotency_key, request_hash, trace_id, purge_after, created_at, updated_at) "
                    "VALUES (1, 'batch-legacy-consent', '1.0', 'user_media', 'scheduled', "
                    "'DRAFT', 'legacy-consent', :digest, 'trace-legacy-consent', :timestamp, "
                    ":timestamp, :timestamp)"
                ),
                {"digest": "0" * 64, "timestamp": "2030-01-01 00:00:00"},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_media_item "
                    "(id, batch_id, input_id, media_type, metadata_payload, "
                    "processable_payload, preprocessing_status, purge_after, "
                    "created_at, updated_at) "
                    "VALUES (1, 1, 'legacy-input', 'image', '{}', '{}', 'pending', "
                    ":timestamp, :timestamp, :timestamp)"
                ),
                {"timestamp": "2030-01-01 00:00:00"},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_media_consent "
                    "(id, item_id, basis, state, scopes, terms_version, evidence_sha256, "
                    "granted_at, created_at, updated_at) "
                    "VALUES (1, 1, 'EXPLICIT_UPLOAD', 'GRANTED', '[\"analysis\"]', "
                    "'legacy-v1', :digest, :timestamp, :timestamp, :timestamp)"
                ),
                {"digest": "1" * 64, "timestamp": "2026-07-19 00:00:00"},
            )
    finally:
        engine.dispose()

    command.upgrade(config, SOURCE_INGESTION_REVISION)
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT basis FROM agent_media_consent WHERE id = 1")
            ) == ("explicit_upload")
        basis_column = next(
            column
            for column in inspect(engine).get_columns("agent_media_consent")
            if column["name"] == "basis"
        )
        assert basis_column["type"].length == len("public_source_analysis")
    finally:
        engine.dispose()

    command.downgrade(config, PRE_SOURCE_INGESTION_REVISION)
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT basis FROM agent_media_consent WHERE id = 1")
            ) == ("EXPLICIT_UPLOAD")
    finally:
        engine.dispose()
