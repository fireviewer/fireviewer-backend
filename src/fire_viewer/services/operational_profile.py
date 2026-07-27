"""Operator-managed incident metrics and external model eligibility.

This service does not generate, store, or load a 3D model.  It persists the
human-reviewed operational inputs and emits an outbox request when the CDC
eligibility rule first becomes true.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.db.models import Episode, IncidentSeries
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.model_eligibility import evaluate_model_generation_eligibility
from fire_viewer.domain.schemas import OperationalProfileRequest, OperationalProfileResponse
from fire_viewer.services.common import (
    emit_outbox,
    episode_snapshot,
    record_operator_audit,
)
from fire_viewer.services.idempotency import find_replay, store_response


@dataclass(frozen=True, slots=True)
class OperationalProfileOutcome:
    response: OperationalProfileResponse
    replayed: bool


def update_operational_profile(
    session: Session,
    *,
    fire_id: str,
    payload: OperationalProfileRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> OperationalProfileOutcome:
    endpoint = f"POST /api/v1/operator/incidents/{fire_id}/operational-profile"
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
        return OperationalProfileOutcome(
            response=OperationalProfileResponse.model_validate(replay.response_body),
            replayed=True,
        )

    incident = session.execute(
        select(IncidentSeries).where(IncidentSeries.fire_id == fire_id).with_for_update()
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = session.execute(
        select(Episode)
        .where(Episode.incident_id == incident.id, Episode.is_current.is_(True))
        .with_for_update()
    ).scalar_one()
    if episode.version != payload.expected_version:
        raise ConflictError(
            "stale_episode_version",
            "The episode was modified by another operation.",
            extra={"current_version": episode.version},
        )

    previous_eligibility = evaluate_model_generation_eligibility(
        estimated_area_ha=episode.estimated_area_ha,
        evacuation_established=episode.evacuation_established,
        area_threshold_ha=settings.model_generation_min_area_ha,
    )
    before = episode_snapshot(episode)
    episode.estimated_area_ha = payload.estimated_area_ha
    episode.evacuation_established = payload.evacuation_established
    episode.evacuation_basis = payload.evacuation_basis
    episode.version += 1

    eligibility = evaluate_model_generation_eligibility(
        estimated_area_ha=episode.estimated_area_ha,
        evacuation_established=episode.evacuation_established,
        area_threshold_ha=settings.model_generation_min_area_ha,
    )
    request_id: str | None = None
    if eligibility.eligible and not previous_eligibility.eligible:
        request_event = emit_outbox(
            session,
            topic="model_generation.eligible",
            aggregate_type="episode",
            aggregate_id=f"{incident.fire_id}/{episode.episode_id}",
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            payload={
                "fire_id": incident.fire_id,
                "episode_id": episode.episode_id,
                "estimated_area_ha": episode.estimated_area_ha,
                "evacuation_established": episode.evacuation_established,
                "eligibility_reasons": list(eligibility.reasons),
                "execution_scope": "external_pipeline_not_implemented",
            },
        )
        request_id = request_event.event_id

    session.flush()
    record_operator_audit(
        session,
        actor=actor,
        action="episode.operational_profile.updated",
        target_type="episode",
        target_id=f"{incident.fire_id}/{episode.episode_id}",
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=episode_snapshot(episode),
        payload={
            "model_generation_eligible": eligibility.eligible,
            "eligibility_reasons": list(eligibility.reasons),
            "external_request_id": request_id,
        },
    )
    response = OperationalProfileResponse(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        version=episode.version,
        estimated_area_ha=episode.estimated_area_ha,
        evacuation_established=episode.evacuation_established,
        model_generation_eligible=eligibility.eligible,
        eligibility_reasons=list(eligibility.reasons),
        terrain_bake_request_id=request_id,
        trace_id=trace_id,
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json", exclude_none=True),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return OperationalProfileOutcome(response=response, replayed=False)
