"""Persistent, model-directed public-source research on the shared GPU queue."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import suppress
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.research_sources import research_source_policy_payload
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentAnalysisWindow,
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    AgentSourceCandidate,
    AgentSourcePackageItem,
    AgentSourceResearchRun,
    Episode,
    IncidentSeries,
)
from fire_viewer.domain.agent_schemas import (
    AgentSourceCandidateResponse,
    AgentSourceResearchResponse,
    WorkerResearchInputV1,
    WorkerResearchOutputV1,
)
from fire_viewer.domain.enums import (
    ActorType,
    AgentBatchPriority,
    AgentBatchState,
    AgentBatchType,
    AgentConsentBasis,
    AgentConsentState,
    AgentMediaType,
    AgentSourceCandidateState,
    AgentSourceResearchState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.services.agent_source_packages import create_private_media_url
from fire_viewer.services.blob_uploads import (
    ALLOWED_SOURCE_CONTENT_TYPES,
    create_source_blob_upload_grant,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.storage import build_object_store
from fire_viewer.storage.object_store import ObjectStorageError

_REMOTE_ACTIVE = frozenset({"IN_QUEUE", "IN_PROGRESS", "RUNNING"})
_CLAIMABLE = (
    AgentSourceResearchState.QUEUED,
    AgentSourceResearchState.SUBMITTING,
    AgentSourceResearchState.RUNNING,
    AgentSourceResearchState.CANCEL_REQUESTED,
)


class ResearchRunPodTransport(Protocol):
    def submit(self, payload: Mapping[str, object]) -> dict[str, Any]: ...

    def status(self, remote_job_id: str) -> dict[str, Any]: ...

    def cancel(self, remote_job_id: str) -> dict[str, Any]: ...


def _system_actor(worker_id: str) -> Actor:
    return Actor(actor_id=worker_id, roles=frozenset(), actor_type=ActorType.SYSTEM)


def _incident_episode(session: Session, fire_id: str) -> tuple[IncidentSeries, Episode]:
    incident = session.execute(
        select(IncidentSeries).where(IncidentSeries.fire_id == fire_id)
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = session.execute(
        select(Episode).where(Episode.incident_id == incident.id, Episode.is_current.is_(True))
    ).scalar_one_or_none()
    if episode is None:
        raise ConflictError("incident_without_current_episode", "Incident has no current episode.")
    return incident, episode


def _load_run(session: Session, research_id: str) -> AgentSourceResearchRun:
    run = session.execute(
        select(AgentSourceResearchRun)
        .where(AgentSourceResearchRun.research_id == research_id)
        .options(
            selectinload(AgentSourceResearchRun.incident),
            selectinload(AgentSourceResearchRun.episode),
            selectinload(AgentSourceResearchRun.analysis_window),
            selectinload(AgentSourceResearchRun.candidates),
        )
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError("agent_source_research", research_id)
    return run


def _response(run: AgentSourceResearchRun) -> AgentSourceResearchResponse:
    return AgentSourceResearchResponse(
        research_id=run.research_id,
        fire_id=run.incident.fire_id,
        episode_id=run.episode.episode_id,
        analysis_id=run.analysis_window.analysis_id,
        local_date=run.analysis_window.local_date,
        state=run.state,
        progress_percent=run.progress_percent,
        queued_at=as_utc(run.queued_at),
        started_at=as_utc(run.started_at) if run.started_at else None,
        completed_at=as_utc(run.completed_at) if run.completed_at else None,
        candidates=[
            AgentSourceCandidateResponse(
                candidate_id=candidate.candidate_id,
                state=candidate.state,
                canonical_url=candidate.canonical_url,
                source_domain=candidate.source_domain,
                title=candidate.title,
                published_at=(as_utc(candidate.published_at) if candidate.published_at else None),
                acquired_at=(as_utc(candidate.acquired_at) if candidate.acquired_at else None),
                media_type=candidate.media_type,
                media_sha256=candidate.media_sha256,
                cutoff_eligible=candidate.cutoff_eligible,
                license_identifier=candidate.license_identifier,
                attribution=candidate.attribution,
            )
            for candidate in run.candidates
        ],
    )


def create_source_research(
    session: Session,
    *,
    fire_id: str,
    expected_analysis_window_id: str,
    location_hint: str | None,
    actor: Actor,
    trace_id: str,
    settings: Settings,
    query_plan_overrides: Mapping[str, object] | None = None,
) -> AgentSourceResearchResponse:
    if not settings.agent_dispatch_enabled or not settings.agent_research_enabled:
        raise ConflictError("agent_research_disabled", "Public-source research is not enabled.")
    if settings.object_storage_backend != "vercel_blob":
        raise ConflictError(
            "agent_research_storage_unavailable",
            "Public-source research requires private Vercel Blob storage.",
    )
    incident, episode = _incident_episode(session, fire_id)
    window = session.execute(
        select(AgentAnalysisWindow).where(
            AgentAnalysisWindow.analysis_id == expected_analysis_window_id,
            AgentAnalysisWindow.incident_id == incident.id,
            AgentAnalysisWindow.episode_id == episode.id,
        )
    ).scalar_one_or_none()
    if window is None:
        raise ConflictError(
            "agent_analysis_window_mismatch",
            "The requested analysis window is not the active incident window.",
        )
    active = session.execute(
        select(AgentSourceResearchRun).where(
            AgentSourceResearchRun.analysis_window_id == window.id,
            AgentSourceResearchRun.state.in_(
                {
                    AgentSourceResearchState.QUEUED,
                    AgentSourceResearchState.SUBMITTING,
                    AgentSourceResearchState.RUNNING,
                }
            ),
        )
    ).scalar_one_or_none()
    if active is not None:
        return _response(_load_run(session, active.research_id))

    research_id = new_prefixed_id("SR")
    upload_id = uuid4().hex
    store = build_object_store(settings)
    pathname_prefix = store.pathname_for(f"source-packages/{upload_id}")
    now = utcnow()
    run = AgentSourceResearchRun(
        research_id=research_id,
        incident_id=incident.id,
        episode_id=episode.id,
        analysis_window_id=window.id,
        state=AgentSourceResearchState.QUEUED,
        cutoff_at=as_utc(window.window_end_at),
        location_hint=location_hint,
        requested_by=actor.actor_id,
        source_registry_version=settings.agent_research_source_registry_version,
        upload_id=upload_id,
        pathname_prefix=pathname_prefix,
        query_plan={
            "incident_name": incident.canonical_name,
            "location_hint": location_hint,
            "include_daily_municipal_updates": True,
            **dict(query_plan_overrides or {}),
        },
        result_summary=None,
        progress_percent=0,
        attempt=0,
        max_attempts=settings.agent_dispatch_max_attempts,
        remote_job_id=None,
        remote_status=None,
        lease_owner=None,
        lease_until=None,
        next_attempt_at=now,
        poll_count=0,
        payload_hash=None,
        output_hash=None,
        trace_id=trace_id,
        queued_at=now,
        submitted_at=None,
        last_polled_at=None,
        purge_after=now + timedelta(days=settings.agent_source_package_retention_days),
        started_at=None,
        completed_at=None,
        last_error_code=None,
        last_error_detail=None,
    )
    session.add(run)
    record_operator_audit(
        session,
        actor=actor,
        action="agent.source_research_queued",
        target_type="agent_source_research",
        target_id=research_id,
        reason="Model-directed public-source research queued for one historical day.",
        trace_id=trace_id,
        after={
            "fire_id": fire_id,
            "local_date": window.local_date.isoformat(),
            "cutoff_at": as_utc(window.window_end_at).isoformat(),
        },
    )
    from fire_viewer.services.agent_validation_campaigns import (
        refresh_campaign_day_review_state,
    )

    refresh_campaign_day_review_state(
        session,
        analysis_window_id=run.analysis_window_id,
    )
    session.commit()
    return _response(_load_run(session, research_id))


def get_source_research(session: Session, research_id: str) -> AgentSourceResearchResponse:
    return _response(_load_run(session, research_id))


def claim_next_source_research(
    session: Session,
    *,
    worker_id: str,
    settings: Settings,
    states: tuple[AgentSourceResearchState, ...] = _CLAIMABLE,
) -> int | None:
    if not settings.agent_research_enabled:
        return None
    now = utcnow()
    due_id = (
        select(AgentSourceResearchRun.id)
        .where(
            AgentSourceResearchRun.state.in_(states),
            or_(
                AgentSourceResearchRun.next_attempt_at.is_(None),
                AgentSourceResearchRun.next_attempt_at <= now,
            ),
            or_(
                AgentSourceResearchRun.lease_until.is_(None),
                AgentSourceResearchRun.lease_until <= now,
            ),
        )
        .order_by(AgentSourceResearchRun.next_attempt_at, AgentSourceResearchRun.id)
        .limit(1)
        .scalar_subquery()
    )
    claimed = session.execute(
        update(AgentSourceResearchRun)
        .where(AgentSourceResearchRun.id == due_id)
        .values(
            lease_owner=worker_id,
            lease_until=now + timedelta(seconds=settings.agent_dispatch_lease_seconds),
        )
        .returning(AgentSourceResearchRun.id)
    ).scalar_one_or_none()
    session.commit()
    return claimed


def _release(run: AgentSourceResearchRun) -> None:
    run.lease_owner = None
    run.lease_until = None


def _dead_letter(
    session: Session,
    run: AgentSourceResearchRun,
    *,
    worker_id: str,
    code: str,
    detail: str,
) -> None:
    run.state = AgentSourceResearchState.DEAD_LETTER
    run.last_error_code = code[:128]
    run.last_error_detail = detail[:1_000]
    run.completed_at = utcnow()
    run.next_attempt_at = None
    _release(run)
    record_operator_audit(
        session,
        actor=_system_actor(worker_id),
        action="agent.source_research_dead_lettered",
        target_type="agent_source_research",
        target_id=run.research_id,
        reason=run.last_error_detail,
        trace_id=run.trace_id,
        after={"state": run.state.value, "error_code": run.last_error_code},
    )
    from fire_viewer.services.agent_validation_campaigns import (
        refresh_campaign_day_review_state,
    )

    refresh_campaign_day_review_state(
        session,
        analysis_window_id=run.analysis_window_id,
    )
    session.commit()


def _worker_payload(
    run: AgentSourceResearchRun, *, settings: Settings, worker_id: str
) -> dict[str, object]:
    grant = create_source_blob_upload_grant(
        package_id=run.research_id,
        file_count=settings.agent_source_package_max_files,
        total_size_bytes=settings.agent_source_package_max_total_bytes,
        actor=_system_actor(worker_id),
        settings=settings,
        upload_id=run.upload_id,
    )
    base = str(settings.agent_media_proxy_base_url).rstrip("/")
    validated = WorkerResearchInputV1.model_validate(
        {
            "research_id": run.research_id,
            "analysis_window": {
                "analysis_id": run.analysis_window.analysis_id,
                "fire_id": run.incident.fire_id,
                "episode_id": run.episode.episode_id,
                "window_start_at": as_utc(run.analysis_window.window_start_at),
                "window_end_at": as_utc(run.analysis_window.window_end_at),
                "local_date": run.analysis_window.local_date,
                "timezone": run.analysis_window.timezone,
            },
            "incident_name": run.incident.canonical_name,
            "incident_reference": [run.incident.reference_lon, run.incident.reference_lat],
            "cutoff_at": as_utc(run.cutoff_at),
            "location_hint": run.location_hint,
            "source_registry_version": run.source_registry_version,
            "allowed_domains": sorted(settings.agent_research_allowed_domains),
            "source_policies": research_source_policy_payload(
                settings.agent_research_allowed_domains
            ),
            "search_templates": settings.agent_research_search_templates,
            "max_fetch_bytes": settings.agent_research_max_fetch_bytes,
            "request_timeout_seconds": settings.agent_research_request_timeout_seconds,
            "private_upload": {
                "pathname_prefix": grant.pathname_prefix,
                "upload_grant": grant.token,
                "token_endpoint": f"{base}/api/v1/admin/blob-upload-token",
                "resource_id": run.research_id,
                "maximum_file_size_bytes": settings.agent_source_package_max_file_bytes,
                "allowed_content_types": list(ALLOWED_SOURCE_CONTENT_TYPES),
            },
        }
    )
    return validated.model_dump(mode="json", exclude_none=True)


def _submit(
    session: Session,
    run: AgentSourceResearchRun,
    *,
    worker_id: str,
    settings: Settings,
    client: ResearchRunPodTransport,
) -> None:
    if run.attempt >= run.max_attempts:
        _dead_letter(
            session,
            run,
            worker_id=worker_id,
            code="agent_research_attempts_exhausted",
            detail="The research operation exhausted its submission attempts.",
        )
        return
    payload = _worker_payload(run, settings=settings, worker_id=worker_id)
    run.payload_hash = sha256_hex(payload)
    run.state = AgentSourceResearchState.SUBMITTING
    run.attempt += 1
    run.next_attempt_at = None
    session.commit()
    try:
        response = client.submit(payload)
        remote_job_id = response["id"]
        if not isinstance(remote_job_id, str) or not remote_job_id:
            raise ValueError("RunPod research submission has no job id")
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        _dead_letter(
            session,
            run,
            worker_id=worker_id,
            code="agent_research_submission_ambiguous",
            detail=f"RunPod research submission outcome is ambiguous: {exc}",
        )
        return
    now = utcnow()
    run.remote_job_id = remote_job_id
    run.remote_status = str(response.get("status") or "IN_QUEUE").upper()
    run.state = AgentSourceResearchState.RUNNING
    run.progress_percent = 1
    run.submitted_at = now
    run.started_at = now
    run.next_attempt_at = now + timedelta(seconds=settings.agent_poll_interval_seconds)
    _release(run)
    session.commit()


def _canonical_url(value: str) -> tuple[str, str]:
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold().rstrip(".")
    if parts.scheme.casefold() != "https" or not host or parts.username or parts.password:
        raise BadRequestError("research_candidate_url_invalid", "Candidate URL must use HTTPS.")
    port = parts.port
    authority = host if port in {None, 443} else f"{host}:{port}"
    path = parts.path or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(("https", authority, path, query, "")), host


def _allowed_domain(host: str, settings: Settings) -> bool:
    return any(
        host == domain.casefold() or host.endswith(f".{domain.casefold()}")
        for domain in settings.agent_research_allowed_domains
    )


def _create_external_batches(
    session: Session,
    *,
    run: AgentSourceResearchRun,
    accepted: list[AgentSourceCandidate],
    settings: Settings,
) -> list[str]:
    processable = [
        candidate
        for candidate in accepted
        if candidate.media_type is not None
        and candidate.media_type != AgentMediaType.SATELLITE_IMAGE
    ]
    batch_ids: list[str] = []
    for offset in range(0, len(processable), 32):
        chunk = processable[offset : offset + 32]
        batch = AgentMediaBatch(
            batch_id=new_prefixed_id("AB"),
            schema_version="2.0",
            batch_type=AgentBatchType.EXTERNAL_MEDIA,
            priority=AgentBatchPriority.SCHEDULED_COMBINED,
            state=AgentBatchState.DRAFT,
            incident_id=run.incident_id,
            episode_id=run.episode_id,
            analysis_window_id=run.analysis_window_id,
            reference_bundle_payload=None,
            idempotency_key=f"source-research:{run.research_id}:{offset // 32}",
            request_hash=hashlib.sha256(
                "\n".join(candidate.canonical_url_hash for candidate in chunk).encode()
            ).hexdigest(),
            trace_id=run.trace_id,
            deadline_at=None,
            purge_after=run.purge_after,
        )
        session.add(batch)
        batch_ids.append(batch.batch_id)
        for candidate in chunk:
            proxy_url = (
                create_private_media_url(
                    source_kind="source_research",
                    source_id=run.research_id,
                    item_id=candidate.candidate_id,
                    purge_after=run.purge_after,
                    settings=settings,
                )
                if candidate.object_uri
                else None
            )
            media_url = (
                proxy_url
                if candidate.media_type in {AgentMediaType.IMAGE, AgentMediaType.VIDEO}
                else None
            )
            captured_at = candidate.acquired_at or candidate.published_at
            source_policy_value = candidate.provenance_payload.get("source_policy")
            source_policy = source_policy_value if isinstance(source_policy_value, dict) else {}
            source_kind = source_policy.get("kind")
            source_confidence = source_policy.get("confidence_level")
            source_policy_domain = candidate.provenance_payload.get("source_policy_domain")
            claim_types = source_policy.get("claim_types")
            publication_policy = source_policy.get("publication_policy")
            media_item = AgentMediaItem(
                input_id=candidate.candidate_id,
                media_type=candidate.media_type,
                working_file_url=media_url,
                media_sha256=candidate.media_sha256,
                size_bytes=(
                    candidate.provenance_payload.get("size_bytes") if candidate.object_uri else None
                ),
                metadata_payload={
                    "provenance": {
                        "source_key": candidate.candidate_id,
                        "source_reference_url": candidate.canonical_url,
                        "license_identifier": candidate.license_identifier
                        or "UNVERIFIED_PRIVATE_ANALYSIS_ONLY",
                        "attribution": candidate.attribution,
                        "trust": (
                            "institutional"
                            if source_kind
                            in {
                                "authority",
                                "emergency_service",
                                "satellite",
                                "weather",
                                "air_quality",
                                "context",
                                "directory",
                            }
                            else "unverified"
                        ),
                        "source_registry_version": run.source_registry_version,
                        "source_policy_domain": (
                            source_policy_domain if isinstance(source_policy_domain, str) else None
                        ),
                        "source_kind": source_kind,
                        "source_confidence": source_confidence,
                        "publication_policy": publication_policy,
                        "claim_types": claim_types if isinstance(claim_types, list) else [],
                    },
                    "captured_at": as_utc(captured_at).isoformat() if captured_at else None,
                    "camera": None,
                    "satellite": None,
                    "source_research": {
                        "research_id": run.research_id,
                        "candidate_id": candidate.candidate_id,
                    },
                },
                processable_payload={
                    "frames": [],
                    "audio_url": (
                        proxy_url if candidate.media_type == AgentMediaType.AUDIO else None
                    ),
                    "article_text": (
                        candidate.excerpt
                        if candidate.media_type == AgentMediaType.ARTICLE
                        else None
                    ),
                },
                preprocessing_status="validated",
                purge_after=run.purge_after,
            )
            media_item.consent = AgentMediaConsent(
                basis=AgentConsentBasis.PUBLIC_SOURCE_ANALYSIS,
                state=AgentConsentState.GRANTED,
                scopes=["temporary_storage", "agent_analysis", "human_review"],
                terms_version="firewarning-public-source-private-analysis-v1",
                evidence_sha256=hashlib.sha256(
                    f"{candidate.canonical_url}\0{candidate.media_sha256 or ''}".encode()
                ).hexdigest(),
                subject_reference_hash=None,
                source_reference_url=candidate.canonical_url,
                license_identifier=candidate.license_identifier,
                granted_at=run.queued_at,
                expires_at=run.purge_after,
            )
            batch.items.append(media_item)
            session.flush()
            candidate.agent_media_item_id = media_item.id
    return batch_ids


def _persist_output(
    session: Session,
    run: AgentSourceResearchRun,
    *,
    raw_output: object,
    settings: Settings,
    worker_id: str,
) -> None:
    output = WorkerResearchOutputV1.model_validate(raw_output)
    if output.research_id != run.research_id:
        raise ValueError("research output id does not match its persisted operation")
    expected_revision = settings.agent_expected_model_revisions.get("source_research")
    if expected_revision and output.model_run.revision != expected_revision:
        raise ValueError("source research model revision does not match the pinned backend value")
    store = build_object_store(settings)
    existing_url_hashes = set(session.scalars(select(AgentSourceCandidate.canonical_url_hash)))
    existing_media_hashes = {
        digest for digest in session.scalars(select(AgentSourceCandidate.media_sha256)) if digest
    }
    existing_media_hashes.update(session.scalars(select(AgentSourcePackageItem.sha256)))
    accepted: list[AgentSourceCandidate] = []
    seen_run_urls: set[str] = set()
    for candidate_output in output.candidates:
        canonical_url, host = _canonical_url(str(candidate_output.canonical_url))
        if not _allowed_domain(host, settings):
            raise ValueError(f"research candidate domain is not allowed: {host}")
        url_hash = hashlib.sha256(canonical_url.encode()).hexdigest()
        if url_hash in seen_run_urls:
            continue
        seen_run_urls.add(url_hash)
        timestamps = [
            as_utc(value)
            for value in (candidate_output.published_at, candidate_output.acquired_at)
            if value is not None
        ]
        cutoff_eligible = bool(timestamps) and all(
            value <= as_utc(run.cutoff_at) for value in timestamps
        )
        object_uri = None
        media_hash = candidate_output.media_sha256
        size_bytes = candidate_output.size_bytes
        if candidate_output.blob_pathname is not None:
            expected_prefix = f"{run.pathname_prefix}/"
            if not candidate_output.blob_pathname.startswith(expected_prefix):
                raise ValueError("research media object is outside the granted private prefix")
            object_uri = store.uri_for_pathname(candidate_output.blob_pathname)
            content = store.read_bytes(object_uri)
            authoritative_hash = hashlib.sha256(content).hexdigest()
            if authoritative_hash != media_hash or len(content) != size_bytes:
                raise ValueError("research media hash or size does not match private storage")
        elif candidate_output.excerpt:
            media_hash = hashlib.sha256(candidate_output.excerpt.encode()).hexdigest()
        duplicate = url_hash in existing_url_hashes or (
            media_hash is not None and media_hash in existing_media_hashes
        )
        state = (
            AgentSourceCandidateState.DUPLICATE
            if duplicate
            else (
                AgentSourceCandidateState.ACCEPTED
                if cutoff_eligible
                else AgentSourceCandidateState.REJECTED
            )
        )
        candidate = AgentSourceCandidate(
            candidate_id=candidate_output.candidate_id,
            research_run_id=run.id,
            agent_media_item_id=None,
            state=state,
            canonical_url=canonical_url,
            canonical_url_hash=url_hash,
            source_domain=host,
            title=candidate_output.title,
            published_at=candidate_output.published_at,
            acquired_at=candidate_output.acquired_at,
            media_type=candidate_output.media_type,
            media_sha256=media_hash,
            object_uri=object_uri,
            excerpt=candidate_output.excerpt,
            license_identifier=candidate_output.license_identifier,
            attribution=candidate_output.attribution,
            provenance_payload={
                **candidate_output.provenance,
                "size_bytes": size_bytes,
                "worker_candidate_id": candidate_output.candidate_id,
            },
            cutoff_eligible=cutoff_eligible,
            duplicate_of_candidate_id=None,
        )
        run.candidates.append(candidate)
        existing_url_hashes.add(url_hash)
        if media_hash:
            existing_media_hashes.add(media_hash)
        if state == AgentSourceCandidateState.ACCEPTED:
            accepted.append(candidate)
    session.flush()
    batch_ids = _create_external_batches(
        session,
        run=run,
        accepted=accepted,
        settings=settings,
    )
    run.result_summary = {
        "queries": output.queries,
        "candidate_count": len(run.candidates),
        "accepted_count": len(accepted),
        "cutoff_at": as_utc(run.cutoff_at).isoformat(),
        "human_review_required": True,
        "publication_authorized": False,
        "analysis_batch_ids": batch_ids,
    }
    run.output_hash = sha256_hex(output)
    run.progress_percent = 95
    session.commit()

    # The public research result is only the discovery stage. Accepted media must
    # traverse the same persisted dispatcher as every other media batch before
    # the operation can be considered complete.
    from fire_viewer.services.agent_batches import enqueue_agent_batch

    enqueue_errors: list[str] = []
    enqueued_batch_ids: list[str] = []
    for batch_id in batch_ids:
        try:
            enqueue_agent_batch(
                session,
                batch_id=batch_id,
                actor=_system_actor(worker_id),
                trace_id=run.trace_id,
                settings=settings,
            )
            enqueued_batch_ids.append(batch_id)
        except ConflictError as exc:
            enqueue_errors.append(f"{batch_id}:{exc.code}")

    run.progress_percent = 100
    run.state = {
        "succeeded": AgentSourceResearchState.SUCCEEDED,
        "partial_failure": AgentSourceResearchState.PARTIAL_FAILURE,
        "failed": AgentSourceResearchState.FAILED,
    }[output.status]
    if enqueue_errors and run.state == AgentSourceResearchState.SUCCEEDED:
        run.state = AgentSourceResearchState.PARTIAL_FAILURE
    run.completed_at = utcnow()
    run.next_attempt_at = None
    run.last_error_code = "agent_research_media_enqueue_failed" if enqueue_errors else None
    run.last_error_detail = "; ".join(enqueue_errors)[:1_000] or None
    run.result_summary = {
        **(run.result_summary or {}),
        "enqueued_analysis_batch_ids": enqueued_batch_ids,
        "enqueue_error_count": len(enqueue_errors),
    }
    _release(run)
    record_operator_audit(
        session,
        actor=_system_actor(worker_id),
        action="agent.source_research_completed",
        target_type="agent_source_research",
        target_id=run.research_id,
        reason="Research candidates persisted privately with temporal and duplicate checks.",
        trace_id=run.trace_id,
        after={
            "state": run.state.value,
            "candidates": len(run.candidates),
            "accepted": len(accepted),
            "analysis_batches_enqueued": len(enqueued_batch_ids),
            "publication_authorized": False,
        },
    )
    from fire_viewer.services.agent_validation_campaigns import (
        refresh_campaign_day_review_state,
    )

    refresh_campaign_day_review_state(
        session,
        analysis_window_id=run.analysis_window_id,
    )
    session.commit()


def _poll(
    session: Session,
    run: AgentSourceResearchRun,
    *,
    worker_id: str,
    settings: Settings,
    client: ResearchRunPodTransport,
) -> None:
    if run.remote_job_id is None:
        _dead_letter(
            session,
            run,
            worker_id=worker_id,
            code="agent_research_remote_id_missing",
            detail="A running research operation has no remote job identifier.",
        )
        return
    try:
        response = client.status(run.remote_job_id)
    except httpx.HTTPError as exc:
        run.last_error_code = "agent_research_poll_failed"
        run.last_error_detail = str(exc)[:1_000]
        run.next_attempt_at = utcnow() + timedelta(seconds=settings.agent_poll_interval_seconds)
        _release(run)
        session.commit()
        return
    now = utcnow()
    remote_status = str(response.get("status") or "").upper()
    run.poll_count += 1
    run.last_polled_at = now
    run.remote_status = remote_status
    if remote_status in _REMOTE_ACTIVE:
        run.progress_percent = min(95, max(run.progress_percent, 5 + run.poll_count))
        run.next_attempt_at = now + timedelta(seconds=settings.agent_poll_interval_seconds)
        _release(run)
        session.commit()
        return
    if remote_status == "COMPLETED":
        try:
            _persist_output(
                session,
                run,
                raw_output=response.get("output"),
                settings=settings,
                worker_id=worker_id,
            )
        except (ValidationError, ValueError, BadRequestError, ObjectStorageError) as exc:
            _dead_letter(
                session,
                run,
                worker_id=worker_id,
                code="agent_research_output_invalid",
                detail=f"Research output failed validation: {exc}",
            )
        return
    run.state = (
        AgentSourceResearchState.CANCELLED
        if remote_status == "CANCELLED"
        else AgentSourceResearchState.FAILED
    )
    run.last_error_code = f"agent_research_remote_{remote_status.casefold() or 'unknown'}"
    run.last_error_detail = "The remote research job ended without a completed output."
    run.completed_at = now
    run.next_attempt_at = None
    _release(run)
    from fire_viewer.services.agent_validation_campaigns import (
        refresh_campaign_day_review_state,
    )

    refresh_campaign_day_review_state(
        session,
        analysis_window_id=run.analysis_window_id,
    )
    session.commit()


def process_claimed_source_research(
    session: Session,
    *,
    research_row_id: int,
    worker_id: str,
    settings: Settings,
    client: ResearchRunPodTransport,
) -> None:
    run = session.execute(
        select(AgentSourceResearchRun)
        .where(AgentSourceResearchRun.id == research_row_id)
        .options(
            selectinload(AgentSourceResearchRun.incident),
            selectinload(AgentSourceResearchRun.episode),
            selectinload(AgentSourceResearchRun.analysis_window),
            selectinload(AgentSourceResearchRun.candidates),
        )
    ).scalar_one()
    if run.lease_owner != worker_id:
        return
    if run.state == AgentSourceResearchState.SUBMITTING:
        _dead_letter(
            session,
            run,
            worker_id=worker_id,
            code="agent_research_stale_submitting",
            detail="A previous dispatcher stopped after crossing the submission fence.",
        )
    elif run.state == AgentSourceResearchState.QUEUED:
        _submit(session, run, worker_id=worker_id, settings=settings, client=client)
    elif run.state == AgentSourceResearchState.CANCEL_REQUESTED:
        if run.remote_job_id:
            with suppress(httpx.HTTPError):
                client.cancel(run.remote_job_id)
        run.state = AgentSourceResearchState.CANCELLED
        run.completed_at = utcnow()
        run.next_attempt_at = None
        _release(run)
        from fire_viewer.services.agent_validation_campaigns import (
            refresh_campaign_day_review_state,
        )

        refresh_campaign_day_review_state(
            session,
            analysis_window_id=run.analysis_window_id,
        )
        session.commit()
    else:
        _poll(session, run, worker_id=worker_id, settings=settings, client=client)


def run_source_research_dispatcher_once(
    factory: sessionmaker[Session],
    *,
    worker_id: str,
    settings: Settings,
    client: ResearchRunPodTransport,
) -> bool:
    with factory() as session:
        row_id = claim_next_source_research(
            session,
            worker_id=worker_id,
            settings=settings,
        )
        if row_id is None:
            return False
        process_claimed_source_research(
            session,
            research_row_id=row_id,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
        return True
