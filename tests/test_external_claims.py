from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from fire_viewer.db.models import ExternalClaim, IncidentCandidate, IncidentSeries
from fire_viewer.domain.enums import ExternalArtifactStatus, ExternalSemanticRole
from fire_viewer.domain.errors import BadRequestError, ConflictError
from fire_viewer.domain.external_source_schemas import (
    ExternalArtifactInput,
    ExternalClaimInput,
    ExternalCollectionInput,
    ExternalProviderInput,
)
from fire_viewer.services.external_claims import (
    create_private_incident_candidate_from_external_claim,
    register_external_claim,
)
from fire_viewer.services.external_source_registry import (
    register_external_artifact_revision,
    register_external_collection,
    register_external_provider,
)


def _artifact(
    session,
    *,
    role: ExternalSemanticRole,
    suffix: str,
    status: ExternalArtifactStatus = ExternalArtifactStatus.VALIDATED,
):
    provider_key = f"official-{suffix}"
    register_external_provider(
        session,
        payload=ExternalProviderInput(
            provider_key=provider_key,
            display_name=f"Official {suffix}",
            allowed_domains=[f"{suffix}.example.test"],
            authentication_kind="none",
            attribution=f"Official attribution {suffix}",
            enabled=True,
        ),
        actor_id="claim-test",
        trace_id=f"trace-provider-{suffix}",
    )
    collection = register_external_collection(
        session,
        payload=ExternalCollectionInput(
            provider_key=provider_key,
            collection_key=f"collection-{suffix}",
            product_name=f"Collection {suffix}",
            sensor="TEST" if role == ExternalSemanticRole.SENSOR_DETECTION else None,
            platform=None,
            license="Open test license",
            cadence_seconds=300,
            semantic_role=role,
            configuration={},
        ),
        actor_id="claim-test",
        trace_id=f"trace-collection-{suffix}",
    )
    artifact = register_external_artifact_revision(
        session,
        payload=ExternalArtifactInput(
            collection_id=collection.id,
            external_product_id=f"product-{suffix}",
            source_url=f"https://{suffix}.example.test/product.json",
            content_hash=("a" if suffix == "statement" else "b") * 64,
            acquisition_start_at=(
                datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
                if role == ExternalSemanticRole.SENSOR_DETECTION
                else None
            ),
            acquisition_granule_id=(
                f"granule-{suffix}" if role == ExternalSemanticRole.SENSOR_DETECTION else None
            ),
            acquisition_pixel_id=(
                "pixel-001" if role == ExternalSemanticRole.SENSOR_DETECTION else None
            ),
            retrieved_at=datetime(2026, 8, 3, 12, 5, tzinfo=UTC),
            status=status,
        ),
        actor_id="claim-test",
        trace_id=f"trace-artifact-{suffix}",
    ).artifact
    return artifact


def test_official_claim_is_idempotent_and_only_creates_private_candidate(session) -> None:
    artifact = _artifact(
        session,
        role=ExternalSemanticRole.OFFICIAL_INCIDENT_STATEMENT,
        suffix="statement",
    )
    payload = ExternalClaimInput(
        artifact_revision_id=artifact.artifact_revision_id,
        assertion_kind="incident_declaration",
        assertion_payload={"place_name": "Massif des Maures"},
        geometry_geojson={"type": "Point", "coordinates": [6.02, 43.29]},
        horizontal_accuracy_m=250,
        confidence=0.9,
    )
    claim = register_external_claim(
        session, payload=payload, actor_id="claim-test", trace_id="trace-claim"
    )
    replay = register_external_claim(
        session, payload=payload, actor_id="claim-test", trace_id="trace-replay"
    )
    candidate = create_private_incident_candidate_from_external_claim(
        session,
        claim_id=claim.claim_id,
        actor_id="claim-test",
        trace_id="trace-candidate",
    )
    candidate_replay = create_private_incident_candidate_from_external_claim(
        session,
        claim_id=claim.claim_id,
        actor_id="claim-test",
        trace_id="trace-candidate-replay",
    )

    assert replay.id == claim.id
    assert candidate_replay.id == candidate.id
    assert candidate.state.value == "PRIVATE_MATCHING"
    assert candidate.origin_kind == "OFFICIAL_STATEMENT"
    assert candidate.reference_lon == 6.02
    assert candidate.reference_lat == 43.29
    assert session.scalar(select(func.count()).select_from(ExternalClaim)) == 1
    assert session.scalar(select(func.count()).select_from(IncidentCandidate)) == 1
    assert session.scalar(select(func.count()).select_from(IncidentSeries)) == 0
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(
            update(ExternalClaim)
            .where(ExternalClaim.id == claim.id)
            .values(assertion_kind="official_status")
        )
    session.rollback()


def test_hotspot_claim_cannot_seed_incident_or_candidate(session) -> None:
    artifact = _artifact(
        session,
        role=ExternalSemanticRole.SENSOR_DETECTION,
        suffix="hotspot",
    )
    claim = register_external_claim(
        session,
        payload=ExternalClaimInput(
            artifact_revision_id=artifact.artifact_revision_id,
            assertion_kind="thermal_hotspot",
            assertion_payload={"frp_mw": 12.5},
            geometry_geojson={"type": "Point", "coordinates": [6.02, 43.29]},
            horizontal_accuracy_m=1_000,
        ),
        actor_id="claim-test",
        trace_id="trace-hotspot",
    )

    with pytest.raises(BadRequestError, match="official incident declaration"):
        create_private_incident_candidate_from_external_claim(
            session,
            claim_id=claim.claim_id,
            actor_id="claim-test",
            trace_id="trace-hotspot-candidate",
        )
    assert session.scalar(select(func.count()).select_from(IncidentCandidate)) == 0
    assert session.scalar(select(func.count()).select_from(IncidentSeries)) == 0


def test_claim_geometry_is_wgs84_and_accuracy_is_explicit() -> None:
    with pytest.raises(ValidationError, match="WGS84"):
        ExternalClaimInput(
            artifact_revision_id="EAR-test",
            assertion_kind="incident_declaration",
            assertion_payload={},
            geometry_geojson={"type": "Point", "coordinates": [700_000, 6_600_000]},
            horizontal_accuracy_m=250,
        )
    with pytest.raises(ValidationError, match="horizontal_accuracy_m"):
        ExternalClaimInput(
            artifact_revision_id="EAR-test",
            assertion_kind="incident_declaration",
            assertion_payload={},
            geometry_geojson={"type": "Point", "coordinates": [6.02, 43.29]},
        )


def test_retracted_artifact_cannot_produce_a_claim(session) -> None:
    original = _artifact(
        session,
        role=ExternalSemanticRole.OFFICIAL_INCIDENT_STATEMENT,
        suffix="retracted",
    )
    artifact = register_external_artifact_revision(
        session,
        payload=ExternalArtifactInput(
            collection_id=original.collection_id,
            external_product_id=original.external_product_id,
            source_url=original.source_url,
            content_hash="c" * 64,
            retrieved_at=datetime(2026, 8, 3, 13, 0, tzinfo=UTC),
            status=ExternalArtifactStatus.RETRACTED,
        ),
        actor_id="claim-test",
        trace_id="trace-retraction",
    ).artifact
    with pytest.raises(ConflictError, match="retracted external artifact"):
        register_external_claim(
            session,
            payload=ExternalClaimInput(
                artifact_revision_id=artifact.artifact_revision_id,
                assertion_kind="incident_declaration",
                assertion_payload={"place_name": "Document retiré"},
            ),
            actor_id="claim-test",
            trace_id="trace-retracted",
        )
