from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text

from fire_viewer.db.sqlite_invariants import SQLITE_CRITICAL_TRIGGERS


def _sqlite_object_names(engine: Engine, object_type: str) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = :object_type"),
                {"object_type": object_type},
            ).scalars()
        )


def main() -> None:
    project_root = Path(__file__).resolve().parents[3]
    with TemporaryDirectory(prefix="fire-viewer-migrations-") as temporary_directory:
        database_path = Path(temporary_directory) / "migration_check.db"
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

        command.upgrade(config, "head")
        # The same declarative target must be safe to apply repeatedly.
        command.upgrade(config, "head")
        command.check(config)

        engine = create_engine(f"sqlite:///{database_path}")
        try:
            triggers = _sqlite_object_names(engine, "trigger")
            missing_triggers = SQLITE_CRITICAL_TRIGGERS - triggers
            if missing_triggers:
                raise RuntimeError(
                    f"Upgrade did not install critical SQLite triggers: {sorted(missing_triggers)}"
                )
            rtree_tables = {
                name
                for name in _sqlite_object_names(engine, "table")
                if name.startswith("incident_series_rtree")
            }
            if "incident_series_rtree" not in rtree_tables:
                raise RuntimeError("Upgrade did not install incident_series_rtree")
        finally:
            engine.dispose()

        # Do not leave an inspected SQLite connection in the pool while Alembic rebuilds tables.
        command.downgrade(config, "base")

        engine = create_engine(f"sqlite:///{database_path}")
        try:
            remaining = set(inspect(engine).get_table_names())
            remaining_triggers = _sqlite_object_names(engine, "trigger")
            remaining_rtree = {
                name
                for name in _sqlite_object_names(engine, "table")
                if name.startswith("incident_series_rtree")
            }
        finally:
            engine.dispose()
        if remaining - {"alembic_version"}:
            raise RuntimeError(f"Downgrade left unexpected tables: {sorted(remaining)}")
        if remaining_triggers:
            raise RuntimeError(f"Downgrade left SQLite triggers: {sorted(remaining_triggers)}")
        if remaining_rtree:
            raise RuntimeError(f"Downgrade left RTree tables: {sorted(remaining_rtree)}")

    print("Alembic upgrade/idempotent-upgrade/check/downgrade: OK")


if __name__ == "__main__":
    main()
