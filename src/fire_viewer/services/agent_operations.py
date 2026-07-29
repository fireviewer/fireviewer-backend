"""Small operator control plane for already-persisted private analysis batches."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentAnalysisWindow,
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
    AgentOperationWindow,
)
from fire_viewer.domain.enums import (
    AgentAnalysisState,
    AgentBatchState,
    AgentBatchType,
    AgentConsentState,
    AgentSourceResearchState,
    AgentValidationCampaignDayState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.services.agent_batches import enqueue_agent_batch
from fire_viewer.services.agent_source_research import create_source_research
from fire_viewer.services.agent_validation_campaigns import (
    ActiveAnalysisWindow,
    batch_is_allowed_for_active_campaign,
    mark_running,
    resolve_active_analysis_window,
    resolve_requested_analysis_window,
)

_ACTION_ORDER: tuple[AgentOperationType, ...] = (
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


def _operation_schedule_state(
    active_window: ActiveAnalysisWindow,
    operation_type: AgentOperationType,
) -> str:
    day = active_window.campaign_day
    if day is None or operation_type in set(day.required_operations):
        return "required"
    if operation_type in set(day.declared_absences):
        return "declared_absent"
    return "not_scheduled"


def _require_scheduled_operation(
    active_window: ActiveAnalysisWindow,
    operation_type: AgentOperationType,
) -> None:
    schedule_state = _operation_schedule_state(active_window, operation_type)
    if schedule_state == "declared_absent":
        raise ConflictError(
            "agent_operation_declared_absent",
            "This operation is explicitly absent from the active analysis window.",
        )
    if schedule_state == "not_scheduled":
        raise ConflictError(
            "agent_operation_not_scheduled",
            "This operation is not scheduled in the active analysis window.",
        )


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
    analysis_window_id: int,
) -> list[AgentMediaBatch]:
    return list(
        session.execute(
            select(AgentMediaBatch)
            .where(
                AgentMediaBatch.incident_id == incident_id,
                AgentMediaBatch.episode_id == episode_id,
                AgentMediaBatch.analysis_window_id == analysis_window_id,
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
    analysis_window_id: int,
) -> list[AgentSourceResearchRun]:
    return list(
        session.scalars(
            select(AgentSourceResearchRun)
            .where(
                AgentSourceResearchRun.incident_id == incident_id,
                AgentSourceResearchRun.episode_id == episode_id,
                AgentSourceResearchRun.analysis_window_id == analysis_window_id,
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
    active_window: ActiveAnalysisWindow,
    batches: list[AgentMediaBatch],
    research_runs: list[AgentSourceResearchRun],
    settings: Settings,
) -> AgentOperationsOverview:
    actions: list[AgentOperationStatus] = []
    for operation_type in _ACTION_ORDER:
        schedule_state = _operation_schedule_state(active_window, operation_type)
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
            if schedule_state == "declared_absent":
                blocked_reason = "operation_declared_absent"
            elif schedule_state == "not_scheduled":
                blocked_reason = "operation_not_scheduled"
            elif not settings.agent_dispatch_enabled:
                blocked_reason = "dispatch_disabled"
            elif not settings.agent_research_enabled:
                blocked_reason = "research_disabled"
            elif active_runs:
                blocked_reason = "already_running"
            actions.append(
                AgentOperationStatus(
                    operation_type="source_research",
                    schedule_state=schedule_state,
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
        if schedule_state == "declared_absent":
            blocked_reason = "operation_declared_absent"
        elif schedule_state == "not_scheduled":
            blocked_reason = "operation_not_scheduled"
        elif not settings.agent_dispatch_enabled:
            blocked_reason = "dispatch_disabled"
        elif any(batch.state in _ACTIVE_STATES for batch in matching):
            blocked_reason = "already_running"
        elif not pending:
            blocked_reason = "already_completed" if matching else "input_not_ready"
        actions.append(
            AgentOperationStatus(
                operation_type=operation_type,
                schedule_state=schedule_state,
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
        analysis_window_id=active_window.window.analysis_id,
        local_date=active_window.window.local_date,
        campaign_day_state=(
            active_window.campaign_day.state.value if active_window.campaign_day else None
        ),
        actions=actions,
        available_windows=[],
    )


def _overview_window(
    *,
    incident: IncidentSeries,
    episode: Episode,
    active_window: ActiveAnalysisWindow,
    settings: Settings,
    session: Session,
) -> AgentOperationWindow:
    batches = [
        batch
        for batch in _batches_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            analysis_window_id=active_window.window.id,
        )
        if batch_is_allowed_for_active_campaign(batch, active_window)
    ]
    overview = _overview(
        incident=incident,
        episode=episode,
        active_window=active_window,
        batches=batches,
        research_runs=_research_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            analysis_window_id=active_window.window.id,
        ),
        settings=settings,
    )
    return AgentOperationWindow(
        analysis_window_id=overview.analysis_window_id,
        local_date=overview.local_date,
        campaign_day_state=overview.campaign_day_state,
        actions=overview.actions,
    )


def _available_operation_windows(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    settings: Settings,
) -> list[AgentOperationWindow]:
    """Expose manifest-bound runnable windows; dates are informational only."""

    from fire_viewer.services.agent_validation_campaigns import active_campaign

    campaign = active_campaign(session)
    if campaign is None:
        windows = list(
            session.scalars(
                select(AgentAnalysisWindow)
                .where(
                    AgentAnalysisWindow.incident_id == incident.id,
                    AgentAnalysisWindow.episode_id == episode.id,
                    AgentAnalysisWindow.state.not_in(
                        [
                            AgentAnalysisState.COMPLETED,
                            AgentAnalysisState.CANCELLED,
                        ]
                    ),
                )
                .order_by(
                    AgentAnalysisWindow.local_date.asc(),
                    AgentAnalysisWindow.id.asc(),
                )
                .limit(1_000)
            )
        )
        return [
            _overview_window(
                incident=incident,
                episode=episode,
                active_window=ActiveAnalysisWindow(window=window, campaign_day=None),
                settings=settings,
                session=session,
            )
            for window in windows
        ]
    days = sorted(
        (
            day
            for day in campaign.days
            if day.analysis_window.incident_id == incident.id
            and day.analysis_window.episode_id == episode.id
            and day.state
            in {
                AgentValidationCampaignDayState.READY,
                AgentValidationCampaignDayState.RUNNING,
            }
        ),
        key=lambda day: (day.analysis_window.local_date, day.ordinal),
    )
    return [
        _overview_window(
            incident=incident,
            episode=episode,
            active_window=ActiveAnalysisWindow(window=day.analysis_window, campaign_day=day),
            settings=settings,
            session=session,
        )
        for day in days
    ]


def get_agent_operations(
    session: Session,
    *,
    fire_id: str,
    settings: Settings,
) -> AgentOperationsOverview:
    incident, episode = _incident_episode(session, fire_id)
    active = resolve_active_analysis_window(session, incident=incident, episode=episode)
    batches = [
        batch
        for batch in _batches_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            analysis_window_id=active.window.id,
        )
        if batch_is_allowed_for_active_campaign(batch, active)
    ]
    overview = _overview(
        incident=incident,
        episode=episode,
        active_window=active,
        batches=batches,
        research_runs=_research_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            analysis_window_id=active.window.id,
        ),
        settings=settings,
    )
    return overview.model_copy(
        update={
            "available_windows": _available_operation_windows(
                session,
                incident=incident,
                episode=episode,
                settings=settings,
            )
        }
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
    active = resolve_requested_analysis_window(
        session,
        incident=incident,
        episode=episode,
        expected_analysis_window_id=payload.expected_analysis_window_id,
    )
    _require_scheduled_operation(active, operation_type)
    if operation_type == "source_research":
        active_research = [
            run
            for run in _research_for_episode(
                session,
                incident_id=incident.id,
                episode_id=episode.id,
                analysis_window_id=active.window.id,
            )
            if run.state
            in {
                AgentSourceResearchState.QUEUED,
                AgentSourceResearchState.SUBMITTING,
                AgentSourceResearchState.RUNNING,
                AgentSourceResearchState.CANCEL_REQUESTED,
            }
        ]
        if active_research:
            return AgentOperationRunResponse(
                fire_id=incident.fire_id,
                episode_id=episode.episode_id,
                analysis_window_id=active.window.analysis_id,
                operation_type=operation_type,
                operation_ids=[run.research_id for run in active_research],
                queued_files=0,
            )
        research = create_source_research(
            session,
            fire_id=fire_id,
            expected_analysis_window_id=active.window.analysis_id,
            location_hint=incident.canonical_name or fire_id,
            actor=actor,
            trace_id=trace_id,
            settings=settings,
        )
        mark_running(session, active)
        session.commit()
        return AgentOperationRunResponse(
            fire_id=incident.fire_id,
            episode_id=episode.episode_id,
            analysis_window_id=active.window.analysis_id,
            operation_type=operation_type,
            operation_ids=[research.research_id],
            queued_files=0,
        )
    batch_type = AgentBatchType(operation_type)
    matching = [
        batch
        for batch in _batches_for_episode(
            session,
            incident_id=incident.id,
            episode_id=episode.id,
            analysis_window_id=active.window.id,
        )
        if batch.batch_type == batch_type and batch_is_allowed_for_active_campaign(batch, active)
    ]
    candidates = [batch for batch in matching if _is_processable(batch)]
    if not candidates:
        already_running = [batch for batch in matching if batch.state in _ACTIVE_STATES]
        if already_running:
            return AgentOperationRunResponse(
                fire_id=incident.fire_id,
                episode_id=episode.episode_id,
                analysis_window_id=active.window.analysis_id,
                operation_type=operation_type,
                operation_ids=[batch.batch_id for batch in already_running],
                queued_files=sum(len(batch.items) for batch in already_running),
            )
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
    mark_running(session, active)
    session.commit()
    return AgentOperationRunResponse(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        analysis_window_id=active.window.analysis_id,
        operation_type=operation_type,
        operation_ids=queued_batch_ids,
        queued_files=queued_files,
    )
