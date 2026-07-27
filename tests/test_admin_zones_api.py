from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from fire_viewer.db.models import (
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import SpatialPackageFileKind, SpatialPackageState
from fire_viewer.domain.spatial import RAF20_GRID_SHA256


def seed_zone(session: Session) -> None:
    zone = SpatialZone(zone_id="DIE-PONTAIX-08", label="Die-Pontaix")
    session.add(zone)
    session.flush()
    revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=5.2601,
        origin_lat=44.7555,
        source_orthometric_height_m=410.0,
        geoid_undulation_m=50.0,
        origin_ellipsoid_height_m=460.0,
        vertical_grid_sha256=RAF20_GRID_SHA256,
        min_east_m=-100.0,
        max_east_m=100.0,
        min_north_m=-120.0,
        max_north_m=120.0,
        min_up_m=-10.0,
        max_up_m=50.0,
    )
    session.add(revision)
    session.flush()
    package = SpatialPackage(
        package_id="pkg-die-pontaix-private-preview",
        manifest_uri="s3://private-admin/pkg-die-pontaix/manifest.json",
        manifest_sha256="a" * 64,
        manifest_size_bytes=512,
        storage_uri="s3://private-admin/pkg-die-pontaix/",
        state=SpatialPackageState.DRAFT,
        provenance={"pipeline": "unity-export"},
        verification_report={},
        created_by="admin-ui-test",
        files=[
            SpatialPackageFile(
                kind=SpatialPackageFileKind.GLB,
                uri="s3://private-admin/pkg-die-pontaix/model.glb",
                sha256="b" * 64,
                size_bytes=1024,
                media_type="model/gltf-binary",
                provenance={},
            )
        ],
    )
    session.add(package)
    session.flush()
    package.verification_report = {"status": "passed"}
    package.verified_at = datetime.now(UTC)
    package.state = SpatialPackageState.VERIFIED
    session.flush()
    package.spatial_zone_revision_id = revision.id
    package.state = SpatialPackageState.PREVIEWABLE
    session.commit()


def test_admin_session_confirms_the_server_authorized_actor_without_cache(client) -> None:
    response = client.get("/api/v1/admin/session")

    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    assert response.headers["Cache-Control"] == "no-store"


def test_admin_zones_list_and_detail_are_available_to_administrators(client) -> None:
    created = client.post(
        "/api/v1/admin/zones",
        json={
            "zone_id": "DIE-PONTAIX-08",
            "label": "Die-Pontaix",
            "description": "Zone locale administrée pour les essais d'API.",
            "bounds_l93_m": [900_000.0, 6_400_000.0, 901_000.0, 6_401_000.0],
            "reason": "Création de la zone d'essai administrateur.",
        },
        headers={"Idempotency-Key": "admin-zone-list-detail-0001"},
    )

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    assert created.json()["zone"] == {
        "zone_id": "DIE-PONTAIX-08",
        "label": "Die-Pontaix",
        "description": "Zone locale administrée pour les essais d'API.",
        "visibility": "DRAFT",
        "bounds_l93_m": [900_000.0, 6_400_000.0, 901_000.0, 6_401_000.0],
        "created_at": created.json()["zone"]["created_at"],
        "updated_at": created.json()["zone"]["updated_at"],
    }

    listing = client.get("/api/v1/admin/zones")

    assert listing.status_code == 200
    assert listing.headers["Cache-Control"] == "no-store"
    assert len(listing.json()["zones"]) == 1
    listed_zone = listing.json()["zones"][0]
    assert {
        key: listed_zone[key]
        for key in ("zone_id", "label", "description", "visibility", "bounds_l93_m")
    } == {
        key: created.json()["zone"][key]
        for key in ("zone_id", "label", "description", "visibility", "bounds_l93_m")
    }
    assert listed_zone["created_at"]
    assert listed_zone["updated_at"]

    detail = client.get("/api/v1/admin/zones/DIE-PONTAIX-08")
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert detail.json()["uploads"] == []
    assert detail.json()["information"] == []
    assert {
        key: detail.json()["zone"][key]
        for key in ("zone_id", "label", "description", "visibility", "bounds_l93_m")
    } == {
        key: created.json()["zone"][key]
        for key in ("zone_id", "label", "description", "visibility", "bounds_l93_m")
    }


def test_admin_private_preview_exposes_metadata_without_private_file_locations(
    client, session
) -> None:
    seed_zone(session)

    revision = client.get("/api/v1/admin/zones/DIE-PONTAIX-08/revisions/1")
    assert revision.status_code == 200
    assert revision.headers["Cache-Control"] == "no-store"

    response = client.get("/api/v1/admin/zones/DIE-PONTAIX-08/revisions/1/preview")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload["preview_scope"] == "private-admin"
    assert payload["package_id"] == "pkg-die-pontaix-private-preview"
    assert payload["files"] == [
        {
            "file_id": 1,
            "path": None,
            "kind": "GLB",
            "sha256": "b" * 64,
            "size_bytes": 1024,
            "media_type": "model/gltf-binary",
        }
    ]
    assert "s3://private-admin" not in response.text
    assert "model.glb" not in response.text


def test_admin_zone_missing_resources_return_problem_details(client) -> None:
    response = client.get("/api/v1/admin/zones/UNKNOWN-ZONE")

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["type"].endswith("not_found")


def test_admin_zone_id_requires_the_canonical_uppercase_format(client) -> None:
    response = client.get("/api/v1/admin/zones/die-pontaix-08")

    assert response.status_code == 422


def test_publication_listing_returns_an_empty_registry(client) -> None:
    response = client.get("/api/v1/admin/publications")

    assert response.status_code == 200
    assert response.json() == {"publications": []}
    assert response.headers["Cache-Control"] == "no-store"
