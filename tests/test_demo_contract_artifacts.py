from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from fire_viewer.domain.enums import IncidentStatus, PublicVisibility, VerificationState
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.public_visibility import (
    canonical_public_visibility,
    permits_public_location,
    permits_public_viewer_asset,
)
from fire_viewer.domain.schemas import ViewerManifest
from fire_viewer.scripts.seed_demo import seed_demo

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = BACKEND_ROOT / "contracts" / "demo" / "v1"
VIEWER_SCHEMA_PATH = (
    BACKEND_ROOT / "contracts" / "viewer-manifest" / "v2" / "viewer-manifest.schema.json"
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_seeded_manifest_matches_versioned_contract_artifact(client, session, settings) -> None:
    expected_payload = _read_json(DEMO_ROOT / "seed-manifest.json")
    expected_digest = (DEMO_ROOT / "seed-manifest.sha256").read_text(encoding="utf-8").strip()
    viewer_schema = _read_json(VIEWER_SCHEMA_PATH)

    ViewerManifest.model_validate(expected_payload)
    Draft202012Validator(viewer_schema).validate(expected_payload)
    assert sha256_hex(expected_payload) == expected_digest

    first = seed_demo(session, settings)
    session.commit()
    second = seed_demo(session, settings)

    response = client.get(f"/api/v1/incident/{first.fire_id}/manifest")
    unchanged = client.get(
        f"/api/v1/incident/{first.fire_id}/manifest",
        headers={"If-None-Match": f'"{expected_digest}"'},
    )

    assert first.created is True
    assert second.created is False
    assert first.manifest_etag == second.manifest_etag == f'"{expected_digest}"'
    assert response.status_code == 200
    assert response.json() == expected_payload
    assert response.headers["ETag"] == f'"{expected_digest}"'
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["ETag"] == f'"{expected_digest}"'


def test_versioned_visibility_matrix_matches_canonical_backend_policy() -> None:
    matrix = _read_json(DEMO_ROOT / "visibility-matrix.json")
    scenarios = matrix["scenarios"]

    assert matrix["matrix_version"] == "2.0"
    assert {scenario["status"] for scenario in scenarios} == {
        status.value for status in IncidentStatus
    }

    for scenario in scenarios:
        status = IncidentStatus(scenario["status"])
        verification = VerificationState(scenario["verification_state"])
        visibility = PublicVisibility(scenario["visibility"])
        exposes = scenario["exposes"]
        model_state = scenario["model_state"]

        assert scenario["http_status"] == 200
        assert canonical_public_visibility(status, verification) == visibility
        assert exposes["location"] is permits_public_location(status, visibility, verification)

        if model_state == "available":
            assert permits_public_viewer_asset(status, visibility, verification)
            assert exposes == {"location": True, "asset": True, "frame": True}
        elif model_state == "not_available":
            assert exposes == {"location": True, "asset": False, "frame": False}
        else:
            assert model_state == "withheld"
            assert exposes == {"location": False, "asset": False, "frame": False}

    tombstone = matrix["tombstone"]
    assert tombstone["visibility"] == PublicVisibility.TOMBSTONED.value
    assert tombstone["http_status"] == 410
    assert tombstone["content_type"] == "application/problem+json"
    assert tombstone["exposes"] == {"location": False, "asset": False, "frame": False}
