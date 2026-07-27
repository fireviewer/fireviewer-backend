"""Private administration API for dedicated agent media batches."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from fire_viewer.api.dependencies import (
    ActorDep,
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    TraceIdDep,
)
from fire_viewer.core.security import Actor, require_role
from fire_viewer.domain.agent_schemas import (
    AgentBatchCreatePayload,
    AgentBatchResponse,
    AgentConsentWithdrawRequest,
    AgentConsentWithdrawResponse,
    AgentOperationRunResponse,
    AgentOperationsOverview,
)
from fire_viewer.domain.enums import AgentBatchType
from fire_viewer.services.agent_batches import (
    create_agent_batch,
    enqueue_agent_batch,
    get_agent_batch,
    withdraw_agent_consent,
)
from fire_viewer.services.agent_operations import get_agent_operations, run_agent_operation

router = APIRouter(prefix="/api/v2/admin/agent-batches", tags=["admin-agent-batches"])
SafeIdPath = Annotated[
    str,
    Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]


def _require_agent_operator(actor: Actor) -> None:
    require_role(actor, "administrator", "analyst", "validator", "security_operator")


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post(
    "",
    response_model=AgentBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_batch(
    payload: AgentBatchCreatePayload,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AgentBatchResponse:
    _require_agent_operator(actor)
    outcome = create_agent_batch(
        session,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    if outcome.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return outcome.batch


@router.get(
    "/incidents/{fire_id}/operations",
    response_model=AgentOperationsOverview,
)
def read_incident_operations(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AgentOperationsOverview:
    _require_agent_operator(actor)
    _private(response)
    return get_agent_operations(session, fire_id=fire_id, settings=settings)


@router.post(
    "/incidents/{fire_id}/operations/{batch_type}/run",
    response_model=AgentOperationRunResponse,
)
def run_incident_operation(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    batch_type: AgentBatchType,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentOperationRunResponse:
    _require_agent_operator(actor)
    result = run_agent_operation(
        session,
        fire_id=fire_id,
        batch_type=batch_type,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    return result


@router.get("/{batch_id}", response_model=AgentBatchResponse)
def read_batch(
    batch_id: SafeIdPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AgentBatchResponse:
    _require_agent_operator(actor)
    _private(response)
    return get_agent_batch(session, batch_id)


@router.post("/{batch_id}/enqueue", response_model=AgentBatchResponse)
def enqueue_batch(
    batch_id: SafeIdPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentBatchResponse:
    _require_agent_operator(actor)
    outcome = enqueue_agent_batch(
        session,
        batch_id=batch_id,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    if outcome.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return outcome.batch


@router.post(
    "/{batch_id}/items/{input_id}/consent/withdraw",
    response_model=AgentConsentWithdrawResponse,
)
def withdraw_consent(
    batch_id: SafeIdPath,
    input_id: SafeIdPath,
    payload: AgentConsentWithdrawRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> AgentConsentWithdrawResponse:
    _require_agent_operator(actor)
    result = withdraw_agent_consent(
        session,
        batch_id=batch_id,
        input_id=input_id,
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    _private(response)
    return result
