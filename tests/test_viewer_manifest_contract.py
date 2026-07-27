from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fire_viewer.core.config import Settings
from fire_viewer.db.models import (
    Episode,
    IncidentSeries,
    ManifestRevision,
    ModelAsset,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import AssetLod, AssetState, IncidentStatus, PublicVisibility
from fire_viewer.domain.schemas import ViewerManifest
from fire_viewer.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = BACKEND_ROOT / "contracts" / "viewer-manifest" / "v2"
EXAMPLES_ROOT = CONTRACT_ROOT / "examples"
CONTRACT_SCHEMA_PATH = CONTRACT_ROOT / "viewer-manifest.schema.json"
OPENAPI_ARTIFACT_PATH = BACKEND_ROOT / "openapi" / "openapi.json"


def _manifest_path(fire_id: str) -> str:
    return f"/api/v1/incident/{fire_id}/manifest"


def _fixture_payload(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES_ROOT / f"{name}.json").read_text(encoding="utf-8"))


def _normalize_pydantic_references(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_pydantic_references(item)
            for key, item in value.items()
            # FastAPI omits nullable default values from OpenAPI response schemas.
            if not (key == "default" and item is None)
        }
    if isinstance(value, list):
        return [_normalize_pydantic_references(item) for item in value]
    if isinstance(value, str):
        return value.replace("#/$defs/", "#/components/schemas/")
    return value


def _seed_published_asset(
    session,
    incident: IncidentSeries,
    episode: Episode,
) -> None:
    generated_at = datetime(2026, 7, 12, 8, 20, tzinfo=UTC)
    spatial_zone = SpatialZone(
        zone_id="zone-contract-fixture-0001",
        label="Fictitious contract fixture zone",
    )
    session.add(spatial_zone)
    session.flush()
    spatial_zone_revision = SpatialZoneRevision(
        spatial_zone_id=spatial_zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=412.7,
        geoid_undulation_m=49.31100405064734,
        origin_ellipsoid_height_m=462.01100405064733,
        min_east_m=-2_500.0,
        max_east_m=2_500.0,
        min_north_m=-2_500.0,
        max_north_m=2_500.0,
        min_up_m=-500.0,
        max_up_m=2_000.0,
    )
    session.add(spatial_zone_revision)
    session.flush()
    asset = ModelAsset(
        asset_id="asset-contract-fixture-0001",
        spatial_zone_revision_id=spatial_zone_revision.id,
        version=1,
        lod=AssetLod.DESKTOP,
        state=AssetState.PUBLISHED,
        glb_url="https://assets.example.invalid/fire-viewer/FR-83-00042/E01/v1.glb",
        sha256="a" * 64,
        size_bytes=123_456,
        terrain_source_year=2024,
        generated_at=generated_at,
        published_at=generated_at,
    )
    session.add(asset)
    session.flush()
    session.add(
        ManifestRevision(
            incident_id=incident.id,
            episode_id=episode.id,
            asset_id=asset.id,
            spatial_zone_revision_id=spatial_zone_revision.id,
            revision=1,
            is_current=True,
            reason="Published contract fixture for the public viewer.",
            actor_id="contract-fixture",
        )
    )
    session.commit()


@pytest.mark.parametrize("fixture_name", ["available", "not_available", "withheld"])
def test_shared_manifest_fixtures_round_trip_through_pydantic(fixture_name: str) -> None:
    payload = _fixture_payload(fixture_name)

    manifest = ViewerManifest.model_validate(payload)

    assert manifest.model_dump(mode="json", exclude_none=False) == payload


def test_viewer_manifest_schema_version_is_required() -> None:
    payload = _fixture_payload("available")
    payload.pop("schema_version")

    with pytest.raises(ValidationError, match="schema_version"):
        ViewerManifest.model_validate(payload)


def test_versioned_contract_schema_is_the_pydantic_serialization_schema() -> None:
    contract_schema = json.loads(CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))

    assert contract_schema == ViewerManifest.model_json_schema(mode="serialization")


def test_openapi_viewer_manifest_components_match_pydantic_serialization_schema(client) -> None:
    openapi = client.get("/openapi.json").json()
    pydantic_schema = deepcopy(ViewerManifest.model_json_schema(mode="serialization"))
    pydantic_definitions = pydantic_schema.pop("$defs")
    components = openapi["components"]["schemas"]

    assert components["ViewerManifest"] == _normalize_pydantic_references(pydantic_schema)
    for name, definition in pydantic_definitions.items():
        assert components[name] == _normalize_pydantic_references(definition)


def test_checked_in_openapi_artifact_matches_runtime_schema(client) -> None:
    checked_in_openapi = json.loads(OPENAPI_ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert checked_in_openapi == client.get("/openapi.json").json()


def test_canonical_manifest_returns_available_state_with_published_asset(
    client,
    session,
    seed_incident,
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00042",
        sequence=42,
        lon=0.0,
        lat=0.0,
        status=IncidentStatus.MONITORING,
    )
    _seed_published_asset(session, incident, episode)

    response = client.get(_manifest_path(incident.fire_id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "2.0"
    assert payload["status"]["code"] == "MONITORING"
    assert payload["model_state"] == "available"
    assert payload["asset"] is not None
    assert payload["frame"] is not None
    assert response.headers["ETag"].startswith('"')
    assert response.headers["Cache-Control"] == "public, max-age=30, must-revalidate"
    assert response.headers["X-Trace-Id"]


def test_canonical_manifest_returns_not_available_without_published_asset(
    client,
    seed_incident,
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00043",
        sequence=43,
        lon=0.0,
        lat=0.0,
        status=IncidentStatus.MONITORING,
    )

    response = client.get(_manifest_path(incident.fire_id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["code"] == "MONITORING"
    assert payload["model_state"] == "not_available"
    assert payload["location"] is not None
    assert payload["asset"] is None
    assert payload["frame"] is None


def test_canonical_manifest_withholds_public_projection_when_visibility_is_limited(
    client,
    session,
    seed_incident,
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00044",
        sequence=44,
        lon=0.0,
        lat=0.0,
        status=IncidentStatus.CANDIDATE,
    )
    incident.public_visibility = PublicVisibility.LIMITED
    session.commit()

    response = client.get(_manifest_path(incident.fire_id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_state"] == "withheld"
    assert payload["location"] is None
    assert payload["asset"] is None
    assert payload["frame"] is None


def test_canonical_manifest_returns_304_with_the_same_cache_headers(client, seed_incident) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00045",
        sequence=45,
        lon=0.0,
        lat=0.0,
    )

    current = client.get(_manifest_path(incident.fire_id))
    unchanged = client.get(
        _manifest_path(incident.fire_id),
        headers={"If-None-Match": current.headers["ETag"]},
    )

    assert current.status_code == 200
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["ETag"] == current.headers["ETag"]
    assert unchanged.headers["Cache-Control"] == current.headers["Cache-Control"]
    assert unchanged.headers["X-Trace-Id"]


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/api/v1/incident/INVALID/manifest", 400),
        ("/api/v1/incident/FR-83-99999/manifest", 404),
    ],
)
def test_canonical_manifest_problem_responses_include_trace_id(
    client,
    path: str,
    expected_status: int,
) -> None:
    response = client.get(path)

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


def test_canonical_manifest_returns_410_problem_for_tombstoned_incident(
    client,
    session,
    seed_incident,
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00046",
        sequence=46,
        lon=0.0,
        lat=0.0,
    )
    incident.public_visibility = PublicVisibility.TOMBSTONED
    session.commit()

    response = client.get(_manifest_path(incident.fire_id))

    assert response.status_code == 410
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


def test_canonical_manifest_returns_503_problem_without_current_episode(
    client,
    session,
    seed_incident,
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00047",
        sequence=47,
        lon=0.0,
        lat=0.0,
    )
    episode.is_current = False
    session.commit()

    response = client.get(_manifest_path(incident.fire_id))

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


def test_manifest_openapi_documents_only_contract_statuses_and_headers(client) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/api/v1/incident/{fire_id}/manifest"][
        "get"
    ]
    responses = operation["responses"]

    assert set(responses) == {"200", "304", "400", "404", "410", "503"}
    assert "409" not in responses
    assert set(responses["200"]["headers"]) == {"ETag", "Cache-Control", "X-Trace-Id"}
    assert set(responses["304"]["headers"]) == {"ETag", "Cache-Control", "X-Trace-Id"}
    for status_code in ("400", "404", "410", "503"):
        response = responses[status_code]
        assert set(response["content"]) == {"application/problem+json"}
        assert "trace_id" in response["content"]["application/problem+json"]["schema"]["required"]
        assert set(response["headers"]) == {"X-Trace-Id"}


def test_manifest_cors_preflight_allows_if_none_match_and_hides_untrusted_origin(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        database_url=f"sqlite:///{tmp_path / 'cors.db'}",
        trusted_hosts=["testserver", "localhost"],
        cors_origins=["http://localhost:5173", "http://localhost:3000"],
        log_level="CRITICAL",
    )
    app = create_app(settings)
    requested_headers = "If-None-Match, Content-Type"

    with TestClient(app, raise_server_exceptions=False) as cors_client:
        initial_get = cors_client.get("/healthz", headers={"Origin": "http://localhost:5173"})
        allowed = cors_client.options(
            _manifest_path("FR-83-00042"),
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": requested_headers,
            },
        )
        blocked = cors_client.options(
            _manifest_path("FR-83-00042"),
            headers={
                "Origin": "https://untrusted.example.invalid",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": requested_headers,
            },
        )

    assert initial_get.status_code == 200
    assert initial_get.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "if-none-match" in allowed.headers["access-control-allow-headers"].lower()
    assert blocked.status_code == 400
    assert "access-control-allow-origin" not in blocked.headers
