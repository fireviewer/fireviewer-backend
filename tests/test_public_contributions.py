from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from io import BytesIO

from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select

from fire_viewer.core.security import Actor
from fire_viewer.db.models import (
    AgentDispatch,
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    AgentScheduleRun,
    AgentSourcePackage,
    AgentSourcePackageItem,
    Job,
    PublicContributionSubmission,
)
from fire_viewer.domain.enums import (
    ActorType,
    AgentAnalysisState,
    AgentBatchType,
    AgentConsentState,
)
from fire_viewer.services.agent_source_packages import ensure_daily_analysis_window
from fire_viewer.services.blob_uploads import BlobUploadGrant, create_source_blob_upload_grant
from fire_viewer.services.public_contribution_schedule import (
    run_public_contribution_schedule_once,
)
from fire_viewer.storage.object_store import ObjectMetadata


class _FakePublicStore:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def list_prefix(self, key: str, *, limit: int) -> list[ObjectMetadata]:
        prefix = f"firewarning/{key}/"
        return [
            ObjectMetadata(pathname=pathname, size_bytes=len(content), content_type="image/png")
            for pathname, content in sorted(self.files.items())
            if pathname.startswith(prefix)
        ][:limit]

    def uri_for_pathname(self, pathname: str) -> str:
        return f"local-test://{pathname}"

    def read_bytes(self, uri: str) -> bytes:
        return self.files[uri.removeprefix("local-test://")]

    def delete_tree(self, key: str) -> None:
        self.deleted.append(key)
        fragment = f"/{key}/"
        self.files = {path: data for path, data in self.files.items() if fragment not in path}


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), color=(210, 50, 20)).save(output, format="PNG")
    return output.getvalue()


def _configure_upload(monkeypatch, settings) -> tuple[_FakePublicStore, bytes]:
    store = _FakePublicStore()
    content = _png()
    settings.object_storage_backend = "vercel_blob"
    settings.blob_read_write_token = SecretStr("vercel_blob_rw_teststore_test-secret")
    settings.agent_media_proxy_base_url = "https://testserver"
    settings.agent_media_allowed_hosts = ["testserver"]

    def fake_grant(**kwargs):
        assert kwargs["purpose"] == "public_contribution"
        return BlobUploadGrant(
            upload_id="public-upload-fixed",
            pathname_prefix="firewarning/source-packages/public-upload-fixed",
            token="g" * 128,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "fire_viewer.services.public_contributions.create_source_blob_upload_grant", fake_grant
    )
    monkeypatch.setattr(
        "fire_viewer.services.agent_source_packages.build_object_store", lambda _settings: store
    )
    monkeypatch.setattr(
        "fire_viewer.services.public_contributions.build_object_store", lambda _settings: store
    )
    return store, content


def _payload(
    *,
    content_size: int | None = None,
    kind: str = "incident_evidence",
    fire_id: str | None = "FR-26-00001",
    with_media: bool = True,
    contact: str | None = "Temoin@example.fr",
) -> dict[str, object]:
    observed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    return {
        "kind": kind,
        "fire_id": fire_id,
        "location": {"mode": "place", "label": "Die, massif de Justin"},
        "observation": {
            "observation_type": "front de flammes",
            "observed_at": observed_at,
            "direct_observation": True,
            "description": "Un front de flammes est visible sur le versant au-dessus de Die.",
        },
        "media": (
            {
                "filename": "preuve.png",
                "content_type": "image/png",
                "size_bytes": content_size,
                "direction": "vers le nord-est",
            }
            if with_media
            else None
        ),
        "consents": {
            "private_analysis": True,
            "retain_evidence": True,
            "public_display": True,
            "spatial_display": False,
        },
        "contact_email": contact,
    }


def _open(client, payload: dict[str, object], *, key: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/contributions/open",
        headers={"Idempotency-Key": key},
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_public_incident_evidence_uses_private_user_media_and_human_review(
    client, session, settings, seed_incident, monkeypatch
) -> None:
    seed_incident(fire_id="FR-26-00001", sequence=1, lon=5.37, lat=44.75)
    store, content = _configure_upload(monkeypatch, settings)
    payload = _payload(content_size=len(content))
    opened = _open(client, payload, key="public-evidence-die-0001")
    assert opened["state"] == "OPEN"
    assert opened["upload"]["allowed_content_types"] == [
        "image/jpeg",
        "image/png",
        "image/webp",
    ]
    contribution_id = opened["contribution_id"]
    token = opened["tracking_token"]
    # Opening allocates only the opaque receipt and private upload grant.  No agent
    # batch exists until the contributor has finalized the declared evidence.
    assert session.scalar(select(func.count()).select_from(AgentMediaBatch)) == 0
    store.files["firewarning/source-packages/public-upload-fixed/0001-preuve.png"] = content

    finalized = client.post(
        f"/api/v1/contributions/{contribution_id}/finalize",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert finalized.status_code == 202, finalized.text
    assert finalized.json()["contribution"]["state"] == "PENDING"
    assert session.scalar(select(func.count()).select_from(PublicContributionSubmission)) == 1
    assert session.scalar(select(func.count()).select_from(AgentSourcePackage)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMediaBatch)) == 1
    assert session.scalar(select(func.count()).select_from(AgentMediaItem)) == 1
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    batch = session.scalar(select(AgentMediaBatch))
    assert batch is not None
    assert batch.schema_version == "2.0"
    assert batch.batch_type == AgentBatchType.USER_MEDIA
    assert batch.incident_id is not None and batch.analysis_window_id is not None
    media_item = session.scalar(select(AgentMediaItem))
    assert media_item is not None
    declared = media_item.metadata_payload["provenance"]["declared_observation"]
    assert datetime.fromisoformat(declared["observed_at"].replace("Z", "+00:00")) == (
        datetime.fromisoformat(payload["observation"]["observed_at"])
    )
    assert {key: value for key, value in declared.items() if key != "observed_at"} == {
        "observation_type": "front de flammes",
        "direct_observation": True,
        "description": "Un front de flammes est visible sur le versant au-dessus de Die.",
        "location_mode": "place",
        "location_label": "Die, massif de Justin",
        "latitude": None,
        "longitude": None,
        "uncertainty_m": None,
        "media_captured_at": None,
        "media_direction": "vers le nord-est",
    }
    assert datetime.fromisoformat(
        media_item.metadata_payload["captured_at"].replace("Z", "+00:00")
    ) == datetime.fromisoformat(payload["observation"]["observed_at"])
    consent = session.scalar(select(AgentMediaConsent))
    assert consent is not None
    assert consent.state == AgentConsentState.GRANTED
    assert consent.scopes == [
        "temporary_storage",
        "agent_analysis",
        "human_review",
        "retain_evidence",
        "display_media",
    ]
    row = session.scalar(select(PublicContributionSubmission))
    assert row is not None
    assert row.contact_reference_hash is not None
    assert "Temoin@example.fr" not in str(row.submission_payload)

    # Finalization authorizes private agent handling, but not public exposure.
    private_view = client.get("/api/v1/incident/FR-26-00001/public-view")
    assert private_view.status_code == 200, private_view.text
    assert private_view.json()["participatory_observation_count"] is None
    assert private_view.json()["participatory_published_count"] is None


    assert private_view.json()["participatory_received_count"] is None
    assert private_view.json()["gallery"] == []

    settings.agent_dispatch_enabled = True
    assert (
        run_public_contribution_schedule_once(
            session,
            worker_id="vercel-contributions:test",
            settings=settings,
        )
        == 1
    )
    schedule = session.scalar(select(AgentScheduleRun))
    assert schedule is not None
    assert schedule.last_run_at is not None
    assert schedule.next_run_at > schedule.last_run_at
    assert (
        schedule.next_run_at - schedule.last_run_at
    ).total_seconds() == settings.public_contribution_agent_interval_seconds
    assert (
        run_public_contribution_schedule_once(
            session,
            worker_id="vercel-contributions:test-replay",
            settings=settings,
        )
        == 0
    )

    # The manual action cannot create a duplicate after the three-hour
    # scheduler has already created the dispatch.
    launched = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations/user_media/run",
        json={"expected_analysis_window_id": batch.analysis_window.analysis_id},
    )
    assert launched.status_code == 200, launched.text
    assert launched.json()["operation_ids"] == [batch.batch_id]
    dispatch = session.scalar(select(AgentDispatch))
    assert dispatch is not None
    dispatched_item = dispatch.payload["items"][0]
    assert datetime.fromisoformat(dispatched_item["captured_at"].replace("Z", "+00:00")) == (
        datetime.fromisoformat(payload["observation"]["observed_at"])
    )
    assert dispatched_item["provenance"]["declared_observation"] == {
        key: value for key, value in declared.items() if value is not None
    }

    pending = client.get("/api/v1/admin/public-contributions?state=PENDING")
    assert pending.status_code == 200, pending.text
    contribution = pending.json()["contributions"][0]
    assert contribution["private_media_urls"]
    reviewed = client.post(
        f"/api/v1/admin/public-contributions/{contribution_id}/review",
        headers={"Idempotency-Key": "public-evidence-review-0001"},
        json={
            "state": "ACCEPTED",
            "reason": "Preuve cohérente, acceptée pour analyse privée et rapprochement.",
            "expected_version": contribution["version"],
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["contribution"]["state"] == "ACCEPTED"
    public_view = client.get("/api/v1/incident/FR-26-00001/public-view")
    assert public_view.status_code == 200, public_view.text
    assert public_view.json()["participatory_observation_count"] == 1
    assert public_view.json()["participatory_published_count"] == 1
    # Received includes private submissions, so it remains deliberately absent.
    assert public_view.json()["participatory_received_count"] is None
    # An accepted contribution is not an editorial gallery item.
    assert public_view.json()["gallery"] == []
    tracked = client.get(
        f"/api/v1/contributions/{contribution_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert tracked.status_code == 200
    assert tracked.json()["contribution"]["state"] == "ACCEPTED"


def test_public_historical_evidence_does_not_reopen_terminal_agent_window(
    client, session, settings, seed_incident, monkeypatch
) -> None:
    incident, episode = seed_incident(
        fire_id="FR-26-00001",
        sequence=1,
        lon=5.37,
        lat=44.75,
    )
    window = ensure_daily_analysis_window(
        session,
        incident=incident,
        episode=episode,
        local_date=datetime(2026, 7, 12, tzinfo=UTC).date(),
    )
    window.state = AgentAnalysisState.COMPLETED
    session.commit()

    store, content = _configure_upload(monkeypatch, settings)
    payload = _payload(content_size=len(content))
    payload["observation"]["observed_at"] = "2026-07-12T18:30:00+00:00"
    opened = _open(client, payload, key="public-terminal-window-0001")
    store.files["firewarning/source-packages/public-upload-fixed/0001-preuve.png"] = content

    finalized = client.post(
        f"/api/v1/contributions/{opened['contribution_id']}/finalize",
        headers={"Authorization": f"Bearer {opened['tracking_token']}"},
    )

    assert finalized.status_code == 202, finalized.text
    assert finalized.json()["contribution"]["state"] == "PENDING"
    session.refresh(window)
    assert window.state == AgentAnalysisState.COMPLETED
    batch = session.scalar(select(AgentMediaBatch))
    assert batch is not None
    assert batch.schema_version == "1.0"
    assert batch.analysis_window_id is None
    package_item = session.scalar(select(AgentSourcePackageItem))
    assert package_item is not None
    assert package_item.classified_local_date == datetime(2026, 7, 12, tzinfo=UTC).date()
    assert package_item.analysis_window_id is None
    assert package_item.metadata_payload["terminal_analysis_window_unlinked"] is True


def test_new_fire_image_creates_unassigned_v1_batch(client, session, settings, monkeypatch) -> None:
    store, content = _configure_upload(monkeypatch, settings)
    opened = _open(
        client,
        _payload(content_size=len(content), kind="new_fire", fire_id=None, contact=None),
        key="public-new-fire-0001",
    )
    store.files["firewarning/source-packages/public-upload-fixed/0001-preuve.png"] = content
    finalized = client.post(
        f"/api/v1/contributions/{opened['contribution_id']}/finalize",
        headers={"Authorization": f"Bearer {opened['tracking_token']}"},
    )
    assert finalized.status_code == 202, finalized.text
    batch = session.scalar(select(AgentMediaBatch))
    assert batch is not None
    assert batch.schema_version == "1.0"
    assert batch.incident_id is None
    assert batch.episode_id is None
    assert batch.analysis_window_id is None


def test_public_text_contribution_is_persistent_without_media(client, session) -> None:
    opened = _open(
        client,
        _payload(kind="new_fire", fire_id=None, with_media=False, contact=None),
        key="public-new-fire-text-0001",
    )
    assert opened["state"] == "PENDING"
    assert opened["upload"] is None
    row = session.scalar(select(PublicContributionSubmission))
    assert row is not None and row.received_at is not None
    assert session.scalar(select(func.count()).select_from(AgentSourcePackage)) == 0


def test_public_blob_callback_only_grants_private_image_contract(client, settings) -> None:
    settings.object_storage_backend = "vercel_blob"
    settings.blob_read_write_token = SecretStr("vercel_blob_rw_teststore_testsecret")
    grant = create_source_blob_upload_grant(
        package_id="SP-PUBLIC-0001",
        file_count=1,
        total_size_bytes=1_024,
        actor=Actor(
            actor_id="public-contribution:PC-0001",
            roles=frozenset(),
            actor_type=ActorType.PUBLIC_SOURCE,
        ),
        settings=settings,
        purpose="public_contribution",
    )
    payload = {
        "type": "blob.generate-client-token",
        "payload": {
            "pathname": f"{grant.pathname_prefix}/0001-preuve.webp",
            "multipart": True,
            "clientPayload": "SP-PUBLIC-0001",
        },
    }

    response = client.post(
        "/api/v1/contributions/blob-upload-token",
        json=payload,
        headers={"X-Blob-Upload-Grant": grant.token},
    )

    assert response.status_code == 200, response.text
    token_prefix = "vercel_blob_client_teststore_"
    client_token = response.json()["clientToken"]
    assert client_token.startswith(token_prefix)
    secured = base64.b64decode(client_token.removeprefix(token_prefix)).decode("ascii")
    _, encoded_payload = secured.split(".", maxsplit=1)
    token_payload = json.loads(base64.b64decode(encoded_payload))
    assert token_payload["allowedContentTypes"] == ["image/jpeg", "image/png", "image/webp"]
    assert token_payload["maximumSizeInBytes"] == settings.public_contribution_max_image_bytes
    assert token_payload["allowOverwrite"] is False

    invalid_type = {
        **payload,
        "payload": {**payload["payload"], "pathname": f"{grant.pathname_prefix}/preuve.pdf"},
    }
    denied = client.post(
        "/api/v1/contributions/blob-upload-token",
        json=invalid_type,
        headers={"X-Blob-Upload-Grant": grant.token},
    )
    assert denied.status_code == 400


def test_public_media_capture_time_requires_timezone(client, seed_incident) -> None:
    seed_incident(fire_id="FR-26-00001", sequence=1, lon=5.37, lat=44.75)
    payload = _payload(content_size=1_024)
    payload["media"]["captured_at"] = "2026-07-10T09:00:00"

    response = client.post(
        "/api/v1/contributions/open",
        headers={"Idempotency-Key": "public-naive-capture-0001"},
        json=payload,
    )

    assert response.status_code == 422, response.text


def test_tracking_token_idempotency_rate_limit_and_withdrawal(
    client, session, settings, seed_incident, monkeypatch
) -> None:
    seed_incident(fire_id="FR-26-00001", sequence=1, lon=5.37, lat=44.75)
    store, content = _configure_upload(monkeypatch, settings)
    payload = _payload(content_size=len(content))
    opened = _open(client, payload, key="public-idempotent-0001")
    replay = client.post(
        "/api/v1/contributions/open",
        headers={"Idempotency-Key": "public-idempotent-0001"},
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["tracking_token"] == opened["tracking_token"]
    conflict_payload = dict(payload)
    conflict_payload["contact_email"] = "autre@example.fr"
    conflict = client.post(
        "/api/v1/contributions/open",
        headers={"Idempotency-Key": "public-idempotent-0001"},
        json=conflict_payload,
    )
    assert conflict.status_code == 409
    denied = client.get(
        f"/api/v1/contributions/{opened['contribution_id']}",
        headers={"Authorization": "Bearer " + "x" * 43},
    )
    assert denied.status_code == 403

    store.files["firewarning/source-packages/public-upload-fixed/0001-preuve.png"] = content
    client.post(
        f"/api/v1/contributions/{opened['contribution_id']}/finalize",
        headers={"Authorization": f"Bearer {opened['tracking_token']}"},
    )
    withdrawn = client.post(
        f"/api/v1/contributions/{opened['contribution_id']}/withdraw",
        headers={"Authorization": f"Bearer {opened['tracking_token']}"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["contribution"]["state"] == "WITHDRAWN"
    assert store.deleted == ["source-packages/public-upload-fixed"]
    consent = session.scalar(select(AgentMediaConsent))
    assert consent is not None and consent.state == AgentConsentState.WITHDRAWN
    batch = session.scalar(select(AgentMediaBatch))
    assert batch is not None and batch.state.value == "CANCELLED"

    settings.public_contribution_rate_limit_per_day = 2
    first = _payload(kind="new_fire", fire_id=None, with_media=False, contact=None)
    assert (
        client.post(
            "/api/v1/contributions/open",
            headers={"Idempotency-Key": "public-limit-0001"},
            json=first,
        ).status_code
        == 201
    )
    limited = client.post(
        "/api/v1/contributions/open",
        headers={"Idempotency-Key": "public-limit-0002"},
        json=first,
    )
    assert limited.status_code == 409
