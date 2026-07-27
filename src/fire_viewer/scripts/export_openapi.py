from __future__ import annotations

import json
from pathlib import Path

from fire_viewer.core.config import Settings
from fire_viewer.main import create_app


def main() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        database_url="sqlite:///:memory:",
        trusted_hosts=["testserver"],
    )
    schema = create_app(settings).openapi()
    output = Path("openapi/openapi.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
