from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from fire_viewer.db.models import (
    AgentAnalysisWindow,
    AgentMediaItem,
    AgentValidationCampaignDay,
)
from fire_viewer.domain.enums import AgentValidationCampaignDayState, IncidentStatus
from fire_viewer.services.agent_source_packages import ensure_daily_analysis_window
from fire_viewer.services.agent_validation_campaigns import (
    _advance_campaign_calendar,
    active_campaign,
    create_campaign_from_manifest,
    resolve_active_analysis_window,
)
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


def test_campaign_rejects_a_day_without_any_provenance_source(
    session, seed_incident, tmp_path
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-77-00001",
        sequence=1,
        lon=2.61,
        lat=48.39,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    window = ensure_daily_analysis_window(
        session,
        incident=incident,
        episode=episode,
        local_date=date(2026, 7, 12),
    )
    cutoff_at = window.window_end_at
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=UTC)
    day: dict[str, object] = {
        "ordinal": 1,
        "fire_id": incident.fire_id,
        "local_date": window.local_date.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "allowed_media_sha256": ["a" * 64],
        "required_operations": ["source_research"],
        "declared_absences": ["user_media", "satellite_media"],
    }
    day["manifest_sha256"] = _canonical_sha256(day, "manifest_sha256")
    campaign: dict[str, object] = {
        "schema_version": "2.0",
        "campaign_id": "missing-provenance-source",
        "days": [day],
    }
    campaign["manifest_sha256"] = _canonical_sha256(campaign, "manifest_sha256")
    manifest_path = tmp_path / "campaign-without-source.json"
    manifest_path.write_text(json.dumps(campaign), encoding="utf-8")

    with pytest.raises(ValueError, match="at least one unique HTTPS provenance source"):
        create_campaign_from_manifest(
            session,
            manifest_path=manifest_path,
            created_by="test-suite",
        )


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
        "expected_public_sources": ["https://example.test/source"],
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


def test_campaign_runs_every_incident_in_the_same_calendar_slot_before_next_day(
    seed_incident,
    session,
    tmp_path,
) -> None:
    first_incident, first_episode = seed_incident(
        fire_id="FR-66-00991",
        sequence=991,
        lon=2.72,
        lat=42.71,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    second_incident, second_episode = seed_incident(
        fire_id="FR-77-00992",
        sequence=992,
        lon=2.68,
        lat=48.40,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    first_date = date(2026, 7, 12)
    next_date = date(2026, 7, 13)
    windows = [
        ensure_daily_analysis_window(
            session,
            incident=first_incident,
            episode=first_episode,
            local_date=first_date,
        ),
        ensure_daily_analysis_window(
            session,
            incident=second_incident,
            episode=second_episode,
            local_date=first_date,
        ),
        ensure_daily_analysis_window(
            session,
            incident=first_incident,
            episode=first_episode,
            local_date=next_date,
        ),
    ]

    days: list[dict[str, object]] = []
    for ordinal, (fire_id, window, media_hash) in enumerate(
        (
            (first_incident.fire_id, windows[0], "a" * 64),
            (second_incident.fire_id, windows[1], "b" * 64),
            (first_incident.fire_id, windows[2], "c" * 64),
        ),
        start=1,
    ):
        cutoff_at = window.window_end_at
        if cutoff_at.tzinfo is None:
            cutoff_at = cutoff_at.replace(tzinfo=UTC)
        day: dict[str, object] = {
            "ordinal": ordinal,
            "fire_id": fire_id,
            "local_date": window.local_date.isoformat(),
            "cutoff_at": cutoff_at.isoformat(),
            "allowed_media_sha256": [media_hash],
            "expected_public_sources": [
                f"https://example.test/source/{fire_id}/{window.local_date.isoformat()}"
            ],
            "required_operations": ["source_research"],
            "declared_absences": ["user_media", "satellite_media"],
        }
        day["manifest_sha256"] = _canonical_sha256(day, "manifest_sha256")
        days.append(day)
    payload: dict[str, object] = {
        "schema_version": "2.0",
        "campaign_id": "multi-incident-calendar-test",
        "days": days,
    }
    payload["manifest_sha256"] = _canonical_sha256(payload, "manifest_sha256")
    manifest_path = tmp_path / "multi-incident-campaign.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    create_campaign_from_manifest(
        session,
        manifest_path=manifest_path,
        created_by="test-suite",
    )

    campaign = active_campaign(session)
    assert campaign is not None
    campaign_days = sorted(campaign.days, key=lambda item: item.ordinal)
    assert [item.state for item in campaign_days] == [
        AgentValidationCampaignDayState.READY,
        AgentValidationCampaignDayState.READY,
        AgentValidationCampaignDayState.LOCKED,
    ]
    assert (
        resolve_active_analysis_window(
            session,
            incident=first_incident,
            episode=first_episode,
        ).window.id
        == windows[0].id
    )
    assert (
        resolve_active_analysis_window(
            session,
            incident=second_incident,
            episode=second_episode,
        ).window.id
        == windows[1].id
    )

    campaign_days[0].state = AgentValidationCampaignDayState.PUBLISHED
    assert _advance_campaign_calendar(campaign_days[0], now=datetime.now(UTC)) is False
    assert campaign_days[2].state == AgentValidationCampaignDayState.LOCKED

    campaign_days[1].state = AgentValidationCampaignDayState.PUBLISHED
    assert _advance_campaign_calendar(campaign_days[1], now=datetime.now(UTC)) is True
    assert campaign_days[2].state == AgentValidationCampaignDayState.READY
    assert campaign.is_active is True
    assert (
        session.scalar(
            select(AgentValidationCampaignDay).where(
                AgentValidationCampaignDay.id == campaign_days[2].id
            )
        )
        is campaign_days[2]
    )
