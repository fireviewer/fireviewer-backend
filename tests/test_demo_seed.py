from __future__ import annotations

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from fire_viewer.db.models import (
    Episode,
    FireIdCounter,
    IncidentSeries,
    ManifestRevision,
    ModelAsset,
    Source,
    SpatialZone,
    SpatialZoneRevision,
    ZoneArchiveSnapshot,
)
from fire_viewer.domain.enums import IncidentStatus, PublicVisibility
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.scripts.seed_demo import (
    DEMO_SEED,
    DemoSeedConflictError,
    seed_demo,
)
from fire_viewer.services.queries import get_viewer_manifest


def _count(session: Session, model: type[object]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_seed_creates_synthetic_dataset_without_assets_even_when_legacy_env_is_set(
    session: Session,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FV_DEMO_ASSET_URL", "https://assets.example.invalid/ignored.glb")
    monkeypatch.setenv("FV_DEMO_ASSET_SHA256", "a" * 64)
    monkeypatch.setenv("FV_DEMO_ASSET_SIZE_BYTES", "12345")

    result = seed_demo(session, settings)
    session.commit()

    incident = session.scalar(
        select(IncidentSeries).where(IncidentSeries.fire_id == DEMO_SEED.fire_id)
    )
    assert incident is not None
    episodes = session.scalars(
        select(Episode).where(Episode.incident_id == incident.id).order_by(Episode.ordinal)
    ).all()
    manifest = get_viewer_manifest(session, DEMO_SEED.fire_id, settings)
    manifest_payload = manifest.model_dump(mode="json", exclude_none=False)

    assert result.created is True
    assert result.fire_id == DEMO_SEED.fire_id
    assert result.current_episode_id == "E03"
    assert result.manifest_etag == f'"{sha256_hex(manifest_payload)}"'
    assert incident.canonical_name == "Exercice fictif — zone Delta"
    assert incident.public_visibility == PublicVisibility.PUBLIC
    assert [(episode.episode_id, episode.status, episode.is_current) for episode in episodes] == [
        ("E01", IncidentStatus.CLOSED, False),
        ("E02", IncidentStatus.CLOSED, False),
        ("E03", IncidentStatus.MONITORING, True),
    ]
    assert manifest.model_state == "not_available"
    assert manifest.asset is None
    assert manifest.frame is None
    assert _count(session, ModelAsset) == 0
    assert _count(session, ManifestRevision) == 0
    assert _count(session, SpatialZone) == 0
    assert _count(session, SpatialZoneRevision) == 0
    assert _count(session, ZoneArchiveSnapshot) == 0


def test_seed_second_run_verifies_existing_dataset_without_writes(
    session: Session, settings
) -> None:
    first = seed_demo(session, settings)
    session.commit()
    session.expire_all()

    write_statements: list[str] = []

    def capture_write(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            write_statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture_write)
    try:
        second = seed_demo(session, settings)
    finally:
        event.remove(engine, "before_cursor_execute", capture_write)

    assert first.created is True
    assert second.created is False
    assert second == second.__class__(
        fire_id=DEMO_SEED.fire_id,
        current_episode_id="E03",
        created=False,
        manifest_etag=first.manifest_etag,
    )
    assert write_statements == []
    assert not session.new
    assert not session.dirty


def test_seed_rejects_conflicting_fixture_id_without_mutating(
    session: Session, settings, seed_incident
) -> None:
    conflicting, _episode = seed_incident(
        fire_id=DEMO_SEED.fire_id,
        sequence=DEMO_SEED.sequence,
        lon=DEMO_SEED.longitude,
        lat=DEMO_SEED.latitude,
        canonical_name="Other fixture occupant",
    )
    before = {
        "incident_name": conflicting.canonical_name,
        "episodes": _count(session, Episode),
        "sources": _count(session, Source),
        "counter": session.get(FireIdCounter, DEMO_SEED.territory_code).next_sequence,
        "assets": _count(session, ModelAsset),
        "manifest_revisions": _count(session, ManifestRevision),
    }

    with pytest.raises(DemoSeedConflictError, match=DEMO_SEED.fire_id):
        seed_demo(session, settings)

    session.expire_all()
    after = session.scalar(select(IncidentSeries).where(IncidentSeries.id == conflicting.id))
    assert after is not None
    assert after.canonical_name == before["incident_name"]
    assert _count(session, Episode) == before["episodes"]
    assert _count(session, Source) == before["sources"]
    assert session.get(FireIdCounter, DEMO_SEED.territory_code).next_sequence == before["counter"]
    assert _count(session, ModelAsset) == before["assets"]
    assert _count(session, ManifestRevision) == before["manifest_revisions"]
