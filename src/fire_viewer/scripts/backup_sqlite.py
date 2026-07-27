from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from fire_viewer.core.config import get_settings
from fire_viewer.services.sqlite_recovery import (
    SQLiteValidationReport,
    create_validated_backup,
)


def sqlite_path_from_url(url: str) -> Path:
    prefixes = ("sqlite:///", "sqlite+pysqlite:///")
    for prefix in prefixes:
        if url.startswith(prefix):
            value = unquote(url.removeprefix(prefix))
            if value == ":memory:":
                raise ValueError("An in-memory database cannot be backed up")
            return Path(value).resolve()
    raise ValueError("fire-viewer-backup only supports SQLite database URLs")


def create_backup(source_path: Path, destination_path: Path) -> SQLiteValidationReport:
    """Create a validated, consistent local SQLite snapshot.

    The source is opened read-only by the shared recovery service.  SQLite's
    backup API reads a coherent snapshot including WAL content without forcing a
    checkpoint on the running application database.
    """

    return create_validated_backup(source_path, destination_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    source = sqlite_path_from_url(settings.database_url)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or Path("backups") / f"fire_viewer_{timestamp}.db"
    report = create_backup(source, output)
    print(json.dumps({"operation": "backup", "validation": report.as_dict()}))


if __name__ == "__main__":
    main()
