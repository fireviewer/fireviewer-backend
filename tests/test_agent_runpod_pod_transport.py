from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from fire_viewer.services.agent_dispatcher import RunPodPodClient, build_runpod_client


def test_persistent_pod_transport_uses_authenticated_direct_job_routes(settings) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/jobs":
            body = json.loads(request.content)
            assert body["input"] == {"schema_version": "2.0", "batch_id": "batch-1"}
            assert body["policy"]["executionTimeout"] > 0
            return httpx.Response(202, json={"id": "pod-job-1", "status": "IN_QUEUE"})
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "pod-job-1", "status": "CANCELLED"})
        return httpx.Response(200, json={"id": "pod-job-1", "status": "IN_PROGRESS"})

    pod_settings = settings.model_copy(
        update={
            "agent_runpod_transport": "pod",
            "agent_runpod_pod_base_url": "https://pod-test.example",
            "agent_runpod_pod_auth_token": SecretStr("x" * 40),
        }
    )
    client = build_runpod_client(pod_settings)
    assert isinstance(client, RunPodPodClient)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handle), headers=client._headers)
    try:
        assert client.submit({"schema_version": "2.0", "batch_id": "batch-1"})["id"] == "pod-job-1"
        assert client.status("pod-job-1")["status"] == "IN_PROGRESS"
        assert client.cancel("pod-job-1")["status"] == "CANCELLED"
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/v1/jobs",
        "/v1/jobs/pod-job-1",
        "/v1/jobs/pod-job-1/cancel",
    ]
    assert all(request.headers["authorization"] == f"Bearer {'x' * 40}" for request in requests)
