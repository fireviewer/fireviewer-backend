"""Short-lived authorization for direct browser uploads to private Vercel Blob storage."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

import jwt

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.domain.errors import BadRequestError, ConflictError, ForbiddenError
from fire_viewer.domain.schemas import AdminBlobUploadGrantRequest
from fire_viewer.storage import build_object_store

BLOB_UPLOAD_GRANT_ISSUER = "fire-viewer-api"
BLOB_UPLOAD_GRANT_AUDIENCE = "fire-viewer-blob-upload"
ALLOWED_PACKAGE_CONTENT_TYPES = (
    "application/json",
    "application/vnd.fireviewer.terrain",
    "application/vnd.fireviewer.tile",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/geotiff",
    "model/gltf-binary",
)
ALLOWED_SOURCE_CONTENT_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/tiff",
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "audio/mpeg",
    "audio/mp4",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "text/plain",
    "text/markdown",
    "text/html",
)
ALLOWED_DAILY_SATELLITE_CONTENT_TYPES = (
    "application/json",
    "image/jpeg",
    "image/png",
    "image/tiff",
)
ALLOWED_GALLERY_CONTENT_TYPES = ("image/jpeg", "image/png")
INCIDENT_GALLERY_MAX_BYTES = 8 * 1_024 * 1_024
_ALLOWED_SUFFIXES = frozenset(
    {".json", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".glb", ".fwtile", ".fwterrain"}
)
_ALLOWED_SOURCE_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".tif",
        ".tiff",
        ".mp4",
        ".mov",
        ".webm",
        ".mp3",
        ".m4a",
        ".wav",
        ".ogg",
        ".txt",
        ".md",
        ".html",
        ".htm",
    }
)
_ALLOWED_GALLERY_SUFFIXES = frozenset({".jpg", ".png"})
_ALLOWED_PUBLIC_CONTRIBUTION_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_ALLOWED_DAILY_SATELLITE_SUFFIXES = frozenset(
    {".json", ".geojson", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
)


@dataclass(frozen=True, slots=True)
class BlobUploadGrant:
    upload_id: str
    pathname_prefix: str
    token: str
    expires_at: datetime


def _read_write_token(settings: Settings) -> str:
    if settings.object_storage_backend != "vercel_blob" or settings.blob_read_write_token is None:
        raise ConflictError(
            "blob_upload_unavailable",
            "Direct package uploads require the configured private Vercel Blob store.",
        )
    return settings.blob_read_write_token.get_secret_value()


def create_blob_upload_grant(
    *,
    payload: AdminBlobUploadGrantRequest,
    actor: Actor,
    settings: Settings,
) -> BlobUploadGrant:
    token = _read_write_token(settings)
    if payload.file_count > settings.zone_upload_max_files:
        raise BadRequestError("too_many_package_files", "The package declares too many files.")
    if payload.total_size_bytes > settings.zone_upload_max_unpacked_bytes:
        raise BadRequestError("package_too_large", "The package exceeds the configured size limit.")
    upload_id = uuid4().hex
    pathname_prefix = build_object_store(settings).pathname_for(f"packages/{upload_id}")
    now = utcnow()
    expires_at = now + timedelta(minutes=settings.blob_upload_grant_minutes)
    grant = jwt.encode(
        {
            "iss": BLOB_UPLOAD_GRANT_ISSUER,
            "aud": BLOB_UPLOAD_GRANT_AUDIENCE,
            "sub": actor.actor_id,
            "iat": now,
            "exp": expires_at,
            "upload_id": upload_id,
            "pathname_prefix": pathname_prefix,
            "package_id": payload.package_id,
            "file_count": payload.file_count,
            "total_size_bytes": payload.total_size_bytes,
            "purpose": "spatial_package",
        },
        token,
        algorithm="HS256",
    )
    return BlobUploadGrant(upload_id, pathname_prefix, grant, expires_at)


def create_source_blob_upload_grant(
    *,
    package_id: str,
    file_count: int,
    total_size_bytes: int,
    actor: Actor,
    settings: Settings,
    upload_id: str | None = None,
    purpose: str = "source_package",
) -> BlobUploadGrant:
    token = _read_write_token(settings)
    if file_count > settings.agent_source_package_max_files:
        raise BadRequestError("too_many_source_files", "The source package has too many files.")
    if total_size_bytes > settings.agent_source_package_max_total_bytes:
        raise BadRequestError("source_package_too_large", "The source package is too large.")
    upload_id = upload_id or uuid4().hex
    pathname_prefix = build_object_store(settings).pathname_for(f"source-packages/{upload_id}")
    now = utcnow()
    expires_at = now + timedelta(minutes=settings.blob_upload_grant_minutes)
    grant = jwt.encode(
        {
            "iss": BLOB_UPLOAD_GRANT_ISSUER,
            "aud": BLOB_UPLOAD_GRANT_AUDIENCE,
            "sub": actor.actor_id,
            "iat": now,
            "exp": expires_at,
            "upload_id": upload_id,
            "pathname_prefix": pathname_prefix,
            "package_id": package_id,
            "file_count": file_count,
            "total_size_bytes": total_size_bytes,
            "purpose": purpose,
        },
        token,
        algorithm="HS256",
    )
    return BlobUploadGrant(upload_id, pathname_prefix, grant, expires_at)


def create_gallery_blob_upload_grant(
    *,
    fire_id: str,
    zone_revision_id: str,
    size_bytes: int,
    actor: Actor,
    settings: Settings,
) -> BlobUploadGrant:
    token = _read_write_token(settings)
    if size_bytes <= 0 or size_bytes > INCIDENT_GALLERY_MAX_BYTES:
        raise BadRequestError("map_capture_too_large", "The map capture exceeds the size limit.")
    upload_id = uuid4().hex
    pathname_prefix = build_object_store(settings).pathname_for(f"gallery-captures/{upload_id}")
    now = utcnow()
    expires_at = now + timedelta(minutes=settings.blob_upload_grant_minutes)
    grant = jwt.encode(
        {
            "iss": BLOB_UPLOAD_GRANT_ISSUER,
            "aud": BLOB_UPLOAD_GRANT_AUDIENCE,
            "sub": actor.actor_id,
            "iat": now,
            "exp": expires_at,
            "upload_id": upload_id,
            "pathname_prefix": pathname_prefix,
            "package_id": zone_revision_id,
            "fire_id": fire_id,
            "file_count": 1,
            "total_size_bytes": size_bytes,
            "purpose": "incident_gallery",
        },
        token,
        algorithm="HS256",
    )
    return BlobUploadGrant(upload_id, pathname_prefix, grant, expires_at)


def _decode_upload_grant(grant: str, *, settings: Settings) -> dict[str, Any]:
    token = _read_write_token(settings)
    try:
        claims = jwt.decode(
            grant,
            token,
            algorithms=["HS256"],
            audience=BLOB_UPLOAD_GRANT_AUDIENCE,
            issuer=BLOB_UPLOAD_GRANT_ISSUER,
            options={"require": ["exp", "iat", "sub", "upload_id", "pathname_prefix"]},
        )
    except jwt.PyJWTError as exc:
        raise ForbiddenError("The Blob upload grant is invalid or expired.") from exc
    if not isinstance(claims, dict):
        raise ForbiddenError("The Blob upload grant is invalid.")
    return claims


def _safe_granted_path(pathname: str, *, prefix: str, allowed_suffixes: frozenset[str]) -> None:
    expected_prefix = f"{prefix}/"
    if not pathname.startswith(expected_prefix):
        raise ForbiddenError("The Blob pathname is outside the granted package prefix.")
    relative = pathname.removeprefix(expected_prefix)
    if (
        not relative
        or "\\" in relative
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
        or PurePosixPath(relative).suffix.casefold() not in allowed_suffixes
    ):
        raise BadRequestError("invalid_blob_pathname", "The Blob pathname is not allowed.")


def _store_id(read_write_token: str) -> str:
    parts = read_write_token.split("_")
    if len(parts) < 5 or parts[:3] != ["vercel", "blob", "rw"] or not parts[3]:
        raise ConflictError("invalid_blob_token", "The configured Vercel Blob token is invalid.")
    return parts[3]


def issue_blob_client_token(
    *,
    pathname: str,
    client_payload: str | None,
    upload_grant: str,
    settings: Settings,
) -> str:
    claims = _decode_upload_grant(upload_grant, settings=settings)
    prefix = claims.get("pathname_prefix")
    package_id = claims.get("package_id")
    if not isinstance(prefix, str) or not isinstance(package_id, str):
        raise ForbiddenError("The Blob upload grant is incomplete.")
    if client_payload is not None and not hmac.compare_digest(client_payload, package_id):
        raise ForbiddenError("The Blob upload payload does not match the granted package.")
    purpose = claims.get("purpose", "spatial_package")
    allowed_suffixes: frozenset[str]
    allowed_content_types: tuple[str, ...]
    maximum_size: int
    if purpose == "source_package":
        allowed_suffixes = _ALLOWED_SOURCE_SUFFIXES
        allowed_content_types = ALLOWED_SOURCE_CONTENT_TYPES
        maximum_size = settings.agent_source_package_max_file_bytes
    elif purpose == "admin_daily_satellite":
        allowed_suffixes = _ALLOWED_DAILY_SATELLITE_SUFFIXES
        allowed_content_types = ALLOWED_DAILY_SATELLITE_CONTENT_TYPES
        maximum_size = settings.agent_source_package_max_file_bytes
    elif purpose == "public_contribution":
        allowed_suffixes = _ALLOWED_PUBLIC_CONTRIBUTION_SUFFIXES
        allowed_content_types = ("image/jpeg", "image/png", "image/webp")
        maximum_size = settings.public_contribution_max_image_bytes
    elif purpose == "spatial_package":
        allowed_suffixes = _ALLOWED_SUFFIXES
        allowed_content_types = ALLOWED_PACKAGE_CONTENT_TYPES
        maximum_size = settings.zone_upload_max_bytes
    elif purpose == "incident_gallery":
        allowed_suffixes = _ALLOWED_GALLERY_SUFFIXES
        allowed_content_types = ALLOWED_GALLERY_CONTENT_TYPES
        maximum_size = INCIDENT_GALLERY_MAX_BYTES
    else:
        raise ForbiddenError("The Blob upload grant purpose is invalid.")
    _safe_granted_path(pathname, prefix=prefix, allowed_suffixes=allowed_suffixes)

    read_write_token = _read_write_token(settings)
    grant_exp_ms = int(float(claims["exp"]) * 1_000)
    requested_exp_ms = int(
        (utcnow() + timedelta(minutes=settings.blob_client_token_minutes)).timestamp() * 1_000
    )
    payload = {
        "pathname": pathname,
        "maximumSizeInBytes": maximum_size,
        "allowedContentTypes": list(allowed_content_types),
        "validUntil": min(grant_exp_ms, requested_exp_ms),
        "addRandomSuffix": False,
        "allowOverwrite": False,
        "cacheControlMaxAge": 31_536_000,
    }
    encoded_payload = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(
        read_write_token.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    secured = base64.b64encode(f"{signature}.{encoded_payload}".encode("ascii")).decode("ascii")
    return f"vercel_blob_client_{_store_id(read_write_token)}_{secured}"
