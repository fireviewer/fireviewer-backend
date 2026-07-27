"""Small operator control plane for already-persisted private analysis batches."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import AgentMediaBatch, AgentMediaItem, Episode, IncidentSeries
from fire_viewer.domain.agent_schemas import (
    AgentOperationRunResponse,
    AgentOperationsOverview,
    AgentOperationStatus,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentBatchType,
    AgentConsentState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.services.agent_batches import enqueue_agent_batch

_ACTION_ORDER = (
    AgentBatchType.USER_MEDIA,
    AgentBatchType.EXTERNAL_MEDIA,
    AgentBatchType.SATELLITE_MEDIA,
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
    session: Session, *, incident_id: int, episode_id: int
) -> list[AgentMediaBatch]:
    return list(
        session.execute(
            select(AgentMediaBatch)
            .where(
                AgentMediaBatch.incident_id == incident_id,
                AgentMediaBatch.episode_id == episode_id,
            )
            .options(
                selectinload(AgentMediaBatch.items).selectinload(AgentMediaItem.consent),
                selectinload(AgentMediaBatch.dispatch),
            )
            .order_by(AgentMediaBatch.created_at.asc(), AgentMediaBatch.id.asc())
            .limit(1_000)
        ).scalars()
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
    batches: list[AgentMediaBatch],
    settings: Settings,
) -> AgentOperationsOverview:
    actions: list[AgentOperationStatus] = []
    for batch_type in _ACTION_ORDER:
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
                batch_type=batch_type,
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
        actions=actions,
    )


def get_agent_operations(
    session: Session, *, fire_id: str, settings: Settings
) -> AgentOperationsOverview:
    incident, episode = _incident_episode(session, fire_id)
    return _overview(
        incident=incident,
        episode=episode,
        batches=_batches_for_episode(session, incident_id=incident.id, episode_id=episode.id),
        settings=settings,
    )


def run_agent_operation(
    session: Session,
    *,
    fire_id: str,
    batch_type: AgentBatchType,
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
    candidates = [
        batch
        for batch in _batches_for_episode(session, incident_id=incident.id, episode_id=episode.id)
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
        batch_type=batch_type,
        queued_batch_ids=queued_batch_ids,
        queued_files=queued_files,
    )
