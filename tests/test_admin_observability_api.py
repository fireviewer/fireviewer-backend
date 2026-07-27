from __future__ import annotations

from fire_viewer.domain.enums import ActorType
from fire_viewer.services.common import record_audit


def test_global_audit_is_safe_filterable_and_never_returns_snapshots(client, session) -> None:
    event = record_audit(
        session,
        actor_type=ActorType.OPERATOR,
        actor_id="operator-42",
        action="incident.transition",
        target_type="incident",
        target_id="FR-83-00042",
        reason="Transition validée avec une base documentaire.",
        trace_id="trace-observability-001",
        before={"private": "before"},
        after={"private": "after"},
        payload={"internal": "never-public"},
    )
    session.commit()

    response = client.get("/api/v1/admin/audit?action=incident.transition&target_id=FR-83-00042")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "events": [
            {
                "event_id": event.event_id,
                "occurred_at": response.json()["events"][0]["occurred_at"],
                "action": "incident.transition",
                "target_type": "incident",
                "target_id": "FR-83-00042",
                "actor_type": "operator",
                "actor_id": "operator-42",
                "reason": "Transition validée avec une base documentaire.",
                "trace_id": "trace-observability-001",
            }
        ]
    }
    assert "before_snapshot" not in response.text
    assert "after_snapshot" not in response.text
    assert "never-public" not in response.text


def test_admin_governance_reads_expose_only_safe_operational_configuration(
    client, settings
) -> None:
    roles = client.get("/api/v1/admin/roles")
    system = client.get("/api/v1/admin/system")
    configuration = client.get("/api/v1/admin/configuration")

    assert roles.status_code == system.status_code == configuration.status_code == 200
    assert all(
        response.headers["Cache-Control"] == "no-store"
        for response in (roles, system, configuration)
    )
    assert "administrator" in roles.json()["assigned_roles"]
    assert {item["role"] for item in roles.json()["catalog"]} >= {
        "administrator",
        "analyst",
        "validator",
    }

    system_payload = system.json()
    assert system_payload["database"]["reachable"] is True
    assert system_payload["worker_heartbeat"] == "not_persisted"
    assert system_payload["queues"]["reports_pending"] == 0

    configuration_payload = configuration.json()
    assert configuration_payload["environment"] == settings.environment
    assert configuration_payload["identity_management"] == "authentification désactivée"
    assert configuration_payload["matching"]["policy_id"] == settings.matching_policy_id
    assert "zone_upload_storage_dir" not in configuration.text
    assert "public_report_hash_secret" not in configuration.text
    assert "oidc" not in configuration.text.lower() or "identity_management" in configuration.text
