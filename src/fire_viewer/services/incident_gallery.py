"""Validated 3D map captures for the incident gallery."""

from __future__ import annotations

import hashlib
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    Episode,
    IncidentMapCapture,
    IncidentSeries,
)
from fire_viewer.domain.enums import ActiveFireZoneReviewState
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.incident_spatial_schemas import (
    AdminIncidentMapCapture,
    IncidentMapCaptureFinalizeRequest,
    IncidentMapCaptureUploadGrantRequest,
)
from fire_viewer.services.blob_uploads import (
    ALLOWED_GALLERY_CONTENT_TYPES,
    INCIDENT_GALLERY_MAX_BYTES,
    BlobUploadGrant,
    create_gallery_blob_upload_grant,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.storage import build_object_store
from fire_viewer.storage.object_store import ObjectStorageError

_PARIS = ZoneInfo("Europe/Paris")


def _incident_and_episode(session: Session, fire_id: str) -> tuple[IncidentSeries, Episode]:
    incident = session.execute(
        select(IncidentSeries)
        .where(IncidentSeries.fire_id == fire_id)
        .options(selectinload(IncidentSeries.episodes))
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = next((item for item in incident.episodes if item.is_current), None)
    if episode is None:
        raise ConflictError("incident_without_current_episode", "Incident has no current episode.")
    return incident, episode


def _reviewed_zone(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    zone_revision_id: str,
) -> ActiveFireZoneRevision:
    zone = session.execute(
        select(ActiveFireZoneRevision)
        .where(
            ActiveFireZoneRevision.zone_revision_id == zone_revision_id,
            ActiveFireZoneRevision.incident_id == incident.id,
            ActiveFireZoneRevision.episode_id == episode.id,
        )
        .options(selectinload(ActiveFireZoneRevision.analysis_window))
    ).scalar_one_or_none()
    if zone is None:
        raise NotFoundError("active_fire_zone_revision", zone_revision_id)
    if zone.review_state != ActiveFireZoneReviewState.READY_FOR_PUBLICATION:
        raise ConflictError(
            "map_capture_zone_not_reviewed",
            "A map capture requires a human-approved activity-zone layer.",
        )
    return zone


def grant_map_capture_upload(
    session: Session,
    *,
    fire_id: str,
    payload: IncidentMapCaptureUploadGrantRequest,
    actor: Actor,
    settings: Settings,
) -> BlobUploadGrant:
    incident, episode = _incident_and_episode(session, fire_id)
    _reviewed_zone(
        session,
        incident=incident,
        episode=episode,
        zone_revision_id=payload.zone_revision_id,
    )
    if payload.media_type not in ALLOWED_GALLERY_CONTENT_TYPES:
        raise BadRequestError("map_capture_media_type", "Unsupported map capture type.")
    return create_gallery_blob_upload_grant(
        fire_id=fire_id,
        zone_revision_id=payload.zone_revision_id,
        size_bytes=payload.size_bytes,
        actor=actor,
        settings=settings,
    )


def map_capture_response(item: IncidentMapCapture, fire_id: str) -> AdminIncidentMapCapture:
    return AdminIncidentMapCapture(
        capture_id=item.capture_id,
        zone_revision_id=item.active_zone_revision.zone_revision_id,
        local_date=item.local_date,
        captured_at=as_utc(item.captured_at),
        image_url=f"/api/v1/admin/incidents/{fire_id}/map-gallery/{item.capture_id}",
        width_px=item.width_px,
        height_px=item.height_px,
    )


def finalize_map_capture(
    session: Session,
    *,
    fire_id: str,
    payload: IncidentMapCaptureFinalizeRequest,
    actor: Actor,
    settings: Settings,
    trace_id: str,
) -> AdminIncidentMapCapture:
    incident, episode = _incident_and_episode(session, fire_id)
    zone = _reviewed_zone(
        session,
        incident=incident,
        episode=episode,
        zone_revision_id=payload.zone_revision_id,
    )
    store = build_object_store(settings)
    expected_prefix = store.pathname_for(f"gallery-captures/{payload.upload_id}")
    expected_pathname = f"{expected_prefix}/{payload.object.path}"
    if payload.object.pathname != expected_pathname:
        raise BadRequestError(
            "map_capture_path_mismatch", "The map capture is outside its granted upload path."
        )
    if payload.object.content_type not in ALLOWED_GALLERY_CONTENT_TYPES:
        raise BadRequestError("map_capture_media_type", "Unsupported map capture type.")
    object_uri = store.uri_for_pathname(expected_pathname)
    replay = session.execute(
        select(IncidentMapCapture)
        .where(IncidentMapCapture.object_uri == object_uri)
        .options(selectinload(IncidentMapCapture.active_zone_revision))
    ).scalar_one_or_none()
    if replay is not None:
        return map_capture_response(replay, fire_id)
    try:
        metadata = store.head(object_uri)
        content = store.read_bytes(object_uri)
    except ObjectStorageError as exc:
        raise ConflictError(
            "map_capture_object_unavailable", "The uploaded map capture is unavailable."
        ) from exc
    if (
        metadata.pathname != expected_pathname
        or metadata.size_bytes != payload.object.size_bytes
        or len(content) != payload.object.size_bytes
        or len(content) > INCIDENT_GALLERY_MAX_BYTES
        or (
            metadata.content_type is not None
            and metadata.content_type != payload.object.content_type
        )
    ):
        raise ConflictError(
            "map_capture_integrity_failed", "The uploaded map capture metadata is inconsistent."
        )
    try:
        with Image.open(BytesIO(content)) as image:
            width_px, height_px = image.size
            image_format = image.format
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise BadRequestError(
            "map_capture_invalid_image", "The map capture is not a valid image."
        ) from exc
    expected_format = "JPEG" if payload.object.content_type == "image/jpeg" else "PNG"
    if image_format != expected_format or width_px < 640 or height_px < 360:
        raise BadRequestError(
            "map_capture_invalid_image",
            "The map capture type or dimensions do not meet the gallery contract.",
        )
    captured_at = utcnow()
    local_date = (
        zone.analysis_window.local_date
        if zone.analysis_window is not None
        else as_utc(zone.valid_at).astimezone(_PARIS).date()
    )
    capture = IncidentMapCapture(
        capture_id=new_prefixed_id("mapcap"),
        incident_id=incident.id,
        episode_id=episode.id,
        active_zone_revision_id=zone.id,
        local_date=local_date,
        object_uri=object_uri,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type=payload.object.content_type,
        width_px=width_px,
        height_px=height_px,
        captured_at=captured_at,
        created_by=actor.actor_id,
    )
    capture.active_zone_revision = zone
    session.add(capture)
    session.flush()
    record_operator_audit(
        session,
        actor=actor,
        action="incident.map_capture_published",
        target_type="incident_map_capture",
        target_id=capture.capture_id,
        reason="Vue 3D avec calque incendie contrôlée et ajoutée à la galerie par un opérateur.",
        trace_id=trace_id,
        after={
            "zone_revision_id": zone.zone_revision_id,
            "local_date": local_date.isoformat(),
            "sha256": capture.sha256,
        },
    )
    session.commit()
    return map_capture_response(capture, fire_id)
