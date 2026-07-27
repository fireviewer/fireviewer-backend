from __future__ import annotations

import argparse
import json
from pathlib import Path

from fire_viewer.services.sqlite_recovery import restore_sqlite_backup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore a validated Fire-Viewer SQLite backup to a new local target"
    )
    parser.add_argument("--source", type=Path, required=True, help="Validated SQLite backup")
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="New SQLite target; an existing file is never overwritten",
    )
    args = parser.parse_args()
    report = restore_sqlite_backup(args.source, args.target)
    print(json.dumps({"operation": "restore", "validation": report.as_dict()}))


if __name__ == "__main__":
    main()
