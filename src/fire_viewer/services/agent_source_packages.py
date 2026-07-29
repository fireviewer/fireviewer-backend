"""Normal private ingestion contract for user-provided incident sources."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, cast
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import jwt
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from tifffile import TiffFile, TiffFileError

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentAnalysisWindow,
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    AgentSourceCandidate,
    AgentSourcePackage,
    AgentSourcePackageItem,
    Episode,
    IncidentSeries,
)
from fire_viewer.domain.agent_schemas import (
    AgentDailyHotspotManifestItem,
    AgentDailySatelliteImageManifestItem,
    AgentDailySatelliteManifest,
    AgentDailySatellitePackageOpenRequest,
    AgentSourceFileDateMetadata,
    AgentSourcePackageDateGroupResponse,
    AgentSourcePackageItemResponse,
    AgentSourcePackageOpenRequest,
    AgentSourcePackageOpenResponse,
    AgentSourcePackageResponse,
)
from fire_viewer.domain.enums import (
    AgentAnalysisState,
    AgentBatchPriority,
    AgentBatchState,
    AgentBatchType,
    AgentConsentBasis,
    AgentConsentState,
    AgentMediaType,
    AgentSourcePackageKind,
    AgentSourcePackageState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.services.blob_uploads import (
    ALLOWED_DAILY_SATELLITE_CONTENT_TYPES,
    ALLOWED_SOURCE_CONTENT_TYPES,
    create_source_blob_upload_grant,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.storage import build_object_store
from fire_viewer.storage.object_store import ObjectMetadata, ObjectStorageError, ObjectStore

_MEDIA_JWT_ISSUER = "fire-viewer-api"
_MEDIA_JWT_AUDIENCE = "fire-viewer-agent-private-media"
_TERMS_VERSION = "firewarning-private-analysis-v1"
_ADMIN_SATELLITE_TERMS_VERSION = "firewarning-admin-satellite-v1"
_DAILY_SATELLITE_MANIFEST_NAME = "fireviewer-satellite-manifest.json"
_TIMEZONE = ZoneInfo("Europe/Paris")
_CANONICAL_BURNED_AREA_BANDS = (
    "BLUE",
    "GREEN",
    "RED",
    "NIR_NARROW",
    "SWIR_1",
    "SWIR_2",
)
_BAND_ORDER_TAG = re.compile(
    r'<Item name="FIREVIEWER_BAND_ORDER">([^<]{1,512})</Item>'
)
_ISO_DATE_IN_FILENAME = re.compile(r"(?<![0-9])([12][0-9]{3}-[0-9]{2}-[0-9]{2})(?![0-9])")
_SUFFIX_MEDIA: dict[str, tuple[AgentMediaType, str]] = {
    ".jpg": (AgentMediaType.IMAGE, "image/jpeg"),
    ".jpeg": (AgentMediaType.IMAGE, "image/jpeg"),
    ".png": (AgentMediaType.IMAGE, "image/png"),
    ".webp": (AgentMediaType.IMAGE, "image/webp"),
    ".tif": (AgentMediaType.IMAGE, "image/tiff"),
    ".tiff": (AgentMediaType.IMAGE, "image/tiff"),
    ".mp4": (AgentMediaType.VIDEO, "video/mp4"),
    ".mov": (AgentMediaType.VIDEO, "video/quicktime"),
    ".webm": (AgentMediaType.VIDEO, "video/webm"),
    ".mp3": (AgentMediaType.AUDIO, "audio/mpeg"),
    ".m4a": (AgentMediaType.AUDIO, "audio/mp4"),
    ".wav": (AgentMediaType.AUDIO, "audio/wav"),
    ".ogg": (AgentMediaType.AUDIO, "audio/ogg"),
    ".txt": (AgentMediaType.ARTICLE, "text/plain"),
    ".md": (AgentMediaType.ARTICLE, "text/markdown"),
    ".html": (AgentMediaType.ARTICLE, "text/html"),
    ".htm": (AgentMediaType.ARTICLE, "text/html"),
}


@dataclass(frozen=True, slots=True)
class PrivateMediaPayload:
    content: bytes
    content_type: str
    filename: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if normalized:
            self.parts.append(normalized)


def _incident_episode(session: Session, fire_id: str) -> tuple[IncidentSeries, Episode]:
    incident = session.execute(
        select(IncidentSeries).where(IncidentSeries.fire_id == fire_id)
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = session.execute(
        select(Episode).where(Episode.incident_id == incident.id, Episode.is_current.is_(True))
    ).scalar_one_or_none()
    if episode is None:
        raise ConflictError("incident_without_current_episode", "Incident has no current episode.")
    return incident, episode


def _daily_analysis_window(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    local_date: date,
) -> AgentAnalysisWindow | None:
    return session.execute(
        select(AgentAnalysisWindow).where(
            AgentAnalysisWindow.incident_id == incident.id,
            AgentAnalysisWindow.episode_id == episode.id,
            AgentAnalysisWindow.local_date == local_date,
        )
    ).scalar_one_or_none()


def ensure_daily_analysis_window(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    local_date: date,
) -> AgentAnalysisWindow:
    existing = _daily_analysis_window(
        session,
        incident=incident,
        episode=episode,
        local_date=local_date,
    )
    if existing is not None:
        if existing.state in {
            AgentAnalysisState.COMPLETED,
            AgentAnalysisState.CANCELLED,
        }:
            raise ConflictError(
                "agent_analysis_window_terminal",
                "A completed or cancelled analysis window cannot accept new sources.",
            )
        return existing
    start_local = datetime.combine(local_date, time.min, tzinfo=_TIMEZONE)
    end_local = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=_TIMEZONE)
    window = AgentAnalysisWindow(
        analysis_id=new_prefixed_id("AN"),
        incident_id=incident.id,
        episode_id=episode.id,
        window_start_at=start_local.astimezone(UTC),
        window_end_at=end_local.astimezone(UTC),
        local_date=local_date,
        timezone=str(_TIMEZONE),
        state=AgentAnalysisState.COLLECTING,
        version=1,
    )
    session.add(window)
    session.flush()
    return window


def _load_package(session: Session, package_id: str) -> AgentSourcePackage:
    package = session.execute(
        select(AgentSourcePackage)
        .where(AgentSourcePackage.package_id == package_id)
        .options(
            selectinload(AgentSourcePackage.incident),
            selectinload(AgentSourcePackage.episode),
            selectinload(AgentSourcePackage.analysis_window),
            selectinload(AgentSourcePackage.public_contribution),
            selectinload(AgentSourcePackage.items).selectinload(
                AgentSourcePackageItem.analysis_window
            ),
            selectinload(AgentSourcePackage.items)
            .selectinload(AgentSourcePackageItem.agent_media_item)
            .selectinload(AgentMediaItem.batch),
        )
    ).scalar_one_or_none()
    if package is None:
        raise NotFoundError("agent_source_package", package_id)
    return package


def _package_response(package: AgentSourcePackage) -> AgentSourcePackageResponse:
    batch_ids = sorted(
        {
            item.agent_media_item.batch.batch_id
            for item in package.items
            if item.agent_media_item is not None
        }
    )
    date_groups: list[AgentSourcePackageDateGroupResponse] = []
    grouped_items: dict[int, list[AgentSourcePackageItem]] = {}
    for item in package.items:
        if item.analysis_window_id is not None:
            grouped_items.setdefault(item.analysis_window_id, []).append(item)
    for _window_id, items in sorted(
        grouped_items.items(),
        key=lambda entry: (
            entry[1][0].analysis_window.local_date
            if entry[1][0].analysis_window is not None
            else date.max
        ),
    ):
        window = items[0].analysis_window
        if window is None:
            continue
        group_batch_ids = sorted(
            {
                item.agent_media_item.batch.batch_id
                for item in items
                if item.agent_media_item is not None
            }
        )
        date_groups.append(
            AgentSourcePackageDateGroupResponse(
                local_date=window.local_date,
                analysis_window_id=window.analysis_id,
                item_count=len(items),
                batch_ids=group_batch_ids,
            )
        )
    classified_item_count = sum(
        item.date_classification == "CLASSIFIED" for item in package.items
    )
    to_classify_item_count = sum(
        item.date_classification == "TO_CLASSIFY" for item in package.items
    )
    return AgentSourcePackageResponse(
        package_id=package.package_id,
        package_kind=package.package_kind,
        fire_id=package.incident.fire_id if package.incident is not None else None,
        episode_id=package.episode.episode_id if package.episode is not None else None,
        state=package.state,
        known_start_date=package.known_start_date,
        known_end_date=package.known_end_date,
        location_hint=package.location_hint,
        analysis_authorized=package.analysis_authorized,
        publication_authorized=package.publication_authorized,
        purge_after=as_utc(package.purge_after),
        finalized_at=as_utc(package.finalized_at) if package.finalized_at else None,
        classified_item_count=classified_item_count,
        to_classify_item_count=to_classify_item_count,
        analysis_window_count=len(date_groups),
        date_groups=date_groups,
        batch_ids=batch_ids,
        items=[
            AgentSourcePackageItemResponse(
                item_id=item.item_id,
                original_filename=item.original_filename,
                content_type=item.content_type,
                media_type=item.media_type,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                captured_at=as_utc(item.captured_at) if item.captured_at else None,
                date_classification=item.date_classification,
                date_evidence=item.date_evidence,
                classified_local_date=item.classified_local_date,
                analysis_window_id=(
                    item.analysis_window.analysis_id
                    if item.analysis_window is not None
                    else None
                ),
                batch_id=(item.agent_media_item.batch.batch_id if item.agent_media_item else None),
                input_id=item.agent_media_item.input_id if item.agent_media_item else None,
            )
            for item in package.items
        ],
    )


def _resumable_package_filenames(
    package: AgentSourcePackage,
    *,
    settings: Settings,
) -> list[str]:
    if package.state != AgentSourcePackageState.OPEN:
        raise ConflictError(
            "source_package_already_finalized",
            "A finalized source package cannot issue another upload grant.",
        )
    try:
        inventory = build_object_store(settings).list_prefix(
            f"source-packages/{package.upload_id}",
            limit=package.declared_file_count + 1,
        )
    except ObjectStorageError as exc:
        raise ConflictError(
            "source_package_inventory_unavailable",
            "The private upload cannot be inspected before resuming it.",
        ) from exc
    if (
        len(inventory) > package.declared_file_count
        or sum(item.size_bytes for item in inventory) > package.declared_total_size_bytes
    ):
        raise ConflictError(
            "source_package_inventory_invalid",
            "The partial private upload exceeds its opened transfer contract.",
        )
    filenames = sorted(PurePosixPath(item.pathname).name for item in inventory)
    if len(filenames) != len(set(filenames)):
        raise ConflictError(
            "source_package_inventory_ambiguous",
            "The partial private upload contains duplicate basenames.",
        )
    return filenames


def open_source_package(
    session: Session,
    *,
    fire_id: str,
    payload: AgentSourcePackageOpenRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AgentSourcePackageOpenResponse:
    if settings.object_storage_backend != "vercel_blob":
        raise ConflictError(
            "source_upload_unavailable",
            "Private browser source uploads require the configured Vercel Blob store.",
        )
    if payload.file_count > settings.agent_source_package_max_files:
        raise BadRequestError("too_many_source_files", "The source package has too many files.")
    if payload.total_size_bytes > settings.agent_source_package_max_total_bytes:
        raise BadRequestError("source_package_too_large", "The source package is too large.")
    incident, episode = _incident_episode(session, fire_id)
    request_hash = sha256_hex(payload)
    existing = session.execute(
        select(AgentSourcePackage).where(AgentSourcePackage.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.incident_id != incident.id
            or existing.episode_id != episode.id
            or existing.request_hash != request_hash
        ):
            raise ConflictError(
                "source_package_idempotency_conflict",
                "The idempotency key was already used for another source package.",
            )
        already_uploaded_filenames = _resumable_package_filenames(
            existing,
            settings=settings,
        )
        grant = create_source_blob_upload_grant(
            package_id=existing.package_id,
            file_count=existing.declared_file_count,
            total_size_bytes=existing.declared_total_size_bytes,
            actor=actor,
            settings=settings,
            upload_id=existing.upload_id,
        )
        return AgentSourcePackageOpenResponse(
            package_id=existing.package_id,
            upload_id=grant.upload_id,
            pathname_prefix=grant.pathname_prefix,
            upload_grant=grant.token,
            expires_at=grant.expires_at,
            maximum_file_size_bytes=settings.agent_source_package_max_file_bytes,
            allowed_content_types=list(ALLOWED_SOURCE_CONTENT_TYPES),
            already_uploaded_filenames=already_uploaded_filenames,
        )

    package_id = new_prefixed_id("SP")
    grant = create_source_blob_upload_grant(
        package_id=package_id,
        file_count=payload.file_count,
        total_size_bytes=payload.total_size_bytes,
        actor=actor,
        settings=settings,
    )
    now = utcnow()
    end_date = None
    if payload.known_start_date is not None:
        end_date = payload.known_end_date or payload.known_start_date
    package = AgentSourcePackage(
        package_id=package_id,
        incident_id=incident.id,
        episode_id=episode.id,
        analysis_window_id=None,
        package_kind=AgentSourcePackageKind.ADMIN_SOURCES,
        state=AgentSourcePackageState.OPEN,
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        declared_file_count=payload.file_count,
        declared_total_size_bytes=payload.total_size_bytes,
        known_start_date=payload.known_start_date,
        known_end_date=end_date,
        location_hint=payload.location_hint,
        file_date_metadata=[
            item.model_dump(mode="json") for item in payload.file_date_metadata
        ],
        analysis_authorized=True,
        publication_authorized=False,
        terms_version=_TERMS_VERSION,
        consent_evidence_sha256=hashlib.sha256(
            f"{actor.actor_id}\0{package_id}\0{request_hash}\0private-analysis".encode()
        ).hexdigest(),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        trace_id=trace_id,
        purge_after=now + timedelta(days=settings.agent_source_package_retention_days),
    )
    session.add(package)
    record_operator_audit(
        session,
        actor=actor,
        action="agent.source_package_opened",
        target_type="agent_source_package",
        target_id=package_id,
        reason="Private user source transfer opened for analysis only.",
        trace_id=trace_id,
        after={
            "fire_id": fire_id,
            "file_count": payload.file_count,
            "explicitly_dated_files": len(payload.file_date_metadata),
            "publication_authorized": False,
        },
    )
    session.commit()
    return AgentSourcePackageOpenResponse(
        package_id=package_id,
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        upload_grant=grant.token,
        expires_at=grant.expires_at,
        maximum_file_size_bytes=settings.agent_source_package_max_file_bytes,
        allowed_content_types=list(ALLOWED_SOURCE_CONTENT_TYPES),
    )


def open_daily_satellite_package(
    session: Session,
    *,
    fire_id: str,
    payload: AgentDailySatellitePackageOpenRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AgentSourcePackageOpenResponse:
    from fire_viewer.services.agent_validation_campaigns import (
        resolve_requested_analysis_window,
    )

    if settings.object_storage_backend != "vercel_blob":
        raise ConflictError(
            "source_upload_unavailable",
            "Private browser source uploads require the configured Vercel Blob store.",
        )
    if payload.file_count > settings.agent_source_package_max_files:
        raise BadRequestError("too_many_source_files", "The source package has too many files.")
    if payload.total_size_bytes > settings.agent_source_package_max_total_bytes:
        raise BadRequestError("source_package_too_large", "The source package is too large.")
    incident, episode = _incident_episode(session, fire_id)
    active = resolve_requested_analysis_window(
        session,
        incident=incident,
        episode=episode,
        expected_analysis_window_id=payload.expected_analysis_window_id,
    )
    day = active.campaign_day
    if day is not None:
        required = set(day.required_operations)
        if "satellite_media" not in required:
            reason = (
                "This operation is explicitly absent from the active analysis window."
                if "satellite_media" in set(day.declared_absences)
                else "This operation is not scheduled in the active analysis window."
            )
            raise ConflictError("agent_satellite_input_not_scheduled", reason)

    request_hash = sha256_hex(payload)
    existing = session.scalar(
        select(AgentSourcePackage).where(
            AgentSourcePackage.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.incident_id != incident.id
            or existing.episode_id != episode.id
            or existing.analysis_window_id != active.window.id
            or existing.package_kind != AgentSourcePackageKind.ADMIN_SATELLITE
            or existing.request_hash != request_hash
        ):
            raise ConflictError(
                "source_package_idempotency_conflict",
                "The idempotency key was already used for another source package.",
            )
        already_uploaded_filenames = _resumable_package_filenames(
            existing,
            settings=settings,
        )
        grant = create_source_blob_upload_grant(
            package_id=existing.package_id,
            file_count=existing.declared_file_count,
            total_size_bytes=existing.declared_total_size_bytes,
            actor=actor,
            settings=settings,
            upload_id=existing.upload_id,
            purpose="admin_daily_satellite",
        )
        return AgentSourcePackageOpenResponse(
            package_id=existing.package_id,
            upload_id=grant.upload_id,
            pathname_prefix=grant.pathname_prefix,
            upload_grant=grant.token,
            expires_at=grant.expires_at,
            maximum_file_size_bytes=settings.agent_source_package_max_file_bytes,
            allowed_content_types=list(ALLOWED_DAILY_SATELLITE_CONTENT_TYPES),
            already_uploaded_filenames=already_uploaded_filenames,
        )

    package_id = new_prefixed_id("SP")
    grant = create_source_blob_upload_grant(
        package_id=package_id,
        file_count=payload.file_count,
        total_size_bytes=payload.total_size_bytes,
        actor=actor,
        settings=settings,
        purpose="admin_daily_satellite",
    )
    now = utcnow()
    package = AgentSourcePackage(
        package_id=package_id,
        incident_id=incident.id,
        episode_id=episode.id,
        analysis_window_id=active.window.id,
        package_kind=AgentSourcePackageKind.ADMIN_SATELLITE,
        state=AgentSourcePackageState.OPEN,
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        declared_file_count=payload.file_count,
        declared_total_size_bytes=payload.total_size_bytes,
        known_start_date=active.window.local_date,
        known_end_date=active.window.local_date,
        location_hint=None,
        analysis_authorized=True,
        publication_authorized=False,
        terms_version=_ADMIN_SATELLITE_TERMS_VERSION,
        consent_evidence_sha256=hashlib.sha256(
            f"{actor.actor_id}\0{package_id}\0{request_hash}\0admin-satellite".encode()
        ).hexdigest(),
        consent_scopes=["temporary_storage", "agent_analysis", "human_review"],
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        trace_id=trace_id,
        purge_after=now + timedelta(days=settings.agent_source_package_retention_days),
    )
    session.add(package)
    record_operator_audit(
        session,
        actor=actor,
        action="agent.daily_satellite_package_opened",
        target_type="agent_source_package",
        target_id=package_id,
        reason="Daily institutional satellite transfer opened for the active window.",
        trace_id=trace_id,
        after={
            "fire_id": fire_id,
            "analysis_window_id": active.window.analysis_id,
            "file_count": payload.file_count,
            "publication_authorized": False,
        },
    )
    session.commit()
    return AgentSourcePackageOpenResponse(
        package_id=package_id,
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        upload_grant=grant.token,
        expires_at=grant.expires_at,
        maximum_file_size_bytes=settings.agent_source_package_max_file_bytes,
        allowed_content_types=list(ALLOWED_DAILY_SATELLITE_CONTENT_TYPES),
    )


def _validate_signature(content: bytes, suffix: str) -> None:
    if suffix in {".jpg", ".jpeg"} and not content.startswith(b"\xff\xd8\xff"):
        raise BadRequestError("source_media_type_mismatch", "A JPEG file has invalid bytes.")
    if suffix == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BadRequestError("source_media_type_mismatch", "A PNG file has invalid bytes.")
    if suffix == ".webp" and not (content.startswith(b"RIFF") and content[8:12] == b"WEBP"):
        raise BadRequestError("source_media_type_mismatch", "A WebP file has invalid bytes.")
    if suffix in {".tif", ".tiff"} and content[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise BadRequestError("source_media_type_mismatch", "A TIFF file has invalid bytes.")
    if suffix in {".mp4", ".mov", ".m4a"} and content[4:8] != b"ftyp":
        raise BadRequestError("source_media_type_mismatch", "An ISO media file is invalid.")
    if suffix == ".webm" and not content.startswith(b"\x1aE\xdf\xa3"):
        raise BadRequestError("source_media_type_mismatch", "A WebM file is invalid.")
    if suffix == ".wav" and not (content.startswith(b"RIFF") and content[8:12] == b"WAVE"):
        raise BadRequestError("source_media_type_mismatch", "A WAV file is invalid.")
    if suffix == ".ogg" and not content.startswith(b"OggS"):
        raise BadRequestError("source_media_type_mismatch", "An Ogg file is invalid.")
    if suffix == ".mp3" and not (
        content.startswith(b"ID3") or (len(content) > 1 and content[0] == 0xFF)
    ):
        raise BadRequestError("source_media_type_mismatch", "An MP3 file is invalid.")
    if suffix in {".txt", ".md", ".html", ".htm"} and b"\x00" in content[:16_384]:
        raise BadRequestError("source_media_type_mismatch", "A text file contains binary data.")


def _image_metadata(content: bytes) -> tuple[datetime | None, dict[str, object]]:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            metadata: dict[str, object] = {
                "image_width_px": image.width,
                "image_height_px": image.height,
                "image_format": image.format,
            }
            captured_at = None
            exif = image.getexif()
            raw_original_date = exif.get(36867)
            raw_general_date = exif.get(306)
            raw_date = raw_original_date or raw_general_date
            if isinstance(raw_date, str):
                try:
                    captured_at = datetime.strptime(raw_date, "%Y:%m:%d %H:%M:%S").replace(
                        tzinfo=_TIMEZONE
                    )
                    metadata["date_evidence"] = (
                        "EXIF_DATETIME_ORIGINAL"
                        if raw_original_date
                        else "EXIF_DATETIME"
                    )
                except ValueError:
                    metadata["unparsed_capture_date"] = raw_date[:128]
            return captured_at, metadata
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise BadRequestError(
            "source_image_invalid", "An uploaded image cannot be decoded safely."
        ) from exc


def _geotiff_metadata(
    content: bytes,
    *,
    declared_bands: list[str],
    declared_bbox_wgs84: tuple[float, float, float, float] | None,
) -> dict[str, object]:
    try:
        with TiffFile(BytesIO(content)) as tiff:
            if len(tiff.pages) != 1:
                raise ValueError("GeoTIFF must expose exactly one raster image")
            page = cast(Any, tiff.pages[0])
            width = int(page.imagewidth)
            height = int(page.imagelength)
            samples_per_pixel = int(page.samplesperpixel)
            pixel_scale_tag = page.tags.get("ModelPixelScaleTag")
            tiepoint_tag = page.tags.get("ModelTiepointTag")
            geo_key_tag = page.tags.get("GeoKeyDirectoryTag")
            metadata_tag = page.tags.get("GDAL_METADATA")
            if (
                width <= 0
                or height <= 0
                or samples_per_pixel != len(declared_bands)
                or pixel_scale_tag is None
                or tiepoint_tag is None
                or geo_key_tag is None
            ):
                raise ValueError("GeoTIFF dimensions, bands or georeferencing are invalid")
            pixel_scale = tuple(float(value) for value in pixel_scale_tag.value)
            tiepoint = tuple(float(value) for value in tiepoint_tag.value)
            geo_keys = tuple(int(value) for value in geo_key_tag.value)
            if (
                len(pixel_scale) < 2
                or len(tiepoint) < 6
                or pixel_scale[0] <= 0
                or pixel_scale[1] <= 0
                or len(geo_keys) < 4
                or (len(geo_keys) - 4) % 4 != 0
            ):
                raise ValueError("GeoTIFF georeferencing tags are invalid")
            epsg = None
            for offset in range(4, len(geo_keys), 4):
                key_id, location, count, value = geo_keys[offset : offset + 4]
                if key_id == 2048 and location == 0 and count == 1:
                    epsg = value
                    break
            if epsg != 4326:
                raise ValueError("GeoTIFF must declare EPSG:4326")

            scale_x, scale_y = pixel_scale[:2]
            origin_x = tiepoint[3] - tiepoint[0] * scale_x
            origin_y = tiepoint[4] + tiepoint[1] * scale_y
            actual_bbox = (
                origin_x,
                origin_y - height * scale_y,
                origin_x + width * scale_x,
                origin_y,
            )
            if declared_bbox_wgs84 is not None:
                tolerance = max(scale_x, scale_y) * 1.1
                if any(
                    abs(actual - declared) > tolerance
                    for actual, declared in zip(
                        actual_bbox,
                        declared_bbox_wgs84,
                        strict=True,
                    )
                ):
                    raise ValueError("GeoTIFF georeferencing differs from the declared bbox")

            if tuple(declared_bands) == _CANONICAL_BURNED_AREA_BANDS:
                raw_metadata = "" if metadata_tag is None else str(metadata_tag.value)
                match = _BAND_ORDER_TAG.search(raw_metadata[:16_384])
                if match is None or tuple(match.group(1).split(",")) != (
                    _CANONICAL_BURNED_AREA_BANDS
                ):
                    raise ValueError("GeoTIFF canonical band order is not embedded")

            return {
                "image_width_px": width,
                "image_height_px": height,
                "image_format": "TIFF",
                "samples_per_pixel": samples_per_pixel,
                "crs": "EPSG:4326",
                "geotransform": [
                    origin_x,
                    scale_x,
                    0.0,
                    origin_y,
                    0.0,
                    -scale_y,
                ],
                "bbox_wgs84": list(actual_bbox),
            }
    except (TiffFileError, OSError, TypeError, ValueError) as exc:
        raise BadRequestError(
            "daily_satellite_geotiff_invalid",
            "The uploaded GeoTIFF does not match its signed geospatial contract.",
        ) from exc


def _tiff_samples_per_pixel(content: bytes) -> int:
    try:
        with TiffFile(BytesIO(content)) as tiff:
            if len(tiff.pages) != 1:
                return 0
            return int(cast(Any, tiff.pages[0]).samplesperpixel)
    except (TiffFileError, OSError, TypeError, ValueError):
        return 0


def _article_text(content: bytes, content_type: str) -> str:
    text = content.decode("utf-8", errors="replace")
    if content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        text = "\n".join(parser.parts)
    return text[:100_000]


_EXPLICIT_DATE_EVIDENCE = {
    "captured_at": "EXPLICIT_CAPTURED_AT",
    "observed_at": "EXPLICIT_OBSERVED_AT",
    "published_at": "EXPLICIT_PUBLISHED_AT",
    "acquired_at": "EXPLICIT_ACQUIRED_AT",
}


def _file_date_metadata(
    package: AgentSourcePackage,
) -> dict[str, AgentSourceFileDateMetadata]:
    declarations: dict[str, AgentSourceFileDateMetadata] = {}
    for raw in package.file_date_metadata:
        try:
            declaration = AgentSourceFileDateMetadata.model_validate(raw)
        except ValidationError as exc:
            raise ConflictError(
                "source_file_date_metadata_invalid",
                "Persisted source file date metadata is invalid.",
            ) from exc
        declarations[declaration.filename.casefold()] = declaration
    return declarations


def _resolve_admin_item_date(
    *,
    package: AgentSourcePackage,
    package_item: AgentSourcePackageItem,
    explicit: AgentSourceFileDateMetadata | None,
) -> None:
    exif_at = package_item.captured_at
    exif_evidence = package_item.metadata_payload.get("date_evidence")
    filename_dates: list[date] = []
    for raw_date in _ISO_DATE_IN_FILENAME.findall(package_item.original_filename):
        try:
            filename_dates.append(date.fromisoformat(raw_date))
        except ValueError:
            continue
    filename_dates = sorted(set(filename_dates))
    evidence_dates: dict[str, date] = {}
    explicit_at = as_utc(explicit.effective_at) if explicit is not None else None
    explicit_evidence = (
        _EXPLICIT_DATE_EVIDENCE[explicit.basis] if explicit is not None else None
    )
    if explicit_at is not None and explicit_evidence is not None:
        evidence_dates[explicit_evidence] = explicit_at.astimezone(_TIMEZONE).date()
    if exif_at is not None and isinstance(exif_evidence, str):
        evidence_dates[exif_evidence] = as_utc(exif_at).astimezone(_TIMEZONE).date()
    if len(filename_dates) == 1:
        evidence_dates["FILENAME_ISO_DATE"] = filename_dates[0]
    elif len(filename_dates) > 1:
        evidence_dates["FILENAME_ISO_DATE"] = filename_dates[0]
        package_item.metadata_payload["ambiguous_filename_dates"] = [
            value.isoformat() for value in filename_dates
        ]
    if (
        not evidence_dates
        and package.known_start_date is not None
        and package.known_start_date == package.known_end_date
    ):
        evidence_dates["PACKAGE_SINGLE_DATE"] = package.known_start_date

    distinct_dates = set(evidence_dates.values())
    if len(distinct_dates) != 1 or len(filename_dates) > 1:
        package_item.captured_at = None
        package_item.date_evidence = (
            "CONFLICTING_METADATA" if evidence_dates else None
        )
        package_item.metadata_payload["date_evidence"] = package_item.date_evidence
        if evidence_dates:
            package_item.metadata_payload["date_conflict"] = {
                key: value.isoformat() for key, value in evidence_dates.items()
            }
        package_item.metadata_payload["date_classification"] = "TO_CLASSIFY"
        return

    classified_date = next(iter(distinct_dates))
    if explicit_at is not None and explicit_evidence is not None:
        package_item.captured_at = (
            explicit_at if explicit is not None and explicit.basis == "captured_at" else None
        )
        package_item.date_evidence = explicit_evidence
        package_item.metadata_payload["explicit_effective_at"] = explicit_at.isoformat()
    elif exif_at is not None and isinstance(exif_evidence, str):
        package_item.date_evidence = exif_evidence
    else:
        package_item.captured_at = None
        package_item.date_evidence = next(iter(evidence_dates))
    package_item.metadata_payload["date_evidence"] = package_item.date_evidence
    package_item.metadata_payload["classified_local_date_candidate"] = (
        classified_date.isoformat()
    )


def create_private_media_url(
    *,
    source_kind: str,
    source_id: str,
    item_id: str,
    purge_after: datetime,
    settings: Settings,
) -> str:
    now = utcnow()
    token = jwt.encode(
        {
            "iss": _MEDIA_JWT_ISSUER,
            "aud": _MEDIA_JWT_AUDIENCE,
            "sub": item_id,
            "source_kind": source_kind,
            "source_id": source_id,
            "iat": now,
            "exp": as_utc(purge_after),
        },
        settings.agent_media_signing_secret.get_secret_value(),
        algorithm="HS256",
    )
    base = str(settings.agent_media_proxy_base_url).rstrip("/")
    return f"{base}/api/v2/private-agent-media/{quote(item_id, safe='')}?token={quote(token)}"


def _create_media_batches(
    session: Session,
    *,
    package: AgentSourcePackage,
    unique_items: list[tuple[AgentSourcePackageItem, bytes]],
    settings: Settings,
) -> None:
    declared_observation: dict[str, object] | None = None
    if package.public_contribution is not None:
        submission = package.public_contribution.submission_payload
        observation = submission["observation"]
        location = submission["location"]
        media = submission.get("media") or {}
        declared_observation = {
            "observed_at": observation["observed_at"],
            "observation_type": observation["observation_type"],
            "direct_observation": observation["direct_observation"],
            "description": observation["description"],
            "location_mode": location["mode"],
            "location_label": location.get("label"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "uncertainty_m": location.get("uncertainty_m"),
            "media_captured_at": media.get("captured_at"),
            "media_direction": media.get("direction"),
        }
    by_date_and_type: dict[
        tuple[date, AgentBatchType],
        list[tuple[AgentSourcePackageItem, bytes]],
    ] = {}
    classified_dates: set[date] = set()
    for item, content in unique_items:
        if item.date_evidence == "CONFLICTING_METADATA":
            continue
        captured_at = item.captured_at
        candidate_local_date = item.metadata_payload.get(
            "classified_local_date_candidate"
        )
        if declared_observation is not None:
            declared_capture = declared_observation.get("media_captured_at")
            public_at = datetime.fromisoformat(
                str(declared_capture or declared_observation["observed_at"]).replace(
                    "Z", "+00:00"
                )
            )
            if captured_at is not None and (
                as_utc(captured_at).astimezone(_TIMEZONE).date()
                != as_utc(public_at).astimezone(_TIMEZONE).date()
            ):
                item.metadata_payload["exif_date_conflicts_with_public_submission"] = True
            captured_at = public_at
            item.captured_at = public_at
            item.date_evidence = "PUBLIC_OBSERVED_AT"
            item.metadata_payload["date_evidence"] = item.date_evidence
        if captured_at is not None:
            item_date = as_utc(captured_at).astimezone(_TIMEZONE).date()
        elif isinstance(candidate_local_date, str):
            item_date = date.fromisoformat(candidate_local_date)
        else:
            item.metadata_payload["date_classification"] = "TO_CLASSIFY"
            continue
        if (
            package.known_start_date is not None
            and package.known_end_date is not None
            and not package.known_start_date <= item_date <= package.known_end_date
        ):
            item.metadata_payload["capture_date_outside_declared_period"] = True
        if item.metadata_payload.get("admin_satellite_six_band") is True:
            item.metadata_payload["satellite_manifest_required"] = True
            item.metadata_payload["date_classification"] = "TO_CLASSIFY"
            item.date_classification = "TO_CLASSIFY"
            item.date_evidence = None
            item.classified_local_date = None
            continue
        batch_type = AgentBatchType.USER_MEDIA
        by_date_and_type.setdefault((item_date, batch_type), []).append((item, content))
        classified_dates.add(item_date)

    windows_by_date: dict[date, AgentAnalysisWindow | None] = {}
    for local_date in sorted(classified_dates):
        window = None
        if package.incident is not None and package.episode is not None:
            existing = _daily_analysis_window(
                session,
                incident=package.incident,
                episode=package.episode,
                local_date=local_date,
            )
            if (
                package.public_contribution is not None
                and existing is not None
                and existing.state
                in {
                    AgentAnalysisState.COMPLETED,
                    AgentAnalysisState.CANCELLED,
                }
            ):
                # Public evidence follows its own moderation contract. Receiving a
                # historical contribution must not reopen or mutate a terminal
                # agentic analysis window, so keep this batch unlinked (schema 1.0)
                # while preserving the classified observation date for review.
                window = None
            else:
                window = ensure_daily_analysis_window(
                    session,
                    incident=package.incident,
                    episode=package.episode,
                    local_date=local_date,
                )
        windows_by_date[local_date] = window

    for (local_date, batch_type), dated_items in sorted(
        by_date_and_type.items(),
        key=lambda entry: (entry[0][0], entry[0][1].value),
    ):
        window = windows_by_date[local_date]
        for package_item, _content in dated_items:
            package_item.date_classification = "CLASSIFIED"
            package_item.classified_local_date = local_date
            package_item.analysis_window_id = window.id if window is not None else None
            package_item.metadata_payload["date_classification"] = "CLASSIFIED"
            package_item.metadata_payload["classified_local_date"] = local_date.isoformat()
            if package.public_contribution is not None and window is None:
                package_item.metadata_payload["terminal_analysis_window_unlinked"] = True
        for offset in range(0, len(dated_items), 32):
            chunk = dated_items[offset : offset + 32]
            batch_id = new_prefixed_id("AB")
            batch = AgentMediaBatch(
                batch_id=batch_id,
                schema_version="2.0" if window is not None else "1.0",
                batch_type=batch_type,
                priority=AgentBatchPriority.SCHEDULED_COMBINED,
                state=AgentBatchState.DRAFT,
                incident_id=package.incident_id,
                episode_id=package.episode_id,
                analysis_window_id=window.id if window is not None else None,
                reference_bundle_payload=None,
                idempotency_key=(
                    f"source-package:{package.package_id}:{local_date}:"
                    f"{batch_type.value}:{offset // 32}"
                ),
                request_hash=hashlib.sha256(
                    "\n".join(item.sha256 for item, _content in chunk).encode()
                ).hexdigest(),
                trace_id=package.trace_id,
                deadline_at=None,
                purge_after=package.purge_after,
            )
            session.add(batch)
            for package_item, content in chunk:
                captured_at_value: str | None = None
                if declared_observation is not None:
                    declared_capture = declared_observation.get("media_captured_at")
                    if isinstance(declared_capture, str):
                        captured_at_value = declared_capture
                if captured_at_value is None and package_item.captured_at is not None:
                    captured_at_value = as_utc(package_item.captured_at).isoformat()
                if captured_at_value is None and declared_observation is not None:
                    observed_at = declared_observation.get("observed_at")
                    if isinstance(observed_at, str):
                        captured_at_value = observed_at
                proxy_url = create_private_media_url(
                    source_kind="source_package",
                    source_id=package.package_id,
                    item_id=package_item.item_id,
                    purge_after=package.purge_after,
                    settings=settings,
                )
                media_url = (
                    proxy_url
                    if package_item.media_type
                    in {
                        AgentMediaType.IMAGE,
                        AgentMediaType.VIDEO,
                    }
                    else None
                )
                processable: dict[str, object] = {
                    "frames": [],
                    "audio_url": (
                        proxy_url
                        if package_item.media_type == AgentMediaType.AUDIO
                        else None
                    ),
                    "article_text": (
                        _article_text(content, package_item.content_type)
                        if package_item.media_type == AgentMediaType.ARTICLE
                        else None
                    ),
                }
                media_item = AgentMediaItem(
                    input_id=package_item.item_id,
                    media_type=package_item.media_type,
                    working_file_url=media_url,
                    media_sha256=package_item.sha256,
                    size_bytes=package_item.size_bytes,
                    metadata_payload={
                        "provenance": {
                            "source_key": package.package_id,
                            "source_reference_url": None,
                            "license_identifier": "USER_PRIVATE_ANALYSIS",
                            "attribution": package.location_hint,
                            "trust": "unverified",
                            "declared_observation": declared_observation,
                        },
                        "captured_at": captured_at_value,
                        "camera": None,
                        "satellite": None,
                        "private_source_package": {
                            "package_id": package.package_id,
                            "item_id": package_item.item_id,
                            "object_uri": package_item.object_uri,
                        },
                        **package_item.metadata_payload,
                    },
                    processable_payload=processable,
                    preprocessing_status="validated",
                    purge_after=package.purge_after,
                )
                media_item.consent = AgentMediaConsent(
                    basis=AgentConsentBasis.EXPLICIT_UPLOAD,
                    state=AgentConsentState.GRANTED,
                    scopes=list(package.consent_scopes),
                    terms_version=package.terms_version,
                    evidence_sha256=package.consent_evidence_sha256,
                    subject_reference_hash=package.subject_reference_hash,
                    source_reference_url=None,
                    license_identifier=None,
                    granted_at=package.created_at,
                    expires_at=package.purge_after,
                )
                batch.items.append(media_item)
                session.flush()
                package_item.agent_media_item_id = media_item.id

    if len(classified_dates) == 1 and package.incident is not None:
        only_date = next(iter(classified_dates))
        window = windows_by_date.get(only_date)
        if window is not None:
            package.analysis_window_id = window.id
    elif len(classified_dates) > 1:
        package.analysis_window_id = None

    ordered_dates = sorted(classified_dates)
    if ordered_dates and package.known_start_date is None:
        package.known_start_date = ordered_dates[0]
        package.known_end_date = ordered_dates[-1]


def _daily_satellite_inventory(
    package: AgentSourcePackage,
    settings: Settings,
) -> tuple[ObjectStore, list[ObjectMetadata]]:
    store = build_object_store(settings)
    key = f"source-packages/{package.upload_id}"
    try:
        inventory = store.list_prefix(key, limit=package.declared_file_count + 1)
    except ObjectStorageError as exc:
        raise ConflictError(
            "source_package_inventory_unavailable", "The private upload cannot be inspected."
        ) from exc
    if len(inventory) != package.declared_file_count:
        raise ConflictError(
            "source_package_inventory_incomplete",
            "The uploaded file count does not match the opened transfer.",
        )
    if sum(item.size_bytes for item in inventory) != package.declared_total_size_bytes:
        raise ConflictError(
            "source_package_size_mismatch",
            "The uploaded byte count does not match the opened transfer.",
        )
    return store, inventory


def _parse_daily_satellite_manifest(
    *,
    content: bytes,
    expected_analysis_window_id: str,
) -> AgentDailySatelliteManifest:
    try:
        raw = json.loads(content)
        manifest = AgentDailySatelliteManifest.model_validate(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise BadRequestError(
            "daily_satellite_manifest_invalid",
            "The daily satellite manifest is invalid.",
        ) from exc
    if manifest.expected_analysis_window_id != expected_analysis_window_id:
        raise ConflictError(
            "agent_analysis_window_stale",
            "The uploaded manifest does not target the active analysis window.",
        )
    for item in manifest.items:
        if PurePosixPath(item.filename).name != item.filename:
            raise BadRequestError(
                "daily_satellite_filename_invalid",
                "Daily satellite manifest filenames must not contain directories.",
            )
    return manifest


def _validate_hotspot_geojson(content: bytes) -> str:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BadRequestError(
            "hotspot_geojson_invalid",
            "The hotspot product is not valid UTF-8 GeoJSON.",
        ) from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BadRequestError(
            "hotspot_geojson_invalid",
            "The hotspot product must be a GeoJSON FeatureCollection.",
        )
    features = payload.get("features")
    if not isinstance(features, list) or len(features) > 5_000:
        raise BadRequestError(
            "hotspot_geojson_invalid",
            "The hotspot product contains an invalid number of features.",
        )
    for feature in features:
        if not isinstance(feature, dict):
            raise BadRequestError(
                "hotspot_geojson_invalid",
                "The hotspot product contains an invalid feature.",
            )
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "Point":
            raise BadRequestError(
                "hotspot_geojson_invalid",
                "The hotspot product may contain only Point geometries.",
            )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _finalize_daily_satellite_package(
    session: Session,
    *,
    package: AgentSourcePackage,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AgentSourcePackageResponse:
    from fire_viewer.services.agent_validation_campaigns import (
        batch_is_allowed_for_active_campaign,
        resolve_requested_analysis_window,
    )

    if package.incident is None or package.episode is None or package.analysis_window is None:
        raise ConflictError(
            "daily_satellite_package_unbound",
            "The daily satellite transfer is not bound to an incident window.",
        )
    active = resolve_requested_analysis_window(
        session,
        incident=package.incident,
        episode=package.episode,
        expected_analysis_window_id=package.analysis_window.analysis_id,
    )
    store, inventory = _daily_satellite_inventory(package, settings)
    by_filename = {PurePosixPath(stored.pathname).name: stored for stored in inventory}
    if len(by_filename) != len(inventory):
        raise BadRequestError(
            "daily_satellite_filename_duplicate",
            "Daily satellite upload filenames must be unique.",
        )
    manifest_stored = by_filename.get(_DAILY_SATELLITE_MANIFEST_NAME)
    if manifest_stored is None:
        raise BadRequestError(
            "daily_satellite_manifest_missing",
            f"The upload must include {_DAILY_SATELLITE_MANIFEST_NAME}.",
        )
    manifest_uri = store.uri_for_pathname(manifest_stored.pathname)
    manifest_content = store.read_bytes(manifest_uri)
    if len(manifest_content) != manifest_stored.size_bytes:
        raise ConflictError(
            "source_media_size_changed", "A private source changed during finalization."
        )
    manifest_sha256 = hashlib.sha256(manifest_content).hexdigest()
    manifest = _parse_daily_satellite_manifest(
        content=manifest_content,
        expected_analysis_window_id=package.analysis_window.analysis_id,
    )
    expected_filenames = {_DAILY_SATELLITE_MANIFEST_NAME}
    expected_filenames.update(item.filename for item in manifest.items)
    if set(by_filename) != expected_filenames:
        raise BadRequestError(
            "daily_satellite_inventory_mismatch",
            "The uploaded files do not match the daily satellite manifest.",
        )
    if any(
        as_utc(item.acquired_at) > as_utc(package.analysis_window.window_end_at)
        for item in manifest.items
    ):
        raise BadRequestError(
            "daily_satellite_after_cutoff",
            "A satellite product was acquired after the active analysis cutoff.",
        )

    package.state = AgentSourcePackageState.FINALIZING
    manifest_package_item = AgentSourcePackageItem(
        item_id=new_prefixed_id("SI"),
        pathname=manifest_stored.pathname,
        object_uri=manifest_uri,
        original_filename=_DAILY_SATELLITE_MANIFEST_NAME,
        content_type="application/json",
        media_type=AgentMediaType.ARTICLE,
        sha256=manifest_sha256,
        size_bytes=len(manifest_content),
        captured_at=None,
        metadata_payload={"daily_satellite_manifest": True},
    )
    package.items.append(manifest_package_item)
    reference_url = create_private_media_url(
        source_kind="source_package_manifest",
        source_id=package.package_id,
        item_id=package.package_id,
        purge_after=package.purge_after,
        settings=settings,
    )
    reference_bundle = {
        "reference_id": package.package_id,
        "manifest_sha256": manifest_sha256,
        "assets": [
            {
                "kind": "source_manifest",
                "working_file_url": reference_url,
                "sha256": manifest_sha256,
                "crs": "EPSG:4326",
                "resolution_m": None,
            }
        ],
    }

    prepared: list[
        tuple[
            AgentSourcePackageItem,
            AgentDailySatelliteImageManifestItem | AgentDailyHotspotManifestItem,
            str | None,
        ]
    ] = []
    seen_hashes: set[str] = set()
    for declared in manifest.items:
        stored = by_filename[declared.filename]
        object_uri = store.uri_for_pathname(stored.pathname)
        content = store.read_bytes(object_uri)
        if len(content) != stored.size_bytes:
            raise ConflictError(
                "source_media_size_changed", "A private source changed during finalization."
            )
        digest = hashlib.sha256(content).hexdigest()
        if digest != declared.sha256:
            raise ConflictError(
                "daily_satellite_hash_mismatch",
                f"The uploaded product hash does not match the manifest: {declared.filename}.",
            )
        if digest in seen_hashes:
            raise BadRequestError(
                "daily_satellite_duplicate",
                "The daily satellite manifest contains duplicate binary content.",
            )
        seen_hashes.add(digest)
        normalized_geojson = None
        metadata: dict[str, object]
        if isinstance(declared, AgentDailySatelliteImageManifestItem):
            suffix = PurePosixPath(declared.filename).suffix.casefold()
            if suffix not in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
                raise BadRequestError(
                    "daily_satellite_image_type_invalid",
                    "Satellite image products must be JPEG, PNG, or TIFF.",
            )
            _validate_signature(content, suffix)
            if (
                tuple(declared.bands) == _CANONICAL_BURNED_AREA_BANDS
                and suffix not in {".tif", ".tiff"}
            ):
                raise BadRequestError(
                    "daily_satellite_multispectral_type_invalid",
                    "Canonical six-band satellite products must be GeoTIFF files.",
                )
            if suffix in {".tif", ".tiff"}:
                image_metadata = _geotiff_metadata(
                    content,
                    declared_bands=declared.bands,
                    declared_bbox_wgs84=declared.bbox_wgs84,
                )
            else:
                _captured_at, image_metadata = _image_metadata(content)
            width_value = image_metadata["image_width_px"]
            height_value = image_metadata["image_height_px"]
            if not isinstance(width_value, int) or not isinstance(height_value, int):
                raise BadRequestError(
                    "daily_satellite_image_invalid",
                    "The satellite image dimensions are invalid.",
                )
            width = width_value
            height = height_value
            min_lon, min_lat, max_lon, max_lat = declared.bbox_wgs84
            geotransform = image_metadata.get(
                "geotransform",
                [
                    min_lon,
                    (max_lon - min_lon) / width,
                    0.0,
                    max_lat,
                    0.0,
                    -(max_lat - min_lat) / height,
                ],
            )
            media_type = AgentMediaType.SATELLITE_IMAGE
            content_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".tif": "image/tiff",
                ".tiff": "image/tiff",
            }[suffix]
            metadata = {
                **image_metadata,
                "satellite": {
                    "product_id": declared.product_id,
                    "provider": declared.provider,
                    "acquired_at": as_utc(declared.acquired_at).isoformat(),
                    "crs": image_metadata.get("crs", "EPSG:4326"),
                    "raster_width_px": width,
                    "raster_height_px": height,
                    "geotransform": geotransform,
                    "bbox_wgs84": image_metadata.get(
                        "bbox_wgs84",
                        list(declared.bbox_wgs84),
                    ),
                    "resolution_m": declared.resolution_m,
                    "bands": declared.bands,
                    "cloud_cover_percent": declared.cloud_cover_percent,
                },
                "hotspot": None,
            }
        else:
            if PurePosixPath(declared.filename).suffix.casefold() not in {".json", ".geojson"}:
                raise BadRequestError(
                    "hotspot_geojson_type_invalid",
                    "Hotspot products must be JSON or GeoJSON.",
                )
            normalized_geojson = _validate_hotspot_geojson(content)
            media_type = AgentMediaType.SATELLITE_DATA
            content_type = "application/geo+json"
            metadata = {
                "satellite": None,
                "hotspot": {
                    "product_id": declared.product_id,
                    "provider": declared.provider,
                    "acquired_at": as_utc(declared.acquired_at).isoformat(),
                    "sensor_names": declared.sensor_names,
                    "resolution_m": declared.resolution_m,
                    "bbox_wgs84": list(declared.bbox_wgs84),
                },
            }
        package_item = AgentSourcePackageItem(
            item_id=new_prefixed_id("SI"),
            pathname=stored.pathname,
            object_uri=object_uri,
            original_filename=declared.filename,
            content_type=content_type,
            media_type=media_type,
            sha256=digest,
            size_bytes=len(content),
            captured_at=as_utc(declared.acquired_at),
            metadata_payload=metadata,
        )
        package.items.append(package_item)
        prepared.append((package_item, declared, normalized_geojson))
    session.flush()

    for offset in range(0, len(prepared), 32):
        chunk = prepared[offset : offset + 32]
        batch = AgentMediaBatch(
            batch_id=new_prefixed_id("AB"),
            schema_version="2.0",
            batch_type=AgentBatchType.SATELLITE_MEDIA,
            priority=AgentBatchPriority.SCHEDULED_COMBINED,
            state=AgentBatchState.DRAFT,
            incident_id=package.incident_id,
            episode_id=package.episode_id,
            analysis_window_id=package.analysis_window_id,
            reference_bundle_payload=reference_bundle,
            idempotency_key=f"daily-satellite:{package.package_id}:{offset // 32}",
            request_hash=hashlib.sha256(
                "\n".join(item.sha256 for item, _declared, _geojson in chunk).encode()
            ).hexdigest(),
            trace_id=package.trace_id,
            deadline_at=None,
            purge_after=package.purge_after,
        )
        session.add(batch)
        for package_item, declared, normalized_geojson in chunk:
            proxy_url = create_private_media_url(
                source_kind="source_package",
                source_id=package.package_id,
                item_id=package_item.item_id,
                purge_after=package.purge_after,
                settings=settings,
            )
            media_item = AgentMediaItem(
                input_id=package_item.item_id,
                media_type=package_item.media_type,
                working_file_url=(
                    proxy_url
                    if package_item.media_type == AgentMediaType.SATELLITE_IMAGE
                    else None
                ),
                media_sha256=package_item.sha256,
                size_bytes=package_item.size_bytes,
                metadata_payload={
                    "provenance": {
                        "source_key": declared.product_id,
                        "source_reference_url": str(declared.source_reference_url),
                        "license_identifier": declared.license_identifier,
                        "attribution": declared.attribution,
                        "trust": "institutional",
                    },
                    "captured_at": as_utc(declared.acquired_at).isoformat(),
                    "camera": None,
                    **package_item.metadata_payload,
                    "private_source_package": {
                        "package_id": package.package_id,
                        "item_id": package_item.item_id,
                        "object_uri": package_item.object_uri,
                    },
                },
                processable_payload={
                    "frames": [],
                    "audio_url": None,
                    "article_text": normalized_geojson,
                },
                preprocessing_status="validated",
                purge_after=package.purge_after,
            )
            media_item.consent = AgentMediaConsent(
                basis=AgentConsentBasis.INSTITUTIONAL_MANDATE,
                state=AgentConsentState.GRANTED,
                scopes=list(package.consent_scopes),
                terms_version=package.terms_version,
                evidence_sha256=package.consent_evidence_sha256,
                subject_reference_hash=None,
                source_reference_url=str(declared.source_reference_url),
                license_identifier=declared.license_identifier,
                granted_at=package.created_at,
                expires_at=package.purge_after,
            )
            batch.items.append(media_item)
            session.flush()
            package_item.agent_media_item_id = media_item.id
        if not batch_is_allowed_for_active_campaign(batch, active):
            raise ConflictError(
                "agent_campaign_media_not_allowed",
                "The daily satellite package contains a file outside the active campaign manifest.",
            )

    package.state = AgentSourcePackageState.CONVERTED
    package.finalized_at = utcnow()
    record_operator_audit(
        session,
        actor=actor,
        action="agent.daily_satellite_package_finalized",
        target_type="agent_source_package",
        target_id=package.package_id,
        reason="Daily institutional products validated for the active analysis window.",
        trace_id=trace_id,
        after={
            "analysis_window_id": package.analysis_window.analysis_id,
            "products": len(prepared),
            "publication_authorized": False,
        },
    )
    session.commit()
    return _package_response(_load_package(session, package.package_id))


def finalize_source_package(
    session: Session,
    *,
    package_id: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AgentSourcePackageResponse:
    package = _load_package(session, package_id)
    if package.state == AgentSourcePackageState.CONVERTED:
        return _package_response(package)
    if package.state != AgentSourcePackageState.OPEN:
        raise ConflictError(
            "source_package_not_open", "Only an open source package can be finalized."
        )
    if package.package_kind == AgentSourcePackageKind.ADMIN_SATELLITE:
        return _finalize_daily_satellite_package(
            session,
            package=package,
            actor=actor,
            trace_id=trace_id,
            settings=settings,
        )
    store = build_object_store(settings)
    key = f"source-packages/{package.upload_id}"
    try:
        inventory = store.list_prefix(key, limit=package.declared_file_count + 1)
    except ObjectStorageError as exc:
        raise ConflictError(
            "source_package_inventory_unavailable", "The private upload cannot be inspected."
        ) from exc
    if len(inventory) != package.declared_file_count:
        raise ConflictError(
            "source_package_inventory_incomplete",
            "The uploaded file count does not match the opened transfer.",
        )
    if sum(item.size_bytes for item in inventory) != package.declared_total_size_bytes:
        raise ConflictError(
            "source_package_size_mismatch",
            "The uploaded byte count does not match the opened transfer.",
        )

    package.state = AgentSourcePackageState.FINALIZING
    explicit_dates = _file_date_metadata(package)
    uploaded_filenames = [
        PurePosixPath(stored.pathname).name.casefold() for stored in inventory
    ]
    if explicit_dates and len(uploaded_filenames) != len(set(uploaded_filenames)):
        raise BadRequestError(
            "source_filename_ambiguous",
            "Explicit date metadata requires unique uploaded filenames.",
        )
    unknown_date_files = set(explicit_dates).difference(uploaded_filenames)
    if unknown_date_files:
        raise BadRequestError(
            "source_file_date_metadata_mismatch",
            "Explicit date metadata references a file that was not uploaded.",
        )
    unique_hashes: set[str] = set()
    unique_items: list[tuple[AgentSourcePackageItem, bytes]] = []
    for stored in inventory:
        suffix = PurePosixPath(stored.pathname).suffix.casefold()
        media_shape = _SUFFIX_MEDIA.get(suffix)
        if media_shape is None:
            raise BadRequestError(
                "source_media_type_unsupported", "The source package contains an unsupported file."
            )
        media_type, content_type = media_shape
        object_uri = store.uri_for_pathname(stored.pathname)
        content = store.read_bytes(object_uri)
        if len(content) != stored.size_bytes:
            raise ConflictError(
                "source_media_size_changed", "A private source changed during finalization."
            )
        _validate_signature(content, suffix)
        captured_at = None
        metadata: dict[str, object] = {
            "declared_location_hint": package.location_hint,
            "detected_content_type": content_type,
        }
        if media_type == AgentMediaType.IMAGE:
            if suffix in {".tif", ".tiff"} and _tiff_samples_per_pixel(content) == len(
                _CANONICAL_BURNED_AREA_BANDS
            ):
                image_metadata = _geotiff_metadata(
                    content,
                    declared_bands=list(_CANONICAL_BURNED_AREA_BANDS),
                    declared_bbox_wgs84=None,
                )
                media_type = AgentMediaType.SATELLITE_IMAGE
                captured_at = None
                image_metadata["admin_satellite_six_band"] = True
            else:
                captured_at, image_metadata = _image_metadata(content)
            metadata.update(image_metadata)
        digest = hashlib.sha256(content).hexdigest()
        package_item = AgentSourcePackageItem(
            item_id=new_prefixed_id("SI"),
            pathname=stored.pathname,
            object_uri=object_uri,
            original_filename=PurePosixPath(stored.pathname).name,
            content_type=content_type,
            media_type=media_type,
            sha256=digest,
            size_bytes=len(content),
            captured_at=captured_at,
            metadata_payload=metadata,
        )
        if package.public_contribution is None:
            _resolve_admin_item_date(
                package=package,
                package_item=package_item,
                explicit=explicit_dates.get(package_item.original_filename.casefold()),
            )
        elif captured_at is not None:
            package_item.date_evidence = cast(str, metadata.get("date_evidence"))
        package.items.append(package_item)
        if digest in unique_hashes:
            package_item.metadata_payload["duplicate_within_package"] = True
        else:
            unique_hashes.add(digest)
            unique_items.append((package_item, content))

    _create_media_batches(
        session,
        package=package,
        unique_items=unique_items,
        settings=settings,
    )
    unique_by_hash = {item.sha256: item for item, _content in unique_items}
    for package_item in package.items:
        if not package_item.metadata_payload.get("duplicate_within_package"):
            continue
        source_item = unique_by_hash[package_item.sha256]
        if package.package_kind == AgentSourcePackageKind.ADMIN_SOURCES:
            candidate_date = package_item.metadata_payload.get(
                "classified_local_date_candidate"
            )
            source_date = source_item.classified_local_date
            if (
                package_item.date_evidence == "CONFLICTING_METADATA"
                or source_item.date_classification != "CLASSIFIED"
                or source_date is None
                or candidate_date != source_date.isoformat()
            ):
                package_item.date_classification = "TO_CLASSIFY"
                package_item.classified_local_date = None
                package_item.analysis_window_id = None
                package_item.metadata_payload["date_classification"] = "TO_CLASSIFY"
                package_item.metadata_payload["duplicate_date_requires_review"] = True
                continue
            package_item.date_classification = "CLASSIFIED"
            package_item.classified_local_date = source_date
            package_item.analysis_window_id = source_item.analysis_window_id
            package_item.metadata_payload["date_classification"] = "CLASSIFIED"
            package_item.metadata_payload["classified_local_date"] = source_date.isoformat()
            # Keep the duplicate file's own timestamp and evidence. Deduplication
            # reuses the routed media item, not the provenance of every upload.
            continue
        package_item.captured_at = source_item.captured_at
        package_item.date_classification = source_item.date_classification
        package_item.date_evidence = source_item.date_evidence
        package_item.classified_local_date = source_item.classified_local_date
        package_item.analysis_window_id = source_item.analysis_window_id
        package_item.metadata_payload.update(
            {
                "date_classification": source_item.date_classification,
                "date_evidence": source_item.date_evidence,
                "classified_local_date": (
                    source_item.classified_local_date.isoformat()
                    if source_item.classified_local_date is not None
                    else None
                ),
            }
        )
    package.state = AgentSourcePackageState.CONVERTED
    package.finalized_at = utcnow()
    record_operator_audit(
        session,
        actor=actor,
        action="agent.source_package_finalized",
        target_type="agent_source_package",
        target_id=package.package_id,
        reason="Private upload validated and converted to normal user_media batches.",
        trace_id=trace_id,
        after={
            "files": len(package.items),
            "unique_media": len(unique_items),
            "classified_files": sum(
                item.date_classification == "CLASSIFIED" for item in package.items
            ),
            "files_to_classify": sum(
                item.date_classification == "TO_CLASSIFY" for item in package.items
            ),
            "publication_authorized": False,
        },
    )
    session.commit()
    return _package_response(_load_package(session, package.package_id))


def get_source_package(session: Session, package_id: str) -> AgentSourcePackageResponse:
    return _package_response(_load_package(session, package_id))


def read_private_source_media(
    session: Session,
    *,
    item_id: str,
    token: str,
    settings: Settings,
) -> PrivateMediaPayload:
    try:
        claims = jwt.decode(
            token,
            settings.agent_media_signing_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=_MEDIA_JWT_AUDIENCE,
            issuer=_MEDIA_JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "source_kind", "source_id"]},
        )
    except jwt.PyJWTError as exc:
        raise ForbiddenError("The private media link is invalid or expired.") from exc
    if claims.get("sub") != item_id:
        raise ForbiddenError("The private media link does not match this item.")
    source_kind = claims.get("source_kind")
    source_id = claims.get("source_id")
    media_item: AgentMediaItem | None
    object_uri: str
    expected_hash: str | None
    content_type: str | None
    filename: str
    if source_kind == "source_package":
        package_item = session.execute(
            select(AgentSourcePackageItem)
            .where(AgentSourcePackageItem.item_id == item_id)
            .options(
                selectinload(AgentSourcePackageItem.package),
                selectinload(AgentSourcePackageItem.agent_media_item).selectinload(
                    AgentMediaItem.consent
                ),
            )
        ).scalar_one_or_none()
        if package_item is None or source_id != package_item.package.package_id:
            raise NotFoundError("private_agent_media", item_id)
        media_item = package_item.agent_media_item
        if (
            package_item.package.state != AgentSourcePackageState.CONVERTED
            or as_utc(package_item.package.purge_after) <= utcnow()
        ):
            raise ForbiddenError("This private media is no longer available for analysis.")
        object_uri = package_item.object_uri
        expected_hash = package_item.sha256
        content_type = package_item.content_type
        filename = package_item.original_filename
    elif source_kind == "source_package_manifest":
        package = session.execute(
            select(AgentSourcePackage)
            .where(AgentSourcePackage.package_id == source_id)
            .options(selectinload(AgentSourcePackage.items))
        ).scalar_one_or_none()
        if (
            package is None
            or package.package_id != item_id
            or package.package_kind != AgentSourcePackageKind.ADMIN_SATELLITE
            or package.state != AgentSourcePackageState.CONVERTED
            or not package.analysis_authorized
            or as_utc(package.purge_after) <= utcnow()
        ):
            raise ForbiddenError("This private source manifest is no longer available.")
        package_item = next(
            (
                candidate
                for candidate in package.items
                if candidate.original_filename == _DAILY_SATELLITE_MANIFEST_NAME
            ),
            None,
        )
        if package_item is None:
            raise NotFoundError("private_agent_manifest", item_id)
        content = build_object_store(settings).read_bytes(package_item.object_uri)
        if hashlib.sha256(content).hexdigest() != package_item.sha256:
            raise ConflictError(
                "private_media_integrity_failed", "Private media integrity failed."
            )
        return PrivateMediaPayload(
            content=content,
            content_type=package_item.content_type,
            filename=package_item.original_filename,
        )
    elif source_kind == "source_research":
        candidate = session.execute(
            select(AgentSourceCandidate)
            .where(AgentSourceCandidate.candidate_id == item_id)
            .options(
                selectinload(AgentSourceCandidate.research_run),
                selectinload(AgentSourceCandidate.agent_media_item).selectinload(
                    AgentMediaItem.consent
                ),
            )
        ).scalar_one_or_none()
        if candidate is None or source_id != candidate.research_run.research_id:
            raise NotFoundError("private_agent_media", item_id)
        media_item = candidate.agent_media_item
        if not candidate.cutoff_eligible or candidate.object_uri is None:
            raise ForbiddenError("This research media is not eligible for analysis.")
        object_uri = candidate.object_uri
        expected_hash = candidate.media_sha256
        content_type = mimetypes.guess_type(urlparse(candidate.canonical_url).path)[0]
        filename = PurePosixPath(urlparse(candidate.canonical_url).path).name or item_id
    else:
        raise ForbiddenError("The private media link source is invalid.")
    if (
        media_item is None
        or media_item.purged_at is not None
        or media_item.consent.state != AgentConsentState.GRANTED
        or as_utc(media_item.purge_after) <= utcnow()
    ):
        raise ForbiddenError("This private media is no longer available for analysis.")
    content = build_object_store(settings).read_bytes(object_uri)
    if expected_hash is None or hashlib.sha256(content).hexdigest() != expected_hash:
        raise ConflictError("private_media_integrity_failed", "Private media integrity failed.")
    return PrivateMediaPayload(
        content=content,
        content_type=content_type or "application/octet-stream",
        filename=filename,
    )
