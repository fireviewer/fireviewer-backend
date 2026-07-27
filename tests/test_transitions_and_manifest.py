from sqlalchemy import select

from fire_viewer.db.models import AuditEvent, Episode, IncidentSeries


def _create_incident(client, payload_factory, key: str = "transition-create-0001") -> str:
    response = client.post(
        "/api/v1/incidents/detect",
        headers={"Idempotency-Key": key},
        json=payload_factory(content_char="3"),
    )
    assert response.status_code == 201
    return response.json()["fire_id"]


def _current_episode_version(session, fire_id: str) -> int:
    return session.execute(
        select(Episode.version)
        .join(IncidentSeries, IncidentSeries.id == Episode.incident_id)
        .where(IncidentSeries.fire_id == fire_id, Episode.is_current.is_(True))
    ).scalar_one()


def test_candidate_location_is_withheld_until_human_confirmation(client, payload_factory) -> None:
    fire_id = _create_incident(client, payload_factory, "visibility-create-0001")
    incident = client.get(f"/api/v1/incident/{fire_id}")
    assert incident.status_code == 200
    assert incident.json()["visibility"] == "LIMITED"
    assert incident.json()["location"] is None
    manifest = client.get(f"/api/v1/incident/{fire_id}/manifest")
    assert manifest.status_code == 200
    assert manifest.json()["location"] is None
    assert manifest.json()["model_state"] == "withheld"


def test_illegal_transition_returns_409_and_is_audited(client, session, payload_factory) -> None:
    fire_id = _create_incident(client, payload_factory)
    response = client.post(
        f"/api/v1/operator/incidents/{fire_id}/transitions",
        headers={"Idempotency-Key": "test-test-test-test"},
        json={
            "target_status": "EXTINGUISHED",
            "expected_version": _current_episode_version(session, fire_id),
            "reason": "Attempt an intentionally illegal test transition.",
        },
    )
    assert response.status_code == 409
    audit = session.execute(
        select(AuditEvent).where(AuditEvent.action == "incident.transition.rejected")
    ).scalar_one()
    assert audit.payload["cause"] == "illegal_transition"


def test_active_confirmation_requires_validation_basis(client, session, payload_factory) -> None:
    fire_id = _create_incident(client, payload_factory, "transition-create-0002")
    response = client.post(
        f"/api/v1/operator/incidents/{fire_id}/transitions",
        headers={"Idempotency-Key": "confirmation-no-basis-0001"},
        json={
            "target_status": "ACTIVE_CONFIRMED",
            "expected_version": _current_episode_version(session, fire_id),
            "reason": "Confirm without basis for negative test coverage.",
        },
    )
    assert response.status_code == 409
    assert response.json()["type"].endswith("validation_basis_required")


def test_confirm_transition_is_idempotent_and_manifest_supports_etag(
    client, session, payload_factory
) -> None:
    fire_id = _create_incident(client, payload_factory, "transition-create-0003")
    expected_version = _current_episode_version(session, fire_id)
    request = {
        "target_status": "ACTIVE_CONFIRMED",
        "expected_version": expected_version,
        "reason": "Human validator confirmed the incident during a test.",
        "validation_basis": "Two authorized sources and operator review.",
    }
    first = client.post(
        f"/api/v1/operator/incidents/{fire_id}/transitions",
        headers={"Idempotency-Key": "confirmation-transition-0001"},
        json=request,
    )
    second = client.post(
        f"/api/v1/operator/incidents/{fire_id}/transitions",
        headers={"Idempotency-Key": "confirmation-transition-0001"},
        json=request,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replay"] == "true"
    episode = session.execute(select(Episode).where(Episode.is_current.is_(True))).scalar_one()
    assert episode.version == expected_version + 1
    audit = session.execute(
        select(AuditEvent).where(AuditEvent.action == "incident.status.changed")
    ).scalar_one()
    assert audit.before_snapshot["episode"]["status"] == "CANDIDATE"
    assert audit.after_snapshot["episode"]["status"] == "ACTIVE_CONFIRMED"
    assert audit.before_hash and audit.after_hash
    manifest = client.get(f"/api/v1/incident/{fire_id}/manifest")
    assert manifest.status_code == 200
    assert manifest.json()["status"]["code"] == "ACTIVE_CONFIRMED"
    assert manifest.json()["location"] is not None
    etag = manifest.headers["ETag"]
    unchanged = client.get(
        f"/api/v1/incident/{fire_id}/manifest",
        headers={"If-None-Match": etag},
    )
    assert unchanged.status_code == 304


def test_suspension_masks_location_and_asset(client, session, payload_factory) -> None:
    fire_id = _create_incident(client, payload_factory, "transition-create-0004")
    response = client.post(
        f"/api/v1/operator/incidents/{fire_id}/transitions",
        headers={"Idempotency-Key": "suspension-transition-0001"},
        json={
            "target_status": "SUSPENDED",
            "expected_version": _current_episode_version(session, fire_id),
            "reason": "Security review requires immediate public suspension.",
            "public_note": "Data temporarily withheld pending review.",
        },
    )
    assert response.status_code == 200
    incident = client.get(f"/api/v1/incident/{fire_id}").json()
    assert incident["location"] is None
    manifest = client.get(f"/api/v1/incident/{fire_id}/manifest").json()
    assert manifest["location"] is None
    assert manifest["asset"] is None
    assert manifest["model_state"] == "withheld"


def test_invalid_fire_id_returns_400(client) -> None:
    response = client.get("/api/v1/incident/../../etc/passwd")
    assert response.status_code in {400, 404}
    direct = client.get("/api/v1/incident/INVALID")
    assert direct.status_code == 400
