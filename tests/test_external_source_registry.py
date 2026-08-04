from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from pyproj import network
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from fire_viewer.db.models import (
    ArtifactLineage,
    ExternalArtifactRevision,
    IncidentSourcePlan,
)
from fire_viewer.domain.enums import (
    ExternalArtifactStatus,
    ExternalLineageRelation,
    ExternalSemanticRole,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError
from fire_viewer.domain.external_source_schemas import (
    ExternalArtifactInput,
    ExternalCollectionInput,
    ExternalProviderInput,
    IncidentSourcePlanInput,
)
from fire_viewer.services.external_source_registry import (
    claim_due_incident_source_plans,
    due_incident_source_plans,
    record_source_plan_failure,
    record_source_plan_success,
    register_external_artifact_revision,
    register_external_collection,
    register_external_provider,
    register_incident_source_plan,
    source_plan_snapshot,
)


def _provider(session, *, key: str, domains: list[str]):
    return register_external_provider(
        session,
        payload=ExternalProviderInput(
            provider_key=key,
            display_name=f"Provider {key}",
            allowed_domains=domains,
            authentication_kind="none",
            attribution=f"Attribution {key}",
            enabled=True,
        ),
        actor_id="registry-test",
        trace_id=f"trace-{key}",
    )


def _collection(
    session,
    *,
    provider_key: str,
    key: str,
    role: ExternalSemanticRole,
    sensor: str | None = None,
    platform: str | None = None,
    cadence_seconds: int | None = 300,
):
    return register_external_collection(
        session,
        payload=ExternalCollectionInput(
            provider_key=provider_key,
            collection_key=key,
            product_name=f"Product {key}",
            sensor=sensor,
            platform=platform,
            license="Open test license",
            cadence_seconds=cadence_seconds,
            semantic_role=role,
            configuration={"catalog_url": f"https://{provider_key}.example.test/catalog"},
        ),
        actor_id="registry-test",
        trace_id=f"trace-{key}",
    )


def _artifact(
    collection_id: int,
    *,
    product_id: str,
    source_url: str,
    content_hash: str,
    status: ExternalArtifactStatus = ExternalArtifactStatus.VALIDATED,
    **changes,
) -> ExternalArtifactInput:
    values = {
        "collection_id": collection_id,
        "external_product_id": product_id,
        "source_url": source_url,
        "content_hash": content_hash,
        "retrieved_at": datetime.now(UTC),
        "status": status,
    }
    values.update(changes)
    return ExternalArtifactInput.model_validate(values)


def test_exact_domain_rights_and_crs_are_fail_closed(session) -> None:
    with pytest.raises(BadRequestError, match="exact host names"):
        register_external_provider(
            session,
            payload=ExternalProviderInput(
                provider_key="wildcard-provider",
                display_name="Wildcard provider",
                allowed_domains=["*.example.test"],
                authentication_kind="none",
                attribution="Test attribution",
            ),
            actor_id="registry-test",
            trace_id="trace-wildcard",
        )

    _provider(session, key="official-provider", domains=["official.example.test"])
    collection = _collection(
        session,
        provider_key="official-provider",
        key="official-polygons",
        role=ExternalSemanticRole.INTERPRETED_OBSERVATION,
    )
    with pytest.raises(BadRequestError, match="allowlisted domain"):
        register_external_artifact_revision(
            session,
            payload=_artifact(
                collection.id,
                product_id="outside-domain",
                source_url="https://sub.official.example.test/product.geojson",
                content_hash="a" * 64,
            ),
            actor_id="registry-test",
            trace_id="trace-outside-domain",
        )
    with pytest.raises(BadRequestError, match="secret-like query"):
        register_external_artifact_revision(
            session,
            payload=_artifact(
                collection.id,
                product_id="signed-url",
                source_url=(
                    "https://official.example.test/product.geojson?"
                    "X-Amz-Signature=redacted-test-value"
                ),
                content_hash="e" * 64,
            ),
            actor_id="registry-test",
            trace_id="trace-secret-query",
        )

    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[700000, 6600000], [700100, 6600000], [700100, 6600100], [700000, 6600000]]
        ],
    }
    with pytest.raises(ValidationError, match="supplied together"):
        _artifact(
            collection.id,
            product_id="missing-crs",
            source_url="https://official.example.test/missing-crs.geojson",
            content_hash="b" * 64,
            footprint_geojson=polygon,
        )
    with pytest.raises(BadRequestError, match="unknown or unsupported"):
        register_external_artifact_revision(
            session,
            payload=_artifact(
                collection.id,
                product_id="unknown-crs",
                source_url="https://official.example.test/unknown-crs.geojson",
                content_hash="c" * 64,
                footprint_geojson=polygon,
                native_crs="EPSG:999999",
            ),
            actor_id="registry-test",
            trace_id="trace-unknown-crs",
        )
    network.set_network_enabled(True)  # type: ignore[attr-defined]
    try:
        accepted = register_external_artifact_revision(
            session,
            payload=_artifact(
                collection.id,
                product_id="lambert-polygon",
                source_url="https://official.example.test/lambert.geojson",
                content_hash="d" * 64,
                footprint_geojson=polygon,
                native_crs="EPSG:2154",
            ),
            actor_id="registry-test",
            trace_id="trace-lambert",
        )
        assert network.is_network_enabled() is False  # type: ignore[attr-defined]
    finally:
        network.set_network_enabled(False)  # type: ignore[attr-defined]

    assert accepted.artifact.native_crs == "EPSG:2154"
    coordinates = accepted.artifact.footprint_geojson["coordinates"][0][0]
    assert -180 <= coordinates[0] <= 180
    assert -90 <= coordinates[1] <= 90
    assert accepted.artifact.license == "Open test license"
    assert accepted.artifact.attribution == "Attribution official-provider"


def test_artifact_revisions_mirrors_corrections_and_retractions_are_immutable(session) -> None:
    _provider(
        session,
        key="revision-provider",
        domains=["source.example.test", "mirror.example.test"],
    )
    collection = _collection(
        session,
        provider_key="revision-provider",
        key="revision-products",
        role=ExternalSemanticRole.INTERPRETED_OBSERVATION,
    )
    first = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://SOURCE.EXAMPLE.TEST.:443/product-1",
            content_hash="1" * 64,
        ),
        actor_id="registry-test",
        trace_id="trace-revision-1",
    )
    replay = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://source.example.test/product-1",
            content_hash="1" * 64,
        ),
        actor_id="registry-test",
        trace_id="trace-replay",
    )
    with pytest.raises(ConflictError, match="multiple external products"):
        register_external_artifact_revision(
            session,
            payload=_artifact(
                collection.id,
                product_id="different-product",
                source_url="https://source.example.test/product-1",
                content_hash="1" * 64,
            ),
            actor_id="registry-test",
            trace_id="trace-source-product-conflict",
        )
    provisional = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-state",
            source_url="https://source.example.test/product-state",
            content_hash="8" * 64,
            status=ExternalArtifactStatus.PROVISIONAL,
        ),
        actor_id="registry-test",
        trace_id="trace-state-provisional",
    )
    validated = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-state",
            source_url="https://source.example.test/product-state",
            content_hash="8" * 64,
            status=ExternalArtifactStatus.VALIDATED,
        ),
        actor_id="registry-test",
        trace_id="trace-state-validated",
    )
    second = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://source.example.test/product-1",
            content_hash="2" * 64,
        ),
        actor_id="registry-test",
        trace_id="trace-revision-2",
    )
    mirror = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="mirror-product",
            source_url="https://mirror.example.test/product-1",
            content_hash="2" * 64,
        ),
        actor_id="registry-test",
        trace_id="trace-mirror",
    )
    corrected = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://source.example.test/product-1",
            content_hash="3" * 64,
            status=ExternalArtifactStatus.CORRECTED,
        ),
        actor_id="registry-test",
        trace_id="trace-correction",
    )
    retracted = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://source.example.test/product-1",
            content_hash="3" * 64,
            status=ExternalArtifactStatus.RETRACTED,
        ),
        actor_id="registry-test",
        trace_id="trace-retraction",
    )
    retraction_replay = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://source.example.test/product-1",
            content_hash="3" * 64,
            status=ExternalArtifactStatus.RETRACTED,
        ),
        actor_id="registry-test",
        trace_id="trace-retraction-replay",
    )
    remote_mirror = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="remote-content",
            source_url="https://mirror.example.test/remote-content",
            content_hash="9" * 64,
        ),
        actor_id="registry-test",
        trace_id="trace-remote-content",
    )
    lifecycle_and_mirror = register_external_artifact_revision(
        session,
        payload=_artifact(
            collection.id,
            product_id="product-1",
            source_url="https://source.example.test/product-1",
            content_hash="9" * 64,
        ),
        actor_id="registry-test",
        trace_id="trace-lifecycle-and-mirror",
    )

    assert replay.replayed is True
    assert replay.artifact.id == first.artifact.id
    assert first.artifact.source_url == "https://source.example.test/product-1"
    assert provisional.artifact.revision == 1
    assert validated.artifact.revision == 2
    assert validated.lineage_relations == (ExternalLineageRelation.SUPERSEDES,)
    assert session.get(ExternalArtifactRevision, provisional.artifact.id).status == (
        ExternalArtifactStatus.PROVISIONAL
    )
    assert second.artifact.revision == 2
    assert second.lineage_relations == (ExternalLineageRelation.SUPERSEDES,)
    assert mirror.lineage_relations == (ExternalLineageRelation.MIRRORS,)
    assert corrected.artifact.revision == 3
    assert ExternalLineageRelation.SUPERSEDES in corrected.lineage_relations
    assert retracted.artifact.revision == 4
    assert ExternalLineageRelation.RETRACTS in retracted.lineage_relations
    assert retraction_replay.replayed is True
    assert retraction_replay.artifact.id == retracted.artifact.id
    assert remote_mirror.artifact.revision == 1
    assert lifecycle_and_mirror.artifact.revision == 5
    assert ExternalLineageRelation.SUPERSEDES in lifecycle_and_mirror.lineage_relations
    assert ExternalLineageRelation.MIRRORS in lifecycle_and_mirror.lineage_relations
    assert session.get(ExternalArtifactRevision, first.artifact.id).status == (
        ExternalArtifactStatus.VALIDATED
    )
    assert session.scalar(select(func.count()).select_from(ExternalArtifactRevision)) == 9
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(
            update(ExternalArtifactRevision)
            .where(ExternalArtifactRevision.id == first.artifact.id)
            .values(attribution="tampered")
        )
        session.commit()
    session.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(
            delete(ExternalArtifactRevision).where(ExternalArtifactRevision.id == first.artifact.id)
        )
        session.commit()
    session.rollback()
    lineage_id = session.scalar(select(ArtifactLineage.id).order_by(ArtifactLineage.id))
    assert lineage_id is not None
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(
            update(ArtifactLineage)
            .where(ArtifactLineage.id == lineage_id)
            .values(reason="tampered")
        )
        session.commit()
    session.rollback()


def test_sensor_family_prevents_double_corroboration_and_forecast_is_separate(session) -> None:
    _provider(session, key="sensor-a", domains=["sensor-a.example.test"])
    _provider(session, key="sensor-b", domains=["sensor-b.example.test"])
    first_collection = _collection(
        session,
        provider_key="sensor-a",
        key="viirs-hotspots-a",
        role=ExternalSemanticRole.SENSOR_DETECTION,
        sensor="VIIRS",
        platform="S-NPP",
    )
    second_collection = _collection(
        session,
        provider_key="sensor-b",
        key="viirs-hotspots-b",
        role=ExternalSemanticRole.SENSOR_DETECTION,
        sensor="viirs",
        platform="s-npp",
    )
    acquired = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    identity = {
        "acquisition_start_at": acquired,
        "acquisition_granule_id": "granule-42",
        "acquisition_pixel_id": "pixel-10-20",
    }
    first = register_external_artifact_revision(
        session,
        payload=_artifact(
            first_collection.id,
            product_id="firms-relay",
            source_url="https://sensor-a.example.test/hotspot",
            content_hash="4" * 64,
            acquisition_end_at=acquired + timedelta(minutes=5),
            **identity,
        ),
        actor_id="registry-test",
        trace_id="trace-family-a",
    )
    second = register_external_artifact_revision(
        session,
        payload=_artifact(
            second_collection.id,
            product_id="effis-relay",
            source_url="https://sensor-b.example.test/hotspot",
            content_hash="5" * 64,
            **identity,
        ),
        actor_id="registry-test",
        trace_id="trace-family-b",
    )

    assert first.artifact.evidence_family_key == second.artifact.evidence_family_key
    assert ExternalLineageRelation.SAME_ACQUISITION_AS in second.lineage_relations

    forecast_collection = _collection(
        session,
        provider_key="sensor-a",
        key="weather-forecast",
        role=ExternalSemanticRole.WEATHER_FORECAST,
        sensor="VIIRS",
        platform="S-NPP",
    )
    with pytest.raises(BadRequestError, match="forecast run and valid"):
        register_external_artifact_revision(
            session,
            payload=_artifact(
                forecast_collection.id,
                product_id="forecast-missing-times",
                source_url="https://sensor-a.example.test/forecast",
                content_hash="6" * 64,
            ),
            actor_id="registry-test",
            trace_id="trace-forecast-missing",
        )
    forecast = register_external_artifact_revision(
        session,
        payload=_artifact(
            forecast_collection.id,
            product_id="forecast-valid",
            source_url="https://sensor-a.example.test/forecast-valid",
            content_hash="6" * 64,
            forecast_run_at=acquired,
            forecast_valid_at=acquired + timedelta(hours=1),
            **identity,
        ),
        actor_id="registry-test",
        trace_id="trace-forecast-valid",
    )
    assert forecast.artifact.semantic_role == ExternalSemanticRole.WEATHER_FORECAST
    assert forecast.artifact.evidence_family_key != first.artifact.evidence_family_key

    observation_collection = _collection(
        session,
        provider_key="sensor-a",
        key="weather-observation",
        role=ExternalSemanticRole.WEATHER_OBSERVATION,
    )
    with pytest.raises(BadRequestError, match="cannot be attached to an observation"):
        register_external_artifact_revision(
            session,
            payload=_artifact(
                observation_collection.id,
                product_id="observation-with-forecast-time",
                source_url="https://sensor-a.example.test/observation",
                content_hash="7" * 64,
                forecast_run_at=acquired,
                forecast_valid_at=acquired + timedelta(hours=1),
            ),
            actor_id="registry-test",
            trace_id="trace-observation-forecast",
        )


def test_incident_source_plan_watermark_and_bounded_backoff_are_visible(
    session,
    seed_incident,
    settings,
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00991",
        sequence=991,
        lon=6.02,
        lat=43.29,
    )
    provider = _provider(
        session,
        key="scheduler-provider",
        domains=["scheduler.example.test"],
    )
    collection = _collection(
        session,
        provider_key="scheduler-provider",
        key="scheduled-observations",
        role=ExternalSemanticRole.INTERPRETED_OBSERVATION,
        cadence_seconds=300,
    )
    plan_payload = IncidentSourcePlanInput(
        incident_id=incident.id,
        collection_id=collection.id,
        configuration={"aoi_buffer_m": 5000},
    )
    plan = register_incident_source_plan(
        session,
        payload=plan_payload,
        actor_id="scheduler-test",
        trace_id="trace-plan",
    )
    replay = register_incident_source_plan(
        session,
        payload=plan_payload,
        actor_id="scheduler-test",
        trace_id="trace-plan-replay",
    )
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    plan.next_poll_at = now
    session.commit()
    enabled_settings = settings.model_copy(
        update={
            "official_connectors_enabled": True,
            "official_connector_backoff_initial_seconds": 60,
            "official_connector_backoff_max_seconds": 120,
        }
    )

    assert replay.id == plan.id
    assert due_incident_source_plans(session, settings=settings, now=now) == []
    assert [
        row.id for row in due_incident_source_plans(session, settings=enabled_settings, now=now)
    ] == [plan.id]
    provider.enabled = False
    session.commit()
    assert due_incident_source_plans(session, settings=enabled_settings, now=now) == []
    provider.enabled = True
    session.commit()
    first_claims = claim_due_incident_source_plans(
        session,
        settings=enabled_settings,
        worker_id="scheduler-worker-1",
        lease_seconds=300,
        now=now,
    )
    assert len(first_claims) == 1
    first_token = first_claims[0].lease_token
    assert first_claims[0].plan.id == plan.id
    assert due_incident_source_plans(session, settings=enabled_settings, now=now) == []
    assert (
        claim_due_incident_source_plans(
            session,
            settings=enabled_settings,
            worker_id="scheduler-worker-2",
            lease_seconds=300,
            now=now,
        )
        == []
    )
    first_failure = record_source_plan_failure(
        session,
        plan_id=plan.plan_id,
        lease_token=first_token,
        error="HTTP 503",
        settings=enabled_settings,
        actor_id="scheduler-test",
        trace_id="trace-failure-1",
        now=now,
    )
    first_backoff = first_failure.backoff_seconds
    second_claims = claim_due_incident_source_plans(
        session,
        settings=enabled_settings,
        worker_id="scheduler-worker-2",
        lease_seconds=300,
        now=now + timedelta(seconds=60),
    )
    assert len(second_claims) == 1
    second_token = second_claims[0].lease_token
    with pytest.raises(ConflictError, match="absent, expired, or owned"):
        record_source_plan_success(
            session,
            plan_id=plan.plan_id,
            lease_token=first_token,
            watermark="stale-worker-watermark",
            actor_id="scheduler-worker-1",
            trace_id="trace-stale-success",
            now=now + timedelta(seconds=61),
        )
    second_failure = record_source_plan_failure(
        session,
        plan_id=plan.plan_id,
        lease_token=second_token,
        error="quota exceeded",
        settings=enabled_settings,
        actor_id="scheduler-test",
        trace_id="trace-failure-2",
        now=now + timedelta(seconds=60),
    )
    second_backoff = second_failure.backoff_seconds
    final_claims = claim_due_incident_source_plans(
        session,
        settings=enabled_settings,
        worker_id="scheduler-worker-3",
        lease_seconds=300,
        now=now + timedelta(seconds=180),
    )
    assert len(final_claims) == 1
    final_token = final_claims[0].lease_token
    with pytest.raises(BadRequestError, match="1000 characters"):
        record_source_plan_success(
            session,
            plan_id=plan.plan_id,
            lease_token=final_token,
            watermark="w" * 1_001,
            actor_id="scheduler-test",
            trace_id="trace-long-watermark",
            now=now + timedelta(seconds=180),
        )
    success = record_source_plan_success(
        session,
        plan_id=plan.plan_id,
        lease_token=final_token,
        watermark="cursor:2026-08-03T12:05Z",
        actor_id="scheduler-test",
        trace_id="trace-success",
        now=now + timedelta(seconds=180),
    )

    assert first_backoff == 60
    assert second_backoff == 120
    assert success.backoff_seconds == 0
    assert success.last_error is None
    assert success.watermark == "cursor:2026-08-03T12:05Z"
    assert success.next_poll_at == now + timedelta(seconds=480)
    snapshot = source_plan_snapshot(success)
    assert snapshot["watermark"] == "cursor:2026-08-03T12:05Z"
    assert snapshot["backoff_seconds"] == 0
    assert snapshot["lease_active"] is False
    assert snapshot["lease_until"] is None
    assert session.scalar(select(func.count()).select_from(IncidentSourcePlan)) == 1


def test_collection_configuration_rejects_persisted_secrets(session) -> None:
    _provider(session, key="secret-provider", domains=["secret.example.test"])
    with pytest.raises(BadRequestError, match="cannot be persisted"):
        register_external_collection(
            session,
            payload=ExternalCollectionInput(
                provider_key="secret-provider",
                collection_key="secret-collection",
                product_name="Secret collection",
                license="Test license",
                semantic_role=ExternalSemanticRole.INTERPRETED_OBSERVATION,
                configuration={"api_key": "must-not-be-stored"},
            ),
            actor_id="registry-test",
            trace_id="trace-secret",
        )
