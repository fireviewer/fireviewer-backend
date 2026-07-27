from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select

from fire_viewer.db.models import (
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    AgentSourcePackage,
    AgentSourcePackageItem,
    Job,
)
from fire_viewer.domain.enums import AgentBatchType, AgentConsentState
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
