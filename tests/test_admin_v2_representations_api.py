from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select

from fire_viewer.db.models import (
    ManifestRevision,
    ModelAsset,
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import (
    IncidentStatus,
    SpatialPackageFileKind,
    SpatialPackageState,
)
from fire_viewer.domain.spatial import derive_raf20_origin


def _seed_previewable_package(
    session, settings, *, remote_tiles: bool = False
) -> tuple[SpatialPackage, bytes, bytes]:
    zone = SpatialZone(zone_id="ZONE-ATTACH", label="Zone à attacher")
    session.add(zone)
    session.flush()
    origin = derive_raf20_origin(6.0214, 43.2897, 400.0)
    revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=origin.source_orthometric_height_m,
        geoid_undulation_m=origin.geoid_undulation_m,
        origin_ellipsoid_height_m=origin.ellipsoid_height_m,
        min_east_m=-1_000.0,
        max_east_m=1_000.0,
        min_north_m=-1_000.0,
        max_north_m=1_000.0,
        min_up_m=-100.0,
        max_up_m=1_000.0,
    )
    session.add(revision)
    session.flush()
    package = SpatialPackage(
        package_id="PKG-ATTACH-R1",
        manifest_uri="local://uploads/attach/content/package-manifest.json",
        manifest_sha256="d" * 64,
        manifest_size_bytes=100,
        storage_uri="local://uploads/attach",
        state=SpatialPackageState.DRAFT,
        provenance={"zone_id": zone.zone_id, "revision": 1},
        verification_report={"status": "imported"},
        created_by="attach-test",
    )
    session.add(package)
    session.flush()
    package.state = SpatialPackageState.VERIFIED
    package.verification_report = {"status": "passed"}
    package.verified_at = datetime(2026, 7, 15, 8, 10, tzinfo=UTC)
    package.spatial_zone_revision_id = revision.id
    session.flush()
    package.state = SpatialPackageState.PREVIEWABLE
    session.flush()

    close_content = b"glTF-close-model"
    local_content = b"glTF-local-model"
    close_path = settings.zone_upload_storage_dir / "uploads/attach/content/assets/close.glb"
    local_path = settings.zone_upload_storage_dir / "uploads/attach/content/assets/local.glb"
    close_path.parent.mkdir(parents=True, exist_ok=True)
    close_path.write_bytes(close_content)
    local_path.write_bytes(local_content)
    if remote_tiles:
        far_content = b"fireviewer-far-terrain"
        tile_content = b"fireviewer-detail-tile"
        far_path = settings.zone_upload_storage_dir / (
            "uploads/attach/content/assets/far/global.fwterrain"
        )
        tile_path = settings.zone_upload_storage_dir / (
            "uploads/attach/content/assets/detail/T00/tile.fwtile"
        )
        far_path.parent.mkdir(parents=True, exist_ok=True)
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        far_path.write_bytes(far_content)
        tile_path.write_bytes(tile_content)
        package_files = [
            SpatialPackageFile(
                spatial_package_id=package.id,
                kind=SpatialPackageFileKind.FWTERRAIN,
                uri="local://uploads/attach/content/assets/far/global.fwterrain",
                sha256=hashlib.sha256(far_content).hexdigest(),
                size_bytes=len(far_content),
                media_type="application/vnd.fireviewer.terrain",
                provenance={"catalog_path": "assets/far/global.fwterrain"},
            ),
            SpatialPackageFile(
                spatial_package_id=package.id,
                kind=SpatialPackageFileKind.FWTILE,
                uri="local://uploads/attach/content/assets/detail/T00/tile.fwtile",
                sha256=hashlib.sha256(tile_content).hexdigest(),
                size_bytes=len(tile_content),
                media_type="application/vnd.fireviewer.tile",
                provenance={"catalog_path": "assets/detail/T00/tile.fwtile"},
            ),
        ]
    else:
        package_files = [
            SpatialPackageFile(
                spatial_package_id=package.id,
                kind=SpatialPackageFileKind.GLB,
                uri="local://uploads/attach/content/assets/close.glb",
                sha256="e" * 64,
                size_bytes=len(close_content),
                media_type="model/gltf-binary",
                provenance={"catalog_path": "assets/close.glb"},
            ),
            SpatialPackageFile(
                spatial_package_id=package.id,
                kind=SpatialPackageFileKind.GLB,
                uri="local://uploads/attach/content/assets/local.glb",
                sha256="f" * 64,
                size_bytes=len(local_content),
                media_type="model/gltf-binary",
                provenance={"catalog_path": "assets/local.glb"},
            ),
        ]
    session.add_all(package_files)
    session.commit()
    return package, close_content, local_content


def test_attach_package_creates_versioned_assets_and_current_manifest(
    client, session, settings, seed_incident
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00201",
        sequence=201,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    package, _, local_content = _seed_previewable_package(session, settings)
    payload = {
        "package_id": package.package_id,
        "expected_incident_version": incident.version,
        "primary_profile": "local",
        "reason": "Association manuelle du paquet local vérifié à cet incendie.",
    }
    headers = {"Idempotency-Key": "attach-package-0001"}

    response = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/representations",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["Idempotent-Replay"] == "false"
    body = response.json()
    assert body["package_id"] == package.package_id
    assert body["episode_id"] == episode.episode_id
    assert len(body["model_asset_ids"]) == 2
    assert body["incident_version"] == incident.version + 1
    manifest = session.execute(
        select(ManifestRevision).where(
            ManifestRevision.incident_id == incident.id,
            ManifestRevision.is_current.is_(True),
        )
    ).scalar_one()
    assert manifest.spatial_package_id == package.id
    assert manifest.asset is not None
    assert manifest.asset.lod.value == "local"
    assert session.execute(select(ModelAsset)).scalars().all()

    replay = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/representations",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == body

    map_response = client.get("/api/v2/admin/operational-map")
    mapped = next(
        item for item in map_response.json()["incidents"] if item["fire_id"] == incident.fire_id
    )
    assert mapped["current_package_id"] == package.package_id
    assert {model["profile"] for model in mapped["models"]} == {"close", "local"}
    assert all(model["is_current"] for model in mapped["models"])
    local_model = next(model for model in mapped["models"] if model["profile"] == "local")

    binary = client.get(local_model["access_path"])
    assert binary.status_code == 200
    assert binary.content == local_content
    assert binary.headers["content-type"] == "model/gltf-binary"
    not_modified = client.get(
        local_model["access_path"], headers={"If-None-Match": binary.headers["etag"]}
    )
    assert not_modified.status_code == 304
    assert not not_modified.content


def test_attach_package_rejects_stale_incident_version(
    client, session, settings, seed_incident
) -> None:
    incident, _ = seed_incident(
        fire_id="FR-83-00202",
        sequence=202,
        lon=6.1214,
        lat=43.3897,
        status=IncidentStatus.MONITORING,
    )
    package, _, _ = _seed_previewable_package(session, settings)

    response = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/representations",
        json={
            "package_id": package.package_id,
            "expected_incident_version": incident.version + 1,
            "primary_profile": "local",
            "reason": "Tentative avec une version d'incident devenue obsolète.",
        },
        headers={"Idempotency-Key": "attach-package-stale-0001"},
    )

    assert response.status_code == 409
    assert response.json()["type"].endswith("stale_incident_version")


def test_attach_tiled_package_publishes_controlled_scene_without_fake_model_asset(
    client, session, settings, seed_incident
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00203",
        sequence=203,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    package, _, _ = _seed_previewable_package(session, settings, remote_tiles=True)
    package.state = SpatialPackageState.PUBLISHED
    catalog = json.dumps({"schema_version": "1.1", "test": "tiled-scene"}).encode()
    catalog_path = settings.zone_upload_storage_dir / "uploads/attach/catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_bytes(catalog)
    session.commit()

    response = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/representations",
        json={
            "package_id": package.package_id,
            "expected_incident_version": incident.version,
            "primary_profile": "local",
            "reason": "Association du catalogue 3D tuilé publié à l'incident confirmé.",
        },
        headers={"Idempotency-Key": "attach-tiled-package-0001"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["primary_asset_id"] is None
    assert response.json()["model_asset_ids"] == []
    manifest_row = session.execute(
        select(ManifestRevision).where(
            ManifestRevision.incident_id == incident.id,
            ManifestRevision.is_current.is_(True),
        )
    ).scalar_one()
    assert manifest_row.asset_id is None
    assert manifest_row.spatial_package_id == package.id

    manifest = client.get(f"/api/v1/incident/{incident.fire_id}/manifest")
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["model_state"] == "available"
    assert manifest.json()["asset"] is None
    assert len(manifest.json()["scene"]["files"]) == 2
    catalog_response = client.get(manifest.json()["scene"]["catalog_url"])
    assert catalog_response.status_code == 200
    assert catalog_response.content == catalog
    tile_file = next(item for item in manifest.json()["scene"]["files"] if item["kind"] == "FWTILE")
    tile_response = client.get(tile_file["url"])
    assert tile_response.status_code == 200
    assert tile_response.content == b"fireviewer-detail-tile"
    range_response = client.get(tile_file["url"], headers={"Range": "bytes=5-9"})
    assert range_response.status_code == 206
    assert range_response.headers["accept-ranges"] == "bytes"
    assert range_response.headers["content-range"] == "bytes 5-9/22"
    assert range_response.content == b"iewer"
