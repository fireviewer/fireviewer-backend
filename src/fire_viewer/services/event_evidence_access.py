"""Short-lived, integrity-checked access to private event evidence for the worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import EventCandidate, EvidenceAsset
from fire_viewer.domain.enums import (
    EventCandidateState,
    EvidenceAssetState,
    MalwareScanState,
)
from fire_viewer.domain.errors import ConflictError, ForbiddenError, NotFoundError
from fire_viewer.services.evidence_security import file_sha256
from fire_viewer.storage import ObjectStorageError, build_object_store

_EVENT_JWT_ISSUER = "fire-viewer-event-worker"
_EVENT_JWT_AUDIENCE = "fire-viewer-private-event-evidence"


@dataclass(frozen=True, slots=True)
class PrivateEventEvidencePayload:
    local_path: Path
    content_type: str
    filename: str
    size_bytes: int


def create_event_evidence_worker_url(
    *,
    candidate_id: str,
    asset_id: str,
    sha256: str,
    settings: Settings,
) -> str:
    """Mint a bounded URL for one exact candidate/asset/hash tuple."""

    now = utcnow()
    token = jwt.encode(
        {
            "iss": _EVENT_JWT_ISSUER,
            "aud": _EVENT_JWT_AUDIENCE,
            "sub": asset_id,
            "candidate_id": candidate_id,
            "sha256": sha256,
            "iat": now,
            "exp": now + timedelta(seconds=settings.event_worker_evidence_url_ttl_seconds),
        },
        settings.agent_media_signing_secret.get_secret_value(),
        algorithm="HS256",
    )
    base = str(settings.agent_media_proxy_base_url).rstrip("/")
    return f"{base}/api/v2/private-event-evidence/{quote(asset_id, safe='')}?token={quote(token)}"


def materialize_private_event_evidence(
    session: Session,
    *,
    asset_id: str,
    token: str,
    settings: Settings,
) -> PrivateEventEvidencePayload:
    """Resolve one signed evidence object without exposing its storage URI."""

    try:
        claims = jwt.decode(
            token,
            settings.agent_media_signing_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=_EVENT_JWT_AUDIENCE,
            issuer=_EVENT_JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "candidate_id", "sha256"]},
        )
    except jwt.PyJWTError as exc:
        raise ForbiddenError("The private evidence link is invalid or expired.") from exc
    if claims.get("sub") != asset_id:
        raise ForbiddenError("The private evidence link does not match this asset.")

    asset = session.execute(
        select(EvidenceAsset).where(EvidenceAsset.asset_id == asset_id)
    ).scalar_one_or_none()
    if asset is None or asset.event_candidate_id is None:
        raise NotFoundError("event_evidence", asset_id)
    candidate = session.execute(
        select(EventCandidate).where(EventCandidate.id == asset.event_candidate_id)
    ).scalar_one_or_none()
    if candidate is None or claims.get("candidate_id") != candidate.candidate_id:
        raise ForbiddenError("The private evidence link does not match this candidate.")
    if (
        candidate.state != EventCandidateState.ANALYZING
        or not candidate.consent_analysis
        or not candidate.consent_retention
    ):
        raise ForbiddenError("This evidence is not currently available for analysis.")
    if (
        asset.state != EvidenceAssetState.VERIFIED
        or asset.malware_scan_state != MalwareScanState.CLEAN
        or asset.detected_media_type is None
        or asset.sha256 is None
        or asset.purged_at is not None
        or (asset.purge_after is not None and as_utc(asset.purge_after) <= utcnow())
    ):
        raise ForbiddenError("This evidence is not currently available for analysis.")
    if claims.get("sha256") != asset.sha256:
        raise ForbiddenError("The private evidence revision no longer matches this link.")

    return materialize_verified_event_evidence(asset, settings=settings)


def materialize_verified_event_evidence(
    asset: EvidenceAsset,
    *,
    settings: Settings,
) -> PrivateEventEvidencePayload:
    """Materialize one verified object without retaining a second durable copy."""

    if (
        asset.state != EvidenceAssetState.VERIFIED
        or asset.malware_scan_state != MalwareScanState.CLEAN
        or asset.detected_media_type is None
        or asset.sha256 is None
        or asset.purged_at is not None
    ):
        raise ForbiddenError("This evidence is not available.")
    staging_dir = settings.zone_upload_storage_dir / ".event-response-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    local_path = staging_dir / f"{asset.asset_id}-{uuid4().hex}.bin"
    store = build_object_store(settings)
    try:
        metadata = store.materialize(asset.object_uri, local_path)
        if metadata.size_bytes != asset.size_bytes or local_path.stat().st_size != asset.size_bytes:
            raise ConflictError(
                "private_evidence_integrity_failed", "Private evidence integrity failed."
            )
    except ObjectStorageError as exc:
        local_path.unlink(missing_ok=True)
        raise NotFoundError("event_evidence", asset.asset_id) from exc
    except BaseException:
        local_path.unlink(missing_ok=True)
        raise
    if file_sha256(local_path) != asset.sha256:
        local_path.unlink(missing_ok=True)
        raise ConflictError(
            "private_evidence_integrity_failed", "Private evidence integrity failed."
        )
    return PrivateEventEvidencePayload(
        local_path=local_path,
        content_type=asset.detected_media_type,
        filename=asset.file_name,
        size_bytes=asset.size_bytes,
    )
