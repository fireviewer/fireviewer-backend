from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_cli_honors_fv_database_url_and_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "explicit-g1.db"
    fallback_path = tmp_path / "data" / "fire_viewer.db"
    fallback_path.parent.mkdir()
    ini_path = tmp_path / "alembic.ini"
    ini_path.write_text(
        (PROJECT_ROOT / "alembic.ini")
        .read_text()
        .replace("script_location = migrations", f"script_location = {PROJECT_ROOT / 'migrations'}")
    )
    database_url = f"sqlite:///{database_path.as_posix()}"
    environment = dict(os.environ, FV_DATABASE_URL=database_url)
    command = [sys.executable, "-m", "alembic", "-c", str(ini_path), "upgrade", "head"]

    subprocess.run(  # noqa: S603 - fixed interpreter and temporary local Alembic config
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603 - fixed interpreter and temporary local Alembic config
        command,
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert database_path.is_file()
    assert not fallback_path.exists()
    with closing(sqlite3.connect(database_path)) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    expected_revision = ScriptDirectory.from_config(config).get_current_head()
    assert revision == (expected_revision,)
