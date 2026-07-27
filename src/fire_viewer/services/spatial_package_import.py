"""Creation and controlled import of immutable spatial package registry entries."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.db.models import SpatialZone, SpatialZoneRevision
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.errors import NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminZoneRevisionCreateRequest,
    AdminZoneRevisionEnvelope,
    AdminZoneRevisionSummary,
)
from fire_viewer.domain.spatial import (
    PRODUCTION_GROUND_MODEL,
    PRODUCTION_GROUND_RESOLUTION_M,
    PRODUCTION_HORIZONTAL_CRS,
    PRODUCTION_SURFACE_HEIGHT_REFERENCE,
    PRODUCTION_VERTICAL_CRS,
    RAF20_GRID_SHA256,
    SPATIAL_PROFILE_VERSION,
    wgs84_to_lambert93,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.idempotency import find_replay, store_response


@dataclass(frozen=True, slots=True)
class SpatialImportOutcome:
    response: AdminZoneRevisionEnvelope
    replayed: bool


def _revision_response(row: SpatialZoneRevision) -> AdminZoneRevisionSummary:
    return AdminZoneRevisionSummary(
        revision=row.revision,
        spatial_profile_version=row.spatial_profile_version,
        origin_l93_ngf=(
            row.origin_easting_l93,
            row.origin_northing_l93,
            row.source_orthometric_height_m,
        )
        if row.origin_easting_l93 is not None and row.origin_northing_l93 is not None
        else None,
        horizontal_crs=row.horizontal_crs,
        vertical_crs=row.vertical_crs,
        ground_model=row.ground_model,
        ground_resolution_m=row.ground_resolution_m,
        surface_height_reference=row.surface_height_reference,
        origin_wgs84=(row.origin_lon, row.origin_lat, row.origin_ellipsoid_height_m),
        local_frame="ENU",
        meters_per_unit=row.meters_per_unit,
        vertical_datum=row.vertical_datum,
        bounds_m={
            "east": (row.min_east_m, row.max_east_m),
            "north": (row.min_north_m, row.max_north_m),
            "up": (row.min_up_m, row.max_up_m),
        },
    )


def create_spatial_revision(
    session: Session,
    *,
    zone_id: str,
    payload: AdminZoneRevisionCreateRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> SpatialImportOutcome:
    endpoint = f"POST /api/v1/admin/zones/{zone_id}/revisions"
    request_hash = sha256_hex(
        {"actor_id": actor.actor_id, "payload": payload.model_dump(mode="json")}
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
        return SpatialImportOutcome(
            AdminZoneRevisionEnvelope.model_validate(replay.response_body),
            True,
        )
    zone = session.execute(
        select(SpatialZone).where(SpatialZone.zone_id == zone_id).with_for_update()
    ).scalar_one_or_none()
    if zone is None:
        raise NotFoundError("spatial_zone", zone_id)
    revision_number = (
        int(
            session.execute(
                select(func.max(SpatialZoneRevision.revision)).where(
                    SpatialZoneRevision.spatial_zone_id == zone.id
                )
            ).scalar_one()
            or 0
        )
        + 1
    )
    east_min, east_max, north_min, north_max, up_min, up_max = payload.bounds_m
    origin_easting_l93, origin_northing_l93 = wgs84_to_lambert93(
        payload.origin_lon, payload.origin_lat
    )
    row = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=revision_number,
        spatial_profile_version=SPATIAL_PROFILE_VERSION,
        origin_easting_l93=origin_easting_l93,
        origin_northing_l93=origin_northing_l93,
        horizontal_crs=PRODUCTION_HORIZONTAL_CRS,
        vertical_crs=PRODUCTION_VERTICAL_CRS,
        ground_model=PRODUCTION_GROUND_MODEL,
        ground_resolution_m=PRODUCTION_GROUND_RESOLUTION_M,
        surface_height_reference=PRODUCTION_SURFACE_HEIGHT_REFERENCE,
        origin_lon=payload.origin_lon,
        origin_lat=payload.origin_lat,
        source_orthometric_height_m=payload.source_orthometric_height_m,
        geoid_undulation_m=payload.geoid_undulation_m,
        origin_ellipsoid_height_m=(
            payload.source_orthometric_height_m + payload.geoid_undulation_m
        ),
        vertical_grid_sha256=RAF20_GRID_SHA256,
        min_east_m=east_min,
        max_east_m=east_max,
        min_north_m=north_min,
        max_north_m=north_max,
        min_up_m=up_min,
        max_up_m=up_max,
    )
    session.add(row)
    session.flush()
    response = AdminZoneRevisionEnvelope(revision=_revision_response(row), trace_id=trace_id)
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_zone_revision.created",
        target_type="spatial_zone_revision",
        target_id=f"{zone_id}/r{row.revision}",
        reason=payload.reason,
        trace_id=trace_id,
        after=response.revision.model_dump(mode="json"),
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
    return SpatialImportOutcome(response, False)
