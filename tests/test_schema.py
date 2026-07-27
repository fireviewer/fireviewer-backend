import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from fire_viewer.db.models import Observation


def test_minimal_incident_schema_is_migrated(app) -> None:
    inspector = inspect(app.state.engine)
    tables = set(inspector.get_table_names())
    assert {
        "incident_series",
        "episode",
        "observation",
        "model_asset",
        "job",
        "source",
        "audit_event",
        "manifest_revision",
        "spatial_zone",
        "spatial_zone_revision",
        "zone_archive_snapshot",
        "idempotency_record",
        "outbox_event",
    }.issubset(tables)
    audit_columns = {column["name"] for column in inspector.get_columns("audit_event")}
    assert {"before_snapshot", "after_snapshot", "before_hash", "after_hash"}.issubset(
        audit_columns
    )
    observation_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints("observation")
    }
    assert {
        "ck_observation_attached_pair_complete",
        "ck_observation_proposed_pair_complete",
    }.issubset(observation_checks)


def test_detection_attach_persists_a_complete_incident_episode_pair(
    client, session, payload_factory, seed_incident
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00701",
        sequence=701,
        lon=6.0214,
        lat=43.2897,
    )

    response = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": "schema-coherent-attach-0001"},
        json=payload_factory(source_id="schema-coherent-attach", content_char="7"),
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "attach"
    observation = session.execute(
        select(Observation).where(Observation.observation_id == response.json()["observation_id"])
    ).scalar_one()
    assert (observation.attached_incident_id, observation.attached_episode_id) == (
        incident.id,
        episode.id,
    )
    assert (observation.proposed_incident_id, observation.proposed_episode_id) == (None, None)


def test_observation_link_triggers_reject_partial_and_cross_incident_sql(
    client, session, payload_factory, seed_incident
) -> None:
    first_incident, _first_episode = seed_incident(
        fire_id="FR-83-00711",
        sequence=711,
        lon=6.0214,
        lat=43.2897,
    )
    second_incident, second_episode = seed_incident(
        fire_id="FR-83-00712",
        sequence=712,
        lon=6.3014,
        lat=43.5897,
    )
    response = client.post(
        "/api/v1/incident/detect",
        headers={"Idempotency-Key": "schema-observation-links-0001"},
        json=payload_factory(source_id="schema-observation-links", content_char="8"),
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "attach"
    observation = session.execute(
        select(Observation).where(Observation.observation_id == response.json()["observation_id"])
    ).scalar_one()

    # Exercise the INSERT trigger directly, not the ORM validation path.
    with pytest.raises(IntegrityError, match="pairs must be supplied together"):
        session.execute(
            text(
                "INSERT INTO observation ("
                "observation_id, source_id, observed_at, received_at, geometry_type, longitude, "
                "latitude, altitude_m, vertical_datum, horizontal_uncertainty_m, territory_code, "
                "toponyms, canonical_name_hint, evidence_hash, evidence_license, "
                "external_reference, "
                "request_hash, verification_state, attached_incident_id, attached_episode_id, "
                "proposed_incident_id, proposed_episode_id, match_decision, match_score, "
                "margin_to_second_candidate, match_factors, review_reasons, policy_id, trace_id, "
                "version, created_at"
                ") SELECT :observation_id, source_id, observed_at, received_at, geometry_type, "
                "longitude, latitude, altitude_m, vertical_datum, horizontal_uncertainty_m, "
                "territory_code, toponyms, canonical_name_hint, evidence_hash, evidence_license, "
                "external_reference, request_hash, verification_state, attached_incident_id, NULL, "
                "proposed_incident_id, proposed_episode_id, match_decision, match_score, "
                "margin_to_second_candidate, match_factors, review_reasons, policy_id, trace_id, "
                "version, created_at FROM observation WHERE id = :source_id"
            ),
            {"observation_id": "OBS-SCHEMA-PARTIAL-INSERT", "source_id": observation.id},
        )
    session.rollback()

    with pytest.raises(IntegrityError, match="pairs must be supplied together"):
        session.execute(
            text("UPDATE observation SET proposed_incident_id = :incident_id WHERE id = :id"),
            {"incident_id": first_incident.id, "id": observation.id},
        )
    session.rollback()

    with pytest.raises(IntegrityError, match="episode must belong to its incident"):
        session.execute(
            text("UPDATE observation SET attached_incident_id = :incident_id WHERE id = :id"),
            {"incident_id": second_incident.id, "id": observation.id},
        )
    session.rollback()

    with pytest.raises(IntegrityError, match="episode incident_id is immutable"):
        session.execute(
            text("UPDATE episode SET incident_id = :incident_id WHERE id = :episode_id"),
            {"incident_id": first_incident.id, "episode_id": second_episode.id},
        )
        session.commit()
    session.rollback()

    with pytest.raises(IntegrityError, match="episode must belong to its incident"):
        session.execute(
            text(
                "UPDATE observation SET proposed_incident_id = :incident_id, "
                "proposed_episode_id = :episode_id WHERE id = :id"
            ),
            {
                "incident_id": first_incident.id,
                "episode_id": second_episode.id,
                "id": observation.id,
            },
        )
    session.rollback()
