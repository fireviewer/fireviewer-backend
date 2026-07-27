from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from fire_viewer.db.models import (
    AgentDeadLetter,
    AgentDispatch,
    AgentMediaItem,
    AgentModelRun,
    AgentReviewTask,
    IncidentSpatialMarker,
    Job,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentDispatchState,
    IncidentMarkerReviewState,
)
from fire_viewer.services.agent_dispatcher import run_dispatcher_once
from test_agent_batches_api import _batch_payload, _create


class FakeRunPod:
    def __init__(self, *, output: dict[str, Any] | None = None) -> None:
        self.output = output
        self.submissions = 0
        self.status_reads = 0
        self.cancellations = 0

    def submit(self, _payload) -> dict[str, Any]:
        self.submissions += 1
        return {"id": "runpod-job-0001", "status": "IN_QUEUE"}

    def status(self, _remote_job_id: str) -> dict[str, Any]:
        self.status_reads += 1
        return {
            "id": "runpod-job-0001",
            "status": "COMPLETED",
            "executionTime": 1234,
            "delayTime": 50,
            "output": self.output,
        }

    def cancel(self, _remote_job_id: str) -> dict[str, Any]:
        self.cancellations += 1
        return {"id": "runpod-job-0001", "status": "CANCELLED"}


class AmbiguousSubmitRunPod(FakeRunPod):
    def submit(self, _payload) -> dict[str, Any]:
        self.submissions += 1
        request = httpx.Request("POST", "https://api.runpod.ai/v2/endpoint/run")
        raise httpx.ReadTimeout("response lost", request=request)


def _worker_output(*, batch_id: str = "agent-batch-0001", revision: str = "rev-ok"):
    now = datetime.now(UTC)
    return {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "status": "succeeded",
        "retryable": False,
        "model_runs": [
            {
                "model_role": "fire_detection",
                "model_id": "PekingU/rtdetr_v2_r18vd",
                "revision": revision,
                "status": "succeeded",
                "started_at": (now - timedelta(seconds=2)).isoformat(),
                "finished_at": now.isoformat(),
                "load_ms": 500,
                "inference_ms": 120,
                "peak_vram_bytes": 2_000_000_000,
            }
        ],
        "items": [
            {
                "input_id": "image-0001",
                "metadata_result": {
                    "capture_location_available": True,
                    "capture_location_origin": "METADATA",
                },
                "transcript": {"language": None, "segments": []},
                "pixel_regions": [
                    {
                        "region_id": "fire-region-1",
                        "evidence_id": "image-0001",
                        "label": "flame_visible",
                        "bbox_normalized": [0.1, 0.2, 0.5, 0.7],
                        "task": "fire_detection",
                        "model_score": 0.91,
                    }
                ],
                "visual_evidence_selection": [
                    {
                        "evidence_id": "image-0001",
                        "selected_for_grounding": True,
                        "selection_reason": "single_image",
                        "max_detection_score": 0.91,
                    }
                ],
                "factual_observations": [
                    {
                        "type": "visible_flame",
                        "evidence_kind": "image",
                        "evidence_id": "image-0001",
                        "region_id": "fire-region-1",
                        "description": "Flamme directement visible dans la région détectée.",
                        "certainty": "directly_visible",
                    }
                ],
                "explicit_places": [],
                "explicit_times": [],
                "location_status": "CAPTURE_LOCATION_ONLY",
                "geographic_marker_candidate": {
                    "type": "media_capture",
                    "geometry_origin": "METADATA",
                },
                "observed_phenomenon_marker": None,
                "requires_human_review": True,
            }
        ],
        "validation_errors": [],
        "boot_ms": 900,
    }


def _enqueue(client) -> None:
    created = _create(client, _batch_payload())
    assert created.status_code == 201, created.text
    enqueued = client.post("/api/v2/admin/agent-batches/agent-batch-0001/enqueue")
    assert enqueued.status_code == 200, enqueued.text


def test_linked_batch_creates_private_pending_capture_marker(
    client, session, app, settings, seed_incident
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00503", sequence=503, lon=6.0214, lat=43.2897)
    payload = _batch_payload(batch_id="agent-batch-linked")
    payload["fire_id"] = incident.fire_id
    payload["episode_id"] = episode.episode_id
    payload["items"][0]["consent"]["scopes"].append("display_spatial_marker")
    assert _create(client, payload, key="agent-linked-key").status_code == 201
    enqueued = client.post("/api/v2/admin/agent-batches/agent-batch-linked/enqueue")
    assert enqueued.status_code == 200, enqueued.text
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    dispatch.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.expected_models = {"fire_detection": "rev-ok"}
    session.commit()
    runpod = FakeRunPod(output=_worker_output(batch_id="agent-batch-linked"))

    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-linked-test",
        settings=settings,
        client=runpod,
    )
    dispatch.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-linked-test",
        settings=settings,
        client=runpod,
    )

    session.expire_all()
    marker = session.scalar(select(IncidentSpatialMarker))
    assert marker is not None
    assert marker.incident_id == incident.id
    assert marker.episode_id == episode.id
    assert marker.review_state == IncidentMarkerReviewState.PENDING
    assert marker.spatial_display_allowed is True


def _make_due(session) -> AgentDispatch:
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    dispatch.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.lease_owner = None
    dispatch.lease_until = None
    session.commit()
    return dispatch


def test_dispatcher_submits_once_and_persists_strict_output_for_review(
    client, session, app, settings
) -> None:
    _enqueue(client)
    dispatch = _make_due(session)
    dispatch.expected_models = {"fire_detection": "rev-ok"}
    session.commit()
    runpod = FakeRunPod(output=_worker_output())

    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )
    _make_due(session)
    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )

    session.expire_all()
    completed = session.scalar(select(AgentDispatch))
    assert completed is not None
    assert completed.state == AgentDispatchState.SUCCEEDED
    assert completed.batch.state == AgentBatchState.SUCCEEDED
    assert completed.raw_output["batch_id"] == "agent-batch-0001"
    assert runpod.submissions == 1
    assert runpod.status_reads == 1
    assert session.scalar(select(func.count()).select_from(AgentModelRun)) == 1
    assert session.scalar(select(func.count()).select_from(AgentReviewTask)) == 1
    assert session.scalar(select(func.count()).select_from(AgentDeadLetter)) == 0
    assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_ambiguous_submission_is_dead_lettered_without_resubmission(
    client, session, app, settings
) -> None:
    _enqueue(client)
    _make_due(session)
    runpod = AmbiguousSubmitRunPod()

    assert run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )
    assert not run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )

    session.expire_all()
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    assert dispatch.state == AgentDispatchState.DEAD_LETTER
    assert dispatch.last_error_code == "agent_submission_ambiguous"
    assert runpod.submissions == 1
    assert session.scalar(select(func.count()).select_from(AgentDeadLetter)) == 1


def test_invalid_model_revision_is_dead_lettered(client, session, app, settings) -> None:
    _enqueue(client)
    dispatch = _make_due(session)
    dispatch.expected_models = {"fire_detection": "rev-required"}
    session.commit()
    runpod = FakeRunPod(output=_worker_output(revision="rev-wrong"))

    run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )
    _make_due(session)
    run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )

    session.expire_all()
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    assert dispatch.state == AgentDispatchState.DEAD_LETTER
    assert dispatch.last_error_code == "agent_worker_output_invalid"
    assert session.scalar(select(func.count()).select_from(AgentReviewTask)) == 0


def test_consent_withdrawal_cancels_queued_dispatch_locally(client, session, app, settings) -> None:
    _enqueue(client)
    response = client.post(
        "/api/v2/admin/agent-batches/agent-batch-0001/items/image-0001/consent/withdraw",
        json={"reason": "Retrait explicite avant soumission du traitement agentique."},
    )
    assert response.status_code == 200
    _make_due(session)
    runpod = FakeRunPod()

    run_dispatcher_once(
        app.state.session_factory,
        worker_id="dispatcher-test",
        settings=settings,
        client=runpod,
    )

    session.expire_all()
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    assert dispatch.state == AgentDispatchState.CANCELLED
    assert dispatch.batch.state == AgentBatchState.CANCELLED
    item = session.scalar(select(AgentMediaItem))
    assert item is not None
    assert item.purged_at is not None
    assert item.working_file_url is None
    assert dispatch.payload["redacted"] is True
    assert runpod.submissions == 0
    assert runpod.cancellations == 0
