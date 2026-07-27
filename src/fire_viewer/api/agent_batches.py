"""Private administration API for dedicated agent media batches."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response, status

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
    AgentDispatcherTickResponse,
    AgentOperationRunRequest,
    AgentOperationRunResponse,
    AgentOperationsOverview,
    AgentOperationType,
    AgentSourcePackageOpenRequest,
    AgentSourcePackageOpenResponse,
    AgentSourcePackageResponse,
    AgentSourceResearchRequest,
    AgentSourceResearchResponse,
)
from fire_viewer.domain.errors import ConflictError
from fire_viewer.services.agent_batches import (
    create_agent_batch,
    enqueue_agent_batch,
    get_agent_batch,
    withdraw_agent_consent,
)
from fire_viewer.services.agent_operations import get_agent_operations, run_agent_operation
from fire_viewer.services.agent_source_packages import (
    finalize_source_package,
    get_source_package,
    open_source_package,
)
from fire_viewer.services.agent_source_research import (
    create_source_research,
    get_source_research,
)
from fire_viewer.services.hosted_agent_dispatcher import process_one_hosted_dispatch

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
    "/incidents/{fire_id}/source-packages/open",
    response_model=AgentSourcePackageOpenResponse,
    status_code=status.HTTP_201_CREATED,
)
def open_incident_source_package(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    payload: AgentSourcePackageOpenRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AgentSourcePackageOpenResponse:
    _require_agent_operator(actor)
    result = open_source_package(
        session,
        fire_id=fire_id,
        payload=payload,
        idempotency_key=idempotency_key,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    return result


@router.post(
    "/source-packages/{package_id}/finalize",
    response_model=AgentSourcePackageResponse,
)
def finalize_incident_source_package(
    package_id: SafeIdPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentSourcePackageResponse:
    _require_agent_operator(actor)
    result = finalize_source_package(
        session,
        package_id=package_id,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    return result


@router.get(
    "/source-packages/{package_id}",
    response_model=AgentSourcePackageResponse,
)
def read_incident_source_package(
    package_id: SafeIdPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AgentSourcePackageResponse:
    _require_agent_operator(actor)
    _private(response)
    return get_source_package(session, package_id)


@router.post(
    "/incidents/{fire_id}/source-research",
    response_model=AgentSourceResearchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_incident_source_research(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    payload: AgentSourceResearchRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentSourceResearchResponse:
    _require_agent_operator(actor)
    result = create_source_research(
        session,
        fire_id=fire_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    return result


@router.get(
    "/source-research/{research_id}",
    response_model=AgentSourceResearchResponse,
)
def read_incident_source_research(
    research_id: SafeIdPath,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
) -> AgentSourceResearchResponse:
    _require_agent_operator(actor)
    _private(response)
    return get_source_research(session, research_id)


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
    local_date: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
) -> AgentOperationsOverview:
    _require_agent_operator(actor)
    _private(response)
    from datetime import date

    return get_agent_operations(
        session,
        fire_id=fire_id,
        local_date=date.fromisoformat(local_date),
        settings=settings,
    )


@router.post(
    "/incidents/{fire_id}/operations/{operation_type}/run",
    response_model=AgentOperationRunResponse,
)
def run_incident_operation(
    fire_id: Annotated[str, Path(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")],
    operation_type: AgentOperationType,
    payload: AgentOperationRunRequest,
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
        operation_type=operation_type,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    _private(response)
    return result


@router.post("/dispatcher/tick", response_model=AgentDispatcherTickResponse)
def tick_agent_dispatcher(
    request: Request,
    response: Response,
    actor: ActorDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentDispatcherTickResponse:
    require_role(actor, "administrator", "security_operator")
    if not settings.agent_dispatch_enabled:
        raise ConflictError(
            "agent_dispatch_disabled",
            "The private inference dispatcher is not enabled.",
        )
    processed = process_one_hosted_dispatch(
        request.app.state.session_factory,
        worker_id=f"admin-dispatcher:{trace_id}",
        settings=settings,
    )
    _private(response)
    return AgentDispatcherTickResponse(processed=processed)


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
