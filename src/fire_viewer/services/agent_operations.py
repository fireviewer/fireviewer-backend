"""Small operator control plane for already-persisted private analysis batches."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentMediaBatch,
    AgentMediaItem,
    AgentSourceResearchRun,
    Episode,
    IncidentSeries,
)
from fire_viewer.domain.agent_schemas import (
    AgentOperationRunRequest,
    AgentOperationRunResponse,
    AgentOperationsOverview,
    AgentOperationStatus,
    AgentOperationType,
    AgentSourceResearchRequest,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentBatchType,
    AgentConsentState,
    AgentSourceResearchState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.services.agent_batches import enqueue_agent_batch
from fire_viewer.services.agent_source_research import create_source_research

_ACTION_ORDER = (
    "user_media",
    "source_research",
    "satellite_media",
)
_ACTIVE_STATES = {
    AgentBatchState.QUEUED,
    AgentBatchState.SUBMITTING,
    AgentBatchState.RUNNING,
    AgentBatchState.CANCEL_REQUESTED,
}
_REQUIRED_SCOPES = {"temporary_storage", "agent_analysis", "human_review"}


def _incident_episode(session: Session, fire_id: str) -> tuple[IncidentSeries, Episode]:
    incident = session.execute(
        select(IncidentSeries).where(IncidentSeries.fire_id == fire_id)
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = session.execute(
        select(Episode).where(
            Episode.incident_id == incident.id,
            Episode.is_current.is_(True),
        )
    ).scalar_one_or_none()
    if episode is None:
        raise ConflictError("incident_without_current_episode", "Incident has no current episode.")
    return incident, episode


def _batches_for_episode(
    session: Session,
    *,
    incident_id: int,
    episode_id: int,
    local_date: date,
) -> list[AgentMediaBatch]:
    return list(
        session.execute(
            select(AgentMediaBatch)
            .where(
                AgentMediaBatch.incident_id == incident_id,
                AgentMediaBatch.episode_id == episode_id,
                AgentMediaBatch.analysis_window.has(local_date=local_date),
            )
            .options(
                selectinload(AgentMediaBatch.items).selectinload(AgentMediaItem.consent),
                selectinload(AgentMediaBatch.dispatch),
            )
            .order_by(AgentMediaBatch.created_at.asc(), AgentMediaBatch.id.asc())
            .limit(1_000)
        ).scalars()
    )


def _research_for_episode(
    session: Session,
    *,
    incident_id: int,
    episode_id: int,
    local_date: date,
) -> list[AgentSourceResearchRun]:
    return list(
        session.scalars(
            select(AgentSourceResearchRun)
            .where(
                AgentSourceResearchRun.incident_id == incident_id,
                AgentSourceResearchRun.episode_id == episode_id,
                AgentSourceResearchRun.analysis_window.has(local_date=local_date),
            )
            .order_by(
                AgentSourceResearchRun.queued_at.asc(),
                AgentSourceResearchRun.id.asc(),
            )
            .limit(100)
        )
    )


def _is_processable(batch: AgentMediaBatch) -> bool:
    if batch.state != AgentBatchState.DRAFT or not batch.items:
        return False
    now = utcnow()
    if batch.deadline_at is not None and as_utc(batch.deadline_at) <= now:
        return False
    for item in batch.items:
        consent = item.consent
        if item.purged_at is not None or consent.state != AgentConsentState.GRANTED:
            return False
        if consent.expires_at is not None and as_utc(consent.expires_at) <= now:
            return False
        if not _REQUIRED_SCOPES.issubset(set(consent.scopes)):
            return False
    return True


def _overview(
    *,
    incident: IncidentSeries,
    episode: Episode,
    local_date: date,
    batches: list[AgentMediaBatch],
    research_runs: list[AgentSourceResearchRun],
    settings: Settings,
) -> AgentOperationsOverview:
    actions: list[AgentOperationStatus] = []
    for operation_type in _ACTION_ORDER:
        if operation_type == "source_research":
            active_runs = [
                run
                for run in research_runs
                if run.state
                in {
                    AgentSourceResearchState.QUEUED,
                    AgentSourceResearchState.SUBMITTING,
                    AgentSourceResearchState.RUNNING,
                    AgentSourceResearchState.CANCEL_REQUESTED,
                }
            ]
            blocked_reason = None
            if not settings.agent_dispatch_enabled:
                blocked_reason = "dispatch_disabled"
            elif not settings.agent_research_enabled:
                blocked_reason = "research_disabled"
            elif active_runs:
                blocked_reason = "already_running"
            actions.append(
                AgentOperationStatus(
                    operation_type="source_research",
                    pending_files=0,
                    pending_analyses=0 if active_runs else 1,
                    running_analyses=len(active_runs),
                    last_run_at=(
                        max(as_utc(run.queued_at) for run in research_runs)
                        if research_runs
                        else None
                    ),
                    can_run=blocked_reason is None,
                    blocked_reason=blocked_reason,
                )
            )
            continue
        batch_type = AgentBatchType(operation_type)
        matching = [batch for batch in batches if batch.batch_type == batch_type]
        pending = [batch for batch in matching if _is_processable(batch)]
        submitted = [as_utc(batch.submitted_at) for batch in matching if batch.submitted_at]
        pending_files = sum(len(batch.items) for batch in pending)
        blocked_reason = None
        if not settings.agent_dispatch_enabled:
            blocked_reason = "dispatch_disabled"
        elif not pending:
            blocked_reason = "nothing_to_process"
        actions.append(
            AgentOperationStatus(
                operation_type=operation_type,
                pending_files=pending_files,
                pending_analyses=len(pending),
                running_analyses=sum(batch.state in _ACTIVE_STATES for batch in matching),
                last_run_at=max(submitted) if submitted else None,
                can_run=blocked_reason is None,
                blocked_reason=blocked_reason,
            )
        )
    return AgentOperationsOverview(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        local_date=local_date,
        actions=actions,
    )


def get_agent_operations(
    session: Session,
    *,
    fire_id: str,
    local_date: date,
    settings: Settings,
) -> AgentOperationsOverview:
    incident, episode = _incident_episode(session, fire_id)
    return _overview(
        incident=incident,
        episode=episode,
        local_date=local_date,
        batches=_batches_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            local_date=local_date,
        ),
        research_runs=_research_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            local_date=local_date,
        ),
        settings=settings,
    )


def run_agent_operation(
    session: Session,
    *,
    fire_id: str,
    operation_type: AgentOperationType,
    payload: AgentOperationRunRequest,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AgentOperationRunResponse:
    if not settings.agent_dispatch_enabled:
        raise ConflictError(
            "agent_dispatch_disabled",
            "The private inference dispatcher is not enabled.",
        )
    incident, episode = _incident_episode(session, fire_id)
    if operation_type == "source_research":
        research = create_source_research(
            session,
            fire_id=fire_id,
            payload=AgentSourceResearchRequest(
                local_date=payload.local_date,
                location_hint=payload.location_hint,
            ),
            actor=actor,
            trace_id=trace_id,
            settings=settings,
        )
        return AgentOperationRunResponse(
            fire_id=incident.fire_id,
            episode_id=episode.episode_id,
            operation_type=operation_type,
            operation_ids=[research.research_id],
            queued_files=0,
        )
    batch_type = AgentBatchType(operation_type)
    candidates = [
        batch
        for batch in _batches_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            local_date=payload.local_date,
        )
        if batch.batch_type == batch_type and _is_processable(batch)
    ]
    if not candidates:
        raise ConflictError(
            "agent_analysis_nothing_to_run",
            "No processable private batch is waiting for this analysis.",
        )
    queued_batch_ids: list[str] = []
    queued_files = 0
    for batch in candidates:
        outcome = enqueue_agent_batch(
            session,
            batch_id=batch.batch_id,
            actor=actor,
            trace_id=trace_id,
            settings=settings,
        )
        queued_batch_ids.append(outcome.batch.batch_id)
        queued_files += len(outcome.batch.items)
    return AgentOperationRunResponse(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        operation_type=operation_type,
        operation_ids=queued_batch_ids,
        queued_files=queued_files,
    )
