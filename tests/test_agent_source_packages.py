from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select
from tifffile import imwrite

from fire_viewer.db.models import (
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    AgentSourcePackage,
    AgentSourcePackageItem,
    Job,
)
from fire_viewer.domain.enums import (
    AgentBatchType,
    AgentConsentBasis,
    AgentConsentState,
    AgentMediaType,
)
from fire_viewer.domain.errors import BadRequestError
from fire_viewer.services.agent_batches import _worker_payload
from fire_viewer.services.agent_source_packages import (
    _geotiff_metadata,
    ensure_daily_analysis_window,
)
from fire_viewer.services.agent_validation_campaigns import create_campaign_from_manifest
from fire_viewer.services.blob_uploads import BlobUploadGrant
from fire_viewer.storage.object_store import ObjectMetadata


class _FakeSourceStore:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def list_prefix(self, key: str, *, limit: int) -> list[ObjectMetadata]:
        prefix = f"firewarning/{key}/"
        return [
            ObjectMetadata(pathname=pathname, size_bytes=len(content), content_type=None)
            for pathname, content in sorted(self.files.items())
            if pathname.startswith(prefix)
        ][:limit]

    def uri_for_pathname(self, pathname: str) -> str:
        return f"local-test://{pathname}"

    def read_bytes(self, uri: str) -> bytes:
        return self.files[uri.removeprefix("local-test://")]


def _png(index: int) -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 4), color=(index % 255, (index * 3) % 255, 17)).save(output, format="PNG")
    return output.getvalue()


def _six_band_geotiff(
    *,
    origin_x: float = 2.0,
    origin_y: float = 49.0,
    pixel_size: float = 0.01,
) -> bytes:
    output = BytesIO()
    metadata = (
        '<GDALMetadata><Item name="FIREVIEWER_BAND_ORDER">'
        "BLUE,GREEN,RED,NIR_NARROW,SWIR_1,SWIR_2"
        "</Item></GDALMetadata>"
    )
    geo_keys = (
        1,
        1,
        0,
        3,
        1024,
        0,
        1,
        2,
        1025,
        0,
        1,
        1,
        2048,
        0,
        1,
        4326,
    )
    imwrite(
        output,
        np.zeros((10, 20, 6), dtype=np.uint16),
        photometric="minisblack",
        planarconfig="contig",
        metadata=None,
        extratags=[
            (33550, "d", 3, (pixel_size, pixel_size, 0.0), False),
            (33922, "d", 6, (0.0, 0.0, 0.0, origin_x, origin_y, 0.0), False),
            (34735, "H", len(geo_keys), geo_keys, False),
            (42112, "s", len(metadata) + 1, metadata, False),
        ],
    )
    return output.getvalue()


def test_geotiff_metadata_validates_six_band_georeferencing() -> None:
    metadata = _geotiff_metadata(
        _six_band_geotiff(),
        declared_bands=[
            "BLUE",
            "GREEN",
            "RED",
            "NIR_NARROW",
            "SWIR_1",
            "SWIR_2",
        ],
        declared_bbox_wgs84=(2.0, 48.9, 2.2, 49.0),
    )

    assert metadata["image_width_px"] == 20
    assert metadata["image_height_px"] == 10
    assert metadata["samples_per_pixel"] == 6
    assert metadata["crs"] == "EPSG:4326"
    assert metadata["geotransform"] == [2.0, 0.01, 0.0, 49.0, 0.0, -0.01]


def test_geotiff_metadata_rejects_a_declared_bbox_mismatch() -> None:
    with pytest.raises(BadRequestError):
        _geotiff_metadata(
            _six_band_geotiff(),
            declared_bands=[
                "BLUE",
                "GREEN",
                "RED",
                "NIR_NARROW",
                "SWIR_1",
                "SWIR_2",
            ],
            declared_bbox_wgs84=(3.0, 48.9, 3.2, 49.0),
        )


def _prepare_upload(monkeypatch, settings, *, count: int) -> tuple[_FakeSourceStore, int]:
    files = {
        f"firewarning/source-packages/upload-fixed/photo-{index:02d}.png": _png(index)
        for index in range(count)
    }
    store = _FakeSourceStore(files)
    total_size = sum(len(content) for content in files.values())
    settings.object_storage_backend = "vercel_blob"
    settings.blob_read_write_token = SecretStr("vercel_blob_rw_teststore_test-secret")
    settings.agent_media_proxy_base_url = "https://testserver"
    settings.agent_media_allowed_hosts = ["testserver"]

    def fake_grant(**kwargs):
        del kwargs
        return BlobUploadGrant(
            upload_id="upload-fixed",
            pathname_prefix="firewarning/source-packages/upload-fixed",
            token="g" * 128,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "fire_viewer.services.agent_source_packages.create_source_blob_upload_grant",
        fake_grant,
    )
    monkeypatch.setattr(
        "fire_viewer.services.agent_source_packages.build_object_store", lambda _settings: store
    )
    return store, total_size


def _canonical_sha256(payload: dict[str, object], excluded_key: str) -> str:
    normalized = {key: value for key, value in payload.items() if key != excluded_key}
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def test_normal_source_package_endpoint_splits_to_user_media_without_job(
    client, session, settings, seed_incident, monkeypatch
) -> None:
    seed_incident(fire_id="FR-26-00001", sequence=1, lon=5.37, lat=44.75)
    _store, total_size = _prepare_upload(monkeypatch, settings, count=33)
    opened = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/source-packages/open",
        headers={"Idempotency-Key": "die-user-package-day-0001"},
        json={
            "file_count": 33,
            "total_size_bytes": total_size,
            "known_start_date": "2026-07-09",
            "known_end_date": "2026-07-09",
            "location_hint": "Die, massif de Justin",
            "authorize_private_analysis": True,
        },
    )
    assert opened.status_code == 201, opened.text
    package_id = opened.json()["package_id"]

    finalized = client.post(f"/api/v2/admin/agent-batches/source-packages/{package_id}/finalize")
    assert finalized.status_code == 200, finalized.text
    result = finalized.json()
    assert result["state"] == "CONVERTED"
    assert result["analysis_authorized"] is True
    assert result["publication_authorized"] is False
    assert len(result["items"]) == 33
    assert len(result["batch_ids"]) == 2
    assert session.scalar(select(func.count()).select_from(AgentSourcePackage)) == 1
    assert session.scalar(select(func.count()).select_from(AgentSourcePackageItem)) == 33
    assert session.scalar(select(func.count()).select_from(AgentMediaBatch)) == 2
    assert session.scalar(select(func.count()).select_from(AgentMediaItem)) == 33
    assert session.scalar(select(func.count()).select_from(AgentMediaConsent)) == 33
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert {batch.batch_type for batch in session.scalars(select(AgentMediaBatch)).all()} == {
        AgentBatchType.USER_MEDIA
    }


def test_daily_satellite_package_uses_active_window_and_builds_sensor_inputs(
    client, session, settings, seed_incident, monkeypatch, tmp_path
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-77-00001",
        sequence=1,
        lon=2.61,
        lat=48.39,
    )
    window = ensure_daily_analysis_window(
        session,
        incident=incident,
        episode=episode,
        local_date=date(2026, 7, 12),
    )
    image = _six_band_geotiff(origin_x=2.5, origin_y=48.49)
    hotspots = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [2.61, 48.39]},
                    "properties": {"sensor": "VIIRS SNPP"},
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    image_sha = hashlib.sha256(image).hexdigest()
    hotspot_sha = hashlib.sha256(hotspots).hexdigest()
    cutoff_at = window.window_end_at
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=UTC)
    day: dict[str, object] = {
        "ordinal": 1,
        "fire_id": incident.fire_id,
        "local_date": window.local_date.isoformat(),
        "cutoff_at": cutoff_at.isoformat(),
        "allowed_media_sha256": [image_sha, hotspot_sha],
        "expected_public_sources": ["https://example.test/fontainebleau/source"],
        "required_operations": ["satellite_media"],
        "declared_absences": ["user_media", "source_research"],
    }
    day["manifest_sha256"] = _canonical_sha256(day, "manifest_sha256")
    campaign: dict[str, object] = {
        "schema_version": "2.0",
        "campaign_id": "fontainebleau-satellite-package-test",
        "days": [day],
    }
    campaign["manifest_sha256"] = _canonical_sha256(campaign, "manifest_sha256")
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
    create_campaign_from_manifest(
        session,
        manifest_path=campaign_path,
        created_by="test-suite",
    )

    manifest = json.dumps(
        {
            "schema_version": "1.0",
            "expected_analysis_window_id": window.analysis_id,
            "items": [
                {
                    "kind": "satellite_image",
                    "filename": "sentinel-six-band.tif",
                    "sha256": image_sha,
                    "product_id": "S2-FONT-20260712",
                    "provider": "Copernicus",
                    "acquired_at": "2026-07-12T10:30:00Z",
                    "bbox_wgs84": [2.5, 48.39, 2.7, 48.49],
                    "resolution_m": 20,
                    "bands": [
                        "BLUE",
                        "GREEN",
                        "RED",
                        "NIR_NARROW",
                        "SWIR_1",
                        "SWIR_2",
                    ],
                    "source_reference_url": "https://dataspace.copernicus.eu/",
                    "license_identifier": "COPERNICUS-DATA",
                    "attribution": "Copernicus Sentinel-2",
                },
                {
                    "kind": "hotspot_geojson",
                    "filename": "hotspots.geojson",
                    "sha256": hotspot_sha,
                    "product_id": "VIIRS-FONT-20260712",
                    "provider": "NASA FIRMS",
                    "acquired_at": "2026-07-12T18:00:00Z",
                    "bbox_wgs84": [2.46, 48.34, 2.72, 48.44],
                    "resolution_m": 375,
                    "sensor_names": ["VIIRS SNPP"],
                    "source_reference_url": "https://firms.modaps.eosdis.nasa.gov/",
                    "license_identifier": "NASA-OPEN-DATA",
                    "attribution": "NASA FIRMS",
                },
            ],
        },
        separators=(",", ":"),
    ).encode()
    files = {
        "firewarning/source-packages/upload-fixed/fireviewer-satellite-manifest.json": manifest,
        "firewarning/source-packages/upload-fixed/sentinel-six-band.tif": image,
        "firewarning/source-packages/upload-fixed/hotspots.geojson": hotspots,
    }
    store = _FakeSourceStore(files)
    total_size = sum(len(content) for content in files.values())
    settings.object_storage_backend = "vercel_blob"
    settings.blob_read_write_token = SecretStr("vercel_blob_rw_teststore_test-secret")
    settings.agent_media_proxy_base_url = "https://testserver"
    settings.agent_media_allowed_hosts = ["testserver"]

    def fake_grant(**kwargs):
        del kwargs
        return BlobUploadGrant(
            upload_id="upload-fixed",
            pathname_prefix="firewarning/source-packages/upload-fixed",
            token="g" * 128,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "fire_viewer.services.agent_source_packages.create_source_blob_upload_grant",
        fake_grant,
    )
    monkeypatch.setattr(
        "fire_viewer.services.agent_source_packages.build_object_store",
        lambda _settings: store,
    )

    opened = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-77-00001/daily-inputs/satellite/open",
        headers={"Idempotency-Key": "fontainebleau-satellite-day-0001"},
        json={
            "expected_analysis_window_id": window.analysis_id,
            "file_count": len(files),
            "total_size_bytes": total_size,
        },
    )
    assert opened.status_code == 201, opened.text
    finalized = client.post(
        f"/api/v2/admin/agent-batches/source-packages/{opened.json()['package_id']}/finalize"
    )
    assert finalized.status_code == 200, finalized.text
    result = finalized.json()
    assert result["package_kind"] == "ADMIN_SATELLITE"
    assert result["known_start_date"] == "2026-07-12"
    assert len(result["items"]) == 3
    assert len(result["batch_ids"]) == 1

    batch = session.scalar(select(AgentMediaBatch))
    assert batch is not None
    assert batch.batch_type == AgentBatchType.SATELLITE_MEDIA
    assert batch.analysis_window_id == window.id
    assert batch.state.value == "DRAFT"
    assert batch.reference_bundle_payload is not None
    assert batch.reference_bundle_payload["assets"][0]["kind"] == "source_manifest"
    media_items = list(session.scalars(select(AgentMediaItem).order_by(AgentMediaItem.id)))
    assert {item.media_type for item in media_items} == {
        AgentMediaType.SATELLITE_IMAGE,
        AgentMediaType.SATELLITE_DATA,
    }
    hotspot_item = next(
        item for item in media_items if item.media_type == AgentMediaType.SATELLITE_DATA
    )
    assert hotspot_item.working_file_url is None
    assert hotspot_item.processable_payload["article_text"].startswith('{"features":')
    assert hotspot_item.metadata_payload["hotspot"]["provider"] == "NASA FIRMS"
    assert hotspot_item.consent.basis == AgentConsentBasis.INSTITUTIONAL_MANDATE
    assert session.scalar(select(func.count()).select_from(Job)) == 0

    worker_payload = _worker_payload(batch)
    assert worker_payload["analysis_window"]["analysis_id"] == window.analysis_id
    assert worker_payload["reference_bundle"]["assets"][0]["kind"] == "source_manifest"
    hotspot_payload = next(
        item for item in worker_payload["items"] if item["media_type"] == "satellite_data"
    )
    assert hotspot_payload["hotspot"]["provider"] == "NASA FIRMS"
    assert hotspot_payload["hotspot"]["bbox_wgs84"] == [2.46, 48.34, 2.72, 48.44]
    image_payload = next(
        item for item in worker_payload["items"] if item["media_type"] == "satellite_image"
    )
    assert image_payload["satellite"]["bands"] == [
        "BLUE",
        "GREEN",
        "RED",
        "NIR_NARROW",
        "SWIR_1",
        "SWIR_2",
    ]
    assert image_payload["satellite"]["crs"] == "EPSG:4326"
    assert image_payload["satellite"]["geotransform"] == [
        2.5,
        0.01,
        0.0,
        48.49,
        0.0,
        -0.01,
    ]


def test_private_media_proxy_rechecks_consent(
    client, session, settings, seed_incident, monkeypatch
) -> None:
    seed_incident(fire_id="FR-26-00001", sequence=1, lon=5.37, lat=44.75)
    _store, total_size = _prepare_upload(monkeypatch, settings, count=1)
    opened = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/source-packages/open",
        headers={"Idempotency-Key": "die-user-package-proxy-0001"},
        json={
            "file_count": 1,
            "total_size_bytes": total_size,
            "known_start_date": "2026-07-09",
            "location_hint": "Die",
            "authorize_private_analysis": True,
        },
    )
    package_id = opened.json()["package_id"]
    finalized = client.post(f"/api/v2/admin/agent-batches/source-packages/{package_id}/finalize")
    assert finalized.json()["items"]
    media_item = session.scalar(select(AgentMediaItem))
    assert media_item is not None and media_item.working_file_url is not None
    parsed = urlparse(media_item.working_file_url)
    token = parse_qs(parsed.query)["token"][0]

    downloaded = client.get(f"{parsed.path}?token={token}")
    assert downloaded.status_code == 200
    assert downloaded.content.startswith(b"\x89PNG")

    consent = session.scalar(select(AgentMediaConsent))
    assert consent is not None
    consent.state = AgentConsentState.WITHDRAWN
    session.commit()
    denied = client.get(f"{parsed.path}?token={token}")
    assert denied.status_code == 403
