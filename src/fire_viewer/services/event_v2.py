from __future__ import annotations

from datetime import timedelta
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_event_id, new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    ArtifactLineage,
    Episode,
    EventAnalysisJob,
    EventCandidate,
    EvidenceAsset,
    ExternalArtifactRevision,
    FireActivityEvent,
    FireActivityEventEvidence,
    IncidentCandidate,
    IncidentSeries,
    LocalizationAttempt,
    OutboxEvent,
    PublicationSnapshot,
    Viewpoint,
)
from fire_viewer.domain.enums import (
    ActorType,
    EventAnalysisJobState,
    EventCandidateState,
    EvidenceAssetState,
    ExternalArtifactStatus,
    ExternalLineageRelation,
    ExternalSemanticRole,
    FireActivityEventState,
    IncidentCandidateState,
    LocalizationAttemptState,
    MalwareScanState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.event_schemas import (
    EventCandidateCreateRequest,
    EventCandidateListResponse,
    EventCandidateResponse,
    EvidenceUploadAssetResponse,
    EvidenceUploadFinalizedAssetResponse,
    EvidenceUploadFinalizeRequest,
    EvidenceUploadFinalizeResponse,
    EvidenceUploadOpenRequest,
    EvidenceUploadOpenResponse,
    PrivateViewpointSummary,
)
from fire_viewer.domain.geometry_contract import validate_geojson_geometry
from fire_viewer.domain.hashing import json_safe, sha256_hex
from fire_viewer.services.blob_uploads import create_source_blob_upload_grant
from fire_viewer.services.common import record_audit
from fire_viewer.services.evidence_security import (
    antivirus_file_is_clean,
    detected_media_type_from_file,
    file_sha256,
)
from fire_viewer.storage import ObjectStorageError, build_object_store


def _asset_limit(media_type: str, settings: Settings) -> int:
    if media_type.startswith("image/"):
        return settings.event_max_image_bytes
    if media_type.startswith("video/"):
        return settings.event_max_video_bytes
    raise BadRequestError(
        "unsupported_evidence_media_type", "Only image and video evidence is accepted."
    )


def open_evidence_upload(
    session: Session,
    *,
    payload: EvidenceUploadOpenRequest,
    actor: Actor,
    settings: Settings,
    trace_id: str,
) -> EvidenceUploadOpenResponse:
    if len(payload.files) > settings.event_max_assets:
        raise BadRequestError(
            "too_many_evidence_assets", "The contribution contains too many media files."
        )
    total = 0
    for item in payload.files:
        if item.size_bytes > _asset_limit(item.media_type, settings):
            raise BadRequestError(
                "evidence_asset_too_large", f"{item.file_name} exceeds its media limit."
            )
        total += item.size_bytes
    if total > settings.event_max_contribution_bytes:
        raise BadRequestError(
            "evidence_upload_too_large", "The contribution exceeds the total size limit."
        )

    package_id = new_prefixed_id("EU")
    grant = None
    upload_id = uuid4().hex
    store = build_object_store(settings)
    prepared_assets: list[tuple[int, Any, str, str, str]] = []
    for ordinal, item in enumerate(payload.files, start=1):
        asset_id = new_prefixed_id("EA")
        safe_name = PurePosixPath(item.file_name).name
        key = f"source-packages/{upload_id}/{ordinal:02d}-{asset_id}/{safe_name}"
        pathname = store.pathname_for(key)
        prepared_assets.append((ordinal, item, asset_id, safe_name, pathname))
    if settings.object_storage_backend == "vercel_blob":
        grant = create_source_blob_upload_grant(
            package_id=package_id,
            file_count=len(payload.files),
            total_size_bytes=total,
            actor=actor,
            settings=settings,
            upload_id=upload_id,
            purpose="event_evidence",
            allowed_files=tuple(
                {
                    "pathname": pathname,
                    "media_type": item.media_type,
                    "size_bytes": item.size_bytes,
                }
                for _, item, _, _, pathname in prepared_assets
            ),
        )

    response_assets: list[EvidenceUploadAssetResponse] = []
    for ordinal, item, asset_id, safe_name, pathname in prepared_assets:
        row = EvidenceAsset(
            asset_id=asset_id,
            owner_subject=actor.actor_id,
            upload_id=upload_id,
            file_name=safe_name,
            object_uri=store.uri_for_pathname(pathname),
            declared_media_type=item.media_type,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            state=EvidenceAssetState.PENDING_UPLOAD,
            malware_scan_state=MalwareScanState.PENDING,
            metadata_payload={"trace_id": trace_id, "ordinal": ordinal},
            purge_after=utcnow() + timedelta(hours=72),
        )
        session.add(row)
        response_assets.append(
            EvidenceUploadAssetResponse(
                evidence_asset_id=asset_id,
                pathname=pathname,
                upload_state=row.state,
            )
        )
    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action="event.evidence_upload.opened",
        target_type="evidence_upload",
        target_id=upload_id,
        reason="Authenticated contributor opened a private event evidence upload.",
        trace_id=trace_id,
        after={
            "asset_ids": [item.evidence_asset_id for item in response_assets],
            "size_bytes": total,
        },
    )
    session.commit()
    return EvidenceUploadOpenResponse(
        upload_id=upload_id,
        upload_grant=grant.token if grant else None,
        client_payload=package_id,
        expires_at=grant.expires_at if grant else None,
        assets=response_assets,
    )


def finalize_evidence_upload(
    session: Session,
    *,
    upload_id: str,
    payload: EvidenceUploadFinalizeRequest,
    actor: Actor,
    settings: Settings,
    trace_id: str,
) -> EvidenceUploadFinalizeResponse:
    rows = list(
        session.scalars(
            select(EvidenceAsset).where(EvidenceAsset.asset_id.in_(payload.evidence_asset_ids))
        )
    )
    by_id = {row.asset_id: row for row in rows}
    if set(by_id) != set(payload.evidence_asset_ids):
        raise BadRequestError(
            "unknown_evidence_asset", "At least one evidence asset does not exist."
        )
    for row in rows:
        if row.owner_subject != actor.actor_id or row.upload_id != upload_id:
            raise BadRequestError(
                "unknown_evidence_asset", "At least one evidence asset does not exist."
            )
        if row.event_candidate_id is not None:
            raise ConflictError(
                "evidence_asset_already_attached", "An evidence asset is already attached."
            )
        if (
            row.state == EvidenceAssetState.VERIFIED
            and row.malware_scan_state == MalwareScanState.CLEAN
        ):
            continue
        if row.state != EvidenceAssetState.PENDING_UPLOAD:
            raise ConflictError(
                "evidence_asset_not_finalizable",
                f"Evidence asset {row.asset_id} is in state {row.state.value}.",
            )

    store = build_object_store(settings)
    staging_dir = settings.zone_upload_storage_dir / ".event-verification-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    verified: dict[str, tuple[str, str]] = {}
    for row in rows:
        if row.state == EvidenceAssetState.VERIFIED:
            if row.detected_media_type is None or row.sha256 is None:
                raise RuntimeError("Verified evidence asset integrity invariant is broken")
            verified[row.asset_id] = (row.detected_media_type, row.sha256)
            continue
        with TemporaryDirectory(prefix=f"{row.asset_id}-", dir=staging_dir) as temporary_directory:
            materialized = Path(temporary_directory) / "evidence.bin"
            try:
                metadata = store.materialize(row.object_uri, materialized)
            except ObjectStorageError as exc:
                raise ConflictError(
                    "evidence_object_unavailable",
                    "An uploaded evidence object is not available for verification.",
                ) from exc
            if (
                metadata.size_bytes != row.size_bytes
                or materialized.stat().st_size != row.size_bytes
            ):
                row.state = EvidenceAssetState.REJECTED
                row.malware_scan_state = MalwareScanState.FAILED
                session.commit()
                raise ConflictError(
                    "evidence_size_mismatch",
                    "An uploaded evidence object does not match its declared size.",
                )
            detected = detected_media_type_from_file(materialized)
            if detected != row.declared_media_type:
                row.state = EvidenceAssetState.REJECTED
                row.malware_scan_state = MalwareScanState.FAILED
                row.detected_media_type = detected
                session.commit()
                raise ConflictError(
                    "evidence_mime_mismatch",
                    "An uploaded evidence object signature differs from its declared MIME type.",
                )
            digest = file_sha256(materialized)
            if row.sha256 is not None and row.sha256 != digest:
                row.state = EvidenceAssetState.REJECTED
                row.malware_scan_state = MalwareScanState.FAILED
                session.commit()
                raise ConflictError(
                    "evidence_hash_mismatch",
                    "An uploaded evidence object differs from its declared SHA-256.",
                )
            if not antivirus_file_is_clean(materialized, settings):
                row.state = EvidenceAssetState.QUARANTINED
                row.malware_scan_state = MalwareScanState.INFECTED
                session.commit()
                raise ConflictError(
                    "evidence_malware_detected",
                    "An uploaded evidence object was quarantined by the antivirus scanner.",
                )
        verified[row.asset_id] = (detected, digest)

    response_assets: list[EvidenceUploadFinalizedAssetResponse] = []
    for asset_id in payload.evidence_asset_ids:
        row = by_id[asset_id]
        detected, digest = verified[asset_id]
        row.detected_media_type = detected
        row.sha256 = digest
        row.malware_scan_state = MalwareScanState.CLEAN
        row.state = EvidenceAssetState.VERIFIED
        response_assets.append(
            EvidenceUploadFinalizedAssetResponse(
                evidence_asset_id=row.asset_id,
                upload_state=row.state,
                scan_state=row.malware_scan_state,
                detected_media_type=detected,
                sha256=digest,
            )
        )
    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action="event.evidence_upload.finalized",
        target_type="evidence_upload",
        target_id=upload_id,
        reason="Private evidence passed object, signature, hash and antivirus verification.",
        trace_id=trace_id,
        after={"asset_ids": payload.evidence_asset_ids},
    )
    session.commit()
    return EvidenceUploadFinalizeResponse(upload_id=upload_id, assets=response_assets)


def _response(session: Session, row: EventCandidate) -> EventCandidateResponse:
    incident_fire_id = None
    if row.incident_id is not None:
        incident_fire_id = session.scalar(
            select(IncidentSeries.fire_id).where(IncidentSeries.id == row.incident_id)
        )
    private_candidate_id = None
    if row.incident_candidate_id is not None:
        private_candidate_id = session.scalar(
            select(IncidentCandidate.candidate_id).where(
                IncidentCandidate.id == row.incident_candidate_id
            )
        )
    viewpoint = session.get(Viewpoint, row.viewpoint_id)
    if viewpoint is None:
        raise RuntimeError("Event candidate viewpoint invariant is broken")
    asset_ids = list(
        session.scalars(
            select(EvidenceAsset.asset_id)
            .where(EvidenceAsset.event_candidate_id == row.id)
            .order_by(EvidenceAsset.id)
        )
    )
    analysis_job_id = session.scalar(
        select(EventAnalysisJob.job_id).where(EventAnalysisJob.event_candidate_id == row.id)
    )
    if analysis_job_id is None:
        raise RuntimeError("Event candidate analysis job invariant is broken")
    return EventCandidateResponse(
        candidate_id=row.candidate_id,
        analysis_job_id=analysis_job_id,
        tracking_id=row.candidate_id,
        state=row.state,
        incident_id=incident_fire_id,
        incident_candidate_id=private_candidate_id,
        observed_start_at=as_utc(row.observed_start_at),
        observed_end_at=as_utc(row.observed_end_at) if row.observed_end_at else None,
        message=row.message,
        review_message=row.review_message,
        evidence_asset_ids=asset_ids,
        viewpoint=PrivateViewpointSummary(
            horizontal_accuracy_m=viewpoint.horizontal_accuracy_m,
            origin=viewpoint.origin,
            has_orientation=viewpoint.yaw_deg is not None and viewpoint.fov_deg is not None,
        ),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def create_event_candidate(
    session: Session,
    *,
    payload: EventCandidateCreateRequest,
    actor: Actor,
    settings: Settings,
    trace_id: str,
) -> tuple[EventCandidateResponse, bool]:
    request_body = payload.model_dump(mode="json", exclude_none=True)
    request_hash = sha256_hex(request_body)
    key = str(payload.idempotency_key)
    existing = session.execute(
        select(EventCandidate).where(
            EventCandidate.owner_subject == actor.actor_id,
            EventCandidate.idempotency_key == key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ConflictError(
                "event_candidate_idempotency_conflict",
                "The idempotency key was already used with a different contribution.",
            )
        return _response(session, existing), True

    now = utcnow()
    if as_utc(payload.observed_time.start_at) > now + timedelta(
        seconds=settings.max_clock_skew_seconds
    ):
        raise BadRequestError(
            "observation_time_in_future", "The observation time is in the future."
        )

    assets: list[EvidenceAsset] = []
    if payload.evidence_asset_ids:
        assets = list(
            session.scalars(
                select(EvidenceAsset)
                .where(EvidenceAsset.asset_id.in_(payload.evidence_asset_ids))
                .with_for_update()
            )
        )
        by_id = {asset.asset_id: asset for asset in assets}
        if set(by_id) != set(payload.evidence_asset_ids):
            raise BadRequestError(
                "unknown_evidence_asset", "At least one evidence asset does not exist."
            )
        for asset in assets:
            if asset.owner_subject != actor.actor_id:
                # Do not expose the existence of another contributor's private asset.
                raise BadRequestError(
                    "unknown_evidence_asset", "At least one evidence asset does not exist."
                )
            if asset.event_candidate_id is not None:
                raise ConflictError(
                    "evidence_asset_already_attached", "An evidence asset is already attached."
                )
            if (
                asset.state != EvidenceAssetState.VERIFIED
                or asset.malware_scan_state != MalwareScanState.CLEAN
                or asset.detected_media_type
                not in {
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "video/mp4",
                    "video/quicktime",
                    "video/webm",
                }
                or asset.sha256 is None
            ):
                raise ConflictError(
                    "evidence_asset_not_verified",
                    "Every attached evidence asset must pass signature, hash and antivirus checks.",
                )

    incident = None
    if payload.incident_id is not None:
        incident = session.execute(
            select(IncidentSeries).where(IncidentSeries.fire_id == payload.incident_id)
        ).scalar_one_or_none()
        if incident is None:
            raise NotFoundError("incident", payload.incident_id)

    viewpoint = Viewpoint(
        viewpoint_id=new_prefixed_id("VP"),
        owner_subject=actor.actor_id,
        longitude=payload.viewpoint.longitude,
        latitude=payload.viewpoint.latitude,
        horizontal_accuracy_m=payload.viewpoint.horizontal_accuracy_m,
        altitude_m=payload.viewpoint.altitude_m,
        label=payload.viewpoint.label,
        yaw_deg=payload.viewpoint.yaw_deg,
        fov_deg=payload.viewpoint.fov_deg,
        origin=payload.viewpoint.origin,
        public_derivative_allowed=payload.consent.public_derivative,
    )
    session.add(viewpoint)
    session.flush()

    private_incident = None
    if incident is None:
        private_incident = IncidentCandidate(
            candidate_id=new_prefixed_id("IC"),
            state=IncidentCandidateState.PRIVATE_MATCHING,
            origin_kind="CONTRIBUTION",
            created_by_subject=actor.actor_id,
            reference_lon=payload.viewpoint.longitude,
            reference_lat=payload.viewpoint.latitude,
            horizontal_accuracy_m=payload.viewpoint.horizontal_accuracy_m,
            version=1,
        )
        session.add(private_incident)
        session.flush()

    candidate_id = new_prefixed_id("EC")
    analysis_job_id = new_prefixed_id("AJ")
    outbox_event_id = new_event_id()
    state_history = [
        {
            "state": EventCandidateState.RECEIVED.value,
            "at": now.isoformat(),
            "actor": actor.actor_id,
        },
        {
            "state": EventCandidateState.QUEUED.value,
            "at": now.isoformat(),
            "actor": "event-v2-api",
            "reason": "Persisted and queued atomically",
        },
    ]
    row = EventCandidate(
        candidate_id=candidate_id,
        owner_subject=actor.actor_id,
        incident_id=incident.id if incident else None,
        incident_candidate_id=private_incident.id if private_incident else None,
        viewpoint_id=viewpoint.id,
        state=EventCandidateState.QUEUED,
        observed_start_at=as_utc(payload.observed_time.start_at),
        observed_end_at=(
            as_utc(payload.observed_time.end_at) if payload.observed_time.end_at else None
        ),
        message=payload.message,
        consent_analysis=True,
        consent_retention=True,
        consent_public_derivative=payload.consent.public_derivative,
        idempotency_key=key,
        request_hash=request_hash,
        analysis_outbox_event_id=outbox_event_id,
        state_history=state_history,
        version=1,
    )
    session.add(row)
    session.flush()
    session.add(
        EventAnalysisJob(
            job_id=analysis_job_id,
            event_candidate_id=row.id,
            outbox_event_id=outbox_event_id,
            state=EventAnalysisJobState.QUEUED,
            attempts=0,
            result_summary={},
        )
    )
    for asset in assets:
        asset.event_candidate_id = row.id
        asset.purge_after = None
    session.add(
        OutboxEvent(
            event_id=outbox_event_id,
            topic="event_candidate.analyze",
            aggregate_type="event_candidate",
            aggregate_id=candidate_id,
            trace_id=trace_id,
            idempotency_key=key,
            payload={
                "schema_version": "event-2.0",
                "bundle": {
                    "candidate_id": candidate_id,
                    "incident_id": incident.fire_id if incident else None,
                    "incident_candidate_id": (
                        private_incident.candidate_id if private_incident else None
                    ),
                    "viewpoint": {
                        "longitude": viewpoint.longitude,
                        "latitude": viewpoint.latitude,
                        "horizontal_accuracy_m": viewpoint.horizontal_accuracy_m,
                        "altitude_m": viewpoint.altitude_m,
                        "label": viewpoint.label,
                        "yaw_deg": viewpoint.yaw_deg,
                        "fov_deg": viewpoint.fov_deg,
                        "origin": viewpoint.origin.value,
                    },
                    "observed_time": {
                        "start_at": as_utc(payload.observed_time.start_at).isoformat(),
                        "end_at": (
                            as_utc(payload.observed_time.end_at).isoformat()
                            if payload.observed_time.end_at
                            else None
                        ),
                    },
                    "message": payload.message,
                    "evidence_assets": [
                        {
                            "evidence_asset_id": asset.asset_id,
                            "kind": (
                                "image"
                                if asset.declared_media_type.startswith("image/")
                                else "video"
                            ),
                            "object_uri": f"private://{asset.asset_id}",
                            "declared_media_type": asset.declared_media_type,
                            "size_bytes": asset.size_bytes,
                            "sha256": asset.sha256,
                        }
                        for asset in assets
                    ],
                    "consent": payload.consent.model_dump(mode="json"),
                    "provenance": {
                        "received_at": now.isoformat(),
                        "idempotency_key": key,
                        "trace_id": trace_id,
                    },
                    "external_observations": [],
                },
                "perception_anchors": [],
                "spatial_evidence": [],
            },
        )
    )
    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action="event_candidate.created_and_queued",
        target_type="event_candidate",
        target_id=candidate_id,
        reason="Authenticated contribution persisted and queued for private analysis.",
        trace_id=trace_id,
        after={
            "candidate_id": candidate_id,
            "state": EventCandidateState.QUEUED.value,
            "incident_id": incident.fire_id if incident else None,
            "incident_candidate_id": private_incident.candidate_id if private_incident else None,
            "viewpoint_id": viewpoint.viewpoint_id,
            "asset_ids": [asset.asset_id for asset in assets],
        },
    )
    session.commit()
    return _response(session, row), False


def get_own_event_candidate(
    session: Session, *, candidate_id: str, actor: Actor
) -> EventCandidateResponse:
    row = session.execute(
        select(EventCandidate).where(
            EventCandidate.candidate_id == candidate_id,
            EventCandidate.owner_subject == actor.actor_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("event_candidate", candidate_id)
    return _response(session, row)


def list_own_event_candidates(
    session: Session,
    *,
    actor: Actor,
    limit: int = 100,
    offset: int = 0,
) -> EventCandidateListResponse:
    rows = list(
        session.scalars(
            select(EventCandidate)
            .where(EventCandidate.owner_subject == actor.actor_id)
            .order_by(EventCandidate.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    total = int(
        session.scalar(
            select(func.count())
            .select_from(EventCandidate)
            .where(EventCandidate.owner_subject == actor.actor_id)
        )
        or 0
    )
    return EventCandidateListResponse(items=[_response(session, row) for row in rows], total=total)


def _set_candidate_review_state(
    candidate: EventCandidate,
    *,
    state: EventCandidateState,
    actor_id: str,
    reason: str,
) -> None:
    now = utcnow()
    candidate.state = state
    candidate.review_message = reason
    candidate.state_history = [
        *candidate.state_history,
        {
            "state": state.value,
            "at": now.isoformat(),
            "actor": actor_id,
            "reason": reason,
        },
    ]
    candidate.version += 1


def _complete_candidate_review_if_terminal(
    session: Session,
    event: FireActivityEvent,
    *,
    actor_id: str,
    reason: str,
) -> None:
    if event.source_candidate_id is None:
        return
    candidate = session.get(EventCandidate, event.source_candidate_id)
    if candidate is None or candidate.state not in {
        EventCandidateState.NEEDS_REVIEW,
        EventCandidateState.VALIDATED,
    }:
        return
    states = set(
        session.scalars(
            select(FireActivityEvent.state).where(
                FireActivityEvent.source_candidate_id == candidate.id
            )
        )
    )
    if FireActivityEventState.DRAFT in states:
        return
    accepted = bool(
        states.intersection(
            {
                FireActivityEventState.ANALYST_VALIDATED,
                FireActivityEventState.EDITOR_PUBLISHED,
            }
        )
    )
    _set_candidate_review_state(
        candidate,
        state=EventCandidateState.VALIDATED if accepted else EventCandidateState.REJECTED,
        actor_id=actor_id,
        reason=reason,
    )


def transition_fire_activity_event(
    session: Session,
    *,
    event_id: str,
    action: str,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> FireActivityEvent:
    row = session.execute(
        select(FireActivityEvent).where(FireActivityEvent.event_id == event_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("fire_activity_event", event_id)
    before = {"state": row.state.value, "version": row.version}
    now = utcnow()
    if action == "validate" and row.state == FireActivityEventState.DRAFT:
        row.state = FireActivityEventState.ANALYST_VALIDATED
        row.analyst_validated_by = actor.actor_id
        row.analyst_validated_at = now
    elif action == "reject" and row.state in {
        FireActivityEventState.DRAFT,
        FireActivityEventState.ANALYST_VALIDATED,
    }:
        row.state = FireActivityEventState.RETRACTED
    elif action == "publish" and row.state == FireActivityEventState.ANALYST_VALIDATED:
        if row.source_candidate_id is not None:
            source_candidate = session.get(EventCandidate, row.source_candidate_id)
            if source_candidate is None:
                raise RuntimeError("Fire activity event source-candidate invariant is broken")
            if source_candidate.state != EventCandidateState.VALIDATED:
                raise ConflictError(
                    "source_candidate_not_validated",
                    "The source candidate must be fully validated before publication.",
                )
            if not source_candidate.consent_public_derivative:
                raise ConflictError(
                    "public_derivative_consent_required",
                    "This contributor evidence cannot produce a public derivative.",
                )
        row.state = FireActivityEventState.EDITOR_PUBLISHED
        row.editor_published_by = actor.actor_id
        row.editor_published_at = now
        latest_revision = int(
            session.scalar(
                select(func.max(PublicationSnapshot.revision)).where(
                    PublicationSnapshot.incident_id == row.incident_id
                )
            )
            or 0
        )
        public_payload = {
            "event_id": row.event_id,
            "state": row.state.value,
            "phenomenon_kind": row.phenomenon_kind,
            "observed_start_at": as_utc(row.observed_start_at).isoformat(),
            "observed_end_at": as_utc(row.observed_end_at).isoformat()
            if row.observed_end_at
            else None,
            "geometry": _canonical_public_geometry(row.geometry_geojson),
            "uncertainty": _canonical_public_geometry(row.uncertainty_geojson),
            "method": row.method,
            "evidence_count": int(
                session.scalar(
                    select(func.count())
                    .select_from(FireActivityEventEvidence)
                    .where(FireActivityEventEvidence.fire_activity_event_id == row.id)
                )
                or 0
            ),
        }
        session.add(
            PublicationSnapshot(
                snapshot_id=new_prefixed_id("PS"),
                incident_id=row.incident_id,
                revision=latest_revision + 1,
                public_payload=json_safe(public_payload),
                payload_sha256=sha256_hex(public_payload),
                published_by=actor.actor_id,
                published_at=now,
            )
        )
    elif action == "retract" and row.state == FireActivityEventState.EDITOR_PUBLISHED:
        snapshot = next(
            (
                candidate_snapshot
                for candidate_snapshot in session.scalars(
                    select(PublicationSnapshot)
                    .where(
                        PublicationSnapshot.incident_id == row.incident_id,
                        PublicationSnapshot.retracted_at.is_(None),
                    )
                    .order_by(PublicationSnapshot.revision.desc())
                )
                if candidate_snapshot.public_payload.get("event_id") == row.event_id
            ),
            None,
        )
        if snapshot is None:
            raise ConflictError(
                "publication_snapshot_missing",
                "The published event has no active immutable publication snapshot.",
            )
        snapshot.retracted_at = now
        snapshot.retracted_by = actor.actor_id
        snapshot.retraction_reason = reason
        row.state = FireActivityEventState.RETRACTED
    else:
        raise ConflictError(
            "invalid_event_transition", f"Cannot {action} an event in state {row.state.value}."
        )
    row.version += 1
    if action in {"validate", "reject"}:
        session.flush()
        _complete_candidate_review_if_terminal(
            session,
            row,
            actor_id=actor.actor_id,
            reason=reason,
        )
    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action=f"fire_activity_event.{action}",
        target_type="fire_activity_event",
        target_id=row.event_id,
        reason=reason,
        trace_id=trace_id,
        before=before,
        after={"state": row.state.value, "version": row.version},
    )
    session.commit()
    return row


def public_incident_event_timeline(session: Session, fire_id: str) -> dict[str, Any]:
    incident = session.execute(
        select(IncidentSeries).where(IncidentSeries.fire_id == fire_id)
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    snapshots = list(
        session.scalars(
            select(PublicationSnapshot)
            .where(PublicationSnapshot.incident_id == incident.id)
            .order_by(PublicationSnapshot.revision)
        )
    )
    by_event: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        if snapshot.retracted_at is not None:
            continue
        payload = snapshot.public_payload
        if sha256_hex(payload) != snapshot.payload_sha256:
            raise RuntimeError("Publication snapshot integrity verification failed")
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or payload.get("state") != "EDITOR_PUBLISHED":
            continue
        by_event[event_id] = {
            "event_id": event_id,
            "state": "EDITOR_PUBLISHED",
            "phenomenon_kind": payload.get("phenomenon_kind"),
            "observed_start_at": payload.get("observed_start_at"),
            "observed_end_at": payload.get("observed_end_at"),
            "geometry": _canonical_public_geometry(payload.get("geometry")),
            "uncertainty": _canonical_public_geometry(payload.get("uncertainty")),
            "method": payload.get("method"),
            "publication_revision": snapshot.revision,
        }
    events = sorted(
        by_event.values(),
        key=lambda item: (str(item["observed_start_at"]), str(item["event_id"])),
    )
    return {
        "incident_id": incident.fire_id,
        "revision": max((snapshot.revision for snapshot in snapshots), default=0),
        "events": events,
    }


def _canonical_public_geometry(value: object) -> dict[str, Any]:
    geometry = validate_geojson_geometry(value)
    return {
        "type": geometry["type"],
        "coordinates": json_safe(geometry["coordinates"]),
    }


def list_internal_event_candidates(
    session: Session,
    *,
    state: EventCandidateState | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    query = select(EventCandidate)
    count_query = select(func.count()).select_from(EventCandidate)
    if state is not None:
        query = query.where(EventCandidate.state == state)
        count_query = count_query.where(EventCandidate.state == state)
    rows = list(
        session.scalars(
            query.order_by(EventCandidate.updated_at.desc()).offset(offset).limit(limit)
        )
    )
    return {
        "items": [internal_event_candidate_detail(session, row.candidate_id) for row in rows],
        "total": int(session.scalar(count_query) or 0),
        "limit": limit,
        "offset": offset,
    }


def internal_event_candidate_detail(session: Session, candidate_id: str) -> dict[str, Any]:
    row = session.execute(
        select(EventCandidate).where(EventCandidate.candidate_id == candidate_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("event_candidate", candidate_id)
    viewpoint = session.get(Viewpoint, row.viewpoint_id)
    if viewpoint is None:
        raise RuntimeError("Event candidate viewpoint invariant is broken")
    assets = list(
        session.scalars(
            select(EvidenceAsset)
            .where(EvidenceAsset.event_candidate_id == row.id)
            .order_by(EvidenceAsset.id)
        )
    )
    attempts = list(
        session.scalars(
            select(LocalizationAttempt)
            .where(LocalizationAttempt.event_candidate_id == row.id)
            .order_by(LocalizationAttempt.id)
        )
    )
    events = list(
        session.scalars(
            select(FireActivityEvent)
            .where(FireActivityEvent.source_candidate_id == row.id)
            .order_by(FireActivityEvent.id)
        )
    )
    job = session.execute(
        select(EventAnalysisJob).where(EventAnalysisJob.event_candidate_id == row.id)
    ).scalar_one()
    return {
        "candidate_id": row.candidate_id,
        "state": row.state.value,
        "incident_id": session.scalar(
            select(IncidentSeries.fire_id).where(IncidentSeries.id == row.incident_id)
        )
        if row.incident_id is not None
        else None,
        "incident_candidate_id": session.scalar(
            select(IncidentCandidate.candidate_id).where(
                IncidentCandidate.id == row.incident_candidate_id
            )
        )
        if row.incident_candidate_id is not None
        else None,
        "owner_subject": row.owner_subject,
        "observed_start_at": as_utc(row.observed_start_at).isoformat(),
        "observed_end_at": as_utc(row.observed_end_at).isoformat() if row.observed_end_at else None,
        "message": row.message,
        "review_message": row.review_message,
        "review_context": row.review_context,
        "state_history": row.state_history,
        "viewpoint": {
            "longitude": viewpoint.longitude,
            "latitude": viewpoint.latitude,
            "horizontal_accuracy_m": viewpoint.horizontal_accuracy_m,
            "altitude_m": viewpoint.altitude_m,
            "label": viewpoint.label,
            "yaw_deg": viewpoint.yaw_deg,
            "fov_deg": viewpoint.fov_deg,
            "origin": viewpoint.origin.value,
        },
        "evidence_assets": [
            {
                "evidence_asset_id": asset.asset_id,
                "file_name": asset.file_name,
                "media_type": asset.detected_media_type or asset.declared_media_type,
                "size_bytes": asset.size_bytes,
                "state": asset.state.value,
                "scan_state": asset.malware_scan_state.value,
            }
            for asset in assets
        ],
        "localization_attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "state": attempt.state.value,
                "method": attempt.method,
                "model_id": attempt.model_id,
                "model_revision": attempt.model_revision,
                "view_profile": attempt.view_profile,
                "anchor": attempt.anchor_payload,
                "geometry": attempt.geometry_geojson,
                "uncertainty": attempt.uncertainty_geojson,
                "horizontal_uncertainty_m": attempt.horizontal_uncertainty_m,
                "abstention_reason": attempt.abstention_reason,
                "provenance": attempt.provenance,
            }
            for attempt in attempts
        ],
        "fire_activity_events": [
            {
                "event_id": event.event_id,
                "state": event.state.value,
                "phenomenon_kind": event.phenomenon_kind,
                "geometry": event.geometry_geojson,
                "uncertainty": event.uncertainty_geojson,
                "method": event.method,
                "version": event.version,
            }
            for event in events
        ],
        "analysis_job": {
            "job_id": job.job_id,
            "state": job.state.value,
            "result_summary": job.result_summary,
            "last_error_code": job.last_error_code,
        },
        "created_at": as_utc(row.created_at).isoformat(),
        "updated_at": as_utc(row.updated_at).isoformat(),
    }


def review_event_candidate(
    session: Session,
    *,
    candidate_id: str,
    action: str,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> EventCandidate:
    row = session.execute(
        select(EventCandidate).where(EventCandidate.candidate_id == candidate_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("event_candidate", candidate_id)
    if row.state != EventCandidateState.NEEDS_REVIEW and not (
        action == "reject" and row.state == EventCandidateState.VALIDATED
    ):
        raise ConflictError(
            "event_candidate_not_reviewable",
            f"The candidate is in state {row.state.value}.",
        )
    before = {"state": row.state.value, "version": row.version}
    if action == "reject":
        has_published_event = session.scalar(
            select(func.count())
            .select_from(FireActivityEvent)
            .where(
                FireActivityEvent.source_candidate_id == row.id,
                FireActivityEvent.state == FireActivityEventState.EDITOR_PUBLISHED,
            )
        )
        if has_published_event:
            raise ConflictError(
                "event_candidate_has_published_events",
                "Published events must be retracted by an editor before candidate rejection.",
            )
        for event in session.scalars(
            select(FireActivityEvent).where(
                FireActivityEvent.source_candidate_id == row.id,
                FireActivityEvent.state.in_(
                    {
                        FireActivityEventState.DRAFT,
                        FireActivityEventState.ANALYST_VALIDATED,
                    }
                ),
            )
        ):
            event.state = FireActivityEventState.RETRACTED
            event.version += 1
        _set_candidate_review_state(
            row,
            state=EventCandidateState.REJECTED,
            actor_id=actor.actor_id,
            reason=reason,
        )
    else:
        row.review_message = reason
        row.review_context = {
            **row.review_context,
            action: {
                "at": utcnow().isoformat(),
                "actor": actor.actor_id,
                "reason": reason,
            },
        }
        row.state_history = [
            *row.state_history,
            {
                "state": row.state.value,
                "at": utcnow().isoformat(),
                "actor": actor.actor_id,
                "reason": reason,
                "review_action": action,
            },
        ]
        row.version += 1
    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action=f"event_candidate.review.{action}",
        target_type="event_candidate",
        target_id=row.candidate_id,
        reason=reason,
        trace_id=trace_id,
        before=before,
        after={"state": row.state.value, "version": row.version},
    )
    session.commit()
    return row


def attach_event_candidate_to_incident(
    session: Session,
    *,
    candidate_id: str,
    fire_id: str,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> EventCandidate:
    row = session.execute(
        select(EventCandidate).where(EventCandidate.candidate_id == candidate_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("event_candidate", candidate_id)
    if row.state != EventCandidateState.NEEDS_REVIEW or row.incident_candidate_id is None:
        raise ConflictError("event_candidate_not_matchable", "The candidate is not matchable.")
    incident = session.execute(
        select(IncidentSeries).where(IncidentSeries.fire_id == fire_id)
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = session.execute(
        select(Episode)
        .where(Episode.incident_id == incident.id, Episode.is_current.is_(True))
        .order_by(Episode.ordinal.desc())
        .limit(1)
    ).scalar_one_or_none()
    if episode is None:
        raise ConflictError(
            "incident_has_no_current_episode", "The incident has no current episode."
        )
    attempts = list(
        session.scalars(
            select(LocalizationAttempt).where(
                LocalizationAttempt.event_candidate_id == row.id,
                LocalizationAttempt.state == LocalizationAttemptState.PROPOSED,
            )
        )
    )
    attempts = [
        attempt
        for attempt in attempts
        if attempt.method != "cross_view_raycast"
        and not bool(attempt.provenance.get("shadow_only"))
    ]
    if not attempts:
        raise ConflictError(
            "event_candidate_has_no_localization",
            "The candidate has no reviewable localization.",
        )
    private_incident = session.get(IncidentCandidate, row.incident_candidate_id)
    if private_incident is None:
        raise RuntimeError("Event candidate private incident invariant is broken")
    row.incident_id = incident.id
    row.incident_candidate_id = None
    row.review_message = reason
    row.version += 1
    private_incident.state = IncidentCandidateState.MERGED
    private_incident.matched_incident_id = incident.id
    private_incident.resolution_reason = reason
    private_incident.resolved_by = actor.actor_id
    private_incident.resolved_at = utcnow()
    private_incident.version += 1
    evidence_rows = list(
        session.scalars(select(EvidenceAsset).where(EvidenceAsset.event_candidate_id == row.id))
    )
    kind_map = {
        "active_fire_point": "active_fire",
        "visible_fire_front": "visible_front",
        "smoke_origin": "smoke_origin",
    }
    for attempt in attempts:
        phenomenon = str(attempt.anchor_payload.get("phenomenon") or "")
        if (
            phenomenon not in kind_map
            or attempt.geometry_geojson is None
            or attempt.uncertainty_geojson is None
        ):
            continue
        event = FireActivityEvent(
            event_id=new_prefixed_id("FAE"),
            incident_id=incident.id,
            episode_id=episode.id,
            source_candidate_id=row.id,
            localization_attempt_id=attempt.id,
            state=FireActivityEventState.DRAFT,
            phenomenon_kind=kind_map[phenomenon],
            observed_start_at=row.observed_start_at,
            observed_end_at=row.observed_end_at,
            geometry_geojson=attempt.geometry_geojson,
            uncertainty_geojson=attempt.uncertainty_geojson,
            method=attempt.method,
            version=1,
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                FireActivityEventEvidence(
                    fire_activity_event_id=event.id,
                    evidence_asset_id=evidence.id,
                    role="support",
                )
                for evidence in evidence_rows
            ]
        )
    if not session.scalar(
        select(func.count())
        .select_from(FireActivityEvent)
        .where(FireActivityEvent.source_candidate_id == row.id)
    ):
        raise ConflictError(
            "event_candidate_has_no_publishable_proposal",
            "No localized proposal can be attached to the incident.",
        )
    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action="event_candidate.incident_attached",
        target_type="event_candidate",
        target_id=row.candidate_id,
        reason=reason,
        trace_id=trace_id,
        before={"incident_candidate_id": private_incident.candidate_id},
        after={"incident_id": incident.fire_id},
    )
    session.commit()
    return row


def create_private_incident_candidate_from_official_statement(
    session: Session,
    *,
    artifact_revision_id: int,
    actor_id: str,
    longitude: float | None = None,
    latitude: float | None = None,
    accuracy_m: float | None = None,
    trace_id: str | None = None,
) -> IncidentCandidate:
    """The only external-source seed path. Sensor detections/hotspots cannot call it."""

    artifact = session.get(ExternalArtifactRevision, artifact_revision_id)
    if artifact is None:
        raise NotFoundError("external_artifact_revision", str(artifact_revision_id))
    if artifact.semantic_role != ExternalSemanticRole.OFFICIAL_INCIDENT_STATEMENT:
        raise BadRequestError(
            "official_statement_required",
            "Only an official incident statement may seed a private incident candidate.",
        )
    if artifact.status not in {
        ExternalArtifactStatus.VALIDATED,
        ExternalArtifactStatus.CORRECTED,
    }:
        raise ConflictError(
            "official_statement_not_current",
            "Only a validated current official statement may seed an incident candidate.",
        )
    replacement = session.execute(
        select(ArtifactLineage).where(
            ArtifactLineage.parent_revision_id == artifact.id,
            ArtifactLineage.relation.in_(
                {
                    ExternalLineageRelation.SUPERSEDES,
                    ExternalLineageRelation.RETRACTS,
                }
            ),
        )
    ).scalar_one_or_none()
    if replacement is not None:
        raise ConflictError(
            "official_statement_superseded",
            "A superseded or retracted statement cannot seed a new incident candidate.",
        )
    if (longitude is None) != (latitude is None):
        raise BadRequestError(
            "official_statement_coordinates_incomplete",
            "Official statement coordinates require longitude and latitude together.",
        )
    if longitude is not None and not -180 <= longitude <= 180:
        raise BadRequestError("official_statement_longitude_invalid", "Longitude is invalid.")
    if latitude is not None and not -90 <= latitude <= 90:
        raise BadRequestError("official_statement_latitude_invalid", "Latitude is invalid.")
    if accuracy_m is not None and accuracy_m <= 0:
        raise BadRequestError(
            "official_statement_accuracy_invalid", "Horizontal accuracy must be positive."
        )
    if accuracy_m is not None and longitude is None:
        raise BadRequestError(
            "official_statement_accuracy_without_coordinates",
            "Horizontal accuracy requires statement coordinates.",
        )
    existing = session.execute(
        select(IncidentCandidate).where(
            IncidentCandidate.source_statement_revision_id == artifact.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = IncidentCandidate(
        candidate_id=new_prefixed_id("IC"),
        state=IncidentCandidateState.PRIVATE_MATCHING,
        origin_kind="OFFICIAL_STATEMENT",
        created_by_subject=actor_id,
        source_statement_revision_id=artifact_revision_id,
        reference_lon=longitude,
        reference_lat=latitude,
        horizontal_accuracy_m=accuracy_m,
        version=1,
    )
    session.add(row)
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="incident_candidate.created_from_official_statement",
        target_type="incident_candidate",
        target_id=row.candidate_id,
        reason="Validated official statement created a private matching dossier only.",
        trace_id=trace_id or new_prefixed_id("TRC"),
        after={
            "state": row.state.value,
            "origin_kind": row.origin_kind,
            "source_statement_revision_id": artifact.artifact_revision_id,
        },
    )
    return row
