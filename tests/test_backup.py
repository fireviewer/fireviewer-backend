import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from fire_viewer.scripts.backup_sqlite import create_backup, sqlite_path_from_url
from fire_viewer.services import sqlite_recovery
from fire_viewer.services.sqlite_recovery import (
    SQLiteValidationError,
    expected_alembic_revision,
    restore_sqlite_backup,
    validate_sqlite_backup,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _sqlite_data_artifacts(path: Path) -> dict[str, bytes]:
    return {
        candidate.name: candidate.read_bytes()
        for candidate in (path, path.with_name(f"{path.name}-wal"))
        if candidate.exists()
    }


def test_sqlite_recovery_finds_migration_assets_from_deployment_workdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-editable install must still work with Docker's /app-style layout."""

    installed_module = (
        tmp_path / "site-packages" / "fire_viewer" / "services" / "sqlite_recovery.py"
    )
    installed_module.parent.mkdir(parents=True)
    installed_module.touch()
    deployment_root = tmp_path / "app"
    (deployment_root / "migrations").mkdir(parents=True)
    (deployment_root / "alembic.ini").touch()
    (deployment_root / "migrations" / "env.py").touch()

    monkeypatch.setattr(sqlite_recovery, "__file__", str(installed_module))
    monkeypatch.chdir(deployment_root)

    assert sqlite_recovery._project_root() == deployment_root


def _create_audited_backup(client, settings, payload_factory, tmp_path: Path) -> Path:
    response = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": "backup-restore-audit-0001"},
        json=payload_factory(source_id="backup-restore-source-001", content_char="a"),
    )
    assert response.status_code == 201

    source = sqlite_path_from_url(settings.database_url)
    backup = tmp_path / "backups" / "audited-snapshot.db"
    create_backup(source, backup)
    return backup


def test_sqlite_backup_is_integrity_checked_and_atomically_published(
    app, settings, tmp_path
) -> None:
    source = sqlite_path_from_url(settings.database_url)
    destination = tmp_path / "backups" / "snapshot.db"
    report = create_backup(source, destination)

    assert destination.exists()
    assert not list(destination.parent.glob("*.part"))
    assert report.alembic_revision
    assert report.audit_event_count == 0
    assert report.audit_snapshot_count == 0
    assert set(report.required_audit_triggers) == {
        "audit_event_no_update",
        "audit_event_no_delete",
    }
    assert validate_sqlite_backup(destination) == report
    with closing(sqlite3.connect(destination)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "incident_series" in tables
    assert "audit_event" in tables

    with pytest.raises(ValueError, match="must differ"):
        create_backup(source, source)


def test_backup_and_restore_preserve_consistent_audited_snapshot_without_mutating_source(
    client, settings, payload_factory, tmp_path
) -> None:
    response = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": "backup-source-immutable-0001"},
        json=payload_factory(source_id="backup-source-immutable-001", content_char="b"),
    )
    assert response.status_code == 201

    source = sqlite_path_from_url(settings.database_url)
    source_artifacts_before = _sqlite_data_artifacts(source)
    backup = tmp_path / "backups" / "snapshot.db"
    source_report = create_backup(source, backup)
    assert _sqlite_data_artifacts(source) == source_artifacts_before
    assert source_report.audit_event_count > 0
    assert source_report.audit_snapshot_count > 0

    restored = tmp_path / "restored" / "fire_viewer.db"
    restored_report = restore_sqlite_backup(backup, restored)
    assert restored_report == source_report
    assert restored.exists()
    assert not list(restored.parent.glob("*.part"))

    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM incident_series").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM observation").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM outbox_event").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM audit_event").fetchone() == (
            source_report.audit_event_count,
        )


def test_restore_rejects_missing_audit_trigger_without_publishing_target(
    client, settings, payload_factory, tmp_path
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    with closing(sqlite3.connect(backup)) as connection:
        connection.execute("DROP TRIGGER audit_event_no_delete")
        connection.commit()

    target = tmp_path / "restored" / "missing-trigger.db"
    with pytest.raises(SQLiteValidationError, match="missing_audit_trigger"):
        restore_sqlite_backup(backup, target)
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))


def test_restore_rejects_weakened_audit_trigger_without_publishing_target(
    client, settings, payload_factory, tmp_path
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    with closing(sqlite3.connect(backup)) as connection:
        connection.execute("DROP TRIGGER audit_event_no_update")
        connection.execute(
            "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event WHEN 0 "
            "BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END"
        )
        connection.commit()

    target = tmp_path / "restored" / "weakened-trigger.db"
    with pytest.raises(SQLiteValidationError, match="invalid_audit_trigger"):
        restore_sqlite_backup(backup, target)
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))


@pytest.mark.parametrize(
    ("trigger_name", "drop_statement"),
    (
        ("episode_incident_immutable", "DROP TRIGGER episode_incident_immutable"),
        ("spatial_zone_zone_id_immutable", "DROP TRIGGER spatial_zone_zone_id_immutable"),
        ("zone_publication_no_delete", "DROP TRIGGER zone_publication_no_delete"),
    ),
)
def test_restore_rejects_missing_current_sqlite_guard_without_publishing_target(
    client, settings, payload_factory, tmp_path, trigger_name: str, drop_statement: str
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    with closing(sqlite3.connect(backup)) as connection:
        connection.execute(drop_statement)
        connection.commit()

    target = tmp_path / "restored" / f"missing-{trigger_name}.db"
    with pytest.raises(SQLiteValidationError, match="missing_sqlite_invariant_trigger"):
        restore_sqlite_backup(backup, target)
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))


def test_restore_rejects_weakened_admin_zone_guard_without_publishing_target(
    client, settings, payload_factory, tmp_path
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    with closing(sqlite3.connect(backup)) as connection:
        connection.execute("DROP TRIGGER zone_publication_insert_valid")
        connection.execute(
            "CREATE TRIGGER zone_publication_insert_valid BEFORE INSERT ON zone_publication "
            "WHEN 0 BEGIN SELECT RAISE(ABORT, 'zone publication must start as draft'); END"
        )
        connection.commit()

    target = tmp_path / "restored" / "weakened-admin-zone-guard.db"
    with pytest.raises(SQLiteValidationError, match="invalid_sqlite_invariant_trigger"):
        restore_sqlite_backup(backup, target)
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))


def test_restore_rejects_tampered_audit_hash_without_publishing_target(
    client, settings, payload_factory, tmp_path
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    with closing(sqlite3.connect(backup)) as connection:
        connection.execute("DROP TRIGGER audit_event_no_update")
        result = connection.execute(
            "UPDATE audit_event SET after_hash = ? WHERE after_snapshot IS NOT NULL",
            ("0" * 64,),
        )
        connection.execute(
            "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event "
            "BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END"
        )
        connection.commit()
    assert result.rowcount and result.rowcount > 0

    target = tmp_path / "restored" / "tampered-hash.db"
    with pytest.raises(SQLiteValidationError, match="audit_snapshot_hash_mismatch"):
        restore_sqlite_backup(backup, target)
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))


def test_restore_rejects_truncated_backup_and_preserves_existing_target(
    client, settings, payload_factory, tmp_path
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    truncated = tmp_path / "backups" / "truncated.db"
    truncated.write_bytes(backup.read_bytes()[:64])
    invalid_target = tmp_path / "restored" / "truncated-target.db"
    with pytest.raises(SQLiteValidationError):
        restore_sqlite_backup(truncated, invalid_target)
    assert not invalid_target.exists()
    assert not list(invalid_target.parent.glob("*.part"))

    existing_target = tmp_path / "restored" / "existing.db"
    existing_target.parent.mkdir(parents=True, exist_ok=True)
    existing_target.write_bytes(b"existing target must remain unchanged")
    with pytest.raises(FileExistsError, match="already exists"):
        restore_sqlite_backup(backup, existing_target)
    assert existing_target.read_bytes() == b"existing target must remain unchanged"

    with pytest.raises(ValueError, match="must differ"):
        restore_sqlite_backup(backup, backup)


def test_restore_migrates_a_compatible_previous_revision_only_in_temporary_target(
    client, settings, payload_factory, tmp_path
) -> None:
    backup = _create_audited_backup(client, settings, payload_factory, tmp_path)
    command.downgrade(_alembic_config(backup), "c6d4f13a9b20")
    historical_report = validate_sqlite_backup(backup, require_current_revision=False)
    assert historical_report.alembic_revision == "c6d4f13a9b20"
    with pytest.raises(SQLiteValidationError, match="unexpected_alembic_revision"):
        validate_sqlite_backup(backup)

    source_bytes_before_restore = backup.read_bytes()
    target = tmp_path / "restored" / "migrated-to-head.db"
    restored_report = restore_sqlite_backup(backup, target)

    assert backup.read_bytes() == source_bytes_before_restore
    assert restored_report.alembic_revision == expected_alembic_revision()
    assert restored_report.audit_event_count == historical_report.audit_event_count
    assert restored_report.audit_snapshot_count == historical_report.audit_snapshot_count
    assert target.exists()
    assert not list(target.parent.glob("*.part"))
