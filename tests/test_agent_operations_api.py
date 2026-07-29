from __future__ import annotations

import hashlib
import json
from datetime import UTC

from sqlalchemy import select

from fire_viewer.db.models import AgentAnalysisWindow, AgentMediaItem
from fire_viewer.domain.enums import IncidentStatus
from fire_viewer.services.agent_validation_campaigns import create_campaign_from_manifest
from test_agent_intelligence_v2 import _v2_payload


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


def test_admin_runs_each_available_analysis_type_without_technical_input(
    client, settings, seed_incident, session, tmp_path
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
    window = session.scalar(select(AgentAnalysisWindow))
    media = session.scalar(select(AgentMediaItem))
    assert window is not None and media is not None and media.media_sha256 is not None
    cutoff_at = window.window_end_at
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=UTC)
    day: dict[str, object] = {
        "ordinal": 1,
        "fire_id": "FR-26-00001",
        "local_date": local_date,
        "cutoff_at": cutoff_at.isoformat(),
        "allowed_media_sha256": [media.media_sha256],
        "required_operations": ["user_media"],
        "declared_absences": ["satellite_media"],
    }
    day["manifest_sha256"] = _canonical_sha256(day, "manifest_sha256")
    campaign: dict[str, object] = {
        "schema_version": "2.0",
        "campaign_id": "test-operations-campaign",
        "days": [day],
    }
    campaign["manifest_sha256"] = _canonical_sha256(campaign, "manifest_sha256")
    manifest_path = tmp_path / "campaign.json"
    manifest_path.write_text(json.dumps(campaign), encoding="utf-8")
    create_campaign_from_manifest(
        session,
        manifest_path=manifest_path,
        created_by="test-suite",
    )

    disabled = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
    )
    assert disabled.status_code == 200, disabled.text
    disabled_user = next(
        action for action in disabled.json()["actions"] if action["operation_type"] == "user_media"
    )
    assert disabled_user == {
        "operation_type": "user_media",
        "schedule_state": "required",
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
    )
    ready_user = next(
        action for action in ready.json()["actions"] if action["operation_type"] == "user_media"
    )
    assert ready_user["can_run"] is True

    launched = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations/user_media/run",
        json={"expected_analysis_window_id": window.analysis_id},
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["operation_ids"] == ["agent-v2-batch-0001"]
    assert launched.json()["queued_files"] == 1

    updated = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
    )
    updated_user = next(
        action for action in updated.json()["actions"] if action["operation_type"] == "user_media"
    )
    assert updated_user["pending_files"] == 0
    assert updated_user["pending_analyses"] == 0
    assert updated_user["running_analyses"] == 1
    assert updated_user["last_run_at"] is not None
    assert updated_user["blocked_reason"] == "already_running"

    overview = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
    )
    assert overview.status_code == 200, overview.text
    source_research = next(
        action
        for action in overview.json()["actions"]
        if action["operation_type"] == "source_research"
    )
    satellite = next(
        action
        for action in overview.json()["actions"]
        if action["operation_type"] == "satellite_media"
    )
    assert source_research["schedule_state"] == "not_scheduled"
    assert source_research["blocked_reason"] == "operation_not_scheduled"
    assert satellite["schedule_state"] == "declared_absent"
    assert satellite["blocked_reason"] == "operation_declared_absent"

    unscheduled = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations/source_research/run",
        json={"expected_analysis_window_id": window.analysis_id},
    )
    assert unscheduled.status_code == 409, unscheduled.text
    assert unscheduled.json()["type"].endswith("agent_operation_not_scheduled")

    declared_absent = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations/satellite_media/run",
        json={"expected_analysis_window_id": window.analysis_id},
    )
    assert declared_absent.status_code == 409, declared_absent.text
    assert declared_absent.json()["type"].endswith("agent_operation_declared_absent")
