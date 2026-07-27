from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from fire_viewer.db.models import AuditEvent, Episode, IncidentSeries, Observation, OutboxEvent
from fire_viewer.domain.enums import IncidentStatus
from fire_viewer.domain.hashing import sha256_hex


def _create_pending_review_observation(client, payload_factory, seed_incident) -> str:
    seed_incident(
        fire_id="FR-83-00910",
        sequence=910,
        lon=6.0200,
        lat=43.2897,
        uncertainty_m=500,
    )
    seed_incident(
        fire_id="FR-83-00911",
        sequence=911,
        lon=6.0220,
        lat=43.2897,
        uncertainty_m=500,
    )
    detection = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": "review-pending-detect-0001"},
        json=payload_factory(
            source_id="review-pending-source",
            lon=6.0210,
            lat=43.2897,
            uncertainty_m=450,
            content_char="a",
        ),
    )
    assert detection.status_code == 200
    assert detection.json()["decision"] == "review"
    return str(detection.json()["observation_id"])


def test_review_resolution_create_is_idempotent(
    client, session, payload_factory, seed_incident
) -> None:
    seed_incident(fire_id="FR-83-00030", sequence=30, lon=6.0200, lat=43.2897, uncertainty_m=500)
    seed_incident(fire_id="FR-83-00031", sequence=31, lon=6.0220, lat=43.2897, uncertainty_m=500)
    detection = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": "review-detect-0001"},
        json=payload_factory(
            source_id="review-resolution-source",
            lon=6.0210,
            lat=43.2897,
            uncertainty_m=450,
            content_char="4",
        ),
    )
    assert detection.status_code == 200
    assert detection.json()["decision"] == "review"
    observation_id = detection.json()["observation_id"]
    resolution_payload = {
        "action": "create",
        "expected_version": 1,
        "reason": "Validator determined this is a distinct incident series.",
    }
    first = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-resolve-0001"},
        json=resolution_payload,
    )
    assert first.status_code == 200
    audits_after_first = session.scalar(select(func.count()).select_from(AuditEvent))
    outbox_after_first = session.scalar(select(func.count()).select_from(OutboxEvent))
    second = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-resolve-0001"},
        json=resolution_payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replay"] == "true"
    assert session.scalar(select(func.count()).select_from(IncidentSeries)) == 3
    observation = session.execute(
        select(Observation).where(Observation.observation_id == observation_id)
    ).scalar_one()
    assert observation.attached_incident_id is not None
    assert observation.verification_state.value == "VERIFIED"
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == audits_after_first
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_after_first
    audit_actions = {
        event.action
        for event in session.execute(
            select(AuditEvent).where(AuditEvent.trace_id == first.json()["trace_id"])
        ).scalars()
    }
    assert {"incident.created", "episode.created", "observation.review.resolved"}.issubset(
        audit_actions
    )


def test_review_attach_updates_only_the_episode_and_observation_audit(
    client, session, payload_factory, seed_incident
) -> None:
    now = datetime.now(UTC)
    _target, episode = seed_incident(
        fire_id="FR-83-00910",
        sequence=910,
        lon=6.0200,
        lat=43.2897,
        uncertainty_m=500,
        observed_at=now - timedelta(hours=1),
    )
    seed_incident(
        fire_id="FR-83-00911",
        sequence=911,
        lon=6.0220,
        lat=43.2897,
        uncertainty_m=500,
        observed_at=now - timedelta(hours=1),
    )
    detection = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": "review-attach-detect-0001"},
        json=payload_factory(
            source_id="review-attach-source",
            lon=6.0210,
            lat=43.2897,
            uncertainty_m=450,
            content_char="b",
            observed_at=now,
        ),
    )
    assert detection.status_code == 200
    assert detection.json()["decision"] == "review"
    observation_id = detection.json()["observation_id"]
    request = {
        "action": "attach",
        "target_fire_id": "FR-83-00910",
        "expected_version": 1,
        "reason": "Validator attached the observation to the first matching incident.",
    }
    first = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-attach-resolve-0001"},
        json=request,
    )
    assert first.status_code == 200
    assert first.json()["fire_id"] == "FR-83-00910"
    assert first.json()["episode_id"] == "E01"
    audits_after_first = session.scalar(select(func.count()).select_from(AuditEvent))
    outbox_after_first = session.scalar(select(func.count()).select_from(OutboxEvent))
    audit_events = (
        session.execute(select(AuditEvent).where(AuditEvent.trace_id == first.json()["trace_id"]))
        .scalars()
        .all()
    )
    actions = {event.action for event in audit_events}
    assert actions == {"episode.timeline.advanced", "observation.review.resolved"}
    episode_audit = next(
        event for event in audit_events if event.action == "episode.timeline.advanced"
    )
    assert episode_audit.before_snapshot is not None
    assert episode_audit.after_snapshot is not None
    assert episode_audit.before_hash == sha256_hex(episode_audit.before_snapshot)
    assert episode_audit.after_hash == sha256_hex(episode_audit.after_snapshot)
    assert (
        episode_audit.before_snapshot["last_observed_at"]
        < episode_audit.after_snapshot["last_observed_at"]
    )
    session.expire_all()
    assert session.get(Episode, episode.id).version == 3

    replay = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-attach-resolve-0001"},
        json=request,
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == audits_after_first
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_after_first

    conflicting = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-attach-resolve-0001"},
        json={**request, "reason": "A different request body must not reuse this key."},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["type"].endswith("idempotency_key_reused")
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == audits_after_first
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_after_first


def test_review_reject_mutates_and_audits_only_the_observation(
    client, session, payload_factory, seed_incident
) -> None:
    observation_id = _create_pending_review_observation(client, payload_factory, seed_incident)
    response = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-reject-resolution-0001"},
        json={
            "action": "reject",
            "expected_version": 1,
            "reason": "Validator rejected this ambiguous synthetic observation.",
        },
    )

    assert response.status_code == 200
    assert response.json()["verification_state"] == "REJECTED"
    audit_events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.trace_id == response.json()["trace_id"])
        )
        .scalars()
        .all()
    )
    assert [event.action for event in audit_events] == ["observation.review.resolved"]
    observation = session.execute(
        select(Observation).where(Observation.observation_id == observation_id)
    ).scalar_one()
    assert observation.verification_state.value == "REJECTED"


def test_review_attach_to_closed_incident_audits_reactivation_aggregates(
    client, session, payload_factory, seed_incident
) -> None:
    now = datetime.now(UTC)
    target, previous_episode = seed_incident(
        fire_id="FR-83-00920",
        sequence=920,
        lon=6.0200,
        lat=43.2897,
        uncertainty_m=500,
        status=IncidentStatus.CLOSED,
        observed_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=1),
    )
    seed_incident(
        fire_id="FR-83-00921",
        sequence=921,
        lon=6.0220,
        lat=43.2897,
        uncertainty_m=500,
        observed_at=now - timedelta(hours=2),
    )
    detection = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": "review-reactivation-detect-0001"},
        json=payload_factory(
            source_id="review-reactivation-source",
            lon=6.0210,
            lat=43.2897,
            uncertainty_m=450,
            content_char="c",
            observed_at=now,
        ),
    )
    assert detection.status_code == 200
    assert detection.json()["decision"] == "review"
    observation_id = detection.json()["observation_id"]

    response = client.post(
        f"/api/v1/operator/observations/{observation_id}/resolve",
        headers={"Idempotency-Key": "review-reactivation-resolve-0001"},
        json={
            "action": "attach",
            "target_fire_id": target.fire_id,
            "expected_version": 1,
            "reason": "Validator confirmed that the closed incident has reactivated.",
        },
    )

    assert response.status_code == 200
    assert response.json()["episode_id"] == "E02"
    audit_events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.trace_id == response.json()["trace_id"])
        )
        .scalars()
        .all()
    )
    assert {event.action for event in audit_events} == {
        "episode.reactivation.previous_closed",
        "incident.reactivation.updated",
        "episode.reactivation.created",
        "episode.evidence.verified",
        "observation.review.resolved",
    }
    session.expire_all()
    assert session.get(Episode, previous_episode.id).is_current is False
    current_episode = session.execute(
        select(Episode).where(Episode.incident_id == target.id, Episode.is_current.is_(True))
    ).scalar_one()
    assert current_episode.episode_id == "E02"
