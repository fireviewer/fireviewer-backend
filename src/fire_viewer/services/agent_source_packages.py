"""Normal private ingestion contract for user-provided incident sources."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import jwt
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
    AgentSourcePackageState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.services.blob_uploads import (
    ALLOWED_SOURCE_CONTENT_TYPES,
    create_source_blob_upload_grant,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.storage import build_object_store
from fire_viewer.storage.object_store import ObjectStorageError

_MEDIA_JWT_ISSUER = "fire-viewer-api"
_MEDIA_JWT_AUDIENCE = "fire-viewer-agent-private-media"
_TERMS_VERSION = "firewarning-private-analysis-v1"
_TIMEZONE = ZoneInfo("Europe/Paris")
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


def ensure_daily_analysis_window(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    local_date: date,
) -> AgentAnalysisWindow:
    existing = session.execute(
        select(AgentAnalysisWindow).where(
            AgentAnalysisWindow.incident_id == incident.id,
            AgentAnalysisWindow.episode_id == episode.id,
            AgentAnalysisWindow.local_date == local_date,
        )
    ).scalar_one_or_none()
    if existing is not None:
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
            selectinload(AgentSourcePackage.public_contribution),
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
    return AgentSourcePackageResponse(
        package_id=package.package_id,
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
                batch_id=(item.agent_media_item.batch.batch_id if item.agent_media_item else None),
                input_id=item.agent_media_item.input_id if item.agent_media_item else None,
            )
            for item in package.items
        ],
    )


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
    end_date = payload.known_end_date or payload.known_start_date
    package = AgentSourcePackage(
        package_id=package_id,
        incident_id=incident.id,
        episode_id=episode.id,
        analysis_window_id=None,
        state=AgentSourcePackageState.OPEN,
        upload_id=grant.upload_id,
        pathname_prefix=grant.pathname_prefix,
        declared_file_count=payload.file_count,
        declared_total_size_bytes=payload.total_size_bytes,
        known_start_date=payload.known_start_date,
        known_end_date=end_date,
        location_hint=payload.location_hint,
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
            raw_date = image.getexif().get(36867) or image.getexif().get(306)
            if isinstance(raw_date, str):
                try:
                    captured_at = datetime.strptime(raw_date, "%Y:%m:%d %H:%M:%S").replace(
                        tzinfo=_TIMEZONE
                    )
                except ValueError:
                    metadata["unparsed_capture_date"] = raw_date[:128]
            return captured_at, metadata
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise BadRequestError(
            "source_image_invalid", "An uploaded image cannot be decoded safely."
        ) from exc


def _article_text(content: bytes, content_type: str) -> str:
    text = content.decode("utf-8", errors="replace")
    if content_type == "text/html":
        parser = _TextExtractor()
        parser.feed(text)
        text = "\n".join(parser.parts)
    return text[:100_000]


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
    by_date: dict[date, list[tuple[AgentSourcePackageItem, bytes]]] = {}
    for item, content in unique_items:
        item_date = (
            as_utc(item.captured_at).astimezone(_TIMEZONE).date()
            if item.captured_at is not None
            else package.known_end_date
        )
        if not package.known_start_date <= item_date <= package.known_end_date:
            item.metadata_payload["capture_date_outside_declared_period"] = True
            item_date = package.known_end_date
        by_date.setdefault(item_date, []).append((item, content))

    for local_date, dated_items in sorted(by_date.items()):
        window = None
        if package.incident is not None and package.episode is not None:
            window = ensure_daily_analysis_window(
                session,
                incident=package.incident,
                episode=package.episode,
                local_date=local_date,
            )
            if package.analysis_window_id is None and len(by_date) == 1:
                package.analysis_window_id = window.id
        for offset in range(0, len(dated_items), 32):
            chunk = dated_items[offset : offset + 32]
            batch_id = new_prefixed_id("AB")
            batch = AgentMediaBatch(
                batch_id=batch_id,
                schema_version="2.0" if window is not None else "1.0",
                batch_type=AgentBatchType.USER_MEDIA,
                priority=AgentBatchPriority.SCHEDULED_COMBINED,
                state=AgentBatchState.DRAFT,
                incident_id=package.incident_id,
                episode_id=package.episode_id,
                analysis_window_id=window.id if window is not None else None,
                reference_bundle_payload=None,
                idempotency_key=f"source-package:{package.package_id}:{local_date}:{offset // 32}",
                request_hash=hashlib.sha256(
                    "\n".join(item.sha256 for item, _content in chunk).encode()
                ).hexdigest(),
                trace_id=package.trace_id,
                deadline_at=None,
                purge_after=package.purge_after,
            )
            session.add(batch)
            for package_item, content in chunk:
                captured_at = (
                    declared_observation.get("media_captured_at")
                    if declared_observation is not None
                    else None
                )
                if captured_at is None and package_item.captured_at is not None:
                    captured_at = as_utc(package_item.captured_at).isoformat()
                if captured_at is None and declared_observation is not None:
                    captured_at = declared_observation["observed_at"]
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
                        proxy_url if package_item.media_type == AgentMediaType.AUDIO else None
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
                        "captured_at": captured_at,
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
        package.items.append(package_item)
        if digest in unique_hashes:
            package_item.metadata_payload["duplicate_within_package"] = True
        else:
            unique_hashes.add(digest)
            unique_items.append((package_item, content))
    session.flush()

    _create_media_batches(
        session,
        package=package,
        unique_items=unique_items,
        settings=settings,
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
