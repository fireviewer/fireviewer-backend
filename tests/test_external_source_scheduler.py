from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from fire_viewer.db.models import ExternalArtifactRevision, IncidentSourcePlan
from fire_viewer.domain.enums import ExternalArtifactStatus, ExternalSemanticRole
from fire_viewer.domain.external_source_schemas import (
    ExternalArtifactInput,
    ExternalCollectionInput,
    ExternalProviderInput,
    IncidentSourcePlanInput,
)
from fire_viewer.services.external_source_registry import (
    register_external_collection,
    register_external_provider,
    register_incident_source_plan,
)
from fire_viewer.services.external_source_scheduler import (
    ExternalCollectionContext,
    ExternalConnectorRegistry,
    ExternalFetchResult,
    run_external_source_scheduler_once,
)


class FakeConnector:
    def __init__(self, *, collection_id: int, wrong_collection: bool = False) -> None:
        self.collection_id = collection_id
        self.wrong_collection = wrong_collection
        self.contexts: list[ExternalCollectionContext] = []

    def fetch(self, context: ExternalCollectionContext) -> ExternalFetchResult:
        self.contexts.append(context)
        return ExternalFetchResult(
            artifacts=(
                ExternalArtifactInput(
                    collection_id=(
                        self.collection_id + 1 if self.wrong_collection else self.collection_id
                    ),
                    external_product_id="official-product-1",
                    source_url="https://official.example.test/products/official-product-1.json",
                    content_hash="a" * 64,
                    acquisition_granule_id="granule-20260803T100000Z",
                    acquisition_pixel_id="pixel-001",
                    acquisition_start_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
                    acquisition_end_at=datetime(2026, 8, 3, 10, 5, tzinfo=UTC),
                    retrieved_at=datetime(2026, 8, 3, 10, 6, tzinfo=UTC),
                    status=ExternalArtifactStatus.VALIDATED,
                ),
            ),
            watermark="cursor:official-product-1",
        )


def _seed_plan(session, seed_incident):
    incident, _episode = seed_incident(fire_id="FR-83-00080", sequence=80, lon=6.02, lat=43.29)
    register_external_provider(
        session,
        payload=ExternalProviderInput(
            provider_key="official-provider",
            display_name="Official provider",
            allowed_domains=["official.example.test"],
            authentication_kind="none",
            attribution="Official provider attribution",
            enabled=True,
        ),
        actor_id="scheduler-test",
        trace_id="trace-provider",
    )
    collection = register_external_collection(
        session,
        payload=ExternalCollectionInput(
            provider_key="official-provider",
            collection_key="active-fire",
            product_name="Active fire observations",
            sensor="TEST-SENSOR",
            platform="TEST-PLATFORM",
            license="Open test license",
            cadence_seconds=300,
            semantic_role=ExternalSemanticRole.SENSOR_DETECTION,
            configuration={"catalog_url": "https://official.example.test/catalog"},
        ),
        actor_id="scheduler-test",
        trace_id="trace-collection",
    )
    plan = register_incident_source_plan(
        session,
        payload=IncidentSourcePlanInput(
            incident_id=incident.id,
            collection_id=collection.id,
            configuration={"window_before_seconds": 3600},
        ),
        actor_id="scheduler-test",
        trace_id="trace-plan",
    )
    plan.next_poll_at = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    session.commit()
    return incident, collection, plan


def test_scheduler_persists_artifact_and_advances_watermark(
    app, session, settings, seed_incident
) -> None:
    incident, collection, plan = _seed_plan(session, seed_incident)
    connector = FakeConnector(collection_id=collection.id)
    connectors = ExternalConnectorRegistry()
    connectors.register(
        provider_key="official-provider",
        collection_key="active-fire",
        connector=connector,
    )
    enabled = settings.model_copy(update={"official_connectors_enabled": True})
    now = datetime(2026, 8, 3, 10, 10, tzinfo=UTC)

    assert run_external_source_scheduler_once(
        app.state.session_factory,
        settings=enabled,
        worker_id="official-scheduler:test",
        connectors=connectors,
        now=now,
    )

    session.expire_all()
    stored_plan = session.get(IncidentSourcePlan, plan.id)
    artifact = session.scalar(select(ExternalArtifactRevision))
    assert stored_plan is not None
    assert stored_plan.watermark == "cursor:official-product-1"
    assert stored_plan.last_error is None
    assert stored_plan.lease_owner is None
    assert artifact is not None
    assert artifact.external_product_id == "official-product-1"
    assert len(connector.contexts) == 1
    assert connector.contexts[0].target_public_id == incident.fire_id
    assert connector.contexts[0].bbox_wgs84 == (
        incident.bbox_min_lon,
        incident.bbox_min_lat,
        incident.bbox_max_lon,
        incident.bbox_max_lat,
    )


def test_scheduler_fails_closed_without_registered_connector(
    app, session, settings, seed_incident
) -> None:
    _incident, _collection, plan = _seed_plan(session, seed_incident)
    enabled = settings.model_copy(update={"official_connectors_enabled": True})

    assert run_external_source_scheduler_once(
        app.state.session_factory,
        settings=enabled,
        worker_id="official-scheduler:test",
        connectors=ExternalConnectorRegistry(),
        now=datetime(2026, 8, 3, 10, 10, tzinfo=UTC),
    )

    session.expire_all()
    stored_plan = session.get(IncidentSourcePlan, plan.id)
    assert stored_plan is not None
    assert stored_plan.last_error == "connector_not_registered"
    assert stored_plan.backoff_seconds == enabled.official_connector_backoff_initial_seconds
    assert stored_plan.lease_owner is None
    assert session.scalar(select(ExternalArtifactRevision)) is None


def test_scheduler_rejects_artifact_for_another_collection(
    app, session, settings, seed_incident
) -> None:
    _incident, collection, plan = _seed_plan(session, seed_incident)
    connector = FakeConnector(collection_id=collection.id, wrong_collection=True)
    connectors = ExternalConnectorRegistry()
    connectors.register(
        provider_key="official-provider",
        collection_key="active-fire",
        connector=connector,
    )
    enabled = settings.model_copy(update={"official_connectors_enabled": True})

    assert run_external_source_scheduler_once(
        app.state.session_factory,
        settings=enabled,
        worker_id="official-scheduler:test",
        connectors=connectors,
        now=datetime(2026, 8, 3, 10, 10, tzinfo=UTC),
    )

    session.expire_all()
    stored_plan = session.get(IncidentSourcePlan, plan.id)
    assert stored_plan is not None
    assert stored_plan.last_error == "connector_contract_invalid"
    assert session.scalar(select(ExternalArtifactRevision)) is None


def test_scheduler_is_inert_while_feature_flag_is_disabled(app, settings) -> None:
    assert not run_external_source_scheduler_once(
        app.state.session_factory,
        settings=settings,
        worker_id="official-scheduler:test",
        connectors=ExternalConnectorRegistry(),
    )
