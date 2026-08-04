from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor, require_current_role
from fire_viewer.db.models import (
    EventAnalysisJob,
    EventCandidate,
    EvidenceAsset,
    ExternalArtifactRevision,
    ExternalCollection,
    ExternalProvider,
    IncidentCandidate,
    OutboxEvent,
    Viewpoint,
)
from fire_viewer.domain.enums import (
    EventCandidateState,
    ExternalArtifactStatus,
    ExternalSemanticRole,
)
from fire_viewer.domain.errors import BadRequestError, ForbiddenError
from fire_viewer.main import create_app
from fire_viewer.services.blob_uploads import (
    create_source_blob_upload_grant,
    issue_blob_client_token,
)
from fire_viewer.services.event_evidence_access import create_event_evidence_worker_url
from fire_viewer.services.event_v2 import create_private_incident_candidate_from_official_statement

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _migrate(settings: Settings) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")


def _payload(
    *, key: str, message: str | None = "Flammes visibles sur le versant."
) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "viewpoint": {
            "longitude": 6.0214,
            "latitude": 43.2897,
            "horizontal_accuracy_m": 18,
            "origin": "USER_PLACED",
        },
        "observed_time": {"start_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()},
        "message": message,
        "evidence_asset_ids": [],
        "consent": {"analysis": True, "retention": True, "public_derivative": False},
    }


def test_message_only_candidate_is_private_idempotent_and_atomically_queued(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        event_v2_enabled=True,
        event_antivirus_mode="test_clean",
        database_url=f"sqlite:///{tmp_path / 'event-v2.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
        trusted_hosts=["testserver"],
        log_level="CRITICAL",
    )
    _migrate(settings)
    app = create_app(settings)
    key = "c7de51f9-247a-4ab2-b700-61f47bf1fc41"
    submission = _payload(key=key)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            first = client.post("/api/v2/event-candidates", json=submission)
            replay = client.post("/api/v2/event-candidates", json=submission)
            listing = client.get("/api/v2/me/event-candidates")
        with app.state.session_factory() as session:
            candidate_count = session.scalar(select(func.count()).select_from(EventCandidate))
            job_count = session.scalar(select(func.count()).select_from(EventAnalysisJob))
            private_incident_count = session.scalar(
                select(func.count()).select_from(IncidentCandidate)
            )
            outbox = session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "event_candidate.analyze")
            ).scalar_one()
            viewpoint = session.execute(select(Viewpoint)).scalar_one()
    finally:
        app.state.engine.dispose()

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert replay.headers["Idempotent-Replay"] == "true"
    assert first.json()["candidate_id"] == replay.json()["candidate_id"]
    assert first.json()["analysis_job_id"] == replay.json()["analysis_job_id"]
    assert first.json()["incident_id"] is None
    assert first.json()["incident_candidate_id"].startswith("IC-")
    assert first.json()["viewpoint"] == {
        "horizontal_accuracy_m": 18.0,
        "origin": "USER_PLACED",
        "has_orientation": False,
        "exact_position_withheld": True,
    }
    assert "longitude" not in first.text and "latitude" not in first.text
    assert listing.json()["total"] == 1
    assert candidate_count == 1
    assert job_count == 1
    assert private_incident_count == 1
    assert viewpoint.public_derivative_allowed is False
    assert outbox.payload["schema_version"] == "event-2.0"
    assert outbox.payload["bundle"]["message"] == "Flammes visibles sur le versant."
    assert outbox.payload["bundle"]["evidence_assets"] == []
    assert outbox.payload["perception_anchors"] == []


def test_event_candidate_validation_and_private_upload_limits(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        event_v2_enabled=True,
        event_antivirus_mode="test_clean",
        database_url=f"sqlite:///{tmp_path / 'event-validation.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
        trusted_hosts=["testserver"],
        log_level="CRITICAL",
    )
    _migrate(settings)
    app = create_app(settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            missing_viewpoint = _payload(key="b59555e8-4395-4f31-a548-b61b5542134e")
            missing_viewpoint.pop("viewpoint")
            invalid = client.post("/api/v2/event-candidates", json=missing_viewpoint)
            no_evidence = client.post(
                "/api/v2/event-candidates",
                json=_payload(key="02e45082-7175-4569-96c3-e6cfc1aa63e6", message=None),
            )
            mismatched_extension = client.post(
                "/api/v2/evidence/uploads",
                json={
                    "files": [
                        {
                            "file_name": "scene.gif",
                            "media_type": "image/jpeg",
                            "size_bytes": 16,
                        }
                    ]
                },
            )
            upload = client.post(
                "/api/v2/evidence/uploads",
                json={
                    "files": [
                        {
                            "file_name": "scene.jpg",
                            "media_type": "image/jpeg",
                            "size_bytes": 16,
                        },
                        {
                            "file_name": "sequence.mp4",
                            "media_type": "video/mp4",
                            "size_bytes": 20,
                        },
                    ]
                },
            )
            asset_ids = [item["evidence_asset_id"] for item in upload.json()["assets"]]
            pending = client.post(
                "/api/v2/event-candidates",
                json={
                    **_payload(
                        key="cb1696fa-a9cd-43e1-ae28-74f3d3a365ba",
                        message=None,
                    ),
                    "evidence_asset_ids": asset_ids,
                },
            )
            local_uploads = [
                client.put(
                    f"/api/v2/evidence/uploads/{upload.json()['upload_id']}/assets/{asset_ids[0]}",
                    content=b"\xff\xd8\xff" + b"\x00" * 13,
                    headers={"Content-Type": "image/jpeg"},
                ),
                client.put(
                    f"/api/v2/evidence/uploads/{upload.json()['upload_id']}/assets/{asset_ids[1]}",
                    content=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8,
                    headers={"Content-Type": "video/mp4"},
                ),
            ]
            finalized = client.post(
                f"/api/v2/evidence/uploads/{upload.json()['upload_id']}/finalize",
                json={"evidence_asset_ids": asset_ids},
            )
            review_media = client.get(f"/api/v2/internal/evidence-assets/{asset_ids[0]}/content")
            accepted = client.post(
                "/api/v2/event-candidates",
                json={
                    **_payload(
                        key="cb1696fa-a9cd-43e1-ae28-74f3d3a365ba",
                        message=None,
                    ),
                    "evidence_asset_ids": asset_ids,
                },
            )
    finally:
        app.state.engine.dispose()

    assert invalid.status_code == 422
    assert no_evidence.status_code == 422
    assert mismatched_extension.status_code == 422
    assert upload.status_code == 201, upload.text
    assert upload.json()["upload_grant"] is None
    assert upload.json()["client_payload"].startswith("EU-")
    assert len(upload.json()["assets"]) == 2
    assert all(item["upload_state"] == "PENDING_UPLOAD" for item in upload.json()["assets"])
    assert pending.status_code == 409
    assert all(response.status_code == 204 for response in local_uploads)
    assert finalized.status_code == 200, finalized.text
    assert all(item["upload_state"] == "VERIFIED" for item in finalized.json()["assets"])
    assert all(item["scan_state"] == "CLEAN" for item in finalized.json()["assets"])
    assert review_media.status_code == 200, review_media.text
    assert review_media.headers["content-type"].startswith("image/jpeg")
    assert review_media.content == b"\xff\xd8\xff" + b"\x00" * 13
    assert accepted.status_code == 202, accepted.text


def test_private_worker_evidence_is_exact_integrity_checked_and_streamed(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        event_v2_enabled=True,
        event_antivirus_mode="test_clean",
        database_url=f"sqlite:///{tmp_path / 'private-worker-evidence.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
        trusted_hosts=["testserver"],
        log_level="CRITICAL",
    )
    _migrate(settings)
    app = create_app(settings)
    content = b"\xff\xd8\xff" + b"\x00" * 13
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            upload = client.post(
                "/api/v2/evidence/uploads",
                json={
                    "files": [
                        {
                            "file_name": "scene.jpg",
                            "media_type": "image/jpeg",
                            "size_bytes": len(content),
                        }
                    ]
                },
            )
            asset_id = upload.json()["assets"][0]["evidence_asset_id"]
            upload_id = upload.json()["upload_id"]
            assert (
                client.put(
                    f"/api/v2/evidence/uploads/{upload_id}/assets/{asset_id}",
                    content=content,
                    headers={"Content-Type": "image/jpeg"},
                ).status_code
                == 204
            )
            finalized = client.post(
                f"/api/v2/evidence/uploads/{upload_id}/finalize",
                json={"evidence_asset_ids": [asset_id]},
            )
            assert finalized.status_code == 200, finalized.text
            created = client.post(
                "/api/v2/event-candidates",
                json={
                    **_payload(
                        key="a645e815-774b-4d59-b7f6-9686921f31ea",
                        message=None,
                    ),
                    "evidence_asset_ids": [asset_id],
                },
            )
            assert created.status_code == 202, created.text
            with app.state.session_factory() as session:
                candidate = session.execute(
                    select(EventCandidate).where(
                        EventCandidate.candidate_id == created.json()["candidate_id"]
                    )
                ).scalar_one()
                candidate.state = EventCandidateState.ANALYZING
                asset = session.execute(
                    select(EvidenceAsset).where(EvidenceAsset.asset_id == asset_id)
                ).scalar_one()
                session.commit()
                worker_url = create_event_evidence_worker_url(
                    candidate_id=candidate.candidate_id,
                    asset_id=asset.asset_id,
                    sha256=str(asset.sha256),
                    settings=settings,
                )

            parsed = urlsplit(worker_url)
            exact = client.get(f"{parsed.path}?{parsed.query}")
            wrong_asset = client.get(
                f"{parsed.path.replace(asset_id, 'EA-wrong-asset')}?{parsed.query}"
            )
    finally:
        app.state.engine.dispose()

    assert exact.status_code == 200, exact.text
    assert exact.content == content
    assert exact.headers["cache-control"] == "private, no-store"
    assert wrong_asset.status_code == 403
    response_staging = settings.zone_upload_storage_dir / ".event-response-staging"
    assert not response_staging.exists() or not any(response_staging.iterdir())


def test_event_blob_grant_authorizes_only_the_exact_declared_object() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        object_storage_backend="vercel_blob",
        FV_BLOB_READ_WRITE_TOKEN="vercel_blob_rw_test_store-secret",
    )
    actor = Actor(
        actor_id="contributor-a",
        roles=frozenset({"contributor"}),
        email_verified=True,
    )
    pathname = "firewarning/source-packages/exact-upload/01-EA-one/scene.jpg"
    grant = create_source_blob_upload_grant(
        package_id="EU-exact",
        file_count=1,
        total_size_bytes=16,
        actor=actor,
        settings=settings,
        upload_id="exact-upload",
        purpose="event_evidence",
        allowed_files=({"pathname": pathname, "media_type": "image/jpeg", "size_bytes": 16},),
    )

    client_token = issue_blob_client_token(
        pathname=pathname,
        client_payload="EU-exact",
        upload_grant=grant.token,
        settings=settings,
    )
    with pytest.raises(ForbiddenError, match="not a declared event asset"):
        issue_blob_client_token(
            pathname="firewarning/source-packages/exact-upload/02-EA-rogue/scene.jpg",
            client_payload="EU-exact",
            upload_grant=grant.token,
            settings=settings,
        )

    secured = client_token.removeprefix("vercel_blob_client_test_")
    _signature, encoded_payload = base64.b64decode(secured).decode("ascii").split(".", 1)
    payload = json.loads(base64.b64decode(encoded_payload))
    assert payload["pathname"] == pathname
    assert payload["allowedContentTypes"] == ["image/jpeg"]
    assert payload["maximumSizeInBytes"] == 16


class _StaticSigningKey:
    def __init__(self, key: Any) -> None:
        self.key = key


class _StaticJwkClient:
    def __init__(self, key: Any) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, _token: str) -> _StaticSigningKey:
        return _StaticSigningKey(self._key)


def _supabase_token(
    private_key: Any,
    *,
    subject: str,
    app_roles: list[str],
    user_roles: list[str] | None = None,
    email_verified: bool | None = True,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": "https://project.supabase.co/auth/v1",
        "aud": "authenticated",
        "iat": int(now.timestamp()),
        "auth_time": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "email": f"{subject}@example.test",
        "is_anonymous": False,
        "app_metadata": {"roles": app_roles},
        "user_metadata": {"roles": user_roles or []},
    }
    if email_verified is not None:
        claims["email_verified"] = email_verified
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
    )


def test_supabase_roles_use_app_metadata_and_candidate_receipts_are_idor_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="supabase",
        supabase_auth_enabled=True,
        supabase_url="https://project.supabase.co",
        event_v2_enabled=True,
        v2_publication_enabled=True,
        supabase_session_validation_enabled=True,
        supabase_publishable_key="sb_publishable_test_only",
        database_url=f"sqlite:///{tmp_path / 'event-supabase.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
        trusted_hosts=["testserver"],
        log_level="CRITICAL",
    )
    _migrate(settings)
    app = create_app(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    app.state.jwt_verifier._jwk_client = _StaticJwkClient(private_key.public_key())
    token_a = _supabase_token(
        private_key,
        subject="contributor-a",
        app_roles=[],
        email_verified=None,
    )
    # A role injected in user_metadata must not grant editor publication rights.
    token_b = _supabase_token(
        private_key,
        subject="contributor-b",
        app_roles=[],
        user_roles=["editor", "administrator"],
    )
    unverified = _supabase_token(
        private_key,
        subject="contributor-unverified",
        app_roles=[],
        email_verified=False,
    )

    class _UserResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    def _active_user(_url: str, *, headers: dict[str, str], timeout: float) -> _UserResponse:
        assert timeout == 5.0
        if headers["Authorization"] == f"Bearer {token_a}":
            return _UserResponse(
                {
                    "id": "contributor-a",
                    "email": "contributor-a@example.test",
                    "email_confirmed_at": "2026-08-03T12:00:00Z",
                    "is_anonymous": False,
                }
            )
        return _UserResponse(
            {
                "id": "contributor-unverified",
                "email": "contributor-unverified@example.test",
                "email_confirmed_at": None,
                "is_anonymous": False,
            }
        )

    monkeypatch.setattr("fire_viewer.core.security.httpx.get", _active_user)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            created = client.post(
                "/api/v2/event-candidates",
                headers={"Authorization": f"Bearer {token_a}"},
                json=_payload(key="924ba2fc-b0a3-46b8-9c8d-2344621180cd"),
            )
            hidden = client.get(
                f"/api/v2/me/event-candidates/{created.json()['candidate_id']}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            forbidden_editor = client.post(
                "/api/v2/internal/fire-activity-events/unknown/publish",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"reason": "Tentative non autorisée depuis metadata utilisateur."},
            )
            rejected_unverified = client.get(
                "/api/v2/me/event-candidates",
                headers={"Authorization": f"Bearer {unverified}"},
            )
    finally:
        app.state.engine.dispose()

    assert created.status_code == 202, created.text
    assert hidden.status_code == 404
    assert forbidden_editor.status_code == 403
    assert rejected_unverified.status_code == 403


def test_supabase_publication_requires_live_session_validation() -> None:
    with pytest.raises(ValueError, match="session validation"):
        Settings(
            _env_file=None,
            environment="test",
            auth_mode="supabase",
            supabase_auth_enabled=True,
            supabase_url="https://project.supabase.co",
            event_v2_enabled=True,
            v2_publication_enabled=True,
        )


def test_event_v2_rejects_the_known_media_signing_secret_outside_dev() -> None:
    with pytest.raises(ValueError, match="agent_media_signing_secret must be replaced"):
        Settings(
            _env_file=None,
            environment="staging",
            database_url="postgresql+psycopg://user:password@db.example.test/fireviewer",
            auth_mode="supabase",
            supabase_auth_enabled=True,
            supabase_url="https://project.supabase.co",
            supabase_session_validation_enabled=True,
            supabase_publishable_key="sb_publishable_test_only",
            event_v2_enabled=True,
            event_antivirus_mode="clamav",
            agent_media_proxy_base_url="https://api.example.test",
        )


def test_event_3d_feature_flag_uses_the_canonical_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FV_3D_PRIMARY_ENABLED", "true")
    assert Settings(_env_file=None).three_d_primary_enabled is True


def test_internal_event_openapi_contracts_are_closed_and_media_is_binary(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        event_v2_enabled=True,
        event_antivirus_mode="test_clean",
        database_url=f"sqlite:///{tmp_path / 'openapi.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
    )
    app = create_app(settings)
    try:
        schema = app.openapi()
    finally:
        app.state.engine.dispose()
    list_response = schema["paths"]["/api/v2/internal/event-candidates"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    detail_response = schema["paths"]["/api/v2/internal/event-candidates/{candidate_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    media_content = schema["paths"]["/api/v2/internal/evidence-assets/{evidence_asset_id}/content"][
        "get"
    ]["responses"]["200"]["content"]
    assert list_response["$ref"].endswith("/InternalEventCandidateListResponse")
    assert detail_response["$ref"].endswith("/InternalEventCandidateResponse")
    assert "application/octet-stream" in media_content


def test_supabase_role_recheck_rejects_a_role_removed_after_token_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="supabase",
        supabase_auth_enabled=True,
        supabase_url="https://project.supabase.co",
        supabase_session_validation_enabled=True,
        supabase_publishable_key="sb_publishable_test_only",
    )
    actor = Actor(
        actor_id="former-editor",
        roles=frozenset({"contributor", "editor"}),
        email_verified=True,
        issued_at=datetime.now(UTC),
        auth_time=datetime.now(UTC),
        token="stale-editor-token",
    )

    class _UserResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "id": "former-editor",
                "email": "former-editor@example.test",
                "email_confirmed_at": "2026-08-03T12:00:00Z",
                "is_anonymous": False,
                "app_metadata": {"roles": []},
            }

    monkeypatch.setattr(
        "fire_viewer.core.security.httpx.get",
        lambda *_args, **_kwargs: _UserResponse(),
    )
    with pytest.raises(ForbiddenError, match="editor"):
        require_current_role(actor, settings, "editor", "administrator")


def test_hotspot_cannot_seed_incident_but_official_statement_can(session) -> None:
    provider = ExternalProvider(
        provider_key="test-official-provider",
        display_name="Test official provider",
        allowed_domains=["official.example.test"],
        authentication_kind="none",
        attribution="Official test fixture",
        enabled=True,
    )
    session.add(provider)
    session.flush()
    collection = ExternalCollection(
        provider_id=provider.id,
        collection_key="mixed-test-products",
        product_name="Mixed fixture",
        license="Test fixture license",
        semantic_role=ExternalSemanticRole.SENSOR_DETECTION,
        configuration={},
    )
    session.add(collection)
    session.flush()
    hotspot = ExternalArtifactRevision(
        artifact_revision_id="EAR-hotspot-test",
        collection_id=collection.id,
        external_product_id="hotspot-1",
        source_url="https://official.example.test/hotspot-1",
        revision=1,
        content_hash="a" * 64,
        retrieved_at=datetime.now(UTC),
        quality_flags={},
        license="Test fixture license",
        attribution="Official test fixture",
        status=ExternalArtifactStatus.VALIDATED,
        semantic_role=ExternalSemanticRole.SENSOR_DETECTION,
    )
    statement = ExternalArtifactRevision(
        artifact_revision_id="EAR-statement-test",
        collection_id=collection.id,
        external_product_id="statement-1",
        source_url="https://official.example.test/statement-1",
        revision=1,
        content_hash="b" * 64,
        retrieved_at=datetime.now(UTC),
        quality_flags={},
        license="Test fixture license",
        attribution="Official test fixture",
        status=ExternalArtifactStatus.VALIDATED,
        semantic_role=ExternalSemanticRole.OFFICIAL_INCIDENT_STATEMENT,
    )
    session.add_all([hotspot, statement])
    session.flush()

    with pytest.raises(BadRequestError, match="official incident statement"):
        create_private_incident_candidate_from_official_statement(
            session,
            artifact_revision_id=hotspot.id,
            actor_id="official-connector",
        )
    created = create_private_incident_candidate_from_official_statement(
        session,
        artifact_revision_id=statement.id,
        actor_id="official-connector",
    )
    session.flush()
    replay = create_private_incident_candidate_from_official_statement(
        session,
        artifact_revision_id=statement.id,
        actor_id="official-connector",
    )

    assert created.state.value == "PRIVATE_MATCHING"
    assert created.origin_kind == "OFFICIAL_STATEMENT"
    assert replay.id == created.id
