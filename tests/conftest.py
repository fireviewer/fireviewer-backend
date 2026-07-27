from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.domain.geospatial import bbox_for_point
from fire_viewer.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        database_url=f"sqlite:///{tmp_path / 'fire_viewer_test.db'}",
        trusted_hosts=["testserver", "localhost"],
        log_level="CRITICAL",
        max_clock_skew_seconds=300,
        zone_upload_storage_dir=tmp_path / "zone_upload_storage",
        zone_upload_max_bytes=2_097_152,
        zone_upload_max_unpacked_bytes=4_194_304,
        zone_upload_max_files=100,
    )


@pytest.fixture
def app(settings: Settings):
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def session(app) -> Iterator[Session]:
    db_session = app.state.session_factory()
    try:
        yield db_session
    finally:
        db_session.close()


@pytest.fixture
def payload_factory() -> Callable[..., dict[str, Any]]:
    def factory(
        *,
        source_id: str = "local-feed-001",
        trust: str = "unverified",
        source_type: str = "text",
        lon: float = 6.0214,
        lat: float = 43.2897,
        uncertainty_m: float = 250.0,
        territory_code: str = "83",
        canonical_name: str = "Massif des Maures",
        toponyms: list[str] | None = None,
        content_char: str = "a",
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        observed = observed_at or (datetime.now(UTC) - timedelta(minutes=5))
        return {
            "source": {"id": source_id, "type": source_type, "trust": trust},
            "observed_at": observed.isoformat(),
            "received_at": (observed + timedelta(seconds=30)).isoformat(),
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
                "horizontal_uncertainty_m": uncertainty_m,
            },
            "evidence": {
                "content_hash": f"sha256:{content_char * 64}",
                "license": "test-fixture",
            },
            "context": {
                "territory_code": territory_code,
                "toponyms": toponyms if toponyms is not None else [canonical_name],
                "canonical_name": canonical_name,
            },
        }

    return factory


@pytest.fixture
def seed_incident(session: Session):
    from fire_viewer.db.models import Episode, FireIdCounter, IncidentSeries
    from fire_viewer.domain.enums import IncidentStatus, VerificationState
    from fire_viewer.domain.public_visibility import canonical_public_visibility

    def seed(
        *,
        fire_id: str,
        sequence: int,
        lon: float,
        lat: float,
        uncertainty_m: float = 250.0,
        canonical_name: str = "Massif des Maures",
        status: IncidentStatus = IncidentStatus.MONITORING,
        observed_at: datetime | None = None,
        ended_at: datetime | None = None,
        verification_state: VerificationState | None = None,
    ) -> tuple[IncidentSeries, Episode]:
        observed = observed_at or (datetime.now(UTC) - timedelta(minutes=10))
        effective_verification = verification_state or (
            VerificationState.UNVERIFIED
            if status in {IncidentStatus.CANDIDATE, IncidentStatus.UNDER_REVIEW}
            else VerificationState.VERIFIED
        )
        bbox = bbox_for_point(lon, lat, uncertainty_m)
        incident = IncidentSeries(
            fire_id=fire_id,
            territory_code="83",
            sequence=sequence,
            canonical_name=canonical_name,
            reference_lon=lon,
            reference_lat=lat,
            horizontal_uncertainty_m=uncertainty_m,
            bbox_min_lon=bbox.min_lon,
            bbox_max_lon=bbox.max_lon,
            bbox_min_lat=bbox.min_lat,
            bbox_max_lat=bbox.max_lat,
            public_visibility=canonical_public_visibility(status, effective_verification),
            version=1,
        )
        session.add(incident)
        session.flush()
        episode = Episode(
            incident_id=incident.id,
            episode_id="E01",
            ordinal=1,
            status=status,
            verification_state=effective_verification,
            evidence_basis_at=(
                observed if effective_verification == VerificationState.VERIFIED else None
            ),
            review_required=status in {IncidentStatus.CANDIDATE, IncidentStatus.UNDER_REVIEW},
            is_current=True,
            confidence_policy="g1-default-v1",
            started_at=observed - timedelta(hours=1),
            last_observed_at=observed,
            validated_at=(
                observed if effective_verification == VerificationState.VERIFIED else None
            ),
            ended_at=ended_at,
            version=1,
        )
        session.add(episode)
        counter = session.get(FireIdCounter, "83")
        if counter is None:
            session.add(FireIdCounter(territory_code="83", next_sequence=sequence + 1))
        else:
            counter.next_sequence = max(counter.next_sequence, sequence + 1)
        session.commit()
        return incident, episode

    return seed
