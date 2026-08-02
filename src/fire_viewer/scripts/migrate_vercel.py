from __future__ import annotations

import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from fire_viewer.db.engine import normalize_database_url
from fire_viewer.scripts.restore_die_retrospective import _load_payload, restore

_PRODUCTION = "production"
_LOCK_KEY = 2_026_072_800_001
_PACKAGED_RETROSPECTIVE_DATASETS = (
    "ledenon-2026-v1.json",
    "oupia-pouzols-2026-v1.json",
    "taradeau-2026-v1.json",
    "trevillach-2026-v1.json",
    "fontainebleau-2026-v1.json",
)


def _project_root() -> Path:
    candidate = Path.cwd()
    if (candidate / "alembic.ini").is_file() and (candidate / "migrations" / "env.py").is_file():
        return candidate
    raise RuntimeError("Vercel migration assets are unavailable from the build directory")


def _alembic_config(project_root: Path) -> Config:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    return config


def _restore_die_retrospective_if_requested(engine: Engine, project_root: Path) -> None:
    if os.environ.get("FV_RESTORE_DIE_RETROSPECTIVE") != "1":
        return

    dataset = project_root / "src" / "fire_viewer" / "retrospectives" / "die-2026-v1.json"
    payload = _load_payload(dataset)
    with Session(bind=engine) as session:
        result = restore(
            session,
            payload,
            actor="fireviewer-retrospective-recovery",
            apply=True,
        )
    print(
        "FireViewer retrospective restoration: "
        + json.dumps(result, ensure_ascii=False, sort_keys=True)
    )


def _restore_packaged_retrospectives_if_requested(engine: Engine, project_root: Path) -> None:
    """Restore reviewed daily layer pairs only when an operator enables the gate."""

    if os.environ.get("FV_RESTORE_PACKAGED_RETROSPECTIVES") != "1":
        return

    retrospective_root = project_root / "src" / "fire_viewer" / "retrospectives"
    with Session(bind=engine) as session:
        results = [
            restore(
                session,
                _load_payload(retrospective_root / dataset_name),
                actor="fireviewer-retrospective-recovery",
                apply=True,
            )
            for dataset_name in _PACKAGED_RETROSPECTIVE_DATASETS
        ]
    print(
        "FireViewer packaged retrospective restoration: "
        + json.dumps(results, ensure_ascii=False, sort_keys=True)
    )


def _upgrade_postgresql(
    database_url: str,
    config: Config,
    expected_revision: str,
    *,
    project_root: Path,
) -> None:
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
        _restore_die_retrospective_if_requested(engine, project_root)
        _restore_packaged_retrospectives_if_requested(engine, project_root)
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

    _upgrade_postgresql(
        database_url,
        config,
        expected_revision,
        project_root=project_root,
    )
    print(f"FireViewer migration gate: production schema ready at {expected_revision}")


if __name__ == "__main__":
    main()
