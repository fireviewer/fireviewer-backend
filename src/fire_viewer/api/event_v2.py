from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi import (
    Path as ApiPath,
)
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from fire_viewer.api.dependencies import ActorDep, SessionDep, SettingsDep, TraceIdDep
from fire_viewer.core.security import (
    require_current_role,
    require_recent_active_session,
    require_role,
    require_verified_contributor,
)
from fire_viewer.db.models import EvidenceAsset
from fire_viewer.domain.enums import (
    EventCandidateState,
    EvidenceAssetState,
    MalwareScanState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.event_schemas import (
    EventCandidateAttachIncidentRequest,
    EventCandidateCreateRequest,
    EventCandidateListResponse,
    EventCandidateMutationResponse,
    EventCandidateResponse,
    EventCandidateReviewRequest,
    EventTransitionRequest,
    EvidenceUploadFinalizeRequest,
    EvidenceUploadFinalizeResponse,
    EvidenceUploadOpenRequest,
    EvidenceUploadOpenResponse,
    FireActivityEventMutationResponse,
    InternalEventCandidateListResponse,
    InternalEventCandidateResponse,
    PublicIncidentEventTimelineResponse,
)
from fire_viewer.domain.schemas import AdminBlobUploadTokenRequest, AdminBlobUploadTokenResponse
from fire_viewer.services.blob_uploads import issue_blob_client_token
from fire_viewer.services.common import record_audit
from fire_viewer.services.event_evidence_access import (
    materialize_private_event_evidence,
    materialize_verified_event_evidence,
)
from fire_viewer.services.event_v2 import (
    attach_event_candidate_to_incident,
    create_event_candidate,
    finalize_evidence_upload,
    get_own_event_candidate,
    internal_event_candidate_detail,
    list_internal_event_candidates,
    list_own_event_candidates,
    open_evidence_upload,
    public_incident_event_timeline,
    review_event_candidate,
    transition_fire_activity_event,
)
from fire_viewer.storage import ObjectStorageError, build_object_store

router = APIRouter(prefix="/api/v2", tags=["event-documentation-v2"])


def _require_enabled(settings: SettingsDep) -> None:
    if not settings.event_v2_enabled:
        raise HTTPException(status_code=404, detail="Event documentation v2 is disabled.")


@router.get(
    "/private-event-evidence/{evidence_asset_id}",
    include_in_schema=False,
    response_class=Response,
)
def private_event_evidence(
    evidence_asset_id: Annotated[str, ApiPath(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")],
    token: Annotated[str, Query(min_length=64, max_length=4_096)],
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    _require_enabled(settings)
    payload = materialize_private_event_evidence(
        session,
        asset_id=evidence_asset_id,
        token=token,
        settings=settings,
    )
    return FileResponse(
        payload.local_path,
        media_type=payload.content_type,
        filename=payload.filename,
        content_disposition_type="inline",
        background=BackgroundTask(payload.local_path.unlink, missing_ok=True),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/evidence/uploads",
    response_model=EvidenceUploadOpenResponse | AdminBlobUploadTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def evidence_upload(
    payload: dict[str, Any],
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    upload_grant: Annotated[
        str | None,
        Header(alias="X-Evidence-Upload-Grant", min_length=64, max_length=4096),
    ] = None,
) -> EvidenceUploadOpenResponse | AdminBlobUploadTokenResponse:
    """Open an upload or answer the private Vercel Blob client-token callback."""

    _require_enabled(settings)
    require_verified_contributor(actor, settings)
    if payload.get("type") == "blob.generate-client-token":
        if upload_grant is None:
            raise HTTPException(status_code=403, detail="Evidence upload grant is required.")
        try:
            callback = TypeAdapter(AdminBlobUploadTokenRequest).validate_python(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        response.status_code = status.HTTP_200_OK
        return AdminBlobUploadTokenResponse(
            clientToken=issue_blob_client_token(
                pathname=callback.payload.pathname,
                client_payload=callback.payload.clientPayload,
                upload_grant=upload_grant,
                settings=settings,
            )
        )
    try:
        request = TypeAdapter(EvidenceUploadOpenRequest).validate_python(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    response.headers["Cache-Control"] = "no-store"
    return open_evidence_upload(
        session,
        payload=request,
        actor=actor,
        settings=settings,
        trace_id=trace_id,
    )


@router.post(
    "/evidence/uploads/{upload_id}/finalize",
    response_model=EvidenceUploadFinalizeResponse,
)
def finalize_upload(
    upload_id: str,
    payload: EvidenceUploadFinalizeRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> EvidenceUploadFinalizeResponse:
    _require_enabled(settings)
    require_verified_contributor(actor, settings)
    response.headers["Cache-Control"] = "no-store"
    return finalize_evidence_upload(
        session,
        upload_id=upload_id,
        payload=payload,
        actor=actor,
        settings=settings,
        trace_id=trace_id,
    )


@router.put("/evidence/uploads/{upload_id}/assets/{evidence_asset_id}", status_code=204)
async def upload_local_evidence_asset(
    upload_id: str,
    evidence_asset_id: str,
    request: Request,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> Response:
    """Stream one immutable object into the local development evidence store."""

    _require_enabled(settings)
    require_verified_contributor(actor, settings)
    if settings.object_storage_backend != "local" or settings.environment not in {
        "development",
        "test",
    }:
        raise NotFoundError("evidence_upload", evidence_asset_id)
    row = session.execute(
        select(EvidenceAsset).where(EvidenceAsset.asset_id == evidence_asset_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.owner_subject != actor.actor_id or row.upload_id != upload_id:
        raise NotFoundError("evidence_asset", evidence_asset_id)
    if row.state != EvidenceAssetState.PENDING_UPLOAD or row.event_candidate_id is not None:
        raise ConflictError(
            "evidence_asset_not_uploadable", "The evidence asset is not uploadable."
        )

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != row.declared_media_type:
        raise HTTPException(status_code=415, detail="The uploaded media type does not match.")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.") from exc
        if declared_length != row.size_bytes:
            raise ConflictError("evidence_size_mismatch", "The uploaded size does not match.")

    staging_dir = settings.zone_upload_storage_dir / ".event-upload-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    written = 0
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f"{evidence_asset_id}-",
            suffix=".partial",
            dir=staging_dir,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            async for chunk in request.stream():
                written += len(chunk)
                if written > row.size_bytes:
                    raise ConflictError(
                        "evidence_size_mismatch", "The uploaded size does not match."
                    )
                handle.write(chunk)
        if written != row.size_bytes:
            raise ConflictError("evidence_size_mismatch", "The uploaded size does not match.")
        build_object_store(settings).put_file(temporary_path, row.object_uri)
        temporary_path = None
    except ObjectStorageError as exc:
        raise ConflictError(
            "evidence_object_unavailable", "The evidence object could not be stored."
        ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action="event.evidence_upload.object_stored",
        target_type="evidence_asset",
        target_id=row.asset_id,
        reason="Authenticated contributor streamed a local development evidence object.",
        trace_id=trace_id,
        after={"size_bytes": written, "media_type": row.declared_media_type},
    )
    session.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post(
    "/event-candidates",
    response_model=EventCandidateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_event_candidate(
    payload: EventCandidateCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> EventCandidateResponse:
    _require_enabled(settings)
    require_verified_contributor(actor, settings)
    result, replayed = create_event_candidate(
        session,
        payload=payload,
        actor=actor,
        settings=settings,
        trace_id=trace_id,
    )
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotent-Replay"] = "true"
    return result


@router.get("/me/event-candidates", response_model=EventCandidateListResponse)
def my_event_candidates(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventCandidateListResponse:
    _require_enabled(settings)
    require_verified_contributor(actor, settings)
    response.headers["Cache-Control"] = "no-store"
    return list_own_event_candidates(session, actor=actor, limit=limit, offset=offset)


@router.get("/me/event-candidates/{candidate_id}", response_model=EventCandidateResponse)
def my_event_candidate(
    candidate_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> EventCandidateResponse:
    _require_enabled(settings)
    require_verified_contributor(actor, settings)
    response.headers["Cache-Control"] = "no-store"
    return get_own_event_candidate(session, candidate_id=candidate_id, actor=actor)


@router.get(
    "/incidents/{incident_id}/timeline",
    response_model=PublicIncidentEventTimelineResponse,
)
def public_incident_timeline(
    incident_id: Annotated[str, ApiPath(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> PublicIncidentEventTimelineResponse:
    _require_enabled(settings)
    if not settings.v2_publication_enabled:
        raise HTTPException(status_code=404, detail="Event publication v2 is disabled.")
    response.headers["Cache-Control"] = "public, max-age=30, must-revalidate"
    return PublicIncidentEventTimelineResponse.model_validate(
        public_incident_event_timeline(session, incident_id)
    )


@router.get(
    "/internal/event-candidates",
    response_model=InternalEventCandidateListResponse,
)
def internal_event_candidates(
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    state: Annotated[EventCandidateState | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InternalEventCandidateListResponse:
    _require_enabled(settings)
    require_current_role(
        actor,
        settings,
        "analyst",
        "editor",
        "security_operator",
        "administrator",
    )
    return InternalEventCandidateListResponse.model_validate(
        list_internal_event_candidates(
            session,
            state=state,
            limit=limit,
            offset=offset,
        )
    )


@router.get(
    "/internal/event-candidates/{candidate_id}",
    response_model=InternalEventCandidateResponse,
)
def internal_event_candidate(
    candidate_id: str,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> InternalEventCandidateResponse:
    _require_enabled(settings)
    require_current_role(
        actor,
        settings,
        "analyst",
        "editor",
        "security_operator",
        "administrator",
    )
    return InternalEventCandidateResponse.model_validate(
        internal_event_candidate_detail(session, candidate_id)
    )


@router.get(
    "/internal/evidence-assets/{evidence_asset_id}/content",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def internal_evidence_asset_content(
    evidence_asset_id: str,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    _require_enabled(settings)
    require_current_role(
        actor,
        settings,
        "analyst",
        "editor",
        "security_operator",
        "administrator",
    )
    row = session.execute(
        select(EvidenceAsset).where(EvidenceAsset.asset_id == evidence_asset_id)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("evidence_asset", evidence_asset_id)
    if (
        row.state != EvidenceAssetState.VERIFIED
        or row.malware_scan_state != MalwareScanState.CLEAN
        or row.detected_media_type is None
    ):
        raise ConflictError(
            "evidence_asset_not_reviewable", "The evidence asset is not safe for review."
        )
    payload = materialize_verified_event_evidence(row, settings=settings)
    return FileResponse(
        payload.local_path,
        media_type=payload.content_type,
        filename=payload.filename,
        content_disposition_type="inline",
        background=BackgroundTask(payload.local_path.unlink, missing_ok=True),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/internal/event-candidates/{candidate_id}/review",
    response_model=EventCandidateMutationResponse,
)
def review_internal_event_candidate(
    candidate_id: str,
    payload: EventCandidateReviewRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> EventCandidateMutationResponse:
    _require_enabled(settings)
    require_current_role(actor, settings, "analyst", "administrator")
    row = review_event_candidate(
        session,
        candidate_id=candidate_id,
        action=payload.action,
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    return EventCandidateMutationResponse(
        candidate_id=row.candidate_id,
        state=row.state,
        version=row.version,
    )


@router.post(
    "/internal/event-candidates/{candidate_id}/attach-incident",
    response_model=EventCandidateMutationResponse,
)
def attach_internal_event_candidate(
    candidate_id: str,
    payload: EventCandidateAttachIncidentRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> EventCandidateMutationResponse:
    _require_enabled(settings)
    require_current_role(actor, settings, "analyst", "administrator")
    row = attach_event_candidate_to_incident(
        session,
        candidate_id=candidate_id,
        fire_id=payload.incident_id,
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    return EventCandidateMutationResponse(
        candidate_id=row.candidate_id,
        state=row.state,
        version=row.version,
    )


@router.post(
    "/internal/fire-activity-events/{event_id}/validate",
    response_model=FireActivityEventMutationResponse,
)
def validate_fire_activity_event(
    event_id: str,
    payload: EventTransitionRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> FireActivityEventMutationResponse:
    _require_enabled(settings)
    require_current_role(actor, settings, "analyst", "administrator")
    row = transition_fire_activity_event(
        session,
        event_id=event_id,
        action="validate",
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    return FireActivityEventMutationResponse(
        event_id=row.event_id,
        state=row.state,
        version=row.version,
    )


@router.post(
    "/internal/fire-activity-events/{event_id}/reject",
    response_model=FireActivityEventMutationResponse,
)
def reject_fire_activity_event(
    event_id: str,
    payload: EventTransitionRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> FireActivityEventMutationResponse:
    _require_enabled(settings)
    require_current_role(actor, settings, "analyst", "administrator")
    row = transition_fire_activity_event(
        session,
        event_id=event_id,
        action="reject",
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    return FireActivityEventMutationResponse(
        event_id=row.event_id,
        state=row.state,
        version=row.version,
    )


@router.post(
    "/internal/fire-activity-events/{event_id}/publish",
    response_model=FireActivityEventMutationResponse,
)
def publish_fire_activity_event(
    event_id: str,
    payload: EventTransitionRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> FireActivityEventMutationResponse:
    _require_enabled(settings)
    if not settings.v2_publication_enabled:
        raise HTTPException(status_code=404, detail="Event publication v2 is disabled.")
    require_role(actor, "editor", "administrator")
    require_recent_active_session(
        actor,
        settings,
        required_roles=("editor", "administrator"),
    )
    row = transition_fire_activity_event(
        session,
        event_id=event_id,
        action="publish",
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    return FireActivityEventMutationResponse(
        event_id=row.event_id,
        state=row.state,
        version=row.version,
    )


@router.post(
    "/internal/fire-activity-events/{event_id}/retract",
    response_model=FireActivityEventMutationResponse,
)
def retract_fire_activity_event(
    event_id: str,
    payload: EventTransitionRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> FireActivityEventMutationResponse:
    _require_enabled(settings)
    if not settings.v2_publication_enabled:
        raise HTTPException(status_code=404, detail="Event publication v2 is disabled.")
    require_role(actor, "editor", "administrator")
    require_recent_active_session(
        actor,
        settings,
        required_roles=("editor", "administrator"),
    )
    row = transition_fire_activity_event(
        session,
        event_id=event_id,
        action="retract",
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    return FireActivityEventMutationResponse(
        event_id=row.event_id,
        state=row.state,
        version=row.version,
    )
