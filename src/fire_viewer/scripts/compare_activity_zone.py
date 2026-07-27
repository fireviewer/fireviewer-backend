"""Compare one private daily layer with an official same-day GeoJSON perimeter."""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

from fire_viewer.services.activity_zone_quality import compare_activity_zones


def _geojson(path: Path) -> dict[str, Any]:
    source = path.read_bytes()
    raw = gzip.decompress(source) if path.suffix.casefold() == ".gz" else source
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not contain a GeoJSON object")
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Advisory same-day comparison; it never validates or publishes a layer."
    )
    parser.add_argument("--predicted", type=Path, required=True)
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--local-date", type=date.fromisoformat, required=True)
    parser.add_argument("--official-local-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare_activity_zones(
        _geojson(args.predicted),
        _geojson(args.official),
        predicted_local_date=args.local_date,
        official_local_date=args.official_local_date or args.local_date,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
