from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from fire_viewer.core.config import Settings
from fire_viewer.db.engine import normalize_database_url
from fire_viewer.main import trusted_hosts_for_runtime
from fire_viewer.storage.object_store import (
    LocalObjectStore,
    ObjectStorageError,
    VercelBlobObjectStore,
    build_object_store,
)


def test_neon_database_urls_select_psycopg3() -> None:
    assert (
        normalize_database_url("postgres://user:secret@db.example/firewarning?sslmode=require")
        == "postgresql+psycopg://user:secret@db.example/firewarning?sslmode=require"
    )
    assert (
        normalize_database_url("postgresql://user:secret@db.example/firewarning")
        == "postgresql+psycopg://user:secret@db.example/firewarning"
    )
    assert normalize_database_url("sqlite:///./local.db") == "sqlite:///./local.db"


def test_hosted_runtime_rejects_sqlite_and_production_local_storage() -> None:
    common = {
        "_env_file": None,
        "auth_mode": "jwt",
        "oidc_jwks_url": "https://identity.example/.well-known/jwks.json",
        "oidc_issuer": "https://identity.example/",
        "oidc_audience": "firewarning",
        "public_report_hash_secret": "a" * 32,
        "trusted_hosts": ["firewarning.example"],
    }

    with pytest.raises(ValidationError, match="PostgreSQL is required"):
        Settings(environment="staging", database_url="sqlite:///./data.db", **common)

    with pytest.raises(ValidationError, match="Vercel Blob private storage is required"):
        Settings(
            environment="production",
            database_url="postgresql://user:secret@db.example/firewarning",
            object_storage_backend="local",
            **common,
        )


def test_blob_token_accepts_vercel_marketplace_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel-marketplace-token")
    settings = Settings(_env_file=None)
    assert settings.blob_read_write_token is not None
    assert settings.blob_read_write_token.get_secret_value() == "vercel-marketplace-token"


def test_cron_secret_accepts_vercel_native_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "x" * 40)
    settings = Settings(_env_file=None)
    assert settings.cron_secret is not None
    assert settings.cron_secret.get_secret_value() == "x" * 40


def test_vercel_runtime_host_is_added_to_the_exact_trusted_host_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL_URL", "fireviewer-api-abc123-charli-dev420s-projects.vercel.app")

    allowed_hosts = trusted_hosts_for_runtime(
        Settings(_env_file=None, trusted_hosts=["fireviewer-api.vercel.app"])
    )

    assert allowed_hosts == [
        "fireviewer-api-abc123-charli-dev420s-projects.vercel.app",
        "fireviewer-api.vercel.app",
    ]


def test_untrusted_runtime_host_is_not_added(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERCEL_URL", "unexpected.example")

    allowed_hosts = trusted_hosts_for_runtime(
        Settings(_env_file=None, trusted_hosts=["fireviewer-api.vercel.app"])
    )

    assert allowed_hosts == ["fireviewer-api.vercel.app"]


def test_direct_pod_dispatch_requires_a_long_token_and_https() -> None:
    with pytest.raises(ValidationError, match="at least 32 items"):
        Settings(
            _env_file=None,
            agent_dispatch_enabled=True,
            agent_runpod_transport="pod",
            agent_runpod_pod_base_url="https://pod-test.example",
            agent_runpod_pod_auth_token="too-short",
        )

    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            _env_file=None,
            agent_dispatch_enabled=True,
            agent_runpod_transport="pod",
            agent_runpod_pod_base_url="http://pod-test.example",
            agent_runpod_pod_auth_token="x" * 40,
        )


def test_local_object_store_round_trip_is_immutable(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "manifest.json").write_text('{"version": 1}', encoding="utf-8")

    store.finalize_tree(staged, "zones/Z-001/revision-1")
    uri = store.uri_for("zones/Z-001/revision-1/manifest.json")

    assert not staged.exists()
    assert store.read_bytes(uri) == b'{"version": 1}'

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "manifest.json").write_text("duplicate", encoding="utf-8")
    with pytest.raises(ObjectStorageError, match="already exists"):
        store.finalize_tree(duplicate, "zones/Z-001/revision-1")


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "/absolute",
        "zones\\windows",
        "zones/./revision",
        "zones/../escape",
        "zones//revision",
    ],
)
def test_object_store_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(ObjectStorageError):
        store.uri_for(key)


def test_vercel_blob_store_uses_private_immutable_objects(tmp_path: Path) -> None:
    calls: list[tuple[Path, str, dict[str, object]]] = []
    deleted: list[list[str]] = []
    objects: dict[str, bytes] = {}

    class FakeBlobClient:
        def upload_file(self, source: Path, object_key: str, **kwargs: object):
            calls.append((source, object_key, kwargs))
            objects[object_key] = source.read_bytes()
            return SimpleNamespace(pathname=object_key, url=f"https://blob.example/{object_key}")

        def delete(self, urls: list[str]) -> None:
            deleted.append(urls)

        def iter_objects(self, *, prefix: str):
            return iter([])

        def get(self, pathname: str, **kwargs: object):
            assert kwargs == {"access": "private", "use_cache": False}
            return SimpleNamespace(status_code=200, content=objects[pathname])

    store = VercelBlobObjectStore(prefix="firewarning", token="secret")
    store.client = FakeBlobClient()  # type: ignore[assignment]
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "scene.glb").write_bytes(b"glb")

    store.finalize_tree(staged, "zones/Z-001/revision-1")
    uri = store.uri_for("zones/Z-001/revision-1/scene.glb")

    assert uri == "vercel-blob://firewarning/zones/Z-001/revision-1/scene.glb"
    assert store.read_bytes(uri) == b"glb"
    assert deleted == []
    assert len(calls) == 1
    assert calls[0][1] == "firewarning/zones/Z-001/revision-1/scene.glb"
    assert calls[0][2]["access"] == "private"
    assert calls[0][2]["overwrite"] is False
    assert calls[0][2]["add_random_suffix"] is False


def test_vercel_blob_store_lists_a_bounded_prefix_inventory() -> None:
    calls: list[dict[str, object]] = []

    class FakeBlobClient:
        def iter_objects(self, **kwargs: object):
            calls.append(kwargs)
            return iter(
                [
                    SimpleNamespace(
                        pathname="firewarning/packages/upload/catalog.json",
                        size=123,
                    ),
                    SimpleNamespace(
                        pathname="firewarning/packages/upload/assets/tile.fwtile",
                        size=456,
                    ),
                ]
            )

    store = VercelBlobObjectStore(prefix="firewarning", token="secret")
    store.client = FakeBlobClient()  # type: ignore[assignment]

    result = store.list_prefix("packages/upload", limit=2_001)

    assert calls == [
        {
            "prefix": "firewarning/packages/upload/",
            "batch_size": 1_000,
            "limit": 2_001,
        }
    ]
    assert [(item.pathname, item.size_bytes, item.content_type) for item in result] == [
        ("firewarning/packages/upload/catalog.json", 123, None),
        ("firewarning/packages/upload/assets/tile.fwtile", 456, None),
    ]


def test_object_store_factory_uses_configured_backend(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        zone_upload_storage_dir=tmp_path / "objects",
        object_storage_backend="local",
    )
    assert isinstance(build_object_store(settings), LocalObjectStore)


def test_readiness_requires_current_schema_and_spatial_runtime(client, session) -> None:
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "database": "ok",
        "schema_revision": "f4b7d2c9a610",
        "spatial_index": "ok",
    }

    session.execute(text("UPDATE alembic_version SET version_num = '000000000000'"))
    session.commit()

    stale = client.get("/readyz")
    assert stale.status_code == 503
    assert stale.json()["type"] == "urn:fire-viewer:error:not_ready"
