from __future__ import annotations

import json
from pathlib import Path

from fire_viewer.domain.schemas import ViewerManifest


def main() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    output = (
        repository_root / "contracts" / "viewer-manifest" / "v2" / "viewer-manifest.schema.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    schema = ViewerManifest.model_json_schema(mode="serialization")
    output.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
