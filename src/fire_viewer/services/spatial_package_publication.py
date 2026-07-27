"""Validated admin lifecycle for immutable spatial package registry entries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import (
    ManifestRevision,
    SpatialPackage,
    SpatialZone,
    SpatialZoneRevision,
    ZonePublication,
    ZonePublicationEvent,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import (
    SpatialPackageFileKind,
    SpatialPackageState,
    ZonePublicationState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminPublicationActionRequest,
    AdminPublicationListResponse,
    AdminPublicationSummary,
    AdminSpatialPackageActionRequest,
    AdminSpatialPackagePublicationEnvelope,
    AdminSpatialPackagePublicationRequest,
    AdminSpatialPackagePublicationResponse,
)
from fire_viewer.domain.zone_publication import assert_zone_publication_transition
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.idempotency import find_replay, store_response


@dataclass(frozen=True, slots=True)
class SpatialPackageMutationOutcome:
    response: AdminSpatialPackagePublicationEnvelope
    replayed: bool


def list_publications(
    session: Session, *, state: ZonePublicationState | None = None
) -> AdminPublicationListResponse:
    statement = (
        select(ZonePublication)
        .options(
            selectinload(ZonePublication.zone), selectinload(ZonePublication.spatial_zone_revision)
        )
        .order_by(ZonePublication.updated_at.desc(), ZonePublication.id.desc())
    )
    if state is not None:
        statement = statement.where(ZonePublication.state == state)
    rows = session.execute(statement).scalars().all()
    result: list[AdminPublicationSummary] = []
    for row in rows:
        fire_ids = list(
            session.execute(
                select(ManifestRevision.incident_id).where(
                    ManifestRevision.spatial_zone_revision_id == row.spatial_zone_revision_id
                )
            ).scalars()
        )
        # Resolve the stable public id only through the persisted manifest relation.
        from fire_viewer.db.models import IncidentSeries

        linked = (
            list(
                session.execute(
                    select(IncidentSeries.fire_id).where(IncidentSeries.id.in_(fire_ids))
                ).scalars()
            )
            if fire_ids
            else []
        )
        result.append(
            AdminPublicationSummary(
                publication_id=row.publication_id,
                zone_id=row.zone.zone_id,
                revision=row.spatial_zone_revision.revision,
                package_id=row.package.package_id,
                state=row.state,
                is_active=row.is_active,
                updated_at=row.updated_at,
                linked_fire_ids=sorted(set(linked)),
            )
        )
    return AdminPublicationListResponse(publications=result)


def change_publication_state(
    session: Session,
    *,
    publication_id: str,
    target: ZonePublicationState,
    payload: AdminPublicationActionRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> SpatialPackageMutationOutcome:
    endpoint = f"POST /api/v1/admin/publications/{publication_id}/{target.value.casefold()}"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return SpatialPackageMutationOutcome(
            AdminSpatialPackagePublicationEnvelope.model_validate(replay.response_body), True
        )
    publication = session.execute(
        select(ZonePublication)
        .where(ZonePublication.publication_id == publication_id)
        .options(
            selectinload(ZonePublication.package),
            selectinload(ZonePublication.spatial_zone_revision).selectinload(
                SpatialZoneRevision.zone
            ),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if publication is None:
        raise NotFoundError("zone_publication", publication_id)
    if payload.confirm_publication_id != publication_id:
        raise ConflictError(
            "publication_confirmation_mismatch",
            "The publication confirmation does not match the target.",
        )
    before = _publication_snapshot(publication)
    _transition_publication(
        session,
        publication=publication,
        target=target,
        action=target.value.casefold(),
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    if (
        target == ZonePublicationState.WITHDRAWN
        and publication.package.state == SpatialPackageState.PUBLISHED
    ):
        publication.package.state = SpatialPackageState.WITHDRAWN
    if target == ZonePublicationState.PUBLISHED:
        other = session.execute(
            select(ZonePublication).where(
                ZonePublication.spatial_zone_id == publication.spatial_zone_id,
                ZonePublication.is_active.is_(True),
                ZonePublication.id != publication.id,
            )
        ).scalar_one_or_none()
        if other is not None:
            raise ConflictError(
                "active_publication_exists", "Another publication is already active for this zone."
            )
        publication.package.state = SpatialPackageState.PUBLISHED
    response = _response(
        zone_id=publication.spatial_zone_revision.zone.zone_id,
        revision=publication.spatial_zone_revision.revision,
        package=publication.package,
        publication=publication,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action=f"zone_publication.{target.value.casefold()}",
        target_type="zone_publication",
        target_id=publication_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=_publication_snapshot(publication),
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
    return SpatialPackageMutationOutcome(response, False)


def _request_hash(actor: Actor, value: dict[str, Any]) -> str:
    return sha256_hex({"actor_id": actor.actor_id, **value})


def _get_revision(session: Session, *, zone_id: str, revision: int) -> SpatialZoneRevision:
    row = session.execute(
        select(SpatialZoneRevision)
        .join(SpatialZone)
        .where(SpatialZone.zone_id == zone_id, SpatialZoneRevision.revision == revision)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("spatial_zone_revision", f"{zone_id}/revisions/{revision}")
    return row


def _get_package(session: Session, *, package_id: str) -> SpatialPackage:
    row = session.execute(
        select(SpatialPackage)
        .where(SpatialPackage.package_id == package_id)
        .options(selectinload(SpatialPackage.files))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("spatial_package", package_id)
    return row


def _get_publication(
    session: Session,
    *,
    revision: SpatialZoneRevision,
    package: SpatialPackage,
) -> ZonePublication:
    row = session.execute(
        select(ZonePublication)
        .where(
            ZonePublication.spatial_zone_revision_id == revision.id,
            ZonePublication.spatial_package_id == package.id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ConflictError(
            "zone_publication_missing",
            "The verified package has no registered publication lifecycle.",
        )
    return row


def _response(
    *,
    zone_id: str,
    revision: int,
    package: SpatialPackage,
    publication: ZonePublication,
    trace_id: str,
) -> AdminSpatialPackagePublicationEnvelope:
    return AdminSpatialPackagePublicationEnvelope(
        publication=AdminSpatialPackagePublicationResponse(
            zone_id=zone_id,
            revision=revision,
            package_id=package.package_id,
            package_state=package.state,
            publication_id=publication.publication_id,
            publication_state=publication.state,
            is_active=publication.is_active,
        ),
        trace_id=trace_id,
    )


def _package_snapshot(package: SpatialPackage) -> dict[str, Any]:
    return {
        "package_id": package.package_id,
        "state": package.state.value,
        "spatial_zone_revision_id": package.spatial_zone_revision_id,
        "verified_at": package.verified_at,
        "file_kinds": sorted(file.kind.value for file in package.files),
    }


def _publication_snapshot(publication: ZonePublication) -> dict[str, Any]:
    return {
        "publication_id": publication.publication_id,
        "state": publication.state.value,
        "is_active": publication.is_active,
        "spatial_package_id": publication.spatial_package_id,
        "spatial_zone_revision_id": publication.spatial_zone_revision_id,
    }


def _transition_publication(
    session: Session,
    *,
    publication: ZonePublication,
    target: ZonePublicationState,
    action: str,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> None:
    previous = publication.state
    assert_zone_publication_transition(previous, target)
    session.add(
        ZonePublicationEvent(
            event_id=new_prefixed_id("ZPE"),
            zone_publication_id=publication.id,
            from_state=previous,
            to_state=target,
            action=action,
            reason=reason,
            actor_id=actor.actor_id,
            event_metadata={"trace_id": trace_id},
        )
    )
    session.flush()
    publication.state = target
    publication.is_active = target == ZonePublicationState.PUBLISHED
    publication.reason = reason
    publication.actor_id = actor.actor_id
    session.flush()


def _create_verified_publication(
    session: Session,
    *,
    revision: SpatialZoneRevision,
    package: SpatialPackage,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> ZonePublication:
    publication = ZonePublication(
        publication_id=new_prefixed_id("ZP"),
        spatial_zone_id=revision.spatial_zone_id,
        spatial_zone_revision_id=revision.id,
        spatial_package_id=package.id,
        state=ZonePublicationState.DRAFT,
        is_active=False,
        reason=reason,
        actor_id=actor.actor_id,
    )
    session.add(publication)
    session.flush()
    session.add(
        ZonePublicationEvent(
            event_id=new_prefixed_id("ZPE"),
            zone_publication_id=publication.id,
            from_state=None,
            to_state=ZonePublicationState.DRAFT,
            action="created",
            reason=reason,
            actor_id=actor.actor_id,
            event_metadata={"trace_id": trace_id},
        )
    )
    session.flush()
    _transition_publication(
        session,
        publication=publication,
        target=ZonePublicationState.VERIFIED,
        action="validated",
        reason=reason,
        actor=actor,
        trace_id=trace_id,
    )
    return publication


def _assert_required_preview_files(package: SpatialPackage) -> None:
    kinds = {file.kind for file in package.files}
    tiled_kinds = {
        SpatialPackageFileKind.FWTILE,
        SpatialPackageFileKind.FWTERRAIN,
    }
    required = (
        {*tiled_kinds, SpatialPackageFileKind.JPEG}
        if kinds.intersection(tiled_kinds)
        else {SpatialPackageFileKind.PNG, SpatialPackageFileKind.GLB}
    )
    missing = sorted(kind.value for kind in required.difference(kinds))
    if missing:
        raise ConflictError(
            "spatial_package_missing_preview_assets",
            f"The registered package is missing required preview assets: {', '.join(missing)}.",
        )


def make_spatial_package_previewable(
    session: Session,
    *,
    revision: SpatialZoneRevision,
    package: SpatialPackage,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> ZonePublication:
    """Validate and expose a freshly imported package inside the caller transaction."""

    if package.state != SpatialPackageState.DRAFT or package.spatial_zone_revision_id is not None:
        raise ConflictError(
            "spatial_package_not_draft",
            "Only an unattached draft package can enter registry validation.",
        )
    _assert_required_preview_files(package)
    package.verification_report = {
        "status": "passed",
        "scope": "registered-metadata-and-immutable-inventory",
        "checks": ["manifest-metadata", "immutable-file-inventory", "preview-assets"],
        "scene_kind": (
            "remote_tiles"
            if {
                SpatialPackageFileKind.FWTILE,
                SpatialPackageFileKind.FWTERRAIN,
            }.issubset({file.kind for file in package.files})
            else "single_asset"
        ),
        "package_id": package.package_id,
    }
    package.verified_at = utcnow()
    package.spatial_zone_revision_id = revision.id
    package.state = SpatialPackageState.VERIFIED
    session.flush()
    publication = _create_verified_publication(
        session,
        revision=revision,
        package=package,
        reason=reason,
        actor=actor,
        trace_id=trace_id,
    )
    package.state = SpatialPackageState.PREVIEWABLE
    _transition_publication(
        session,
        publication=publication,
        target=ZonePublicationState.PREVIEWABLE,
        action="preview_enabled",
        reason=reason,
        actor=actor,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_package.imported_for_incident",
        target_type="spatial_package",
        target_id=package.package_id,
        reason=reason,
        trace_id=trace_id,
        after=_package_snapshot(package),
        payload={
            "zone_revision_id": revision.id,
            "publication_id": publication.publication_id,
        },
    )
    return publication


def validate_spatial_package(
    session: Session,
    *,
    zone_id: str,
    revision: int,
    payload: AdminSpatialPackageActionRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> SpatialPackageMutationOutcome:
    endpoint = f"POST /api/v1/admin/zones/{zone_id}/revisions/{revision}/validations"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return SpatialPackageMutationOutcome(
            AdminSpatialPackagePublicationEnvelope.model_validate(replay.response_body), True
        )

    revision_row = _get_revision(session, zone_id=zone_id, revision=revision)
    package = _get_package(session, package_id=payload.package_id)
    if package.state != SpatialPackageState.DRAFT or package.spatial_zone_revision_id is not None:
        raise ConflictError(
            "spatial_package_not_draft",
            "Only an unattached draft package can enter registry validation.",
        )
    _assert_required_preview_files(package)
    before = _package_snapshot(package)
    package.verification_report = {
        "status": "passed",
        "scope": "registered-metadata-and-immutable-inventory",
        "checks": ["manifest-metadata", "immutable-file-inventory", "preview-assets"],
        "scene_kind": (
            "remote_tiles"
            if {
                SpatialPackageFileKind.FWTILE,
                SpatialPackageFileKind.FWTERRAIN,
            }.issubset({file.kind for file in package.files})
            else "single_asset"
        ),
        "package_id": package.package_id,
    }
    package.verified_at = utcnow()
    package.spatial_zone_revision_id = revision_row.id
    package.state = SpatialPackageState.VERIFIED
    session.flush()
    publication = _create_verified_publication(
        session,
        revision=revision_row,
        package=package,
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    response = _response(
        zone_id=zone_id,
        revision=revision,
        package=package,
        publication=publication,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_package.validated",
        target_type="spatial_package",
        target_id=package.package_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=_package_snapshot(package),
        payload={
            "zone_id": zone_id,
            "revision": revision,
            "publication_id": publication.publication_id,
        },
    )
    record_operator_audit(
        session,
        actor=actor,
        action="zone_publication.verified",
        target_type="zone_publication",
        target_id=publication.publication_id,
        reason=payload.reason,
        trace_id=trace_id,
        after=_publication_snapshot(publication),
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
    return SpatialPackageMutationOutcome(response, False)


def enable_spatial_package_preview(
    session: Session,
    *,
    zone_id: str,
    revision: int,
    payload: AdminSpatialPackageActionRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> SpatialPackageMutationOutcome:
    endpoint = f"POST /api/v1/admin/zones/{zone_id}/revisions/{revision}/preview"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return SpatialPackageMutationOutcome(
            AdminSpatialPackagePublicationEnvelope.model_validate(replay.response_body), True
        )

    revision_row = _get_revision(session, zone_id=zone_id, revision=revision)
    package = _get_package(session, package_id=payload.package_id)
    publication = _get_publication(session, revision=revision_row, package=package)
    if (
        package.state != SpatialPackageState.VERIFIED
        or publication.state != ZonePublicationState.VERIFIED
    ):
        raise ConflictError(
            "spatial_package_not_verified",
            "A package and its publication must both be verified before private preview.",
        )
    _assert_required_preview_files(package)
    package_before = _package_snapshot(package)
    publication_before = _publication_snapshot(publication)
    package.state = SpatialPackageState.PREVIEWABLE
    session.flush()
    _transition_publication(
        session,
        publication=publication,
        target=ZonePublicationState.PREVIEWABLE,
        action="preview_enabled",
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    response = _response(
        zone_id=zone_id,
        revision=revision,
        package=package,
        publication=publication,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_package.preview_enabled",
        target_type="spatial_package",
        target_id=package.package_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=package_before,
        after=_package_snapshot(package),
        payload={
            "zone_id": zone_id,
            "revision": revision,
            "publication_id": publication.publication_id,
        },
    )
    record_operator_audit(
        session,
        actor=actor,
        action="zone_publication.preview_enabled",
        target_type="zone_publication",
        target_id=publication.publication_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=publication_before,
        after=_publication_snapshot(publication),
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
    return SpatialPackageMutationOutcome(response, False)


def publish_spatial_package(
    session: Session,
    *,
    payload: AdminSpatialPackagePublicationRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> SpatialPackageMutationOutcome:
    endpoint = "POST /api/v1/admin/publications"
    request_hash = _request_hash(actor, {"payload": payload.model_dump(mode="json")})
    begin_write_transaction(session)
    replay = find_replay(
        session, endpoint=endpoint, idempotency_key=idempotency_key, request_hash=request_hash
    )
    if replay:
        session.rollback()
        return SpatialPackageMutationOutcome(
            AdminSpatialPackagePublicationEnvelope.model_validate(replay.response_body), True
        )

    revision_row = _get_revision(session, zone_id=payload.zone_id, revision=payload.revision)
    package = _get_package(session, package_id=payload.package_id)
    publication = _get_publication(session, revision=revision_row, package=package)
    if (
        package.state != SpatialPackageState.PREVIEWABLE
        or publication.state != ZonePublicationState.PREVIEWABLE
    ):
        raise ConflictError(
            "spatial_package_not_previewable",
            "A package and its publication must both be previewable before publication.",
        )
    linked_manifest = session.execute(
        select(ManifestRevision.id).where(
            ManifestRevision.spatial_package_id == package.id,
            ManifestRevision.spatial_zone_revision_id == revision_row.id,
            ManifestRevision.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if linked_manifest is None:
        raise ConflictError(
            "spatial_package_not_linked_to_incident",
            "Attach the package to an incident before publishing it.",
        )

    active_publication = session.execute(
        select(ZonePublication)
        .where(
            ZonePublication.spatial_zone_id == revision_row.spatial_zone_id,
            ZonePublication.is_active.is_(True),
        )
        .options(selectinload(ZonePublication.package))
        .with_for_update()
    ).scalar_one_or_none()
    if active_publication is not None:
        active_before = _publication_snapshot(active_publication)
        _transition_publication(
            session,
            publication=active_publication,
            target=ZonePublicationState.WITHDRAWN,
            action="withdrawn_for_replacement",
            reason=payload.reason,
            actor=actor,
            trace_id=trace_id,
        )
        if active_publication.package.state == SpatialPackageState.PUBLISHED:
            active_publication.package.state = SpatialPackageState.WITHDRAWN
            session.flush()
        record_operator_audit(
            session,
            actor=actor,
            action="zone_publication.withdrawn",
            target_type="zone_publication",
            target_id=active_publication.publication_id,
            reason=payload.reason,
            trace_id=trace_id,
            before=active_before,
            after=_publication_snapshot(active_publication),
            payload={"replaced_by": publication.publication_id},
        )

    package_before = _package_snapshot(package)
    publication_before = _publication_snapshot(publication)
    package.state = SpatialPackageState.PUBLISHED
    session.flush()
    _transition_publication(
        session,
        publication=publication,
        target=ZonePublicationState.PUBLISHED,
        action="published",
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    response = _response(
        zone_id=payload.zone_id,
        revision=payload.revision,
        package=package,
        publication=publication,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_package.published",
        target_type="spatial_package",
        target_id=package.package_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=package_before,
        after=_package_snapshot(package),
        payload={
            "zone_id": payload.zone_id,
            "revision": payload.revision,
            "publication_id": publication.publication_id,
        },
    )
    record_operator_audit(
        session,
        actor=actor,
        action="zone_publication.published",
        target_type="zone_publication",
        target_id=publication.publication_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=publication_before,
        after=_publication_snapshot(publication),
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
    return SpatialPackageMutationOutcome(response, False)
