"""Create one private incident from a position supplied by an administrator."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import IncidentStatus
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import AdminIncidentCreateRequest, AdminIncidentCreateResponse
from fire_viewer.services.common import (
    create_incident_and_episode,
    emit_outbox,
    episode_snapshot,
    incident_snapshot,
    record_operator_audit,
)
from fire_viewer.services.idempotency import find_replay, store_response

# A map click or a copied position is an operator reference point. This value is
# persistence metadata, not another field the operator must estimate.
ADMIN_PLACEMENT_ACCURACY_M = 25.0


@dataclass(frozen=True, slots=True)
class AdminIncidentCreationOutcome:
    response: AdminIncidentCreateResponse
    replayed: bool


def create_admin_incident(
    session: Session,
    *,
    payload: AdminIncidentCreateRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AdminIncidentCreationOutcome:
    endpoint = "POST /api/v2/admin/incidents"
    request_hash = sha256_hex(
        {
            "actor_id": actor.actor_id,
            "payload": payload.model_dump(mode="json", exclude_none=True),
        }
    )
    begin_write_transaction(session)
    replay = find_replay(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay:
        session.rollback()
        return AdminIncidentCreationOutcome(
            AdminIncidentCreateResponse.model_validate(replay.response_body),
            True,
        )

    created_at = utcnow()
    incident, episode = create_incident_and_episode(
        session,
        territory_code=payload.territory_code,
        longitude=payload.longitude,
        latitude=payload.latitude,
        uncertainty_m=ADMIN_PLACEMENT_ACCURACY_M,
        canonical_name=payload.canonical_name,
        observed_at=created_at,
        policy_id=settings.matching_policy_id,
        initial_status=IncidentStatus.MONITORING,
    )
    response = AdminIncidentCreateResponse(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        canonical_name=incident.canonical_name,
        territory_code=incident.territory_code,
        longitude=incident.reference_lon,
        latitude=incident.reference_lat,
        status=episode.status,
        verification_state=episode.verification_state,
        visibility=incident.public_visibility,
        created_at=created_at,
    )
    reason = "Incident créé manuellement depuis l'administration opérationnelle."
    record_operator_audit(
        session,
        actor=actor,
        action="incident.admin_created",
        target_type="incident_series",
        target_id=incident.fire_id,
        reason=reason,
        trace_id=trace_id,
        after=incident_snapshot(incident),
        payload={"episode_id": episode.episode_id, "placement": "operator_position"},
    )
    record_operator_audit(
        session,
        actor=actor,
        action="episode.admin_created",
        target_type="episode",
        target_id=f"{incident.fire_id}/{episode.episode_id}",
        reason=reason,
        trace_id=trace_id,
        after=episode_snapshot(episode),
        payload={"fire_id": incident.fire_id},
    )
    emit_outbox(
        session,
        topic="incident.created",
        aggregate_type="incident",
        aggregate_id=incident.fire_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        payload={
            "fire_id": incident.fire_id,
            "episode_id": episode.episode_id,
            "origin": "admin_manual",
        },
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=201,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return AdminIncidentCreationOutcome(response, False)
