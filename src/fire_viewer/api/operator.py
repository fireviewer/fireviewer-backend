from fastapi import APIRouter, Response

from fire_viewer.api.dependencies import (
    ActorDep,
    FireIdDep,
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    SourceKeyDep,
    TraceIdDep,
)
from fire_viewer.core.security import require_role
from fire_viewer.domain.schemas import (
    OperationalProfileRequest,
    OperationalProfileResponse,
    ReviewResolutionRequest,
    ReviewResolutionResponse,
    SourceResponse,
    SourceUpsertRequest,
    TransitionRequest,
    TransitionResponse,
)
from fire_viewer.services.operational_profile import update_operational_profile
from fire_viewer.services.reviews import resolve_review
from fire_viewer.services.source_registry import upsert_source
from fire_viewer.services.transitions import transition_incident

router = APIRouter(prefix="/operator", tags=["operator"])


@router.put("/sources/{source_key}", response_model=SourceResponse)
def register_source(
    source_key: SourceKeyDep,
    payload: SourceUpsertRequest,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> SourceResponse:
    require_role(actor, "administrator")
    return upsert_source(
        session,
        source_key=source_key,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
    )


@router.post(
    "/observations/{observation_id}/resolve",
    response_model=ReviewResolutionResponse,
)
def resolve_observation_review(
    observation_id: str,
    payload: ReviewResolutionRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> ReviewResolutionResponse:
    require_role(actor, "validator")
    outcome = resolve_review(
        session,
        observation_id=observation_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    response.headers["Cache-Control"] = "no-store"
    return outcome.response


@router.post(
    "/incidents/{fire_id}/transitions",
    response_model=TransitionResponse,
)
def transition_episode(
    fire_id: FireIdDep,
    payload: TransitionRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> TransitionResponse:
    outcome = transition_incident(
        session,
        fire_id=fire_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    response.headers["Cache-Control"] = "no-store"
    return outcome.response


@router.post(
    "/incidents/{fire_id}/operational-profile",
    response_model=OperationalProfileResponse,
)
def set_operational_profile(
    fire_id: FireIdDep,
    payload: OperationalProfileRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> OperationalProfileResponse:
    require_role(actor, "validator")
    outcome = update_operational_profile(
        session,
        fire_id=fire_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    response.headers["Cache-Control"] = "no-store"
    return outcome.response
