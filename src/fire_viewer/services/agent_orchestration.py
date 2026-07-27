"""Durable Vercel schedules for agent work.

The schedules only enqueue source-safe work. The shared dispatcher remains the
single place allowed to submit or poll RunPod jobs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentScheduleRun,
    AgentSourceResearchRun,
    Episode,
    IncidentSeries,
)
from fire_viewer.domain.agent_schemas import AgentSourceResearchRequest
from fire_viewer.domain.enums import ActorType, IncidentStatus
from fire_viewer.services.agent_source_research import create_source_research

PARIS_TIMEZONE = ZoneInfo("Europe/Paris")
MEDIA_RESEARCH_HOURS = frozenset({11, 23})
_MEDIA_SCHEDULE_PREFIX = "public-source-media-v1"


def _schedule_row(session: Session, *, schedule_key: str, now: datetime) -> AgentScheduleRun:
    row = session.execute(
        select(AgentScheduleRun)
        .where(AgentScheduleRun.schedule_key == schedule_key)
        .with_for_update()
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = AgentScheduleRun(
        schedule_key=schedule_key,
        next_run_at=now,
        last_run_at=None,
        lease_owner=None,
        lease_until=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        row = session.execute(
            select(AgentScheduleRun)
            .where(AgentScheduleRun.schedule_key == schedule_key)
            .with_for_update()
        ).scalar_one()
    return row


def _next_local_slot(local_now: datetime, hour: int) -> datetime:
    tomorrow = local_now.date() + timedelta(days=1)
    return datetime(
        tomorrow.year,
        tomorrow.month,
        tomorrow.day,
        hour,
        tzinfo=PARIS_TIMEZONE,
    )


def run_public_source_schedule_once(
    session: Session,
    *,
    worker_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    """Queue one media-source research run per active fire at 11:00/23:00 Paris.

    Satellite, hotspot and thermal acquisition is attached only to the 11:00
    run. Incidents are enqueued in a deterministic order; the dispatcher later
    executes them strictly one at a time.
    """

    current = as_utc(now or utcnow())
    local_now = current.astimezone(PARIS_TIMEZONE)
    if local_now.hour not in MEDIA_RESEARCH_HOURS:
        return 0

    schedule_key = f"{_MEDIA_SCHEDULE_PREFIX}-{local_now.hour:02d}"
    schedule = _schedule_row(session, schedule_key=schedule_key, now=current)
    if schedule.last_run_at is not None:
        last_local = as_utc(schedule.last_run_at).astimezone(PARIS_TIMEZONE)
        if last_local.date() == local_now.date() and last_local.hour == local_now.hour:
            session.commit()
            return 0

    schedule.lease_owner = worker_id
    schedule.lease_until = current + timedelta(seconds=settings.agent_dispatch_lease_seconds)
    session.commit()

    active_incidents = (
        session.execute(
            select(IncidentSeries)
            .join(Episode, Episode.incident_id == IncidentSeries.id)
            .where(
                Episode.is_current.is_(True),
                Episode.status == IncidentStatus.ACTIVE_CONFIRMED,
            )
            .order_by(Episode.started_at.asc(), IncidentSeries.fire_id.asc())
        )
        .scalars()
        .all()
    )

    actor = Actor(
        actor_id=f"agent-scheduler:media-sources:{local_now.hour:02d}",
        roles=frozenset(),
        actor_type=ActorType.SYSTEM,
    )
    include_satellite = local_now.hour == 11
    queued = 0
    schedule_slot = f"{local_now.date().isoformat()}T{local_now.hour:02d}:00-Europe/Paris"
    for incident in active_incidents:
        response = create_source_research(
            session,
            fire_id=incident.fire_id,
            payload=AgentSourceResearchRequest(
                local_date=local_now.date(),
                location_hint=incident.canonical_name or incident.fire_id,
            ),
            actor=actor,
            trace_id=f"scheduled-media:{schedule_slot}:{incident.fire_id}",
            settings=settings,
            query_plan_overrides={
                "schedule_slot": schedule_slot,
                "research_kind": "online_media_sources",
                "include_satellite": include_satellite,
                "include_hotspots": include_satellite,
                "include_thermal": include_satellite,
            },
        )
        run = session.execute(
            select(AgentSourceResearchRun).where(
                AgentSourceResearchRun.research_id == response.research_id
            )
        ).scalar_one()
        if run.query_plan.get("schedule_slot") == schedule_slot:
            queued += 1

    schedule = session.execute(
        select(AgentScheduleRun)
        .where(AgentScheduleRun.schedule_key == schedule_key)
        .with_for_update()
    ).scalar_one()
    schedule.last_run_at = current
    schedule.next_run_at = _next_local_slot(local_now, local_now.hour).astimezone(ZoneInfo("UTC"))
    schedule.lease_owner = None
    schedule.lease_until = None
    session.commit()
    return queued
