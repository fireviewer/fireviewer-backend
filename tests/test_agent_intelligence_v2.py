from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentDeadLetter,
    AgentDispatch,
    AgentFactProposal,
    AgentSituationReportRevision,
    AgentSourceAnnotation,
    AgentSpatialProposal,
    IncidentSpatialMarker,
    Job,
)
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    AgentAnalysisState,
    AgentDispatchState,
    AgentProposalReviewState,
    AgentReportReviewState,
)
from fire_viewer.services.agent_dispatcher import run_dispatcher_once
from fire_viewer.services.agent_traceability import get_spatial_proposal_trace


class FakeRunPodV2:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.submissions = 0

    def submit(self, _payload) -> dict[str, Any]:
        self.submissions += 1
        return {"id": "runpod-v2-job-0001", "status": "IN_QUEUE"}

    def status(self, _remote_job_id: str) -> dict[str, Any]:
        return {
            "id": "runpod-v2-job-0001",
            "status": "COMPLETED",
            "executionTime": 2100,
            "delayTime": 40,
            "output": self.output,
        }

    def cancel(self, _remote_job_id: str) -> dict[str, Any]:
        return {"id": "runpod-v2-job-0001", "status": "CANCELLED"}


def _v2_payload(*, fire_id: str, episode_id: str) -> dict[str, object]:
    now = datetime.now(UTC)
    window_start = now - timedelta(days=1)
    window_end = window_start + timedelta(hours=23, minutes=59)
    return {
        "schema_version": "2.0",
        "batch_id": "agent-v2-batch-0001",
        "batch_type": "external_media",
        "priority": "scheduled_combined",
        "analysis_window": {
            "analysis_id": "analysis-die-2026-07-09",
            "fire_id": fire_id,
            "episode_id": episode_id,
            "window_start_at": window_start.isoformat(),
            "window_end_at": window_end.isoformat(),
            "local_date": window_start.date().isoformat(),
            "timezone": "Europe/Paris",
        },
        "purge_after": (now + timedelta(days=2)).isoformat(),
        "reference_bundle": {
            "reference_id": "die-reference-r1",
            "manifest_sha256": "d" * 64,
            "assets": [
                {
                    "kind": "scene_catalog",
                    "working_file_url": "https://localhost/private/catalog.json?signature=test",
                    "sha256": "e" * 64,
                    "crs": "EPSG:2154",
                }
            ],
        },
        "items": [
            {
                "input_id": "media-die-0001",
                "media_type": "image",
                "working_file_url": "https://localhost/private/die-0001.jpg?signature=test",
                "media_sha256": "a" * 64,
                "size_bytes": 4096,
                "provenance": {
                    "source_key": "press-die-0001",
                    "source_reference_url": "https://example.test/die/source",
                    "license_identifier": "PRESS-TEST-AUTHORIZED",
                    "attribution": "Source de test",
                    "trust": "unverified",
                },
                "captured_at": window_start.isoformat(),
                "article_text": "La source indique 120 personnes engagées.",
                "camera": {
                    "latitude": 44.753,
                    "longitude": 5.371,
                    "horizontal_accuracy_m": 100,
                    "pose_origin": "USER_DECLARED",
                },
                "consent": {
                    "basis": "source_license",
                    "scopes": ["temporary_storage", "agent_analysis", "human_review"],
                    "terms_version": "firewarning-media-v2",
                    "evidence_sha256": "b" * 64,
                    "source_reference_url": "https://example.test/die/source",
                    "license_identifier": "PRESS-TEST-AUTHORIZED",
                    "granted_at": now.isoformat(),
                },
            }
        ],
    }


def _v2_output(*, analysis_id: str = "analysis-die-2026-07-09") -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "schema_version": "2.0",
        "batch_id": "agent-v2-batch-0001",
        "analysis_id": analysis_id,
        "status": "succeeded",
        "retryable": False,
        "orchestration_contract_digest": "e" * 64,
        "stage_traces": [
            {
                "stage_role": "visual_grounding",
                "contract_id": "stage.visual_grounding.v1",
                "sequence": 1,
                "status": "succeeded",
                "retryable": False,
                "preflight": {
                    "phase": "preflight",
                    "decision": "pass",
                    "reason_codes": ["capabilities_available"],
                    "available_capabilities": ["visual_grounding"],
                    "missing_capabilities": [],
                    "downstream_possible": True,
                },
                "postflight": {
                    "phase": "postflight",
                    "decision": "pass",
                    "reason_codes": ["output_validated"],
                    "available_capabilities": ["visual_grounding"],
                    "missing_capabilities": [],
                    "downstream_possible": True,
                },
                "attempts": [
                    {
                        "attempt": 1,
                        "kind": "initial",
                        "status": "succeeded",
                        "started_at": (now - timedelta(seconds=3)).isoformat(),
                        "finished_at": now.isoformat(),
                        "inference_ms": 900,
                        "peak_vram_bytes": 4_000_000_000,
                    }
                ],
            }
        ],
        "model_runs": [
            {
                "model_role": "visual_grounding",
                "model_id": "microsoft/Florence-2-large-ft",
                "revision": "florence-test-rev",
                "status": "succeeded",
                "started_at": (now - timedelta(seconds=3)).isoformat(),
                "finished_at": now.isoformat(),
                "load_ms": 800,
                "inference_ms": 900,
                "peak_vram_bytes": 4_000_000_000,
            }
        ],
        "items": [
            {
                "input_id": "media-die-0001",
                "source_annotations": [
                    {
                        "annotation_id": "annotation-fire-0001",
                        "evidence_id": "media-die-0001",
                        "evidence_kind": "image",
                        "semantic_anchor": "active_fire_point",
                        "source_point_normalized": [0.43, 0.57],
                        "model_score": 0.88,
                    }
                ],
                "spatial_proposals": [
                    {
                        "proposal_id": "spatial-fire-0001",
                        "annotation_id": "annotation-fire-0001",
                        "status": "ground_point",
                        "observed_at": now.isoformat(),
                        "geometry_origin": "CROSS_VIEW_RAYCAST",
                        "longitude": 5.369,
                        "latitude": 44.751,
                        "altitude_m": 825,
                        "horizontal_accuracy_m": 180,
                        "reference_bundle_sha256": "d" * 64,
                        "uncertainty_codes": ["single_view"],
                    },
                    {
                        "proposal_id": "spatial-abstention-0001",
                        "status": "insufficient_geometry",
                        "uncertainty_codes": ["camera_orientation_missing"],
                    },
                ],
                "fact_proposals": [
                    {
                        "fact_id": "fact-resources-0001",
                        "input_id": "media-die-0001",
                        "category": "resources",
                        "fact_key": "teams_engaged",
                        "as_of": now.isoformat(),
                        "evidence_kind": "article_text",
                        "evidence_id": "media-die-0001",
                        "certainty": "explicitly_written",
                        "value_number": 120,
                        "unit": "people",
                        "summary": "La source indique 120 personnes engagées.",
                    }
                ],
                "explicit_places": [],
                "explicit_times": [],
                "requires_human_review": True,
            }
        ],
        "report_draft": {
            "title": "Situation du jour",
            "body_markdown": "Brouillon privé à vérifier avant toute publication.",
            "sections": [
                {
                    "key": "resources",
                    "heading": "Moyens engagés",
                    "body": "La source indique 120 personnes engagées.",
                    "fact_ids": ["fact-resources-0001"],
                    "basis_codes": [],
                }
            ],
        },
        "validation_errors": [],
        "boot_ms": 1100,
    }


def _create_and_enqueue_v2(client, session, seed_incident) -> AgentDispatch:
    incident, episode = seed_incident(
        fire_id="FR-26-00001",
        sequence=1,
        lon=5.371,
        lat=44.753,
        canonical_name="Die - massif de Justin",
    )
    created = client.post(
        "/api/v2/admin/agent-batches",
        headers={"Idempotency-Key": "agent-v2-idempotency-0001"},
        json=_v2_payload(fire_id=incident.fire_id, episode_id=episode.episode_id),
    )
    assert created.status_code == 201, created.text
    assert created.json()["analysis_id"] == "analysis-die-2026-07-09"
    enqueued = client.post("/api/v2/admin/agent-batches/agent-v2-batch-0001/enqueue")
    assert enqueued.status_code == 200, enqueued.text
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    dispatch.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.expected_models = {"visual_grounding": "florence-test-rev"}
    session.commit()
    return dispatch


def _run_to_completion(app, session, settings, runpod: FakeRunPodV2) -> None:
    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-v2-test",
        settings=settings,
        client=runpod,
    )
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    dispatch.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-v2-test",
        settings=settings,
        client=runpod,
    )


def test_v2_result_stays_private_and_persists_grounding_abstention_and_report(
    client, session, app, settings, seed_incident
) -> None:
    dispatch = _create_and_enqueue_v2(client, session, seed_incident)
    assert dispatch.payload["schema_version"] == "2.0"
    assert dispatch.payload["analysis_window"]["analysis_id"] == "analysis-die-2026-07-09"
    assert dispatch.payload["reference_bundle"]["manifest_sha256"] == "d" * 64
    assert "consent" not in dispatch.payload["items"][0]
    assert session.scalar(select(func.count()).select_from(Job)) == 0

    _run_to_completion(app, session, settings, FakeRunPodV2(_v2_output()))

    session.expire_all()
    completed = session.scalar(select(AgentDispatch))
    analysis = session.scalar(select(AgentAnalysisWindow))
    proposals = list(
        session.scalars(select(AgentSpatialProposal).order_by(AgentSpatialProposal.proposal_id))
    )
    assert completed is not None and completed.state == AgentDispatchState.SUCCEEDED, (
        completed.last_error_detail if completed is not None else "missing dispatch"
    )
    assert analysis is not None and analysis.state == AgentAnalysisState.REVIEW_PENDING
    assert session.scalar(select(func.count()).select_from(AgentSourceAnnotation)) == 1
    assert [proposal.status for proposal in proposals] == [
        "insufficient_geometry",
        "ground_point",
    ]
    grounded = next(proposal for proposal in proposals if proposal.status == "ground_point")
    assert grounded.proposal_kind == "legacy_ground_point"
    assert grounded.geometry_geojson == {
        "type": "Point",
        "coordinates": [5.369, 44.751],
    }
    annotation = session.scalar(select(AgentSourceAnnotation))
    assert annotation is not None
    assert annotation.source_geometry_normalized == {
        "type": "Point",
        "coordinates": [0.43, 0.57],
    }
    trace = get_spatial_proposal_trace(
        session,
        proposal_id="spatial-fire-0001",
    )
    assert trace is not None
    assert trace.analysis_window.analysis_id == "analysis-die-2026-07-09"
    assert trace.source.input_id == "media-die-0001"
    assert trace.source.source_reference_url == "https://example.test/die/source"
    assert trace.annotation is not None
    assert trace.annotation.annotation_id == "annotation-fire-0001"
    assert trace.proposal_kind == "legacy_ground_point"
    assert all(proposal.review_state == AgentProposalReviewState.PENDING for proposal in proposals)
    assert session.scalar(select(func.count()).select_from(AgentFactProposal)) == 1
    zone = session.scalar(select(ActiveFireZoneRevision))
    assert zone is not None
    assert zone.analysis_window_id == analysis.id
    assert zone.geometry_origin == "AGENT_DERIVED"
    assert zone.review_state.value == "DRAFT"
    assert zone.supporting_marker_ids == ["proposal:spatial-fire-0001"]
    assert zone.geometry_geojson["type"] == "MultiPolygon"
    report = session.scalar(select(AgentSituationReportRevision))
    assert report is not None and report.review_state == AgentReportReviewState.DRAFT
    assert session.scalar(select(func.count()).select_from(IncidentSpatialMarker)) == 0
    assert session.scalar(select(func.count()).select_from(AgentDeadLetter)) == 0


def test_v2_rejects_output_bound_to_another_analysis_window(
    client, session, app, settings, seed_incident
) -> None:
    _create_and_enqueue_v2(client, session, seed_incident)
    _run_to_completion(
        app,
        session,
        settings,
        FakeRunPodV2(_v2_output(analysis_id="analysis-wrong-day")),
    )

    session.expire_all()
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None and dispatch.state == AgentDispatchState.DEAD_LETTER
    assert dispatch.last_error_code == "agent_worker_output_invalid"
    assert session.scalar(select(func.count()).select_from(AgentSpatialProposal)) == 0
    assert session.scalar(select(func.count()).select_from(AgentFactProposal)) == 0


def test_human_edit_keeps_the_agent_analysis_day(
    client, session, app, settings, seed_incident
) -> None:
    _create_and_enqueue_v2(client, session, seed_incident)
    _run_to_completion(app, session, settings, FakeRunPodV2(_v2_output()))

    proposal = session.scalar(
        select(AgentSpatialProposal).where(AgentSpatialProposal.status == "ground_point")
    )
    analysis = session.scalar(select(AgentAnalysisWindow))
    zone = session.scalar(select(ActiveFireZoneRevision))
    assert proposal is not None and analysis is not None and zone is not None
    reviewed = client.post(
        f"/api/v1/admin/incidents/FR-26-00001/spatial-markers/proposal:{proposal.proposal_id}/review",
        json={
            "action": "validate",
            "expected_version": proposal.version,
            "reason": "Point actif et géolocalisation contrôlés dans la preuve source.",
        },
    )
    assert reviewed.status_code == 200, reviewed.text

    edited = client.post(
        "/api/v1/admin/incidents/FR-26-00001/active-zone-revisions",
        json={
            "expected_latest_revision": zone.revision,
            "valid_at": "2026-07-19T21:00:00Z",
            "analysis_id": analysis.analysis_id,
            "geometry_geojson": zone.geometry_geojson,
            "supporting_marker_ids": [f"proposal:{proposal.proposal_id}"],
            "reason": "Contour quotidien corrigé manuellement sans changer sa journée d'analyse.",
        },
    )

    assert edited.status_code == 201, edited.text
    assert edited.json()["analysis_id"] == analysis.analysis_id
    returned_valid_at = datetime.fromisoformat(edited.json()["valid_at"].replace("Z", "+00:00"))
    assert returned_valid_at.replace(tzinfo=None) == analysis.window_end_at.replace(tzinfo=None)
    persisted = session.scalar(
        select(ActiveFireZoneRevision).where(ActiveFireZoneRevision.revision == 2)
    )
    assert persisted is not None and persisted.analysis_window_id == analysis.id


def test_withdrawing_consent_invalidates_private_v2_results(
    client, session, app, settings, seed_incident
) -> None:
    _create_and_enqueue_v2(client, session, seed_incident)
    _run_to_completion(app, session, settings, FakeRunPodV2(_v2_output()))

    response = client.post(
        "/api/v2/admin/agent-batches/agent-v2-batch-0001/items/media-die-0001/consent/withdraw",
        json={"reason": "Retrait explicite après analyse et avant validation humaine."},
    )
    assert response.status_code == 200, response.text

    session.expire_all()
    assert all(
        proposal.review_state == AgentProposalReviewState.INVALIDATED
        for proposal in session.scalars(select(AgentSpatialProposal))
    )
    fact = session.scalar(select(AgentFactProposal))
    report = session.scalar(select(AgentSituationReportRevision))
    zone = session.scalar(select(ActiveFireZoneRevision))
    assert fact is not None and fact.review_state == AgentProposalReviewState.INVALIDATED
    assert report is not None and report.review_state == AgentReportReviewState.INVALIDATED
    assert zone is not None and zone.review_state == ActiveFireZoneReviewState.REJECTED
