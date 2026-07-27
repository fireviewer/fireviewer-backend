from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from fire_viewer.db.models import (
    AuditEvent,
    ManifestRevision,
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
    ZoneProfile,
    ZonePublication,
)
from fire_viewer.domain.enums import (
    IncidentStatus,
    SpatialPackageFileKind,
    SpatialPackageState,
    ZonePublicationState,
)
from fire_viewer.domain.schemas import AdminSpatialPackageFromBlobRequest
from fire_viewer.domain.spatial import wgs84_to_lambert93
from fire_viewer.services.spatial_package_blob_import import validate_blob_package
from fire_viewer.storage import ObjectMetadata

_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_GLB = b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00"
_UPLOAD_ID = "1" * 32


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def _create_zone_and_revision(client) -> None:
    created = client.post(
        "/api/v1/admin/zones",
        json={
            "zone_id": "IMPORT-TEST-01",
            "label": "Import test zone",
            "description": "Référence technique utilisée pour les essais d'import contrôlé.",
            "bounds_l93_m": [900_000.0, 6_400_000.0, 901_000.0, 6_401_000.0],
            "reason": "Création de la référence technique pour les essais d'import.",
        },
        headers=_headers("spatial-import-zone-0001"),
    )
    assert created.status_code == 201
    revision = client.post(
        "/api/v1/admin/zones/IMPORT-TEST-01/revisions",
        json={
            "origin_lon": 5.2601,
            "origin_lat": 44.7555,
            "source_orthometric_height_m": 410.0,
            "geoid_undulation_m": 50.0,
            "bounds_m": [-100.0, 100.0, -120.0, 120.0, -10.0, 50.0],
            "reason": "Création de la première révision spatiale de test.",
        },
        headers=_headers("spatial-import-revision-0001"),
    )
    assert revision.status_code == 201
    assert revision.json()["revision"]["revision"] == 1


def _package_documents(*, package_id: str, revision: int = 1) -> dict[str, bytes]:
    assets = {
        "assets/preview.png": _PNG,
        "assets/model.glb": _GLB,
    }
    catalog = {
        "assets": [
            {
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_count": len(payload),
            }
            for path, payload in assets.items()
        ]
    }
    catalog_raw = json.dumps(catalog, separators=(",", ":")).encode()
    manifest = {
        "package_id": package_id,
        "catalog": {
            "path": "catalog.json",
            "sha256": hashlib.sha256(catalog_raw).hexdigest(),
            "byte_count": len(catalog_raw),
        },
        "zones": [{"zone_id": "IMPORT-TEST-01", "revision_id": f"R{revision}"}],
    }
    return {
        "package-manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "catalog.json": catalog_raw,
        **assets,
    }


def _remote_tile_package_documents(
    *,
    package_id: str,
    zone_id: str,
    origin_lon: float,
    origin_lat: float,
    origin_height_m: float = 410.0,
) -> dict[str, bytes]:
    origin_easting, origin_northing = wgs84_to_lambert93(origin_lon, origin_lat)
    origin = [origin_easting, origin_northing, origin_height_m]
    bounds = [
        origin_easting - 500.0,
        origin_northing - 600.0,
        origin_easting + 700.0,
        origin_northing + 800.0,
    ]
    terrain_header = json.dumps(
        {
            "schema": "fireviewer.fwtile.v1",
            "kind": "global_far_terrain",
            "crs": "EPSG:2154",
            "origin_l93_m": origin,
            "bounds_l93_m": bounds,
            "sections": [
                {
                    "name": "terrain",
                    "metadata": {
                        "elevation_quantization": {
                            "minimum_m": -35.0,
                            "maximum_m": 65.0,
                        }
                    },
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    far_terrain = (
        b"FWTILE1\0"
        + (1).to_bytes(2, "little")
        + b"\0\0"
        + len(terrain_header).to_bytes(4, "little")
        + terrain_header
    )
    assets = {
        "assets/far/global.jpg": b"far-imagery",
        "assets/far/global.fwterrain": far_terrain,
        "assets/imagery/tile.jpg": b"detail-imagery",
        "assets/detail/tile/tile.fwtile": b"detail-terrain",
    }

    def reference(path: str) -> dict[str, Any]:
        return {
            "path": path,
            "sha256": hashlib.sha256(assets[path]).hexdigest(),
            "byte_count": len(assets[path]),
        }

    catalog = {
        "schema": "fireviewer.remote-tile-catalog.v1",
        "crs": "EPSG:2154",
        "linear_unit": "metre",
        "origin_l93_m": origin,
        "lod_policy": {
            "far": {
                "bounds_l93_m": bounds,
                "terrain": reference("assets/far/global.fwterrain"),
                "imagery": reference("assets/far/global.jpg"),
            }
        },
        "tiles": [
            {
                "id": "tile",
                "payload": reference("assets/detail/tile/tile.fwtile"),
                "imagery": reference("assets/imagery/tile.jpg"),
            }
        ],
    }
    catalog_raw = json.dumps(catalog, separators=(",", ":")).encode()
    manifest = {
        "package_id": package_id,
        "catalog": {
            "path": "catalog.json",
            "sha256": hashlib.sha256(catalog_raw).hexdigest(),
            "byte_count": len(catalog_raw),
        },
        "zones": [{"zone_id": zone_id, "revision_id": "R1"}],
    }
    return {
        "package-manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "catalog.json": catalog_raw,
        **assets,
    }


def _stage_documents(settings, documents: dict[str, bytes], *, upload_id: str):
    root = settings.zone_upload_storage_dir / "packages" / upload_id
    objects: list[dict[str, Any]] = []
    for path, content in documents.items():
        target = root / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        objects.append(
            {
                "path": path,
                "pathname": f"packages/{upload_id}/{path}",
                "size_bytes": len(content),
                "content_type": _content_type(path),
            }
        )
    return objects


def _content_type(path: str) -> str:
    return {
        ".json": "application/json",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".glb": "model/gltf-binary",
        ".fwtile": "application/vnd.fireviewer.tile",
        ".fwterrain": "application/vnd.fireviewer.terrain",
    }[Path(path).suffix]


def _stage_blob_objects(
    settings,
    *,
    package_id: str,
    revision: int = 1,
    upload_id: str = _UPLOAD_ID,
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    documents = _package_documents(package_id=package_id, revision=revision)
    root = settings.zone_upload_storage_dir / "packages" / upload_id
    objects: list[dict[str, Any]] = []
    for path, payload in documents.items():
        target = root / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        objects.append(
            {
                "path": path,
                "pathname": f"packages/{upload_id}/{path}",
                "size_bytes": len(payload),
                "content_type": _content_type(path),
            }
        )
    return documents, objects


def _finalize(client, *, package_id: str, objects: list[dict[str, Any]], key: str):
    return client.post(
        "/api/v1/admin/zones/IMPORT-TEST-01/revisions/1/packages/from-blob",
        json={
            "upload_id": _UPLOAD_ID,
            "package_id": package_id,
            "reason": "Finalisation contrôlée du package spatial envoyé directement.",
            "objects": objects,
        },
        headers=_headers(key),
    )


def _recover(client, *, package_id: str, key: str):
    return client.post(
        "/api/v1/admin/zones/IMPORT-TEST-01/revisions/1/packages/recover-from-blob",
        json={
            "upload_id": _UPLOAD_ID,
            "package_id": package_id,
            "reason": "Reprise contrôlée de la finalisation du package déjà stocké.",
        },
        headers=_headers(key),
    )


def test_large_blob_inventory_uses_one_listing_and_two_metadata_heads(settings) -> None:
    package_id = "pkg-large-unity-r1"
    asset_count = 952
    asset_digest = hashlib.sha256(b"x").hexdigest()
    catalog = {
        "assets": [
            {
                "path": f"assets/detail/tile-{index:04d}.fwtile",
                "sha256": asset_digest,
                "byte_count": 1,
            }
            for index in range(asset_count)
        ]
    }
    catalog_raw = json.dumps(catalog, separators=(",", ":")).encode()
    manifest_raw = json.dumps(
        {
            "package_id": package_id,
            "catalog": {
                "path": "catalog.json",
                "sha256": hashlib.sha256(catalog_raw).hexdigest(),
                "byte_count": len(catalog_raw),
            },
            "zones": [{"zone_id": "IMPORT-TEST-01", "revision_id": "R1"}],
        },
        separators=(",", ":"),
    ).encode()
    documents = {
        "package-manifest.json": manifest_raw,
        "catalog.json": catalog_raw,
        **{f"assets/detail/tile-{index:04d}.fwtile": b"x" for index in range(asset_count)},
    }
    storage_key = f"packages/{_UPLOAD_ID}"
    content_types = {path: _content_type(path) for path in documents}
    objects = [
        {
            "path": path,
            "pathname": f"{storage_key}/{path}",
            "size_bytes": len(content),
            "content_type": content_types[path],
        }
        for path, content in documents.items()
    ]

    class RecordingStore:
        def __init__(self) -> None:
            self.list_calls = 0
            self.head_calls: list[str] = []
            self.read_calls: list[str] = []

        def pathname_for(self, key: str) -> str:
            return key

        def uri_for(self, key: str) -> str:
            return f"local://{key}"

        def list_prefix(self, key: str, *, limit: int) -> list[ObjectMetadata]:
            self.list_calls += 1
            assert key == storage_key
            assert limit == 2_001
            return [
                ObjectMetadata(
                    pathname=f"{storage_key}/{path}",
                    size_bytes=len(content),
                    content_type=None,
                )
                for path, content in documents.items()
            ][:limit]

        def head(self, uri: str) -> ObjectMetadata:
            self.head_calls.append(uri)
            path = uri.removeprefix(f"local://{storage_key}/")
            return ObjectMetadata(
                pathname=f"{storage_key}/{path}",
                size_bytes=len(documents[path]),
                content_type=content_types[path],
            )

        def read_bytes(self, uri: str) -> bytes:
            self.read_calls.append(uri)
            return documents[uri.removeprefix(f"local://{storage_key}/")]

    store = RecordingStore()
    validated = validate_blob_package(
        zone_id="IMPORT-TEST-01",
        revision=1,
        payload=AdminSpatialPackageFromBlobRequest.model_validate(
            {
                "upload_id": _UPLOAD_ID,
                "package_id": package_id,
                "reason": "Finalisation contrôlée du grand package spatial Unity.",
                "objects": objects,
            }
        ),
        settings=settings.model_copy(update={"zone_upload_max_files": 2_000}),
        store=store,  # type: ignore[arg-type]
    )

    assert validated.object_count == 954
    assert len(validated.asset_catalog) == asset_count
    assert store.list_calls == 1
    assert set(store.head_calls) == {
        f"local://{storage_key}/package-manifest.json",
        f"local://{storage_key}/catalog.json",
    }
    assert set(store.read_calls) == set(store.head_calls)


def test_admin_finalizes_exact_blob_inventory_then_previews_and_publishes(
    client, settings, session, seed_incident
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-26-00902",
        sequence=902,
        lon=5.2601,
        lat=44.7555,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    _create_zone_and_revision(client)
    documents, objects = _stage_blob_objects(settings, package_id="pkg-import-test-r1")

    response = _finalize(
        client,
        package_id="pkg-import-test-r1",
        objects=objects,
        key="spatial-import-package-0001",
    )

    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["package"] == {
        "package_id": "pkg-import-test-r1",
        "state": "DRAFT",
        "upload_id": _UPLOAD_ID,
        "object_count": 4,
        "total_size_bytes": sum(map(len, documents.values())),
        "asset_count": 2,
        "validation_summary": "Stored Blob inventory and package metadata were verified.",
    }

    replay = _finalize(
        client,
        package_id="pkg-import-test-r1",
        objects=objects,
        key="spatial-import-package-0001",
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == response.json()

    package = session.scalar(
        select(SpatialPackage).where(SpatialPackage.package_id == "pkg-import-test-r1")
    )
    assert package is not None
    stored_files = list(
        session.scalars(
            select(SpatialPackageFile).where(SpatialPackageFile.spatial_package_id == package.id)
        )
    )
    assert {item.uri for item in stored_files} == {
        f"local://packages/{_UPLOAD_ID}/assets/model.glb",
        f"local://packages/{_UPLOAD_ID}/assets/preview.png",
    }
    assert session.scalar(
        select(AuditEvent).where(AuditEvent.action == "spatial_package.finalized_from_blob")
    )

    base = "/api/v1/admin/zones/IMPORT-TEST-01/revisions/1"
    validated = client.post(
        f"{base}/validations",
        json={
            "package_id": "pkg-import-test-r1",
            "reason": "Validation des contrôles spatiaux et des hashes.",
        },
        headers=_headers("spatial-import-validation-0001"),
    )
    assert validated.status_code == 200
    preview = client.post(
        f"{base}/preview",
        json={
            "package_id": "pkg-import-test-r1",
            "reason": "Activation de l'aperçu privé après validation.",
        },
        headers=_headers("spatial-import-preview-0001"),
    )
    assert preview.status_code == 200
    image = client.get(f"{base}/preview/packages/pkg-import-test-r1/png")
    assert image.status_code == 200
    assert image.headers["Cache-Control"] == "no-store"
    assert image.content == _PNG
    attached = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/representations",
        json={
            "package_id": "pkg-import-test-r1",
            "expected_incident_version": incident.version,
            "primary_profile": "local",
            "reason": "Rattachement explicite du package importé à l'incident de test.",
        },
        headers=_headers("spatial-import-attach-0001"),
    )
    assert attached.status_code == 200, attached.text
    published = client.post(
        "/api/v1/admin/publications",
        json={
            "zone_id": "IMPORT-TEST-01",
            "revision": 1,
            "package_id": "pkg-import-test-r1",
            "reason": "Publication après comparaison et revue privée.",
        },
        headers=_headers("spatial-import-publish-0001"),
    )
    assert published.status_code == 200
    assert published.json()["publication"]["package_state"] == "PUBLISHED"


def test_admin_imports_map_inside_incident_project_atomically(
    client, settings, session, seed_incident
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-26-00912",
        sequence=912,
        lon=5.2601,
        lat=44.7555,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    expected_incident_version = incident.version
    _create_zone_and_revision(client)
    documents, objects = _stage_blob_objects(
        settings,
        package_id="pkg-project-map-r1",
    )

    response = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/spatial-package/from-blob",
        json={
            "upload_id": _UPLOAD_ID,
            "package_id": "pkg-project-map-r1",
            "zone_id": "IMPORT-TEST-01",
            "revision": 1,
            "expected_incident_version": expected_incident_version,
            "primary_profile": "local",
            "reason": "Import du fond 3D directement depuis le projet incendie de test.",
            "objects": objects,
        },
        headers=_headers("incident-project-map-import-0001"),
    )

    assert response.status_code == 201, response.text
    assert response.headers["Idempotent-Replay"] == "false"
    assert response.json() == {
        "fire_id": incident.fire_id,
        "episode_id": "E01",
        "package_id": "pkg-project-map-r1",
        "package_state": "PREVIEWABLE",
        "zone_id": "IMPORT-TEST-01",
        "revision": 1,
        "manifest_revision": 1,
        "incident_version": expected_incident_version + 1,
        "object_count": 4,
        "total_size_bytes": sum(map(len, documents.values())),
        "asset_count": 2,
        "trace_id": response.json()["trace_id"],
    }
    session.expire_all()
    package = session.scalar(
        select(SpatialPackage).where(SpatialPackage.package_id == "pkg-project-map-r1")
    )
    assert package is not None
    assert package.state == SpatialPackageState.PREVIEWABLE
    publication = session.scalar(
        select(ZonePublication).where(ZonePublication.spatial_package_id == package.id)
    )
    assert publication is not None
    assert publication.state == ZonePublicationState.PREVIEWABLE
    manifest = session.scalar(
        select(ManifestRevision).where(
            ManifestRevision.spatial_package_id == package.id,
            ManifestRevision.is_current.is_(True),
        )
    )
    assert manifest is not None
    assert manifest.incident_id == incident.id
    workspace = client.get(f"/api/v1/admin/incidents/{incident.fire_id}/spatial-review")
    assert workspace.status_code == 200, workspace.text
    scene = workspace.json()["scene"]
    assert scene["zone_id"] == "IMPORT-TEST-01"
    assert scene["zone_revision"] == 1
    assert scene["package_id"] == "pkg-project-map-r1"
    assert scene["package_state"] == "PREVIEWABLE"
    assert scene["publication_id"] == publication.publication_id
    assert scene["publication_state"] == "PREVIEWABLE"
    assert scene["publication_active"] is False
    assert scene["catalog_url"] == (
        "/api/v1/admin/zones/IMPORT-TEST-01/revisions/1/preview/packages/pkg-project-map-r1/catalog"
    )
    assert scene["files"]
    assert all(
        url.startswith("/api/v2/admin/packages/pkg-project-map-r1/files/")
        for url in scene["files"].values()
    )
    private_catalog = client.get(scene["catalog_url"])
    assert private_catalog.status_code == 200, private_catalog.text
    private_file = client.get(next(iter(scene["files"].values())))
    assert private_file.status_code == 200, private_file.text

    replay = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/spatial-package/from-blob",
        json={
            "upload_id": _UPLOAD_ID,
            "package_id": "pkg-project-map-r1",
            "zone_id": "IMPORT-TEST-01",
            "revision": 1,
            "expected_incident_version": expected_incident_version,
            "primary_profile": "local",
            "reason": "Import du fond 3D directement depuis le projet incendie de test.",
            "objects": objects,
        },
        headers=_headers("incident-project-map-import-0001"),
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == response.json()


def test_incident_project_map_import_creates_missing_zone_and_revision(
    client, settings, session, seed_incident
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-77-00914",
        sequence=914,
        lon=2.588886,
        lat=48.3894574,
        canonical_name="Forêt de Fontainebleau",
        status=IncidentStatus.MONITORING,
    )
    package_id = "pkg-fontainebleau-auto-r1"
    zone_id = "FONTAINEBLEAU-AUTO-01"
    upload_id = "2" * 32
    documents = _remote_tile_package_documents(
        package_id=package_id,
        zone_id=zone_id,
        origin_lon=incident.reference_lon,
        origin_lat=incident.reference_lat,
    )
    objects = _stage_documents(settings, documents, upload_id=upload_id)

    response = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/spatial-package/from-blob",
        json={
            "upload_id": upload_id,
            "package_id": package_id,
            "zone_id": zone_id,
            "revision": 1,
            "expected_incident_version": incident.version,
            "primary_profile": "local",
            "reason": "Import automatique du fond 3D depuis le projet Fontainebleau.",
            "objects": objects,
        },
        headers=_headers("incident-project-map-auto-0001"),
    )

    assert response.status_code == 201, response.text
    session.expire_all()
    zone = session.scalar(select(SpatialZone).where(SpatialZone.zone_id == zone_id))
    assert zone is not None
    assert zone.label == "Forêt de Fontainebleau"
    profile = session.scalar(select(ZoneProfile).where(ZoneProfile.spatial_zone_id == zone.id))
    assert profile is not None
    origin_easting, origin_northing = wgs84_to_lambert93(
        incident.reference_lon, incident.reference_lat
    )
    assert profile.min_easting_l93 == pytest.approx(origin_easting - 500.0)
    assert profile.max_northing_l93 == pytest.approx(origin_northing + 800.0)
    revision = session.scalar(
        select(SpatialZoneRevision).where(
            SpatialZoneRevision.spatial_zone_id == zone.id,
            SpatialZoneRevision.revision == 1,
        )
    )
    assert revision is not None
    assert revision.origin_easting_l93 == pytest.approx(origin_easting)
    assert revision.origin_northing_l93 == pytest.approx(origin_northing)
    assert revision.source_orthometric_height_m == 410.0
    assert revision.min_up_m == -35.0
    assert revision.max_up_m == 65.0
    assert session.scalar(
        select(AuditEvent).where(AuditEvent.action == "spatial_zone.created_from_incident_package")
    )
    assert session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "spatial_zone_revision.created_from_incident_package"
        )
    )


def test_incident_project_map_import_rolls_back_when_incident_version_is_stale(
    client, settings, session, seed_incident
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-26-00913",
        sequence=913,
        lon=5.2601,
        lat=44.7555,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    _create_zone_and_revision(client)
    _documents, objects = _stage_blob_objects(
        settings,
        package_id="pkg-project-map-stale-r1",
    )

    response = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/spatial-package/from-blob",
        json={
            "upload_id": _UPLOAD_ID,
            "package_id": "pkg-project-map-stale-r1",
            "zone_id": "IMPORT-TEST-01",
            "revision": 1,
            "expected_incident_version": incident.version + 1,
            "primary_profile": "local",
            "reason": "Import refusé pour vérifier le rollback atomique du projet.",
            "objects": objects,
        },
        headers=_headers("incident-project-map-import-stale-0001"),
    )

    assert response.status_code == 409
    session.expire_all()
    assert (
        session.scalar(
            select(SpatialPackage).where(SpatialPackage.package_id == "pkg-project-map-stale-r1")
        )
        is None
    )


def test_admin_recovers_a_complete_stored_upload_without_reupload(
    client, settings, session
) -> None:
    _create_zone_and_revision(client)
    documents, _objects = _stage_blob_objects(settings, package_id="pkg-recovered-r1")

    response = _recover(
        client,
        package_id="pkg-recovered-r1",
        key="spatial-import-recovery-0001",
    )

    assert response.status_code == 201, response.text
    assert response.json()["package"] == {
        "package_id": "pkg-recovered-r1",
        "state": "DRAFT",
        "upload_id": _UPLOAD_ID,
        "object_count": 4,
        "total_size_bytes": sum(map(len, documents.values())),
        "asset_count": 2,
        "validation_summary": "Stored Blob inventory and package metadata were verified.",
    }
    package = session.scalar(
        select(SpatialPackage).where(SpatialPackage.package_id == "pkg-recovered-r1")
    )
    assert package is not None
    assert package.provenance["upload_id"] == _UPLOAD_ID


def test_admin_finalizes_unity_remote_tile_inventory(client, settings, session) -> None:
    _create_zone_and_revision(client)
    package_id = "pkg-unity-remote-r1"
    assets = {
        "assets/validation/unity-preview.png": _PNG,
        "assets/far/global.jpg": b"far-imagery",
        "assets/far/global.fwterrain": b"far-terrain",
        "assets/imagery/tile.jpg": b"tile-imagery",
        "assets/detail/tile/tile.fwtile": b"tile-payload",
    }
    catalog = {
        "schema": "fireviewer.remote-tile-catalog.v1",
        "assets": [
            {
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_count": len(payload),
            }
            for path, payload in assets.items()
        ],
    }
    catalog_raw = json.dumps(catalog, separators=(",", ":")).encode()
    manifest = {
        "package_id": package_id,
        "catalog": {
            "path": "catalog.json",
            "sha256": hashlib.sha256(catalog_raw).hexdigest(),
            "byte_count": len(catalog_raw),
        },
        "zones": [{"zone_id": "IMPORT-TEST-01", "revision_id": "R1"}],
    }
    documents = {
        "package-manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
        "catalog.json": catalog_raw,
        **assets,
    }
    root = settings.zone_upload_storage_dir / "packages" / _UPLOAD_ID
    objects: list[dict[str, Any]] = []
    for path, payload in documents.items():
        target = root / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        objects.append(
            {
                "path": path,
                "pathname": f"packages/{_UPLOAD_ID}/{path}",
                "size_bytes": len(payload),
                "content_type": _content_type(path),
            }
        )

    response = _finalize(
        client,
        package_id=package_id,
        objects=objects,
        key="spatial-import-unity-remote-0001",
    )

    assert response.status_code == 201
    package = session.scalar(select(SpatialPackage).where(SpatialPackage.package_id == package_id))
    assert package is not None
    kinds = set(
        session.scalars(
            select(SpatialPackageFile.kind).where(
                SpatialPackageFile.spatial_package_id == package.id
            )
        )
    )
    assert kinds == {
        SpatialPackageFileKind.PNG,
        SpatialPackageFileKind.JPEG,
        SpatialPackageFileKind.FWTILE,
        SpatialPackageFileKind.FWTERRAIN,
    }
    base = "/api/v1/admin/zones/IMPORT-TEST-01/revisions/1"
    validated = client.post(
        f"{base}/validations",
        json={"package_id": package_id, "reason": "Validation du package Unity distant."},
        headers=_headers("spatial-import-unity-remote-validation-0001"),
    )
    assert validated.status_code == 200, validated.text
    preview = client.post(
        f"{base}/preview",
        json={"package_id": package_id, "reason": "Aperçu privé du package Unity distant."},
        headers=_headers("spatial-import-unity-remote-preview-0001"),
    )
    assert preview.status_code == 200, preview.text


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing", "missing_blob_object"),
        ("extra", "package_inventory_mismatch"),
        ("size", "blob_metadata_mismatch"),
        ("revision", "package_revision_mismatch"),
    ],
)
def test_finalization_rejects_incomplete_or_inconsistent_blob_packages(
    client, settings, session, mutation: str, expected_error: str
) -> None:
    _create_zone_and_revision(client)
    revision = 2 if mutation == "revision" else 1
    _, objects = _stage_blob_objects(
        settings,
        package_id=f"pkg-invalid-{mutation}",
        revision=revision,
    )
    if mutation == "missing":
        (settings.zone_upload_storage_dir / objects[-1]["pathname"]).unlink()
    elif mutation == "extra":
        extra = settings.zone_upload_storage_dir / "packages" / _UPLOAD_ID / "assets/extra.png"
        extra.write_bytes(_PNG)
        objects.append(
            {
                "path": "assets/extra.png",
                "pathname": f"packages/{_UPLOAD_ID}/assets/extra.png",
                "size_bytes": len(_PNG),
                "content_type": "image/png",
            }
        )
    elif mutation == "size":
        objects[-1]["size_bytes"] += 1

    response = _finalize(
        client,
        package_id=f"pkg-invalid-{mutation}",
        objects=objects,
        key=f"spatial-import-invalid-{mutation}-0001",
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith(expected_error)
    assert (
        session.scalar(
            select(SpatialPackage).where(SpatialPackage.package_id == f"pkg-invalid-{mutation}")
        )
        is None
    )


def test_finalization_rejects_unsafe_and_unknown_object_paths(client, settings) -> None:
    _create_zone_and_revision(client)
    _, objects = _stage_blob_objects(settings, package_id="pkg-unsafe-path")
    objects[-1]["path"] = "../model.glb"
    response = _finalize(
        client,
        package_id="pkg-unsafe-path",
        objects=objects,
        key="spatial-import-unsafe-path-0001",
    )
    assert response.status_code == 400
    assert response.json()["type"].endswith("unsafe_package_path")

    _, objects = _stage_blob_objects(settings, package_id="pkg-unknown-type")
    objects[-1]["path"] = "assets/model.exe"
    objects[-1]["pathname"] = f"packages/{_UPLOAD_ID}/assets/model.exe"
    objects[-1]["content_type"] = "application/octet-stream"
    response = _finalize(
        client,
        package_id="pkg-unknown-type",
        objects=objects,
        key="spatial-import-unknown-type-0001",
    )
    assert response.status_code == 400
    assert response.json()["type"].endswith("unsupported_package_file_type")


def test_admin_grant_issues_a_prefix_limited_vercel_blob_client_token(client, settings) -> None:
    _create_zone_and_revision(client)
    settings.object_storage_backend = "vercel_blob"
    settings.blob_read_write_token = SecretStr("vercel_blob_rw_teststore_testsecret")
    settings.object_storage_prefix = "fire-viewer"

    grant_response = client.post(
        "/api/v1/admin/zones/IMPORT-TEST-01/revisions/1/packages/upload-grant",
        json={
            "package_id": "pkg-granted-upload",
            "file_count": 4,
            "total_size_bytes": 1024,
        },
    )
    assert grant_response.status_code == 201
    grant = grant_response.json()
    assert grant["pathname_prefix"].startswith("fire-viewer/packages/")

    token_response = client.post(
        "/api/v1/admin/blob-upload-token",
        json={
            "type": "blob.generate-client-token",
            "payload": {
                "pathname": f"{grant['pathname_prefix']}/assets/model.glb",
                "multipart": True,
                "clientPayload": "pkg-granted-upload",
            },
        },
        headers={"X-Blob-Upload-Grant": grant["upload_grant"]},
    )
    assert token_response.status_code == 200
    client_token = token_response.json()["clientToken"]
    token_prefix = "vercel_blob_client_teststore_"
    assert client_token.startswith(token_prefix)

    secured = base64.b64decode(client_token.removeprefix(token_prefix)).decode("ascii")
    signature, encoded_payload = secured.split(".", maxsplit=1)
    expected_signature = hmac.new(
        b"vercel_blob_rw_teststore_testsecret",
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(signature, expected_signature)

    token_payload = json.loads(base64.b64decode(encoded_payload))
    assert token_payload["pathname"] == (f"{grant['pathname_prefix']}/assets/model.glb")
    assert token_payload["allowedContentTypes"] == [
        "application/json",
        "application/vnd.fireviewer.terrain",
        "application/vnd.fireviewer.tile",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/geotiff",
        "model/gltf-binary",
    ]
    assert token_payload["addRandomSuffix"] is False
    assert token_payload["allowOverwrite"] is False
    assert token_payload["maximumSizeInBytes"] == settings.zone_upload_max_bytes
    assert token_payload["validUntil"] > 0

    denied = client.post(
        "/api/v1/admin/blob-upload-token",
        json={
            "type": "blob.generate-client-token",
            "payload": {
                "pathname": "fire-viewer/packages/another-upload/assets/model.glb",
                "multipart": True,
                "clientPayload": "pkg-granted-upload",
            },
        },
        headers={"X-Blob-Upload-Grant": grant["upload_grant"]},
    )
    assert denied.status_code == 403
