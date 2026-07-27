from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from fire_viewer.db.models import (
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import IncidentStatus, SpatialPackageFileKind, SpatialPackageState
from fire_viewer.domain.spatial import RAF20_GRID_SHA256


def _seed_draft_package(
    session,
    *,
    package_id: str = "pkg-workflow-01",
    include_png: bool = True,
    tiled: bool = False,
    include_fwterrain: bool = True,
) -> None:
    zone = SpatialZone(zone_id="PACKAGE-WORKFLOW-01", label="Package workflow")
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
    session.add(
        SpatialPackage(
            package_id=package_id,
            manifest_uri=f"s3://private/{package_id}/manifest.json",
            manifest_sha256="a" * 64,
            manifest_size_bytes=512,
            storage_uri=f"s3://private/{package_id}/",
            state=SpatialPackageState.DRAFT,
            provenance={"pipeline": "test"},
            verification_report={},
            created_by="test",
            files=(
                [
                    SpatialPackageFile(
                        kind=SpatialPackageFileKind.PNG,
                        uri=f"s3://private/{package_id}/preview.png",
                        sha256="b" * 64,
                        size_bytes=1024,
                        media_type="image/png",
                        provenance={},
                    ),
                ]
                if include_png
                else []
            )
            + (
                [
                    SpatialPackageFile(
                        kind=SpatialPackageFileKind.JPEG,
                        uri=f"s3://private/{package_id}/far/global.jpg",
                        sha256="e" * 64,
                        size_bytes=2048,
                        media_type="image/jpeg",
                        provenance={"catalog_path": "assets/far/global.jpg"},
                    ),
                    SpatialPackageFile(
                        kind=SpatialPackageFileKind.FWTILE,
                        uri=f"s3://private/{package_id}/detail/tile.fwtile",
                        sha256="c" * 64,
                        size_bytes=2048,
                        media_type="application/vnd.fireviewer.tile",
                        provenance={"catalog_path": "assets/detail/tile.fwtile"},
                    ),
                    *(
                        [
                            SpatialPackageFile(
                                kind=SpatialPackageFileKind.FWTERRAIN,
                                uri=f"s3://private/{package_id}/far/global.fwterrain",
                                sha256="d" * 64,
                                size_bytes=2048,
                                media_type="application/vnd.fireviewer.terrain",
                                provenance={"catalog_path": "assets/far/global.fwterrain"},
                            )
                        ]
                        if include_fwterrain
                        else []
                    ),
                ]
                if tiled
                else [
                    SpatialPackageFile(
                        kind=SpatialPackageFileKind.GLB,
                        uri=f"s3://private/{package_id}/model.glb",
                        sha256="c" * 64,
                        size_bytes=2048,
                        media_type="model/gltf-binary",
                        provenance={},
                    ),
                ]
            ),
        )
    )
    session.commit()


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def test_admin_can_validate_preview_link_and_publish_registered_spatial_package(
    client, session, seed_incident
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-26-00901",
        sequence=901,
        lon=5.2601,
        lat=44.7555,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    _seed_draft_package(session)
    base = "/api/v1/admin/zones/PACKAGE-WORKFLOW-01/revisions/1"
    validation = client.post(
        f"{base}/validations",
        json={"package_id": "pkg-workflow-01", "reason": "Validation du package spatial de test."},
        headers=_headers("package-workflow-validation-0001"),
    )
    assert validation.status_code == 200
    assert validation.json()["publication"].get("package_state") == "VERIFIED"
    assert validation.json()["publication"].get("publication_state") == "VERIFIED"

    replay = client.post(
        f"{base}/validations",
        json={"package_id": "pkg-workflow-01", "reason": "Validation du package spatial de test."},
        headers=_headers("package-workflow-validation-0001"),
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == validation.json()

    preview = client.post(
        f"{base}/preview",
        json={"package_id": "pkg-workflow-01", "reason": "Aperçu privé validé par l'operateur."},
        headers=_headers("package-workflow-preview-0001"),
    )
    assert preview.status_code == 200
    assert preview.json()["publication"]["package_state"] == "PREVIEWABLE"
    assert preview.json()["publication"]["publication_state"] == "PREVIEWABLE"

    attached = client.post(
        f"/api/v2/admin/incidents/{incident.fire_id}/representations",
        json={
            "package_id": "pkg-workflow-01",
            "expected_incident_version": incident.version,
            "primary_profile": "local",
            "reason": "Rattachement explicite de la carte à l'incident de test.",
        },
        headers=_headers("package-workflow-attach-0001"),
    )
    assert attached.status_code == 200, attached.text

    publication = client.post(
        "/api/v1/admin/publications",
        json={
            "zone_id": "PACKAGE-WORKFLOW-01",
            "revision": 1,
            "package_id": "pkg-workflow-01",
            "reason": "Publication explicite après revue privée.",
        },
        headers=_headers("package-workflow-publication-0001"),
    )
    assert publication.status_code == 200
    assert publication.json()["publication"]["package_state"] == "PUBLISHED"
    assert publication.json()["publication"]["publication_state"] == "PUBLISHED"
    assert publication.json()["publication"]["is_active"] is True

    descriptor = client.get(f"{base}/preview")
    assert descriptor.status_code == 200
    assert descriptor.json()["package_state"] == "PUBLISHED"
    assert descriptor.json()["publication_state"] == "PUBLISHED"
    assert descriptor.json()["publication_active"] is True
    assert descriptor.json()["linked_fire_ids"] == [incident.fire_id]
    assert "s3://" not in str(descriptor.json())

    revision_record = session.scalar(
        select(SpatialZoneRevision).where(SpatialZoneRevision.revision == 1)
    )
    assert revision_record is not None
    other_package = SpatialPackage(
        package_id="pkg-workflow-same-revision-draft",
        manifest_uri="s3://private/pkg-workflow-same-revision-draft/manifest.json",
        manifest_sha256="f" * 64,
        manifest_size_bytes=256,
        storage_uri="s3://private/pkg-workflow-same-revision-draft/",
        state=SpatialPackageState.DRAFT,
        provenance={"pipeline": "test"},
        verification_report={},
        created_by="test",
    )
    session.add(other_package)
    session.flush()
    other_package.verification_report = {"status": "passed", "checks": ["test"]}
    other_package.verified_at = datetime.now(UTC)
    other_package.state = SpatialPackageState.VERIFIED
    session.flush()
    other_package.spatial_zone_revision_id = revision_record.id
    session.commit()

    publications = client.get("/api/v1/admin/publications")
    assert publications.status_code == 200
    assert [item["publication_id"] for item in publications.json()["publications"]] == [
        publication.json()["publication"]["publication_id"]
    ]


def test_admin_rejects_preview_when_the_registered_package_has_no_png(client, session) -> None:
    _seed_draft_package(session, package_id="pkg-without-preview", include_png=False)

    response = client.post(
        "/api/v1/admin/zones/PACKAGE-WORKFLOW-01/revisions/1/validations",
        json={"package_id": "pkg-without-preview", "reason": "Tentative sans aperçu PNG déclaré."},
        headers=_headers("package-workflow-missing-preview-0001"),
    )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:fire-viewer:error:spatial_package_missing_preview_assets"


def test_admin_validates_remote_tiles_without_requiring_a_glb_or_png(client, session) -> None:
    _seed_draft_package(
        session,
        package_id="pkg-remote-tiles",
        tiled=True,
        include_png=False,
    )
    base = "/api/v1/admin/zones/PACKAGE-WORKFLOW-01/revisions/1"

    validation = client.post(
        f"{base}/validations",
        json={"package_id": "pkg-remote-tiles", "reason": "Validation du catalogue Unity tuilé."},
        headers=_headers("package-workflow-remote-validation-0001"),
    )
    assert validation.status_code == 200, validation.text
    preview = client.post(
        f"{base}/preview",
        json={"package_id": "pkg-remote-tiles", "reason": "Aperçu privé du catalogue Unity tuilé."},
        headers=_headers("package-workflow-remote-preview-0001"),
    )
    assert preview.status_code == 200, preview.text
    descriptor = client.get(f"{base}/preview")
    assert descriptor.status_code == 200
    assert descriptor.json()["scene"] == {
        "catalog_url": (
            "/api/v1/admin/zones/PACKAGE-WORKFLOW-01/revisions/1/preview/"
            "packages/pkg-remote-tiles/catalog"
        ),
        "files": {
            "assets/detail/tile.fwtile": ("/api/v2/admin/packages/pkg-remote-tiles/files/2"),
            "assets/far/global.fwterrain": ("/api/v2/admin/packages/pkg-remote-tiles/files/3"),
            "assets/far/global.jpg": "/api/v2/admin/packages/pkg-remote-tiles/files/1",
        },
    }


def test_admin_rejects_an_incomplete_remote_tile_profile(client, session) -> None:
    _seed_draft_package(
        session,
        package_id="pkg-remote-incomplete",
        tiled=True,
        include_fwterrain=False,
    )

    response = client.post(
        "/api/v1/admin/zones/PACKAGE-WORKFLOW-01/revisions/1/validations",
        json={"package_id": "pkg-remote-incomplete", "reason": "Tentative sans terrain FAR."},
        headers=_headers("package-workflow-remote-incomplete-0001"),
    )
    assert response.status_code == 409
    assert "FWTERRAIN" in response.json()["detail"]
