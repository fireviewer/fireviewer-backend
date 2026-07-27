from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from fire_viewer.db.models import AuditEvent, Episode, Job, Observation, OutboxEvent
from fire_viewer.domain.enums import EvidenceSpatialMode, VerificationState


def _detect(client, payload_factory, *, key: str, source_id: str, content_char: str):
    return client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": key},
        json=payload_factory(
            source_id=source_id,
            content_char=content_char,
            toponyms=["Repère privé temporaire"],
        ),
    )


def _current_episode(session, fire_id: str) -> Episode:
    return session.execute(
        select(Episode)
        .where(Episode.episode_id == "E01")
        .join(Episode.incident)
        .where(
            Episode.incident.has(fire_id=fire_id),
            Episode.is_current.is_(True),
        )
    ).scalar_one()


def _assert_no_forbidden_audit_keys(value: Any) -> None:
    forbidden = {
        "longitude",
        "latitude",
        "altitude_m",
        "toponyms",
        "canonical_name_hint",
        "external_reference",
        "display_name",
        "credential_hash",
        "credential_configured",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for nested in value.values():
            _assert_no_forbidden_audit_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_forbidden_audit_keys(nested)


def test_three_independent_proofs_publish_only_a_generalized_corroborated_view(
    client, session, payload_factory
) -> None:
    responses = [
        _detect(
            client,
            payload_factory,
            key=f"cdc-corroboration-{index:04d}",
            source_id=f"cdc-source-{index}",
            content_char=content_char,
        )
        for index, content_char in enumerate(("a", "b", "c"), start=1)
    ]

    assert [response.status_code for response in responses] == [201, 200, 200]
    assert [response.json()["decision"] for response in responses] == [
        "create",
        "attach",
        "attach",
    ]
    assert responses[2].json()["public_confirmation"] == "corroborated"
    fire_id = responses[0].json()["fire_id"]

    public_view = client.get(f"/api/v1/incident/{fire_id}/public-view")
    assert public_view.status_code == 200
    body = public_view.json()
    assert body["verification"] == "corroborated"
    assert body["canonical_name"] is None
    assert body["last_human_validation_at"] is None
    assert body["location"]["coordinates"] == [6.02, 43.29]
    assert body["location"]["horizontal_uncertainty_m"] >= 1_500
    assert len(body["observations"]) == 3
    assert {
        (item["verification_state"], item["spatial_mode"]) for item in body["observations"]
    } == {("CORROBORATED", "GENERALIZED")}
    assert len(body["evidence_projections"]) == 1
    projection = body["evidence_projections"][0]
    assert projection["kind"] == "generalized_area"
    assert projection["verification_state"] == "CORROBORATED"
    assert projection["radius_m"] >= 1_500
    assert "Repère privé temporaire" not in public_view.text

    discovery = client.get("/api/v1/incidents/recent").json()["incidents"]
    item = next(entry for entry in discovery if entry["fire_id"] == fire_id)
    assert item["verification"] == "corroborated"
    assert item["canonical_name"] == fire_id

    session.expire_all()
    episode = _current_episode(session, fire_id)
    assert episode.verification_state == VerificationState.CORROBORATED
    assert episode.corroborating_source_count == 3
    assert episode.validated_at is None


def test_duplicate_source_or_evidence_does_not_reach_the_three_proof_threshold(
    client, session, payload_factory
) -> None:
    first = _detect(
        client,
        payload_factory,
        key="cdc-duplicate-0001",
        source_id="cdc-duplicate-source-a",
        content_char="d",
    )
    fire_id = first.json()["fire_id"]
    second = _detect(
        client,
        payload_factory,
        key="cdc-duplicate-0002",
        source_id="cdc-duplicate-source-a",
        content_char="e",
    )
    third = _detect(
        client,
        payload_factory,
        key="cdc-duplicate-0003",
        source_id="cdc-duplicate-source-b",
        content_char="d",
    )

    assert second.json()["decision"] == third.json()["decision"] == "attach"
    assert third.json()["public_confirmation"] == "pending"
    view = client.get(f"/api/v1/incident/{fire_id}/public-view").json()
    assert view["verification"] == "review_required"
    assert view["location"] is None
    assert view["observations"] == []
    session.expire_all()
    episode = _current_episode(session, fire_id)
    assert episode.verification_state == VerificationState.UNVERIFIED
    assert episode.corroborating_source_count == 2


def test_spatially_incoherent_corroboration_keeps_the_view_without_drawing_a_false_area(
    client, session, payload_factory
) -> None:
    responses = [
        _detect(
            client,
            payload_factory,
            key=f"cdc-dispersed-{index:04d}",
            source_id=f"cdc-dispersed-source-{index}",
            content_char=content_char,
        )
        for index, content_char in enumerate(("1", "2", "3"), start=1)
    ]
    fire_id = responses[0].json()["fire_id"]
    displaced = session.execute(
        select(Observation).where(
            Observation.observation_id == responses[-1].json()["observation_id"]
        )
    ).scalar_one()
    displaced.longitude = 8.5
    session.commit()

    public_view = client.get(f"/api/v1/incident/{fire_id}/public-view")

    assert public_view.status_code == 200
    assert public_view.json()["verification"] == "corroborated"
    assert public_view.json()["evidence_projections"] == []


def test_human_validation_purges_transient_data_and_controls_exact_publication(
    client, session, payload_factory
) -> None:
    detection = _detect(
        client,
        payload_factory,
        key="cdc-human-detection-0001",
        source_id="cdc-human-source",
        content_char="f",
    )
    assert detection.status_code == 201
    fire_id = detection.json()["fire_id"]
    observation_id = detection.json()["observation_id"]
    observation = session.execute(
        select(Observation).where(Observation.observation_id == observation_id)
    ).scalar_one()
    observation.external_reference = "https://example.invalid/private-evidence"
    session.commit()

    resolution = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "cdc-human-resolution-0001"},
        json={
            "action": "attach",
            "target_fire_id": fire_id,
            "expected_version": observation.version,
            "reason": "Validation humaine de la preuve et autorisation du repère public.",
            "publish_spatial_evidence": True,
        },
    )

    assert resolution.status_code == 200
    assert resolution.json()["verification_state"] == "VERIFIED"
    session.expire_all()
    stored = session.execute(
        select(Observation).where(Observation.observation_id == observation_id)
    ).scalar_one()
    assert stored.toponyms == []
    assert stored.canonical_name_hint is None
    assert stored.external_reference is None
    assert stored.raw_purged_at is not None
    assert stored.raw_purge_due_at == stored.raw_purged_at
    assert stored.public_spatial_mode == EvidenceSpatialMode.EXACT

    view = client.get(f"/api/v1/incident/{fire_id}/public-view").json()
    assert view["verification"] == "verified"
    assert view["last_human_validation_at"] is not None
    assert len(view["evidence_projections"]) == 1
    assert view["evidence_projections"][0]["kind"] == "validated_marker"

    audits = session.execute(select(AuditEvent)).scalars().all()
    for event in audits:
        _assert_no_forbidden_audit_keys(event.before_snapshot)
        _assert_no_forbidden_audit_keys(event.after_snapshot)
        _assert_no_forbidden_audit_keys(event.payload)


def test_operational_profile_uses_500_hectares_or_evacuation_without_running_3d(
    client, session, payload_factory
) -> None:
    detection = _detect(
        client,
        payload_factory,
        key="cdc-profile-detection-0001",
        source_id="cdc-profile-source",
        content_char="9",
    )
    fire_id = detection.json()["fire_id"]
    session.expire_all()
    version = _current_episode(session, fire_id).version

    below = client.post(
        f"/api/v1/operator/incidents/{fire_id}/operational-profile",
        headers={"Idempotency-Key": "cdc-profile-below-0001"},
        json={
            "expected_version": version,
            "estimated_area_ha": 499.99,
            "evacuation_established": False,
            "reason": "Surface validée sous le seuil de production externe.",
        },
    )
    assert below.status_code == 200
    assert below.json()["model_generation_eligible"] is False
    assert below.json()["terrain_bake_request_id"] is None

    threshold = client.post(
        f"/api/v1/operator/incidents/{fire_id}/operational-profile",
        headers={"Idempotency-Key": "cdc-profile-threshold-0001"},
        json={
            "expected_version": below.json()["version"],
            "estimated_area_ha": 500,
            "evacuation_established": False,
            "reason": "Surface validée au seuil exact de production externe.",
        },
    )
    assert threshold.status_code == 200
    assert threshold.json()["model_generation_eligible"] is True
    assert threshold.json()["eligibility_reasons"] == ["area_threshold"]
    assert threshold.json()["terrain_bake_request_id"] is not None

    reset = client.post(
        f"/api/v1/operator/incidents/{fire_id}/operational-profile",
        headers={"Idempotency-Key": "cdc-profile-reset-0001"},
        json={
            "expected_version": threshold.json()["version"],
            "estimated_area_ha": 120,
            "evacuation_established": False,
            "reason": "Surface corrigée sous le seuil après revue humaine.",
        },
    )
    evacuation = client.post(
        f"/api/v1/operator/incidents/{fire_id}/operational-profile",
        headers={"Idempotency-Key": "cdc-profile-evacuation-0001"},
        json={
            "expected_version": reset.json()["version"],
            "estimated_area_ha": 120,
            "evacuation_established": True,
            "evacuation_basis": "Décision institutionnelle contrôlée par un opérateur.",
            "reason": "Évacuation établie indépendamment du seuil de surface.",
        },
    )
    assert evacuation.status_code == 200
    assert evacuation.json()["model_generation_eligible"] is True
    assert evacuation.json()["eligibility_reasons"] == ["established_evacuation"]

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    requests = (
        session.execute(select(OutboxEvent).where(OutboxEvent.topic == "model_generation.eligible"))
        .scalars()
        .all()
    )
    assert len(requests) == 2
    assert all(
        event.payload["execution_scope"] == "external_pipeline_not_implemented"
        for event in requests
    )
