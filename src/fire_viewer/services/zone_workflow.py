"""Focused zone administration, publication and public-read workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import (
    SpatialZone,
    ZoneContribution,
    ZoneInformation,
    ZoneProfile,
    ZoneUpload,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import (
    ActorType,
    ZoneContributionState,
    ZoneInformationState,
    ZoneUploadState,
    ZoneVisibility,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminContributionEnvelope,
    AdminContributionListResponse,
    AdminContributionResponse,
    AdminContributionReviewRequest,
    AdminZoneCreateRequest,
    AdminZoneDetailResponse,
    AdminZoneEnvelope,
    AdminZoneInformationCreateRequest,
    AdminZoneInformationEnvelope,
    AdminZoneInformationResponse,
    AdminZoneInformationUpdateRequest,
    AdminZoneListResponse,
    AdminZoneResponse,
    AdminZoneUpdateRequest,
    AdminZoneUploadResponse,
    PublicZoneContributionReceipt,
    PublicZoneContributionRequest,
    PublicZoneInformationItem,
    PublicZoneInformationResponse,
    PublicZoneResponse,
    ZoneVisibilityRequest,
)
from fire_viewer.services.common import record_audit, record_operator_audit
from fire_viewer.services.idempotency import find_replay, store_response

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ZoneMutationOutcome:
    response: BaseModel
    replayed: bool


def _zone_snapshot(zone: SpatialZone, profile: ZoneProfile) -> dict[str, Any]:
    return {
        "zone_id": zone.zone_id,
        "label": zone.label,
        "description": profile.description,
        "visibility": profile.visibility.value,
        "bounds_l93_m": [
            profile.min_easting_l93,
            profile.min_northing_l93,
            profile.max_easting_l93,
            profile.max_northing_l93,
        ],
    }


def _upload_snapshot(upload: ZoneUpload) -> dict[str, Any]:
    return {
        "upload_id": upload.upload_id,
        "revision": upload.revision,
        "package_id": upload.package_id,
        "archive_sha256": upload.archive_sha256,
        "archive_size_bytes": upload.archive_size_bytes,
        "catalog_sha256": upload.catalog_sha256,
        "catalog_size_bytes": upload.catalog_size_bytes,
        "state": upload.state.value,
        "is_active": upload.is_active,
        "asset_count": len(upload.asset_catalog),
    }


def _information_snapshot(information: ZoneInformation) -> dict[str, Any]:
    return {
        "information_id": information.information_id,
        "state": information.state.value,
        "category": information.category,
        "position_l93": [information.easting_l93, information.northing_l93],
        "published_at": information.published_at,
    }


def _zone_response(zone: SpatialZone, profile: ZoneProfile) -> AdminZoneResponse:
    if zone.label is None:
        # Legacy spatial rows predate the presentation profile and are not a valid MVP admin zone.
        raise ConflictError("zone_profile_incomplete", "The zone has no administrative label.")
    return AdminZoneResponse(
        zone_id=zone.zone_id,
        label=zone.label,
        description=profile.description,
        visibility=profile.visibility,
        bounds_l93_m=(
            profile.min_easting_l93,
            profile.min_northing_l93,
            profile.max_easting_l93,
            profile.max_northing_l93,
        ),
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _upload_response(upload: ZoneUpload) -> AdminZoneUploadResponse:
    return AdminZoneUploadResponse(
        upload_id=upload.upload_id,
        file_name=upload.file_name,
        archive_sha256=upload.archive_sha256,
        size_bytes=upload.archive_size_bytes,
        state=upload.state,
        created_at=upload.created_at,
        validation_summary=upload.validation_summary,
    )


def _information_response(information: ZoneInformation) -> AdminZoneInformationResponse:
    return AdminZoneInformationResponse(
        information_id=information.information_id,
        title=information.title,
        body=information.body,
        category=information.category,
        position_l93=(information.easting_l93, information.northing_l93),
        state=information.state,
        updated_at=information.updated_at,
        review_note=information.review_note,
    )


def _contribution_response(contribution: ZoneContribution) -> AdminContributionResponse:
    position = (
        (contribution.easting_l93, contribution.northing_l93)
        if contribution.easting_l93 is not None and contribution.northing_l93 is not None
        else None
    )
    return AdminContributionResponse(
        contribution_id=contribution.contribution_id,
        zone_id=contribution.zone.zone_id if contribution.zone is not None else None,
        title=contribution.title,
        body=contribution.body,
        position_l93=position,
        state=contribution.state,
        submitted_at=contribution.submitted_at,
    )


def _profile_for_admin(
    session: Session,
    *,
    zone_id: str,
    for_update: bool = False,
) -> tuple[SpatialZone, ZoneProfile]:
    statement = (
        select(SpatialZone, ZoneProfile)
        .join(ZoneProfile, ZoneProfile.spatial_zone_id == SpatialZone.id)
        .where(SpatialZone.zone_id == zone_id)
    )
    if for_update:
        statement = statement.with_for_update()
    row = session.execute(statement).one_or_none()
    if row is None:
        raise NotFoundError("zone", zone_id)
    zone, profile = row
    return zone, profile


def _active_public_zone(
    session: Session,
    *,
    zone_id: str,
) -> tuple[SpatialZone, ZoneProfile, ZoneUpload]:
    row = session.execute(
        select(SpatialZone, ZoneProfile, ZoneUpload)
        .join(ZoneProfile, ZoneProfile.spatial_zone_id == SpatialZone.id)
        .join(ZoneUpload, ZoneUpload.spatial_zone_id == SpatialZone.id)
        .where(
            SpatialZone.zone_id == zone_id,
            ZoneProfile.visibility == ZoneVisibility.PUBLISHED,
            ZoneUpload.is_active.is_(True),
            ZoneUpload.state == ZoneUploadState.VALIDATED,
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("published_zone", zone_id)
    zone, profile, upload = row
    return zone, profile, upload


def _position_is_inside(profile: ZoneProfile, position: tuple[float, float]) -> bool:
    return (
        profile.min_easting_l93 <= position[0] <= profile.max_easting_l93
        and profile.min_northing_l93 <= position[1] <= profile.max_northing_l93
    )


def _assert_position_is_inside(profile: ZoneProfile, position: tuple[float, float]) -> None:
    if not _position_is_inside(profile, position):
        raise ConflictError(
            "position_outside_zone",
            "The Lambert-93 position must remain within the zone bounds.",
        )


def _request_hash(actor: Actor, value: dict[str, Any]) -> str:
    return sha256_hex({"actor_id": actor.actor_id, **value})


def create_zone(
    session: Session,
    *,
    payload: AdminZoneCreateRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> ZoneMutationOutcome:
    endpoint = "POST /api/v1/admin/zones"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return ZoneMutationOutcome(AdminZoneEnvelope.model_validate(replay.response_body), True)
    existing = session.execute(
        select(SpatialZone).where(SpatialZone.zone_id == payload.zone_id).with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("zone_already_exists", "A zone with this identifier already exists.")
    zone = SpatialZone(zone_id=payload.zone_id, label=payload.label)
    profile = ZoneProfile(
        zone=zone,
        description=payload.description,
        visibility=ZoneVisibility.DRAFT,
        min_easting_l93=payload.bounds_l93_m[0],
        min_northing_l93=payload.bounds_l93_m[1],
        max_easting_l93=payload.bounds_l93_m[2],
        max_northing_l93=payload.bounds_l93_m[3],
    )
    session.add_all((zone, profile))
    session.flush()
    response = AdminZoneEnvelope(zone=_zone_response(zone, profile), trace_id=trace_id)
    record_operator_audit(
        session,
        actor=actor,
        action="zone.created",
        target_type="zone",
        target_id=zone.zone_id,
        reason=payload.reason,
        trace_id=trace_id,
        after=_zone_snapshot(zone, profile),
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
    return ZoneMutationOutcome(response, False)


def update_zone(
    session: Session,
    *,
    zone_id: str,
    payload: AdminZoneUpdateRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> ZoneMutationOutcome:
    endpoint = f"PATCH /api/v1/admin/zones/{zone_id}"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return ZoneMutationOutcome(AdminZoneEnvelope.model_validate(replay.response_body), True)
    zone, profile = _profile_for_admin(session, zone_id=zone_id, for_update=True)
    before = _zone_snapshot(zone, profile)
    zone.label = payload.label
    profile.description = payload.description
    (
        profile.min_easting_l93,
        profile.min_northing_l93,
        profile.max_easting_l93,
        profile.max_northing_l93,
    ) = payload.bounds_l93_m
    session.flush()
    response = AdminZoneEnvelope(zone=_zone_response(zone, profile), trace_id=trace_id)
    record_operator_audit(
        session,
        actor=actor,
        action="zone.updated",
        target_type="zone",
        target_id=zone.zone_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=_zone_snapshot(zone, profile),
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return ZoneMutationOutcome(response, False)


def set_zone_visibility(
    session: Session,
    *,
    zone_id: str,
    payload: ZoneVisibilityRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> ZoneMutationOutcome:
    endpoint = f"POST /api/v1/admin/zones/{zone_id}/visibility"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return ZoneMutationOutcome(AdminZoneEnvelope.model_validate(replay.response_body), True)
    zone, profile = _profile_for_admin(session, zone_id=zone_id, for_update=True)
    before = _zone_snapshot(zone, profile)
    uploads = (
        session.execute(
            select(ZoneUpload)
            .where(ZoneUpload.spatial_zone_id == zone.id)
            .order_by(ZoneUpload.revision.desc())
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if payload.visibility == ZoneVisibility.PUBLISHED:
        selected = next((item for item in uploads if item.state == ZoneUploadState.VALIDATED), None)
        if selected is None:
            raise ConflictError(
                "validated_upload_required",
                "A validated archive is required before a zone can be published.",
            )
        for upload in uploads:
            upload.is_active = upload.id == selected.id
        profile.visibility = ZoneVisibility.PUBLISHED
    else:
        for upload in uploads:
            upload.is_active = False
        profile.visibility = ZoneVisibility.HIDDEN
    session.flush()
    response = AdminZoneEnvelope(zone=_zone_response(zone, profile), trace_id=trace_id)
    record_operator_audit(
        session,
        actor=actor,
        action="zone.visibility.changed",
        target_type="zone",
        target_id=zone.zone_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=_zone_snapshot(zone, profile),
        payload={
            "active_upload_id": next(
                (upload.upload_id for upload in uploads if upload.is_active), None
            )
        },
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return ZoneMutationOutcome(response, False)


def create_information(
    session: Session,
    *,
    zone_id: str,
    payload: AdminZoneInformationCreateRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> ZoneMutationOutcome:
    endpoint = f"POST /api/v1/admin/zones/{zone_id}/information"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return ZoneMutationOutcome(
            AdminZoneInformationEnvelope.model_validate(replay.response_body), True
        )
    zone, profile = _profile_for_admin(session, zone_id=zone_id, for_update=True)
    _assert_position_is_inside(profile, payload.position_l93)
    information = ZoneInformation(
        information_id=new_prefixed_id("ZI"),
        spatial_zone_id=zone.id,
        title=payload.title,
        body=payload.body,
        category=payload.category,
        easting_l93=payload.position_l93[0],
        northing_l93=payload.position_l93[1],
        state=ZoneInformationState.DRAFT,
        review_note=None,
        created_by=actor.actor_id,
    )
    session.add(information)
    session.flush()
    response = AdminZoneInformationEnvelope(
        information=_information_response(information), trace_id=trace_id
    )
    record_operator_audit(
        session,
        actor=actor,
        action="zone.information.created",
        target_type="zone_information",
        target_id=information.information_id,
        reason=payload.reason,
        trace_id=trace_id,
        after=_information_snapshot(information),
        payload={"zone_id": zone.zone_id},
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
    return ZoneMutationOutcome(response, False)


def update_information(
    session: Session,
    *,
    zone_id: str,
    information_id: str,
    payload: AdminZoneInformationUpdateRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> ZoneMutationOutcome:
    endpoint = f"PATCH /api/v1/admin/zones/{zone_id}/information/{information_id}"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return ZoneMutationOutcome(
            AdminZoneInformationEnvelope.model_validate(replay.response_body), True
        )
    zone, profile = _profile_for_admin(session, zone_id=zone_id, for_update=True)
    information = session.execute(
        select(ZoneInformation)
        .where(
            ZoneInformation.spatial_zone_id == zone.id,
            ZoneInformation.information_id == information_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if information is None:
        raise NotFoundError("zone_information", information_id)
    _assert_position_is_inside(profile, payload.position_l93)
    before = _information_snapshot(information)
    information.title = payload.title
    information.body = payload.body
    information.category = payload.category
    information.easting_l93, information.northing_l93 = payload.position_l93
    information.state = payload.state
    information.review_note = (
        None if payload.state == ZoneInformationState.PUBLISHED else payload.reason
    )
    information.published_at = utcnow() if payload.state == ZoneInformationState.PUBLISHED else None
    session.flush()
    response = AdminZoneInformationEnvelope(
        information=_information_response(information), trace_id=trace_id
    )
    record_operator_audit(
        session,
        actor=actor,
        action="zone.information.updated",
        target_type="zone_information",
        target_id=information.information_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=_information_snapshot(information),
        payload={"zone_id": zone.zone_id},
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return ZoneMutationOutcome(response, False)


def list_admin_zones(session: Session) -> AdminZoneListResponse:
    rows = session.execute(
        select(SpatialZone, ZoneProfile)
        .join(ZoneProfile, ZoneProfile.spatial_zone_id == SpatialZone.id)
        .order_by(SpatialZone.zone_id)
    ).all()
    return AdminZoneListResponse(zones=[_zone_response(zone, profile) for zone, profile in rows])


def get_admin_zone_detail(session: Session, *, zone_id: str) -> AdminZoneDetailResponse:
    zone, profile = _profile_for_admin(session, zone_id=zone_id)
    uploads = (
        session.execute(
            select(ZoneUpload)
            .where(ZoneUpload.spatial_zone_id == zone.id)
            .order_by(ZoneUpload.revision.desc())
        )
        .scalars()
        .all()
    )
    information = (
        session.execute(
            select(ZoneInformation)
            .where(ZoneInformation.spatial_zone_id == zone.id)
            .order_by(ZoneInformation.updated_at.desc(), ZoneInformation.id.desc())
        )
        .scalars()
        .all()
    )
    return AdminZoneDetailResponse(
        zone=_zone_response(zone, profile),
        uploads=[_upload_response(item) for item in uploads],
        information=[_information_response(item) for item in information],
    )


def list_contributions(
    session: Session, *, state: ZoneContributionState
) -> AdminContributionListResponse:
    contributions = (
        session.execute(
            select(ZoneContribution)
            .where(ZoneContribution.state == state)
            .order_by(ZoneContribution.submitted_at.asc(), ZoneContribution.id.asc())
        )
        .scalars()
        .all()
    )
    return AdminContributionListResponse(
        contributions=[_contribution_response(item) for item in contributions]
    )


def submit_public_contribution(
    session: Session,
    *,
    zone_id: str,
    payload: PublicZoneContributionRequest,
    trace_id: str,
) -> PublicZoneContributionReceipt:
    begin_write_transaction(session)
    zone, profile, _upload = _active_public_zone(session, zone_id=zone_id)
    position = (
        (payload.position_l93.easting, payload.position_l93.northing)
        if payload.position_l93 is not None
        else None
    )
    if position is not None:
        _assert_position_is_inside(profile, position)
    contribution = ZoneContribution(
        contribution_id=new_prefixed_id("ZC"),
        spatial_zone_id=zone.id,
        title=payload.title,
        body=payload.text,
        category=payload.category,
        easting_l93=position[0] if position is not None else None,
        northing_l93=position[1] if position is not None else None,
        state=ZoneContributionState.PENDING,
    )
    session.add(contribution)
    session.flush()
    record_audit(
        session,
        actor_type=ActorType.PUBLIC_SOURCE,
        actor_id="anonymous-zone-contributor",
        action="zone.contribution.submitted",
        target_type="zone_contribution",
        target_id=contribution.contribution_id,
        reason="Public contribution submitted for review.",
        trace_id=trace_id,
        after={
            "zone_id": zone.zone_id,
            "state": contribution.state.value,
            "category": contribution.category,
            "has_position_l93": position is not None,
        },
    )
    session.commit()
    return PublicZoneContributionReceipt(contribution_id=contribution.contribution_id)


def review_contribution(
    session: Session,
    *,
    contribution_id: str,
    payload: AdminContributionReviewRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> ZoneMutationOutcome:
    endpoint = f"POST /api/v1/admin/contributions/{contribution_id}/review"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return ZoneMutationOutcome(
            AdminContributionEnvelope.model_validate(replay.response_body), True
        )
    contribution = session.execute(
        select(ZoneContribution)
        .where(ZoneContribution.contribution_id == contribution_id)
        .with_for_update()
    ).scalar_one_or_none()
    if contribution is None:
        raise NotFoundError("zone_contribution", contribution_id)
    if contribution.state != ZoneContributionState.PENDING:
        raise ConflictError(
            "contribution_already_reviewed", "This contribution is no longer pending review."
        )
    zone, profile = _profile_for_admin(session, zone_id=contribution.zone.zone_id, for_update=True)
    before = {
        "state": contribution.state.value,
        "zone_id": zone.zone_id,
        "has_position_l93": contribution.easting_l93 is not None,
    }
    if payload.decision == "APPROVED":
        if contribution.easting_l93 is None or contribution.northing_l93 is None:
            raise ConflictError(
                "contribution_position_required",
                "A contribution without a Lambert-93 position cannot be published "
                "as zone information.",
            )
        _assert_position_is_inside(profile, (contribution.easting_l93, contribution.northing_l93))
        contribution.state = ZoneContributionState.APPROVED
        session.add(
            ZoneInformation(
                information_id=new_prefixed_id("ZI"),
                spatial_zone_id=zone.id,
                title=contribution.title,
                body=contribution.body,
                category=contribution.category,
                easting_l93=contribution.easting_l93,
                northing_l93=contribution.northing_l93,
                state=ZoneInformationState.PUBLISHED,
                review_note=None,
                published_at=utcnow(),
                created_by=actor.actor_id,
            )
        )
    else:
        contribution.state = ZoneContributionState.REJECTED
    contribution.review_reason = payload.reason
    contribution.reviewed_by = actor.actor_id
    contribution.reviewed_at = utcnow()
    session.flush()
    response = AdminContributionEnvelope(
        contribution=_contribution_response(contribution), trace_id=trace_id
    )
    record_operator_audit(
        session,
        actor=actor,
        action="zone.contribution.reviewed",
        target_type="zone_contribution",
        target_id=contribution.contribution_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after={"state": contribution.state.value, "zone_id": zone.zone_id},
        payload={"decision": payload.decision},
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return ZoneMutationOutcome(response, False)


def get_public_zone(session: Session, *, zone_id: str, settings: Settings) -> PublicZoneResponse:
    zone, profile, upload = _active_public_zone(session, zone_id=zone_id)
    base = f"{settings.api_prefix.rstrip('/')}/zones/{zone.zone_id}"
    return PublicZoneResponse(
        zone_id=zone.zone_id,
        revision=upload.revision,
        label=zone.label or zone.zone_id,
        description=profile.description or None,
        catalog_url=f"{base}/catalog",
        asset_base_url=f"{base}/assets/",
        public_notice=settings.public_notice,
    )


def get_public_information(
    session: Session,
    *,
    zone_id: str,
) -> PublicZoneInformationResponse:
    zone, _profile, upload = _active_public_zone(session, zone_id=zone_id)
    rows = (
        session.execute(
            select(ZoneInformation)
            .where(
                ZoneInformation.spatial_zone_id == zone.id,
                ZoneInformation.state == ZoneInformationState.PUBLISHED,
                ZoneInformation.published_at.is_not(None),
            )
            .order_by(ZoneInformation.published_at.desc(), ZoneInformation.id.desc())
        )
        .scalars()
        .all()
    )
    return PublicZoneInformationResponse(
        zone_id=zone.zone_id,
        revision=upload.revision,
        items=[
            PublicZoneInformationItem(
                information_id=item.information_id,
                title=item.title,
                text=item.body,
                category=item.category,
                published_at=item.published_at,
            )
            for item in rows
            if item.published_at is not None
        ],
        accepts_position_l93=True,
    )


def active_public_upload(session: Session, *, zone_id: str) -> ZoneUpload:
    return _active_public_zone(session, zone_id=zone_id)[2]
