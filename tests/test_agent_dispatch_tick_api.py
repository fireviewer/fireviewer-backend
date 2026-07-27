from __future__ import annotations

from fire_viewer.api import agent_batches as agent_batches_api


def test_dispatcher_tick_rejects_disabled_dispatch(client) -> None:
    response = client.post("/api/v2/admin/agent-batches/dispatcher/tick")

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("agent_dispatch_disabled")


def test_dispatcher_tick_runs_the_shared_persisted_dispatcher(
    client,
    settings,
    monkeypatch,
) -> None:
    settings.agent_dispatch_enabled = True
    observed: dict[str, object] = {}

    def fake_process_one_hosted_dispatch(factory, *, worker_id, settings):
        observed.update(
            factory=factory,
            worker_id=worker_id,
            settings=settings,
        )
        return True

    monkeypatch.setattr(
        agent_batches_api,
        "process_one_hosted_dispatch",
        fake_process_one_hosted_dispatch,
    )

    response = client.post("/api/v2/admin/agent-batches/dispatcher/tick")

    assert response.status_code == 200, response.text
    assert response.json() == {"processed": True}
    assert observed["factory"] is client.app.state.session_factory
    assert observed["settings"] is settings
    assert str(observed["worker_id"]).startswith("admin-dispatcher:tr-")
