from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentSituationReportRevision,
    AgentValidationCampaignDay,
)
from fire_viewer.scripts.restore_die_retrospective import (
    _active_reason,
    _load_payload,
    _next_revision,
    restore,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_MANIFEST = PROJECT_ROOT / "src" / "fire_viewer" / "retrospectives" / "die-2026-v1.json"


def test_active_reason_does_not_require_missing_optional_metadata() -> None:
    assert _active_reason({"local_date": "2026-07-05"}) == (
        "Zone active datée reconstituée depuis les références de la journée."
    )


def test_next_zone_revision_filters_by_incident_episode_and_zone_kind() -> None:
    class RecordingSession:
        statement: object | None = None

        def scalar(self, statement: object) -> int:
            self.statement = statement
            return 4

    session = RecordingSession()

    assert _next_revision(session, 11, 22, "burned") == 5
    statement = str(session.statement)
    assert "active_fire_zone_revision.incident_id" in statement
    assert "active_fire_zone_revision.episode_id" in statement
    assert "active_fire_zone_revision.zone_kind" in statement


def test_restore_materializes_both_daily_layers_and_is_idempotent(
    session: Session,
    seed_incident,
) -> None:
    seed_incident(
        fire_id="FR-26-00001",
        sequence=1,
        lon=5.4,
        lat=44.7,
        canonical_name="Die",
    )
    payload = _load_payload(PACKAGED_MANIFEST)

    first = restore(session, payload, actor="test-retrospective-operator", apply=True)

    assert first == {
        "mode": "applied",
        "fire_id": "FR-26-00001",
        "windows_created": 21,
        "reports_created": 21,
        "zones_created": {"active": 21, "burned": 21},
        "campaign_days_created": 21,
    }
    assert session.scalar(select(func.count()).select_from(AgentSituationReportRevision)) == 21
    assert session.scalar(select(func.count()).select_from(AgentValidationCampaignDay)) == 21
    assert session.scalar(
        select(func.count())
        .select_from(ActiveFireZoneRevision)
        .where(ActiveFireZoneRevision.zone_kind == "active")
    ) == 21
    assert session.scalar(
        select(func.count())
        .select_from(ActiveFireZoneRevision)
        .where(ActiveFireZoneRevision.zone_kind == "burned")
    ) == 21

    second = restore(session, payload, actor="test-retrospective-operator", apply=True)

    assert second["windows_created"] == 0
    assert second["reports_created"] == 0
    assert second["zones_created"] == {"active": 0, "burned": 0}
    assert second["campaign_days_created"] == 0
