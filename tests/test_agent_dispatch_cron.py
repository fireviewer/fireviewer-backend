from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from fire_viewer.api import agent_dispatch_cron


def test_hosted_dispatcher_fails_closed_without_cron_secret(client: TestClient) -> None:
    response = client.get("/api/v1/internal/agent-orchestrator/progress")

    assert response.status_code == 503
    assert response.json()["type"] == "urn:fire-viewer:error:agent_cron_not_configured"


def test_progress_requires_vercel_bearer_and_processes_one_item(
    app: Any,
    client: TestClient,
    monkeypatch,
) -> None:
    secret = "cron-" + ("x" * 40)
    app.state.settings = app.state.settings.model_copy(
        update={
            "agent_dispatch_enabled": True,
            "cron_secret": SecretStr(secret),
        }
    )
    calls: list[dict[str, object]] = []

    def process_one(factory, **kwargs):
        calls.append({"factory": factory, **kwargs})
        return True

    monkeypatch.setattr(agent_dispatch_cron, "process_one_hosted_dispatch", process_one)

    denied = client.get(
        "/api/v1/internal/agent-orchestrator/progress",
        headers={"Authorization": "Bearer wrong"},
    )
    assert denied.status_code == 401
    assert calls == []

    accepted = client.get(
        "/api/v1/internal/agent-orchestrator/progress",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"processed": True, "scheduled": 0}
    assert len(calls) == 1
    assert calls[0]["factory"] is app.state.session_factory
    assert calls[0]["settings"] is app.state.settings
    assert str(calls[0]["worker_id"]).startswith("vercel-cron:tr-")
