from __future__ import annotations

from datetime import UTC, datetime

from fire_viewer.db.models import (
    ManifestRevision,
    ModelAsset,
    Observation,
    Source,
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
    ZonePublication,
    ZonePublicationEvent,
)
from fire_viewer.domain.enums import (
    AssetLod,
    AssetState,
    IncidentStatus,
    MatchDecision,
    SourceTrust,
    SourceType,
    SpatialPackageFileKind,
    SpatialPackageState,
    VerificationState,
    ZonePublicationState,
)
from fire_viewer.domain.spatial import derive_raf20_origin


def test_operational_map_projects_incidents_and_controlled_models(
    client, session, seed_incident
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00142",
        sequence=142,
        lon=6.0214,
        lat=43.2897,
        canonical_name="Massif des Maures",
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    seed_incident(
        fire_id="FR-83-00143",
        sequence=143,
        lon=6.26,
        lat=43.42,
        canonical_name="Estérel",
        status=IncidentStatus.MONITORING,
    )
    zone = SpatialZone(zone_id="ZONE-MAURES", label="Massif des Maures")
    session.add(zone)
    session.flush()
    origin = derive_raf20_origin(6.0214, 43.2897, 400.0)
    revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=origin.source_orthometric_height_m,
        geoid_undulation_m=origin.geoid_undulation_m,
        origin_ellipsoid_height_m=origin.ellipsoid_height_m,
        min_east_m=-1_000.0,
        max_east_m=1_000.0,
        min_north_m=-1_000.0,
        max_north_m=1_000.0,
        min_up_m=-100.0,
        max_up_m=1_000.0,
    )
    session.add(revision)
    session.flush()
    current_asset = ModelAsset(
        asset_id="asset-map-current",
        spatial_zone_revision_id=revision.id,
        version=1,
        lod=AssetLod.DESKTOP,
        state=AssetState.PUBLISHED,
        glb_url="vercel-blob://firewarning/uploads/pkg/assets/rapproche.glb",
        sha256="a" * 64,
        size_bytes=2_048,
        terrain_source_year=2025,
        generated_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
        published_at=datetime(2026, 7, 15, 8, 15, tzinfo=UTC),
    )
    session.add(current_asset)
    session.flush()
    manifest = ManifestRevision(
        incident_id=incident.id,
        episode_id=episode.id,
        asset_id=current_asset.id,
        spatial_zone_revision_id=revision.id,
        revision=1,
        is_current=True,
        reason="Association du paquet spatial vérifié à l'incident.",
        actor_id="map-test",
    )
    session.add(manifest)
    package = SpatialPackage(
        package_id="PKG-MAURES-R1",
        manifest_uri="vercel-blob://firewarning/uploads/pkg/package-manifest.json",
        manifest_sha256="b" * 64,
        manifest_size_bytes=100,
        storage_uri="vercel-blob://firewarning/uploads/pkg",
        state=SpatialPackageState.DRAFT,
        provenance={"zone_id": zone.zone_id, "revision": 1},
        verification_report={"status": "imported"},
        created_by="map-test",
    )
    session.add(package)
    session.flush()
    package.state = SpatialPackageState.VERIFIED
    package.verification_report = {"status": "passed"}
    package.verified_at = datetime(2026, 7, 15, 8, 10, tzinfo=UTC)
    package.spatial_zone_revision_id = revision.id
    session.flush()
    close_file = SpatialPackageFile(
        spatial_package_id=package.id,
        kind=SpatialPackageFileKind.GLB,
        uri="vercel-blob://firewarning/uploads/pkg/assets/rapproche.glb",
        sha256="a" * 64,
        size_bytes=2_048,
        media_type="model/gltf-binary",
        provenance={"catalog_path": "assets/rapproche.glb"},
    )
    extended_file = SpatialPackageFile(
        spatial_package_id=package.id,
        kind=SpatialPackageFileKind.GLB,
        uri="vercel-blob://firewarning/uploads/pkg/assets/etendu.glb",
        sha256="c" * 64,
        size_bytes=4_096,
        media_type="model/gltf-binary",
        provenance={"catalog_path": "assets/etendu.glb"},
    )
    session.add_all([close_file, extended_file])
    session.flush()
    publication = ZonePublication(
        publication_id="PUB-MAURES-R1",
        spatial_zone_id=zone.id,
        spatial_zone_revision_id=revision.id,
        spatial_package_id=package.id,
        state=ZonePublicationState.DRAFT,
        is_active=False,
        reason="Création de la publication pour la carte opérationnelle.",
        actor_id="map-test",
    )
    session.add(publication)
    session.flush()
    session.add(
        ZonePublicationEvent(
            event_id="ZPE-MAURES-0",
            zone_publication_id=publication.id,
            from_state=None,
            to_state=ZonePublicationState.DRAFT,
            action="create",
            reason=publication.reason,
            actor_id="map-test",
            event_metadata={},
        )
    )
    session.flush()
    verified_reason = "Validation de la publication pour la carte opérationnelle."
    session.add(
        ZonePublicationEvent(
            event_id="ZPE-MAURES-1",
            zone_publication_id=publication.id,
            from_state=ZonePublicationState.DRAFT,
            to_state=ZonePublicationState.VERIFIED,
            action="verify",
            reason=verified_reason,
            actor_id="map-test",
            event_metadata={},
        )
    )
    session.flush()
    publication.state = ZonePublicationState.VERIFIED
    publication.reason = verified_reason
    session.flush()
    package.state = SpatialPackageState.PREVIEWABLE
    session.flush()
    preview_reason = "Aperçu validé pour la carte opérationnelle."
    session.add(
        ZonePublicationEvent(
            event_id="ZPE-MAURES-2",
            zone_publication_id=publication.id,
            from_state=ZonePublicationState.VERIFIED,
            to_state=ZonePublicationState.PREVIEWABLE,
            action="preview",
            reason=preview_reason,
            actor_id="map-test",
            event_metadata={},
        )
    )
    session.flush()
    publication.state = ZonePublicationState.PREVIEWABLE
    publication.reason = preview_reason
    session.flush()
    package.state = SpatialPackageState.PUBLISHED
    session.flush()
    current_asset.spatial_package_file_id = close_file.id
    session.flush()
    manifest.spatial_package_id = package.id
    session.flush()
    published_reason = "Publication du paquet vérifié pour la carte opérationnelle."
    session.add(
        ZonePublicationEvent(
            event_id="ZPE-MAURES-3",
            zone_publication_id=publication.id,
            from_state=ZonePublicationState.PREVIEWABLE,
            to_state=ZonePublicationState.PUBLISHED,
            action="publish",
            reason=published_reason,
            actor_id="map-test",
            event_metadata={},
        )
    )
    session.flush()
    publication.state = ZonePublicationState.PUBLISHED
    publication.is_active = True
    publication.reason = published_reason
    session.commit()

    response = client.get("/api/v2/admin/operational-map")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["coordinate_system"] == "EPSG:4326"
    assert body["summary"] == {
        "total_incidents": 2,
        "active_incidents": 1,
        "monitoring_incidents": 1,
        "archived_incidents": 0,
        "incidents_requiring_review": 0,
        "pending_signals": 0,
        "attached_signals": 0,
        "incidents_with_models": 1,
        "model_updates_available": 0,
    }
    assert body["signals"] == []
    mapped = next(item for item in body["incidents"] if item["fire_id"] == incident.fire_id)
    assert mapped["spatial_zone_id"] == zone.zone_id
    assert mapped["active_package_id"] == package.package_id
    assert [model["profile"] for model in mapped["models"]] == ["close", "extended"]
    assert mapped["models"][0]["is_current"] is True
    assert mapped["models"][0]["access_path"].startswith("/api/v2/admin/packages/")
    assert "vercel-blob://" not in response.text
    assert "uploads/pkg" not in response.text


def test_operational_map_projects_pending_attached_and_archived_layers(
    client, session, seed_incident
) -> None:
    active_incident, active_episode = seed_incident(
        fire_id="FR-83-00150",
        sequence=150,
        lon=6.05,
        lat=43.31,
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    seed_incident(
        fire_id="FR-83-00151",
        sequence=151,
        lon=6.15,
        lat=43.35,
        status=IncidentStatus.CLOSED,
        ended_at=datetime(2026, 7, 15, 7, 30, tzinfo=UTC),
    )
    source = Source(
        source_key="operational-map-feed",
        source_type=SourceType.SENSOR,
        trust=SourceTrust.INSTITUTIONAL,
        display_name="Flux de test",
        enabled=True,
    )
    session.add(source)
    session.flush()
    observed_at = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)

    def observation(
        observation_id: str,
        *,
        state: VerificationState,
        attached: bool,
    ) -> Observation:
        return Observation(
            observation_id=observation_id,
            source_id=source.id,
            observed_at=observed_at,
            received_at=observed_at,
            longitude=6.07,
            latitude=43.32,
            horizontal_uncertainty_m=300.0,
            territory_code="83",
            toponyms=["Massif des Maures"],
            canonical_name_hint="Massif des Maures",
            evidence_hash=f"sha256:{observation_id[-1] * 64}",
            evidence_license="test-fixture",
            request_hash=observation_id[-1] * 64,
            verification_state=state,
            attached_incident_id=active_incident.id if attached else None,
            attached_episode_id=active_episode.id if attached else None,
            proposed_incident_id=active_incident.id if not attached else None,
            proposed_episode_id=active_episode.id if not attached else None,
            match_decision=MatchDecision.ATTACH if attached else MatchDecision.REVIEW,
            match_score=0.72,
            match_factors={"distance": 0.8},
            review_reasons=[] if attached else ["score_below_auto_attach_threshold"],
            policy_id="test-policy-v1",
            trace_id=f"trace-{observation_id}",
            version=1,
        )

    session.add_all(
        [
            observation(
                "obs-map-pending-a", state=VerificationState.PENDING_REVIEW, attached=False
            ),
            observation("obs-map-attached-b", state=VerificationState.VERIFIED, attached=True),
            observation("obs-map-rejected-c", state=VerificationState.REJECTED, attached=False),
        ]
    )
    session.commit()

    response = client.get("/api/v2/admin/operational-map")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["archived_incidents"] == 1
    assert body["summary"]["pending_signals"] == 1
    assert body["summary"]["attached_signals"] == 1
    assert [item["observation_id"] for item in body["signals"]] == [
        "obs-map-pending-a",
        "obs-map-attached-b",
    ]
    pending = next(item for item in body["signals"] if item["state"] == "pending")
    assert pending["version"] == 1
    assert pending["proposed_fire_id"] == active_incident.fire_id
    assert pending["attached_fire_id"] is None
    attached = next(item for item in body["signals"] if item["state"] == "attached")
    assert attached["attached_fire_id"] == active_incident.fire_id
    assert "obs-map-rejected-c" not in response.text


def test_operational_map_requires_admin_authentication(client, app) -> None:
    app.state.settings.auth_mode = "jwt"
    response = client.get("/api/v2/admin/operational-map")
    assert response.status_code == 401
