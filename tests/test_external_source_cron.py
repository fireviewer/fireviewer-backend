from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr

from fire_viewer.api import agent_dispatch_cron


def test_external_source_cron_is_authorized_and_runs_one_tick(app, monkeypatch) -> None:
    app.state.settings.official_connectors_enabled = True
    app.state.settings.cron_secret = SecretStr("c" * 32)
    observed: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, trust_env: bool) -> None:
            observed["trust_env"] = trust_env

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_build(settings, *, client):
        observed["settings"] = settings
        observed["client"] = client
        return SimpleNamespace(name="registry")

    def fake_run(factory, *, settings, worker_id, connectors):
        observed["factory"] = factory
        observed["run_settings"] = settings
        observed["worker_id"] = worker_id
        observed["connectors"] = connectors
        return True

    monkeypatch.setattr(agent_dispatch_cron.httpx, "Client", FakeClient)
    monkeypatch.setattr(agent_dispatch_cron, "build_official_connector_registry", fake_build)
    monkeypatch.setattr(agent_dispatch_cron, "run_external_source_scheduler_once", fake_run)

    with TestClient(app, raise_server_exceptions=False) as client:
        unauthorized = client.get("/api/v1/internal/external-sources/progress")
        response = client.get(
            "/api/v1/internal/external-sources/progress",
            headers={"Authorization": f"Bearer {'c' * 32}"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200, response.text
    assert response.json() == {"processed": True, "scheduled": 0}
    assert observed["trust_env"] is False
    assert str(observed["worker_id"]).startswith("vercel-official-sources:")
    assert isinstance(observed["connectors"], SimpleNamespace)
    assert observed["connectors"].name == "registry"


def test_external_source_cron_fails_explicitly_when_disabled(app) -> None:
    app.state.settings.cron_secret = SecretStr("c" * 32)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/internal/external-sources/progress",
            headers={"Authorization": f"Bearer {'c' * 32}"},
        )
    assert response.status_code == 503
    assert response.json()["type"] == "urn:fire-viewer:error:official_connectors_disabled"
