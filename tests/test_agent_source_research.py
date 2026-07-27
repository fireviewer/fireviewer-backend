from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import func, select

from fire_viewer.db.models import (
    AgentDispatch,
    AgentMediaBatch,
    AgentMediaItem,
    AgentSourceCandidate,
    AgentSourceResearchRun,
    Job,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentBatchType,
    AgentSourceCandidateState,
    AgentSourceResearchState,
)
from fire_viewer.services.agent_dispatcher import run_dispatcher_once
from fire_viewer.services.blob_uploads import BlobUploadGrant
from test_agent_source_packages import _prepare_upload

PINNED_RESEARCH_REVISION = "e7974da369bd887ad4f10a072ec4f933ac5391bf"


class _ResearchRunPod:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None
        self.output: dict[str, object] | None = None
        self.submissions = 0
        self.status_reads = 0

    def submit(self, payload) -> dict[str, object]:
        self.payload = dict(payload)
        self.submissions += 1
        return {"id": "research-job-0001", "status": "IN_QUEUE"}

    def status(self, _remote_job_id: str) -> dict[str, object]:
        self.status_reads += 1
        return {
            "id": "research-job-0001",
            "status": "COMPLETED",
            "output": self.output,
        }

    def cancel(self, _remote_job_id: str) -> dict[str, object]:
        return {"id": "research-job-0001", "status": "CANCELLED"}


def _research_output(
    *,
    research_id: str,
    duplicate_pathname: str,
    duplicate_hash: str,
    duplicate_size: int,
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "schema_version": "research-1.0",
        "research_id": research_id,
        "status": "succeeded",
        "retryable": False,
        "model_run": {
            "model_role": "source_research",
            "model_id": "Qwen/Qwen3-4B-Instruct-2507",
            "revision": PINNED_RESEARCH_REVISION,
            "status": "succeeded",
            "started_at": (now - timedelta(seconds=2)).isoformat(),
            "finished_at": now.isoformat(),
            "load_ms": 900,
            "inference_ms": 800,
            "peak_vram_bytes": 9_000_000_000,
        },
        "queries": ["incendie Die 9 juillet 2026 mairie"],
        "candidates": [
            {
                "candidate_id": "candidate-mairie-accepted",
                "canonical_url": "https://mairie-die.fr/actualite/point-feu-9-juillet",
                "source_domain": "mairie-die.fr",
                "title": "Point feu du 9 juillet",
                "published_at": "2026-07-09T08:00:00+02:00",
                "media_type": "article",
                "excerpt": "La mairie publie son point quotidien sur l'incendie.",
                "attribution": "Ville de Die",
                "provenance": {
                    "tool": "fetch",
                    "retrieved_via": "network_broker",
                    "source_policy_domain": "mairie-die.fr",
                    "source_policy": {
                        "source_name": "Ville de Die",
                        "kind": "authority",
                        "scope": "local",
                        "confidence_level": "A+",
                        "claim_types": [
                            "fire_progression",
                            "evacuation_and_shelter",
                            "population_instruction",
                            "media_asset",
                        ],
                        "publication_policy": "per_item_license_check",
                        "minimum_refresh_minutes": 10,
                    },
                },
            },
            {
                "candidate_id": "candidate-future-rejected",
                "canonical_url": "https://mairie-die.fr/actualite/bilan-20-juillet",
                "source_domain": "mairie-die.fr",
                "title": "Bilan rétrospectif",
                "published_at": "2026-07-20T08:00:00+02:00",
                "media_type": "article",
                "excerpt": "Ce bilan a été publié après la journée analysée.",
                "attribution": "Ville de Die",
                "provenance": {"tool": "fetch", "retrieved_via": "network_broker"},
            },
            {
                "candidate_id": "candidate-user-duplicate",
                "canonical_url": "https://mairie-die.fr/medias/photo-feu.png",
                "source_domain": "mairie-die.fr",
                "title": "Photo déjà fournie par l'utilisateur",
                "published_at": "2026-07-09T09:00:00+02:00",
                "media_type": "image",
                "blob_pathname": duplicate_pathname,
                "media_sha256": duplicate_hash,
                "size_bytes": duplicate_size,
                "attribution": "Ville de Die",
                "provenance": {"tool": "fetch", "retrieved_via": "network_broker"},
            },
        ],
        "validation_errors": [],
    }


def test_research_uses_real_contract_cutoff_dedup_and_shared_dispatcher(
    client,
    session,
    settings,
    seed_incident,
    monkeypatch,
) -> None:
    seed_incident(fire_id="FR-26-00001", sequence=1, lon=5.37, lat=44.75)
    store, total_size = _prepare_upload(monkeypatch, settings, count=1)
    settings.agent_dispatch_enabled = True
    settings.agent_research_enabled = True
    settings.agent_runpod_pod_base_url = "https://pod.example.test"
    settings.agent_runpod_pod_auth_token = SecretStr("p" * 32)
    settings.agent_research_allowed_domains = ["mairie-die.fr"]
    settings.agent_research_search_templates = {
        "search.example": "https://search.example/recherche?q={query}"
    }
    settings.agent_expected_model_revisions = {"source_research": PINNED_RESEARCH_REVISION}

    opened = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/source-packages/open",
        headers={"Idempotency-Key": "die-user-package-dedup-0001"},
        json={
            "file_count": 1,
            "total_size_bytes": total_size,
            "known_start_date": "2026-07-09",
            "location_hint": "Die, massif de Justin",
            "authorize_private_analysis": True,
        },
    )
    assert opened.status_code == 201, opened.text
    finalized = client.post(
        f"/api/v2/admin/agent-batches/source-packages/{opened.json()['package_id']}/finalize"
    )
    assert finalized.status_code == 200, finalized.text

    operations = client.get(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations",
        params={"local_date": "2026-07-09"},
    )
    assert operations.status_code == 200, operations.text
    research_action = next(
        action
        for action in operations.json()["actions"]
        if action["operation_type"] == "source_research"
    )
    assert research_action["can_run"] is True
    research = client.post(
        "/api/v2/admin/agent-batches/incidents/FR-26-00001/operations/source_research/run",
        json={"local_date": "2026-07-09", "location_hint": "Die, massif de Justin"},
    )
    assert research.status_code == 200, research.text
    research_id = research.json()["operation_ids"][0]
    run = session.scalar(
        select(AgentSourceResearchRun).where(AgentSourceResearchRun.research_id == research_id)
    )
    assert run is not None

    package_pathname, package_content = next(iter(store.files.items()))
    duplicate_content = package_content
    duplicate_hash = hashlib.sha256(duplicate_content).hexdigest()
    duplicate_pathname = f"{run.pathname_prefix}/duplicate-photo.png"
    store.files[duplicate_pathname] = duplicate_content
    monkeypatch.setattr(
        "fire_viewer.services.agent_source_research.build_object_store",
        lambda _settings: store,
    )

    def fake_research_grant(**kwargs):
        del kwargs
        return BlobUploadGrant(
            upload_id=run.upload_id,
            pathname_prefix=run.pathname_prefix,
            token="r" * 128,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "fire_viewer.services.agent_source_research.create_source_blob_upload_grant",
        fake_research_grant,
    )
    runpod = _ResearchRunPod()
    runpod.output = _research_output(
        research_id=research_id,
        duplicate_pathname=duplicate_pathname,
        duplicate_hash=duplicate_hash,
        duplicate_size=len(duplicate_content),
    )

    assert run_dispatcher_once(
        client.app.state.session_factory,
        worker_id="research-dispatcher-test",
        settings=settings,
        client=runpod,
    )
    assert runpod.payload is not None
    assert runpod.payload["schema_version"] == "research-1.0"
    assert runpod.payload["operation"] == "source_research"
    assert runpod.payload["allowed_domains"] == ["mairie-die.fr"]
    assert runpod.payload["source_policies"]["mairie-die.fr"]["confidence_level"] == "A+"
    mairie_claims = set(runpod.payload["source_policies"]["mairie-die.fr"]["claim_types"])
    assert {
        "fire_progression",
        "evacuation_and_shelter",
        "population_instruction",
        "personnel_engaged",
        "aircraft_engaged",
        "public_relief_and_donations",
        "media_asset",
    }.issubset(mairie_claims)
    assert "candidates" not in runpod.payload

    session.expire_all()
    run = session.scalar(
        select(AgentSourceResearchRun).where(AgentSourceResearchRun.research_id == research_id)
    )
    assert run is not None
    run.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert run_dispatcher_once(
        client.app.state.session_factory,
        worker_id="research-dispatcher-test",
        settings=settings,
        client=runpod,
    )

    session.expire_all()
    completed = session.scalar(
        select(AgentSourceResearchRun).where(AgentSourceResearchRun.research_id == research_id)
    )
    assert completed is not None
    assert completed.state == AgentSourceResearchState.SUCCEEDED
    candidates = {
        candidate.candidate_id: candidate
        for candidate in session.scalars(select(AgentSourceCandidate))
    }
    assert candidates["candidate-mairie-accepted"].state == AgentSourceCandidateState.ACCEPTED
    assert candidates["candidate-future-rejected"].state == AgentSourceCandidateState.REJECTED
    assert candidates["candidate-future-rejected"].cutoff_eligible is False
    assert candidates["candidate-user-duplicate"].state == AgentSourceCandidateState.DUPLICATE
    batches = session.scalars(select(AgentMediaBatch)).all()
    external_batches = [
        batch for batch in batches if batch.batch_type == AgentBatchType.EXTERNAL_MEDIA
    ]
    assert len(external_batches) == 1
    assert external_batches[0].state == AgentBatchState.QUEUED
    accepted_item = session.scalar(
        select(AgentMediaItem).where(AgentMediaItem.input_id == "candidate-mairie-accepted")
    )
    assert accepted_item is not None
    assert accepted_item.metadata_payload["provenance"]["source_confidence"] == "A+"
    assert accepted_item.metadata_payload["provenance"]["source_kind"] == "authority"
    assert "media_asset" in accepted_item.metadata_payload["provenance"]["claim_types"]
    assert session.scalar(select(func.count()).select_from(AgentDispatch)) == 1
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert package_pathname in store.files
    assert runpod.submissions == 1
    assert runpod.status_reads == 1
