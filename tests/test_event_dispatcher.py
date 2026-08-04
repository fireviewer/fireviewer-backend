from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from fire_viewer.core.config import Settings
from fire_viewer.db.models import (
    Episode,
    EventAnalysisJob,
    EventCandidate,
    ExternalArtifactRevision,
    ExternalClaim,
    ExternalCollection,
    ExternalProvider,
    FireActivityEvent,
    IncidentCandidate,
    IncidentSeries,
    LocalizationAttempt,
    OutboxEvent,
    PublicationSnapshot,
)
from fire_viewer.domain.enums import (
    EventAnalysisJobState,
    EventCandidateState,
    ExternalArtifactStatus,
    ExternalSemanticRole,
    FireActivityEventState,
    IncidentCandidateState,
    IncidentStatus,
    LocalizationAttemptState,
    PublicVisibility,
    VerificationState,
)
from fire_viewer.main import create_app
from fire_viewer.services.event_dispatcher import run_event_dispatcher_once

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, *, publication: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        event_v2_enabled=True,
        event_antivirus_mode="test_clean",
        agent_event_pipeline_enabled=True,
        v2_publication_enabled=publication,
        database_url=f"sqlite:///{tmp_path / 'event-dispatch.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
        trusted_hosts=["testserver"],
        log_level="CRITICAL",
    )


def _migrate(settings: Settings) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")


def _submission() -> dict[str, Any]:
    return {
        "idempotency_key": "5e61ad84-6e77-4dc1-9590-59c12e4e53fc",
        "viewpoint": {
            "longitude": 6.0214,
            "latitude": 43.2897,
            "horizontal_accuracy_m": 18,
            "origin": "USER_PLACED",
        },
        "observed_time": {"start_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()},
        "message": "Flammes visibles sur le versant.",
        "evidence_asset_ids": [],
        "consent": {"analysis": True, "retention": True, "public_derivative": False},
    }


def _create_candidate(app: Any) -> dict[str, Any]:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/v2/event-candidates", json=_submission())
    assert response.status_code == 202, response.text
    return response.json()


def _upload_test_image(client: TestClient) -> str:
    content = b"\xff\xd8\xff" + b"\x00" * 13
    upload = client.post(
        "/api/v2/evidence/uploads",
        json={
            "files": [
                {
                    "file_name": "scene.jpg",
                    "media_type": "image/jpeg",
                    "size_bytes": len(content),
                }
            ]
        },
    )
    assert upload.status_code == 201, upload.text
    asset_id = upload.json()["assets"][0]["evidence_asset_id"]
    upload_id = upload.json()["upload_id"]
    stored = client.put(
        f"/api/v2/evidence/uploads/{upload_id}/assets/{asset_id}",
        content=content,
        headers={"Content-Type": "image/jpeg"},
    )
    assert stored.status_code == 204, stored.text
    finalized = client.post(
        f"/api/v2/evidence/uploads/{upload_id}/finalize",
        json={"evidence_asset_ids": [asset_id]},
    )
    assert finalized.status_code == 200, finalized.text
    return asset_id


def _create_candidate_with_image(app: Any) -> tuple[dict[str, Any], str]:
    with TestClient(app, raise_server_exceptions=False) as client:
        asset_id = _upload_test_image(client)
        payload = _submission()
        payload["evidence_asset_ids"] = [asset_id]
        response = client.post("/api/v2/event-candidates", json=payload)
    assert response.status_code == 202, response.text
    return response.json(), asset_id


def _seed_localized_provenance(
    app: Any,
    *,
    output: dict[str, Any],
    evidence_asset_id: str,
) -> None:
    attempt = next(
        item for item in output["localization_attempts"] if item["status"] == "localized"
    )
    perception_anchor = {
        "anchor_id": attempt["anchor_id"],
        "evidence_asset_id": evidence_asset_id,
        "phenomenon": attempt["phenomenon"],
        "source_point_normalized": [0.75, 0.5],
        "source_geometry_normalized": None,
        "model_id": attempt["model_id"],
        "model_revision": attempt["model_revision"],
        "model_score": 0.9,
    }
    spatial_evidence = {
        "anchor_id": attempt["anchor_id"],
        "status": "projected",
        "method": attempt["method"],
        "geometry_geojson": attempt["geometry_geojson"],
        "horizontal_accuracy_m": attempt["horizontal_accuracy_m"],
        "direction_uncertainty_deg": attempt["direction_uncertainty_deg"],
        "distance_uncertainty_m": attempt["distance_uncertainty_m"],
        "reason_codes": [],
        "reference_revision": attempt["reference_revision"],
    }
    output["perception_anchors"] = [perception_anchor]
    output["spatial_evidence"] = [spatial_evidence]
    with app.state.session_factory() as session:
        outbox = session.execute(
            select(OutboxEvent).where(OutboxEvent.topic == "event_candidate.analyze")
        ).scalar_one()
        outbox.payload = {
            **outbox.payload,
            "perception_anchors": [perception_anchor],
            "spatial_evidence": [spatial_evidence],
        }
        session.commit()


def _abstained_output(candidate_id: str) -> dict[str, Any]:
    return {
        "schema_version": "event-result-2.0",
        "candidate_id": candidate_id,
        "status": "abstained",
        "view_profile": None,
        "perception_anchors": [],
        "spatial_evidence": [],
        "localization_attempts": [
            {
                "attempt_id": "LOC-no-anchor",
                "anchor_id": None,
                "phenomenon": None,
                "status": "abstained",
                "method": None,
                "geometry_geojson": None,
                "sector": None,
                "horizontal_accuracy_m": None,
                "direction_uncertainty_deg": None,
                "distance_uncertainty_m": None,
                "reason_codes": ["no_visual_anchor"],
                "model_id": None,
                "model_revision": None,
                "reference_revision": None,
                "shadow_only": False,
            }
        ],
        "event_proposals": [],
        "independent_external_families": [],
        "contradictions": [],
        "reason_codes": ["no_visual_anchor", "view_profile_unclassified"],
        "requires_human_review": True,
    }


def _localized_output(candidate_id: str, *, suffix: str, observed_start_at: str) -> dict[str, Any]:
    output = _abstained_output(candidate_id)
    attempt_id = f"LOC-{suffix}"
    output.update(
        {
            "status": "needs_review",
            "view_profile": "ground_wide_known_viewpoint",
            "localization_attempts": [
                {
                    "attempt_id": attempt_id,
                    "anchor_id": f"ANCHOR-{suffix}",
                    "phenomenon": "active_fire_point",
                    "status": "localized",
                    "method": "camera_raycast",
                    "geometry_geojson": {"type": "Point", "coordinates": [6.03, 43.30]},
                    "sector": None,
                    "horizontal_accuracy_m": 70,
                    "direction_uncertainty_deg": 2,
                    "distance_uncertainty_m": 65,
                    "reason_codes": [],
                    "model_id": "fireviewer/detector",
                    "model_revision": "immutable-revision",
                    "reference_revision": "terrain-r1",
                    "shadow_only": False,
                }
            ],
            "event_proposals": [
                {
                    "proposal_id": f"FAE-{suffix}",
                    "attempt_id": attempt_id,
                    "phenomenon": "active_fire_point",
                    "observed_time": {
                        "start_at": observed_start_at,
                        "end_at": None,
                    },
                    "geometry_geojson": {"type": "Point", "coordinates": [6.03, 43.30]},
                    "horizontal_accuracy_m": 70,
                    "status": "DRAFT",
                    "requires_human_review": True,
                }
            ],
            "reason_codes": [],
        }
    )
    return output


class _PollingClient:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.submissions = 0
        self.polls = 0
        self.payloads: list[dict[str, Any]] = []

    def submit(self, payload: Any) -> dict[str, Any]:
        assert payload["schema_version"] == "event-2.0"
        self.payloads.append(payload)
        self.submissions += 1
        return {"id": "remote-event-1", "status": "IN_QUEUE"}

    def status(self, remote_job_id: str) -> dict[str, Any]:
        assert remote_job_id == "remote-event-1"
        self.polls += 1
        return {"id": remote_job_id, "status": "COMPLETED", "output": self.output}


class _ImmediateClient(_PollingClient):
    def submit(self, payload: Any) -> dict[str, Any]:
        assert payload["schema_version"] == "event-2.0"
        self.payloads.append(payload)
        self.submissions += 1
        return {"id": "remote-event-1", "status": "COMPLETED", "output": self.output}


def test_dispatcher_submits_once_polls_and_persists_terminal_abstention(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _migrate(settings)
    app = create_app(settings)
    receipt = _create_candidate(app)
    client = _PollingClient(_abstained_output(receipt["candidate_id"]))
    try:
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=client,
        )
        with app.state.session_factory() as session:
            interim = session.execute(select(EventAnalysisJob)).scalar_one()
            candidate = session.execute(select(EventCandidate)).scalar_one()
            assert interim.state == EventAnalysisJobState.AWAITING_REMOTE
            assert candidate.state == EventCandidateState.ANALYZING

        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=client,
        )
        assert not run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=client,
        )
        with app.state.session_factory() as session:
            job = session.execute(select(EventAnalysisJob)).scalar_one()
            candidate = session.execute(select(EventCandidate)).scalar_one()
            attempt = session.execute(select(LocalizationAttempt)).scalar_one()
            assert job.state == EventAnalysisJobState.ABSTAINED
            assert job.result_sha256 is not None
            assert candidate.state == EventCandidateState.ABSTAINED
            assert attempt.state == LocalizationAttemptState.ABSTAINED
            assert attempt.geometry_geojson is None
    finally:
        app.state.engine.dispose()
    assert client.submissions == 1
    assert client.polls == 1


def test_reviewable_geometry_remains_private_for_unmatched_incident(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _migrate(settings)
    app = create_app(settings)
    receipt, evidence_asset_id = _create_candidate_with_image(app)
    output = _abstained_output(receipt["candidate_id"])
    output.update(
        {
            "status": "needs_review",
            "view_profile": "ground_wide_known_viewpoint",
            "localization_attempts": [
                {
                    "attempt_id": "LOC-1",
                    "anchor_id": "ANCHOR-1",
                    "phenomenon": "active_fire_point",
                    "status": "localized",
                    "method": "camera_raycast",
                    "geometry_geojson": {"type": "Point", "coordinates": [6.03, 43.30]},
                    "sector": None,
                    "horizontal_accuracy_m": 80,
                    "direction_uncertainty_deg": 2,
                    "distance_uncertainty_m": 70,
                    "reason_codes": [],
                    "model_id": "fireviewer/detector",
                    "model_revision": "immutable-revision",
                    "reference_revision": "terrain-r1",
                    "shadow_only": False,
                }
            ],
            "event_proposals": [
                {
                    "proposal_id": "EVP-1",
                    "attempt_id": "LOC-1",
                    "phenomenon": "active_fire_point",
                    "observed_time": {
                        "start_at": receipt["observed_start_at"],
                        "end_at": None,
                    },
                    "geometry_geojson": {"type": "Point", "coordinates": [6.03, 43.30]},
                    "horizontal_accuracy_m": 80,
                    "status": "DRAFT",
                    "requires_human_review": True,
                }
            ],
            "reason_codes": [],
        }
    )
    _seed_localized_provenance(
        app,
        output=output,
        evidence_asset_id=evidence_asset_id,
    )
    client = _ImmediateClient(output)
    try:
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=client,
        )
        with app.state.session_factory() as session:
            candidate = session.execute(select(EventCandidate)).scalar_one()
            attempt = session.execute(select(LocalizationAttempt)).scalar_one()
            public_event_count = session.scalar(select(func.count()).select_from(FireActivityEvent))
            assert candidate.state == EventCandidateState.NEEDS_REVIEW
            assert attempt.state == LocalizationAttemptState.PROPOSED
            assert attempt.uncertainty_geojson is not None
            assert attempt.anchor_payload["perception"]["evidence_asset_id"] == evidence_asset_id
            assert attempt.provenance["spatial_evidence"]["reference_revision"] == "terrain-r1"
            # The incident is still a private matching candidate, so no incident
            # event can be created or published yet.
            assert public_event_count == 0
        with TestClient(app, raise_server_exceptions=False) as api:
            review_detail = api.get(f"/api/v2/internal/event-candidates/{receipt['candidate_id']}")
            contradictory = api.post(
                f"/api/v2/internal/event-candidates/{receipt['candidate_id']}/review",
                json={
                    "action": "mark_contradictory",
                    "reason": "Les indices visuels et la source externe restent contradictoires.",
                },
            )
            requested = api.post(
                f"/api/v2/internal/event-candidates/{receipt['candidate_id']}/review",
                json={
                    "action": "request_evidence",
                    "reason": "Une orientation de prise de vue est nécessaire.",
                },
            )
            contributor_receipt = api.get(f"/api/v2/me/event-candidates/{receipt['candidate_id']}")
            rejected = api.post(
                f"/api/v2/internal/event-candidates/{receipt['candidate_id']}/review",
                json={
                    "action": "reject",
                    "reason": "Les éléments reçus ne permettent pas de localiser le phénomène.",
                },
            )
        assert review_detail.status_code == 200, review_detail.text
        assert review_detail.headers["cache-control"] == "no-store"
        assert review_detail.json()["viewpoint"]["longitude"] == 6.0214
        assert contradictory.status_code == 200, contradictory.text
        assert contradictory.json()["state"] == "NEEDS_REVIEW"
        assert requested.status_code == 200, requested.text
        assert requested.json()["state"] == "NEEDS_REVIEW"
        assert contributor_receipt.status_code == 200, contributor_receipt.text
        assert contributor_receipt.json()["review_message"].startswith("Une orientation")
        assert "longitude" not in contributor_receipt.json()["viewpoint"]
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["state"] == "REJECTED"
    finally:
        app.state.engine.dispose()


def test_ambiguous_submission_is_never_replayed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _migrate(settings)
    app = create_app(settings)
    receipt = _create_candidate(app)
    client = _ImmediateClient(_abstained_output(receipt["candidate_id"]))
    try:
        with app.state.session_factory() as session:
            job = session.execute(select(EventAnalysisJob)).scalar_one()
            candidate = session.execute(select(EventCandidate)).scalar_one()
            job.state = EventAnalysisJobState.SUBMITTING
            job.submission_started_at = datetime.now(UTC) - timedelta(minutes=10)
            candidate.state = EventCandidateState.ANALYZING
            session.commit()
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=client,
        )
        with app.state.session_factory() as session:
            job = session.execute(select(EventAnalysisJob)).scalar_one()
            candidate = session.execute(select(EventCandidate)).scalar_one()
            assert job.state == EventAnalysisJobState.FAILED
            assert job.last_error_code == "event_submission_outcome_ambiguous"
            assert candidate.state == EventCandidateState.FAILED
    finally:
        app.state.engine.dispose()
    assert client.submissions == 0


def test_worker_anchor_must_reference_media_from_the_persisted_bundle(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _migrate(settings)
    app = create_app(settings)
    receipt, _ = _create_candidate_with_image(app)
    output = _localized_output(
        receipt["candidate_id"],
        suffix="foreign-asset",
        observed_start_at=receipt["observed_start_at"],
    )
    _seed_localized_provenance(
        app,
        output=output,
        evidence_asset_id="EA-not-in-the-persisted-bundle",
    )
    client = _ImmediateClient(output)

    try:
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=client,
        )
        with app.state.session_factory() as session:
            job = session.execute(select(EventAnalysisJob)).scalar_one()
            candidate = session.execute(select(EventCandidate)).scalar_one()
            attempt_count = session.scalar(select(func.count()).select_from(LocalizationAttempt))
            assert job.state == EventAnalysisJobState.FAILED
            assert job.last_error_code == "event_worker_output_invalid"
            assert candidate.state == EventCandidateState.FAILED
            assert attempt_count == 0
    finally:
        app.state.engine.dispose()


def test_worker_cannot_introduce_untrusted_spatial_provenance(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _migrate(settings)
    app = create_app(settings)
    receipt, evidence_asset_id = _create_candidate_with_image(app)
    output = _localized_output(
        receipt["candidate_id"],
        suffix="untrusted-spatial",
        observed_start_at=receipt["observed_start_at"],
    )
    _seed_localized_provenance(
        app,
        output=output,
        evidence_asset_id=evidence_asset_id,
    )
    with app.state.session_factory() as session:
        outbox = session.execute(
            select(OutboxEvent).where(OutboxEvent.topic == "event_candidate.analyze")
        ).scalar_one()
        outbox.payload = {**outbox.payload, "spatial_evidence": []}
        session.commit()

    try:
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=_ImmediateClient(output),
        )
        with app.state.session_factory() as session:
            job = session.execute(select(EventAnalysisJob)).scalar_one()
            candidate = session.execute(select(EventCandidate)).scalar_one()
            attempt_count = session.scalar(select(func.count()).select_from(LocalizationAttempt))
            assert job.state == EventAnalysisJobState.FAILED
            assert job.last_error_code == "event_worker_output_invalid"
            assert candidate.state == EventCandidateState.FAILED
            assert attempt_count == 0
    finally:
        app.state.engine.dispose()


def test_matched_candidate_reaches_validation_then_editor_publication(tmp_path: Path) -> None:
    settings = _settings(tmp_path, publication=True)
    _migrate(settings)
    app = create_app(settings)
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        incident = IncidentSeries(
            fire_id="FR-83-00001",
            territory_code="83",
            sequence=1,
            canonical_name="Incident événementiel test",
            reference_lon=6.02,
            reference_lat=43.29,
            horizontal_uncertainty_m=100,
            bbox_min_lon=5.99,
            bbox_max_lon=6.05,
            bbox_min_lat=43.26,
            bbox_max_lat=43.32,
            public_visibility=PublicVisibility.LIMITED,
            version=1,
        )
        session.add(incident)
        session.flush()
        provider = ExternalProvider(
            provider_key="ground-corroboration-test",
            display_name="Ground corroboration test",
            allowed_domains=["official.example.test"],
            authentication_kind="none",
            attribution="Official test source",
            enabled=True,
        )
        session.add(provider)
        session.flush()
        collection = ExternalCollection(
            provider_id=provider.id,
            collection_key="validated-ground-observations",
            product_name="Validated ground observations",
            sensor=None,
            platform=None,
            license="Open test license",
            cadence_seconds=300,
            semantic_role=ExternalSemanticRole.INTERPRETED_OBSERVATION,
            configuration={},
        )
        session.add(collection)
        session.flush()
        artifact = ExternalArtifactRevision(
            artifact_revision_id="EAR-ground-corroboration",
            collection_id=collection.id,
            external_product_id="ground-product-1",
            source_url="https://official.example.test/ground-product-1",
            revision=1,
            content_hash="d" * 64,
            acquisition_start_at=now - timedelta(minutes=5),
            retrieved_at=now,
            quality_flags={},
            license="Open test license",
            attribution="Official test source",
            status=ExternalArtifactStatus.VALIDATED,
            semantic_role=ExternalSemanticRole.INTERPRETED_OBSERVATION,
        )
        session.add(artifact)
        session.flush()
        session.add(
            ExternalClaim(
                claim_id="ECL-ground-corroboration",
                artifact_revision_id=artifact.id,
                incident_id=incident.id,
                assertion_kind="active_fire_point",
                assertion_payload={},
                geometry_geojson={"type": "Point", "coordinates": [6.031, 43.301]},
                confidence=0.9,
                independent_family_key="GROUND-FAMILY-1",
            )
        )
        session.add(
            Episode(
                incident_id=incident.id,
                episode_id="E01",
                ordinal=1,
                status=IncidentStatus.MONITORING,
                verification_state=VerificationState.VERIFIED,
                evidence_basis_at=now,
                review_required=False,
                is_current=True,
                confidence_policy="event-v2-test",
                started_at=now - timedelta(hours=1),
                last_observed_at=now,
                validated_at=now,
                version=1,
            )
        )
        session.commit()
    payload = _submission()
    payload["incident_id"] = "FR-83-00001"
    payload["consent"]["public_derivative"] = True
    with TestClient(app, raise_server_exceptions=False) as api:
        evidence_asset_id = _upload_test_image(api)
        payload["evidence_asset_ids"] = [evidence_asset_id]
        created = api.post("/api/v2/event-candidates", json=payload)
    assert created.status_code == 202, created.text
    output = _abstained_output(created.json()["candidate_id"])
    output.update(
        {
            "status": "needs_review",
            "view_profile": "ground_wide_known_viewpoint",
            "localization_attempts": [
                {
                    "attempt_id": "LOC-matched-1",
                    "anchor_id": "ANCHOR-matched-1",
                    "phenomenon": "active_fire_point",
                    "status": "localized",
                    "method": "camera_raycast",
                    "geometry_geojson": {"type": "Point", "coordinates": [6.03, 43.30]},
                    "sector": None,
                    "horizontal_accuracy_m": 60,
                    "direction_uncertainty_deg": 2,
                    "distance_uncertainty_m": 55,
                    "reason_codes": [],
                    "model_id": "fireviewer/detector",
                    "model_revision": "immutable-revision",
                    "reference_revision": "terrain-r1",
                    "shadow_only": False,
                }
            ],
            "event_proposals": [
                {
                    "proposal_id": "FAE-MATCHED-1",
                    "attempt_id": "LOC-matched-1",
                    "phenomenon": "active_fire_point",
                    "observed_time": {
                        "start_at": created.json()["observed_start_at"],
                        "end_at": None,
                    },
                    "geometry_geojson": {"type": "Point", "coordinates": [6.03, 43.30]},
                    "horizontal_accuracy_m": 60,
                    "status": "DRAFT",
                    "requires_human_review": True,
                }
            ],
            "reason_codes": [],
        }
    )
    _seed_localized_provenance(
        app,
        output=output,
        evidence_asset_id=evidence_asset_id,
    )
    try:
        worker_client = _ImmediateClient(output)
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=worker_client,
        )
        external_observations = worker_client.payloads[0]["bundle"]["external_observations"]
        assert external_observations == [
            {
                "observation_id": "ECL-ground-corroboration",
                "artifact_revision_id": "EAR-ground-corroboration",
                "lineage_family_id": "GROUND-FAMILY-1",
                "semantic_role": "interpreted_observation",
                "phenomenon": "active_fire_point",
                "observed_at": (now - timedelta(minutes=5)).isoformat(),
                "geometry_geojson": {"type": "Point", "coordinates": [6.031, 43.301]},
                "resolution_m": None,
                "conflicts_with": [],
            }
        ]
        with app.state.session_factory() as session:
            event = session.execute(select(FireActivityEvent)).scalar_one()
            event_id = event.event_id
            assert event.state.value == "DRAFT"
        with TestClient(app, raise_server_exceptions=False) as api:
            queue = api.get("/api/v2/internal/event-candidates")
            validated = api.post(
                f"/api/v2/internal/fire-activity-events/{event_id}/validate",
                json={"reason": "Localisation et incertitude contrôlées par l'analyste."},
            )
            contributor_receipt = api.get(
                f"/api/v2/me/event-candidates/{created.json()['candidate_id']}"
            )
        with app.state.session_factory() as session:
            candidate = session.execute(select(EventCandidate)).scalar_one()
            candidate.consent_public_derivative = False
            session.commit()
        with TestClient(app, raise_server_exceptions=False) as api:
            publication_without_consent = api.post(
                f"/api/v2/internal/fire-activity-events/{event_id}/publish",
                json={"reason": "Tentative de publication sans consentement dérivé."},
            )
        with app.state.session_factory() as session:
            candidate = session.execute(select(EventCandidate)).scalar_one()
            candidate.consent_public_derivative = True
            session.commit()
        with TestClient(app, raise_server_exceptions=False) as api:
            published = api.post(
                f"/api/v2/internal/fire-activity-events/{event_id}/publish",
                json={"reason": "Publication éditoriale après validation des preuves."},
            )
            public_timeline = api.get("/api/v2/incidents/FR-83-00001/timeline")
        assert queue.status_code == 200, queue.text
        assert queue.headers["cache-control"] == "no-store"
        assert queue.json()["total"] == 1
        assert validated.status_code == 200, validated.text
        assert validated.json()["state"] == "ANALYST_VALIDATED"
        assert contributor_receipt.json()["state"] == "VALIDATED"
        assert publication_without_consent.status_code == 409
        assert (
            publication_without_consent.json()["type"]
            == "urn:fire-viewer:error:public_derivative_consent_required"
        )
        assert published.status_code == 200, published.text
        assert published.json()["state"] == "EDITOR_PUBLISHED"
        assert public_timeline.status_code == 200, public_timeline.text
        assert public_timeline.headers["cache-control"].startswith("public")
        assert public_timeline.json()["revision"] == 1
        public_event = public_timeline.json()["events"][0]
        assert public_event["event_id"] == event_id
        assert "viewpoint" not in public_event
        assert "owner_subject" not in public_event
        assert "evidence_asset_ids" not in public_event
        with TestClient(app, raise_server_exceptions=False) as api:
            retracted = api.post(
                f"/api/v2/internal/fire-activity-events/{event_id}/retract",
                json={"reason": "Rétractation éditoriale après réception d'une contradiction."},
            )
            public_after_retraction = api.get("/api/v2/incidents/FR-83-00001/timeline")
        assert retracted.status_code == 200, retracted.text
        assert retracted.json()["state"] == "RETRACTED"
        assert public_after_retraction.status_code == 200
        assert public_after_retraction.json() == {
            "incident_id": "FR-83-00001",
            "revision": 1,
            "events": [],
        }
        with app.state.session_factory() as session:
            snapshot = session.execute(select(PublicationSnapshot)).scalar_one()
            assert "viewpoint" not in snapshot.public_payload
            assert snapshot.public_payload["event_id"] == event_id
            assert snapshot.retracted_at is not None
            assert snapshot.retraction_reason is not None
            with pytest.raises(DBAPIError, match="payload is immutable"):
                session.execute(
                    update(PublicationSnapshot)
                    .where(PublicationSnapshot.id == snapshot.id)
                    .values(public_payload={"tampered": True})
                )
            session.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(
                    delete(PublicationSnapshot).where(PublicationSnapshot.id == snapshot.id)
                )
            session.rollback()
    finally:
        app.state.engine.dispose()


def test_private_incident_candidate_can_be_attached_without_reanalysis(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _migrate(settings)
    app = create_app(settings)
    now = datetime.now(UTC)
    with app.state.session_factory() as session:
        incident = IncidentSeries(
            fire_id="FR-83-00002",
            territory_code="83",
            sequence=2,
            canonical_name="Incident de rattachement test",
            reference_lon=6.02,
            reference_lat=43.29,
            horizontal_uncertainty_m=100,
            bbox_min_lon=5.99,
            bbox_max_lon=6.05,
            bbox_min_lat=43.26,
            bbox_max_lat=43.32,
            public_visibility=PublicVisibility.LIMITED,
            version=1,
        )
        session.add(incident)
        session.flush()
        session.add(
            Episode(
                incident_id=incident.id,
                episode_id="E01",
                ordinal=1,
                status=IncidentStatus.MONITORING,
                verification_state=VerificationState.VERIFIED,
                evidence_basis_at=now,
                review_required=False,
                is_current=True,
                confidence_policy="event-v2-test",
                started_at=now - timedelta(hours=1),
                last_observed_at=now,
                validated_at=now,
                version=1,
            )
        )
        session.commit()
    receipt, evidence_asset_id = _create_candidate_with_image(app)
    output = _localized_output(
        receipt["candidate_id"],
        suffix="ATTACH-1",
        observed_start_at=receipt["observed_start_at"],
    )
    _seed_localized_provenance(
        app,
        output=output,
        evidence_asset_id=evidence_asset_id,
    )
    shadow_anchor = {
        "anchor_id": "ANCHOR-ATTACH-SHADOW",
        "evidence_asset_id": evidence_asset_id,
        "phenomenon": "active_fire_point",
        "source_point_normalized": [0.6, 0.45],
        "source_geometry_normalized": None,
        "model_id": "fireviewer/cross-view",
        "model_revision": "shadow-revision",
        "model_score": 0.7,
    }
    shadow_spatial = {
        "anchor_id": "ANCHOR-ATTACH-SHADOW",
        "status": "projected",
        "method": "cross_view_raycast",
        "geometry_geojson": {"type": "Point", "coordinates": [6.04, 43.31]},
        "horizontal_accuracy_m": 200,
        "direction_uncertainty_deg": 5,
        "distance_uncertainty_m": 180,
        "reason_codes": [],
        "reference_revision": "cross-view-shadow-reference",
    }
    output["perception_anchors"].append(shadow_anchor)
    output["spatial_evidence"].append(shadow_spatial)
    output["localization_attempts"].append(
        {
            "attempt_id": "LOC-ATTACH-SHADOW",
            "anchor_id": "ANCHOR-ATTACH-SHADOW",
            "phenomenon": "active_fire_point",
            "status": "localized",
            "method": "cross_view_raycast",
            "geometry_geojson": shadow_spatial["geometry_geojson"],
            "sector": None,
            "horizontal_accuracy_m": 200,
            "direction_uncertainty_deg": 5,
            "distance_uncertainty_m": 180,
            "reason_codes": ["cross_view_shadow_only"],
            "model_id": "fireviewer/cross-view",
            "model_revision": "shadow-revision",
            "reference_revision": "cross-view-shadow-reference",
            "shadow_only": True,
        }
    )
    with app.state.session_factory() as session:
        outbox = session.execute(
            select(OutboxEvent).where(OutboxEvent.topic == "event_candidate.analyze")
        ).scalar_one()
        outbox.payload = {
            **outbox.payload,
            "perception_anchors": [*outbox.payload["perception_anchors"], shadow_anchor],
            "spatial_evidence": [*outbox.payload["spatial_evidence"], shadow_spatial],
        }
        session.commit()
    try:
        assert run_event_dispatcher_once(
            app.state.session_factory,
            worker_id="event-worker:test",
            settings=settings,
            client=_ImmediateClient(output),
        )
        with app.state.session_factory() as session:
            shadow_attempt = session.execute(
                select(LocalizationAttempt).where(
                    LocalizationAttempt.attempt_id == "LOC-ATTACH-SHADOW"
                )
            ).scalar_one()
            assert shadow_attempt.state == LocalizationAttemptState.SHADOW
        with TestClient(app, raise_server_exceptions=False) as api:
            private_detail = api.get(f"/api/v2/internal/event-candidates/{receipt['candidate_id']}")
        assert private_detail.status_code == 200, private_detail.text
        assert any(
            attempt["attempt_id"] == "LOC-ATTACH-SHADOW" and attempt["state"] == "SHADOW"
            for attempt in private_detail.json()["localization_attempts"]
        )
        # Simulate an imported legacy row carrying an inconsistent PROPOSED state.
        # The attachment service must still reject it from event materialization.
        with app.state.session_factory() as session:
            shadow_attempt = session.execute(
                select(LocalizationAttempt).where(
                    LocalizationAttempt.attempt_id == "LOC-ATTACH-SHADOW"
                )
            ).scalar_one()
            shadow_attempt.state = LocalizationAttemptState.PROPOSED
            session.commit()
        with TestClient(app, raise_server_exceptions=False) as api:
            attached = api.post(
                f"/api/v2/internal/event-candidates/{receipt['candidate_id']}/attach-incident",
                json={
                    "incident_id": "FR-83-00002",
                    "reason": "Le dossier correspond au même incident après revue spatiale.",
                },
            )
        assert attached.status_code == 200, attached.text
        assert attached.json()["state"] == "NEEDS_REVIEW"
        with app.state.session_factory() as session:
            event_id = session.execute(select(FireActivityEvent.event_id)).scalar_one()
        with TestClient(app, raise_server_exceptions=False) as api:
            validated = api.post(
                f"/api/v2/internal/fire-activity-events/{event_id}/validate",
                json={"reason": "La localisation est recevable avant décision finale."},
            )
            rejected = api.post(
                f"/api/v2/internal/fire-activity-events/{event_id}/reject",
                json={"reason": "La géométrie proposée est rejetée après contrôle analyste."},
            )
        assert validated.status_code == 200, validated.text
        assert validated.json()["state"] == "ANALYST_VALIDATED"
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["state"] == "RETRACTED"
        with app.state.session_factory() as session:
            candidate = session.execute(select(EventCandidate)).scalar_one()
            private_incident = session.execute(select(IncidentCandidate)).scalar_one()
            event = session.execute(select(FireActivityEvent)).scalar_one()
            assert candidate.incident_id is not None
            assert candidate.incident_candidate_id is None
            assert candidate.state == EventCandidateState.REJECTED
            assert private_incident.state == IncidentCandidateState.MERGED
            assert event.state == FireActivityEventState.RETRACTED
    finally:
        app.state.engine.dispose()
