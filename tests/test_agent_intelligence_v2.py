from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select

from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentDeadLetter,
    AgentDispatch,
    AgentFactProposal,
    AgentMediaItem,
    AgentSituationReportRevision,
    AgentSourceAnnotation,
    AgentSpatialProposal,
    AgentValidationCampaignDay,
    IncidentMapCapture,
    IncidentSpatialMarker,
    Job,
)
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    AgentAnalysisState,
    AgentDispatchState,
    AgentProposalReviewState,
    AgentReportReviewState,
    AgentValidationCampaignDayState,
)
from fire_viewer.services.agent_daily_consolidation import (
    _select_facts,
    _select_spatial_proposals,
)
from fire_viewer.services.agent_dispatcher import run_dispatcher_once
from fire_viewer.services.agent_traceability import get_spatial_proposal_trace
from fire_viewer.services.agent_validation_campaigns import create_campaign_from_manifest


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


def _v2_payload(
    *, fire_id: str, episode_id: str, batch_type: str = "external_media"
) -> dict[str, object]:
    now = datetime.now(UTC)
    window_start = now - timedelta(days=1)
    window_end = window_start + timedelta(hours=23, minutes=59)
    return {
        "schema_version": "2.0",
        "batch_id": "agent-v2-batch-0001",
        "batch_type": batch_type,
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


def _v2_output(
    *,
    analysis_id: str = "analysis-die-2026-07-09",
    status: str = "succeeded",
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "schema_version": "2.0",
        "batch_id": "agent-v2-batch-0001",
        "analysis_id": analysis_id,
        "status": status,
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


def _create_and_enqueue_v2(
    client, session, seed_incident, *, batch_type: str = "external_media"
) -> AgentDispatch:
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
        json=_v2_payload(
            fire_id=incident.fire_id, episode_id=episode.episode_id, batch_type=batch_type
        ),
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


def _canonical_sha256(payload: dict[str, object], excluded_key: str) -> str:
    normalized = {key: value for key, value in payload.items() if key != excluded_key}
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def test_daily_deduplication_preserves_independent_sources() -> None:
    observed_at = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)

    def fact(fact_id: str, source_media_item_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            fact_id=fact_id,
            source_media_item_id=source_media_item_id,
            evidence_kind="image",
            evidence_id=f"image-{source_media_item_id}",
            category="resources",
            fact_key="firefighters",
            as_of=observed_at,
            value_number=120.0,
            value_boolean=None,
            value_text=None,
            unit="people",
            summary="120 pompiers engagés.",
            certainty="explicitly_written",
        )

    first_fact = fact("fact-1", 10)
    duplicate_fact = fact("fact-duplicate", 10)
    corroborating_fact = fact("fact-2", 20)
    selected_facts, contradictions = _select_facts([first_fact, duplicate_fact, corroborating_fact])
    assert [item.fact_id for item in selected_facts] == ["fact-1", "fact-2"]
    assert contradictions == []

    def proposal(
        proposal_id: str,
        source_media_item_id: int,
        source_annotation_id: int,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            proposal_id=proposal_id,
            source_media_item_id=source_media_item_id,
            source_annotation_id=source_annotation_id,
            status="projected_geometry",
            proposal_kind="active_fire_point",
            observed_at=observed_at,
            geometry_geojson={"type": "Point", "coordinates": [2.7, 48.4]},
            geometry_origin="SATELLITE_GEOTRANSFORM",
            horizontal_accuracy_m=20.0,
            uncertainty_codes=[],
            reference_bundle_sha256="a" * 64,
        )

    first_proposal = proposal("proposal-1", 10, 100)
    duplicate_proposal = proposal("proposal-duplicate", 10, 100)
    corroborating_proposal = proposal("proposal-2", 20, 200)
    selected_proposals = _select_spatial_proposals(
        [first_proposal, duplicate_proposal, corroborating_proposal]
    )
    assert [item.proposal_id for item in selected_proposals] == [
        "proposal-1",
        "proposal-2",
    ]


def test_campaign_creates_one_daily_consolidation_after_all_required_operations(
    client, session, app, settings, seed_incident, tmp_path
) -> None:
    dispatch = _create_and_enqueue_v2(client, session, seed_incident, batch_type="user_media")
    window = dispatch.batch.analysis_window
    media = session.scalar(select(AgentMediaItem))
    assert window is not None and media is not None and media.media_sha256 is not None
    cutoff_at = window.window_end_at
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=UTC)
    day: dict[str, object] = {
        "ordinal": 1,
        "fire_id": "FR-26-00001",
        "local_date": window.local_date.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "allowed_media_sha256": [media.media_sha256],
        "required_operations": ["user_media"],
        "declared_absences": ["source_research", "satellite_media"],
    }
    day["manifest_sha256"] = _canonical_sha256(day, "manifest_sha256")
    campaign: dict[str, object] = {
        "schema_version": "2.0",
        "campaign_id": "daily-consolidation-test",
        "days": [day],
    }
    campaign["manifest_sha256"] = _canonical_sha256(campaign, "manifest_sha256")
    manifest_path = tmp_path / "daily-consolidation-campaign.json"
    manifest_path.write_text(json.dumps(campaign), encoding="utf-8")
    create_campaign_from_manifest(
        session,
        manifest_path=manifest_path,
        created_by="test-suite",
    )
    campaign_day = session.scalar(select(AgentValidationCampaignDay))
    assert campaign_day is not None
    campaign_day.state = AgentValidationCampaignDayState.RUNNING
    session.commit()

    output = _v2_output(status="partial_failure")
    first_item = output["items"][0]
    assert isinstance(first_item, dict)
    first_proposal = first_item["spatial_proposals"][0]
    assert isinstance(first_proposal, dict)
    first_proposal.update(
        {
            "status": "projected_geometry",
            "proposal_kind": "active_fire_point",
            "geometry_geojson": {
                "type": "Point",
                "coordinates": [5.369, 44.751],
            },
        }
    )
    _run_to_completion(
        app,
        session,
        settings,
        FakeRunPodV2(output),
    )

    session.expire_all()
    campaign_day = session.scalar(select(AgentValidationCampaignDay))
    reports = list(session.scalars(select(AgentSituationReportRevision)))
    zone = session.scalar(select(ActiveFireZoneRevision))
    assert campaign_day is not None
    assert campaign_day.state == AgentValidationCampaignDayState.REVIEW
    assert len(reports) == 1
    report = reports[0]
    assert report.created_by == "daily-intelligence-consolidator"
    assert report.reason.startswith("Daily intelligence consolidated")
    assert report.sections_payload[0]["key"] == "_daily_consolidation"
    assert report.sections_payload[0]["operation_outcomes"]["user_media"] == {
        "outcome": "partial_failure",
        "terminal": True,
        "states": ["PARTIAL_FAILURE"],
    }
    assert report.sections_payload[0]["operation_outcomes"]["source_research"] == {
        "outcome": "absent",
        "terminal": True,
        "states": [],
    }
    assert report.sections_payload[0]["spatial_counts"] == {
        "active_fire_point": 1,
        "insufficient_geometry": 1,
    }
    assert len(report.fact_links) == 1
    assert zone is not None
    assert zone.analysis_window_id == window.id

    workspace = client.get("/api/v1/admin/incidents/FR-26-00001/spatial-review")
    assert workspace.status_code == 200, workspace.text
    daily = workspace.json()["daily_intelligence"]
    assert len(daily) == 1
    assert daily[0]["analysis_id"] == window.analysis_id
    assert daily[0]["report"]["report_revision_id"] == report.report_revision_id
    assert daily[0]["operation_outcomes"]["user_media"]["outcome"] == "partial_failure"
    assert len(daily[0]["facts"]) == 1
    fact_payload = daily[0]["facts"][0]
    assert fact_payload["source"]["batch_id"] == dispatch.batch.batch_id
    reviewed_fact = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/agent-facts/"
            f"{fact_payload['fact_id']}/review"
        ),
        json={
            "action": "validate",
            "expected_version": fact_payload["version"],
            "reason": "Fait sourcé contrôlé dans la revue existante.",
        },
    )
    assert reviewed_fact.status_code == 200, reviewed_fact.text
    assert reviewed_fact.json()["review_state"] == "VALIDATED"
    duplicate_fact_review = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/agent-facts/"
            f"{fact_payload['fact_id']}/review"
        ),
        json={
            "action": "validate",
            "expected_version": fact_payload["version"],
            "reason": "Deuxième décision volontairement refusée par le verrou de version.",
        },
    )
    assert duplicate_fact_review.status_code == 409, duplicate_fact_review.text
    reviewed_report = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/agent-situation-reports/"
            f"{report.report_revision_id}/review"
        ),
        json={
            "action": "validate",
            "expected_revision": report.revision,
            "expected_state": "DRAFT",
            "reason": "Rapport quotidien contrôlé après les inférences disponibles.",
        },
    )
    assert reviewed_report.status_code == 200, reviewed_report.text
    assert reviewed_report.json()["review_state"] == "VALIDATED"
    duplicate_report_review = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/agent-situation-reports/"
            f"{report.report_revision_id}/review"
        ),
        json={
            "action": "reject",
            "expected_revision": report.revision,
            "expected_state": "DRAFT",
            "reason": "Deuxième décision volontairement refusée après la validation humaine.",
        },
    )
    assert duplicate_report_review.status_code == 409, duplicate_report_review.text

    # Human review is complete, but publication is a distinct gate.  The public
    # contract must not expose the agent report, fact, point, zone, or capture
    # while the campaign day still waits in REVIEW.
    private_until_publication = client.get("/api/v1/incident/FR-26-00001/public-view")
    assert private_until_publication.status_code == 200, private_until_publication.text
    private_payload = private_until_publication.json()
    assert private_payload["daily_intelligence"] == []
    assert private_payload["active_fire_zone"] is None
    assert private_payload["map_gallery"] == []

    reviewed_point = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/spatial-markers/"
            "proposal:spatial-fire-0001/review"
        ),
        json={
            "action": "validate",
            "expected_version": 1,
            "reason": "Point actif contrôlé dans la source avant publication.",
        },
    )
    assert reviewed_point.status_code == 200, reviewed_point.text
    reviewed_zone = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/active-zone-revisions/"
            f"{zone.zone_revision_id}/review"
        ),
        json={
            "action": "approve",
            "expected_state": "DRAFT",
            "reason": "Périmètre quotidien contrôlé avant publication publique.",
        },
    )
    assert reviewed_zone.status_code == 200, reviewed_zone.text
    assert reviewed_zone.json()["review_state"] == "READY_FOR_PUBLICATION"

    session.add(
        IncidentMapCapture(
            capture_id="capture-daily-intelligence-public-0001",
            incident_id=zone.incident_id,
            episode_id=zone.episode_id,
            active_zone_revision_id=zone.id,
            local_date=window.local_date,
            object_uri="private://test/daily-intelligence-public-0001.png",
            sha256="c" * 64,
            size_bytes=4_096,
            media_type="image/png",
            width_px=960,
            height_px=540,
            captured_at=datetime.now(UTC),
            created_by="test-suite",
        )
    )
    session.flush()

    from fire_viewer.services.agent_validation_campaigns import (
        refresh_campaign_day_publication_state,
        refresh_campaign_day_review_state,
    )

    assert refresh_campaign_day_review_state(session, analysis_window_id=window.id) is False
    assert refresh_campaign_day_publication_state(session, analysis_window_id=window.id) is True
    session.commit()
    assert session.scalar(select(func.count()).select_from(AgentSituationReportRevision)) == 1

    published = client.get("/api/v1/incident/FR-26-00001/public-view")
    assert published.status_code == 200, published.text
    published_payload = published.json()
    assert published_payload["active_fire_zone"]["zone_revision_id"] == zone.zone_revision_id
    assert len(published_payload["map_gallery"]) == 1
    public_days = published_payload["daily_intelligence"]
    assert len(public_days) == 1
    public_day = public_days[0]
    assert public_day["analysis_id"] == window.analysis_id
    assert public_day["report"]["report_revision_id"] == report.report_revision_id
    assert public_day["facts"] == [
        {
            "fact_id": "fact-resources-0001",
            "category": "resources",
            "fact_key": "teams_engaged",
            "as_of": public_day["facts"][0]["as_of"],
            "certainty": "explicitly_written",
            "summary": "La source indique 120 personnes engagées.",
            "value_number": 120.0,
            "value_text": None,
            "value_boolean": None,
            "unit": "people",
            "evidence": {
                "evidence_kind": "article_text",
                "evidence_id": "media-die-0001",
                "source_annotation_id": None,
                "source_reference_url": "https://example.test/die/source",
                "license_identifier": "PRESS-TEST-AUTHORIZED",
            },
        }
    ]
    assert [result["kind"] for result in public_day["spatial_results"]] == [
        "active_fire_point"
    ]
    serialized_public_payload = json.dumps(published_payload)
    assert "working_file_url" not in serialized_public_payload
    assert "https://localhost/private" not in serialized_public_payload


def test_native_projected_point_reuses_existing_admin_spatial_review(
    client, session, app, settings, seed_incident
) -> None:
    _create_and_enqueue_v2(client, session, seed_incident, batch_type="user_media")
    output = _v2_output()
    item = output["items"][0]
    proposal = item["spatial_proposals"][0]
    proposal.update(
        {
            "status": "projected_geometry",
            "proposal_kind": "active_fire_point",
            "geometry_geojson": {
                "type": "Point",
                "coordinates": [5.369, 44.751],
            },
        }
    )

    _run_to_completion(app, session, settings, FakeRunPodV2(output))

    workspace = client.get("/api/v1/admin/incidents/FR-26-00001/spatial-review")
    assert workspace.status_code == 200, workspace.text
    marker = next(
        marker
        for marker in workspace.json()["markers"]
        if marker["marker_id"] == "proposal:spatial-fire-0001"
    )
    assert marker["marker_type"] == "active_fire_point"
    reviewed = client.post(
        "/api/v1/admin/incidents/FR-26-00001/spatial-markers/proposal:spatial-fire-0001/review",
        json={
            "action": "validate",
            "expected_version": 1,
            "reason": "Point V2 vérifié dans la revue spatiale existante.",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_state"] == "VALIDATED"


def test_campaign_reviews_failed_operation_without_content_threshold(
    client, session, app, settings, seed_incident, tmp_path
) -> None:
    dispatch = _create_and_enqueue_v2(client, session, seed_incident, batch_type="user_media")
    window = dispatch.batch.analysis_window
    media = session.scalar(select(AgentMediaItem))
    assert window is not None and media is not None and media.media_sha256 is not None
    cutoff_at = window.window_end_at
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=UTC)
    day: dict[str, object] = {
        "ordinal": 1,
        "fire_id": "FR-26-00001",
        "local_date": window.local_date.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "allowed_media_sha256": [media.media_sha256],
        "required_operations": ["user_media"],
        "declared_absences": ["source_research", "satellite_media"],
    }
    day["manifest_sha256"] = _canonical_sha256(day, "manifest_sha256")
    campaign: dict[str, object] = {
        "schema_version": "2.0",
        "campaign_id": "failed-operation-review-test",
        "days": [day],
    }
    campaign["manifest_sha256"] = _canonical_sha256(campaign, "manifest_sha256")
    manifest_path = tmp_path / "failed-operation-campaign.json"
    manifest_path.write_text(json.dumps(campaign), encoding="utf-8")
    create_campaign_from_manifest(
        session,
        manifest_path=manifest_path,
        created_by="test-suite",
    )
    campaign_day = session.scalar(select(AgentValidationCampaignDay))
    assert campaign_day is not None
    campaign_day.state = AgentValidationCampaignDayState.RUNNING
    session.commit()

    _run_to_completion(
        app,
        session,
        settings,
        FakeRunPodV2(_v2_output(status="failed")),
    )

    session.expire_all()
    completed = session.scalar(select(AgentDispatch))
    campaign_day = session.scalar(select(AgentValidationCampaignDay))
    report = session.scalar(select(AgentSituationReportRevision))
    assert completed is not None and completed.state == AgentDispatchState.DEAD_LETTER
    assert campaign_day is not None
    assert campaign_day.state == AgentValidationCampaignDayState.REVIEW
    assert report is not None
    assert report.sections_payload[0]["operation_outcomes"]["user_media"] == {
        "outcome": "failed",
        "terminal": True,
        "states": ["DEAD_LETTER"],
    }
    assert report.sections_payload[0]["fact_ids"] == []
    assert report.sections_payload[0]["spatial_proposal_ids"] == []
    assert session.scalar(select(func.count()).select_from(ActiveFireZoneRevision)) == 0
    workspace = client.get("/api/v1/admin/incidents/FR-26-00001/spatial-review")
    assert workspace.status_code == 200, workspace.text
    daily = workspace.json()["daily_intelligence"]
    assert daily[0]["facts"] == []
    assert daily[0]["spatial_counts"] == {}
    reviewed_report = client.post(
        (
            "/api/v1/admin/incidents/FR-26-00001/agent-situation-reports/"
            f"{report.report_revision_id}/review"
        ),
        json={
            "action": "validate",
            "expected_revision": report.revision,
            "expected_state": "DRAFT",
            "reason": "Abstention et échec explicitement contrôlés sans inventer de contenu.",
        },
    )
    assert reviewed_report.status_code == 200, reviewed_report.text
    assert reviewed_report.json()["review_state"] == "VALIDATED"
    session.refresh(campaign_day)
    assert campaign_day.state == AgentValidationCampaignDayState.REVIEW
