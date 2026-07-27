from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from sqlalchemy import func, select

from fire_viewer.core.ids import new_trace_id
from fire_viewer.db.models import (
    AuditEvent,
    IdempotencyRecord,
    IncidentSeries,
    Observation,
    OutboxEvent,
    Source,
)
from fire_viewer.domain.enums import IncidentStatus
from fire_viewer.domain.geospatial import bbox_for_point
from fire_viewer.domain.schemas import DetectionRequest
from fire_viewer.services.candidates import find_candidates
from fire_viewer.services.detection import DetectionOutcome, process_detection


def test_idempotency_replay_creates_one_observation_and_event(
    client, session, payload_factory
) -> None:
    payload = payload_factory()
    key = "source-test-idempotency-0001"
    responses = [
        client.post(
            "/api/v1/incidents/detect",
            headers={"Idempotency-Key": key},
            json=payload,
        )
        for _ in range(20)
    ]
    assert responses[0].status_code == 201
    assert responses[0].headers["Idempotent-Replay"] == "false"
    assert all(response.json() == responses[0].json() for response in responses)
    assert all(response.headers["Idempotent-Replay"] == "true" for response in responses[1:])
    assert session.scalar(select(func.count()).select_from(Observation)) == 1
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
    # The first request writes source, incident, episode and observation events.
    # Replays must not append any further audit history.
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4


def test_idempotency_key_reuse_with_different_body_is_rejected(client, payload_factory) -> None:
    key = "source-test-idempotency-0002"
    first = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": key},
        json=payload_factory(content_char="b"),
    )
    assert first.status_code == 201
    second_payload = payload_factory(content_char="c", lon=6.1)
    second = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": key},
        json=second_payload,
    )
    assert second.status_code == 409
    assert second.json()["type"].endswith("idempotency_key_reused")


def test_expired_idempotency_key_can_be_reused(client, session, payload_factory) -> None:
    key = "expired-idempotency-key-0001"
    first = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": key},
        json=payload_factory(content_char="5"),
    )
    assert first.status_code == 201
    record = session.execute(
        select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == key)
    ).scalar_one()
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    second = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": key},
        json=payload_factory(
            source_id="expired-key-source-2",
            content_char="6",
            lon=6.0215,
        ),
    )
    assert second.status_code == 200
    assert second.json()["decision"] == "attach"
    assert second.headers["Idempotent-Replay"] == "false"


def test_concurrent_near_identical_detections_create_one_series(
    app, settings, payload_factory
) -> None:
    factory = app.state.session_factory
    payloads = [
        payload_factory(source_id=f"concurrent-source-{index}", content_char=char)
        for index, char in enumerate(("d", "e"), start=1)
    ]

    def execute(index: int):
        with factory() as db_session:
            return process_detection(
                db_session,
                payload=DetectionRequest.model_validate(payloads[index]),
                idempotency_key=f"concurrent-request-000{index}",
                source_token=None,
                trace_id=new_trace_id(),
                settings=settings,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(execute, (0, 1)))

    assert {result.response.decision.value for result in results} == {"create", "attach"}
    with factory() as db_session:
        assert db_session.scalar(select(func.count()).select_from(IncidentSeries)) == 1
        assert db_session.scalar(select(func.count()).select_from(Observation)) == 2


def test_concurrent_same_idempotency_key_replays_after_sqlite_writer_serialization(
    app, settings, payload_factory
) -> None:
    """SQLite's single writer must still produce one committed aggregate on a same-key retry."""

    factory = app.state.session_factory
    payload = payload_factory(source_id="same-key-concurrent-source", content_char="e")
    barrier = Barrier(2)

    def execute() -> DetectionOutcome:
        with factory() as db_session:
            barrier.wait()
            return process_detection(
                db_session,
                payload=DetectionRequest.model_validate(payload),
                idempotency_key="same-key-concurrent-0001",
                source_token=None,
                trace_id=new_trace_id(),
                settings=settings,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: execute(), range(2)))

    assert sum(outcome.replayed for outcome in outcomes) == 1
    assert outcomes[0].response == outcomes[1].response
    with factory() as db_session:
        assert db_session.scalar(select(func.count()).select_from(IncidentSeries)) == 1
        assert db_session.scalar(select(func.count()).select_from(Observation)) == 1
        assert db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert db_session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        assert db_session.scalar(select(func.count()).select_from(AuditEvent)) == 4


def test_failed_detection_rolls_back_discovered_source_before_request_error(
    client, session, payload_factory
) -> None:
    """Dependency teardown explicitly rolls back writes left by a failed request."""

    response = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": "rollback-uncommitted-source-0001"},
        json=payload_factory(
            source_id="rollback-source-001",
            content_char="f",
            observed_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith("future_timestamp")
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_two_close_candidates_with_small_margin_force_review(
    client, session, payload_factory, seed_incident
) -> None:
    seed_incident(fire_id="FR-83-00010", sequence=10, lon=6.0200, lat=43.2897, uncertainty_m=500)
    seed_incident(fire_id="FR-83-00011", sequence=11, lon=6.0220, lat=43.2897, uncertainty_m=500)
    response = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": "review-margin-test-0001"},
        json=payload_factory(
            source_id="review-source-001",
            lon=6.0210,
            lat=43.2897,
            uncertainty_m=450,
            content_char="f",
        ),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "review"
    assert data["fire_id"] is None
    assert data["proposed_fire_id"] in {"FR-83-00010", "FR-83-00011"}
    assert "candidate_margin_too_low" in data["review_reasons"]
    observation = session.execute(select(Observation)).scalar_one()
    assert observation.attached_incident_id is None
    assert observation.proposed_incident_id is not None


def test_sqlite_candidate_query_breaks_equal_updated_at_ties_by_fire_id(
    session, seed_incident
) -> None:
    first, _first_episode = seed_incident(
        fire_id="FR-83-00051",
        sequence=51,
        lon=6.0200,
        lat=43.2897,
        uncertainty_m=500,
    )
    second, _second_episode = seed_incident(
        fire_id="FR-83-00050",
        sequence=50,
        lon=6.0220,
        lat=43.2897,
        uncertainty_m=500,
    )
    same_updated_at = datetime.now(UTC)
    first.updated_at = same_updated_at
    second.updated_at = same_updated_at
    session.commit()

    candidates, overflow = find_candidates(
        session,
        bbox=bbox_for_point(6.0210, 43.2897, 1_000),
        limit=10,
    )

    assert overflow is False
    assert [candidate.fire_id for candidate in candidates] == ["FR-83-00050", "FR-83-00051"]


def test_closed_incident_match_creates_reactivation_episode(
    client, session, payload_factory, seed_incident
) -> None:
    now = datetime.now(UTC)
    seed_incident(
        fire_id="FR-83-00020",
        sequence=20,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.CLOSED,
        observed_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=1),
    )
    response = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": "reactivation-test-0001"},
        json=payload_factory(
            source_id="reactivation-source-001",
            content_char="9",
            observed_at=now - timedelta(minutes=5),
        ),
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "attach"
    assert response.json()["fire_id"] == "FR-83-00020"
    assert response.json()["episode_id"] == "E02"
    audit_events = session.execute(
        select(AuditEvent).where(AuditEvent.trace_id == response.json()["trace_id"])
    ).scalars()
    actions = {event.action for event in audit_events}
    assert {
        "episode.reactivation.previous_closed",
        "incident.reactivation.updated",
        "episode.reactivation.created",
        "observation.processed",
    }.issubset(actions)


def test_pending_auto_attach_does_not_refresh_public_timeline_until_verified(
    client, payload_factory, seed_incident
) -> None:
    now = datetime.now(UTC)
    _incident, seeded_episode = seed_incident(
        fire_id="FR-83-00040",
        sequence=40,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.ACTIVE_CONFIRMED,
        observed_at=now - timedelta(hours=1),
    )
    original_last_observed_at = seeded_episode.last_observed_at

    detection = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": "pending-attach-timeline-0001"},
        json=payload_factory(
            source_id="pending-timeline-source",
            content_char="8",
            observed_at=now - timedelta(minutes=5),
        ),
    )
    assert detection.status_code == 200
    assert detection.json()["decision"] == "attach"
    assert detection.json()["fire_id"] == "FR-83-00040"

    before_review = client.get("/api/v1/incident/FR-83-00040").json()
    assert datetime.fromisoformat(before_review["last_observed_at"]) == original_last_observed_at

    resolution = client.post(
        f"/api/v1/operator/observations/{detection.json()['observation_id']}/resolve",
        headers={"Idempotency-Key": "pending-attach-resolution-0001"},
        json={
            "action": "attach",
            "target_fire_id": "FR-83-00040",
            "expected_version": 1,
            "reason": "Validator confirmed the pending observation for this incident.",
        },
    )
    assert resolution.status_code == 200
    after_review = client.get("/api/v1/incident/FR-83-00040").json()
    assert datetime.fromisoformat(after_review["last_observed_at"]) > original_last_observed_at
