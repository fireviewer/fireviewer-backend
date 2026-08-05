"""Atomic project-scoped import of one private 3D base map."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import (
    IncidentPerimeterPackage,
    IncidentSeries,
    ManifestRevision,
    SpatialPackage,
    SpatialZone,
    SpatialZoneRevision,
    ZoneProfile,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import SpatialPackageState, ZoneVisibility
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminIncidentPerimeterPackageImportResponse,
    AdminIncidentRepresentationAttachRequest,
    AdminIncidentSpatialPackageFromBlobRequest,
    AdminIncidentSpatialPackageImportResponse,
)
from fire_viewer.domain.spatial import (
    PRODUCTION_GROUND_MODEL,
    PRODUCTION_GROUND_RESOLUTION_M,
    PRODUCTION_HORIZONTAL_CRS,
    PRODUCTION_SURFACE_HEIGHT_REFERENCE,
    PRODUCTION_VERTICAL_CRS,
    RAF20_GRID_SHA256,
    SPATIAL_PROFILE_VERSION,
    SpatialProfileError,
    derive_raf20_origin,
    lambert93_to_wgs84,
    wgs84_to_lambert93,
)
from fire_viewer.services.admin_representations import (
    attach_incident_package_in_transaction,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.idempotency import find_replay, store_response
from fire_viewer.services.spatial_package_blob_import import (
    ValidatedBlobPackage,
    persist_validated_blob_package,
)
from fire_viewer.services.spatial_package_publication import (
    make_spatial_package_previewable,
)


@dataclass(frozen=True, slots=True)
class IncidentSpatialPackageImportOutcome:
    response: AdminIncidentSpatialPackageImportResponse
    replayed: bool


@dataclass(frozen=True, slots=True)
class IncidentPerimeterPackageImportOutcome:
    response: AdminIncidentPerimeterPackageImportResponse
    replayed: bool


def _ensure_spatial_revision(
    session: Session,
    *,
    incident: IncidentSeries,
    payload: AdminIncidentSpatialPackageFromBlobRequest,
    validated: ValidatedBlobPackage,
    actor: Actor,
    trace_id: str,
) -> SpatialZoneRevision:
    revision = session.execute(
        select(SpatialZoneRevision)
        .join(SpatialZone)
        .where(
            SpatialZone.zone_id == payload.zone_id,
            SpatialZoneRevision.revision == payload.revision,
        )
        .options(selectinload(SpatialZoneRevision.zone))
        .with_for_update()
    ).scalar_one_or_none()
    if revision is not None:
        return revision

    profile = validated.spatial_profile
    if profile is None:
        raise BadRequestError(
            "package_spatial_profile_missing",
            "The package cannot create its map reference automatically.",
        )
    try:
        incident_easting, incident_northing = wgs84_to_lambert93(
            incident.reference_lon, incident.reference_lat
        )
        origin_lon, origin_lat = lambert93_to_wgs84(
            profile.origin_easting_l93, profile.origin_northing_l93
        )
        vertical = derive_raf20_origin(
            origin_lon,
            origin_lat,
            profile.source_orthometric_height_m,
        )
    except SpatialProfileError as exc:
        raise BadRequestError(
            "unsupported_package_spatial_profile",
            "The package spatial reference is not supported.",
        ) from exc
    if not (
        profile.min_easting_l93 <= incident_easting <= profile.max_easting_l93
        and profile.min_northing_l93 <= incident_northing <= profile.max_northing_l93
    ):
        raise ConflictError(
            "incident_outside_spatial_package",
            "The incident position is outside the uploaded 3D map.",
        )

    zone = session.execute(
        select(SpatialZone)
        .where(SpatialZone.zone_id == payload.zone_id)
        .options(selectinload(SpatialZone.profile))
        .with_for_update()
    ).scalar_one_or_none()
    incident_label = (incident.canonical_name or incident.fire_id).strip()
    zone_created = zone is None
    if zone is None:
        zone = SpatialZone(zone_id=payload.zone_id, label=incident_label)
        session.add(zone)
        session.flush()
    if zone.profile is None:
        session.add(
            ZoneProfile(
                zone=zone,
                description=f"Fond cartographique 3D associé à l'incident {incident.fire_id}.",
                visibility=ZoneVisibility.DRAFT,
                min_easting_l93=profile.min_easting_l93,
                min_northing_l93=profile.min_northing_l93,
                max_easting_l93=profile.max_easting_l93,
                max_northing_l93=profile.max_northing_l93,
            )
        )
    else:
        zone.profile.min_easting_l93 = min(zone.profile.min_easting_l93, profile.min_easting_l93)
        zone.profile.min_northing_l93 = min(zone.profile.min_northing_l93, profile.min_northing_l93)
        zone.profile.max_easting_l93 = max(zone.profile.max_easting_l93, profile.max_easting_l93)
        zone.profile.max_northing_l93 = max(zone.profile.max_northing_l93, profile.max_northing_l93)

    revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=payload.revision,
        spatial_profile_version=SPATIAL_PROFILE_VERSION,
        origin_easting_l93=profile.origin_easting_l93,
        origin_northing_l93=profile.origin_northing_l93,
        horizontal_crs=PRODUCTION_HORIZONTAL_CRS,
        vertical_crs=PRODUCTION_VERTICAL_CRS,
        ground_model=PRODUCTION_GROUND_MODEL,
        ground_resolution_m=PRODUCTION_GROUND_RESOLUTION_M,
        surface_height_reference=PRODUCTION_SURFACE_HEIGHT_REFERENCE,
        origin_lon=origin_lon,
        origin_lat=origin_lat,
        source_orthometric_height_m=vertical.source_orthometric_height_m,
        geoid_undulation_m=vertical.geoid_undulation_m,
        origin_ellipsoid_height_m=vertical.ellipsoid_height_m,
        vertical_grid_sha256=RAF20_GRID_SHA256,
        min_east_m=profile.min_east_m,
        max_east_m=profile.max_east_m,
        min_north_m=profile.min_north_m,
        max_north_m=profile.max_north_m,
        min_up_m=profile.min_up_m,
        max_up_m=profile.max_up_m,
    )
    session.add(revision)
    session.flush()
    if zone_created:
        record_operator_audit(
            session,
            actor=actor,
            action="spatial_zone.created_from_incident_package",
            target_type="zone",
            target_id=zone.zone_id,
            reason=payload.reason,
            trace_id=trace_id,
            after={"zone_id": zone.zone_id, "label": zone.label},
        )
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_zone_revision.created_from_incident_package",
        target_type="spatial_zone_revision",
        target_id=f"{zone.zone_id}/r{revision.revision}",
        reason=payload.reason,
        trace_id=trace_id,
        after={
            "zone_id": zone.zone_id,
            "revision": revision.revision,
            "origin_l93_ngf": [
                profile.origin_easting_l93,
                profile.origin_northing_l93,
                profile.source_orthometric_height_m,
            ],
        },
    )
    return revision


def import_incident_spatial_package(
    session: Session,
    *,
    fire_id: str,
    payload: AdminIncidentSpatialPackageFromBlobRequest,
    validated: ValidatedBlobPackage,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> IncidentSpatialPackageImportOutcome:
    """Register, validate, preview and attach the map in one database transaction."""

    endpoint = f"POST /api/v2/admin/incidents/{fire_id}/spatial-package/from-blob"
    request_hash = sha256_hex(
        {
            "actor_id": actor.actor_id,
            "fire_id": fire_id,
            "payload": payload.model_dump(mode="json"),
        }
    )
    begin_write_transaction(session)
    replay = find_replay(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay:
        session.rollback()
        return IncidentSpatialPackageImportOutcome(
            AdminIncidentSpatialPackageImportResponse.model_validate(replay.response_body),
            True,
        )

    incident = session.execute(
        select(IncidentSeries)
        .where(IncidentSeries.fire_id == fire_id)
        .options(selectinload(IncidentSeries.episodes))
        .with_for_update()
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    revision = _ensure_spatial_revision(
        session,
        incident=incident,
        payload=payload,
        validated=validated,
        actor=actor,
        trace_id=trace_id,
    )

    package = persist_validated_blob_package(
        session,
        zone_id=payload.zone_id,
        revision=payload.revision,
        validated=validated,
        actor=actor,
        settings=settings,
    )
    make_spatial_package_previewable(
        session,
        revision=revision,
        package=package,
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    attachment = attach_incident_package_in_transaction(
        session,
        incident=incident,
        package=package,
        payload=AdminIncidentRepresentationAttachRequest(
            package_id=package.package_id,
            expected_incident_version=payload.expected_incident_version,
            primary_profile=payload.primary_profile,
            reason=payload.reason,
        ),
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response = AdminIncidentSpatialPackageImportResponse(
        fire_id=attachment.fire_id,
        episode_id=attachment.episode_id,
        package_id=package.package_id,
        package_state=package.state,
        zone_id=payload.zone_id,
        revision=payload.revision,
        manifest_revision=attachment.manifest_revision,
        incident_version=attachment.incident_version,
        object_count=validated.object_count,
        total_size_bytes=validated.total_size_bytes,
        asset_count=len(package.files),
        trace_id=trace_id,
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=201,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return IncidentSpatialPackageImportOutcome(response, False)


def import_incident_perimeter_package(
    session: Session,
    *,
    fire_id: str,
    payload: AdminIncidentSpatialPackageFromBlobRequest,
    validated: ValidatedBlobPackage,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> IncidentPerimeterPackageImportOutcome:
    """Attach one verified OpenUSD perimeter package without replacing the map."""

    endpoint = f"POST /api/v2/admin/incidents/{fire_id}/perimeter-package/from-blob"
    request_hash = sha256_hex(
        {
            "actor_id": actor.actor_id,
            "fire_id": fire_id,
            "payload": payload.model_dump(mode="json"),
        }
    )
    begin_write_transaction(session)
    replay = find_replay(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay:
        session.rollback()
        return IncidentPerimeterPackageImportOutcome(
            AdminIncidentPerimeterPackageImportResponse.model_validate(replay.response_body),
            True,
        )
    if validated.package_role != "omniverse_perimeter" or validated.base_map is None:
        raise BadRequestError(
            "unexpected_package_role", "A perimeter package is required for this attachment."
        )
    incident = session.execute(
        select(IncidentSeries)
        .where(IncidentSeries.fire_id == fire_id)
        .with_for_update()
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    if incident.version != payload.expected_incident_version:
        raise ConflictError(
            "stale_incident_version",
            "The incident was modified by another operation.",
            extra={"current_version": incident.version},
        )
    current_manifest = session.execute(
        select(ManifestRevision)
        .where(
            ManifestRevision.incident_id == incident.id,
            ManifestRevision.is_current.is_(True),
        )
        .options(
            selectinload(ManifestRevision.package).selectinload(SpatialPackage.files),
            selectinload(ManifestRevision.spatial_zone_revision).selectinload(
                SpatialZoneRevision.zone
            ),
        )
        .with_for_update()
    ).scalar_one_or_none()
    base_package = current_manifest.package if current_manifest is not None else None
    base_revision = (
        current_manifest.spatial_zone_revision if current_manifest is not None else None
    )
    base_map = validated.base_map
    if (
        base_package is None
        or base_revision is None
        or base_package.package_id != base_map["package_id"]
        or base_package.provenance.get("package_role") != "omniverse_map"
        or base_package.provenance.get("package_revision") != base_map["revision"]
        or base_package.provenance.get("contract_sha256") != base_map["contract_sha256"]
        or base_package.provenance.get("acceptance_sha256")
        != base_map["acceptance_receipt_sha256"]
        or base_revision.zone.zone_id != payload.zone_id
        or base_revision.revision != payload.revision
        or base_revision.horizontal_crs != base_map["horizontal_crs"]
    ):
        raise ConflictError(
            "perimeter_base_map_not_attached",
            "The perimeter contract does not match the incident's current Omniverse map.",
        )
    package = persist_validated_blob_package(
        session,
        zone_id=payload.zone_id,
        revision=payload.revision,
        validated=validated,
        actor=actor,
        settings=settings,
    )
    package.spatial_zone_revision_id = base_revision.id
    package.state = SpatialPackageState.VERIFIED
    package.verified_at = utcnow()
    package.verification_report = {
        **package.verification_report,
        "status": "passed",
        "scope": "separate-openusd-perimeter-layer",
        "checks": [
            "immutable-file-inventory",
            "active-contract",
            "base-map-identifier",
            "base-map-contract-hash",
            "base-map-acceptance-hash",
            "spatial-reference",
            "ordered-timeline",
        ],
        "base_map_package_id": base_package.package_id,
        "state_count": validated.state_count,
    }
    session.add(
        IncidentPerimeterPackage(
            incident_id=incident.id,
            spatial_package_id=package.id,
            base_spatial_package_id=base_package.id,
            created_by=actor.actor_id,
        )
    )
    session.flush()
    response = AdminIncidentPerimeterPackageImportResponse(
        fire_id=incident.fire_id,
        package_id=package.package_id,
        package_state=package.state,
        base_map_package_id=base_package.package_id,
        zone_id=payload.zone_id,
        revision=payload.revision,
        incident_version=incident.version,
        object_count=validated.object_count,
        total_size_bytes=validated.total_size_bytes,
        asset_count=len(package.files),
        state_count=validated.state_count or 0,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="incident.perimeter_package.attached",
        target_type="incident_series",
        target_id=incident.fire_id,
        reason=payload.reason,
        trace_id=trace_id,
        after=response.model_dump(mode="json"),
        payload={"base_map_package_id": base_package.package_id},
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=201,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return IncidentPerimeterPackageImportOutcome(response, False)
