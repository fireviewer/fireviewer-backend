from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPAIR_REVISION = "d7c5e3a1b920"


def _config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_repair_migration_keeps_unity_kind_width_on_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "spatial-kind-width.db"
    config = _config(f"sqlite:///{database_path}")

    command.upgrade(config, REPAIR_REVISION)

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        column = next(
            item
            for item in inspect(engine).get_columns("spatial_package_file")
            if item["name"] == "kind"
        )
        assert column["type"].length == 9
    finally:
        engine.dispose()


def test_repair_migration_emits_explicit_postgresql_width_change() -> None:
    config = _config("postgresql+psycopg://user:password@localhost/fireviewer")
    output = io.StringIO()

    with redirect_stdout(output):
        command.upgrade(config, f"d2a6e8f1b430:{REPAIR_REVISION}", sql=True)

    assert (
        "ALTER TABLE spatial_package_file ALTER COLUMN kind TYPE VARCHAR(9);" in output.getvalue()
    )


def test_tiled_manifest_migration_emits_valid_postgresql_function_sql() -> None:
    config = _config("postgresql+psycopg://user:password@localhost/fireviewer")
    output = io.StringIO()

    with redirect_stdout(output):
        command.upgrade(config, "c9f1a7d4e620:d2a6e8f1b430", sql=True)

    sql = output.getvalue()
    assert "CREATE OR REPLACE FUNCTION fire_viewer_manifest_package_valid()" in sql
    assert "# noqa" not in sql
