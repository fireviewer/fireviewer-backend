from __future__ import annotations

from fire_viewer.domain.enums import IncidentStatus
from test_agent_intelligence_v2 import _v2_payload


def test_admin_runs_each_available_analysis_type_without_technical_input(
    client, settings, seed_incident
) -> None:
    _, episode = seed_incident(
        fire_id="FR-26-00001",
        sequence=1,
        lon=5.37,
        lat=44.75,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    payload = _v2_payload(fire_id="FR-26-00001", episode_id=episode.episode_id)
    payload["batch_type"] = "user_media"
    local_date = payload["analysis_window"]["local_date"]
    created = client.post(
        "/api/v2/admin/agent-batches",
        headers={"Idempotency-Key": "operation-batch-create-0001"},
        json=payload,
    )
    assert created.status_code == 201, created.text

    disabled = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
        params={"local_date": local_date},
    )
    assert disabled.status_code == 200, disabled.text
    disabled_user = next(
        action for action in disabled.json()["actions"] if action["operation_type"] == "user_media"
    )
    assert disabled_user == {
        "operation_type": "user_media",
        "pending_files": 1,
        "pending_analyses": 1,
        "running_analyses": 0,
        "last_run_at": None,
        "can_run": False,
        "blocked_reason": "dispatch_disabled",
    }

    settings.agent_dispatch_enabled = True
    ready = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
        params={"local_date": local_date},
    )
    ready_user = next(
        action for action in ready.json()["actions"] if action["operation_type"] == "user_media"
    )
    assert ready_user["can_run"] is True

    launched = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations/user_media/run",
        json={"local_date": local_date},
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["operation_ids"] == ["agent-v2-batch-0001"]
    assert launched.json()["queued_files"] == 1

    updated = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
        params={"local_date": local_date},
    )
    updated_user = next(
        action for action in updated.json()["actions"] if action["operation_type"] == "user_media"
    )
    assert updated_user["pending_files"] == 0
    assert updated_user["pending_analyses"] == 0
    assert updated_user["running_analyses"] == 1
    assert updated_user["last_run_at"] is not None
    assert updated_user["blocked_reason"] == "nothing_to_process"
