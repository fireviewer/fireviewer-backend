from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import Episode, IncidentSeries
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import IncidentStatus, VerificationState
from fire_viewer.domain.errors import ConflictError, ForbiddenError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.public_visibility import canonical_public_visibility
from fire_viewer.domain.schemas import TransitionRequest, TransitionResponse
from fire_viewer.domain.state_machine import get_transition_rule
from fire_viewer.services.common import (
    emit_outbox,
    episode_snapshot,
    incident_snapshot,
    record_operator_audit,
)
from fire_viewer.services.idempotency import find_replay, store_response


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    response: TransitionResponse
    replayed: bool


def transition_incident(
    session: Session,
    *,
    fire_id: str,
    payload: TransitionRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> TransitionOutcome:
    endpoint = f"POST /api/v1/operator/incidents/{fire_id}/transitions"
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
        return TransitionOutcome(
            response=TransitionResponse.model_validate(replay.response_body),
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

    rule = get_transition_rule(episode.status, payload.target_status)
    if rule is None:
        record_operator_audit(
            session,
            actor=actor,
            action="incident.transition.rejected",
            target_type="episode",
            target_id=f"{fire_id}/{episode.episode_id}",
            reason=payload.reason,
            trace_id=trace_id,
            before=episode_snapshot(episode),
            payload={
                "requested_status": payload.target_status.value,
                "cause": "illegal_transition",
            },
        )
        session.commit()
        raise ConflictError(
            "illegal_state_transition",
            f"Transition {episode.status.value} -> {payload.target_status.value} is not allowed.",
        )

    role_ok = (
        actor.has_all_roles(rule.required_roles)
        if rule.require_all_roles
        else actor.has_any_role(rule.required_roles)
    )
    if not role_ok:
        record_operator_audit(
            session,
            actor=actor,
            action="incident.transition.rejected",
            target_type="episode",
            target_id=f"{fire_id}/{episode.episode_id}",
            reason=payload.reason,
            trace_id=trace_id,
            before=episode_snapshot(episode),
            payload={"requested_status": payload.target_status.value, "cause": "insufficient_role"},
        )
        session.commit()
        raise ForbiddenError("The authenticated actor is not allowed to perform this transition.")
    if rule.requires_validation_basis and not payload.validation_basis:
        raise ConflictError(
            "validation_basis_required",
            "A documented validation_basis is required for ACTIVE_CONFIRMED.",
        )

    before = {"episode": episode_snapshot(episode), "incident": incident_snapshot(incident)}
    previous_status = episode.status
    now = utcnow()
    episode.status = payload.target_status
    episode.version += 1

    if payload.target_status == IncidentStatus.ACTIVE_CONFIRMED:
        episode.verification_state = VerificationState.VERIFIED
        episode.evidence_basis_at = now
        episode.validated_at = now
        episode.ended_at = None
        episode.review_required = False
    elif payload.target_status in {IncidentStatus.CANDIDATE, IncidentStatus.UNDER_REVIEW}:
        episode.review_required = True
        episode.ended_at = None
    elif payload.target_status == IncidentStatus.MONITORING:
        episode.review_required = False
        episode.ended_at = None
    elif payload.target_status in {
        IncidentStatus.EXTINGUISHED,
        IncidentStatus.CLOSED,
        IncidentStatus.REJECTED,
    }:
        if payload.target_status == IncidentStatus.REJECTED:
            episode.verification_state = VerificationState.REJECTED
            episode.evidence_basis_at = None
        episode.review_required = False
        episode.ended_at = now

    incident.public_visibility = canonical_public_visibility(
        payload.target_status, episode.verification_state
    )
    if payload.target_status == IncidentStatus.SUSPENDED:
        incident.public_note = payload.public_note or "Incident suspended pending review."
    elif payload.target_status in {
        IncidentStatus.CANDIDATE,
        IncidentStatus.UNDER_REVIEW,
        IncidentStatus.REJECTED,
    }:
        if payload.public_note is not None:
            incident.public_note = payload.public_note
    else:
        if payload.public_note is not None or previous_status == IncidentStatus.SUSPENDED:
            incident.public_note = payload.public_note
    incident.version += 1

    session.flush()
    after = {"episode": episode_snapshot(episode), "incident": incident_snapshot(incident)}
    record_operator_audit(
        session,
        actor=actor,
        action="incident.status.changed",
        target_type="episode",
        target_id=f"{fire_id}/{episode.episode_id}",
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=after,
        payload={
            "validation_basis_hash": (
                sha256_hex(payload.validation_basis) if payload.validation_basis else None
            ),
            "public_note": payload.public_note,
        },
    )
    emit_outbox(
        session,
        topic="incident.status_changed",
        aggregate_type="incident_series",
        aggregate_id=fire_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        payload={
            "fire_id": fire_id,
            "episode_id": episode.episode_id,
            "previous_status": previous_status.value,
            "status": episode.status.value,
            "version": episode.version,
        },
    )
    response = TransitionResponse(
        fire_id=fire_id,
        episode_id=episode.episode_id,
        previous_status=previous_status,
        status=episode.status,
        version=episode.version,
        review_required=episode.review_required,
        trace_id=trace_id,
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return TransitionOutcome(response=response, replayed=False)
