from __future__ import annotations

from datetime import UTC, datetime

from fire_viewer.db.models import (
    Job,
    ManifestRevision,
    ModelAsset,
    Observation,
    Source,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import (
    AssetLod,
    AssetState,
    JobKind,
    JobState,
    MatchDecision,
    SourceTrust,
    SourceType,
    VerificationState,
)
from fire_viewer.domain.spatial import derive_raf20_origin


def _observation(
    *,
    observation_id: str,
    source_id: int,
    attached_incident_id: int | None,
    attached_episode_id: int | None,
    proposed_incident_id: int | None = None,
    proposed_episode_id: int | None = None,
    state: VerificationState = VerificationState.VERIFIED,
) -> Observation:
    observed_at = datetime(2026, 7, 15, 8, 30, tzinfo=UTC)
    return Observation(
        observation_id=observation_id,
        source_id=source_id,
        observed_at=observed_at,
        received_at=observed_at,
        longitude=6.0214,
        latitude=43.2897,
        horizontal_uncertainty_m=240.0,
        territory_code="83",
        toponyms=["Massif des Maures"],
        canonical_name_hint="Massif des Maures",
        evidence_hash=f"sha256:{observation_id[-1] * 64}",
        evidence_license="CC-BY-4.0",
        external_reference="https://example.invalid/evidence/record",
        request_hash="b" * 64,
        verification_state=state,
        attached_incident_id=attached_incident_id,
        attached_episode_id=attached_episode_id,
        proposed_incident_id=proposed_incident_id,
        proposed_episode_id=proposed_episode_id,
        match_decision=MatchDecision.REVIEW,
        match_score=0.71,
        margin_to_second_candidate=0.18,
        match_factors={"distance": 0.8},
        review_reasons=["distance compatible", "toponyme cohérent"],
        policy_id="test-policy-v1",
        trace_id="trace-admin-workspace-test",
        version=1,
    )


def test_incident_workspace_endpoints_are_scoped_and_do_not_leak_private_payloads(
    client, session, seed_incident
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00421", sequence=421, lon=6.0214, lat=43.2897)
    other_incident, other_episode = seed_incident(
        fire_id="FR-83-00422", sequence=422, lon=6.0314, lat=43.2997
    )
    source = Source(
        source_key="incident-workspace-source",
        source_type=SourceType.IMAGE,
        trust=SourceTrust.PARTNER,
        display_name="Source interne de test",
        public_display_name="Source publiable de test",
        public_license="CC-BY-4.0",
        public_reference_url="https://example.invalid/source",
        public_transformations=["métadonnées retirées"],
        credential_hash="c" * 64,
        enabled=True,
    )
    session.add(source)
    session.flush()
    session.add_all(
        [
            _observation(
                observation_id="obs-workspace-1",
                source_id=source.id,
                attached_incident_id=incident.id,
                attached_episode_id=episode.id,
            ),
            _observation(
                observation_id="obs-workspace-2",
                source_id=source.id,
                attached_incident_id=None,
                attached_episode_id=None,
                proposed_incident_id=incident.id,
                proposed_episode_id=episode.id,
                state=VerificationState.PENDING_REVIEW,
            ),
            _observation(
                observation_id="obs-workspace-3",
                source_id=source.id,
                attached_incident_id=other_incident.id,
                attached_episode_id=other_episode.id,
            ),
        ]
    )
    zone = SpatialZone(zone_id="ADMIN-WORKSPACE-ZONE", label="Zone de test")
    session.add(zone)
    session.flush()
    origin = derive_raf20_origin(6.0214, 43.2897, 400.0)
    zone_revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=origin.source_orthometric_height_m,
        geoid_undulation_m=origin.geoid_undulation_m,
        origin_ellipsoid_height_m=origin.ellipsoid_height_m,
        min_east_m=-100.0,
        max_east_m=100.0,
        min_north_m=-100.0,
        max_north_m=100.0,
        min_up_m=-50.0,
        max_up_m=500.0,
    )
    session.add(zone_revision)
    session.flush()
    asset = ModelAsset(
        asset_id="asset-admin-workspace-1",
        spatial_zone_revision_id=zone_revision.id,
        version=1,
        lod=AssetLod.DESKTOP,
        state=AssetState.PUBLISHED,
        glb_url="https://internal.example.invalid/private/model.glb",
        sha256="d" * 64,
        size_bytes=123_456,
        terrain_source_year=2024,
        generated_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        published_at=datetime(2026, 7, 15, 8, 15, tzinfo=UTC),
    )
    session.add(asset)
    session.flush()
    session.add(
        ManifestRevision(
            incident_id=incident.id,
            episode_id=episode.id,
            asset_id=asset.id,
            spatial_zone_revision_id=zone_revision.id,
            revision=1,
            is_current=True,
            reason="Révision de manifeste pour le test de surface privée.",
            actor_id="workspace-test",
        )
    )
    session.add(
        Job(
            job_id="job-admin-workspace-1",
            kind=JobKind.TERRAIN_BAKE,
            state=JobState.RETRY_WAIT,
            incident_id=incident.id,
            episode_id=episode.id,
            input_hash="e" * 64,
            input_payload={"private": "input"},
            output_payload={"private": "output"},
            attempt=1,
            max_attempts=5,
            last_error="Erreur interne de test.",
            trace_id="trace-private-job",
            idempotency_key="job-admin-workspace-key",
        )
    )
    session.commit()

    observations = client.get(f"/api/v1/admin/incidents/{incident.fire_id}/observations")
    sources_media = client.get(f"/api/v1/admin/incidents/{incident.fire_id}/sources-media")
    models_pipeline = client.get(f"/api/v1/admin/incidents/{incident.fire_id}/models-pipeline")

    responses = (observations, sources_media, models_pipeline)
    assert all(response.status_code == 200 for response in responses)
    assert all(response.headers["Cache-Control"] == "no-store" for response in responses)
    assert {item["observation_id"] for item in observations.json()["observations"]} == {
        "obs-workspace-1",
        "obs-workspace-2",
    }
    assert observations.json()["observations"][1]["proposed_fire_id"] == incident.fire_id
    assert "request_hash" not in observations.text
    assert "trace-admin-workspace-test" not in observations.text
    assert sources_media.json()["sources"][0]["source_key"] == source.source_key
    assert sources_media.json()["sources"][0]["public_transformations"] == ["métadonnées retirées"]
    assert "credential_hash" not in sources_media.text
    assert "obs-workspace-3" not in sources_media.text
    assert models_pipeline.json()["models"][0]["asset_id"] == asset.asset_id
    assert models_pipeline.json()["models"][0]["spatial_zone_id"] == zone.zone_id
    assert models_pipeline.json()["jobs"][0]["job_id"] == "job-admin-workspace-1"
    assert "glb_url" not in models_pipeline.text
    assert "private/model.glb" not in models_pipeline.text
    assert "input_payload" not in models_pipeline.text
    assert "trace-private-job" not in models_pipeline.text


def test_incident_workspace_endpoints_reject_unknown_fire_id(client) -> None:
    for suffix in ("observations", "sources-media", "models-pipeline"):
        response = client.get(f"/api/v1/admin/incidents/FR-83-99999/{suffix}")
        assert response.status_code == 404
