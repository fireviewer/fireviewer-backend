"""Low-frequency intake for private public-contribution media batches.

The dispatcher still owns remote execution.  This scheduler only promotes
already-finalized, consented evidence from DRAFT to QUEUED every three hours.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentMediaBatch,
    AgentMediaItem,
    AgentScheduleRun,
    AgentSourcePackage,
    AgentSourcePackageItem,
    PublicContributionSubmission,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentValidationCampaignDayState,
    PublicContributionState,
)
from fire_viewer.domain.errors import ConflictError
from fire_viewer.services.agent_batches import enqueue_agent_batch
from fire_viewer.services.agent_validation_campaigns import (
    ActiveAnalysisWindow,
    active_campaign,
    batch_is_allowed_for_active_campaign,
)

_SCHEDULE_KEY = "public-contribution-intake-v1"


def run_public_contribution_schedule_once(
    session: Session, *, worker_id: str, settings: Settings
) -> int:
    """Queue currently admissible private evidence once per durable interval."""

    now = utcnow()
    schedule = session.execute(
        select(AgentScheduleRun)
        .where(AgentScheduleRun.schedule_key == _SCHEDULE_KEY)
        .with_for_update()
    ).scalar_one_or_none()
    if schedule is None:
        schedule = AgentScheduleRun(
            schedule_key=_SCHEDULE_KEY,
            next_run_at=now,
            last_run_at=None,
            lease_owner=None,
            lease_until=None,
        )
        session.add(schedule)
        try:
            session.flush()
        except IntegrityError:
            # Another dispatcher won the initial row creation. Re-read the
            # durable watermark instead of emitting a second batch submission.
            session.rollback()
            schedule = session.execute(
                select(AgentScheduleRun)
                .where(AgentScheduleRun.schedule_key == _SCHEDULE_KEY)
                .with_for_update()
            ).scalar_one()
    if as_utc(schedule.next_run_at) > now:
        session.commit()
        return 0
    schedule.last_run_at = now
    schedule.next_run_at = now + timedelta(
        seconds=settings.public_contribution_agent_interval_seconds
    )
    schedule.lease_owner = worker_id
    schedule.lease_until = now + timedelta(seconds=settings.agent_dispatch_lease_seconds)
    session.commit()

    batches = (
        session.execute(
            select(AgentMediaBatch)
            .join(AgentMediaItem, AgentMediaItem.batch_id == AgentMediaBatch.id)
            .join(
                AgentSourcePackageItem,
                AgentSourcePackageItem.agent_media_item_id == AgentMediaItem.id,
            )
            .join(AgentSourcePackage, AgentSourcePackage.id == AgentSourcePackageItem.package_id)
            .join(
                PublicContributionSubmission,
                PublicContributionSubmission.source_package_id == AgentSourcePackage.id,
            )
            .where(
                PublicContributionSubmission.state.in_(
                    (PublicContributionState.PENDING, PublicContributionState.ACCEPTED)
                ),
                AgentMediaBatch.state == AgentBatchState.DRAFT,
            )
            .options(selectinload(AgentMediaBatch.items).selectinload(AgentMediaItem.consent))
            .order_by(AgentMediaBatch.created_at.asc(), AgentMediaBatch.batch_id.asc())
        )
        .scalars()
        .unique()
        .all()
    )
    actor = Actor(actor_id="agent-scheduler:public-contributions", roles=frozenset())
    campaign = active_campaign(session)
    campaign_window: ActiveAnalysisWindow | None = None
    if campaign is not None:
        active_days = [
            day
            for day in campaign.days
            if day.state
            in {
                AgentValidationCampaignDayState.READY,
                AgentValidationCampaignDayState.RUNNING,
            }
        ]
        if len(active_days) != 1:
            raise ConflictError(
                "agent_campaign_active_window_invalid",
                "The internal campaign must expose exactly one runnable analysis window.",
            )
        campaign_window = ActiveAnalysisWindow(
            window=active_days[0].analysis_window,
            campaign_day=active_days[0],
        )
    queued = 0
    for batch in batches:
        if campaign_window is not None and (
            batch.analysis_window_id != campaign_window.window.id
            or not batch_is_allowed_for_active_campaign(batch, campaign_window)
        ):
            continue
        try:
            outcome = enqueue_agent_batch(
                session,
                batch_id=batch.batch_id,
                actor=actor,
                trace_id=batch.trace_id,
                settings=settings,
            )
        except ConflictError:
            # A concurrent withdrawal or a state transition makes this evidence
            # ineligible; it must never turn into a second submission.
            session.rollback()
            continue
        if not outcome.replayed:
            queued += 1
    return queued
