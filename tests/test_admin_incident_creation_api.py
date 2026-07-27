from __future__ import annotations

from sqlalchemy import select

from fire_viewer.db.models import AuditEvent, Episode, IncidentSeries, OutboxEvent
from fire_viewer.domain.enums import IncidentStatus, PublicVisibility, VerificationState
from fire_viewer.services.admin_incident_creation import ADMIN_PLACEMENT_ACCURACY_M


def test_admin_creates_private_monitoring_incident_visible_on_map(client, session) -> None:
    payload = {
        "territory_code": "26",
        "latitude": 44.7532,
        "longitude": 5.3701,
        "canonical_name": "Massif de Justin",
    }
    headers = {"Idempotency-Key": "admin-create-die-0001"}

    response = client.post("/api/v2/admin/incidents", json=payload, headers=headers)

    assert response.status_code == 201
    assert response.headers["Idempotent-Replay"] == "false"
    body = response.json()
    assert body["fire_id"] == "FR-26-00001"
    assert body["episode_id"] == "E01"
    assert body["status"] == IncidentStatus.MONITORING
    assert body["verification_state"] == VerificationState.UNVERIFIED
    assert body["visibility"] == PublicVisibility.LIMITED

    session.expire_all()
    incident = session.execute(select(IncidentSeries)).scalar_one()
    episode = session.execute(select(Episode)).scalar_one()
    assert incident.reference_lon == payload["longitude"]
    assert incident.reference_lat == payload["latitude"]
    assert incident.horizontal_uncertainty_m == ADMIN_PLACEMENT_ACCURACY_M
    assert episode.status == IncidentStatus.MONITORING
    assert episode.review_required is True
    assert session.execute(
        select(AuditEvent).where(AuditEvent.action == "incident.admin_created")
    ).scalar_one()
    assert session.execute(
        select(OutboxEvent).where(OutboxEvent.topic == "incident.created")
    ).scalar_one()

    operational_map = client.get("/api/v2/admin/operational-map")
    assert operational_map.status_code == 200
    mapped = next(
        item for item in operational_map.json()["incidents"] if item["fire_id"] == body["fire_id"]
    )
    assert mapped["longitude"] == payload["longitude"]
    assert mapped["latitude"] == payload["latitude"]
    assert mapped["status"] == IncidentStatus.MONITORING

    replay = client.post("/api/v2/admin/incidents", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == body
    assert len(session.execute(select(IncidentSeries)).scalars().all()) == 1


def test_admin_incident_creation_rejects_reused_intent_and_invalid_location(client) -> None:
    headers = {"Idempotency-Key": "admin-create-conflict-0001"}
    payload = {
        "territory_code": "2A",
        "latitude": 42.2,
        "longitude": 9.1,
        "canonical_name": "Feu à surveiller",
    }
    assert client.post("/api/v2/admin/incidents", json=payload, headers=headers).status_code == 201

    changed = {**payload, "latitude": 42.3}
    conflict = client.post("/api/v2/admin/incidents", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith("idempotency_key_reused")

    invalid = client.post(
        "/api/v2/admin/incidents",
        json={**payload, "territory_code": "France", "latitude": 120},
        headers={"Idempotency-Key": "admin-create-invalid-0001"},
    )
    assert invalid.status_code == 422
