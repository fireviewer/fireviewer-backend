from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from fire_viewer.db.engine import normalize_database_url

_PRODUCTION = "production"
_LOCK_KEY = 2_026_072_800_001


def _project_root() -> Path:
    candidate = Path.cwd()
    if (candidate / "alembic.ini").is_file() and (candidate / "migrations" / "env.py").is_file():
        return candidate
    raise RuntimeError("Vercel migration assets are unavailable from the build directory")


def _alembic_config(project_root: Path) -> Config:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    return config


def _upgrade_postgresql(database_url: str, config: Config, expected_revision: str) -> None:
    engine = create_engine(normalize_database_url(database_url), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("Production migrations require PostgreSQL")
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _LOCK_KEY},
            )
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if current_revision != expected_revision:
                raise RuntimeError(
                    "Production migration finished on an unexpected schema revision"
                )
    finally:
        engine.dispose()


def main() -> None:
    target_environment = os.environ.get("VERCEL_TARGET_ENV") or os.environ.get("VERCEL_ENV")
    if target_environment != _PRODUCTION:
        print("FireViewer migration gate: skipped outside production")
        return

    database_url = os.environ.get("FV_DATABASE_URL")
    if not database_url:
        raise RuntimeError("FV_DATABASE_URL is required for a production build")

    project_root = _project_root()
    config = _alembic_config(project_root)
    expected_revision = ScriptDirectory.from_config(config).get_current_head()
    if expected_revision is None:
        raise RuntimeError("Alembic has no unique migration head")

    configured_revision = os.environ.get("FV_DATABASE_SCHEMA_REVISION")
    if configured_revision != expected_revision:
        raise RuntimeError(
            "FV_DATABASE_SCHEMA_REVISION does not match the packaged Alembic head"
        )

    _upgrade_postgresql(database_url, config, expected_revision)
    print(f"FireViewer migration gate: production schema ready at {expected_revision}")


if __name__ == "__main__":
    main()
