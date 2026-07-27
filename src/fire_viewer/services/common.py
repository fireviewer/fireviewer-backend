from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from fire_viewer.core.ids import (
    format_episode_id,
    format_fire_id,
    new_event_id,
)
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc
from fire_viewer.db.models import (
    AuditEvent,
    Episode,
    FireIdCounter,
    IncidentSeries,
    Observation,
    OutboxEvent,
    Source,
)
from fire_viewer.domain.enums import ActorType, IncidentStatus, VerificationState
from fire_viewer.domain.geospatial import bbox_for_point
from fire_viewer.domain.hashing import json_safe, sha256_hex
from fire_viewer.domain.public_visibility import canonical_public_visibility


def source_snapshot(source: Source) -> dict[str, Any]:
    return {
        "source_key": source.source_key,
        "source_type": source.source_type.value,
        "trust": source.trust.value,
        "public_display_name": source.public_display_name,
        "public_license": source.public_license,
        "public_reference_url": source.public_reference_url,
        "public_transformations": source.public_transformations,
        "enabled": source.enabled,
    }


def incident_snapshot(incident: IncidentSeries) -> dict[str, Any]:
    return {
        "fire_id": incident.fire_id,
        "public_visibility": incident.public_visibility.value,
        "public_note": incident.public_note,
        "version": incident.version,
    }


def episode_snapshot(episode: Episode) -> dict[str, Any]:
    return {
        "episode_id": episode.episode_id,
        "status": episode.status.value,
        "verification_state": episode.verification_state.value,
        "corroborating_source_count": episode.corroborating_source_count,
        "evidence_basis_at": (
            as_utc(episode.evidence_basis_at) if episode.evidence_basis_at else None
        ),
        "estimated_area_ha": episode.estimated_area_ha,
        "evacuation_established": episode.evacuation_established,
        "review_required": episode.review_required,
        "is_current": episode.is_current,
        "started_at": as_utc(episode.started_at),
        "last_observed_at": as_utc(episode.last_observed_at),
        "validated_at": as_utc(episode.validated_at) if episode.validated_at else None,
        "ended_at": as_utc(episode.ended_at) if episode.ended_at else None,
        "version": episode.version,
    }


def observation_snapshot(observation: Observation) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id,
        "source_id": observation.source_id,
        "observed_at": as_utc(observation.observed_at),
        "received_at": as_utc(observation.received_at),
        "evidence_hash": observation.evidence_hash,
        "evidence_license": observation.evidence_license,
        "verification_state": observation.verification_state.value,
        "public_spatial_mode": observation.public_spatial_mode.value,
        "attached_incident_id": observation.attached_incident_id,
        "attached_episode_id": observation.attached_episode_id,
        "proposed_incident_id": observation.proposed_incident_id,
        "proposed_episode_id": observation.proposed_episode_id,
        "match_decision": observation.match_decision.value,
        "raw_purged_at": (as_utc(observation.raw_purged_at) if observation.raw_purged_at else None),
        "version": observation.version,
    }


def record_audit(
    session: Session,
    *,
    actor_type: ActorType,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    trace_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_id=new_event_id(),
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before_hash=sha256_hex(before) if before is not None else None,
        after_hash=sha256_hex(after) if after is not None else None,
        before_snapshot=json_safe(before) if before is not None else None,
        after_snapshot=json_safe(after) if after is not None else None,
        reason=reason,
        trace_id=trace_id,
        payload=payload or {},
    )
    session.add(event)
    return event


def record_operator_audit(
    session: Session,
    *,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    trace_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    return record_audit(
        session,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        trace_id=trace_id,
        before=before,
        after=after,
        payload=payload,
    )


def emit_outbox(
    session: Session,
    *,
    topic: str,
    aggregate_type: str,
    aggregate_id: str,
    trace_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    event = OutboxEvent(
        event_id=new_event_id(),
        topic=topic,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    session.add(event)
    return event


def allocate_fire_id(session: Session, territory_code: str) -> tuple[str, int]:
    if session.get_bind().dialect.name == "postgresql":
        statement = (
            postgresql_insert(FireIdCounter)
            .values(territory_code=territory_code, next_sequence=2)
            .on_conflict_do_update(
                index_elements=[FireIdCounter.territory_code],
                set_={"next_sequence": FireIdCounter.next_sequence + 1},
            )
            .returning(FireIdCounter.next_sequence - 1)
        )
        sequence = int(session.scalar(statement))
        return format_fire_id(territory_code, sequence), sequence

    counter = session.get(FireIdCounter, territory_code)
    if counter is None:
        sequence = 1
        session.add(FireIdCounter(territory_code=territory_code, next_sequence=2))
    else:
        sequence = counter.next_sequence
        counter.next_sequence += 1
    return format_fire_id(territory_code, sequence), sequence


def get_current_episode(
    session: Session,
    incident_id: int,
    *,
    for_update: bool = False,
) -> Episode | None:
    statement = select(Episode).where(
        Episode.incident_id == incident_id,
        Episode.is_current.is_(True),
    )
    if for_update:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def create_incident_and_episode(
    session: Session,
    *,
    territory_code: str,
    longitude: float,
    latitude: float,
    uncertainty_m: float,
    canonical_name: str | None,
    observed_at: datetime,
    policy_id: str,
    initial_status: IncidentStatus = IncidentStatus.CANDIDATE,
) -> tuple[IncidentSeries, Episode]:
    fire_id, sequence = allocate_fire_id(session, territory_code)
    bbox = bbox_for_point(longitude, latitude, uncertainty_m)
    incident = IncidentSeries(
        fire_id=fire_id,
        territory_code=territory_code,
        sequence=sequence,
        canonical_name=canonical_name,
        reference_lon=longitude,
        reference_lat=latitude,
        horizontal_uncertainty_m=uncertainty_m,
        bbox_min_lon=bbox.min_lon,
        bbox_max_lon=bbox.max_lon,
        bbox_min_lat=bbox.min_lat,
        bbox_max_lat=bbox.max_lat,
        public_visibility=canonical_public_visibility(initial_status, VerificationState.UNVERIFIED),
        version=1,
    )
    session.add(incident)
    session.flush()
    episode = Episode(
        incident_id=incident.id,
        episode_id=format_episode_id(1),
        ordinal=1,
        status=initial_status,
        verification_state=VerificationState.UNVERIFIED,
        corroborating_source_count=0,
        review_required=True,
        is_current=True,
        confidence_policy=policy_id,
        started_at=observed_at,
        last_observed_at=observed_at,
        version=1,
    )
    session.add(episode)
    session.flush()
    return incident, episode


def create_reactivation_episode(
    session: Session,
    *,
    incident: IncidentSeries,
    previous_episode: Episode,
    observed_at: datetime,
    policy_id: str,
) -> Episode:
    previous_episode.is_current = False
    previous_episode.version += 1
    incident.public_visibility = canonical_public_visibility(
        IncidentStatus.UNDER_REVIEW, VerificationState.UNVERIFIED
    )
    incident.version += 1
    max_ordinal = session.execute(
        select(func.max(Episode.ordinal)).where(Episode.incident_id == incident.id)
    ).scalar_one()
    ordinal = int(max_ordinal or 0) + 1
    episode = Episode(
        incident_id=incident.id,
        episode_id=format_episode_id(ordinal),
        ordinal=ordinal,
        status=IncidentStatus.UNDER_REVIEW,
        verification_state=VerificationState.UNVERIFIED,
        corroborating_source_count=0,
        review_required=True,
        is_current=True,
        confidence_policy=policy_id,
        started_at=observed_at,
        last_observed_at=observed_at,
        version=1,
    )
    session.add(episode)
    session.flush()
    return episode
