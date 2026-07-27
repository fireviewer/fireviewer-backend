"""Versioned administration API. Public contracts remain under /api/v1."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from fire_viewer.api.dependencies import (
    ActorDep,
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    TraceIdDep,
)
from fire_viewer.core.security import Actor, require_role
from fire_viewer.db.models import IncidentSeries, ModelAsset, SpatialPackage, SpatialPackageFile
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.schemas import (
    AdminBlobUploadGrantRequest,
    AdminBlobUploadGrantResponse,
    AdminDashboardResponse,
    AdminIncidentCreateRequest,
    AdminIncidentCreateResponse,
    AdminIncidentListResponse,
    AdminIncidentRepresentationAttachRequest,
    AdminIncidentRepresentationAttachResponse,
    AdminIncidentSpatialPackageFromBlobRequest,
    AdminIncidentSpatialPackageImportResponse,
    AdminOperationalMapResponse,
    AdminWorkQueueResponse,
)
from fire_viewer.services.admin_dashboard import get_admin_dashboard
from fire_viewer.services.admin_incident_creation import create_admin_incident
from fire_viewer.services.admin_incidents import get_admin_work_queue, list_admin_incidents
from fire_viewer.services.admin_operational_map import get_operational_map
from fire_viewer.services.admin_representations import attach_incident_package
from fire_viewer.services.blob_uploads import (
    ALLOWED_PACKAGE_CONTENT_TYPES,
    create_blob_upload_grant,
)
from fire_viewer.services.incident_spatial_package_import import (
    import_incident_spatial_package,
)
from fire_viewer.services.spatial_package_blob_import import validate_blob_package
from fire_viewer.storage import build_object_store

router = APIRouter(prefix="/api/v2/admin", tags=["admin-v2"])


def _require_admin(actor: Actor) -> None:
    require_role(actor, "administrator", "analyst", "validator", "security_operator")


def _private_read(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _private_binary(
    *,
    uri: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    settings: SettingsDep,
    if_none_match: str | None,
) -> Response:
    etag = f'"{sha256}"'
    if if_none_match == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )
    if size_bytes > settings.admin_asset_proxy_max_bytes:
        raise ConflictError(
            "admin_asset_proxy_limit_exceeded",
            "This asset exceeds the private FastAPI preview limit.",
            extra={"limit_bytes": settings.admin_asset_proxy_max_bytes},
        )
    try:
        content = build_object_store(settings).read_bytes(uri)
    except RuntimeError as exc:
        raise NotFoundError("private_asset", sha256) from exc
    if len(content) != size_bytes:
        raise ConflictError(
            "private_asset_size_mismatch",
            "The stored asset size does not match its registry metadata.",
        )
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "ETag": etag,
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/dashboard", response_model=AdminDashboardResponse)
def dashboard(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminDashboardResponse:
    _require_admin(actor)
    _private_read(response)
    return get_admin_dashboard(session, settings=settings)


@router.get("/work-queue", response_model=AdminWorkQueueResponse)
def work_queue(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminWorkQueueResponse:
    _require_admin(actor)
    _private_read(response)
    return get_admin_work_queue(session)


@router.get("/incidents", response_model=AdminIncidentListResponse)
def incidents(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminIncidentListResponse:
    _require_admin(actor)
    _private_read(response)
    return list_admin_incidents(session, settings=settings)


@router.post("/incidents", response_model=AdminIncidentCreateResponse, status_code=201)
def create_incident(
    payload: AdminIncidentCreateRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentCreateResponse:
    _require_admin(actor)
    outcome = create_admin_incident(
        session,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    return outcome.response


@router.get("/operational-map", response_model=AdminOperationalMapResponse)
def operational_map(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AdminOperationalMapResponse:
    """National internal map; each marker remains a stable incident-centred fire_id."""

    _require_admin(actor)
    _private_read(response)
    return get_operational_map(session)


@router.post(
    "/incidents/{fire_id}/representations",
    response_model=AdminIncidentRepresentationAttachResponse,
)
def attach_representations(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    payload: AdminIncidentRepresentationAttachRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentRepresentationAttachResponse:
    _require_admin(actor)
    outcome = attach_incident_package(
        session,
        fire_id=fire_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    return outcome.response


@router.post(
    "/incidents/{fire_id}/spatial-package/upload-grant",
    response_model=AdminBlobUploadGrantResponse,
    status_code=201,
)
def grant_incident_spatial_package_upload(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    payload: AdminBlobUploadGrantRequest,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AdminBlobUploadGrantResponse:
    _require_admin(actor)
    if (
        session.execute(
            select(IncidentSeries.id).where(IncidentSeries.fire_id == fire_id)
        ).scalar_one_or_none()
        is None
    ):
        raise NotFoundError("incident", fire_id)
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


@router.post(
    "/incidents/{fire_id}/spatial-package/from-blob",
    response_model=AdminIncidentSpatialPackageImportResponse,
    status_code=201,
)
def finalize_incident_spatial_package_from_blob(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    payload: AdminIncidentSpatialPackageFromBlobRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminIncidentSpatialPackageImportResponse:
    _require_admin(actor)
    validated = validate_blob_package(
        zone_id=payload.zone_id,
        revision=payload.revision,
        payload=payload,
        settings=settings,
    )
    outcome = import_incident_spatial_package(
        session,
        fire_id=fire_id,
        payload=payload,
        validated=validated,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    return outcome.response


@router.get("/packages/{package_id}/files/{file_id}")
def package_file(
    package_id: Annotated[str, Path(min_length=3, max_length=96)],
    file_id: Annotated[int, Path(ge=1)],
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    _require_admin(actor)
    file = session.execute(
        select(SpatialPackageFile)
        .join(SpatialPackage)
        .where(SpatialPackage.package_id == package_id, SpatialPackageFile.id == file_id)
    ).scalar_one_or_none()
    if file is None:
        raise NotFoundError("spatial_package_file", f"{package_id}/{file_id}")
    return _private_binary(
        uri=file.uri,
        sha256=file.sha256,
        size_bytes=file.size_bytes,
        media_type=file.media_type,
        settings=settings,
        if_none_match=if_none_match,
    )


@router.get("/assets/{asset_id}")
def model_asset(
    asset_id: Annotated[str, Path(min_length=3, max_length=64)],
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    _require_admin(actor)
    asset = session.execute(
        select(ModelAsset)
        .where(ModelAsset.asset_id == asset_id)
        .options(selectinload(ModelAsset.spatial_package_file))
    ).scalar_one_or_none()
    if asset is None or asset.spatial_package_file is None:
        raise NotFoundError("model_asset", asset_id)
    return _private_binary(
        uri=asset.spatial_package_file.uri,
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        media_type=asset.spatial_package_file.media_type,
        settings=settings,
        if_none_match=if_none_match,
    )
