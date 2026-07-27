from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from fire_viewer.db.models import (
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import SpatialPackageFileKind, SpatialPackageState
from fire_viewer.domain.spatial import RAF20_GRID_SHA256


def seed_revision(session: Session, *, suffix: str = "") -> SpatialZoneRevision:
    zone = SpatialZone(zone_id=f"admin-package-zone{suffix}", label="Admin package zone")
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
    return revision


def test_admin_spatial_package_registry_tracks_manifest_files_report_and_revision_link(
    session: Session,
) -> None:
    revision = seed_revision(session)
    package = SpatialPackage(
        package_id="pkg-die-pontaix-r1-0001",
        manifest_uri="s3://fire-viewer-admin/packages/pkg-die-pontaix-r1-0001/manifest.json",
        manifest_sha256="a" * 64,
        manifest_size_bytes=2_048,
        storage_uri="s3://fire-viewer-admin/packages/pkg-die-pontaix-r1-0001/",
        state=SpatialPackageState.DRAFT,
        provenance={"pipeline": "unity-export", "operator": "admin-ui-test"},
        verification_report={},
        created_by="admin-ui-test",
        files=[
            SpatialPackageFile(
                kind=SpatialPackageFileKind.COG,
                uri="s3://fire-viewer-admin/packages/pkg-die-pontaix-r1-0001/terrain.cog.tif",
                sha256="b" * 64,
                size_bytes=4_096,
                media_type="image/geotiff",
                provenance={"source": "ign"},
            ),
            SpatialPackageFile(
                kind=SpatialPackageFileKind.PNG,
                uri="s3://fire-viewer-admin/packages/pkg-die-pontaix-r1-0001/archive.png",
                sha256="c" * 64,
                size_bytes=1_024,
                media_type="image/png",
                provenance={"renderer": "giro3d-private-preview"},
            ),
            SpatialPackageFile(
                kind=SpatialPackageFileKind.GLB,
                uri="s3://fire-viewer-admin/packages/pkg-die-pontaix-r1-0001/model.glb",
                sha256="d" * 64,
                size_bytes=8_192,
                media_type="model/gltf-binary",
                provenance={"pipeline": "unity-export"},
            ),
        ],
    )

    session.add(package)
    session.flush()
    package.verification_report = {"status": "passed", "checks": ["hashes", "spatial-contract"]}
    package.verified_at = datetime.now(UTC)
    package.state = SpatialPackageState.VERIFIED
    session.flush()
    package.spatial_zone_revision_id = revision.id
    session.commit()
    session.refresh(revision)

    assert revision.spatial_packages[0].package_id == "pkg-die-pontaix-r1-0001"
    assert {file.kind for file in revision.spatial_packages[0].files} == {
        SpatialPackageFileKind.COG,
        SpatialPackageFileKind.PNG,
        SpatialPackageFileKind.GLB,
    }
    assert revision.spatial_packages[0].verification_report["status"] == "passed"


def test_admin_spatial_package_rejects_revision_link_before_validation(session: Session) -> None:
    revision = seed_revision(session)
    session.add(
        SpatialPackage(
            package_id="pkg-draft-linked",
            manifest_uri="s3://fire-viewer-admin/packages/pkg-draft-linked/manifest.json",
            manifest_sha256="e" * 64,
            manifest_size_bytes=512,
            storage_uri="s3://fire-viewer-admin/packages/pkg-draft-linked/",
            state=SpatialPackageState.DRAFT,
            provenance={},
            verification_report={},
            created_by="admin-ui-test",
            spatial_zone_revision_id=revision.id,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_admin_spatial_package_files_are_immutable_in_sqlite(session: Session) -> None:
    package = SpatialPackage(
        package_id="pkg-file-immutable",
        manifest_uri="s3://fire-viewer-admin/packages/pkg-file-immutable/manifest.json",
        manifest_sha256="f" * 64,
        manifest_size_bytes=512,
        storage_uri="s3://fire-viewer-admin/packages/pkg-file-immutable/",
        state=SpatialPackageState.DRAFT,
        provenance={},
        verification_report={},
        created_by="admin-ui-test",
        files=[
            SpatialPackageFile(
                kind=SpatialPackageFileKind.PNG,
                uri="s3://fire-viewer-admin/packages/pkg-file-immutable/archive.png",
                sha256="1" * 64,
                size_bytes=1_024,
                media_type="image/png",
                provenance={},
            )
        ],
    )
    session.add(package)
    session.commit()

    package.files[0].size_bytes = 2_048
    with pytest.raises(DBAPIError):
        session.commit()


def test_admin_spatial_package_starts_draft_and_cannot_be_deleted(session: Session) -> None:
    invalid_initial_state = SpatialPackage(
        package_id="pkg-invalid-initial-state",
        manifest_uri="s3://fire-viewer-admin/packages/pkg-invalid-initial-state/manifest.json",
        manifest_sha256="2" * 64,
        manifest_size_bytes=512,
        storage_uri="s3://fire-viewer-admin/packages/pkg-invalid-initial-state/",
        state=SpatialPackageState.VERIFIED,
        provenance={},
        verification_report={"status": "passed"},
        created_by="admin-ui-test",
        verified_at=datetime.now(UTC),
    )
    session.add(invalid_initial_state)
    with pytest.raises(DBAPIError, match="spatial package must start as draft"):
        session.commit()
    session.rollback()

    incomplete_verification = SpatialPackage(
        package_id="pkg-incomplete-verification",
        manifest_uri="s3://fire-viewer-admin/packages/pkg-incomplete-verification/manifest.json",
        manifest_sha256="5" * 64,
        manifest_size_bytes=512,
        storage_uri="s3://fire-viewer-admin/packages/pkg-incomplete-verification/",
        state=SpatialPackageState.DRAFT,
        provenance={},
        verification_report={},
        created_by="admin-ui-test",
    )
    session.add(incomplete_verification)
    session.flush()
    incomplete_verification.verified_at = datetime.now(UTC)
    incomplete_verification.state = SpatialPackageState.VERIFIED
    with pytest.raises(DBAPIError, match="requires passed verification report"):
        session.commit()
    session.rollback()

    package = SpatialPackage(
        package_id="pkg-non-destructive",
        manifest_uri="s3://fire-viewer-admin/packages/pkg-non-destructive/manifest.json",
        manifest_sha256="3" * 64,
        manifest_size_bytes=512,
        storage_uri="s3://fire-viewer-admin/packages/pkg-non-destructive/",
        state=SpatialPackageState.DRAFT,
        provenance={},
        verification_report={},
        created_by="admin-ui-test",
    )
    session.add(package)
    session.commit()

    with pytest.raises(DBAPIError, match="spatial packages are non-destructive"):
        session.execute(
            text("DELETE FROM spatial_package WHERE id = :package_id"), {"package_id": package.id}
        )
    session.rollback()


def test_admin_spatial_package_verification_link_and_state_are_guarded(session: Session) -> None:
    first_revision = seed_revision(session, suffix="-first")
    second_revision = seed_revision(session, suffix="-second")
    package = SpatialPackage(
        package_id="pkg-guarded-lifecycle",
        manifest_uri="s3://fire-viewer-admin/packages/pkg-guarded-lifecycle/manifest.json",
        manifest_sha256="4" * 64,
        manifest_size_bytes=512,
        storage_uri="s3://fire-viewer-admin/packages/pkg-guarded-lifecycle/",
        state=SpatialPackageState.DRAFT,
        provenance={},
        verification_report={},
        created_by="admin-ui-test",
    )
    session.add(package)
    session.flush()
    package.verification_report = {"status": "passed"}
    package.verified_at = datetime.now(UTC)
    package.state = SpatialPackageState.VERIFIED
    session.flush()
    package.spatial_zone_revision_id = first_revision.id
    session.commit()

    package.spatial_zone_revision_id = second_revision.id
    with pytest.raises(DBAPIError, match="zone revision is immutable once attached"):
        session.commit()
    session.rollback()

    package = session.get(SpatialPackage, package.id)
    assert package is not None
    package.verification_report = {"status": "tampered"}
    with pytest.raises(DBAPIError, match="spatial package verification is immutable"):
        session.commit()
    session.rollback()

    package = session.get(SpatialPackage, package.id)
    assert package is not None
    package.state = SpatialPackageState.PUBLISHED
    with pytest.raises(DBAPIError, match="invalid spatial package transition"):
        session.commit()
    session.rollback()
