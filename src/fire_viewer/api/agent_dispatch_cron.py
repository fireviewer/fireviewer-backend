from __future__ import annotations

from hmac import compare_digest

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from fire_viewer.api.dependencies import SettingsDep, TraceIdDep
from fire_viewer.domain.errors import DomainError
from fire_viewer.services.agent_orchestration import run_public_source_schedule_once
from fire_viewer.services.external_source_scheduler import run_external_source_scheduler_once
from fire_viewer.services.hosted_agent_dispatcher import process_one_hosted_dispatch
from fire_viewer.services.official_connectors import build_official_connector_registry
from fire_viewer.services.public_contribution_schedule import (
    run_public_contribution_schedule_once,
)

router = APIRouter(tags=["internal"])


class AgentDispatchTickResponse(BaseModel):
    processed: bool
    scheduled: int = 0


def _authorize_cron(request: Request, settings: SettingsDep) -> None:
    if settings.cron_secret is None:
        raise DomainError(
            status_code=503,
            code="agent_cron_not_configured",
            title="Agent dispatcher unavailable",
            detail="The hosted agent dispatcher is not configured.",
        )
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {settings.cron_secret.get_secret_value()}"
    if not compare_digest(supplied, expected):
        raise DomainError(
            status_code=401,
            code="agent_cron_unauthorized",
            title="Unauthorized",
            detail="The dispatcher request is not authorized.",
        )


def _ensure_enabled(settings: SettingsDep) -> None:
    if not settings.agent_dispatch_enabled:
        raise DomainError(
            status_code=503,
            code="agent_dispatch_disabled",
            title="Agent dispatcher unavailable",
            detail="Private agent dispatch is disabled.",
        )


def _ensure_official_connectors_enabled(settings: SettingsDep) -> None:
    if not settings.official_connectors_enabled:
        raise DomainError(
            status_code=503,
            code="official_connectors_disabled",
            title="Official source scheduler unavailable",
            detail="Official source connectors are disabled.",
        )


@router.get(
    "/internal/agent-orchestrator/progress",
    response_model=AgentDispatchTickResponse,
    include_in_schema=False,
)
def progress_hosted_agent_dispatcher(
    request: Request,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentDispatchTickResponse:
    _authorize_cron(request, settings)
    _ensure_enabled(settings)
    worker_id = f"vercel-cron:{trace_id}"
    processed = process_one_hosted_dispatch(
        request.app.state.session_factory,
        worker_id=worker_id,
        settings=settings,
    )
    return AgentDispatchTickResponse(processed=processed)


@router.get(
    "/internal/agent-orchestrator/contributions",
    response_model=AgentDispatchTickResponse,
    include_in_schema=False,
)
def schedule_public_contributions(
    request: Request,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentDispatchTickResponse:
    _authorize_cron(request, settings)
    _ensure_enabled(settings)
    worker_id = f"vercel-contributions:{trace_id}"
    with request.app.state.session_factory() as session:
        scheduled = run_public_contribution_schedule_once(
            session,
            worker_id=worker_id,
            settings=settings,
        )
    processed = process_one_hosted_dispatch(
        request.app.state.session_factory,
        worker_id=worker_id,
        settings=settings,
    )
    return AgentDispatchTickResponse(processed=processed, scheduled=scheduled)


@router.get(
    "/internal/agent-orchestrator/media",
    response_model=AgentDispatchTickResponse,
    include_in_schema=False,
)
def schedule_public_source_research(
    request: Request,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentDispatchTickResponse:
    _authorize_cron(request, settings)
    _ensure_enabled(settings)
    worker_id = f"vercel-media-research:{trace_id}"
    with request.app.state.session_factory() as session:
        scheduled = run_public_source_schedule_once(
            session,
            worker_id=worker_id,
            settings=settings,
        )
    processed = process_one_hosted_dispatch(
        request.app.state.session_factory,
        worker_id=worker_id,
        settings=settings,
    )
    return AgentDispatchTickResponse(processed=processed, scheduled=scheduled)


@router.get(
    "/internal/external-sources/progress",
    response_model=AgentDispatchTickResponse,
    include_in_schema=False,
)
def progress_external_source_scheduler(
    request: Request,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> AgentDispatchTickResponse:
    _authorize_cron(request, settings)
    _ensure_official_connectors_enabled(settings)
    worker_id = f"vercel-official-sources:{trace_id}"
    with httpx.Client(trust_env=False) as client:
        connectors = build_official_connector_registry(settings, client=client)
        processed = run_external_source_scheduler_once(
            request.app.state.session_factory,
            settings=settings,
            worker_id=worker_id,
            connectors=connectors,
        )
    return AgentDispatchTickResponse(processed=processed)
