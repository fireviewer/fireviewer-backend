from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from fire_viewer.db.models import (
    IncidentSpatialMarker,
    ManifestRevision,
    ModelAsset,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import (
    AssetLod,
    AssetState,
    IncidentMarkerReviewState,
)
from fire_viewer.domain.spatial import derive_raf20_origin


def _square(west: float, south: float, east: float, north: float) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def test_admin_3d_workspace_edits_merges_and_approves_without_publishing(
    client, session, seed_incident
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00501", sequence=501, lon=6.0214, lat=43.2897)
    zone = SpatialZone(zone_id="INCIDENT-3D-REVIEW", label="Terrain privé de revue")
    session.add(zone)
    session.flush()
    origin = derive_raf20_origin(6.0214, 43.2897, 400.0)
    spatial_revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=origin.source_orthometric_height_m,
        geoid_undulation_m=origin.geoid_undulation_m,
        origin_ellipsoid_height_m=origin.ellipsoid_height_m,
        min_east_m=-2_000,
        max_east_m=2_000,
        min_north_m=-2_000,
        max_north_m=2_000,
        min_up_m=-100,
        max_up_m=1_000,
    )
    session.add(spatial_revision)
    session.flush()
    asset = ModelAsset(
        asset_id="asset-incident-3d-review",
        spatial_zone_revision_id=spatial_revision.id,
        version=1,
        lod=AssetLod.DESKTOP,
        state=AssetState.VALIDATED,
        glb_url="https://private.example.invalid/incident.glb",
        sha256="d" * 64,
        size_bytes=123_456,
        generated_at=datetime.now(UTC),
    )
    session.add(asset)
    session.flush()
    session.add(
        ManifestRevision(
            incident_id=incident.id,
            episode_id=episode.id,
            asset_id=asset.id,
            spatial_zone_revision_id=spatial_revision.id,
            revision=1,
            is_current=True,
            reason="Revision privee utilisee par l'editeur 3D de zone active.",
            actor_id="spatial-review-test",
        )
    )
    marker = IncidentSpatialMarker(
        marker_id="IM-test-spatial-review",
        incident_id=incident.id,
        episode_id=episode.id,
        marker_type="media_capture",
        longitude=6.0218,
        latitude=43.2899,
        horizontal_accuracy_m=12,
        geometry_origin="METADATA",
        review_state=IncidentMarkerReviewState.PENDING,
        spatial_display_allowed=False,
        version=1,
    )
    session.add(marker)
    session.commit()

    workspace = client.get(f"/api/v1/admin/incidents/{incident.fire_id}/spatial-review")
    assert workspace.status_code == 200, workspace.text
    assert workspace.headers["Cache-Control"] == "no-store"
    assert workspace.json()["scene"]["asset_url"].endswith("incident.glb")
    assert workspace.json()["markers"][0]["gltf_position"] is not None
    projected_origin = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/spatial-review/project-pick",
        json={"gltf_position": [0, 0, 0]},
    )
    assert projected_origin.status_code == 200, projected_origin.text
    assert abs(projected_origin.json()["longitude"] - 6.0214) < 1e-9
    assert abs(projected_origin.json()["latitude"] - 43.2897) < 1e-9

    reviewed_marker = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/spatial-markers/{marker.marker_id}/review",
        json={
            "action": "validate",
            "expected_version": 1,
            "reason": "Coordonnées vérifiées dans la scène et la source originale.",
        },
    )
    assert reviewed_marker.status_code == 200, reviewed_marker.text
    assert reviewed_marker.json()["review_state"] == "VALIDATED"

    first = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/active-zone-revisions",
        json={
            "expected_latest_revision": 0,
            "valid_at": datetime.now(UTC).isoformat(),
            "geometry_geojson": _square(6.020, 43.288, 6.022, 43.290),
            "supporting_marker_ids": [marker.marker_id],
            "reason": "Contour saisi manuellement depuis les références visibles en 3D.",
        },
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/active-zone-revisions",
        json={
            "expected_latest_revision": 1,
            "valid_at": datetime.now(UTC).isoformat(),
            "geometry_geojson": _square(6.021, 43.289, 6.023, 43.291),
            "supporting_marker_ids": [marker.marker_id],
            "reason": "Deuxième contour édité depuis une autre vue géoréférencée.",
        },
    )
    assert second.status_code == 201, second.text

    merged = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/active-zone-revisions/merge",
        json={
            "expected_latest_revision": 2,
            "source_revision_ids": [
                first.json()["zone_revision_id"],
                second.json()["zone_revision_id"],
            ],
            "valid_at": datetime.now(UTC).isoformat(),
            "supporting_marker_ids": [marker.marker_id],
            "reason": "Fusion topologique contrôlée des deux contours édités en 3D.",
        },
    )
    assert merged.status_code == 201, merged.text
    assert merged.json()["geometry_origin"] == "DETERMINISTIC_UNION"
    assert merged.json()["review_state"] == "DRAFT"
    assert merged.json()["gltf_polygons"]

    approved = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/active-zone-revisions/"
        f"{merged.json()['zone_revision_id']}/review",
        json={
            "action": "approve",
            "expected_state": "DRAFT",
            "reason": "Contour fusionné contrôlé visuellement et pièces justificatives validées.",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["review_state"] == "READY_FOR_PUBLICATION"
    assert "published" not in approved.json()
    assert session.scalar(select(func.count()).select_from(ModelAsset)) == 1
    assert session.scalar(select(func.count()).select_from(ManifestRevision)) == 1

    refreshed = client.get(f"/api/v1/admin/incidents/{incident.fire_id}/spatial-review")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["scene"]["asset_url"] == asset.glb_url

    retracted = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/active-zone-revisions/"
        f"{merged.json()['zone_revision_id']}/review",
        json={
            "action": "reject",
            "expected_state": "READY_FOR_PUBLICATION",
            "reason": "Calque retiré de la scène après un nouveau contrôle opérateur.",
        },
    )
    assert retracted.status_code == 200, retracted.text
    assert retracted.json()["review_state"] == "REJECTED"


def test_invalid_self_intersection_is_rejected_instead_of_silently_repaired(
    client, seed_incident
) -> None:
    incident, _ = seed_incident(fire_id="FR-83-00502", sequence=502, lon=6.0214, lat=43.2897)
    response = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/active-zone-revisions",
        json={
            "expected_latest_revision": 0,
            "valid_at": datetime.now(UTC).isoformat(),
            "geometry_geojson": {
                "type": "Polygon",
                "coordinates": [[[6.0, 43.0], [6.1, 43.1], [6.0, 43.1], [6.1, 43.0], [6.0, 43.0]]],
            },
            "reason": "Contour volontairement invalide pour vérifier le rejet déterministe.",
        },
    )
    assert response.status_code == 400
    assert response.json()["type"].endswith("active_zone_geometry_invalid")
