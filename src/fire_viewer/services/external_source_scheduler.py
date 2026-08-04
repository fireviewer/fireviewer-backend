from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    Episode,
    EventCandidate,
    ExternalCollection,
    ExternalProvider,
    IncidentCandidate,
    IncidentSeries,
    IncidentSourcePlan,
)
from fire_viewer.domain.enums import ExternalSemanticRole
from fire_viewer.domain.external_source_schemas import ExternalArtifactInput
from fire_viewer.domain.geospatial import bbox_for_point
from fire_viewer.services.external_source_registry import (
    SourcePlanClaim,
    claim_due_incident_source_plans,
    record_source_plan_failure,
    record_source_plan_success,
    register_external_artifact_revision,
)


@dataclass(frozen=True, slots=True)
class ExternalCollectionContext:
    """Immutable, incident-scoped acquisition request handed to one connector."""

    plan_id: str
    provider_key: str
    collection_id: int
    collection_key: str
    semantic_role: ExternalSemanticRole
    target_kind: str
    target_public_id: str
    bbox_wgs84: tuple[float, float, float, float]
    reference_point_wgs84: tuple[float, float]
    observed_start_at: datetime | None
    observed_end_at: datetime | None
    watermark: str | None
    collection_configuration: dict[str, object]
    plan_configuration: dict[str, object]


@dataclass(frozen=True, slots=True)
class ExternalFetchResult:
    artifacts: tuple[ExternalArtifactInput, ...]
    watermark: str


class ExternalConnector(Protocol):
    def fetch(self, context: ExternalCollectionContext) -> ExternalFetchResult: ...


class ExternalConnectorRegistry:
    """Exact provider/collection routing; wildcards are intentionally unsupported."""

    def __init__(self) -> None:
        self._connectors: dict[tuple[str, str], ExternalConnector] = {}

    def register(
        self,
        *,
        provider_key: str,
        collection_key: str,
        connector: ExternalConnector,
    ) -> None:
        key = (provider_key.strip().casefold(), collection_key.strip().casefold())
        if not all(key):
            raise ValueError("provider_key and collection_key are required")
        if key in self._connectors:
            raise ValueError("one connector is already registered for this exact collection")
        self._connectors[key] = connector

    def resolve(self, *, provider_key: str, collection_key: str) -> ExternalConnector | None:
        return self._connectors.get(
            (provider_key.strip().casefold(), collection_key.strip().casefold())
        )


def _incident_context(
    session: Session,
    plan: IncidentSourcePlan,
) -> tuple[
    str,
    str,
    tuple[float, float, float, float],
    tuple[float, float],
    datetime | None,
    datetime | None,
]:
    if plan.incident_id is not None:
        incident = session.get(IncidentSeries, plan.incident_id)
        if incident is None:
            raise RuntimeError("source_plan_incident_missing")
        episode = session.execute(
            select(Episode)
            .where(Episode.incident_id == incident.id, Episode.is_current.is_(True))
            .order_by(Episode.ordinal.desc())
        ).scalar_one_or_none()
        return (
            "incident",
            incident.fire_id,
            (
                incident.bbox_min_lon,
                incident.bbox_min_lat,
                incident.bbox_max_lon,
                incident.bbox_max_lat,
            ),
            (incident.reference_lon, incident.reference_lat),
            as_utc(episode.started_at) if episode is not None else None,
            as_utc(episode.last_observed_at) if episode is not None else None,
        )

    if plan.incident_candidate_id is None:
        raise RuntimeError("source_plan_target_missing")
    candidate = session.get(IncidentCandidate, plan.incident_candidate_id)
    if candidate is None:
        raise RuntimeError("source_plan_incident_candidate_missing")
    if candidate.reference_lon is None or candidate.reference_lat is None:
        raise RuntimeError("source_plan_candidate_geometry_missing")
    accuracy = candidate.horizontal_accuracy_m or 1_000.0
    bbox = bbox_for_point(candidate.reference_lon, candidate.reference_lat, accuracy)
    observation_window = session.execute(
        select(
            func.min(EventCandidate.observed_start_at),
            func.max(
                func.coalesce(EventCandidate.observed_end_at, EventCandidate.observed_start_at)
            ),
        ).where(EventCandidate.incident_candidate_id == candidate.id)
    ).one()
    return (
        "incident_candidate",
        candidate.candidate_id,
        (bbox.min_lon, bbox.min_lat, bbox.max_lon, bbox.max_lat),
        (candidate.reference_lon, candidate.reference_lat),
        as_utc(observation_window[0]) if observation_window[0] is not None else None,
        as_utc(observation_window[1]) if observation_window[1] is not None else None,
    )


def _collection_context(session: Session, claim: SourcePlanClaim) -> ExternalCollectionContext:
    plan = session.get(IncidentSourcePlan, claim.plan.id)
    if plan is None:
        raise RuntimeError("source_plan_missing_after_claim")
    collection = session.get(ExternalCollection, plan.collection_id)
    if collection is None:
        raise RuntimeError("source_plan_collection_missing")
    provider = session.get(ExternalProvider, collection.provider_id)
    if provider is None or not provider.enabled:
        raise RuntimeError("source_plan_provider_unavailable")
    (
        target_kind,
        target_public_id,
        bbox,
        reference_point,
        observed_start,
        observed_end,
    ) = _incident_context(session, plan)
    return ExternalCollectionContext(
        plan_id=plan.plan_id,
        provider_key=provider.provider_key,
        collection_id=collection.id,
        collection_key=collection.collection_key,
        semantic_role=collection.semantic_role,
        target_kind=target_kind,
        target_public_id=target_public_id,
        bbox_wgs84=bbox,
        reference_point_wgs84=reference_point,
        observed_start_at=observed_start,
        observed_end_at=observed_end,
        watermark=plan.watermark,
        collection_configuration=dict(collection.configuration),
        plan_configuration=dict(plan.configuration),
    )


def _safe_failure_code(exc: Exception) -> str:
    """Persist only a bounded class-level failure code, never URLs, tokens or response bodies."""

    if isinstance(exc, LookupError):
        return "connector_not_registered"
    if isinstance(exc, ValueError):
        return "connector_contract_invalid"
    return f"connector_failed:{type(exc).__name__}"[:1_000]


def _record_failure(
    factory: sessionmaker[Session],
    *,
    claim: SourcePlanClaim,
    settings: Settings,
    worker_id: str,
    exc: Exception,
    now: datetime,
) -> None:
    with factory() as session:
        record_source_plan_failure(
            session,
            plan_id=claim.plan.plan_id,
            lease_token=claim.lease_token,
            error=_safe_failure_code(exc),
            settings=settings,
            actor_id=worker_id,
            trace_id=new_prefixed_id("TRC"),
            now=now,
        )


def run_external_source_scheduler_once(
    factory: sessionmaker[Session],
    *,
    settings: Settings,
    worker_id: str,
    connectors: ExternalConnectorRegistry,
    now: datetime | None = None,
) -> bool:
    """Claim, acquire and persist one due incident source plan.

    Artifact writes are immutable and idempotent. If a batch fails after a partial
    write, the plan stays retryable and the next pass replays the accepted hashes.
    """

    if not settings.official_connectors_enabled:
        return False
    effective_now = as_utc(now) if now is not None else utcnow()
    with factory() as session:
        claims = claim_due_incident_source_plans(
            session,
            settings=settings,
            worker_id=worker_id,
            limit=1,
            now=effective_now,
        )
        if not claims:
            return False
        claim = claims[0]
        try:
            context = _collection_context(session, claim)
        except Exception as exc:
            _record_failure(
                factory,
                claim=claim,
                settings=settings,
                worker_id=worker_id,
                exc=exc,
                now=effective_now,
            )
            return True

    connector = connectors.resolve(
        provider_key=context.provider_key,
        collection_key=context.collection_key,
    )
    if connector is None:
        _record_failure(
            factory,
            claim=claim,
            settings=settings,
            worker_id=worker_id,
            exc=LookupError("connector_not_registered"),
            now=effective_now,
        )
        return True

    try:
        result = connector.fetch(context)
        if not isinstance(result, ExternalFetchResult):
            raise ValueError("connector_result_type_invalid")
        watermark = result.watermark.strip()
        if not watermark or len(watermark) > 1_000:
            raise ValueError("connector_watermark_invalid")
        for artifact in result.artifacts:
            if artifact.collection_id != context.collection_id:
                raise ValueError("connector_artifact_collection_mismatch")
            with factory() as artifact_session:
                register_external_artifact_revision(
                    artifact_session,
                    payload=artifact,
                    actor_id=worker_id,
                    trace_id=new_prefixed_id("TRC"),
                )
        with factory() as completion_session:
            record_source_plan_success(
                completion_session,
                plan_id=claim.plan.plan_id,
                lease_token=claim.lease_token,
                watermark=watermark,
                actor_id=worker_id,
                trace_id=new_prefixed_id("TRC"),
                now=effective_now,
            )
    except Exception as exc:
        _record_failure(
            factory,
            claim=claim,
            settings=settings,
            worker_id=worker_id,
            exc=exc,
            now=effective_now,
        )
    return True
