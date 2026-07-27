from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fire_viewer.db.models import Observation, Source
from fire_viewer.domain.enums import (
    IncidentStatus,
    MatchDecision,
    PublicVisibility,
    SourceTrust,
    SourceType,
    VerificationState,
)


def _add_verified_toponym(session, incident, episode, *, toponym: str) -> None:
    source = Source(
        source_key=f"public-discovery-{incident.fire_id}",
        source_type=SourceType.INSTITUTIONAL,
        trust=SourceTrust.INSTITUTIONAL,
        display_name="Fixture source",
        enabled=True,
    )
    session.add(source)
    session.flush()
    session.add(
        Observation(
            observation_id=f"OBS-{incident.fire_id}",
            source_id=source.id,
            observed_at=episode.last_observed_at,
            received_at=episode.last_observed_at,
            longitude=incident.reference_lon,
            latitude=incident.reference_lat,
            horizontal_uncertainty_m=incident.horizontal_uncertainty_m,
            territory_code=incident.territory_code,
            toponyms=[toponym],
            evidence_hash="sha256:" + "d" * 64,
            evidence_license="test-fixture",
            request_hash="e" * 64,
            verification_state=VerificationState.VERIFIED,
            attached_incident_id=incident.id,
            attached_episode_id=episode.id,
            match_decision=MatchDecision.ATTACH,
            match_factors={},
            review_reasons=[],
            policy_id="test-policy",
            trace_id="trace-public-discovery",
            version=1,
        )
    )
    session.commit()


def test_recent_discovery_is_text_only_bounded_and_excludes_non_public_states(
    client, seed_incident, session
) -> None:
    recent_at = datetime.now(UTC)
    public, public_episode = seed_incident(
        fire_id="FR-83-00201",
        sequence=201,
        lon=6.02,
        lat=43.29,
        canonical_name="Zone publique fictive",
        observed_at=recent_at,
    )
    hidden, _hidden_episode = seed_incident(
        fire_id="FR-83-00202",
        sequence=202,
        lon=6.04,
        lat=43.29,
        canonical_name="Zone en revue fictive",
        status=IncidentStatus.UNDER_REVIEW,
        observed_at=recent_at + timedelta(minutes=1),
    )
    hidden.public_visibility = PublicVisibility.LIMITED
    inconsistent, _inconsistent_episode = seed_incident(
        fire_id="FR-83-00203",
        sequence=203,
        lon=6.06,
        lat=43.29,
        canonical_name="Zone incohérente fictive",
        observed_at=recent_at + timedelta(minutes=2),
    )
    inconsistent.public_visibility = PublicVisibility.LIMITED
    session.commit()

    response = client.get("/api/v1/incidents/recent?limit=20")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=15, must-revalidate"
    payload = response.json()
    assert payload["schema_version"] == "1.0"
    assert [item["fire_id"] for item in payload["incidents"]] == [public.fire_id]
    assert payload["incidents"][0] == {
        "fire_id": public.fire_id,
        "canonical_name": "Zone publique fictive",
        "status": public_episode.status.value,
        "verification": "verified",
        "last_observed_at": public_episode.last_observed_at.isoformat().replace("+00:00", "Z"),
    }
    returned_ids = {item["fire_id"] for item in payload["incidents"]}
    assert hidden.fire_id not in returned_ids
    assert inconsistent.fire_id not in returned_ids
    assert "longitude" not in str(payload)
    assert "source" not in str(payload)
    assert "asset" not in str(payload)


def test_discovery_search_matches_public_name_verified_toponym_and_approximate_coordinates(
    client, seed_incident, session
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00211",
        sequence=211,
        lon=6.0214,
        lat=43.2897,
        canonical_name="Massif fictif du Vercors",
    )
    _add_verified_toponym(session, incident, episode, toponym="Lieu-dit des Pins fictif")

    name = client.get("/api/v1/incidents/search?q=Vercors")
    toponym = client.get("/api/v1/incidents/search?q=Pins%20fictif")
    nearby = client.get("/api/v1/incidents/search?longitude=6.0214&latitude=43.2897&radius_km=1")

    assert name.status_code == 200
    assert toponym.status_code == 200
    assert nearby.status_code == 200
    assert name.json()["incidents"][0]["fire_id"] == incident.fire_id
    assert toponym.json()["incidents"][0]["fire_id"] == incident.fire_id
    assert nearby.json()["incidents"][0]["fire_id"] == incident.fire_id


def test_discovery_rejects_ambiguous_or_incomplete_search_input(client) -> None:
    assert client.get("/api/v1/incidents/search").status_code == 400
    assert client.get("/api/v1/incidents/search?longitude=6.0").status_code == 400
    assert (
        client.get("/api/v1/incidents/search?q=ab&longitude=6&latitude=43&radius_km=1").status_code
        == 400
    )
    assert client.get("/api/v1/incidents/recent?limit=21").status_code == 422
