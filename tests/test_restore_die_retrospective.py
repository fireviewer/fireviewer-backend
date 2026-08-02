from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import shape
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
PACKAGED_NON_DIE_MANIFESTS = tuple(
    PROJECT_ROOT / "src" / "fire_viewer" / "retrospectives" / name
    for name in (
        "ledenon-2026-v1.json",
        "oupia-pouzols-2026-v1.json",
        "taradeau-2026-v1.json",
        "trevillach-2026-v1.json",
        "fontainebleau-2026-v1.json",
    )
)
PACKAGED_RETROSPECTIVE_CASES = (
    (PACKAGED_NON_DIE_MANIFESTS[0], "FR-30-00001", 4.503627, 43.92306, "Lédenon", 1),
    (PACKAGED_NON_DIE_MANIFESTS[1], "FR-34-00001", 2.762352, 43.299972, "Oupia", 1),
    (PACKAGED_NON_DIE_MANIFESTS[2], "FR-83-00001", 6.432859, 43.450667, "Taradeau", 2),
    (PACKAGED_NON_DIE_MANIFESTS[3], "FR-66-00001", 2.56954, 42.668317, "Trévillach", 5),
    (PACKAGED_NON_DIE_MANIFESTS[4], "FR-77-00001", 2.588886, 48.389457, "Fontainebleau", 4),
)


def test_active_reason_does_not_require_missing_optional_metadata() -> None:
    assert _active_reason({"local_date": "2026-07-05"}) == (
        "Zone active datée reconstituée depuis les références de la journée."
    )


@pytest.mark.parametrize("manifest_path", PACKAGED_NON_DIE_MANIFESTS)
def test_packaged_retrospectives_keep_each_active_zone_inside_its_daily_footprint(
    manifest_path: Path,
) -> None:
    payload = _load_payload(manifest_path)

    assert len(payload["activity_zones"]) == len(payload["reports"])
    for activity in payload["activity_zones"]:
        active = shape(activity["geometry_geojson"])
        burned = shape(activity["burned_geometry_geojson"])

        assert not active.is_empty
        assert not burned.is_empty
        assert burned.buffer(1e-9).covers(active)


@pytest.mark.parametrize(
    ("manifest_path", "fire_id", "lon", "lat", "canonical_name", "expected_days"),
    PACKAGED_RETROSPECTIVE_CASES,
)
def test_each_packaged_retrospective_restores_both_daily_layer_kinds(
    session: Session,
    seed_incident,
    manifest_path: Path,
    fire_id: str,
    lon: float,
    lat: float,
    canonical_name: str,
    expected_days: int,
) -> None:
    seed_incident(
        fire_id=fire_id,
        sequence=1,
        lon=lon,
        lat=lat,
        canonical_name=canonical_name,
    )

    result = restore(
        session,
        _load_payload(manifest_path),
        actor="test-retrospective-operator",
        apply=True,
    )

    assert result["windows_created"] == expected_days
    assert result["reports_created"] == expected_days
    assert result["campaign_days_created"] == expected_days
    assert result["zones_created"] == {"active": expected_days, "burned": expected_days}


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


def test_restore_accepts_a_second_incident_with_explicit_satellite_footprint(
    session: Session,
    seed_incident,
    tmp_path: Path,
) -> None:
    seed_incident(
        fire_id="FR-77-00001",
        sequence=1,
        lon=2.6,
        lat=48.4,
        canonical_name="Fontainebleau",
    )
    geometry = {
        "type": "Polygon",
        "coordinates": [[[2.60, 48.40], [2.61, 48.40], [2.61, 48.41], [2.60, 48.40]]],
    }
    payload_path = tmp_path / "fontainebleau-retrospective.json"
    payload_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "dataset_id": "fontainebleau-2026-retrospective-v1",
                "campaign_id": "campaign-fontainebleau-retrospective-v1",
                "identifier_suffix": "retrospective-v1",
                "incident": {"fire_id": "FR-77-00001", "episode_id": "E01"},
                "activity_zones": [
                    {
                        "local_date": "2026-07-13",
                        "valid_at": "2026-07-13T10:46:00Z",
                        "geometry_geojson": geometry,
                        "burned_geometry_geojson": geometry,
                        "geometry_origin": "SATELLITE_PRODUCT",
                        "burned_geometry_origin": "SATELLITE_PRODUCT",
                        "source_revision_ids": ["EMSR894-AOI01-FEP-v1"],
                    }
                ],
                "reports": [
                    {
                        "local_date": "2026-07-13",
                        "title": "Observation satellite datée",
                        "summary": "Empreinte et activité issues du produit Copernicus daté.",
                        "sections": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = restore(
        session,
        _load_payload(payload_path),
        actor="test-retrospective-operator",
        apply=True,
    )

    assert result["fire_id"] == "FR-77-00001"
    assert result["zones_created"] == {"active": 1, "burned": 1}
    zones = list(session.scalars(select(ActiveFireZoneRevision)))
    assert {(zone.zone_kind, zone.geometry_origin) for zone in zones} == {
        ("active", "SATELLITE_PRODUCT"),
        ("burned", "SATELLITE_PRODUCT"),
    }
