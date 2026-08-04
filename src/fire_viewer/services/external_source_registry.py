from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pyproj import CRS, Transformer, network
from shapely.geometry import mapping, shape
from shapely.ops import transform
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    ArtifactLineage,
    ExternalArtifactRevision,
    ExternalCollection,
    ExternalProvider,
    IncidentCandidate,
    IncidentSeries,
    IncidentSourcePlan,
)
from fire_viewer.domain.enums import (
    ActorType,
    ExternalArtifactStatus,
    ExternalLineageRelation,
    ExternalSemanticRole,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.external_source_schemas import (
    ExternalArtifactInput,
    ExternalCollectionInput,
    ExternalProviderInput,
    IncidentSourcePlanInput,
)
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.services.common import record_audit

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "key",
        "sig",
        "signature",
    }
)
_SECRET_FIELD_SUFFIXES = (
    "_password",
    "_secret",
    "_credential",
    "_token",
    "_signature",
    "_sig",
    "_api_key",
    "_access_key",
    "_subscription_key",
    "_private_key",
)
_FORECAST_ROLE = ExternalSemanticRole.WEATHER_FORECAST
_SENSOR_ROLES = frozenset(
    {ExternalSemanticRole.RAW_EARTH_OBSERVATION, ExternalSemanticRole.SENSOR_DETECTION}
)


@dataclass(frozen=True, slots=True)
class ArtifactRegistrationResult:
    artifact: ExternalArtifactRevision
    replayed: bool
    lineage_relations: tuple[ExternalLineageRelation, ...]


@dataclass(frozen=True, slots=True)
class SourcePlanClaim:
    plan: IncidentSourcePlan
    lease_token: str
    lease_until: datetime


def _is_secret_field(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith(_SECRET_FIELD_SUFFIXES)


def _audit_snapshot_provider(row: ExternalProvider) -> dict[str, Any]:
    return {
        "provider_key": row.provider_key,
        "display_name": row.display_name,
        "allowed_domains": row.allowed_domains,
        "authentication_kind": row.authentication_kind,
        "attribution": row.attribution,
        "enabled": row.enabled,
    }


def _audit_snapshot_collection(row: ExternalCollection) -> dict[str, Any]:
    return {
        "provider_id": row.provider_id,
        "collection_key": row.collection_key,
        "product_name": row.product_name,
        "sensor": row.sensor,
        "platform": row.platform,
        "license": row.license,
        "cadence_seconds": row.cadence_seconds,
        "semantic_role": row.semantic_role.value,
        "configuration": row.configuration,
    }


def _normalize_domain(value: str) -> str:
    candidate = value.strip().casefold().rstrip(".")
    if (
        not candidate
        or candidate == "*"
        or candidate.startswith("*.")
        or "/" in candidate
        or ":" in candidate
        or not _DOMAIN_RE.fullmatch(candidate)
    ):
        raise BadRequestError(
            "invalid_external_provider_domain",
            "External provider domains must be exact host names without wildcards.",
        )
    return candidate


def _assert_no_secrets(value: Any, *, path: str = "configuration") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_secret_field(str(key)):
                raise BadRequestError(
                    "external_configuration_contains_secret",
                    f"{path}.{key} is a secret-like field and cannot be persisted.",
                )
            _assert_no_secrets(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secrets(child, path=f"{path}[{index}]")


def _validate_source_url(source_url: str, provider: ExternalProvider) -> str:
    try:
        parts = urlsplit(source_url)
        port = parts.port
    except ValueError as exc:
        raise BadRequestError(
            "external_source_url_not_allowed",
            "The artifact URL is malformed.",
        ) from exc
    hostname = (parts.hostname or "").casefold().rstrip(".")
    allowed = {_normalize_domain(item) for item in provider.allowed_domains}
    if (
        parts.scheme.casefold() != "https"
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in {None, 443}
        or hostname not in allowed
    ):
        raise BadRequestError(
            "external_source_url_not_allowed",
            "The artifact URL must use HTTPS and an exact provider allowlisted domain.",
        )
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        if _is_secret_field(key):
            raise BadRequestError(
                "external_source_url_contains_secret",
                "External artifact URLs cannot persist secret-like query parameters.",
            )
    return urlunsplit(("https", hostname, parts.path, parts.query, ""))


def _normalize_geometry(
    geojson: dict[str, Any] | None,
    native_crs: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if geojson is None:
        return None, None
    network.set_network_enabled(False)  # type: ignore[attr-defined]
    if native_crs is None:
        raise BadRequestError(
            "external_geometry_crs_required",
            "A declared CRS is required for every external geometry.",
        )
    try:
        source_crs = CRS.from_user_input(native_crs)
    except Exception as exc:
        raise BadRequestError(
            "external_geometry_crs_unknown",
            "The external geometry CRS is unknown or unsupported.",
        ) from exc
    try:
        geometry = shape(geojson)
    except Exception as exc:
        raise BadRequestError(
            "external_geometry_invalid", "The external geometry is not valid GeoJSON."
        ) from exc
    if geometry.is_empty or not geometry.is_valid:
        raise BadRequestError(
            "external_geometry_invalid",
            "The external geometry must be non-empty and topologically valid.",
        )
    try:
        if source_crs != CRS.from_epsg(4326):
            transformer = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True)
            geometry = transform(transformer.transform, geometry)
    except Exception as exc:
        raise BadRequestError(
            "external_geometry_transform_failed",
            "The external geometry could not be transformed to EPSG:4326.",
        ) from exc
    if geometry.is_empty or not geometry.is_valid:
        raise BadRequestError(
            "external_geometry_transform_failed",
            "The transformed external geometry is invalid.",
        )
    min_x, min_y, max_x, max_y = geometry.bounds
    if min_x < -180 or max_x > 180 or min_y < -90 or max_y > 90:
        raise BadRequestError(
            "external_geometry_out_of_bounds",
            "The transformed external geometry is outside WGS84 bounds.",
        )
    return dict(mapping(geometry)), source_crs.to_string()


def register_external_provider(
    session: Session,
    *,
    payload: ExternalProviderInput,
    actor_id: str,
    trace_id: str,
) -> ExternalProvider:
    domains = [_normalize_domain(item) for item in payload.allowed_domains]
    _acquire_postgresql_registry_locks(session, f"provider:{payload.provider_key}")
    existing = session.execute(
        select(ExternalProvider)
        .where(ExternalProvider.provider_key == payload.provider_key)
        .with_for_update()
    ).scalar_one_or_none()
    before = _audit_snapshot_provider(existing) if existing is not None else None
    if existing is None:
        row = ExternalProvider(provider_key=payload.provider_key)
        session.add(row)
    else:
        row = existing
    row.display_name = payload.display_name
    row.allowed_domains = domains
    row.authentication_kind = payload.authentication_kind
    row.attribution = payload.attribution
    row.enabled = payload.enabled
    after = _audit_snapshot_provider(row)
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="external_provider.registered" if before is None else "external_provider.updated",
        target_type="external_provider",
        target_id=row.provider_key,
        reason="Versioned external provider policy registered by an internal service.",
        trace_id=trace_id,
        before=before,
        after=after,
    )
    session.commit()
    return row


def register_external_collection(
    session: Session,
    *,
    payload: ExternalCollectionInput,
    actor_id: str,
    trace_id: str,
) -> ExternalCollection:
    _assert_no_secrets(payload.configuration)
    provider = session.execute(
        select(ExternalProvider).where(ExternalProvider.provider_key == payload.provider_key)
    ).scalar_one_or_none()
    if provider is None:
        raise NotFoundError("external_provider", payload.provider_key)
    _acquire_postgresql_registry_locks(
        session,
        f"collection:{provider.id}:{payload.collection_key}",
    )
    existing = session.execute(
        select(ExternalCollection)
        .where(
            ExternalCollection.provider_id == provider.id,
            ExternalCollection.collection_key == payload.collection_key,
        )
        .with_for_update()
    ).scalar_one_or_none()
    before = _audit_snapshot_collection(existing) if existing is not None else None
    if existing is not None:
        artifact_count = int(
            session.scalar(
                select(func.count())
                .select_from(ExternalArtifactRevision)
                .where(ExternalArtifactRevision.collection_id == existing.id)
            )
            or 0
        )
        if artifact_count and (
            existing.semantic_role != payload.semantic_role
            or existing.sensor != payload.sensor
            or existing.platform != payload.platform
        ):
            raise ConflictError(
                "external_collection_identity_immutable",
                "A collection sensor, platform or semantic role cannot change after ingestion.",
            )
    if existing is None:
        row = ExternalCollection(provider_id=provider.id, collection_key=payload.collection_key)
        session.add(row)
    else:
        row = existing
    row.product_name = payload.product_name
    row.sensor = payload.sensor
    row.platform = payload.platform
    row.license = payload.license
    row.cadence_seconds = payload.cadence_seconds
    row.semantic_role = payload.semantic_role
    row.configuration = payload.configuration
    after = _audit_snapshot_collection(row)
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="external_collection.registered"
        if before is None
        else "external_collection.updated",
        target_type="external_collection",
        target_id=f"{payload.provider_key}:{row.collection_key}",
        reason="External collection policy registered without persisted credentials.",
        trace_id=trace_id,
        before=before,
        after=after,
    )
    session.commit()
    return row


def _validate_artifact_semantics(
    collection: ExternalCollection,
    payload: ExternalArtifactInput,
) -> None:
    if collection.semantic_role == _FORECAST_ROLE:
        if payload.forecast_run_at is None or payload.forecast_valid_at is None:
            raise BadRequestError(
                "forecast_times_required",
                "A forecast artifact requires both forecast run and valid times.",
            )
    elif payload.forecast_run_at is not None or payload.forecast_valid_at is not None:
        raise BadRequestError(
            "forecast_times_forbidden",
            "Forecast times cannot be attached to an observation artifact.",
        )
    if collection.semantic_role in _SENSOR_ROLES:
        if collection.sensor is None or payload.acquisition_start_at is None:
            raise BadRequestError(
                "sensor_acquisition_identity_required",
                "Sensor products require a sensor and acquisition start time.",
            )
        if payload.acquisition_granule_id is None:
            raise BadRequestError(
                "sensor_granule_required", "Sensor products require an acquisition granule id."
            )
    if (
        collection.semantic_role == ExternalSemanticRole.SENSOR_DETECTION
        and payload.acquisition_pixel_id is None
    ):
        raise BadRequestError(
            "sensor_pixel_required",
            "Sensor detections require a pixel identity to prevent double corroboration.",
        )


def _has_sensor_acquisition_identity(
    collection: ExternalCollection,
    payload: ExternalArtifactInput,
) -> bool:
    return (
        collection.semantic_role in _SENSOR_ROLES
        and collection.sensor is not None
        and payload.acquisition_start_at is not None
        and payload.acquisition_granule_id is not None
    )


def _evidence_family_key(
    collection: ExternalCollection,
    payload: ExternalArtifactInput,
) -> str:
    identity: dict[str, Any]
    if _has_sensor_acquisition_identity(collection, payload):
        sensor = collection.sensor
        acquisition_start_at = payload.acquisition_start_at
        granule_id = payload.acquisition_granule_id
        assert sensor is not None
        assert acquisition_start_at is not None
        assert granule_id is not None
        identity = {
            "sensor": sensor.strip().casefold(),
            "platform": (collection.platform or "").strip().casefold(),
            "acquisition_start_at": as_utc(acquisition_start_at).isoformat(),
            "granule_id": granule_id.strip().casefold(),
            "pixel_id": (
                payload.acquisition_pixel_id.strip().casefold()
                if payload.acquisition_pixel_id is not None
                else None
            ),
        }
    else:
        # A fallback is intentionally collection-scoped: unrelated products are
        # never counted as independent corroborations merely because metadata is sparse.
        identity = {
            "collection_id": collection.id,
            "external_product_id": payload.external_product_id,
        }
    return sha256_hex(identity)


def _new_lineage(
    session: Session,
    *,
    parent: ExternalArtifactRevision,
    child: ExternalArtifactRevision,
    relation: ExternalLineageRelation,
    reason: str,
) -> ArtifactLineage:
    row = ArtifactLineage(
        parent_revision_id=parent.id,
        child_revision_id=child.id,
        relation=relation,
        reason=reason,
    )
    session.add(row)
    return row


def _acquire_postgresql_registry_locks(session: Session, *identities: str) -> None:
    """Serialize registry identities without adding a lock table or network dependency."""

    if session.get_bind().dialect.name != "postgresql":
        return
    lock_keys = sorted(
        {
            int(sha256_hex({"external_registry_identity": identity})[:15], 16)
            for identity in identities
        }
    )
    for lock_key in lock_keys:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


def register_external_artifact_revision(
    session: Session,
    *,
    payload: ExternalArtifactInput,
    actor_id: str,
    trace_id: str,
) -> ArtifactRegistrationResult:
    collection = session.execute(
        select(ExternalCollection)
        .where(ExternalCollection.id == payload.collection_id)
        .with_for_update()
    ).scalar_one_or_none()
    if collection is None:
        raise NotFoundError("external_collection", str(payload.collection_id))
    provider = session.get(ExternalProvider, collection.provider_id)
    if provider is None:
        raise RuntimeError("External collection provider invariant is broken")
    if not provider.enabled:
        raise ConflictError(
            "external_provider_disabled",
            "The external provider is disabled and cannot ingest artifacts.",
        )
    source_url = _validate_source_url(payload.source_url, provider)
    _acquire_postgresql_registry_locks(
        session,
        f"content:{payload.content_hash}",
    )
    _assert_no_secrets(payload.quality_flags, path="quality_flags")
    _validate_artifact_semantics(collection, payload)
    footprint, normalized_crs = _normalize_geometry(payload.footprint_geojson, payload.native_crs)
    license_value = (payload.license or collection.license).strip()
    attribution = (payload.attribution or provider.attribution).strip()
    if not license_value or not attribution:
        raise BadRequestError(
            "external_rights_required",
            "Every external artifact requires a license and attribution.",
        )

    same_url = (
        session.execute(
            select(ExternalArtifactRevision)
            .where(
                ExternalArtifactRevision.collection_id == collection.id,
                ExternalArtifactRevision.source_url == source_url,
            )
            .order_by(ExternalArtifactRevision.revision.desc())
        )
        .scalars()
        .first()
    )
    if same_url is not None and same_url.external_product_id != payload.external_product_id:
        raise ConflictError(
            "external_source_url_product_conflict",
            "One canonical source URL cannot identify multiple external products.",
        )
    if (
        same_url is not None
        and same_url.content_hash == payload.content_hash
        and same_url.status == payload.status
    ):
        return ArtifactRegistrationResult(same_url, True, ())

    product_predecessor = (
        session.execute(
            select(ExternalArtifactRevision)
            .where(
                ExternalArtifactRevision.collection_id == collection.id,
                ExternalArtifactRevision.external_product_id == payload.external_product_id,
            )
            .order_by(ExternalArtifactRevision.revision.desc())
        )
        .scalars()
        .first()
    )
    predecessor = same_url or product_predecessor
    if (
        payload.status in {ExternalArtifactStatus.CORRECTED, ExternalArtifactStatus.RETRACTED}
        and predecessor is None
    ):
        raise ConflictError(
            "external_revision_predecessor_required",
            "A correction or retraction requires an existing artifact revision.",
        )

    mirror = (
        session.execute(
            select(ExternalArtifactRevision)
            .where(
                ExternalArtifactRevision.content_hash == payload.content_hash,
                ExternalArtifactRevision.source_url != source_url,
            )
            .order_by(ExternalArtifactRevision.id)
        )
        .scalars()
        .first()
    )
    revision = (
        int(
            session.scalar(
                select(func.max(ExternalArtifactRevision.revision)).where(
                    ExternalArtifactRevision.collection_id == collection.id,
                    ExternalArtifactRevision.external_product_id == payload.external_product_id,
                )
            )
            or 0
        )
        + 1
    )
    family_key = _evidence_family_key(collection, payload)
    same_family = None
    if _has_sensor_acquisition_identity(collection, payload):
        same_family = (
            session.execute(
                select(ExternalArtifactRevision)
                .where(ExternalArtifactRevision.evidence_family_key == family_key)
                .order_by(ExternalArtifactRevision.id)
            )
            .scalars()
            .first()
        )
    row = ExternalArtifactRevision(
        artifact_revision_id=new_prefixed_id("EAR"),
        collection_id=collection.id,
        external_product_id=payload.external_product_id,
        source_url=source_url,
        revision=revision,
        content_hash=payload.content_hash,
        etag=payload.etag,
        processing_baseline=payload.processing_baseline,
        acquisition_granule_id=payload.acquisition_granule_id,
        acquisition_pixel_id=payload.acquisition_pixel_id,
        evidence_family_key=family_key,
        acquisition_start_at=(
            as_utc(payload.acquisition_start_at) if payload.acquisition_start_at else None
        ),
        acquisition_end_at=(
            as_utc(payload.acquisition_end_at) if payload.acquisition_end_at else None
        ),
        effective_start_at=(
            as_utc(payload.effective_start_at) if payload.effective_start_at else None
        ),
        effective_end_at=(as_utc(payload.effective_end_at) if payload.effective_end_at else None),
        processed_at=as_utc(payload.processed_at) if payload.processed_at else None,
        published_at=as_utc(payload.published_at) if payload.published_at else None,
        retrieved_at=as_utc(payload.retrieved_at),
        forecast_run_at=(as_utc(payload.forecast_run_at) if payload.forecast_run_at else None),
        forecast_valid_at=(
            as_utc(payload.forecast_valid_at) if payload.forecast_valid_at else None
        ),
        native_crs=normalized_crs,
        footprint_geojson=footprint,
        resolution_m=payload.resolution_m,
        quality_flags=payload.quality_flags,
        license=license_value,
        attribution=attribution,
        status=payload.status,
        semantic_role=collection.semantic_role,
    )
    session.add(row)
    session.flush()
    relations: list[ExternalLineageRelation] = []
    if payload.status == ExternalArtifactStatus.RETRACTED and predecessor is not None:
        _new_lineage(
            session,
            parent=predecessor,
            child=row,
            relation=ExternalLineageRelation.RETRACTS,
            reason="Official retraction preserved as a new immutable revision.",
        )
        relations.append(ExternalLineageRelation.RETRACTS)
    elif (
        payload.status == ExternalArtifactStatus.CORRECTED
        or (
            same_url is not None
            and (same_url.content_hash != payload.content_hash or same_url.status != payload.status)
        )
    ) and predecessor is not None:
        _new_lineage(
            session,
            parent=predecessor,
            child=row,
            relation=ExternalLineageRelation.SUPERSEDES,
            reason="New immutable content supersedes the prior artifact revision.",
        )
        relations.append(ExternalLineageRelation.SUPERSEDES)
    if mirror is not None:
        _new_lineage(
            session,
            parent=mirror,
            child=row,
            relation=ExternalLineageRelation.MIRRORS,
            reason="Identical content retrieved through a different allowed URL.",
        )
        relations.append(ExternalLineageRelation.MIRRORS)

    if same_family is not None and same_family.id != row.id:
        _new_lineage(
            session,
            parent=same_family,
            child=row,
            relation=ExternalLineageRelation.SAME_ACQUISITION_AS,
            reason="Sensor, acquisition, granule and pixel identify one evidence family.",
        )
        relations.append(ExternalLineageRelation.SAME_ACQUISITION_AS)

    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="external_artifact_revision.registered",
        target_type="external_artifact_revision",
        target_id=row.artifact_revision_id,
        reason="Immutable external artifact revision registered after provenance validation.",
        trace_id=trace_id,
        after={
            "collection_id": collection.id,
            "external_product_id": row.external_product_id,
            "revision": row.revision,
            "content_hash": row.content_hash,
            "status": row.status.value,
            "semantic_role": row.semantic_role.value,
            "evidence_family_key": row.evidence_family_key,
            "lineage": [relation.value for relation in relations],
        },
    )
    session.commit()
    return ArtifactRegistrationResult(row, False, tuple(relations))


def register_incident_source_plan(
    session: Session,
    *,
    payload: IncidentSourcePlanInput,
    actor_id: str,
    trace_id: str,
) -> IncidentSourcePlan:
    _assert_no_secrets(payload.configuration)
    collection = session.get(ExternalCollection, payload.collection_id)
    if collection is None:
        raise NotFoundError("external_collection", str(payload.collection_id))
    if payload.incident_id is not None and session.get(IncidentSeries, payload.incident_id) is None:
        raise NotFoundError("incident", str(payload.incident_id))
    if (
        payload.incident_candidate_id is not None
        and session.get(IncidentCandidate, payload.incident_candidate_id) is None
    ):
        raise NotFoundError("incident_candidate", str(payload.incident_candidate_id))
    cadence = payload.cadence_seconds or collection.cadence_seconds
    if cadence is None:
        raise BadRequestError(
            "source_plan_cadence_required",
            "An incident source plan requires an explicit or collection cadence.",
        )
    target_filter = (
        IncidentSourcePlan.incident_id == payload.incident_id
        if payload.incident_id is not None
        else IncidentSourcePlan.incident_candidate_id == payload.incident_candidate_id
    )
    target_kind = "incident" if payload.incident_id is not None else "incident_candidate"
    target_id = payload.incident_id or payload.incident_candidate_id
    _acquire_postgresql_registry_locks(
        session,
        f"source-plan:{target_kind}:{target_id}:{collection.id}",
    )
    row = session.execute(
        select(IncidentSourcePlan)
        .where(
            target_filter,
            IncidentSourcePlan.collection_id == collection.id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    before = source_plan_snapshot(row) if row is not None else None
    if row is None:
        row = IncidentSourcePlan(
            plan_id=new_prefixed_id("ISP"),
            incident_id=payload.incident_id,
            incident_candidate_id=payload.incident_candidate_id,
            collection_id=collection.id,
            watermark=None,
            next_poll_at=utcnow(),
            backoff_seconds=0,
        )
        session.add(row)
    row.enabled = payload.enabled
    row.cadence_seconds = cadence
    row.configuration = payload.configuration
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="incident_source_plan.registered"
        if before is None
        else "incident_source_plan.updated",
        target_type="incident_source_plan",
        target_id=row.plan_id,
        reason="Incident-scoped external acquisition plan registered without network access.",
        trace_id=trace_id,
        before=before,
        after=source_plan_snapshot(row),
    )
    session.commit()
    return row


def source_plan_snapshot(row: IncidentSourcePlan) -> dict[str, Any]:
    return {
        "plan_id": row.plan_id,
        "incident_id": row.incident_id,
        "incident_candidate_id": row.incident_candidate_id,
        "collection_id": row.collection_id,
        "enabled": row.enabled,
        "cadence_seconds": row.cadence_seconds,
        "watermark": row.watermark,
        "next_poll_at": as_utc(row.next_poll_at).isoformat() if row.next_poll_at else None,
        "last_success_at": (
            as_utc(row.last_success_at).isoformat() if row.last_success_at else None
        ),
        "last_error": row.last_error,
        "backoff_seconds": row.backoff_seconds,
        "lease_owner": row.lease_owner,
        "lease_active": row.lease_token_hash is not None,
        "lease_acquired_at": (
            as_utc(row.lease_acquired_at).isoformat() if row.lease_acquired_at else None
        ),
        "lease_until": as_utc(row.lease_until).isoformat() if row.lease_until else None,
        "configuration": row.configuration,
    }


def due_incident_source_plans(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> list[IncidentSourcePlan]:
    if not settings.official_connectors_enabled:
        return []
    effective_now = as_utc(now) if now is not None else utcnow()
    return list(
        session.scalars(
            select(IncidentSourcePlan)
            .join(
                ExternalCollection,
                ExternalCollection.id == IncidentSourcePlan.collection_id,
            )
            .join(
                ExternalProvider,
                ExternalProvider.id == ExternalCollection.provider_id,
            )
            .where(
                IncidentSourcePlan.enabled.is_(True),
                ExternalProvider.enabled.is_(True),
                or_(
                    IncidentSourcePlan.next_poll_at.is_(None),
                    IncidentSourcePlan.next_poll_at <= effective_now,
                ),
                or_(
                    IncidentSourcePlan.lease_until.is_(None),
                    IncidentSourcePlan.lease_until <= effective_now,
                ),
            )
            .order_by(IncidentSourcePlan.next_poll_at, IncidentSourcePlan.id)
        )
    )


def claim_due_incident_source_plans(
    session: Session,
    *,
    settings: Settings,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> list[SourcePlanClaim]:
    if not settings.official_connectors_enabled:
        return []
    normalized_worker_id = worker_id.strip()
    if not normalized_worker_id or len(normalized_worker_id) > 255:
        raise BadRequestError(
            "source_plan_worker_invalid",
            "A source-plan worker id between 1 and 255 characters is required.",
        )
    if limit < 1 or limit > 100:
        raise BadRequestError(
            "source_plan_claim_limit_invalid",
            "A source-plan claim limit between 1 and 100 is required.",
        )
    if lease_seconds < 30 or lease_seconds > 3_600:
        raise BadRequestError(
            "source_plan_lease_invalid",
            "A source-plan lease between 30 and 3600 seconds is required.",
        )
    effective_now = as_utc(now) if now is not None else utcnow()
    rows = list(
        session.scalars(
            select(IncidentSourcePlan)
            .join(
                ExternalCollection,
                ExternalCollection.id == IncidentSourcePlan.collection_id,
            )
            .join(
                ExternalProvider,
                ExternalProvider.id == ExternalCollection.provider_id,
            )
            .where(
                IncidentSourcePlan.enabled.is_(True),
                ExternalProvider.enabled.is_(True),
                or_(
                    IncidentSourcePlan.next_poll_at.is_(None),
                    IncidentSourcePlan.next_poll_at <= effective_now,
                ),
                or_(
                    IncidentSourcePlan.lease_until.is_(None),
                    IncidentSourcePlan.lease_until <= effective_now,
                ),
            )
            .order_by(IncidentSourcePlan.next_poll_at, IncidentSourcePlan.id)
            .limit(limit)
            .with_for_update(skip_locked=True, of=IncidentSourcePlan)
        )
    )
    claims: list[SourcePlanClaim] = []
    for row in rows:
        before = source_plan_snapshot(row)
        lease_token = new_prefixed_id("ISL")
        lease_until = effective_now + timedelta(seconds=lease_seconds)
        row.lease_owner = normalized_worker_id
        row.lease_token_hash = sha256_hex({"lease_token": lease_token})
        row.lease_acquired_at = effective_now
        row.lease_until = lease_until
        record_audit(
            session,
            actor_type=ActorType.SERVICE,
            actor_id=normalized_worker_id,
            action="incident_source_plan.claimed",
            target_type="incident_source_plan",
            target_id=row.plan_id,
            reason="Due external acquisition plan claimed under a bounded worker lease.",
            trace_id=new_prefixed_id("TRC"),
            before=before,
            after=source_plan_snapshot(row),
        )
        claims.append(SourcePlanClaim(row, lease_token, lease_until))
    session.commit()
    return claims


def _leased_source_plan(
    session: Session,
    *,
    plan_id: str,
    lease_token: str,
    now: datetime,
) -> IncidentSourcePlan:
    row = session.execute(
        select(IncidentSourcePlan).where(IncidentSourcePlan.plan_id == plan_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("incident_source_plan", plan_id)
    if (
        not lease_token
        or row.lease_token_hash != sha256_hex({"lease_token": lease_token})
        or row.lease_until is None
        or as_utc(row.lease_until) <= now
    ):
        raise ConflictError(
            "source_plan_lease_stale",
            "The source-plan lease is absent, expired, or owned by another execution.",
        )
    return row


def _clear_source_plan_lease(row: IncidentSourcePlan) -> None:
    row.lease_owner = None
    row.lease_token_hash = None
    row.lease_acquired_at = None
    row.lease_until = None


def record_source_plan_failure(
    session: Session,
    *,
    plan_id: str,
    lease_token: str,
    error: str,
    settings: Settings,
    actor_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> IncidentSourcePlan:
    effective_now = as_utc(now) if now is not None else utcnow()
    row = _leased_source_plan(
        session,
        plan_id=plan_id,
        lease_token=lease_token,
        now=effective_now,
    )
    before = source_plan_snapshot(row)
    backoff = (
        settings.official_connector_backoff_initial_seconds
        if row.backoff_seconds <= 0
        else min(row.backoff_seconds * 2, settings.official_connector_backoff_max_seconds)
    )
    row.backoff_seconds = backoff
    row.last_error = error[:1_000]
    row.next_poll_at = effective_now + timedelta(seconds=backoff)
    _clear_source_plan_lease(row)
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="incident_source_plan.failed",
        target_type="incident_source_plan",
        target_id=row.plan_id,
        reason="External acquisition failure recorded with bounded backoff.",
        trace_id=trace_id,
        before=before,
        after=source_plan_snapshot(row),
    )
    session.commit()
    return row


def record_source_plan_success(
    session: Session,
    *,
    plan_id: str,
    lease_token: str,
    watermark: str,
    actor_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> IncidentSourcePlan:
    normalized_watermark = watermark.strip()
    if not normalized_watermark:
        raise BadRequestError(
            "source_plan_watermark_required", "A non-empty watermark is required."
        )
    if len(normalized_watermark) > 1_000:
        raise BadRequestError(
            "source_plan_watermark_too_long",
            "The source-plan watermark cannot exceed 1000 characters.",
        )
    effective_now = as_utc(now) if now is not None else utcnow()
    row = _leased_source_plan(
        session,
        plan_id=plan_id,
        lease_token=lease_token,
        now=effective_now,
    )
    before = source_plan_snapshot(row)
    row.watermark = normalized_watermark
    row.last_success_at = effective_now
    row.last_error = None
    row.backoff_seconds = 0
    row.next_poll_at = effective_now + timedelta(seconds=row.cadence_seconds)
    _clear_source_plan_lease(row)
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="incident_source_plan.succeeded",
        target_type="incident_source_plan",
        target_id=row.plan_id,
        reason="External acquisition watermark advanced after a successful transaction.",
        trace_id=trace_id,
        before=before,
        after=source_plan_snapshot(row),
    )
    session.commit()
    return row
