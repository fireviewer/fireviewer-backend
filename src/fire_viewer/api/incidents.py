from __future__ import annotations

import csv
import hashlib
from io import StringIO
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from fire_viewer.api.dependencies import (
    FireIdDep,
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    SourceTokenDep,
    TraceIdDep,
)
from fire_viewer.db.models import SpatialPackage
from fire_viewer.domain.errors import NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    DetectionRequest,
    DetectionResponse,
    IncidentPublicResponse,
    PublicIncidentReportReceipt,
    PublicIncidentReportRequest,
    PublicIncidentView,
    ViewerManifest,
)
from fire_viewer.services.detection import process_detection
from fire_viewer.services.public_incident_view import get_public_incident_view, submit_public_report
from fire_viewer.services.queries import get_incident_public, get_viewer_manifest
from fire_viewer.storage.object_store import ObjectStorageError, build_object_store

router = APIRouter(tags=["incidents"])

_MANIFEST_CACHE_CONTROL = "public, max-age=30, must-revalidate"
_PUBLIC_VIEW_CACHE_CONTROL = "public, max-age=30, must-revalidate"
_TRACE_ID_HEADER: dict[str, Any] = {
    "description": "Trace identifier to include when reporting an error.",
    "schema": {"type": "string"},
}
_MANIFEST_SUCCESS_HEADERS: dict[str, dict[str, Any]] = {
    "ETag": {
        "description": "Strong entity tag calculated from the serialized ViewerManifest.",
        "schema": {"type": "string"},
    },
    "Cache-Control": {
        "description": "Short public cache lifetime for this viewer representation.",
        "schema": {"type": "string", "example": _MANIFEST_CACHE_CONTROL},
    },
    "X-Trace-Id": _TRACE_ID_HEADER,
}
_PROBLEM_DETAILS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type", "title", "status", "detail", "instance", "trace_id"],
    "properties": {
        "type": {"type": "string", "format": "uri-reference"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "trace_id": {"type": "string"},
    },
}


def _public_scene_package(
    session: SessionDep, fire_id: str, settings: SettingsDep
) -> SpatialPackage:
    manifest = get_viewer_manifest(session, fire_id, settings)
    if manifest.scene is None:
        raise NotFoundError("spatial_scene", fire_id)
    package = session.execute(
        select(SpatialPackage)
        .where(SpatialPackage.package_id == manifest.scene.package_id)
        .options(selectinload(SpatialPackage.files))
    ).scalar_one_or_none()
    if package is None:
        raise NotFoundError("spatial_scene", fire_id)
    return package


def _public_scene_binary(
    *,
    content: bytes,
    media_type: str,
    sha256: str,
    if_none_match: str | None,
    range_header: str | None = None,
) -> Response:
    etag = f'"{sha256}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=31536000, immutable",
        "Accept-Ranges": "bytes",
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    if range_header:
        unit, separator, raw_range = range_header.partition("=")
        start_text, dash, end_text = raw_range.partition("-")
        size = len(content)
        try:
            if unit.casefold() != "bytes" or not separator or not dash or "," in raw_range:
                raise ValueError
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            else:
                suffix_length = int(end_text)
                if suffix_length <= 0:
                    raise ValueError
                start = max(size - suffix_length, 0)
                end = size - 1
            if start < 0 or start >= size or end < start:
                raise ValueError
            end = min(end, size - 1)
        except ValueError:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(
            content=content[start : end + 1],
            status_code=206,
            media_type=media_type,
            headers=headers,
        )
    return Response(content=content, media_type=media_type, headers=headers)


def _manifest_problem_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {"X-Trace-Id": _TRACE_ID_HEADER},
        "content": {
            "application/problem+json": {"schema": _PROBLEM_DETAILS_SCHEMA},
        },
    }


@router.post(
    "/detect",
    response_model=DetectionResponse,
    responses={201: {"description": "A new incident series was created."}},
)
def detect_incident(
    payload: DetectionRequest,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
    source_token: SourceTokenDep,
) -> DetectionResponse:
    outcome = process_detection(
        session,
        payload=payload,
        idempotency_key=idempotency_key,
        source_token=source_token,
        trace_id=trace_id,
        settings=settings,
    )
    response.status_code = outcome.status_code
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    response.headers["Cache-Control"] = "no-store"
    return outcome.response


@router.get("/{fire_id}", response_model=IncidentPublicResponse)
def get_incident(
    fire_id: FireIdDep,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> IncidentPublicResponse:
    result = get_incident_public(session, fire_id, settings)
    response.headers["Cache-Control"] = "public, max-age=15, must-revalidate"
    return result


@router.get("/{fire_id}/public-view", response_model=PublicIncidentView)
def get_public_view(
    fire_id: FireIdDep,
    session: SessionDep,
    settings: SettingsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> PublicIncidentView | Response:
    view = get_public_incident_view(session, fire_id=fire_id, settings=settings)
    payload = view.model_dump(mode="json", exclude_none=False)
    etag = f'"{sha256_hex(payload)}"'
    headers = {"ETag": etag, "Cache-Control": _PUBLIC_VIEW_CACHE_CONTROL}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=jsonable_encoder(payload), headers=headers)


@router.get("/{fire_id}/public-view/export.json")
def export_public_view_json(
    fire_id: FireIdDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    view = get_public_incident_view(session, fire_id=fire_id, settings=settings)
    return JSONResponse(
        content=jsonable_encoder(view.model_dump(mode="json", exclude_none=False)),
        headers={
            "Cache-Control": _PUBLIC_VIEW_CACHE_CONTROL,
            "Content-Disposition": f'attachment; filename="{fire_id}-public-view.json"',
        },
    )


@router.get("/{fire_id}/public-view/timeline.csv")
def export_public_timeline_csv(
    fire_id: FireIdDep,
    session: SessionDep,
    settings: SettingsDep,
) -> PlainTextResponse:
    view = get_public_incident_view(session, fire_id=fire_id, settings=settings)
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["occurred_at", "kind", "label", "episode_id"])
    for event in view.timeline:
        writer.writerow(
            [event.occurred_at.isoformat(), event.kind, event.label, event.episode_id or ""]
        )
    return PlainTextResponse(
        stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": _PUBLIC_VIEW_CACHE_CONTROL,
            "Content-Disposition": f'attachment; filename="{fire_id}-public-timeline.csv"',
        },
    )


@router.get("/{fire_id}/spatial-scene/catalog")
def get_public_spatial_scene_catalog(
    fire_id: FireIdDep,
    session: SessionDep,
    settings: SettingsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    package = _public_scene_package(session, fire_id, settings)
    uri = f"{package.storage_uri.rstrip('/')}/catalog.json"
    try:
        content = build_object_store(settings).read_bytes(uri)
    except ObjectStorageError as exc:
        raise NotFoundError("spatial_scene_catalog", fire_id) from exc
    catalog_sha256 = str(package.provenance.get("catalog_sha256", ""))
    if len(catalog_sha256) != 64:
        catalog_sha256 = hashlib.sha256(content).hexdigest()
    return _public_scene_binary(
        content=content,
        media_type="application/json",
        sha256=catalog_sha256,
        if_none_match=if_none_match,
    )


@router.get("/{fire_id}/spatial-scene/files/{file_id}")
def get_public_spatial_scene_file(
    fire_id: FireIdDep,
    file_id: int,
    session: SessionDep,
    settings: SettingsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    package = _public_scene_package(session, fire_id, settings)
    file = next((item for item in package.files if item.id == file_id), None)
    if file is None:
        raise NotFoundError("spatial_scene_file", f"{fire_id}/{file_id}")
    try:
        content = build_object_store(settings).read_bytes(file.uri)
    except ObjectStorageError as exc:
        raise NotFoundError("spatial_scene_file", f"{fire_id}/{file_id}") from exc
    return _public_scene_binary(
        content=content,
        media_type=file.media_type,
        sha256=file.sha256,
        if_none_match=if_none_match,
        range_header=range_header,
    )


@router.post("/{fire_id}/reports", response_model=PublicIncidentReportReceipt, status_code=202)
def create_public_report(
    fire_id: FireIdDep,
    payload: PublicIncidentReportRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> PublicIncidentReportReceipt:
    # Do not trust forwarded headers unless a deployment explicitly adds a trusted proxy layer.
    origin = request.client.host if request.client is not None else "unknown"
    receipt = submit_public_report(
        session,
        fire_id=fire_id,
        payload=payload,
        origin=origin,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    return receipt


@router.get(
    "/{fire_id}/manifest",
    response_model=ViewerManifest,
    summary="Get the public viewer manifest",
    description=(
        "Returns the public ViewerManifest in snake_case. A matching If-None-Match returns "
        "304 without a body. This read endpoint never returns 409; conflicts are reserved for "
        "mutations."
    ),
    responses={
        200: {
            "description": "Current public ViewerManifest.",
            "headers": _MANIFEST_SUCCESS_HEADERS,
        },
        304: {
            "description": "Manifest unchanged; the response body is empty.",
            "headers": _MANIFEST_SUCCESS_HEADERS,
        },
        400: _manifest_problem_response("The fire_id path parameter has an invalid format."),
        404: _manifest_problem_response("No incident exists for this fire_id."),
        410: _manifest_problem_response("The incident has been tombstoned."),
        503: _manifest_problem_response("The incident data is temporarily unavailable."),
    },
)
def get_manifest(
    fire_id: FireIdDep,
    session: SessionDep,
    settings: SettingsDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ViewerManifest | Response:
    manifest = get_viewer_manifest(session, fire_id, settings)
    payload = manifest.model_dump(mode="json", exclude_none=False)
    etag = f'"{sha256_hex(payload)}"'
    headers = {
        "ETag": etag,
        "Cache-Control": _MANIFEST_CACHE_CONTROL,
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=jsonable_encoder(payload), headers=headers)
