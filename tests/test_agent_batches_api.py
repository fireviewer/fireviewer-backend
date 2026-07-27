from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from fire_viewer.db.models import (
    AgentDispatch,
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    Job,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentConsentState,
    AgentDispatchState,
)


def _batch_payload(*, batch_id: str = "agent-batch-0001") -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "schema_version": "1.0",
        "batch_id": batch_id,
        "batch_type": "user_media",
        "priority": "scheduled",
        "purge_after": (now + timedelta(days=2)).isoformat(),
        "items": [
            {
                "input_id": "image-0001",
                "media_type": "image",
                "working_file_url": "https://localhost/private/image-0001.jpg?signature=test",
                "media_sha256": "a" * 64,
                "size_bytes": 4096,
                "metadata": {
                    "captured_at": now.isoformat(),
                    "latitude": 43.2897,
                    "longitude": 6.0214,
                    "gps_accuracy_m": 25,
                    "location_origin": "METADATA",
                },
                "consent": {
                    "basis": "explicit_upload",
                    "scopes": [
                        "temporary_storage",
                        "agent_analysis",
                        "human_review",
                    ],
                    "terms_version": "firewarning-media-v1",
                    "evidence_sha256": "b" * 64,
                    "subject_reference_hash": "c" * 64,
                    "granted_at": now.isoformat(),
                },
            }
        ],
    }


def _create(client, payload: dict[str, object], key: str = "agent-api-key-0001"):
    return client.post(
        "/api/v2/admin/agent-batches",
        headers={"Idempotency-Key": key},
        json=payload,
    )


def test_agent_batch_is_persisted_idempotently_without_using_job(client, session) -> None:
    payload = _batch_payload()

    created = _create(client, payload)
    replayed = _create(client, payload)

    assert created.status_code == 201, created.text
    assert created.headers["Cache-Control"] == "no-store"
    assert created.json()["state"] == AgentBatchState.DRAFT
    assert replayed.status_code == 200, replayed.text
    assert replayed.headers["Idempotent-Replay"] == "true"
    assert session.scalar(select(func.count()).select_from(AgentMediaBatch)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMediaItem)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMediaConsent)) == 1
    assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_agent_batch_idempotency_conflict_and_private_host_guard(client) -> None:
    payload = _batch_payload()
    assert _create(client, payload).status_code == 201
    changed = _batch_payload()
    changed["priority"] = "scheduled_combined"
    conflict = _create(client, changed)

    forbidden = _batch_payload(batch_id="agent-batch-forbidden")
    forbidden["items"][0]["working_file_url"] = "https://public.example/image.jpg"
    rejected = _create(client, forbidden, key="agent-api-key-forbidden")

    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith("agent_batch_idempotency_conflict")
    assert rejected.status_code == 400
    assert rejected.json()["type"].endswith("agent_media_url_forbidden")


def test_enqueue_persists_dedicated_dispatch_and_consent_withdrawal_cancels(
    client, session
) -> None:
    assert _create(client, _batch_payload()).status_code == 201

    enqueued = client.post("/api/v2/admin/agent-batches/agent-batch-0001/enqueue")
    replayed = client.post("/api/v2/admin/agent-batches/agent-batch-0001/enqueue")
    withdrawn = client.post(
        "/api/v2/admin/agent-batches/agent-batch-0001/items/image-0001/consent/withdraw",
        json={"reason": "Retrait explicite demandé par la personne concernée."},
    )

    assert enqueued.status_code == 200, enqueued.text
    assert enqueued.json()["state"] == AgentBatchState.QUEUED
    assert enqueued.json()["dispatch"]["state"] == AgentDispatchState.QUEUED
    assert replayed.headers["Idempotent-Replay"] == "true"
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["consent_state"] == AgentConsentState.WITHDRAWN
    assert withdrawn.json()["batch_state"] == AgentBatchState.CANCEL_REQUESTED
    assert withdrawn.json()["dispatch_state"] == AgentDispatchState.CANCEL_REQUESTED
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    assert dispatch.payload["batch_id"] == "agent-batch-0001"
    assert "consent" not in dispatch.payload["items"][0]
    assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_source_license_requires_https_reference_and_identifier(client) -> None:
    payload = _batch_payload(batch_id="agent-batch-license")
    consent = payload["items"][0]["consent"]
    consent["basis"] = "source_license"
    missing = _create(client, payload, key="agent-api-key-license-missing")

    consent["source_reference_url"] = "http://example.test/source"
    consent["license_identifier"] = "CC-BY-4.0"
    insecure = _create(client, payload, key="agent-api-key-license-http")

    consent["source_reference_url"] = "https://example.test/source"
    accepted = _create(client, payload, key="agent-api-key-license-ok")

    assert missing.status_code == 422
    assert insecure.status_code == 400
    assert insecure.json()["type"].endswith("agent_consent_reference_forbidden")
    assert accepted.status_code == 201, accepted.text
