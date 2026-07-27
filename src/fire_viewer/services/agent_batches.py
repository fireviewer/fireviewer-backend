from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, ensure_utc, utcnow
from fire_viewer.db.models import (
    AgentAnalysisWindow,
    AgentDispatch,
    AgentFactProposal,
    AgentMediaBatch,
    AgentMediaConsent,
    AgentMediaItem,
    AgentSituationReportFact,
    AgentSituationReportRevision,
    AgentSpatialProposal,
    Episode,
    IncidentSeries,
)
from fire_viewer.domain.agent_schemas import (
    AgentBatchCreateOutcome,
    AgentBatchCreatePayload,
    AgentBatchCreateRequestV2,
    AgentBatchItemResponse,
    AgentBatchResponse,
    AgentConsentWithdrawResponse,
    AgentDispatchResponse,
    AgentMediaItemInputV2,
    WorkerBatchItem,
    WorkerBatchItemV2,
    WorkerInput,
    WorkerInputV2,
)
from fire_viewer.domain.enums import (
    AgentAnalysisState,
    AgentBatchState,
    AgentConsentState,
    AgentDispatchState,
    AgentProposalReviewState,
    AgentReportReviewState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.hashing import json_safe, sha256_hex
from fire_viewer.services.common import record_operator_audit


@dataclass(frozen=True)
class EnqueueOutcome:
    replayed: bool
    batch: AgentBatchResponse


def _validate_https_host(url: object, allowed_hosts: list[str]) -> None:
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in set(allowed_hosts):
        raise BadRequestError(
            "agent_media_url_forbidden",
            "Processable media URLs must use HTTPS and an explicitly allowed private host.",
        )
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise BadRequestError(
            "agent_media_url_forbidden",
            "Processable media URLs cannot contain credentials or fragments.",
        )


def _validate_https_reference(url: object) -> None:
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or not parsed.hostname:
        raise BadRequestError(
            "agent_consent_reference_forbidden",
            "Consent source references must use HTTPS.",
        )
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise BadRequestError(
            "agent_consent_reference_forbidden",
            "Consent source references cannot contain credentials or fragments.",
        )


def _load_batch(session: Session, batch_id: str) -> AgentMediaBatch:
    batch = session.execute(
        select(AgentMediaBatch)
        .where(AgentMediaBatch.batch_id == batch_id)
        .options(
            selectinload(AgentMediaBatch.items).selectinload(AgentMediaItem.consent),
            selectinload(AgentMediaBatch.dispatch),
            selectinload(AgentMediaBatch.analysis_window),
        )
    ).scalar_one_or_none()
    if batch is None:
        raise NotFoundError("agent_media_batch", batch_id)
    return batch


def _batch_response(batch: AgentMediaBatch) -> AgentBatchResponse:
    dispatch = batch.dispatch
    return AgentBatchResponse(
        batch_id=batch.batch_id,
        fire_id=batch.incident.fire_id if batch.incident else None,
        episode_id=batch.episode.episode_id if batch.episode else None,
        analysis_id=batch.analysis_window.analysis_id if batch.analysis_window else None,
        schema_version=batch.schema_version,
        batch_type=batch.batch_type,
        priority=batch.priority,
        state=batch.state,
        payload_hash=batch.payload_hash,
        deadline_at=as_utc(batch.deadline_at) if batch.deadline_at else None,
        purge_after=as_utc(batch.purge_after),
        submitted_at=as_utc(batch.submitted_at) if batch.submitted_at else None,
        completed_at=as_utc(batch.completed_at) if batch.completed_at else None,
        items=[
            AgentBatchItemResponse(
                input_id=item.input_id,
                media_type=item.media_type,
                media_sha256=item.media_sha256,
                size_bytes=item.size_bytes,
                consent_state=item.consent.state,
                purge_after=as_utc(item.purge_after),
                purged_at=as_utc(item.purged_at) if item.purged_at else None,
            )
            for item in batch.items
        ],
        dispatch=(
            AgentDispatchResponse(
                dispatch_id=dispatch.dispatch_id,
                state=dispatch.state,
                payload_hash=dispatch.payload_hash,
                attempt=dispatch.attempt,
                poll_count=dispatch.poll_count,
                remote_job_id=dispatch.remote_job_id,
                remote_status=dispatch.remote_status,
                next_attempt_at=(
                    as_utc(dispatch.next_attempt_at) if dispatch.next_attempt_at else None
                ),
                submitted_at=as_utc(dispatch.submitted_at) if dispatch.submitted_at else None,
                completed_at=as_utc(dispatch.completed_at) if dispatch.completed_at else None,
                last_error_code=dispatch.last_error_code,
            )
            if dispatch is not None
            else None
        ),
    )


def _ensure_analysis_window(
    session: Session,
    *,
    payload: AgentBatchCreateRequestV2,
    incident: IncidentSeries,
    episode: Episode,
) -> AgentAnalysisWindow:
    requested = payload.analysis_window
    candidates = list(
        session.scalars(
            select(AgentAnalysisWindow).where(
                or_(
                    AgentAnalysisWindow.analysis_id == requested.analysis_id,
                    and_(
                        AgentAnalysisWindow.incident_id == incident.id,
                        AgentAnalysisWindow.episode_id == episode.id,
                        AgentAnalysisWindow.local_date == requested.local_date,
                    ),
                )
            )
        )
    )
    if len(candidates) > 1:
        raise ConflictError(
            "agent_analysis_window_conflict",
            "analysis_id and incident local day identify different analysis windows.",
        )
    if candidates:
        existing = candidates[0]
        same_window = (
            existing.analysis_id == requested.analysis_id
            and existing.incident_id == incident.id
            and existing.episode_id == episode.id
            and existing.local_date == requested.local_date
            and existing.timezone == requested.timezone
            and as_utc(existing.window_start_at) == ensure_utc(requested.window_start_at)
            and as_utc(existing.window_end_at) == ensure_utc(requested.window_end_at)
        )
        if not same_window:
            raise ConflictError(
                "agent_analysis_window_conflict",
                "The analysis window identifier or local day already exists with other bounds.",
            )
        return existing

    analysis_window = AgentAnalysisWindow(
        analysis_id=requested.analysis_id,
        incident_id=incident.id,
        episode_id=episode.id,
        window_start_at=ensure_utc(requested.window_start_at),
        window_end_at=ensure_utc(requested.window_end_at),
        local_date=requested.local_date,
        timezone=requested.timezone,
        state=AgentAnalysisState.COLLECTING,
        version=1,
    )
    session.add(analysis_window)
    session.flush()
    return analysis_window


def create_agent_batch(
    session: Session,
    *,
    payload: AgentBatchCreatePayload,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AgentBatchCreateOutcome:
    now = utcnow()
    try:
        purge_after = ensure_utc(payload.purge_after)
        deadline_at = ensure_utc(payload.deadline_at) if payload.deadline_at else None
    except ValueError as exc:
        raise BadRequestError("agent_datetime_timezone_required", str(exc)) from exc
    if purge_after <= now:
        raise BadRequestError("agent_purge_in_past", "purge_after must be in the future")
    maximum_purge = now + timedelta(days=settings.unpublished_model_retention_days)
    if purge_after > maximum_purge:
        raise BadRequestError(
            "agent_purge_too_late",
            "purge_after exceeds the private unpublished-media retention policy.",
        )
    if deadline_at is not None and deadline_at >= purge_after:
        raise BadRequestError("agent_deadline_after_purge", "deadline_at must precede purge_after")
    if deadline_at is not None and deadline_at <= now:
        raise BadRequestError("agent_deadline_expired", "deadline_at must be in the future")
    if payload.priority.value == "user_deadline" and deadline_at is None:
        raise BadRequestError(
            "agent_deadline_required", "user_deadline batches require deadline_at"
        )

    if isinstance(payload, AgentBatchCreateRequestV2):
        v2_payload: AgentBatchCreateRequestV2 | None = payload
        fire_id: str | None = payload.analysis_window.fire_id
        episode_id: str | None = payload.analysis_window.episode_id
    else:
        v2_payload = None
        fire_id = payload.fire_id
        episode_id = payload.episode_id
    incident = None
    episode = None
    if fire_id is not None and episode_id is not None:
        incident = session.execute(
            select(IncidentSeries).where(IncidentSeries.fire_id == fire_id)
        ).scalar_one_or_none()
        if incident is None:
            raise NotFoundError("incident", fire_id)
        episode = session.execute(
            select(Episode).where(
                Episode.incident_id == incident.id,
                Episode.episode_id == episode_id,
            )
        ).scalar_one_or_none()
        if episode is None:
            raise NotFoundError("episode", episode_id)

    for request_item in payload.items:
        for url in [
            request_item.working_file_url,
            request_item.audio_url,
            *(frame.working_file_url for frame in request_item.frames),
        ]:
            if url is not None:
                _validate_https_host(url, settings.agent_media_allowed_hosts)
        if request_item.consent.source_reference_url is not None:
            _validate_https_reference(request_item.consent.source_reference_url)
        if (
            isinstance(request_item, AgentMediaItemInputV2)
            and request_item.provenance.source_reference_url is not None
        ):
            _validate_https_reference(request_item.provenance.source_reference_url)
    if v2_payload is not None and v2_payload.reference_bundle is not None:
        for asset in v2_payload.reference_bundle.assets:
            _validate_https_host(asset.working_file_url, settings.agent_media_allowed_hosts)

    request_hash = sha256_hex(payload)
    existing = (
        session.execute(
            select(AgentMediaBatch)
            .where(
                or_(
                    AgentMediaBatch.batch_id == payload.batch_id,
                    AgentMediaBatch.idempotency_key == idempotency_key,
                )
            )
            .options(
                selectinload(AgentMediaBatch.items).selectinload(AgentMediaItem.consent),
                selectinload(AgentMediaBatch.dispatch),
                selectinload(AgentMediaBatch.analysis_window),
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if (
            existing.batch_id != payload.batch_id
            or existing.idempotency_key != idempotency_key
            or existing.request_hash != request_hash
        ):
            raise ConflictError(
                "agent_batch_idempotency_conflict",
                "batch_id or Idempotency-Key was already used with a different payload.",
            )
        return AgentBatchCreateOutcome(replayed=True, batch=_batch_response(existing))

    analysis_window = None
    if v2_payload is not None:
        if incident is None or episode is None:
            raise AssertionError("v2 agent batches require an incident episode")
        analysis_window = _ensure_analysis_window(
            session,
            payload=v2_payload,
            incident=incident,
            episode=episode,
        )

    batch = AgentMediaBatch(
        batch_id=payload.batch_id,
        schema_version=payload.schema_version,
        batch_type=payload.batch_type,
        priority=payload.priority,
        state=AgentBatchState.DRAFT,
        incident_id=incident.id if incident else None,
        episode_id=episode.id if episode else None,
        analysis_window_id=analysis_window.id if analysis_window else None,
        reference_bundle_payload=(
            json_safe(v2_payload.reference_bundle)
            if v2_payload is not None and v2_payload.reference_bundle
            else None
        ),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        trace_id=trace_id,
        deadline_at=deadline_at,
        purge_after=purge_after,
    )
    session.add(batch)
    for source_item in payload.items:
        consent_input = source_item.consent
        try:
            granted_at = ensure_utc(consent_input.granted_at)
            expires_at = ensure_utc(consent_input.expires_at) if consent_input.expires_at else None
        except ValueError as exc:
            raise BadRequestError("agent_datetime_timezone_required", str(exc)) from exc
        if granted_at > now + timedelta(seconds=settings.max_clock_skew_seconds):
            raise BadRequestError(
                "agent_consent_granted_in_future",
                f"Consent grant time is in the future for {source_item.input_id}.",
            )
        if expires_at is not None and expires_at <= now:
            raise BadRequestError(
                "agent_consent_expired",
                f"Consent is already expired for {source_item.input_id}.",
            )
        if isinstance(source_item, AgentMediaItemInputV2):
            metadata_payload = {
                "provenance": json_safe(source_item.provenance),
                "captured_at": json_safe(source_item.captured_at),
                "camera": json_safe(source_item.camera) if source_item.camera else None,
                "satellite": json_safe(source_item.satellite) if source_item.satellite else None,
            }
        else:
            metadata_payload = json_safe(source_item.metadata)
        media_item = AgentMediaItem(
            input_id=source_item.input_id,
            media_type=source_item.media_type,
            working_file_url=(
                str(source_item.working_file_url) if source_item.working_file_url else None
            ),
            media_sha256=source_item.media_sha256,
            size_bytes=source_item.size_bytes,
            metadata_payload=metadata_payload,
            processable_payload={
                "frames": [json_safe(frame) for frame in source_item.frames],
                "audio_url": str(source_item.audio_url) if source_item.audio_url else None,
                "article_text": source_item.article_text,
            },
            preprocessing_status="validated",
            purge_after=purge_after,
        )
        media_item.consent = AgentMediaConsent(
            basis=consent_input.basis,
            state=AgentConsentState.GRANTED,
            scopes=list(consent_input.scopes),
            terms_version=consent_input.terms_version,
            evidence_sha256=consent_input.evidence_sha256,
            subject_reference_hash=consent_input.subject_reference_hash,
            source_reference_url=(
                str(consent_input.source_reference_url)
                if consent_input.source_reference_url
                else None
            ),
            license_identifier=consent_input.license_identifier,
            granted_at=granted_at,
            expires_at=expires_at,
        )
        batch.items.append(media_item)
    record_operator_audit(
        session,
        actor=actor,
        action="agent.batch_created",
        target_type="agent_media_batch",
        target_id=batch.batch_id,
        reason="Private media batch persisted with consent evidence.",
        trace_id=trace_id,
        after={"state": batch.state.value, "request_hash": request_hash, "items": len(batch.items)},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = (
            session.execute(
                select(AgentMediaBatch)
                .where(
                    or_(
                        AgentMediaBatch.batch_id == payload.batch_id,
                        AgentMediaBatch.idempotency_key == idempotency_key,
                    )
                )
                .options(
                    selectinload(AgentMediaBatch.items).selectinload(AgentMediaItem.consent),
                    selectinload(AgentMediaBatch.dispatch),
                    selectinload(AgentMediaBatch.analysis_window),
                )
            )
            .scalars()
            .first()
        )
        if (
            concurrent is not None
            and concurrent.batch_id == payload.batch_id
            and concurrent.idempotency_key == idempotency_key
            and concurrent.request_hash == request_hash
        ):
            return AgentBatchCreateOutcome(replayed=True, batch=_batch_response(concurrent))
        if concurrent is not None:
            raise ConflictError(
                "agent_batch_idempotency_conflict",
                "batch_id or Idempotency-Key was concurrently used with a different payload.",
            ) from exc
        raise
    return AgentBatchCreateOutcome(
        replayed=False, batch=_batch_response(_load_batch(session, batch.batch_id))
    )


def _worker_payload(batch: AgentMediaBatch) -> dict[str, object]:
    if batch.schema_version == "2.0":
        analysis_window = batch.analysis_window
        incident = batch.incident
        episode = batch.episode
        if analysis_window is None or incident is None or episode is None:
            raise ConflictError(
                "agent_analysis_window_missing",
                "A v2 batch cannot be dispatched without its persisted analysis window.",
            )
        v2_items: list[WorkerBatchItemV2] = []
        for item in batch.items:
            processable = item.processable_payload
            metadata = item.metadata_payload
            v2_items.append(
                WorkerBatchItemV2.model_validate(
                    {
                        "input_id": item.input_id,
                        "media_type": item.media_type.value,
                        "working_file_url": item.working_file_url,
                        "provenance": metadata.get("provenance"),
                        "captured_at": metadata.get("captured_at"),
                        "camera": metadata.get("camera"),
                        "satellite": metadata.get("satellite"),
                        "frames": processable.get("frames", []),
                        "audio_url": processable.get("audio_url"),
                        "article_text": processable.get("article_text"),
                    }
                )
            )
        worker_input_v2 = WorkerInputV2.model_validate(
            {
                "batch_id": batch.batch_id,
                "batch_type": batch.batch_type.value,
                "priority": batch.priority.value,
                "analysis_window": {
                    "analysis_id": analysis_window.analysis_id,
                    "fire_id": incident.fire_id,
                    "episode_id": episode.episode_id,
                    "window_start_at": as_utc(analysis_window.window_start_at),
                    "window_end_at": as_utc(analysis_window.window_end_at),
                    "local_date": analysis_window.local_date,
                    "timezone": analysis_window.timezone,
                },
                "deadline_at": as_utc(batch.deadline_at) if batch.deadline_at else None,
                "reference_bundle": batch.reference_bundle_payload,
                "items": v2_items,
            }
        )
        return worker_input_v2.model_dump(mode="json", exclude_none=True)

    items: list[WorkerBatchItem] = []
    for item in batch.items:
        processable = item.processable_payload
        items.append(
            WorkerBatchItem.model_validate(
                {
                    "input_id": item.input_id,
                    "media_type": item.media_type.value,
                    "working_file_url": item.working_file_url,
                    "metadata": item.metadata_payload,
                    "frames": processable.get("frames", []),
                    "audio_url": processable.get("audio_url"),
                    "article_text": processable.get("article_text"),
                }
            )
        )
    worker_input = WorkerInput(
        batch_id=batch.batch_id,
        batch_type=batch.batch_type,
        priority=batch.priority,
        deadline_at=as_utc(batch.deadline_at) if batch.deadline_at else None,
        items=items,
    )
    return worker_input.model_dump(mode="json", exclude_none=True)


def enqueue_agent_batch(
    session: Session,
    *,
    batch_id: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> EnqueueOutcome:
    batch = _load_batch(session, batch_id)
    if batch.dispatch is not None:
        if batch.payload_hash == batch.dispatch.payload_hash:
            return EnqueueOutcome(replayed=True, batch=_batch_response(batch))
        raise ConflictError(
            "agent_dispatch_payload_conflict",
            "The batch already has a dispatch with a different payload hash.",
        )
    if batch.state != AgentBatchState.DRAFT:
        raise ConflictError("agent_batch_not_draft", "Only a DRAFT media batch can be enqueued.")
    now = utcnow()
    if batch.deadline_at is not None and as_utc(batch.deadline_at) <= now:
        raise ConflictError("agent_batch_deadline_expired", "The media batch deadline has expired.")
    for item in batch.items:
        consent = item.consent
        if consent.state != AgentConsentState.GRANTED:
            raise ConflictError(
                "agent_consent_not_granted", f"Consent is not active for {item.input_id}."
            )
        if consent.expires_at is not None and as_utc(consent.expires_at) <= now:
            consent.state = AgentConsentState.EXPIRED
            session.commit()
            raise ConflictError("agent_consent_expired", f"Consent expired for {item.input_id}.")
        if not {"temporary_storage", "agent_analysis", "human_review"}.issubset(
            set(consent.scopes)
        ):
            raise ConflictError(
                "agent_consent_scope_invalid", f"Consent scope is incomplete for {item.input_id}."
            )

    worker_payload = _worker_payload(batch)
    payload_hash = sha256_hex(worker_payload)
    batch.payload_hash = payload_hash
    batch.state = AgentBatchState.QUEUED
    batch.submitted_at = now
    dispatch = AgentDispatch(
        dispatch_id=new_prefixed_id("AD"),
        batch=batch,
        state=AgentDispatchState.QUEUED,
        payload=worker_payload,
        payload_hash=payload_hash,
        expected_models=dict(settings.agent_expected_model_revisions),
        attempt=0,
        max_attempts=settings.agent_dispatch_max_attempts,
        poll_count=0,
        next_attempt_at=now,
        deadline_at=batch.deadline_at,
    )
    session.add(dispatch)
    record_operator_audit(
        session,
        actor=actor,
        action="agent.batch_enqueued",
        target_type="agent_media_batch",
        target_id=batch.batch_id,
        reason="Validated private batch queued for the dedicated RunPod dispatcher.",
        trace_id=trace_id,
        before={"state": AgentBatchState.DRAFT.value},
        after={"state": batch.state.value, "payload_hash": payload_hash},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        concurrent = _load_batch(session, batch_id)
        if (
            concurrent.dispatch is not None
            and concurrent.payload_hash == concurrent.dispatch.payload_hash == payload_hash
        ):
            return EnqueueOutcome(replayed=True, batch=_batch_response(concurrent))
        raise ConflictError(
            "agent_dispatch_payload_conflict",
            "A concurrent enqueue created a dispatch with a different payload hash.",
        ) from exc
    return EnqueueOutcome(replayed=False, batch=_batch_response(_load_batch(session, batch_id)))


def get_agent_batch(session: Session, batch_id: str) -> AgentBatchResponse:
    return _batch_response(_load_batch(session, batch_id))


def withdraw_agent_consent(
    session: Session,
    *,
    batch_id: str,
    input_id: str,
    reason: str,
    actor: Actor,
    trace_id: str,
) -> AgentConsentWithdrawResponse:
    batch = _load_batch(session, batch_id)
    item = next((candidate for candidate in batch.items if candidate.input_id == input_id), None)
    if item is None:
        raise NotFoundError("agent_media_item", f"{batch_id}/{input_id}")
    now = utcnow()
    consent = item.consent
    if consent.state != AgentConsentState.WITHDRAWN:
        consent.state = AgentConsentState.WITHDRAWN
        consent.withdrawn_at = now
        consent.withdrawal_reason = reason
    item.purge_after = now
    batch.purge_after = now
    invalidated_facts = list(
        session.scalars(
            select(AgentFactProposal).where(
                AgentFactProposal.source_media_item_id == item.id,
                AgentFactProposal.review_state != AgentProposalReviewState.INVALIDATED,
            )
        )
    )
    invalidated_proposals = list(
        session.scalars(
            select(AgentSpatialProposal).where(
                AgentSpatialProposal.source_media_item_id == item.id,
                AgentSpatialProposal.review_state != AgentProposalReviewState.INVALIDATED,
            )
        )
    )
    for fact in invalidated_facts:
        fact.review_state = AgentProposalReviewState.INVALIDATED
        fact.reviewed_by = actor.actor_id
        fact.reviewed_at = now
        fact.review_reason = reason
    for spatial_proposal in invalidated_proposals:
        spatial_proposal.review_state = AgentProposalReviewState.INVALIDATED
        spatial_proposal.reviewed_by = actor.actor_id
        spatial_proposal.reviewed_at = now
        spatial_proposal.review_reason = reason

    fact_row_ids = [fact.id for fact in invalidated_facts]
    invalidated_reports: list[AgentSituationReportRevision] = []
    if fact_row_ids:
        invalidated_reports = list(
            session.scalars(
                select(AgentSituationReportRevision)
                .join(
                    AgentSituationReportFact,
                    AgentSituationReportFact.report_id == AgentSituationReportRevision.id,
                )
                .where(
                    AgentSituationReportFact.fact_id.in_(fact_row_ids),
                    AgentSituationReportRevision.review_state != AgentReportReviewState.INVALIDATED,
                )
                .distinct()
            )
        )
        for report in invalidated_reports:
            report.review_state = AgentReportReviewState.INVALIDATED
            report.reviewed_by = actor.actor_id
            report.reviewed_at = now
            report.review_reason = reason
    dispatch = batch.dispatch
    terminal_dispatch = {
        AgentDispatchState.SUCCEEDED,
        AgentDispatchState.PARTIAL_FAILURE,
        AgentDispatchState.FAILED,
        AgentDispatchState.DEAD_LETTER,
        AgentDispatchState.CANCELLED,
    }
    if dispatch is None:
        batch.state = AgentBatchState.CANCELLED
        batch.cancelled_at = now
    elif dispatch.state not in terminal_dispatch:
        dispatch.state = AgentDispatchState.CANCEL_REQUESTED
        dispatch.next_attempt_at = now
        batch.state = AgentBatchState.CANCEL_REQUESTED
    record_operator_audit(
        session,
        actor=actor,
        action="agent.consent_withdrawn",
        target_type="agent_media_item",
        target_id=f"{batch_id}/{input_id}",
        reason=reason,
        trace_id=trace_id,
        after={
            "consent_state": consent.state.value,
            "batch_state": batch.state.value,
            "purge_after": now,
            "invalidated_spatial_proposals": len(invalidated_proposals),
            "invalidated_fact_proposals": len(invalidated_facts),
            "invalidated_reports": len(invalidated_reports),
        },
    )
    session.commit()
    refreshed = _load_batch(session, batch_id)
    return AgentConsentWithdrawResponse(
        batch_id=batch_id,
        input_id=input_id,
        consent_state=AgentConsentState.WITHDRAWN,
        batch_state=refreshed.state,
        dispatch_state=refreshed.dispatch.state if refreshed.dispatch else None,
    )
