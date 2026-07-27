from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from fire_viewer.db.sqlite_invariants import SQLITE_CRITICAL_TRIGGERS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRE_NORMALIZATION_REVISION = "f3b8c1d7a920"
NORMALIZATION_REVISION = "a4e9c2f7d610"


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
