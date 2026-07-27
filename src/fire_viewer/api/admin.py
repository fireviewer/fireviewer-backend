from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Header, Path, Query, Request, Response
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from fire_viewer.api.dependencies import (
    ActorDep,
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    TraceIdDep,
)
from fire_viewer.core.security import Actor, new_local_session, require_role, verify_local_password
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import (
    AdminLocalSession,
    AdminLoginAttempt,
    IncidentMapCapture,
    IncidentSeries,
    ManifestRevision,
    SpatialPackage,
    SpatialZone,
    SpatialZoneRevision,
    ZonePublication,
)
from fire_viewer.domain.enums import (
    PublicReportState,
    SpatialPackageFileKind,
    SpatialPackageState,
    ZonePublicationState,
)
from fire_viewer.domain.errors import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
)
from fire_viewer.domain.incident_spatial_schemas import (
    ActiveFireZoneMergeRequest,
    ActiveFireZoneReviewRequest,
    ActiveFireZoneRevisionCreateRequest,
    AdminActiveFireZoneRevision,
    AdminAgentReviewPackage,
    AdminIncidentMapCapture,
    AdminIncidentSpatialMarker,
    AdminIncidentSpatialReviewWorkspace,
    AgentReviewResolutionRequest,
    IncidentGltfPickRequest,
    IncidentGltfPickResponse,
    IncidentMapCaptureFinalizeRequest,
    IncidentMapCaptureUploadGrantRequest,
    IncidentMarkerReviewRequest,
)
from fire_viewer.domain.schemas import (
    AdminAuditListResponse,
    AdminBlobUploadGrantRequest,
    AdminBlobUploadGrantResponse,
    AdminBlobUploadTokenRequest,
    AdminBlobUploadTokenResponse,
    AdminConfigurationResponse,
    AdminIncidentBulletinEntriesResponse,
    AdminIncidentBulletinEntry,
    AdminIncidentBulletinEntryCreateRequest,
    AdminIncidentBulletinEntryRetireRequest,
    AdminIncidentBulletinUpdateRequest,
    AdminIncidentBulletinUpdateResponse,
    AdminIncidentDetail,
    AdminIncidentGalleryCreateRequest,
    AdminIncidentGalleryItem,
    AdminIncidentGalleryResponse,
    AdminIncidentGalleryReviewRequest,
    AdminIncidentListResponse,
    AdminIncidentModelsPipelineResponse,
    AdminIncidentObservationsResponse,
    AdminIncidentOfficialResource,
    AdminIncidentOfficialResourcesResponse,
    AdminIncidentOperationalInformation,
    AdminIncidentOperationalInformationResponse,
    AdminIncidentPublicationStatus,
    AdminIncidentSourcesMediaResponse,
    AdminOfficialResourceReviewRequest,
    AdminOperationalInformationCreateRequest,
    AdminOperationalInformationReviewRequest,
    AdminPublicationActionRequest,
    AdminPublicationListResponse,
    AdminPublicReportEnvelope,
    AdminPublicReportListResponse,
    AdminPublicReportReviewRequest,
    AdminRolesResponse,
    AdminSpatialPackageActionRequest,
    AdminSpatialPackageFromBlobRequest,
    AdminSpatialPackageImportEnvelope,
    AdminSpatialPackagePublicationEnvelope,
    AdminSpatialPackagePublicationRequest,
    AdminSpatialPackageRecoveryRequest,
    AdminSystemStatus,
    AdminWorkQueueResponse,
    AdminZoneCreateRequest,
    AdminZoneDetailResponse,
    AdminZoneEnvelope,
    AdminZoneInformationCreateRequest,
    AdminZoneInformationEnvelope,
    AdminZoneInformationUpdateRequest,
    AdminZoneListResponse,
    AdminZoneRevisionCreateRequest,
    AdminZoneRevisionEnvelope,
    AdminZoneRevisionSummary,
    AdminZoneUpdateRequest,
    StrictModel,
    ZoneVisibilityRequest,
)
from fire_viewer.services.admin_incident_publication import (
    create_admin_incident_bulletin_entry,
    list_admin_incident_bulletin_entries,
    retire_admin_incident_bulletin_entry,
    update_admin_incident_bulletin,
)
from fire_viewer.services.admin_incident_publication import (
    get_admin_incident_publication_status as get_admin_incident_publication_status_service,
)
from fire_viewer.services.admin_incidents import (
    create_admin_incident_gallery_item,
    create_admin_incident_operational_information,
    get_admin_incident,
    get_admin_incident_gallery,
    get_admin_incident_operational_information,
    get_admin_work_queue,
    list_admin_incidents,
    review_admin_incident_gallery_item,
    review_admin_incident_official_resource,
    review_admin_incident_operational_information,
)
from fire_viewer.services.admin_incidents import (
    get_admin_incident_models_pipeline as get_admin_incident_models_pipeline_service,
)
from fire_viewer.services.admin_incidents import (
    get_admin_incident_observations as get_admin_incident_observations_service,
)
from fire_viewer.services.admin_incidents import (
    get_admin_incident_official_resources as get_admin_incident_official_resources_service,
)
from fire_viewer.services.admin_incidents import (
    get_admin_incident_sources_media as get_admin_incident_sources_media_service,
)
from fire_viewer.services.admin_observability import (
    get_admin_roles,
    get_safe_configuration,
    get_system_status,
    list_global_audit,
)
from fire_viewer.services.blob_uploads import (
    ALLOWED_GALLERY_CONTENT_TYPES,
    ALLOWED_PACKAGE_CONTENT_TYPES,
    INCIDENT_GALLERY_MAX_BYTES,
    create_blob_upload_grant,
    issue_blob_client_token,
)
from fire_viewer.services.incident_gallery import (
    finalize_map_capture,
    grant_map_capture_upload,
)
from fire_viewer.services.incident_spatial_review import (
    create_zone_revision as create_active_zone_revision_service,
)
from fire_viewer.services.incident_spatial_review import (
    get_spatial_review_workspace,
    merge_zone_revisions,
    project_gltf_pick,
    resolve_agent_review,
    review_marker,
    review_zone_revision,
)
from fire_viewer.services.public_incident_view import list_public_reports, review_public_report
from fire_viewer.services.spatial_package_blob_import import (
    import_blob_package,
    recover_blob_package_request,
    validate_blob_package,
)
from fire_viewer.services.spatial_package_import import create_spatial_revision
from fire_viewer.services.spatial_package_publication import (
    change_publication_state,
    enable_spatial_package_preview,
    list_publications,
    publish_spatial_package,
    validate_spatial_package,
)
from fire_viewer.services.zone_workflow import (
    create_information,
    create_zone,
    get_admin_zone_detail,
    list_admin_zones,
    set_zone_visibility,
    update_information,
    update_zone,
)
from fire_viewer.storage import build_object_store
from fire_viewer.storage.object_store import ObjectStorageError

router = APIRouter(prefix="/admin", tags=["admin"])

ZoneIdPath = Annotated[str, Path(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9-]*$")]
RevisionPath = Annotated[int, Path(ge=1)]
InformationIdPath = Annotated[str, Path(min_length=3, max_length=96)]
PublicReportIdPath = Annotated[str, Path(min_length=3, max_length=96)]


class AdminSessionStatus(StrictModel):
    authenticated: Literal[True] = True
    csrf_token: str | None = None


class AdminLoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class AdminAuthStatus(StrictModel):
    mode: Literal["local_admin", "jwt", "disabled"]
    authenticated: bool


class AdminPrivatePreviewFile(StrictModel):
    file_id: int
    path: str | None
    kind: str
    sha256: str
    size_bytes: int
    media_type: str


class AdminPrivatePreviewScene(StrictModel):
    catalog_url: str
    files: dict[str, str]


class AdminPrivatePreviewResponse(StrictModel):
    zone_id: str
    revision: int
    preview_scope: Literal["private-admin"]
    package_id: str | None
    package_state: str | None
    publication_id: str | None
    publication_state: str | None
    publication_active: bool
    linked_fire_ids: list[str] = Field(default_factory=list)
    verification_report: dict[str, object]
    preview_package_ids: list[str] = Field(default_factory=list)
    scene: AdminPrivatePreviewScene | None = None
    files: list[AdminPrivatePreviewFile] = Field(default_factory=list)


def _require_admin(actor: Actor) -> None:
    require_role(actor, "administrator")


def _revision_summary(revision: SpatialZoneRevision) -> AdminZoneRevisionSummary:
    return AdminZoneRevisionSummary(
        revision=revision.revision,
        spatial_profile_version=revision.spatial_profile_version,
        origin_l93_ngf=(
            revision.origin_easting_l93,
            revision.origin_northing_l93,
            revision.source_orthometric_height_m,
        )
        if revision.origin_easting_l93 is not None and revision.origin_northing_l93 is not None
        else None,
        horizontal_crs=revision.horizontal_crs,
        vertical_crs=revision.vertical_crs,
        ground_model=revision.ground_model,
        ground_resolution_m=revision.ground_resolution_m,
        surface_height_reference=revision.surface_height_reference,
        origin_wgs84=(
            revision.origin_lon,
            revision.origin_lat,
            revision.origin_ellipsoid_height_m,
        ),
        local_frame="ENU",
        meters_per_unit=revision.meters_per_unit,
        vertical_datum=revision.vertical_datum,
        bounds_m={
            "east": (revision.min_east_m, revision.max_east_m),
            "north": (revision.min_north_m, revision.max_north_m),
            "up": (revision.min_up_m, revision.max_up_m),
        },
    )


def _set_mutation_headers(response: Response, *, replayed: bool) -> None:
    response.headers["Idempotent-Replay"] = "true" if replayed else "false"
    response.headers["Cache-Control"] = "no-store"


def _set_admin_read_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/auth/status", response_model=AdminAuthStatus)
def get_auth_status(
    settings: SettingsDep, fireviewer_admin: Annotated[str | None, Cookie()] = None
) -> AdminAuthStatus:
    return AdminAuthStatus(
        mode=settings.auth_mode,
        authenticated=bool(fireviewer_admin) if settings.auth_mode == "local_admin" else False,
    )


@router.post(
    "/auth/login",
    response_model=AdminSessionStatus,
    response_model_exclude_none=True,
)
def login_local_admin(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminSessionStatus:
    if settings.auth_mode != "local_admin" or not settings.local_admin_password_hash:
        raise NotFoundError("admin_auth", "local-login")
    origin = request.client.host if request.client else "unknown"
    origin_hash = hashlib.sha256(origin.encode()).hexdigest()
    window = utcnow() - timedelta(minutes=15)
    attempts = int(
        session.scalar(
            select(func.count())
            .select_from(AdminLoginAttempt)
            .where(
                AdminLoginAttempt.origin_hash == origin_hash,
                AdminLoginAttempt.attempted_at >= window,
            )
        )
        or 0
    )
    valid = payload.username == settings.local_admin_username and verify_local_password(
        payload.password, settings.local_admin_password_hash
    )
    if attempts >= settings.local_admin_login_limit or not valid:
        session.add(AdminLoginAttempt(origin_hash=origin_hash))
        session.commit()
        raise UnauthorizedError("Invalid administrator credentials.")
    token, csrf = new_local_session(session, settings)
    response.set_cookie(
        "fireviewer_admin",
        token,
        httponly=True,
        secure=settings.environment != "development",
        samesite="strict",
        path="/api",
    )
    _set_admin_read_headers(response)
    return AdminSessionStatus(csrf_token=csrf)


@router.post("/auth/logout", status_code=204)
def logout_local_admin(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    fireviewer_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    _require_admin(actor)
    if settings.auth_mode == "local_admin" and fireviewer_admin:
        row = session.execute(
            select(AdminLocalSession).where(
                AdminLocalSession.session_hash
                == hashlib.sha256(fireviewer_admin.encode()).hexdigest()
            )
        ).scalar_one_or_none()
        if row and row.revoked_at is None:
            row.revoked_at = utcnow()
            session.commit()
    response.delete_cookie("fireviewer_admin", path="/api")
    response.status_code = 204
    _set_admin_read_headers(response)
    return response


@router.get(
    "/session",
    response_model=AdminSessionStatus,
    response_model_exclude_none=True,
)
def get_admin_session(response: Response, actor: ActorDep) -> AdminSessionStatus:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return AdminSessionStatus(csrf_token=actor.csrf_token)


@router.get("/audit", response_model=AdminAuditListResponse)
def get_global_audit(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    action: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    target_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> AdminAuditListResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return list_global_audit(session, limit=limit, action=action, target_id=target_id)


@router.get("/roles", response_model=AdminRolesResponse)
def get_roles(response: Response, actor: ActorDep, settings: SettingsDep) -> AdminRolesResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_roles(actor, settings)


@router.get("/system", response_model=AdminSystemStatus)
def get_system(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminSystemStatus:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_system_status(session, settings)


@router.get("/configuration", response_model=AdminConfigurationResponse)
def get_configuration(
    response: Response,
    actor: ActorDep,
    settings: SettingsDep,
) -> AdminConfigurationResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_safe_configuration(settings)


@router.get("/incidents", response_model=AdminIncidentListResponse)
def list_incidents(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminIncidentListResponse:
    """Private operational inventory keyed by the stable fire_id, never by zone_id."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return list_admin_incidents(session, settings=settings)


@router.get("/work-queue", response_model=AdminWorkQueueResponse)
def get_work_queue(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminWorkQueueResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_work_queue(session)


@router.get("/incidents/{fire_id}", response_model=AdminIncidentDetail)
def get_incident(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminIncidentDetail:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident(session, fire_id=fire_id, settings=settings)


@router.get(
    "/incidents/{fire_id}/publication-status",
    response_model=AdminIncidentPublicationStatus,
)
def get_incident_publication_status(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentPublicationStatus:
    """One private readiness projection; it never changes a publication."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_publication_status_service(session, fire_id=fire_id)


@router.patch(
    "/incidents/{fire_id}/bulletin",
    response_model=AdminIncidentBulletinUpdateResponse,
)
def update_incident_bulletin(
    fire_id: str,
    payload: AdminIncidentBulletinUpdateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentBulletinUpdateResponse:
    """Directly publish a source-qualified bulletin edit, with an audit trail."""
    _require_admin(actor)
    _ = idempotency_key
    result = update_admin_incident_bulletin(
        session, fire_id=fire_id, payload=payload, actor=actor, trace_id=trace_id
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.get(
    "/incidents/{fire_id}/bulletin/entries",
    response_model=AdminIncidentBulletinEntriesResponse,
)
def get_incident_bulletin_entries(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentBulletinEntriesResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return list_admin_incident_bulletin_entries(session, fire_id=fire_id)


@router.post(
    "/incidents/{fire_id}/bulletin/entries",
    response_model=AdminIncidentBulletinEntry,
    status_code=201,
)
def create_incident_bulletin_entry(
    fire_id: str,
    payload: AdminIncidentBulletinEntryCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentBulletinEntry:
    _require_admin(actor)
    _ = idempotency_key
    result = create_admin_incident_bulletin_entry(
        session, fire_id=fire_id, payload=payload, actor=actor, trace_id=trace_id
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.post(
    "/incidents/{fire_id}/bulletin/entries/{entry_id}/retire",
    response_model=AdminIncidentBulletinEntry,
)
def retire_incident_bulletin_entry(
    fire_id: str,
    entry_id: str,
    payload: AdminIncidentBulletinEntryRetireRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentBulletinEntry:
    _require_admin(actor)
    _ = idempotency_key
    result = retire_admin_incident_bulletin_entry(
        session,
        fire_id=fire_id,
        entry_id=entry_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.get(
    "/incidents/{fire_id}/observations",
    response_model=AdminIncidentObservationsResponse,
)
def get_incident_observations(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentObservationsResponse:
    """Private review surface scoped to one permanent fire_id."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_observations_service(session, fire_id=fire_id)


@router.get(
    "/incidents/{fire_id}/sources-media",
    response_model=AdminIncidentSourcesMediaResponse,
)
def get_incident_sources_media(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentSourcesMediaResponse:
    """Private source and evidence-metadata surface; never streams raw media."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_sources_media_service(session, fire_id=fire_id)


@router.get(
    "/incidents/{fire_id}/official-resources",
    response_model=AdminIncidentOfficialResourcesResponse,
)
def get_incident_official_resources(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentOfficialResourcesResponse:
    """Private review queue; proposed links are deliberately never public."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_official_resources_service(session, fire_id=fire_id)


@router.post(
    "/incidents/{fire_id}/official-resources/{resource_id}/review",
    response_model=AdminIncidentOfficialResource,
)
def review_incident_official_resource(
    fire_id: str,
    resource_id: str,
    payload: AdminOfficialResourceReviewRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentOfficialResource:
    _require_admin(actor)
    _ = idempotency_key
    result = review_admin_incident_official_resource(
        session,
        fire_id=fire_id,
        resource_id=resource_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.get(
    "/incidents/{fire_id}/gallery",
    response_model=AdminIncidentGalleryResponse,
)
def get_incident_gallery(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentGalleryResponse:
    """Separate editorial gallery queue. It never exposes agent-media or contribution rows."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_gallery(session, fire_id=fire_id)


@router.post(
    "/incidents/{fire_id}/gallery",
    response_model=AdminIncidentGalleryItem,
    status_code=201,
)
def create_incident_gallery_item(
    fire_id: str,
    payload: AdminIncidentGalleryCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentGalleryItem:
    _require_admin(actor)
    _ = idempotency_key
    result = create_admin_incident_gallery_item(
        session,
        fire_id=fire_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.post(
    "/incidents/{fire_id}/gallery/{gallery_item_id}/review",
    response_model=AdminIncidentGalleryItem,
)
def review_incident_gallery_item(
    fire_id: str,
    gallery_item_id: str,
    payload: AdminIncidentGalleryReviewRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentGalleryItem:
    _require_admin(actor)
    _ = idempotency_key
    result = review_admin_incident_gallery_item(
        session,
        fire_id=fire_id,
        gallery_item_id=gallery_item_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.get(
    "/incidents/{fire_id}/operational-information",
    response_model=AdminIncidentOperationalInformationResponse,
)
def get_incident_operational_information(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentOperationalInformationResponse:
    """Private operational-information queue; only reviewed entries can reach public-view."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_operational_information(session, fire_id=fire_id)


@router.post(
    "/incidents/{fire_id}/operational-information",
    response_model=AdminIncidentOperationalInformation,
    status_code=201,
)
def create_incident_operational_information(
    fire_id: str,
    payload: AdminOperationalInformationCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentOperationalInformation:
    _require_admin(actor)
    _ = idempotency_key
    result = create_admin_incident_operational_information(
        session,
        fire_id=fire_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.post(
    "/incidents/{fire_id}/operational-information/{information_id}/review",
    response_model=AdminIncidentOperationalInformation,
)
def review_incident_operational_information(
    fire_id: str,
    information_id: str,
    payload: AdminOperationalInformationReviewRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentOperationalInformation:
    _require_admin(actor)
    _ = idempotency_key
    result = review_admin_incident_operational_information(
        session,
        fire_id=fire_id,
        information_id=information_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.get(
    "/incidents/{fire_id}/models-pipeline",
    response_model=AdminIncidentModelsPipelineResponse,
)
def get_incident_models_pipeline(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentModelsPipelineResponse:
    """Private manifest, asset and job metadata for one incident dossier."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_incident_models_pipeline_service(session, fire_id=fire_id)


@router.get(
    "/incidents/{fire_id}/spatial-review",
    response_model=AdminIncidentSpatialReviewWorkspace,
)
def get_incident_spatial_review(
    fire_id: str,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminIncidentSpatialReviewWorkspace:
    """Current immutable scene plus private marker and active-zone overlay revisions."""
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_spatial_review_workspace(session, fire_id=fire_id)


@router.get("/incidents/{fire_id}/map-gallery/{capture_id}")
def get_incident_map_capture(
    fire_id: str,
    capture_id: str,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    _require_admin(actor)
    capture = session.execute(
        select(IncidentMapCapture)
        .join(IncidentSeries, IncidentSeries.id == IncidentMapCapture.incident_id)
        .where(
            IncidentSeries.fire_id == fire_id,
            IncidentMapCapture.capture_id == capture_id,
        )
    ).scalar_one_or_none()
    if capture is None:
        raise NotFoundError("incident_map_capture", capture_id)
    store = build_object_store(settings)
    try:
        metadata = store.head(capture.object_uri)
        content = store.read_bytes(capture.object_uri)
    except ObjectStorageError as exc:
        raise NotFoundError("incident_map_capture", capture_id) from exc
    if metadata.size_bytes != capture.size_bytes or not hmac.compare_digest(
        hashlib.sha256(content).hexdigest(), capture.sha256
    ):
        raise ConflictError("map_capture_integrity_failed", "The map capture is inconsistent.")
    return Response(
        content=content,
        media_type=capture.media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post(
    "/incidents/{fire_id}/map-gallery/upload-grant",
    response_model=AdminBlobUploadGrantResponse,
    status_code=201,
)
def grant_incident_map_capture_upload(
    fire_id: str,
    payload: IncidentMapCaptureUploadGrantRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminBlobUploadGrantResponse:
    _require_admin(actor)
    grant = grant_map_capture_upload(
        session,
        fire_id=fire_id,
        payload=payload,
        actor=actor,
        settings=settings,
    )
    return AdminBlobUploadGrantResponse(
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        upload_grant=grant.token,
        expires_at=grant.expires_at,
        maximum_file_size_bytes=INCIDENT_GALLERY_MAX_BYTES,
        allowed_content_types=list(ALLOWED_GALLERY_CONTENT_TYPES),
    )


@router.post(
    "/incidents/{fire_id}/map-gallery/from-blob",
    response_model=AdminIncidentMapCapture,
    status_code=201,
)
def finalize_incident_map_capture(
    fire_id: str,
    payload: IncidentMapCaptureFinalizeRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AdminIncidentMapCapture:
    _require_admin(actor)
    return finalize_map_capture(
        session,
        fire_id=fire_id,
        payload=payload,
        actor=actor,
        settings=settings,
        trace_id=trace_id,
    )


@router.post(
    "/incidents/{fire_id}/spatial-review/project-pick",
    response_model=IncidentGltfPickResponse,
)
def project_incident_spatial_pick(
    fire_id: str,
    payload: IncidentGltfPickRequest,
    actor: ActorDep,
    session: SessionDep,
) -> IncidentGltfPickResponse:
    _require_admin(actor)
    return project_gltf_pick(session, fire_id=fire_id, payload=payload)


@router.post(
    "/incidents/{fire_id}/spatial-markers/{marker_id}/review",
    response_model=AdminIncidentSpatialMarker,
)
def review_incident_spatial_marker(
    fire_id: str,
    marker_id: str,
    payload: IncidentMarkerReviewRequest,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> AdminIncidentSpatialMarker:
    _require_admin(actor)
    return review_marker(
        session,
        fire_id=fire_id,
        marker_id=marker_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )


@router.post(
    "/incidents/{fire_id}/active-zone-revisions",
    response_model=AdminActiveFireZoneRevision,
    status_code=201,
)
def create_incident_active_zone_revision(
    fire_id: str,
    payload: ActiveFireZoneRevisionCreateRequest,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> AdminActiveFireZoneRevision:
    _require_admin(actor)
    return create_active_zone_revision_service(
        session, fire_id=fire_id, payload=payload, actor=actor, trace_id=trace_id
    )


@router.post(
    "/incidents/{fire_id}/active-zone-revisions/merge",
    response_model=AdminActiveFireZoneRevision,
    status_code=201,
)
def merge_incident_active_zone_revisions(
    fire_id: str,
    payload: ActiveFireZoneMergeRequest,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> AdminActiveFireZoneRevision:
    _require_admin(actor)
    return merge_zone_revisions(
        session, fire_id=fire_id, payload=payload, actor=actor, trace_id=trace_id
    )


@router.post(
    "/incidents/{fire_id}/active-zone-revisions/{zone_revision_id}/review",
    response_model=AdminActiveFireZoneRevision,
)
def review_incident_active_zone_revision(
    fire_id: str,
    zone_revision_id: str,
    payload: ActiveFireZoneReviewRequest,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> AdminActiveFireZoneRevision:
    _require_admin(actor)
    return review_zone_revision(
        session,
        fire_id=fire_id,
        zone_revision_id=zone_revision_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )


@router.post(
    "/incidents/{fire_id}/agent-reviews/{review_id}/resolve",
    response_model=AdminAgentReviewPackage,
)
def resolve_incident_agent_review(
    fire_id: str,
    review_id: str,
    payload: AgentReviewResolutionRequest,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> AdminAgentReviewPackage:
    _require_admin(actor)
    return resolve_agent_review(
        session,
        fire_id=fire_id,
        review_id=review_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )


@router.get("/reports", response_model=AdminPublicReportListResponse)
def list_admin_public_reports(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    state: Annotated[PublicReportState | None, Query()] = None,
) -> AdminPublicReportListResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return list_public_reports(session, state=state)


@router.post("/reports/{report_id}/review", response_model=AdminPublicReportEnvelope)
def review_admin_public_report(
    report_id: PublicReportIdPath,
    payload: AdminPublicReportReviewRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminPublicReportEnvelope:
    _require_admin(actor)
    # The report state machine is optimistic-concurrent; the header is still required so clients
    # cannot accidentally repeat an administrative action after a transport failure.
    _ = idempotency_key
    result = review_public_report(
        session,
        report_id=report_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )
    _set_mutation_headers(response, replayed=False)
    return result


@router.get("/zones", response_model=AdminZoneListResponse)
def list_zones(response: Response, actor: ActorDep, session: SessionDep) -> AdminZoneListResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return list_admin_zones(session)


@router.post("/zones", response_model=AdminZoneEnvelope, status_code=201)
def create_admin_zone(
    payload: AdminZoneCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminZoneEnvelope:
    _require_admin(actor)
    outcome = create_zone(
        session,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.status_code = 201
    _set_mutation_headers(response, replayed=outcome.replayed)
    return AdminZoneEnvelope.model_validate(outcome.response)


@router.get("/zones/{zone_id}", response_model=AdminZoneDetailResponse)
def get_zone(
    zone_id: ZoneIdPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminZoneDetailResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return get_admin_zone_detail(session, zone_id=zone_id)


@router.patch("/zones/{zone_id}", response_model=AdminZoneEnvelope)
def update_admin_zone(
    zone_id: ZoneIdPath,
    payload: AdminZoneUpdateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminZoneEnvelope:
    _require_admin(actor)
    outcome = update_zone(
        session,
        zone_id=zone_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return AdminZoneEnvelope.model_validate(outcome.response)


@router.post("/zones/{zone_id}/visibility", response_model=AdminZoneEnvelope)
def set_admin_zone_visibility(
    zone_id: ZoneIdPath,
    payload: ZoneVisibilityRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminZoneEnvelope:
    _require_admin(actor)
    outcome = set_zone_visibility(
        session,
        zone_id=zone_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return AdminZoneEnvelope.model_validate(outcome.response)


@router.post(
    "/zones/{zone_id}/information", response_model=AdminZoneInformationEnvelope, status_code=201
)
def create_admin_information(
    zone_id: ZoneIdPath,
    payload: AdminZoneInformationCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminZoneInformationEnvelope:
    _require_admin(actor)
    outcome = create_information(
        session,
        zone_id=zone_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.status_code = 201
    _set_mutation_headers(response, replayed=outcome.replayed)
    return AdminZoneInformationEnvelope.model_validate(outcome.response)


@router.patch(
    "/zones/{zone_id}/information/{information_id}",
    response_model=AdminZoneInformationEnvelope,
)
def update_admin_information(
    zone_id: ZoneIdPath,
    information_id: InformationIdPath,
    payload: AdminZoneInformationUpdateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminZoneInformationEnvelope:
    _require_admin(actor)
    outcome = update_information(
        session,
        zone_id=zone_id,
        information_id=information_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return AdminZoneInformationEnvelope.model_validate(outcome.response)


# Spatial revisions remain a technical registry. Package creation belongs to the
# pipeline/import path, while validation, preview and publication are explicit admin actions.
@router.post(
    "/zones/{zone_id}/revisions",
    response_model=AdminZoneRevisionEnvelope,
    status_code=201,
)
def create_zone_revision(
    zone_id: ZoneIdPath,
    payload: AdminZoneRevisionCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminZoneRevisionEnvelope:
    _require_admin(actor)
    outcome = create_spatial_revision(
        session,
        zone_id=zone_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return AdminZoneRevisionEnvelope.model_validate(outcome.response)


@router.get("/zones/{zone_id}/revisions/{revision}", response_model=AdminZoneRevisionSummary)
def get_zone_revision(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminZoneRevisionSummary:
    _require_admin(actor)
    _set_admin_read_headers(response)
    row = session.execute(
        select(SpatialZoneRevision)
        .join(SpatialZone)
        .where(SpatialZone.zone_id == zone_id, SpatialZoneRevision.revision == revision)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("spatial_zone_revision", f"{zone_id}/revisions/{revision}")
    return _revision_summary(row)


@router.post(
    "/zones/{zone_id}/revisions/{revision}/packages/upload-grant",
    response_model=AdminBlobUploadGrantResponse,
    status_code=201,
)
def grant_zone_package_upload(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    payload: AdminBlobUploadGrantRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminBlobUploadGrantResponse:
    _require_admin(actor)
    revision_exists = session.execute(
        select(SpatialZoneRevision.id)
        .join(SpatialZone)
        .where(SpatialZone.zone_id == zone_id, SpatialZoneRevision.revision == revision)
    ).scalar_one_or_none()
    if revision_exists is None:
        raise NotFoundError("spatial_zone_revision", f"{zone_id}/revisions/{revision}")
    if (
        session.execute(
            select(SpatialPackage.id).where(SpatialPackage.package_id == payload.package_id)
        ).scalar_one_or_none()
        is not None
    ):
        raise ConflictError(
            "spatial_package_already_exists",
            "The package identifier is already registered.",
        )
    grant = create_blob_upload_grant(payload=payload, actor=actor, settings=settings)
    return AdminBlobUploadGrantResponse(
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        upload_grant=grant.token,
        expires_at=grant.expires_at,
        maximum_file_size_bytes=settings.zone_upload_max_bytes,
        allowed_content_types=list(ALLOWED_PACKAGE_CONTENT_TYPES),
    )


@router.post("/blob-upload-token", response_model=AdminBlobUploadTokenResponse)
def grant_blob_client_token(
    payload: AdminBlobUploadTokenRequest,
    settings: SettingsDep,
    upload_grant: Annotated[
        str, Header(alias="X-Blob-Upload-Grant", min_length=64, max_length=4096)
    ],
) -> AdminBlobUploadTokenResponse:
    return AdminBlobUploadTokenResponse(
        clientToken=issue_blob_client_token(
            pathname=payload.payload.pathname,
            client_payload=payload.payload.clientPayload,
            upload_grant=upload_grant,
            settings=settings,
        )
    )


@router.post(
    "/zones/{zone_id}/revisions/{revision}/packages/from-blob",
    response_model=AdminSpatialPackageImportEnvelope,
    status_code=201,
)
def finalize_zone_package_from_blob(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    payload: AdminSpatialPackageFromBlobRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackageImportEnvelope:
    _require_admin(actor)
    validated = validate_blob_package(
        zone_id=zone_id,
        revision=revision,
        payload=payload,
        settings=settings,
    )
    outcome = import_blob_package(
        session,
        zone_id=zone_id,
        revision=revision,
        payload=payload,
        validated=validated,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.status_code = 201
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response


@router.post(
    "/zones/{zone_id}/revisions/{revision}/packages/recover-from-blob",
    response_model=AdminSpatialPackageImportEnvelope,
    status_code=201,
)
def recover_zone_package_from_blob(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    recovery: AdminSpatialPackageRecoveryRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackageImportEnvelope:
    """Finalize an interrupted upload by rebuilding its bounded Blob inventory."""

    _require_admin(actor)
    payload = recover_blob_package_request(
        upload_id=recovery.upload_id,
        package_id=recovery.package_id,
        reason=recovery.reason,
        settings=settings,
    )
    validated = validate_blob_package(
        zone_id=zone_id,
        revision=revision,
        payload=payload,
        settings=settings,
    )
    outcome = import_blob_package(
        session,
        zone_id=zone_id,
        revision=revision,
        payload=payload,
        validated=validated,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.status_code = 201
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response


@router.post(
    "/zones/{zone_id}/revisions/{revision}/validations",
    response_model=AdminSpatialPackagePublicationEnvelope,
)
def validate_zone_revision(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    payload: AdminSpatialPackageActionRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackagePublicationEnvelope:
    _require_admin(actor)
    outcome = validate_spatial_package(
        session,
        zone_id=zone_id,
        revision=revision,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response


@router.get(
    "/zones/{zone_id}/revisions/{revision}/preview",
    response_model=AdminPrivatePreviewResponse,
)
def get_private_preview(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminPrivatePreviewResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    revision_row = session.execute(
        select(SpatialZoneRevision)
        .join(SpatialZone)
        .where(SpatialZone.zone_id == zone_id, SpatialZoneRevision.revision == revision)
    ).scalar_one_or_none()
    if revision_row is None:
        raise NotFoundError("spatial_zone_revision", f"{zone_id}/revisions/{revision}")
    packages = list(
        session.execute(
            select(SpatialPackage)
            .where(SpatialPackage.spatial_zone_revision_id == revision_row.id)
            .options(selectinload(SpatialPackage.files))
            .order_by(SpatialPackage.created_at.desc(), SpatialPackage.id.desc())
        ).scalars()
    )
    package = packages[0] if packages else None
    publication = (
        session.execute(
            select(ZonePublication)
            .where(ZonePublication.spatial_package_id == package.id)
            .order_by(ZonePublication.created_at.desc(), ZonePublication.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if package
        else None
    )
    tiled = bool(
        package
        and {SpatialPackageFileKind.FWTILE, SpatialPackageFileKind.FWTERRAIN}.issubset(
            {item.kind for item in package.files}
        )
    )
    scene = (
        AdminPrivatePreviewScene(
            catalog_url=(
                f"/api/v1/admin/zones/{zone_id}/revisions/{revision}/preview/"
                f"packages/{package.package_id}/catalog"
            ),
            files={
                str(item.provenance["catalog_path"]): (
                    f"/api/v2/admin/packages/{package.package_id}/files/{item.id}"
                )
                for item in package.files
                if item.provenance.get("catalog_path")
            },
        )
        if package
        and tiled
        and package.state
        in {
            SpatialPackageState.PREVIEWABLE,
            SpatialPackageState.PUBLISHED,
            SpatialPackageState.WITHDRAWN,
        }
        else None
    )
    linked_fire_ids = (
        list(
            session.execute(
                select(IncidentSeries.fire_id)
                .join(ManifestRevision, ManifestRevision.incident_id == IncidentSeries.id)
                .where(
                    ManifestRevision.spatial_package_id == package.id,
                    ManifestRevision.is_current.is_(True),
                )
                .distinct()
                .order_by(IncidentSeries.fire_id)
            ).scalars()
        )
        if package
        else []
    )
    return AdminPrivatePreviewResponse(
        zone_id=zone_id,
        revision=revision,
        preview_scope="private-admin",
        package_id=package.package_id if package else None,
        package_state=str(package.state) if package else None,
        publication_id=publication.publication_id if publication else None,
        publication_state=str(publication.state) if publication else None,
        publication_active=publication.is_active if publication else False,
        linked_fire_ids=linked_fire_ids,
        verification_report=package.verification_report if package else {},
        preview_package_ids=[
            item.package_id
            for item in packages
            if str(item.state) in {"PREVIEWABLE", "PUBLISHED", "WITHDRAWN"}
        ],
        scene=scene,
        files=[
            AdminPrivatePreviewFile(
                file_id=file.id,
                path=(
                    str(file.provenance["catalog_path"])
                    if file.provenance.get("catalog_path")
                    else None
                ),
                kind=str(file.kind),
                sha256=file.sha256,
                size_bytes=file.size_bytes,
                media_type=file.media_type,
            )
            for file in (package.files if package else [])
        ],
    )


@router.get("/zones/{zone_id}/revisions/{revision}/preview/packages/{package_id}/catalog")
def get_private_preview_catalog(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    package_id: Annotated[str, Path(min_length=3, max_length=96)],
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    _require_admin(actor)
    package = session.execute(
        select(SpatialPackage)
        .join(
            SpatialZoneRevision,
            SpatialPackage.spatial_zone_revision_id == SpatialZoneRevision.id,
        )
        .join(SpatialZone, SpatialZoneRevision.spatial_zone_id == SpatialZone.id)
        .where(
            SpatialZone.zone_id == zone_id,
            SpatialZoneRevision.revision == revision,
            SpatialPackage.package_id == package_id,
        )
    ).scalar_one_or_none()
    if package is None:
        raise NotFoundError("spatial_package", package_id)
    if package.state not in {
        SpatialPackageState.PREVIEWABLE,
        SpatialPackageState.PUBLISHED,
        SpatialPackageState.WITHDRAWN,
    }:
        raise ConflictError(
            "spatial_package_preview_not_enabled",
            "Private tiled preview requires a previewable package.",
        )
    try:
        content = build_object_store(settings).read_bytes(
            f"{package.storage_uri.rstrip('/')}/catalog.json"
        )
    except ObjectStorageError as exc:
        raise NotFoundError("spatial_package_catalog", package_id) from exc
    digest = hashlib.sha256(content).hexdigest()
    expected_digest = str(package.provenance.get("catalog_sha256", ""))
    expected_size = package.provenance.get("catalog_size_bytes")
    if (expected_digest and expected_digest != digest) or (
        isinstance(expected_size, int) and expected_size != len(content)
    ):
        raise ConflictError(
            "spatial_package_catalog_changed",
            "The private catalog no longer matches the immutable package registry.",
        )
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "ETag": f'"{digest}"',
        },
    )


@router.get("/zones/{zone_id}/revisions/{revision}/preview/packages/{package_id}/png")
def get_private_preview_png(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    package_id: Annotated[str, Path(min_length=3, max_length=96)],
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    _require_admin(actor)
    package = session.execute(
        select(SpatialPackage)
        .join(
            SpatialZoneRevision,
            SpatialPackage.spatial_zone_revision_id == SpatialZoneRevision.id,
        )
        .join(SpatialZone, SpatialZoneRevision.spatial_zone_id == SpatialZone.id)
        .where(
            SpatialZone.zone_id == zone_id,
            SpatialZoneRevision.revision == revision,
            SpatialPackage.package_id == package_id,
        )
        .options(selectinload(SpatialPackage.files))
    ).scalar_one_or_none()
    if package is None:
        raise NotFoundError("spatial_package", package_id)
    if str(package.state) not in {"PREVIEWABLE", "PUBLISHED", "WITHDRAWN"}:
        raise ConflictError(
            "spatial_package_preview_not_enabled",
            "Private binary preview requires a previewable package.",
        )
    png = next((item for item in package.files if str(item.kind) == "PNG"), None)
    if png is None:
        raise NotFoundError("spatial_package_preview", package_id)
    try:
        content = build_object_store(settings).read_bytes(png.uri)
    except RuntimeError as exc:
        raise NotFoundError("spatial_package_preview", package_id) from exc
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"},
    )


@router.post(
    "/zones/{zone_id}/revisions/{revision}/preview",
    response_model=AdminSpatialPackagePublicationEnvelope,
)
def create_zone_preview(
    zone_id: ZoneIdPath,
    revision: RevisionPath,
    payload: AdminSpatialPackageActionRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackagePublicationEnvelope:
    _require_admin(actor)
    outcome = enable_spatial_package_preview(
        session,
        zone_id=zone_id,
        revision=revision,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response


@router.get("/publications", response_model=AdminPublicationListResponse)
def get_publications(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    state: ZonePublicationState | None = None,
) -> AdminPublicationListResponse:
    _require_admin(actor)
    _set_admin_read_headers(response)
    return list_publications(session, state=state)


@router.post(
    "/publications/{publication_id}/withdraw", response_model=AdminSpatialPackagePublicationEnvelope
)
def withdraw_publication(
    publication_id: str,
    payload: AdminPublicationActionRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackagePublicationEnvelope:
    _require_admin(actor)
    outcome = change_publication_state(
        session,
        publication_id=publication_id,
        target=ZonePublicationState.WITHDRAWN,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response


@router.post(
    "/publications/{publication_id}/restore", response_model=AdminSpatialPackagePublicationEnvelope
)
def restore_publication(
    publication_id: str,
    payload: AdminPublicationActionRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackagePublicationEnvelope:
    _require_admin(actor)
    outcome = change_publication_state(
        session,
        publication_id=publication_id,
        target=ZonePublicationState.PUBLISHED,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response


@router.post("/publications", response_model=AdminSpatialPackagePublicationEnvelope)
def publish_revision(
    payload: AdminSpatialPackagePublicationRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminSpatialPackagePublicationEnvelope:
    _require_admin(actor)
    outcome = publish_spatial_package(
        session,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _set_mutation_headers(response, replayed=outcome.replayed)
    return outcome.response
