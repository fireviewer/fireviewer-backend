from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings, get_settings
from fire_viewer.core.time import as_utc
from fire_viewer.db.engine import create_db_engine, create_session_factory
from fire_viewer.db.models import (
    Episode,
    FireIdCounter,
    IncidentSeries,
    ManifestRevision,
    Source,
    ZoneArchiveSnapshot,
)
from fire_viewer.domain.enums import (
    IncidentStatus,
    PublicVisibility,
    SourceTrust,
    SourceType,
    VerificationState,
)
from fire_viewer.domain.geospatial import BoundingBox, bbox_for_point
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.services.queries import get_viewer_manifest


@dataclass(frozen=True, slots=True)
class DemoSourceSpec:
    source_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class DemoEpisodeSpec:
    episode_id: str
    ordinal: int
    status: IncidentStatus
    started_at: datetime
    last_observed_at: datetime
    validated_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DemoSeedSpec:
    dataset_version: str
    fire_id: str
    territory_code: str
    sequence: int
    canonical_name: str
    public_note: str
    longitude: float
    latitude: float
    horizontal_uncertainty_m: float
    source: DemoSourceSpec
    episodes: tuple[DemoEpisodeSpec, ...]
    created_at: datetime

    @property
    def bbox(self) -> BoundingBox:
        return bbox_for_point(self.longitude, self.latitude, self.horizontal_uncertainty_m)


@dataclass(frozen=True, slots=True)
class DemoSeedResult:
    """Outcome of seeding or verifying the immutable v1 fixture dataset."""

    fire_id: str
    current_episode_id: str
    created: bool
    manifest_etag: str


class DemoSeedConflictError(RuntimeError):
    """The fixture identifier is already occupied by data other than dataset v1."""


DEMO_SEED: Final = DemoSeedSpec(
    dataset_version="v1",
    fire_id="FR-83-00042",
    territory_code="83",
    sequence=42,
    canonical_name="Exercice fictif — zone Delta",
    public_note="Jeu de données de démonstration entièrement fictif (v1).",
    longitude=2.0,
    latitude=46.0,
    horizontal_uncertainty_m=250.0,
    source=DemoSourceSpec(
        source_key="fire-viewer-fixture-v1",
        display_name="Jeu de données fictif Fire Viewer v1",
    ),
    episodes=(
        DemoEpisodeSpec(
            episode_id="E01",
            ordinal=1,
            status=IncidentStatus.CLOSED,
            started_at=datetime(2025, 1, 10, 8, 0, tzinfo=UTC),
            last_observed_at=datetime(2025, 1, 10, 18, 0, tzinfo=UTC),
            ended_at=datetime(2025, 1, 11, 8, 0, tzinfo=UTC),
        ),
        DemoEpisodeSpec(
            episode_id="E02",
            ordinal=2,
            status=IncidentStatus.CLOSED,
            started_at=datetime(2025, 5, 13, 9, 0, tzinfo=UTC),
            last_observed_at=datetime(2025, 5, 13, 16, 0, tzinfo=UTC),
            ended_at=datetime(2025, 5, 14, 8, 0, tzinfo=UTC),
        ),
        DemoEpisodeSpec(
            episode_id="E03",
            ordinal=3,
            status=IncidentStatus.MONITORING,
            started_at=datetime(2026, 1, 15, 8, 5, tzinfo=UTC),
            last_observed_at=datetime(2026, 1, 15, 8, 24, tzinfo=UTC),
            validated_at=datetime(2026, 1, 15, 8, 5, tzinfo=UTC),
        ),
    ),
    created_at=datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
)


def _same_datetime(actual: datetime | None, expected: datetime | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return as_utc(actual) == as_utc(expected)


def _raise_if_mismatched(condition: bool, description: str, mismatches: list[str]) -> None:
    if not condition:
        mismatches.append(description)


def _validate_source(source: Source | None, mismatches: list[str]) -> None:
    _raise_if_mismatched(source is not None, "fixture source is missing", mismatches)
    if source is None:
        return
    _raise_if_mismatched(
        source.source_type == SourceType.OPERATOR,
        "fixture source_type differs",
        mismatches,
    )
    _raise_if_mismatched(
        source.trust == SourceTrust.OPERATOR, "fixture source trust differs", mismatches
    )
    _raise_if_mismatched(
        source.display_name == DEMO_SEED.source.display_name,
        "fixture source display name differs",
        mismatches,
    )
    _raise_if_mismatched(source.enabled, "fixture source is disabled", mismatches)
    _raise_if_mismatched(
        source.credential_hash is None, "fixture source has credentials", mismatches
    )
    _raise_if_mismatched(
        _same_datetime(source.created_at, DEMO_SEED.created_at),
        "fixture source creation time differs",
        mismatches,
    )
    _raise_if_mismatched(
        _same_datetime(source.updated_at, DEMO_SEED.created_at),
        "fixture source update time differs",
        mismatches,
    )


def _validate_incident(incident: IncidentSeries, mismatches: list[str]) -> None:
    bbox = DEMO_SEED.bbox
    expected_values = {
        "territory_code": DEMO_SEED.territory_code,
        "sequence": DEMO_SEED.sequence,
        "canonical_name": DEMO_SEED.canonical_name,
        "public_note": DEMO_SEED.public_note,
        "reference_lon": DEMO_SEED.longitude,
        "reference_lat": DEMO_SEED.latitude,
        "horizontal_uncertainty_m": DEMO_SEED.horizontal_uncertainty_m,
        "bbox_min_lon": bbox.min_lon,
        "bbox_max_lon": bbox.max_lon,
        "bbox_min_lat": bbox.min_lat,
        "bbox_max_lat": bbox.max_lat,
        "public_visibility": PublicVisibility.PUBLIC,
        "version": 1,
    }
    for field_name, expected in expected_values.items():
        _raise_if_mismatched(
            getattr(incident, field_name) == expected,
            f"fixture incident {field_name} differs",
            mismatches,
        )
    _raise_if_mismatched(
        _same_datetime(incident.created_at, DEMO_SEED.created_at),
        "fixture incident creation time differs",
        mismatches,
    )
    _raise_if_mismatched(
        _same_datetime(incident.updated_at, DEMO_SEED.created_at),
        "fixture incident update time differs",
        mismatches,
    )


def _validate_episodes(incident: IncidentSeries, mismatches: list[str]) -> None:
    actual_episodes = sorted(incident.episodes, key=lambda episode: episode.ordinal)
    _raise_if_mismatched(
        len(actual_episodes) == len(DEMO_SEED.episodes),
        "fixture episode count differs",
        mismatches,
    )
    for actual, expected in zip(actual_episodes, DEMO_SEED.episodes, strict=False):
        expected_values = {
            "episode_id": expected.episode_id,
            "ordinal": expected.ordinal,
            "status": expected.status,
            "verification_state": (
                VerificationState.VERIFIED
                if expected.validated_at is not None
                else VerificationState.UNVERIFIED
            ),
            "review_required": False,
            "is_current": expected.episode_id == DEMO_SEED.episodes[-1].episode_id,
            "confidence_policy": "g1-default-v1",
            "version": 1,
        }
        for field_name, expected_value in expected_values.items():
            _raise_if_mismatched(
                getattr(actual, field_name) == expected_value,
                f"fixture episode {expected.episode_id} {field_name} differs",
                mismatches,
            )
        for field_name, expected_value in (
            ("started_at", expected.started_at),
            ("last_observed_at", expected.last_observed_at),
            ("evidence_basis_at", expected.validated_at),
            ("validated_at", expected.validated_at),
            ("ended_at", expected.ended_at),
            ("created_at", DEMO_SEED.created_at),
            ("updated_at", DEMO_SEED.created_at),
        ):
            _raise_if_mismatched(
                _same_datetime(getattr(actual, field_name), expected_value),
                f"fixture episode {expected.episode_id} {field_name} differs",
                mismatches,
            )


def _validate_existing_dataset(
    session: Session,
    incident: IncidentSeries,
    source: Source | None,
    counter: FireIdCounter | None,
) -> None:
    mismatches: list[str] = []
    _validate_source(source, mismatches)
    _validate_incident(incident, mismatches)
    _validate_episodes(incident, mismatches)
    _raise_if_mismatched(
        counter is not None and counter.next_sequence >= DEMO_SEED.sequence + 1,
        "territory counter does not reserve the fixture sequence",
        mismatches,
    )
    manifest_revision_exists = session.execute(
        select(ManifestRevision.id).where(ManifestRevision.incident_id == incident.id).limit(1)
    ).scalar_one_or_none()
    _raise_if_mismatched(
        manifest_revision_exists is None,
        "fixture has a manifest revision",
        mismatches,
    )
    archive_snapshot_exists = session.execute(
        select(ZoneArchiveSnapshot.id)
        .where(ZoneArchiveSnapshot.incident_id == incident.id)
        .limit(1)
    ).scalar_one_or_none()
    _raise_if_mismatched(
        archive_snapshot_exists is None,
        "fixture has an archive snapshot",
        mismatches,
    )
    if mismatches:
        details = "; ".join(mismatches)
        raise DemoSeedConflictError(
            f"{DEMO_SEED.fire_id} is occupied by data that is not fixture dataset "
            f"{DEMO_SEED.dataset_version}: {details}"
        )


def _manifest_result(session: Session, settings: Settings, *, created: bool) -> DemoSeedResult:
    with session.no_autoflush:
        manifest = get_viewer_manifest(session, DEMO_SEED.fire_id, settings)
    payload = manifest.model_dump(mode="json", exclude_none=False)
    return DemoSeedResult(
        fire_id=DEMO_SEED.fire_id,
        current_episode_id=DEMO_SEED.episodes[-1].episode_id,
        created=created,
        manifest_etag=f'"{sha256_hex(payload)}"',
    )


def seed_demo(session: Session, settings: Settings) -> DemoSeedResult:
    """Create dataset v1 once, then only verify it on subsequent invocations.

    This function never commits.  The caller owns the transaction so tests can prove that a
    verified second invocation executes no writes and an incompatible collision is untouched.
    """

    with session.no_autoflush:
        incident = session.execute(
            select(IncidentSeries)
            .where(IncidentSeries.fire_id == DEMO_SEED.fire_id)
            .options(selectinload(IncidentSeries.episodes))
        ).scalar_one_or_none()
        source = session.execute(
            select(Source).where(Source.source_key == DEMO_SEED.source.source_key)
        ).scalar_one_or_none()
        counter = session.get(FireIdCounter, DEMO_SEED.territory_code)

    if incident is not None:
        _validate_existing_dataset(session, incident, source, counter)
        return _manifest_result(session, settings, created=False)

    if source is not None:
        source_mismatches: list[str] = []
        _validate_source(source, source_mismatches)
        if source_mismatches:
            details = "; ".join(source_mismatches)
            raise DemoSeedConflictError(
                f"{DEMO_SEED.source.source_key} is occupied by incompatible source data: {details}"
            )
    else:
        source = Source(
            source_key=DEMO_SEED.source.source_key,
            source_type=SourceType.OPERATOR,
            trust=SourceTrust.OPERATOR,
            display_name=DEMO_SEED.source.display_name,
            enabled=True,
            created_at=DEMO_SEED.created_at,
            updated_at=DEMO_SEED.created_at,
        )
        session.add(source)

    if counter is None:
        session.add(
            FireIdCounter(
                territory_code=DEMO_SEED.territory_code,
                next_sequence=DEMO_SEED.sequence + 1,
            )
        )
    elif counter.next_sequence < DEMO_SEED.sequence + 1:
        counter.next_sequence = DEMO_SEED.sequence + 1

    bbox = DEMO_SEED.bbox
    incident = IncidentSeries(
        fire_id=DEMO_SEED.fire_id,
        territory_code=DEMO_SEED.territory_code,
        sequence=DEMO_SEED.sequence,
        canonical_name=DEMO_SEED.canonical_name,
        reference_lon=DEMO_SEED.longitude,
        reference_lat=DEMO_SEED.latitude,
        horizontal_uncertainty_m=DEMO_SEED.horizontal_uncertainty_m,
        bbox_min_lon=bbox.min_lon,
        bbox_max_lon=bbox.max_lon,
        bbox_min_lat=bbox.min_lat,
        bbox_max_lat=bbox.max_lat,
        public_visibility=PublicVisibility.PUBLIC,
        public_note=DEMO_SEED.public_note,
        version=1,
        created_at=DEMO_SEED.created_at,
        updated_at=DEMO_SEED.created_at,
    )
    session.add(incident)
    session.flush()

    current_episode_id = DEMO_SEED.episodes[-1].episode_id
    session.add_all(
        Episode(
            incident_id=incident.id,
            episode_id=episode.episode_id,
            ordinal=episode.ordinal,
            status=episode.status,
            verification_state=(
                VerificationState.VERIFIED
                if episode.validated_at is not None
                else VerificationState.UNVERIFIED
            ),
            evidence_basis_at=episode.validated_at,
            review_required=False,
            is_current=episode.episode_id == current_episode_id,
            confidence_policy="g1-default-v1",
            started_at=episode.started_at,
            last_observed_at=episode.last_observed_at,
            validated_at=episode.validated_at,
            ended_at=episode.ended_at,
            version=1,
            created_at=DEMO_SEED.created_at,
            updated_at=DEMO_SEED.created_at,
        )
        for episode in DEMO_SEED.episodes
    )
    session.flush()

    return _manifest_result(session, settings, created=True)


def main() -> None:
    settings = get_settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            result = seed_demo(session, settings)
            if result.created:
                session.commit()
                action = "Seeded"
            else:
                action = "Verified"
            print(
                f"{action} {result.fire_id} at {result.current_episode_id} "
                f"(ETag {result.manifest_etag})"
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
