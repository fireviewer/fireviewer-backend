from __future__ import annotations

from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import select

from fire_viewer.db.models import AgentSourceResearchRun
from fire_viewer.domain.enums import AgentSourceResearchState, IncidentStatus
from fire_viewer.services.agent_orchestration import run_public_source_schedule_once


def _research_settings(settings):
    return settings.model_copy(
        update={
            "agent_dispatch_enabled": True,
            "agent_research_enabled": True,
            "object_storage_backend": "vercel_blob",
            "blob_read_write_token": SecretStr("blob-" + ("x" * 40)),
        }
    )


def test_media_schedule_queues_active_fires_in_order_and_satellite_once(
    session,
    settings,
    seed_incident,
) -> None:
    first, _ = seed_incident(
        fire_id="FR-83-00601",
        sequence=601,
        lon=6.02,
        lat=43.28,
        canonical_name="Premier feu",
        status=IncidentStatus.ACTIVE_CONFIRMED,
        observed_at=datetime(2026, 7, 27, 7, 0, tzinfo=UTC),
    )
    second, _ = seed_incident(
        fire_id="FR-83-00602",
        sequence=602,
        lon=6.12,
        lat=43.38,
        canonical_name="Second feu",
        status=IncidentStatus.ACTIVE_CONFIRMED,
        observed_at=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
    )
    seed_incident(
        fire_id="FR-83-00603",
        sequence=603,
        lon=6.22,
        lat=43.48,
        canonical_name="Sous surveillance",
        status=IncidentStatus.MONITORING,
    )
    configured = _research_settings(settings)

    queued = run_public_source_schedule_once(
        session,
        worker_id="schedule-test",
        settings=configured,
        now=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
    )

    assert queued == 2
    rows = (
        session.execute(select(AgentSourceResearchRun).order_by(AgentSourceResearchRun.id))
        .scalars()
        .all()
    )
    assert [row.incident_id for row in rows] == [first.id, second.id]
    assert all(row.query_plan["include_satellite"] is True for row in rows)
    assert all(row.query_plan["include_hotspots"] is True for row in rows)
    assert all(row.query_plan["include_thermal"] is True for row in rows)
    assert (
        run_public_source_schedule_once(
            session,
            worker_id="schedule-test-replay",
            settings=configured,
            now=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
        )
        == 0
    )

    for row in rows:
        row.state = AgentSourceResearchState.SUCCEEDED
    session.commit()
    evening = run_public_source_schedule_once(
        session,
        worker_id="schedule-test-evening",
        settings=configured,
        now=datetime(2026, 7, 27, 21, 0, tzinfo=UTC),
    )
    assert evening == 2
    evening_rows = (
        session.execute(
            select(AgentSourceResearchRun).order_by(AgentSourceResearchRun.id.desc()).limit(2)
        )
        .scalars()
        .all()
    )
    assert all(row.query_plan["include_satellite"] is False for row in evening_rows)


def test_media_schedule_ignores_non_media_hours(session, settings) -> None:
    assert (
        run_public_source_schedule_once(
            session,
            worker_id="schedule-test",
            settings=_research_settings(settings),
            now=datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
        )
        == 0
    )
