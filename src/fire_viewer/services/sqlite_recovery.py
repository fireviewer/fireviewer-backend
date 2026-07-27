"""Local SQLite backup validation and safe restore primitives.

The recovery path intentionally has no network or hosted-storage dependency.  It
only accepts a local SQLite file, opens validation sources read-only, and never
publishes a restore until the copy has passed the same integrity checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from fire_viewer.db.sqlite_invariants import (
    SQLITE_CRITICAL_TRIGGER_HASHES,
    SQLITE_CRITICAL_TRIGGERS,
)
from fire_viewer.domain.hashing import sha256_hex

REQUIRED_AUDIT_TRIGGERS = {
    "audit_event_no_update": (
        "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event "
        "BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END"
    ),
    "audit_event_no_delete": (
        "CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit_event "
        "BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END"
    ),
}


class SQLiteValidationError(RuntimeError):
    """A recovery input failed a non-sensitive, named validation check."""

    def __init__(self, code: str) -> None:
        super().__init__(f"SQLite recovery validation failed: {code}")
        self.code = code


@dataclass(frozen=True)
class SQLiteValidationReport:
    """Validation facts safe to report without disclosing database contents."""

    alembic_revision: str
    audit_event_count: int
    audit_snapshot_count: int
    required_audit_triggers: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "result": "valid",
            "integrity_check": "ok",
            "foreign_key_check": "ok",
            "alembic_revision": self.alembic_revision,
            "audit_event_count": self.audit_event_count,
            "audit_snapshot_count": self.audit_snapshot_count,
            "required_audit_triggers": list(self.required_audit_triggers),
        }


def _project_root() -> Path:
    """Locate the deployment-owned Alembic files without requiring an editable install.

    Development imports the module from ``<project>/src``.  The Docker image,
    however, installs the Python package into site-packages while deliberately
    retaining ``alembic.ini`` and ``migrations/`` under its working directory.
    Resolve the source tree first, then the deployment working directory.
    """

    candidates = (Path(__file__).resolve().parents[3], Path.cwd().resolve())
    for candidate in candidates:
        alembic_config = candidate / "alembic.ini"
        migration_environment = candidate / "migrations" / "env.py"
        if alembic_config.is_file() and migration_environment.is_file():
            return candidate
    raise SQLiteValidationError("migration_assets_unavailable")


def _alembic_config(database_path: Path | None = None) -> Config:
    project_root = _project_root()
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    if database_path is not None:
        path = quote(database_path.resolve().as_posix(), safe="/:")
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def expected_alembic_revision() -> str:
    """Return the single migration head shipped with this backend."""

    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    if len(heads) != 1:
        raise SQLiteValidationError("ambiguous_alembic_head")
    return heads[0]


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        # ``mode=ro`` prevents recovery validation and source copies from creating
        # journals, checkpointing WAL, or otherwise mutating the source database.
        uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except (OSError, sqlite3.Error) as error:
        raise SQLiteValidationError("database_not_readable") from error


def _require_integrity(connection: sqlite3.Connection) -> None:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as error:
        raise SQLiteValidationError("integrity_check_failed") from error
    if rows != [("ok",)]:
        raise SQLiteValidationError("integrity_check_failed")


def _require_foreign_keys(connection: sqlite3.Connection) -> None:
    try:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise SQLiteValidationError("foreign_key_check_failed") from error
    if violations:
        raise SQLiteValidationError("foreign_key_check_failed")


def _require_alembic_revision(
    connection: sqlite3.Connection,
    expected_revision: str | None,
) -> str:
    try:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as error:
        raise SQLiteValidationError("missing_alembic_revision") from error
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise SQLiteValidationError("invalid_alembic_revision")
    revision = rows[0][0]
    if expected_revision is not None and revision != expected_revision:
        raise SQLiteValidationError("unexpected_alembic_revision")
    return revision


def _normalise_sql(statement: str) -> str:
    return " ".join(statement.lower().split()).rstrip(";")


def _require_audit_triggers(connection: sqlite3.Connection) -> tuple[str, ...]:
    names = tuple(REQUIRED_AUDIT_TRIGGERS)
    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'audit_event' AND name IN (?, ?)",
            names,
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteValidationError("missing_audit_trigger") from error

    triggers = {name: sql for name, sql in rows}
    for name, expected_statement in REQUIRED_AUDIT_TRIGGERS.items():
        statement = triggers.get(name)
        if not isinstance(statement, str):
            raise SQLiteValidationError("missing_audit_trigger")
        if _normalise_sql(statement) != _normalise_sql(expected_statement):
            raise SQLiteValidationError("invalid_audit_trigger")
    return names


def _require_current_invariant_triggers(connection: sqlite3.Connection) -> None:
    """Require every SQLite trigger shipped by the current schema contract."""

    try:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteValidationError("sqlite_invariant_trigger_unavailable") from error

    triggers = {name: statement for name, statement in rows}
    present = set(triggers)
    if SQLITE_CRITICAL_TRIGGERS - present:
        raise SQLiteValidationError("missing_sqlite_invariant_trigger")
    for name, expected_hash in SQLITE_CRITICAL_TRIGGER_HASHES.items():
        statement = triggers.get(name)
        if not isinstance(statement, str):
            raise SQLiteValidationError("invalid_sqlite_invariant_trigger")
        actual_hash = hashlib.sha256(_normalise_sql(statement).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise SQLiteValidationError("invalid_sqlite_invariant_trigger")


def _decode_snapshot(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise SQLiteValidationError("invalid_audit_snapshot")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise SQLiteValidationError("invalid_audit_snapshot") from error


def _verify_audit_snapshots(connection: sqlite3.Connection) -> tuple[int, int]:
    try:
        rows = connection.execute(
            "SELECT before_hash, after_hash, before_snapshot, after_snapshot FROM audit_event"
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteValidationError("audit_table_unavailable") from error

    snapshot_count = 0
    for before_hash, after_hash, before_snapshot, after_snapshot in rows:
        for stored_hash, snapshot in (
            (before_hash, before_snapshot),
            (after_hash, after_snapshot),
        ):
            if snapshot is None:
                if stored_hash is not None:
                    raise SQLiteValidationError("audit_snapshot_hash_mismatch")
                continue
            decoded_snapshot = _decode_snapshot(snapshot)
            # SQLAlchemy serializes a nullable JSON column as the JSON literal
            # ``null`` on SQLite. It is semantically the same absent snapshot as
            # a SQL NULL and must therefore have no companion hash.
            if decoded_snapshot is None:
                if stored_hash is not None:
                    raise SQLiteValidationError("audit_snapshot_hash_mismatch")
                continue
            if not isinstance(stored_hash, str):
                raise SQLiteValidationError("audit_snapshot_hash_mismatch")
            actual_hash = sha256_hex(decoded_snapshot)
            if not hmac.compare_digest(stored_hash, actual_hash):
                raise SQLiteValidationError("audit_snapshot_hash_mismatch")
            snapshot_count += 1
    return len(rows), snapshot_count


def validate_sqlite_backup(
    database_path: Path,
    *,
    expected_revision: str | None = None,
    require_current_revision: bool = True,
) -> SQLiteValidationReport:
    """Validate a local SQLite backup without writing to it.

    A valid file must be structurally sound, retain both append-only audit
    triggers, and have audit hashes matching their JSON snapshots. By default it
    must also be at this application's exact Alembic head. Recovery uses
    ``require_current_revision=False`` only for a source that will be copied and
    migrated in a private temporary file before publication.
    """

    database_path = database_path.resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    if expected_revision is None and require_current_revision:
        expected_revision = expected_alembic_revision()

    with closing(_readonly_connection(database_path)) as connection:
        _require_integrity(connection)
        _require_foreign_keys(connection)
        revision = _require_alembic_revision(connection, expected_revision)
        triggers = _require_audit_triggers(connection)
        if revision == expected_alembic_revision():
            _require_current_invariant_triggers(connection)
        audit_event_count, audit_snapshot_count = _verify_audit_snapshots(connection)
    return SQLiteValidationReport(
        alembic_revision=revision,
        audit_event_count=audit_event_count,
        audit_snapshot_count=audit_snapshot_count,
        required_audit_triggers=triggers,
    )


def _copy_consistent_snapshot(source_path: Path, destination_path: Path) -> None:
    with (
        closing(_readonly_connection(source_path)) as source,
        closing(sqlite3.connect(destination_path)) as destination,
    ):
        try:
            source.backup(destination)
        except sqlite3.Error as error:
            raise SQLiteValidationError("snapshot_copy_failed") from error


def _upgrade_temporary_snapshot(temporary_path: Path) -> None:
    """Apply local Alembic migrations only to an unpublished recovery candidate."""

    try:
        command.upgrade(_alembic_config(temporary_path), "head")
    except Exception as error:
        raise SQLiteValidationError("migration_to_current_revision_failed") from error


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _temporary_path(destination_path: Path) -> Path:
    return destination_path.with_name(f".{destination_path.name}.{uuid4().hex}.part")


def create_validated_backup(source_path: Path, destination_path: Path) -> SQLiteValidationReport:
    """Copy a consistent SQLite snapshot and publish it after full validation."""

    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path == destination_path:
        raise ValueError("Backup destination must differ from the source database")

    source_report = validate_sqlite_backup(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(destination_path)
    try:
        _copy_consistent_snapshot(source_path, temporary_path)
        destination_report = validate_sqlite_backup(temporary_path)
        if destination_report != source_report:
            raise SQLiteValidationError("backup_validation_mismatch")
        _fsync_file(temporary_path)
        os.replace(temporary_path, destination_path)
        return destination_report
    finally:
        temporary_path.unlink(missing_ok=True)


def _publish_new_target(temporary_path: Path, target_path: Path) -> None:
    """Atomically publish a new target without ever replacing an existing file."""

    try:
        # A hard-link publication is atomic on a single filesystem and fails if
        # another process created the target after our initial existence check.
        os.link(temporary_path, target_path)
    except FileExistsError as error:
        raise FileExistsError("Restore target already exists") from error
    except OSError as error:
        raise SQLiteValidationError("atomic_publish_failed") from error


def restore_sqlite_backup(source_path: Path, target_path: Path) -> SQLiteValidationReport:
    """Restore a validated SQLite backup to a new target, without overwriting it."""

    source_path = source_path.resolve()
    target_path = target_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path == target_path:
        raise ValueError("Restore target must differ from the source database")
    if target_path.exists():
        raise FileExistsError("Restore target already exists")

    # A historical backup is accepted only after physical, FK, trigger and audit
    # validation. Its original file remains read-only; migrations run solely on
    # the private candidate below.
    source_report = validate_sqlite_backup(source_path, require_current_revision=False)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(target_path)
    try:
        _copy_consistent_snapshot(source_path, temporary_path)
        _upgrade_temporary_snapshot(temporary_path)
        restored_report = validate_sqlite_backup(temporary_path)
        if (
            restored_report.audit_event_count != source_report.audit_event_count
            or restored_report.audit_snapshot_count != source_report.audit_snapshot_count
        ):
            raise SQLiteValidationError("restore_validation_mismatch")
        _fsync_file(temporary_path)
        _publish_new_target(temporary_path, target_path)
        return restored_report
    finally:
        temporary_path.unlink(missing_ok=True)
