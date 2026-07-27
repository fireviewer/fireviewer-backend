from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from fire_viewer.db.models import (
    SpatialPackage,
    SpatialZone,
    SpatialZoneRevision,
    ZonePublication,
    ZonePublicationEvent,
)
from fire_viewer.domain.enums import SpatialPackageState, ZonePublicationState
from fire_viewer.domain.errors import ConflictError
from fire_viewer.domain.spatial import RAF20_GRID_SHA256
from fire_viewer.domain.zone_publication import assert_zone_publication_transition


def seed_publication_dependencies(
    session: Session,
    *,
    suffix: str = "1",
) -> tuple[SpatialZone, SpatialZoneRevision, SpatialPackage]:
    zone = SpatialZone(zone_id=f"publication-zone-{suffix}", label="Publication zone")
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
        package_id=f"pkg-publication-{suffix}",
        manifest_uri=f"s3://fire-viewer-admin/packages/pkg-publication-{suffix}/manifest.json",
        manifest_sha256="a" * 64,
        manifest_size_bytes=512,
        storage_uri=f"s3://fire-viewer-admin/packages/pkg-publication-{suffix}/",
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
    package.spatial_zone_revision_id = revision.id
    return zone, revision, package


def create_draft_publication(
    session: Session,
    *,
    zone: SpatialZone,
    revision: SpatialZoneRevision,
    package: SpatialPackage,
    suffix: str,
) -> ZonePublication:
    publication = ZonePublication(
        publication_id=f"pub-zone-{suffix}-r{revision.revision}",
        spatial_zone_id=zone.id,
        spatial_zone_revision_id=revision.id,
        spatial_package_id=package.id,
        state=ZonePublicationState.DRAFT,
        is_active=False,
        reason="Draft publication created.",
        actor_id="admin-ui-test",
    )
    session.add(publication)
    session.flush()
    session.add(
        ZonePublicationEvent(
            event_id=f"{publication.publication_id}-draft",
            zone_publication_id=publication.id,
            from_state=None,
            to_state=ZonePublicationState.DRAFT,
            action="create",
            reason="Draft publication created.",
            actor_id="admin-ui-test",
            event_metadata={"source": "admin"},
        )
    )
    session.flush()
    return publication


def transition_publication(
    session: Session,
    publication: ZonePublication,
    target: ZonePublicationState,
    *,
    reason: str,
) -> None:
    event = ZonePublicationEvent(
        event_id=(
            f"{publication.publication_id}-{target.value.lower()}-{len(publication.events) + 1}"
        ),
        zone_publication_id=publication.id,
        from_state=publication.state,
        to_state=target,
        action=target.value.lower(),
        reason=reason,
        actor_id="admin-ui-test",
        event_metadata={"source": "admin"},
    )
    session.add(event)
    session.flush()
    publication.state = target
    publication.is_active = target == ZonePublicationState.PUBLISHED
    publication.reason = reason
    publication.actor_id = "admin-ui-test"
    session.flush()


def publish_publication(
    session: Session, publication: ZonePublication, package: SpatialPackage
) -> None:
    transition_publication(
        session,
        publication,
        ZonePublicationState.VERIFIED,
        reason="Package verification accepted.",
    )
    package.state = SpatialPackageState.PREVIEWABLE
    session.flush()
    transition_publication(
        session,
        publication,
        ZonePublicationState.PREVIEWABLE,
        reason="Private preview accepted.",
    )
    package.state = SpatialPackageState.PUBLISHED
    session.flush()
    transition_publication(
        session,
        publication,
        ZonePublicationState.PUBLISHED,
        reason="Explicit public publication.",
    )


def test_zone_publication_state_machine_allows_documented_path_and_blocks_skips() -> None:
    assert_zone_publication_transition(ZonePublicationState.DRAFT, ZonePublicationState.VERIFIED)
    assert_zone_publication_transition(
        ZonePublicationState.VERIFIED,
        ZonePublicationState.PREVIEWABLE,
    )
    assert_zone_publication_transition(
        ZonePublicationState.PREVIEWABLE,
        ZonePublicationState.PUBLISHED,
    )
    assert_zone_publication_transition(
        ZonePublicationState.PUBLISHED,
        ZonePublicationState.WITHDRAWN,
    )

    with pytest.raises(ConflictError):
        assert_zone_publication_transition(
            ZonePublicationState.DRAFT,
            ZonePublicationState.PUBLISHED,
        )


def test_zone_publication_published_state_is_the_single_active_revision(session: Session) -> None:
    zone, revision, package = seed_publication_dependencies(session)
    publication = create_draft_publication(
        session,
        zone=zone,
        revision=revision,
        package=package,
        suffix="1",
    )
    publish_publication(session, publication, package)
    session.commit()

    duplicate = create_draft_publication(
        session,
        zone=zone,
        revision=revision,
        package=package,
        suffix="1-duplicate",
    )
    transition_publication(
        session,
        duplicate,
        ZonePublicationState.VERIFIED,
        reason="Package verification accepted.",
    )
    transition_publication(
        session,
        duplicate,
        ZonePublicationState.PREVIEWABLE,
        reason="Private preview accepted.",
    )
    with pytest.raises(IntegrityError):
        transition_publication(
            session,
            duplicate,
            ZonePublicationState.PUBLISHED,
            reason="Duplicate active publication.",
        )
    session.rollback()


def test_zone_publication_events_are_append_only_in_sqlite(session: Session) -> None:
    zone, revision, package = seed_publication_dependencies(session, suffix="events")
    publication = create_draft_publication(
        session,
        zone=zone,
        revision=revision,
        package=package,
        suffix="events",
    )
    transition_publication(
        session,
        publication,
        ZonePublicationState.VERIFIED,
        reason="Verification report passed.",
    )
    session.commit()

    publication.events[-1].reason = "tamper"
    with pytest.raises(DBAPIError):
        session.commit()
    session.rollback()

    event = session.get(ZonePublicationEvent, publication.events[-1].id)
    assert event is not None
    with pytest.raises(DBAPIError):
        session.execute(
            text("DELETE FROM zone_publication_event WHERE id = :event_id"), {"event_id": event.id}
        )
    session.rollback()


def test_zone_publication_rejects_mismatched_zone_revision_and_package(session: Session) -> None:
    first_zone, first_revision, first_package = seed_publication_dependencies(
        session, suffix="first"
    )
    _, second_revision, second_package = seed_publication_dependencies(session, suffix="second")
    session.commit()

    session.add(
        ZonePublication(
            publication_id="pub-mismatched-zone-revision",
            spatial_zone_id=first_zone.id,
            spatial_zone_revision_id=second_revision.id,
            spatial_package_id=second_package.id,
            state=ZonePublicationState.DRAFT,
            is_active=False,
            reason="Must be rejected.",
            actor_id="admin-ui-test",
        )
    )
    with pytest.raises(DBAPIError, match="revision must belong to its zone"):
        session.commit()
    session.rollback()

    session.add(
        ZonePublication(
            publication_id="pub-mismatched-package-revision",
            spatial_zone_id=first_zone.id,
            spatial_zone_revision_id=first_revision.id,
            spatial_package_id=second_package.id,
            state=ZonePublicationState.DRAFT,
            is_active=False,
            reason="Must be rejected.",
            actor_id="admin-ui-test",
        )
    )
    with pytest.raises(DBAPIError, match="package must match a verified revision"):
        session.commit()
    session.rollback()
    assert first_package.spatial_zone_revision_id == first_revision.id


def test_zone_publication_requires_matching_event_and_rejects_raw_invalid_state(
    session: Session,
) -> None:
    zone, revision, package = seed_publication_dependencies(session, suffix="transition")
    publication = create_draft_publication(
        session,
        zone=zone,
        revision=revision,
        package=package,
        suffix="transition",
    )
    session.commit()

    with pytest.raises(DBAPIError, match="transition requires matching append-only event"):
        session.execute(
            text("UPDATE zone_publication SET state = 'VERIFIED' WHERE id = :publication_id"),
            {"publication_id": publication.id},
        )
    session.rollback()

    with pytest.raises(DBAPIError):
        session.execute(
            text("UPDATE zone_publication SET state = 'INVALID' WHERE id = :publication_id"),
            {"publication_id": publication.id},
        )
    session.rollback()

    publication = session.get(ZonePublication, publication.id)
    assert publication is not None
    transition_publication(
        session,
        publication,
        ZonePublicationState.VERIFIED,
        reason="Verified through append-only event.",
    )
    session.commit()
    assert publication.state == ZonePublicationState.VERIFIED

    with pytest.raises(DBAPIError, match="zone publications are non-destructive"):
        session.execute(
            text("DELETE FROM zone_publication WHERE id = :publication_id"),
            {"publication_id": publication.id},
        )
    session.rollback()


def test_zone_publication_requires_draft_creation_event_before_first_transition(
    session: Session,
) -> None:
    zone, revision, package = seed_publication_dependencies(session, suffix="creation-event")
    publication = ZonePublication(
        publication_id="pub-zone-creation-event-r1",
        spatial_zone_id=zone.id,
        spatial_zone_revision_id=revision.id,
        spatial_package_id=package.id,
        state=ZonePublicationState.DRAFT,
        is_active=False,
        reason="Draft publication created.",
        actor_id="admin-ui-test",
    )
    session.add(publication)
    session.commit()

    session.add(
        ZonePublicationEvent(
            event_id="pub-zone-creation-event-r1-verified",
            zone_publication_id=publication.id,
            from_state=ZonePublicationState.DRAFT,
            to_state=ZonePublicationState.VERIFIED,
            action="verify",
            reason="Must not exist before the creation event.",
            actor_id="admin-ui-test",
            event_metadata={},
        )
    )
    with pytest.raises(DBAPIError, match="transition requires draft creation event"):
        session.commit()
    session.rollback()
