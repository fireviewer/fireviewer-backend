from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_observation_id
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import Episode, IncidentSeries, Observation, Source
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import (
    ActorType,
    IncidentStatus,
    MatchDecision,
    SourceTrust,
    VerificationState,
)
from fire_viewer.domain.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    SourceUnauthorizedError,
)
from fire_viewer.domain.geospatial import bbox_for_point
from fire_viewer.domain.hashing import sha256_hex, sha256_text
from fire_viewer.domain.matching import ObservationForMatch, match_observation
from fire_viewer.domain.schemas import DetectionRequest, DetectionResponse
from fire_viewer.services.candidates import find_candidates
from fire_viewer.services.common import (
    create_incident_and_episode,
    create_reactivation_episode,
    emit_outbox,
    episode_snapshot,
    incident_snapshot,
    observation_snapshot,
    record_audit,
    source_snapshot,
)
from fire_viewer.services.evidence_policy import (
    EvidencePolicyResult,
    recalculate_episode_evidence,
)
from fire_viewer.services.idempotency import find_replay, store_response

DETECTION_ENDPOINT = "POST /api/v1/incident/detect"


@dataclass(frozen=True, slots=True)
class DetectionOutcome:
    response: DetectionResponse
    status_code: int
    replayed: bool


def _validate_times(payload: DetectionRequest, settings: Settings) -> tuple[datetime, datetime]:
    observed_at = as_utc(payload.observed_at)
    received_at = as_utc(payload.received_at)
    now = utcnow()
    skew = timedelta(seconds=settings.max_clock_skew_seconds)
    if observed_at > now + skew or received_at > now + skew:
        raise DomainError(
            status_code=422,
            code="future_timestamp",
            title="Invalid timestamp",
            detail="observed_at and received_at cannot be materially in the future.",
        )
    if received_at + skew < observed_at:
        raise DomainError(
            status_code=422,
            code="received_before_observed",
            title="Invalid timestamp order",
            detail="received_at cannot precede observed_at beyond the configured clock skew.",
        )
    return observed_at, received_at


def _resolve_source(
    session: Session,
    payload: DetectionRequest,
    *,
    source_token: str | None,
    trace_id: str,
) -> Source:
    source = session.execute(
        select(Source).where(Source.source_key == payload.source.id)
    ).scalar_one_or_none()
    if source is None:
        if payload.source.trust != SourceTrust.UNVERIFIED:
            record_audit(
                session,
                actor_type=ActorType.PUBLIC_SOURCE,
                actor_id=payload.source.id,
                action="source.trust_claim.rejected",
                target_type="source",
                target_id=payload.source.id,
                reason="Unknown sources cannot self-assert a trusted classification.",
                trace_id=trace_id,
                payload={"claimed_trust": payload.source.trust.value},
            )
            session.commit()
            raise ForbiddenError("Unknown sources must use trust='unverified'.")
        source = Source(
            source_key=payload.source.id,
            source_type=payload.source.type,
            trust=SourceTrust.UNVERIFIED,
            enabled=True,
        )
        session.add(source)
        session.flush()
        record_audit(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="incident-service",
            action="source.discovered",
            target_type="source",
            target_id=source.source_key,
            reason="First observation received from an unverified source.",
            trace_id=trace_id,
            after=source_snapshot(source),
        )
        return source

    if source.source_type != payload.source.type:
        record_audit(
            session,
            actor_type=ActorType.PUBLIC_SOURCE,
            actor_id=payload.source.id,
            action="source.type_mismatch.rejected",
            target_type="source",
            target_id=source.source_key,
            reason="The submitted source type differs from the registered source type.",
            trace_id=trace_id,
            payload={
                "registered_type": source.source_type.value,
                "submitted_type": payload.source.type.value,
            },
        )
        session.commit()
        raise ConflictError(
            "source_type_mismatch",
            "The submitted source type does not match the source registry.",
        )
    if not source.enabled:
        record_audit(
            session,
            actor_type=ActorType.PUBLIC_SOURCE,
            actor_id=payload.source.id,
            action="source.disabled.rejected",
            target_type="source",
            target_id=source.source_key,
            reason="Observation received from a disabled source.",
            trace_id=trace_id,
        )
        session.commit()
        raise ForbiddenError("This source is disabled.")

    credential_required = (
        source.credential_hash is not None or source.trust != SourceTrust.UNVERIFIED
    )
    credential_valid = (
        source_token is not None
        and source.credential_hash is not None
        and hmac.compare_digest(source.credential_hash, sha256_text(source_token))
    )
    if credential_required and not credential_valid:
        record_audit(
            session,
            actor_type=ActorType.PUBLIC_SOURCE,
            actor_id=payload.source.id,
            action="source.authentication.rejected",
            target_type="source",
            target_id=source.source_key,
            reason="Missing or invalid source ingest credential.",
            trace_id=trace_id,
        )
        session.commit()
        raise SourceUnauthorizedError()

    if source.trust != payload.source.trust:
        record_audit(
            session,
            actor_type=ActorType.PUBLIC_SOURCE,
            actor_id=payload.source.id,
            action="source.trust_claim.ignored",
            target_type="source",
            target_id=source.source_key,
            reason="Matching uses the server-side source registry, not a submitted trust claim.",
            trace_id=trace_id,
            payload={
                "registered_trust": source.trust.value,
                "submitted_trust": payload.source.trust.value,
            },
        )
    return source


def process_detection(
    session: Session,
    *,
    payload: DetectionRequest,
    idempotency_key: str,
    source_token: str | None,
    trace_id: str,
    settings: Settings,
) -> DetectionOutcome:
    request_document = payload.model_dump(mode="json", exclude_none=True)
    request_hash = sha256_hex(request_document)
    begin_write_transaction(session)
    source = _resolve_source(
        session,
        payload,
        source_token=source_token,
        trace_id=trace_id,
    )
    replay = find_replay(
        session,
        endpoint=DETECTION_ENDPOINT,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        session.rollback()
        return DetectionOutcome(
            response=DetectionResponse.model_validate(replay.response_body),
            status_code=replay.response_status,
            replayed=True,
        )

    observed_at, received_at = _validate_times(payload, settings)
    longitude, latitude = payload.geometry.coordinates
    search_bbox = bbox_for_point(
        longitude,
        latitude,
        payload.geometry.horizontal_uncertainty_m
        + settings.matching_max_candidate_distance_m
        + settings.matching_max_incident_uncertainty_m,
    )
    candidates, candidate_overflow = find_candidates(
        session,
        bbox=search_bbox,
        limit=settings.matching_max_candidates,
    )
    match = match_observation(
        ObservationForMatch(
            longitude=longitude,
            latitude=latitude,
            uncertainty_m=payload.geometry.horizontal_uncertainty_m,
            observed_at=observed_at,
            toponyms=tuple(payload.context.toponyms),
            canonical_name_hint=payload.context.canonical_name,
            source_trust=source.trust,
        ),
        candidates,
        settings,
        candidate_overflow=candidate_overflow,
    )

    attached_incident: IncidentSeries | None = None
    attached_episode: Episode | None = None
    proposed_incident: IncidentSeries | None = None
    proposed_episode: Episode | None = None

    if match.decision == MatchDecision.CREATE:
        attached_incident, attached_episode = create_incident_and_episode(
            session,
            territory_code=payload.context.territory_code,
            longitude=longitude,
            latitude=latitude,
            uncertainty_m=payload.geometry.horizontal_uncertainty_m,
            canonical_name=payload.context.canonical_name
            or (payload.context.toponyms[0] if payload.context.toponyms else None),
            observed_at=observed_at,
            policy_id=settings.matching_policy_id,
        )
        record_audit(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="incident-service",
            action="incident.created",
            target_type="incident_series",
            target_id=attached_incident.fire_id,
            reason="No reliable existing candidate matched the observation.",
            trace_id=trace_id,
            after=incident_snapshot(attached_incident),
            payload={"episode_id": attached_episode.episode_id},
        )
        record_audit(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="incident-service",
            action="episode.created",
            target_type="episode",
            target_id=f"{attached_incident.fire_id}/{attached_episode.episode_id}",
            reason="A new incident series starts with its first reviewable episode.",
            trace_id=trace_id,
            after=episode_snapshot(attached_episode),
            payload={"fire_id": attached_incident.fire_id},
        )
    elif match.best is not None:
        proposed_incident = session.get(IncidentSeries, match.best.candidate.incident_db_id)
        proposed_episode = session.get(Episode, match.best.candidate.episode_db_id)
        if proposed_incident is None or proposed_episode is None:
            raise ConflictError(
                "candidate_changed",
                "The proposed incident changed during matching; retry the request.",
            )
        if match.decision == MatchDecision.ATTACH:
            attached_incident = proposed_incident
            current_episode = session.execute(
                select(Episode)
                .where(Episode.incident_id == attached_incident.id, Episode.is_current.is_(True))
                .with_for_update()
            ).scalar_one()
            if current_episode.status in {IncidentStatus.EXTINGUISHED, IncidentStatus.CLOSED}:
                before_incident = incident_snapshot(attached_incident)
                before_episode = episode_snapshot(current_episode)
                attached_episode = create_reactivation_episode(
                    session,
                    incident=attached_incident,
                    previous_episode=current_episode,
                    observed_at=observed_at,
                    policy_id=settings.matching_policy_id,
                )
                record_audit(
                    session,
                    actor_type=ActorType.SYSTEM,
                    actor_id="incident-service",
                    action="episode.reactivation.previous_closed",
                    target_type="episode",
                    target_id=f"{attached_incident.fire_id}/{current_episode.episode_id}",
                    reason="A strong match arrived after the previous episode ended.",
                    trace_id=trace_id,
                    before=before_episode,
                    after=episode_snapshot(current_episode),
                    payload={"next_episode_id": attached_episode.episode_id},
                )
                record_audit(
                    session,
                    actor_type=ActorType.SYSTEM,
                    actor_id="incident-service",
                    action="incident.reactivation.updated",
                    target_type="incident_series",
                    target_id=attached_incident.fire_id,
                    reason="A strong match re-opened the incident for review.",
                    trace_id=trace_id,
                    before=before_incident,
                    after=incident_snapshot(attached_incident),
                    payload={"episode_id": attached_episode.episode_id},
                )
                record_audit(
                    session,
                    actor_type=ActorType.SYSTEM,
                    actor_id="incident-service",
                    action="episode.reactivation.created",
                    target_type="episode",
                    target_id=f"{attached_incident.fire_id}/{attached_episode.episode_id}",
                    reason="A strong match arrived after the previous episode ended.",
                    trace_id=trace_id,
                    after=episode_snapshot(attached_episode),
                )
            else:
                # Pending evidence may be attached for review, but it must not refresh
                # the public episode timeline before explicit verification.
                attached_episode = current_episode
            proposed_incident = None
            proposed_episode = None

    observation = Observation(
        observation_id=new_observation_id(),
        source_id=source.id,
        observed_at=observed_at,
        received_at=received_at,
        geometry_type="Point",
        longitude=longitude,
        latitude=latitude,
        altitude_m=payload.geometry.altitude_m,
        vertical_datum=payload.geometry.vertical_datum,
        horizontal_uncertainty_m=payload.geometry.horizontal_uncertainty_m,
        territory_code=payload.context.territory_code,
        toponyms=payload.context.toponyms,
        canonical_name_hint=payload.context.canonical_name,
        evidence_hash=payload.evidence.content_hash,
        evidence_license=payload.evidence.license,
        external_reference=payload.evidence.external_reference,
        request_hash=request_hash,
        verification_state=VerificationState.PENDING_REVIEW,
        attached_incident_id=attached_incident.id if attached_incident else None,
        attached_episode_id=attached_episode.id if attached_episode else None,
        proposed_incident_id=proposed_incident.id if proposed_incident else None,
        proposed_episode_id=proposed_episode.id if proposed_episode else None,
        match_decision=match.decision,
        match_score=match.best.score if match.best else None,
        margin_to_second_candidate=match.margin,
        match_factors=match.best.factors if match.best else {},
        review_reasons=list(match.review_reasons),
        policy_id=settings.matching_policy_id,
        trace_id=trace_id,
        version=1,
    )
    session.add(observation)
    session.flush()

    evidence_result: EvidencePolicyResult | None = None
    if attached_incident is not None and attached_episode is not None:
        before_evidence = {
            "incident": incident_snapshot(attached_incident),
            "episode": episode_snapshot(attached_episode),
        }
        evidence_result = recalculate_episode_evidence(
            session,
            incident=attached_incident,
            episode=attached_episode,
            threshold=settings.corroboration_min_independent_proofs,
            now=utcnow(),
        )
        if evidence_result.became_corroborated:
            record_audit(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id="incident-service",
                action="incident.corroborated",
                target_type="episode",
                target_id=f"{attached_incident.fire_id}/{attached_episode.episode_id}",
                reason="Independent corroborating evidence threshold reached.",
                trace_id=trace_id,
                before=before_evidence,
                after={
                    "incident": incident_snapshot(attached_incident),
                    "episode": episode_snapshot(attached_episode),
                },
                payload={
                    "threshold": settings.corroboration_min_independent_proofs,
                    "independent_proof_count": evidence_result.independent_proof_count,
                    "observation_ids": list(evidence_result.selected_observation_ids),
                },
            )
            emit_outbox(
                session,
                topic="incident.corroborated",
                aggregate_type="incident_series",
                aggregate_id=attached_incident.fire_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                payload={
                    "fire_id": attached_incident.fire_id,
                    "episode_id": attached_episode.episode_id,
                    "independent_proof_count": evidence_result.independent_proof_count,
                },
            )

    record_audit(
        session,
        actor_type=ActorType.PUBLIC_SOURCE,
        actor_id=source.source_key,
        action="observation.processed",
        target_type="observation",
        target_id=observation.observation_id,
        reason="Normalized observation processed by the versioned matching policy.",
        trace_id=trace_id,
        after=observation_snapshot(observation),
        payload={
            "policy_id": settings.matching_policy_id,
            "decision": match.decision.value,
            "score": match.best.score if match.best else None,
            "margin_to_second_candidate": match.margin,
            "review_reasons": list(match.review_reasons),
        },
    )

    aggregate_fire_id = (
        attached_incident.fire_id
        if attached_incident
        else proposed_incident.fire_id
        if proposed_incident
        else observation.observation_id
    )
    emit_outbox(
        session,
        topic="observation.processed",
        aggregate_type=(
            "incident_series" if attached_incident or proposed_incident else "observation"
        ),
        aggregate_id=aggregate_fire_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        payload={
            "observation_id": observation.observation_id,
            "decision": match.decision.value,
            "fire_id": attached_incident.fire_id if attached_incident else None,
            "episode_id": attached_episode.episode_id if attached_episode else None,
            "proposed_fire_id": proposed_incident.fire_id if proposed_incident else None,
            "proposed_episode_id": proposed_episode.episode_id if proposed_episode else None,
            "policy_id": settings.matching_policy_id,
        },
    )

    response = DetectionResponse(
        observation_id=observation.observation_id,
        decision=match.decision,
        fire_id=attached_incident.fire_id if attached_incident else None,
        episode_id=attached_episode.episode_id if attached_episode else None,
        proposed_fire_id=proposed_incident.fire_id if proposed_incident else None,
        proposed_episode_id=proposed_episode.episode_id if proposed_episode else None,
        score=match.best.score if match.best else None,
        margin_to_second_candidate=match.margin,
        factors=match.best.factors if match.best else {},
        distance_m=match.best.distance_m if match.best else None,
        review_reasons=list(match.review_reasons),
        public_confirmation=(
            "corroborated"
            if evidence_result is not None
            and evidence_result.verification_state == VerificationState.CORROBORATED
            else "pending"
        ),
        policy_id=settings.matching_policy_id,
        trace_id=trace_id,
    )
    status_code = 201 if match.decision == MatchDecision.CREATE else 200
    response_body = response.model_dump(mode="json", exclude_none=True)
    store_response(
        session,
        endpoint=DETECTION_ENDPOINT,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=status_code,
        response_body=response_body,
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return DetectionOutcome(response=response, status_code=status_code, replayed=False)
