def test_payload_size_limit_returns_problem(client) -> None:
    response = client.post(
        "/api/v1/incident/detect",
        headers={
            "Idempotency-Key": "payload-size-test-0001",
            "Content-Type": "application/json",
        },
        content=b"x" * 1_048_577,
    )
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")


def test_canonical_incident_routes_are_in_openapi_and_plural_alias_is_hidden(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/incident/detect" in paths
    assert "/api/v1/incident/{fire_id}" in paths
    assert "/api/v1/incident/{fire_id}/manifest" in paths
    assert "/api/v1/incidents/detect" not in paths
