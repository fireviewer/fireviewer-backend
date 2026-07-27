from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from PIL import Image

from fire_viewer.db.models import ActiveFireZoneRevision
from fire_viewer.domain.enums import ActiveFireZoneReviewState


def _zone(incident, episode, *, zone_revision_id: str, state: ActiveFireZoneReviewState):
    return ActiveFireZoneRevision(
        zone_revision_id=zone_revision_id,
        incident_id=incident.id,
        episode_id=episode.id,
        revision=1,
        valid_at=datetime(2026, 7, 9, 18, tzinfo=UTC),
        geometry_geojson={
            "type": "MultiPolygon",
            "coordinates": [[[[5.36, 44.74], [5.39, 44.74], [5.38, 44.77], [5.36, 44.74]]]],
        },
        geometry_origin="AGENT_DERIVED",
        supporting_marker_ids=[],
        source_revision_ids=[],
        review_state=state,
        created_by="worker-test",
        reviewed_by=(
            "admin-test" if state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION else None
        ),
        reviewed_at=(
            datetime(2026, 7, 9, 18, 5, tzinfo=UTC)
            if state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION
            else None
        ),
        review_reason=(
            "Périmètre et sources contrôlés par un opérateur humain."
            if state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION
            else None
        ),
        reason="Zone quotidienne issue des preuves et soumise au contrôle humain.",
    )


def _jpeg() -> bytes:
    output = BytesIO()
    Image.new("RGB", (960, 540), (16, 36, 41)).save(output, format="JPEG", quality=90)
    return output.getvalue()


def test_reviewed_2d_zone_can_publish_a_real_3d_gallery_capture(
    client, seed_incident, session, settings
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00604", sequence=604, lon=5.37, lat=44.75)
    zone = _zone(
        incident,
        episode,
        zone_revision_id="azr-gallery-604",
        state=ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
    )
    session.add(zone)
    session.commit()

    upload_id = "a" * 32
    content = _jpeg()
    pathname = f"gallery-captures/{upload_id}/capture.jpg"
    stored = settings.zone_upload_storage_dir / pathname
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(content)

    finalized = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/map-gallery/from-blob",
        json={
            "upload_id": upload_id,
            "zone_revision_id": zone.zone_revision_id,
            "object": {
                "path": "capture.jpg",
                "pathname": pathname,
                "size_bytes": len(content),
                "content_type": "image/jpeg",
            },
        },
    )

    assert finalized.status_code == 201, finalized.text
    capture = finalized.json()
    assert capture["zone_revision_id"] == zone.zone_revision_id
    assert capture["local_date"] == "2026-07-09"
    assert capture["width_px"] == 960
    assert capture["height_px"] == 540
    admin_image = client.get(capture["image_url"])
    assert admin_image.status_code == 200
    assert admin_image.content == content
    assert admin_image.headers["cache-control"] == "no-store"

    public_view = client.get(f"/api/v1/incident/{incident.fire_id}/public-view").json()
    assert public_view["map_gallery"] == [
        {
            "capture_id": capture["capture_id"],
            "zone_revision_id": zone.zone_revision_id,
            "local_date": "2026-07-09",
            "captured_at": capture["captured_at"],
            "image_url": f"/api/v1/incident/{incident.fire_id}/map-gallery/{capture['capture_id']}",
            "width_px": 960,
            "height_px": 540,
        }
    ]
    public_image = client.get(public_view["map_gallery"][0]["image_url"])
    assert public_image.status_code == 200
    assert public_image.content == content

    zone.review_state = ActiveFireZoneReviewState.REJECTED
    zone.reviewed_by = "admin-test"
    zone.reviewed_at = datetime(2026, 7, 9, 19, tzinfo=UTC)
    zone.review_reason = "Le périmètre est retiré après un nouveau contrôle des sources."
    session.commit()
    private_again = client.get(f"/api/v1/incident/{incident.fire_id}/public-view").json()
    assert private_again["map_gallery"] == []
    assert client.get(public_view["map_gallery"][0]["image_url"]).status_code == 404


def test_draft_zone_cannot_receive_a_gallery_capture(client, seed_incident, session) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00605", sequence=605, lon=5.38, lat=44.76)
    zone = _zone(
        incident,
        episode,
        zone_revision_id="azr-gallery-draft-605",
        state=ActiveFireZoneReviewState.DRAFT,
    )
    session.add(zone)
    session.commit()

    response = client.post(
        f"/api/v1/admin/incidents/{incident.fire_id}/map-gallery/upload-grant",
        json={
            "zone_revision_id": zone.zone_revision_id,
            "size_bytes": 12_345,
            "media_type": "image/jpeg",
        },
    )

    assert response.status_code == 409
    assert response.json()["type"].endswith("map_capture_zone_not_reviewed")
