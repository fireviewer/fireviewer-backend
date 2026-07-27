from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from fire_viewer.core.config import Settings
from fire_viewer.core.security import hash_local_password
from fire_viewer.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _StaticSigningKey:
    def __init__(self, key: Any) -> None:
        self.key = key


class _StaticJwkClient:
    """Test-only JWK client: keeps JwtVerifier's real decode path offline."""

    def __init__(self, key: Any) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, _token: str) -> _StaticSigningKey:
        return _StaticSigningKey(self._key)


def _claims(*, roles: list[str]) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "sub": "administrator-test-subject",
        "roles": roles,
        "iss": "https://issuer.example.test/",
        "aud": "fire-viewer-admin",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }


def test_admin_session_uses_server_jwt_verification_and_role_check(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="jwt",
        oidc_jwks_url="https://issuer.example.test/jwks",
        oidc_issuer="https://issuer.example.test/",
        oidc_audience="fire-viewer-admin",
        database_url=f"sqlite:///{tmp_path / 'admin_auth.sqlite'}",
        trusted_hosts=["testserver", "localhost"],
        log_level="CRITICAL",
    )
    application = create_app(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    application.state.jwt_verifier._jwk_client = _StaticJwkClient(private_key.public_key())

    admin_token = jwt.encode(_claims(roles=["administrator"]), private_key, algorithm="RS256")
    viewer_token = jwt.encode(_claims(roles=["viewer"]), private_key, algorithm="RS256")
    unsigned_admin_token = jwt.encode(_claims(roles=["administrator"]), key=None, algorithm="none")

    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            missing = client.get("/api/v1/admin/session")
            unsigned = client.get(
                "/api/v1/admin/session",
                headers={"Authorization": f"Bearer {unsigned_admin_token}"},
            )
            viewer = client.get(
                "/api/v1/admin/session",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            administrator = client.get(
                "/api/v1/admin/session",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
    finally:
        application.state.engine.dispose()

    assert missing.status_code == 401
    assert missing.headers["Cache-Control"] == "no-store"
    assert unsigned.status_code == 401
    assert unsigned.headers["Cache-Control"] == "no-store"
    assert unsigned.headers["WWW-Authenticate"] == "Bearer"
    assert viewer.status_code == 403
    assert viewer.headers["Cache-Control"] == "no-store"
    assert administrator.status_code == 200
    assert administrator.headers["Cache-Control"] == "no-store"
    assert administrator.json() == {"authenticated": True}


def test_local_admin_session_returns_in_memory_csrf_and_protects_logout(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local_admin",
        local_admin_username="admin",
        local_admin_password_hash=hash_local_password("correct horse battery staple"),
        database_url=f"sqlite:///{tmp_path / 'local_admin_auth.sqlite'}",
        trusted_hosts=["testserver", "localhost"],
        log_level="CRITICAL",
    )
    migration_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    migration_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    migration_config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(migration_config, "head")
    application = create_app(settings)
    try:
        with TestClient(
            application,
            base_url="https://testserver",
            raise_server_exceptions=False,
        ) as client:
            login = client.post(
                "/api/v1/admin/auth/login",
                json={"username": "admin", "password": "correct horse battery staple"},
            )
            assert login.status_code == 200, login.text
            csrf = login.json()["csrf_token"]
            session_status = client.get("/api/v1/admin/session")
            dashboard = client.get("/api/v2/admin/dashboard")
            rejected_logout = client.post("/api/v1/admin/auth/logout")
            logout = client.post(
                "/api/v1/admin/auth/logout",
                headers={"X-CSRF-Token": csrf},
            )
            expired = client.get("/api/v1/admin/session")
    finally:
        application.state.engine.dispose()

    assert login.status_code == 200
    assert isinstance(csrf, str) and len(csrf) >= 32
    assert "fireviewer_admin=" in login.headers["set-cookie"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "Path=/api" in login.headers["set-cookie"]
    assert "fireviewer_csrf" not in login.headers["set-cookie"]
    assert session_status.json() == {"authenticated": True, "csrf_token": csrf}
    assert dashboard.status_code == 200, dashboard.text
    assert rejected_logout.status_code == 403
    assert logout.status_code == 204
    assert expired.status_code == 401


def test_local_admin_publication_uses_authenticated_session(tmp_path) -> None:
    password = "correct horse battery staple"
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local_admin",
        local_admin_username="admin",
        local_admin_password_hash=hash_local_password(password),
        database_url=f"sqlite:///{tmp_path / 'local_admin_publication.sqlite'}",
        trusted_hosts=["testserver", "localhost"],
        log_level="CRITICAL",
    )
    migration_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    migration_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    migration_config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(migration_config, "head")
    application = create_app(settings)
    publication = {
        "zone_id": "TEST-ZONE-01",
        "revision": 1,
        "package_id": "pkg-test-zone-01",
        "reason": "Publication contrôlée après aperçu privé.",
    }
    try:
        with TestClient(
            application,
            base_url="https://testserver",
            raise_server_exceptions=False,
        ) as client:
            login = client.post(
                "/api/v1/admin/auth/login",
                json={"username": "admin", "password": password},
            )
            assert login.status_code == 200, login.text
            csrf = login.json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "publish-reauth-test"}
            accepted = client.post("/api/v1/admin/publications", json=publication, headers=headers)
            rejected_legacy_secret = client.post(
                "/api/v1/admin/publications",
                json={**publication, "admin_password": "incorrect password"},
                headers=headers,
            )
    finally:
        application.state.engine.dispose()

    assert accepted.status_code == 404
    assert rejected_legacy_secret.status_code == 422
    assert "admin_password" not in accepted.text
    assert "incorrect password" not in rejected_legacy_secret.text
