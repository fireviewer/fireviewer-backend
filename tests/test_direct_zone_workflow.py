from __future__ import annotations

from sqlalchemy import select

from fire_viewer.db.models import AuditEvent, SpatialZone


def _zone_payload(zone_id: str) -> dict[str, object]:
    return {
        "zone_id": zone_id,
        "label": "Zone de test",
        "description": "Reference spatiale technique de test.",
        "bounds_l93_m": [100.0, 200.0, 300.0, 400.0],
        "reason": "Creation de la reference spatiale de test.",
    }


def test_admin_zone_create_replays_without_duplicate_zone_or_audit(client, session) -> None:
    payload = _zone_payload("IDEMPOTENT-ZONE-01")
    headers = {"Idempotency-Key": "direct-zone-idempotency-0001"}  # gitleaks:allow

    first = client.post("/api/v1/admin/zones", json=payload, headers=headers)
    replay = client.post("/api/v1/admin/zones", json=payload, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert first.headers["Idempotent-Replay"] == "false"
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()
    assert (
        session.execute(select(SpatialZone).where(SpatialZone.zone_id == "IDEMPOTENT-ZONE-01"))
        .scalars()
        .all()
    )
    audits = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.action == "zone.created",
                AuditEvent.target_id == "IDEMPOTENT-ZONE-01",
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


def test_public_zone_routes_are_retired(client) -> None:
    assert client.get("/api/v1/zones/TEST-ZONE-01").status_code == 404
    assert client.get("/api/v1/zones/TEST-ZONE-01/catalog").status_code == 404
    assert (
        client.post(
            "/api/v1/zones/TEST-ZONE-01/contributions",
            json={"title": "Observation", "text": "Ne doit pas etre exposee.", "category": "other"},
        ).status_code
        == 404
    )


def test_legacy_archive_upload_route_is_retired(client, tmp_path) -> None:
    created = client.post(
        "/api/v1/admin/zones",
        json=_zone_payload("TEST-ZONE-01"),
        headers={"Idempotency-Key": "direct-zone-create-0001"},
    )
    assert created.status_code == 201
    response = client.post(
        "/api/v1/admin/zones/TEST-ZONE-01/uploads",
        content=b"no binary upload is accepted",
        headers={
            "Content-Type": "application/gzip",
            "Idempotency-Key": "direct-zone-hostile-0001",
        },
    )

    assert response.status_code == 404
    assert not (tmp_path / "outside.txt").exists()
    assert str(tmp_path) not in response.text
