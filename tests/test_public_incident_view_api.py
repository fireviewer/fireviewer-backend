from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from fire_viewer.core.security import Actor
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    IncidentGalleryItem,
    IncidentOperationalInformation,
    Observation,
    Source,
)
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    MatchDecision,
    SourceTrust,
    SourceType,
    VerificationState,
)
from fire_viewer.domain.schemas import (
    AdminIncidentGalleryCreateRequest,
    AdminIncidentGalleryReviewRequest,
    AdminOperationalInformationCreateRequest,
    AdminOperationalInformationReviewRequest,
)
from fire_viewer.services.admin_incidents import (
    create_admin_incident_gallery_item,
    create_admin_incident_operational_information,
    review_admin_incident_gallery_item,
    review_admin_incident_operational_information,
)


def _verified_observation(session, incident, episode, *, state: VerificationState) -> None:
    source = Source(
        source_key=f"public-view-{incident.fire_id}-{state.value}",
        source_type=SourceType.INSTITUTIONAL,
        trust=SourceTrust.INSTITUTIONAL,
        display_name="Private source name",
        public_display_name="Source institutionnelle",
        public_license="ODbL-1.0",
        public_reference_url="https://example.invalid/public-source",
        public_transformations=["normalisation"],
        enabled=True,
    )
    session.add(source)
    session.flush()
    session.add(
        Observation(
            observation_id=f"OBS-{incident.fire_id}-{state.value}",
            source_id=source.id,
            observed_at=episode.last_observed_at,
            received_at=episode.last_observed_at,
            longitude=incident.reference_lon,
            latitude=incident.reference_lat,
            horizontal_uncertainty_m=incident.horizontal_uncertainty_m,
            territory_code=incident.territory_code,
            toponyms=["private precise toponym"],
            evidence_hash="sha256:" + "a" * 64,
            evidence_license="private-license",
            external_reference="https://example.invalid/private-evidence",
            request_hash="b" * 64,
            verification_state=state,
            attached_incident_id=incident.id,
            attached_episode_id=episode.id,
            match_decision=MatchDecision.ATTACH,
            match_factors={},
            review_reasons=[],
            policy_id="test-policy",
            trace_id="trace-public-view",
            version=1,
        )
    )
    session.commit()


def test_public_view_filters_sensitive_observation_fields_and_supports_etag(
    client, seed_incident, session
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00601", sequence=601, lon=6.02, lat=43.29)
    _verified_observation(session, incident, episode, state=VerificationState.VERIFIED)
    _verified_observation(session, incident, episode, state=VerificationState.PENDING_REVIEW)

    response = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=30, must-revalidate"
    body = response.json()
    assert body["schema_version"] == "1.0"
    assert len(body["observations"]) == 1
    assert body["sources"][0]["name"] == "Source institutionnelle"
    rendered = str(body)
    assert "private precise toponym" not in rendered
    assert "private-evidence" not in rendered
    assert "Private source name" not in rendered
    assert (
        client.get(
            f"/api/v1/incident/{incident.fire_id}/public-view",
            headers={"If-None-Match": response.headers["etag"]},
        ).status_code
        == 304
    )


def test_public_view_uses_the_latest_historical_zone_by_effective_date(
    client, seed_incident, session
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00610", sequence=610, lon=6.04, lat=43.31
    )
    geometry = {
        "type": "Polygon",
        "coordinates": [[[6.03, 43.30], [6.05, 43.30], [6.05, 43.32], [6.03, 43.30]]],
    }
    session.add_all(
        [
            ActiveFireZoneRevision(
                zone_revision_id="azr-history-earlier",
                incident_id=incident.id,
                episode_id=episode.id,
                revision=12,
                valid_at=datetime(2026, 7, 12, 23, 59, tzinfo=UTC),
                geometry_geojson=geometry,
                geometry_origin="HUMAN_AUTHORED",
                supporting_marker_ids=[],
                source_revision_ids=[],
                review_state=ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
                created_by="admin-test",
                reviewed_by="admin-test",
                reviewed_at=datetime(2026, 7, 13, tzinfo=UTC),
                review_reason="Calque historique contrôlé avant publication.",
                reason="Contour du 12 juillet conservé pour vérifier le tri temporel.",
            ),
            ActiveFireZoneRevision(
                zone_revision_id="azr-history-later-revision",
                incident_id=incident.id,
                episode_id=episode.id,
                revision=13,
                valid_at=datetime(2026, 7, 11, 23, 59, tzinfo=UTC),
                geometry_geojson=geometry,
                geometry_origin="HUMAN_AUTHORED",
                supporting_marker_ids=[],
                source_revision_ids=[],
                review_state=ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
                created_by="admin-test",
                reviewed_by="admin-test",
                reviewed_at=datetime(2026, 7, 13, tzinfo=UTC),
                review_reason="Calque historique contrôlé avant publication.",
                reason="Contour du 11 juillet saisi après celui du 12 juillet.",
            ),
            ActiveFireZoneRevision(
                zone_revision_id="azr-history-burned-area",
                incident_id=incident.id,
                episode_id=episode.id,
                zone_kind="burned",
                revision=1,
                valid_at=datetime(2026, 7, 12, 23, 59, tzinfo=UTC),
                geometry_geojson=geometry,
                geometry_origin="HUMAN_AUTHORED",
                supporting_marker_ids=[],
                source_revision_ids=[],
                review_state=ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
                created_by="admin-test",
                reviewed_by="admin-test",
                reviewed_at=datetime(2026, 7, 13, tzinfo=UTC),
                review_reason="Zone parcourue contrôlée avant publication.",
                reason="Empreinte cumulée du 12 juillet distincte de la zone active.",
            ),
        ]
    )
    session.commit()

    response = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")

    assert response.status_code == 200
    assert response.json()["active_fire_zone"]["zone_revision_id"] == "azr-history-earlier"
    assert [item["zone_revision_id"] for item in response.json()["active_fire_zones"]] == [
        "azr-history-later-revision",
        "azr-history-earlier",
    ]
    assert response.json()["burned_area_zones"] == [
        {
            "zone_revision_id": "azr-history-burned-area",
            "zone_kind": "burned",
            "revision": 1,
            "valid_at": "2026-07-12T23:59:00Z",
            "analysis_id": None,
            "geometry_geojson": geometry,
        }
    ]
    assert (
        client.get(f"/api/v1/incident/{incident.fire_id}/public-view/export.json").json()["fire_id"]
        == incident.fire_id
    )
    assert (
        "occurred_at"
        in client.get(f"/api/v1/incident/{incident.fire_id}/public-view/timeline.csv").text
    )


def test_public_view_remains_readable_during_additive_bulletin_table_rollout(
    client, seed_incident, session
) -> None:
    incident, _episode = seed_incident(
        fire_id="FR-83-00609",
        sequence=609,
        lon=6.04,
        lat=43.31,
    )
    session.execute(text("DROP TABLE incident_bulletin_entry"))
    session.commit()

    response = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")

    assert response.status_code == 200
    assert response.json()["fire_id"] == incident.fire_id


def test_public_report_is_deduplicated_and_never_changes_public_view(client, seed_incident) -> None:
    incident, _episode = seed_incident(fire_id="FR-83-00602", sequence=602, lon=6.03, lat=43.30)
    payload = {
        "category": "information_obsolete",
        "message": "La date de validation affichée doit être vérifiée.",
    }

    first = client.post(f"/api/v1/incident/{incident.fire_id}/reports", json=payload)
    duplicate = client.post(f"/api/v1/incident/{incident.fire_id}/reports", json=payload)

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["replayed"] is True
    assert client.get(f"/api/v1/incident/{incident.fire_id}/public-view").status_code == 200


def test_public_view_exposes_only_published_operational_information(
    client, seed_incident, session
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00603", sequence=603, lon=6.04, lat=43.31)
    session.add_all(
        [
            IncidentOperationalInformation(
                information_id="opinfo-public-0001",
                incident_id=incident.id,
                episode_id=episode.id,
                kind="evacuated_people",
                title="Personnes évacuées",
                value_number=120,
                unit="personnes",
                locality="Hameau des tests",
                authority_kind="prefecture",
                authority_name="Préfecture de test",
                source_url="https://example.invalid/prefecture-public",
                effective_at=episode.last_observed_at,
                published_at=episode.last_observed_at,
                state="PUBLISHED",
                source_reference_url="https://example.invalid/prefecture-source",
                proposal_reason="Communiqué de la préfecture recoupé et prêt à être publié.",
                proposed_by="validator-test",
            ),
            IncidentOperationalInformation(
                information_id="opinfo-private-0001",
                incident_id=incident.id,
                episode_id=episode.id,
                kind="road_status",
                title="Information routière interne",
                value_text="Fermeture à confirmer",
                authority_kind="police",
                authority_name="Police de test",
                source_url="https://example.invalid/police-public",
                state="PROPOSED",
                source_reference_url="https://example.invalid/police-source",
                proposal_reason="Signal à confirmer avant toute publication publique.",
                proposed_by="agent-test",
            ),
        ]
    )
    session.commit()

    response = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")

    assert response.status_code == 200
    body = response.json()
    assert body["operational_information"] == [
        {
            "information_id": "opinfo-public-0001",
            "kind": "evacuated_people",
            "title": "Personnes évacuées",
            "value_text": None,
            "value_number": 120.0,
            "unit": "personnes",
            "locality": "Hameau des tests",
            "authority_kind": "prefecture",
            "authority_name": "Préfecture de test",
            "source_url": "https://example.invalid/prefecture-public",
            "effective_at": body["operational_information"][0]["effective_at"],
            "published_at": body["operational_information"][0]["published_at"],
            "episode_id": episode.episode_id,
        }
    ]
    assert "Information routière interne" not in str(body)
    assert any(event["kind"] == "operational" for event in body["timeline"])


def test_operational_information_requires_admin_publication_before_public_exposure(
    client, seed_incident, session
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00604", sequence=604, lon=6.05, lat=43.32)
    actor = Actor(actor_id="validator-test", roles=frozenset({"validator"}))
    proposed = create_admin_incident_operational_information(
        session,
        fire_id=incident.fire_id,
        payload=AdminOperationalInformationCreateRequest(
            episode_id=episode.episode_id,
            kind="mobilized_personnel",
            title="Équipe mobilisée",
            value_number=86,
            unit="personnes",
            locality="Secteur des tests",
            authority_kind="prefecture",
            authority_name="Préfecture de test",
            source_url="https://example.invalid/prefecture-public",
            source_reference_url="https://example.invalid/prefecture-source",
            proposal_reason="Communiqué qualifié avant décision de publication.",
        ),
        actor=actor,
        trace_id="trace-operational-information",
    )
    session.commit()

    before = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")
    assert before.status_code == 200
    assert before.json()["operational_information"] == []

    published = review_admin_incident_operational_information(
        session,
        fire_id=incident.fire_id,
        information_id=proposed.information_id,
        payload=AdminOperationalInformationReviewRequest(
            action="publish",
            reason="Publication humaine validée.",
            expected_version=proposed.version,
        ),
        actor=actor,
        trace_id="trace-operational-information-review",
    )
    session.commit()

    after = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")
    assert published.state == "PUBLISHED"
    assert published.published_at is not None
    assert after.status_code == 200
    assert [item["title"] for item in after.json()["operational_information"]] == [
        "Équipe mobilisée"
    ]


def test_editorial_gallery_is_separate_and_requires_admin_publication(
    client, seed_incident, session
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00605", sequence=605, lon=6.06, lat=43.33)
    actor = Actor(actor_id="validator-test", roles=frozenset({"validator"}))
    proposed = create_admin_incident_gallery_item(
        session,
        fire_id=incident.fire_id,
        payload=AdminIncidentGalleryCreateRequest(
            episode_id=episode.episode_id,
            title="Photographie de situation",
            caption="Élément éditorial validé séparément.",
            alt_text="Panache de fumée observé depuis une route publique.",
            media_url="https://media.example.invalid/incident/photo.jpg",
            media_kind="image",
            credit="Rédaction FireWarning",
            license_label="Droits vérifiés",
            source_reference_url="https://example.invalid/editorial-source",
            proposal_reason="Élément éditorial avec provenance et droits vérifiés.",
        ),
        actor=actor,
        trace_id="trace-gallery",
    )
    session.commit()

    before = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")
    assert before.status_code == 200
    assert before.json()["gallery"] == []
    assert proposed.state == "PROPOSED"

    published = review_admin_incident_gallery_item(
        session,
        fire_id=incident.fire_id,
        gallery_item_id=proposed.gallery_item_id,
        payload=AdminIncidentGalleryReviewRequest(
            action="publish",
            reason="Publication éditoriale humaine validée.",
            expected_version=proposed.version,
        ),
        actor=actor,
        trace_id="trace-gallery-review",
    )
    session.commit()

    after = client.get(f"/api/v1/incident/{incident.fire_id}/public-view")
    assert published.state == "PUBLISHED"
    assert published.published_at is not None
    assert after.json()["gallery"] == [
        {
            "gallery_item_id": proposed.gallery_item_id,
            "title": "Photographie de situation",
            "caption": "Élément éditorial validé séparément.",
            "alt_text": "Panache de fumée observé depuis une route publique.",
            "media_url": "https://media.example.invalid/incident/photo.jpg",
            "media_kind": "image",
            "credit": "Rédaction FireWarning",
            "license_label": "Droits vérifiés",
            "captured_at": None,
            "published_at": after.json()["gallery"][0]["published_at"],
            "episode_id": episode.episode_id,
        }
    ]
    assert "agent_media" not in str(after.json())
    assert session.query(IncidentGalleryItem).count() == 1
