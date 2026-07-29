"""Single-purpose RunPod dispatcher for private agent media batches.

Submission is deliberately at-most-once: ``SUBMITTING`` is committed before the
external POST. An interrupted or ambiguous submission is dead-lettered instead
of being submitted again.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from pydantic import ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentDeadLetter,
    AgentDispatch,
    AgentMediaBatch,
    AgentMediaItem,
    AgentModelRun,
    AgentReviewTask,
    AgentSourceResearchRun,
    IncidentSpatialMarker,
)
from fire_viewer.domain.agent_schemas import (
    WorkerItemResult,
    WorkerModelRun,
    WorkerOutput,
    WorkerOutputV2,
)
from fire_viewer.domain.enums import (
    ActorType,
    AgentBatchState,
    AgentDeadLetterState,
    AgentDispatchState,
    AgentModelRunState,
    AgentReviewState,
    AgentSourceResearchState,
    IncidentMarkerReviewState,
)
from fire_viewer.services.agent_intelligence import (
    persist_worker_output_v2,
    validate_worker_output_v2,
)
from fire_viewer.services.agent_source_research import (
    claim_next_source_research,
    process_claimed_source_research,
)
from fire_viewer.services.agent_validation_campaigns import (
    refresh_campaign_day_review_state,
)
from fire_viewer.services.common import record_operator_audit

ACTIVE_REMOTE_STATES = frozenset({"IN_QUEUE", "IN_PROGRESS", "RUNNING"})
TERMINAL_REMOTE_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})
CLAIMABLE_STATES = (
    AgentDispatchState.QUEUED,
    AgentDispatchState.SUBMITTING,
    AgentDispatchState.AWAITING_REMOTE,
    AgentDispatchState.POLL_WAIT,
    AgentDispatchState.CANCEL_REQUESTED,
)
ACTIVE_DISPATCH_STATES = (
    AgentDispatchState.SUBMITTING,
    AgentDispatchState.AWAITING_REMOTE,
    AgentDispatchState.POLL_WAIT,
    AgentDispatchState.CANCEL_REQUESTED,
)
ACTIVE_RESEARCH_STATES = (
    AgentSourceResearchState.SUBMITTING,
    AgentSourceResearchState.RUNNING,
    AgentSourceResearchState.CANCEL_REQUESTED,
)
_GLOBAL_DISPATCH_LOCK = 7_304_723_019


class RunPodTransport(Protocol):
    def submit(self, payload: Mapping[str, object]) -> dict[str, Any]: ...

    def status(self, remote_job_id: str) -> dict[str, Any]: ...

    def cancel(self, remote_job_id: str) -> dict[str, Any]: ...


class RunPodClient:
    """RunPod Serverless transport, reserved for the post-staging deployment."""

    def __init__(self, settings: Settings) -> None:
        if not settings.agent_runpod_endpoint_id or not settings.agent_runpod_api_key:
            raise ValueError("RunPod endpoint credentials are not configured")
        base = str(settings.agent_runpod_base_url).rstrip("/")
        self._endpoint_url = f"{base}/v2/{settings.agent_runpod_endpoint_id}"
        self._headers = {
            "Authorization": f"Bearer {settings.agent_runpod_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        self._policy = {
            "executionTimeout": settings.agent_execution_timeout_ms,
            "ttl": settings.agent_job_ttl_ms,
        }
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
            follow_redirects=False,
            headers=self._headers,
        )

    @staticmethod
    def _object_response(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("RunPod returned a non-object response")
        return value

    def submit(self, payload: Mapping[str, object]) -> dict[str, Any]:
        response = self._client.post(
            f"{self._endpoint_url}/run",
            json={"input": dict(payload), "policy": self._policy},
        )
        value = self._object_response(response)
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError("RunPod submission response has no job id")
        return value

    def status(self, remote_job_id: str) -> dict[str, Any]:
        return self._object_response(
            self._client.get(f"{self._endpoint_url}/status/{remote_job_id}")
        )

    def cancel(self, remote_job_id: str) -> dict[str, Any]:
        return self._object_response(
            self._client.post(f"{self._endpoint_url}/cancel/{remote_job_id}")
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RunPodClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class RunPodPodClient:
    """Direct HTTPS transport to a persistent RunPod pod used during validation."""

    def __init__(self, settings: Settings) -> None:
        if not settings.agent_runpod_pod_base_url or not settings.agent_runpod_pod_auth_token:
            raise ValueError("RunPod pod URL and authentication token are not configured")
        self._jobs_url = f"{str(settings.agent_runpod_pod_base_url).rstrip('/')}/v1/jobs"
        self._headers = {
            "Authorization": (f"Bearer {settings.agent_runpod_pod_auth_token.get_secret_value()}"),
            "Content-Type": "application/json",
        }
        self._policy = {
            "executionTimeout": settings.agent_execution_timeout_ms,
            "ttl": settings.agent_job_ttl_ms,
        }
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
            follow_redirects=False,
            headers=self._headers,
        )

    @staticmethod
    def _object_response(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("RunPod pod returned a non-object response")
        return value

    def submit(self, payload: Mapping[str, object]) -> dict[str, Any]:
        value = self._object_response(
            self._client.post(
                self._jobs_url,
                json={"input": dict(payload), "policy": self._policy},
            )
        )
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError("RunPod pod submission response has no job id")
        return value

    def status(self, remote_job_id: str) -> dict[str, Any]:
        return self._object_response(self._client.get(f"{self._jobs_url}/{remote_job_id}"))

    def cancel(self, remote_job_id: str) -> dict[str, Any]:
        return self._object_response(self._client.post(f"{self._jobs_url}/{remote_job_id}/cancel"))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RunPodPodClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_runpod_client(settings: Settings) -> RunPodClient | RunPodPodClient:
    if settings.agent_runpod_transport == "pod":
        return RunPodPodClient(settings)
    return RunPodClient(settings)


def _system_actor(worker_id: str) -> Actor:
    return Actor(
        actor_id=worker_id,
        roles=frozenset(),
        actor_type=ActorType.SYSTEM,
    )


def _load_dispatch(session: Session, dispatch_id: int) -> AgentDispatch:
    dispatch = session.execute(
        select(AgentDispatch)
        .where(AgentDispatch.id == dispatch_id)
        .options(
            selectinload(AgentDispatch.batch)
            .selectinload(AgentMediaBatch.items)
            .selectinload(AgentMediaItem.consent),
            selectinload(AgentDispatch.model_runs),
            selectinload(AgentDispatch.dead_letter),
            selectinload(AgentDispatch.batch).selectinload(AgentMediaBatch.review_task),
        )
    ).scalar_one()
    return dispatch


def claim_next_dispatch(
    session: Session,
    *,
    worker_id: str,
    settings: Settings,
    states: tuple[AgentDispatchState, ...] = CLAIMABLE_STATES,
) -> int | None:
    """Atomically lease one due dispatch on both SQLite and PostgreSQL."""

    now = utcnow()
    due_id = (
        select(AgentDispatch.id)
        .where(
            AgentDispatch.state.in_(states),
            or_(AgentDispatch.next_attempt_at.is_(None), AgentDispatch.next_attempt_at <= now),
            or_(AgentDispatch.lease_until.is_(None), AgentDispatch.lease_until <= now),
        )
        .order_by(AgentDispatch.next_attempt_at, AgentDispatch.id)
        .limit(1)
        .scalar_subquery()
    )
    claimed = session.execute(
        update(AgentDispatch)
        .where(AgentDispatch.id == due_id)
        .values(
            lease_owner=worker_id,
            lease_until=now + timedelta(seconds=settings.agent_dispatch_lease_seconds),
        )
        .returning(AgentDispatch.id)
    ).scalar_one_or_none()
    session.commit()
    return claimed


def _has_active_agent_work(session: Session) -> bool:
    research_count = session.scalar(
        select(func.count())
        .select_from(AgentSourceResearchRun)
        .where(AgentSourceResearchRun.state.in_(ACTIVE_RESEARCH_STATES))
    )
    dispatch_count = session.scalar(
        select(func.count())
        .select_from(AgentDispatch)
        .where(AgentDispatch.state.in_(ACTIVE_DISPATCH_STATES))
    )
    return bool(research_count or dispatch_count)


def _try_global_dispatch_lock(session: Session) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": _GLOBAL_DISPATCH_LOCK},
        ).scalar_one()
    )


def _release_global_dispatch_lock(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": _GLOBAL_DISPATCH_LOCK},
        )


def _release_lease(dispatch: AgentDispatch) -> None:
    dispatch.lease_owner = None
    dispatch.lease_until = None


def purge_due_agent_media(session: Session, *, limit: int = 100) -> int:
    """Remove private content references once their persisted retention expires.

    The URL provider remains responsible for deleting the referenced object via
    its own lifecycle policy. This purge makes the backend unable to reuse the
    media and removes any persisted worker output containing derived content.
    """

    now = utcnow()
    items = list(
        session.execute(
            select(AgentMediaItem)
            .where(
                AgentMediaItem.purged_at.is_(None),
                AgentMediaItem.purge_after <= now,
            )
            .options(selectinload(AgentMediaItem.batch).selectinload(AgentMediaBatch.dispatch))
            .order_by(AgentMediaItem.purge_after, AgentMediaItem.id)
            .limit(limit)
        ).scalars()
    )
    affected_dispatches: dict[int, AgentDispatch] = {}
    for item in items:
        item.working_file_url = None
        item.metadata_payload = {}
        item.processable_payload = {
            "frames": [],
            "audio_url": None,
            "article_text": None,
        }
        item.preprocessing_status = "purged"
        item.purged_at = now
        dispatch = item.batch.dispatch
        if dispatch is not None:
            affected_dispatches[dispatch.id] = dispatch

    terminal_states = {
        AgentDispatchState.SUCCEEDED,
        AgentDispatchState.PARTIAL_FAILURE,
        AgentDispatchState.FAILED,
        AgentDispatchState.DEAD_LETTER,
        AgentDispatchState.CANCELLED,
    }
    for dispatch in affected_dispatches.values():
        dispatch.payload = {
            "schema_version": "1.0",
            "batch_id": dispatch.batch.batch_id,
            "redacted": True,
        }
        dispatch.raw_output = None
        if dispatch.state not in terminal_states:
            dispatch.state = AgentDispatchState.CANCEL_REQUESTED
            dispatch.batch.state = AgentBatchState.CANCEL_REQUESTED
            dispatch.next_attempt_at = now
            dispatch.lease_owner = None
            dispatch.lease_until = None
    if items:
        session.commit()
    return len(items)


def _dead_letter(
    session: Session,
    dispatch: AgentDispatch,
    *,
    worker_id: str,
    failure_class: str,
    error_code: str,
    detail: str,
) -> None:
    now = utcnow()
    safe_detail = detail[:1_000] or "Unspecified dispatcher failure"
    dispatch.state = AgentDispatchState.DEAD_LETTER
    dispatch.batch.state = AgentBatchState.DEAD_LETTER
    dispatch.completed_at = now
    dispatch.batch.completed_at = now
    dispatch.last_error_code = error_code[:128]
    dispatch.last_error_detail = safe_detail
    dispatch.next_attempt_at = None
    if dispatch.dead_letter is None:
        dispatch.dead_letter = AgentDeadLetter(
            dead_letter_id=new_prefixed_id("DLQ"),
            state=AgentDeadLetterState.OPEN,
            failure_class=failure_class[:64],
            error_code=error_code[:128],
            error_detail=safe_detail,
            payload_hash=dispatch.payload_hash,
            remote_job_id=dispatch.remote_job_id,
            failed_at=now,
        )
    _release_lease(dispatch)
    record_operator_audit(
        session,
        actor=_system_actor(worker_id),
        action="agent.dispatch_dead_lettered",
        target_type="agent_dispatch",
        target_id=dispatch.dispatch_id,
        reason=safe_detail,
        trace_id=dispatch.batch.trace_id,
        after={
            "state": dispatch.state.value,
            "error_code": dispatch.last_error_code,
            "remote_job_id": dispatch.remote_job_id,
        },
    )
    if dispatch.batch.analysis_window_id is not None:
        refresh_campaign_day_review_state(
            session,
            analysis_window_id=dispatch.batch.analysis_window_id,
        )
    session.commit()


def _submit(
    session: Session,
    dispatch: AgentDispatch,
    *,
    worker_id: str,
    settings: Settings,
    client: RunPodTransport,
) -> None:
    if dispatch.attempt >= dispatch.max_attempts:
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="submission",
            error_code="agent_submission_attempts_exhausted",
            detail="The dispatch exhausted its allowed submission attempts.",
        )
        return

    # This transaction boundary is the at-most-once fence. A stale SUBMITTING
    # row is never sent again because the remote side may already have accepted it.
    dispatch.state = AgentDispatchState.SUBMITTING
    dispatch.batch.state = AgentBatchState.SUBMITTING
    dispatch.attempt += 1
    dispatch.next_attempt_at = None
    session.commit()

    try:
        response = client.submit(dispatch.payload)
        remote_job_id = response["id"]
        if not isinstance(remote_job_id, str) or not remote_job_id:
            raise ValueError("RunPod submission response has no job id")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # A POST failure cannot prove that RunPod did not accept the job.
        # Retrying would violate at-most-once execution.
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="ambiguous_submission",
            error_code="agent_submission_ambiguous",
            detail=f"RunPod submission outcome is ambiguous: {exc}",
        )
        return

    now = utcnow()
    dispatch.remote_job_id = remote_job_id
    dispatch.remote_status = str(response.get("status") or "IN_QUEUE").upper()
    dispatch.state = AgentDispatchState.POLL_WAIT
    dispatch.submitted_at = now
    dispatch.next_attempt_at = now + timedelta(seconds=settings.agent_poll_interval_seconds)
    dispatch.last_error_code = None
    dispatch.last_error_detail = None
    dispatch.batch.state = AgentBatchState.QUEUED
    _release_lease(dispatch)
    record_operator_audit(
        session,
        actor=_system_actor(worker_id),
        action="agent.dispatch_submitted",
        target_type="agent_dispatch",
        target_id=dispatch.dispatch_id,
        reason="Private agent batch accepted by the configured RunPod endpoint.",
        trace_id=dispatch.batch.trace_id,
        after={"remote_job_id": remote_job_id, "payload_hash": dispatch.payload_hash},
    )
    session.commit()


def _expected_evidence_ids(item: AgentMediaItem, result: WorkerItemResult) -> dict[str, set[str]]:
    processable = item.processable_payload
    frame_ids = {
        str(frame["frame_id"])
        for frame in processable.get("frames", [])
        if isinstance(frame, dict) and isinstance(frame.get("frame_id"), str)
    }
    transcript_ids = {segment.segment_id for segment in result.transcript.segments}
    return {
        "frame": frame_ids,
        "image": {item.input_id},
        "transcript_segment": transcript_ids,
        "article_text": {item.input_id},
        "metadata": {item.input_id},
    }


def _validate_item_evidence(item: AgentMediaItem, result: WorkerItemResult) -> None:
    evidence_ids = _expected_evidence_ids(item, result)
    pixel_evidence = evidence_ids["frame"] | evidence_ids["image"]
    region_ids: set[str] = set()
    visual_ids = evidence_ids["frame"] or (
        evidence_ids["image"] if item.working_file_url is not None else set()
    )
    selected_ids = [selection.evidence_id for selection in result.visual_evidence_selection]
    if set(selected_ids) != visual_ids or len(selected_ids) != len(set(selected_ids)):
        raise ValueError(f"visual selection does not cover inputs for {item.input_id}")
    if visual_ids and not any(
        selection.selected_for_grounding for selection in result.visual_evidence_selection
    ):
        raise ValueError(f"visual selection drops every input for {item.input_id}")
    for region in result.pixel_regions:
        if region.region_id in region_ids:
            raise ValueError(f"duplicate region_id for {item.input_id}: {region.region_id}")
        region_ids.add(region.region_id)
        if region.evidence_id not in pixel_evidence:
            raise ValueError(f"unknown pixel evidence for {item.input_id}: {region.evidence_id}")
    for observation in result.factual_observations:
        if observation.evidence_id not in evidence_ids[observation.evidence_kind]:
            raise ValueError(
                f"unknown {observation.evidence_kind} evidence for {item.input_id}: "
                f"{observation.evidence_id}"
            )
        if observation.region_id is not None and observation.region_id not in region_ids:
            raise ValueError(
                f"unknown region reference for {item.input_id}: {observation.region_id}"
            )
    for literal in [*result.explicit_places, *result.explicit_times]:
        if literal.evidence_id not in evidence_ids[literal.evidence_kind]:
            raise ValueError(
                f"unknown {literal.evidence_kind} evidence for {item.input_id}: "
                f"{literal.evidence_id}"
            )


def _validate_worker_output(
    dispatch: AgentDispatch,
    raw_output: object,
) -> WorkerOutput | WorkerOutputV2:
    if dispatch.batch.schema_version == "2.0":
        return validate_worker_output_v2(dispatch, raw_output)
    output = WorkerOutput.model_validate(raw_output)
    if output.batch_id != dispatch.batch.batch_id:
        raise ValueError("worker batch_id does not match the persisted batch")

    persisted_items = {item.input_id: item for item in dispatch.batch.items}
    result_items = {item.input_id: item for item in output.items}
    if len(result_items) != len(output.items):
        raise ValueError("worker output contains duplicate input_id values")
    if set(result_items) != set(persisted_items):
        raise ValueError("worker output input_id set does not match the persisted batch")
    for input_id, result in result_items.items():
        _validate_item_evidence(persisted_items[input_id], result)

    model_runs: dict[str, WorkerModelRun] = {run.model_role: run for run in output.model_runs}
    if len(model_runs) != len(output.model_runs):
        raise ValueError("worker output contains duplicate model roles")
    for role, revision in dispatch.expected_models.items():
        run = model_runs.get(role)
        if run is None:
            raise ValueError(f"worker output is missing expected model role: {role}")
        if run.revision != revision:
            raise ValueError(f"worker model revision mismatch for {role}")
    return output


def _persist_model_runs(
    dispatch: AgentDispatch,
    output: WorkerOutput | WorkerOutputV2,
) -> None:
    if dispatch.model_runs:
        raise ValueError("model runs were already persisted for this dispatch")
    for run in output.model_runs:
        dispatch.model_runs.append(
            AgentModelRun(
                model_role=run.model_role,
                model_id=run.model_id,
                revision=run.revision,
                state=AgentModelRunState(run.status),
                started_at=run.started_at,
                finished_at=run.finished_at,
                load_ms=run.load_ms,
                inference_ms=run.inference_ms,
                peak_vram_bytes=run.peak_vram_bytes,
                error_code=run.error_code,
            )
        )


def _persist_capture_markers(
    session: Session, dispatch: AgentDispatch, output: WorkerOutput
) -> None:
    batch = dispatch.batch
    if batch.incident_id is None or batch.episode_id is None:
        return
    items_by_input = {item.input_id: item for item in batch.items}
    for result in output.items:
        if result.geographic_marker_candidate is None:
            continue
        item = items_by_input[result.input_id]
        metadata = item.metadata_payload
        longitude = metadata.get("longitude")
        latitude = metadata.get("latitude")
        if not isinstance(longitude, int | float) or not isinstance(latitude, int | float):
            raise ValueError(f"capture marker lacks coordinates for {item.input_id}")
        captured_at = metadata.get("captured_at")
        observed_at = None
        if isinstance(captured_at, str):
            observed_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        item_marker = IncidentSpatialMarker(
            marker_id=new_prefixed_id("IM"),
            incident_id=batch.incident_id,
            episode_id=batch.episode_id,
            source_media_item_id=item.id,
            marker_type=result.geographic_marker_candidate.type,
            longitude=float(longitude),
            latitude=float(latitude),
            horizontal_accuracy_m=(
                float(metadata["gps_accuracy_m"])
                if isinstance(metadata.get("gps_accuracy_m"), int | float)
                else None
            ),
            geometry_origin=result.geographic_marker_candidate.geometry_origin,
            review_state=IncidentMarkerReviewState.PENDING,
            observed_at=observed_at,
            spatial_display_allowed="display_spatial_marker" in item.consent.scopes,
        )
        session.add(item_marker)


def _complete(
    session: Session,
    dispatch: AgentDispatch,
    *,
    worker_id: str,
    response: Mapping[str, object],
) -> None:
    raw_output = response.get("output")
    try:
        output = _validate_worker_output(dispatch, raw_output)
        execution_ms = _optional_nonnegative_int(response.get("executionTime"))
        delay_ms = _optional_nonnegative_int(response.get("delayTime"))
    except (ValidationError, ValueError) as exc:
        dispatch.raw_output = raw_output if isinstance(raw_output, dict) else None
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="invalid_output",
            error_code="agent_worker_output_invalid",
            detail=f"Worker output failed strict validation: {exc}",
        )
        return

    now = utcnow()
    dispatch.raw_output = output.model_dump(mode="json")
    dispatch.execution_ms = execution_ms
    dispatch.delay_ms = delay_ms
    dispatch.remote_status = "COMPLETED"

    reason_codes = ["agent_output_requires_human_review"]
    if output.status == "partial_failure":
        reason_codes.append("agent_partial_failure")
    if output.validation_errors:
        reason_codes.append("worker_validation_errors")
    if dispatch.batch.review_task is None:
        dispatch.batch.review_task = AgentReviewTask(
            review_id=new_prefixed_id("AR"),
            state=AgentReviewState.PENDING,
            reason_codes=reason_codes,
        )

    if output.status == "failed":
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="worker_failure",
            error_code="agent_worker_reported_failure",
            detail="The worker completed the request but reported a failed batch.",
        )
        return

    try:
        with session.begin_nested():
            _persist_model_runs(dispatch, output)
            if isinstance(output, WorkerOutputV2):
                persist_worker_output_v2(
                    session,
                    dispatch,
                    output,
                    worker_id=worker_id,
                )
            else:
                _persist_capture_markers(session, dispatch, output)
            session.flush()
    except (IntegrityError, ValueError) as exc:
        dispatch.raw_output = output.model_dump(mode="json")
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="invalid_output",
            error_code="agent_worker_output_persistence_invalid",
            detail=f"Worker output could not be persisted safely: {exc}",
        )
        return

    dispatch.state = (
        AgentDispatchState.SUCCEEDED
        if output.status == "succeeded"
        else AgentDispatchState.PARTIAL_FAILURE
    )
    dispatch.batch.state = (
        AgentBatchState.SUCCEEDED
        if output.status == "succeeded"
        else AgentBatchState.PARTIAL_FAILURE
    )
    dispatch.completed_at = now
    dispatch.batch.completed_at = now
    dispatch.next_attempt_at = None
    dispatch.last_error_code = None
    dispatch.last_error_detail = None
    _release_lease(dispatch)
    if dispatch.batch.analysis_window_id is not None:
        refresh_campaign_day_review_state(
            session,
            analysis_window_id=dispatch.batch.analysis_window_id,
        )
    record_operator_audit(
        session,
        actor=_system_actor(worker_id),
        action="agent.dispatch_completed",
        target_type="agent_dispatch",
        target_id=dispatch.dispatch_id,
        reason="Strict worker output persisted for mandatory human review.",
        trace_id=dispatch.batch.trace_id,
        after={
            "state": dispatch.state.value,
            "remote_job_id": dispatch.remote_job_id,
            "review_required": True,
        },
    )
    session.commit()


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("RunPod timing metric must be a non-negative number")
    return int(value)


def _schedule_poll_retry(
    session: Session,
    dispatch: AgentDispatch,
    *,
    settings: Settings,
    error_code: str,
    detail: str,
) -> None:
    now = utcnow()
    delay = min(
        300,
        settings.agent_poll_interval_seconds * (2 ** min(dispatch.poll_count, 6)),
    )
    dispatch.state = AgentDispatchState.POLL_WAIT
    dispatch.next_attempt_at = now + timedelta(seconds=delay)
    dispatch.last_error_code = error_code
    dispatch.last_error_detail = detail[:1_000]
    _release_lease(dispatch)
    session.commit()


def _poll(
    session: Session,
    dispatch: AgentDispatch,
    *,
    worker_id: str,
    settings: Settings,
    client: RunPodTransport,
) -> None:
    if dispatch.remote_job_id is None:
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="state_corruption",
            error_code="agent_remote_job_id_missing",
            detail="A pollable dispatch has no persisted RunPod job id.",
        )
        return
    try:
        response = client.status(dispatch.remote_job_id)
        raw_status = response.get("status")
        if not isinstance(raw_status, str):
            raise ValueError("RunPod status response has no status string")
        remote_status = raw_status.upper()
        if remote_status not in ACTIVE_REMOTE_STATES | TERMINAL_REMOTE_STATES:
            raise ValueError(f"Unknown RunPod job status: {remote_status}")
    except (httpx.HTTPError, ValueError) as exc:
        dispatch.poll_count += 1
        _schedule_poll_retry(
            session,
            dispatch,
            settings=settings,
            error_code="agent_status_unavailable",
            detail=f"RunPod status could not be read: {exc}",
        )
        return

    now = utcnow()
    dispatch.poll_count += 1
    dispatch.last_polled_at = now
    dispatch.remote_status = remote_status
    if remote_status in ACTIVE_REMOTE_STATES:
        dispatch.state = AgentDispatchState.POLL_WAIT
        dispatch.batch.state = (
            AgentBatchState.RUNNING if remote_status != "IN_QUEUE" else AgentBatchState.QUEUED
        )
        dispatch.next_attempt_at = now + timedelta(seconds=settings.agent_poll_interval_seconds)
        dispatch.last_error_code = None
        dispatch.last_error_detail = None
        _release_lease(dispatch)
        session.commit()
        return
    if remote_status == "COMPLETED":
        _complete(session, dispatch, worker_id=worker_id, response=response)
        return
    if remote_status == "CANCELLED" and dispatch.batch.state == AgentBatchState.CANCEL_REQUESTED:
        dispatch.state = AgentDispatchState.CANCELLED
        dispatch.batch.state = AgentBatchState.CANCELLED
        dispatch.completed_at = now
        dispatch.batch.completed_at = now
        dispatch.batch.cancelled_at = now
        dispatch.next_attempt_at = None
        _release_lease(dispatch)
        refresh_campaign_day_review_state(
            session,
            analysis_window_id=dispatch.batch.analysis_window_id,
        )
        session.commit()
        return
    _dead_letter(
        session,
        dispatch,
        worker_id=worker_id,
        failure_class="remote_failure",
        error_code=f"agent_remote_{remote_status.casefold()}",
        detail=f"RunPod reached terminal state {remote_status}.",
    )


def _cancel(
    session: Session,
    dispatch: AgentDispatch,
    *,
    worker_id: str,
    settings: Settings,
    client: RunPodTransport,
) -> None:
    now = utcnow()
    if dispatch.remote_job_id is None:
        dispatch.state = AgentDispatchState.CANCELLED
        dispatch.batch.state = AgentBatchState.CANCELLED
        dispatch.completed_at = now
        dispatch.batch.completed_at = now
        dispatch.batch.cancelled_at = now
        dispatch.next_attempt_at = None
        _release_lease(dispatch)
        refresh_campaign_day_review_state(
            session,
            analysis_window_id=dispatch.batch.analysis_window_id,
        )
        session.commit()
        return
    try:
        response = client.cancel(dispatch.remote_job_id)
        remote_status = str(response.get("status") or "CANCELLED").upper()
    except httpx.HTTPError as exc:
        dispatch.poll_count += 1
        _schedule_poll_retry(
            session,
            dispatch,
            settings=settings,
            error_code="agent_cancel_unavailable",
            detail=f"RunPod cancellation could not be confirmed: {exc}",
        )
        dispatch.state = AgentDispatchState.CANCEL_REQUESTED
        dispatch.batch.state = AgentBatchState.CANCEL_REQUESTED
        session.commit()
        return
    if remote_status not in {"CANCELLED", "COMPLETED"}:
        dispatch.state = AgentDispatchState.CANCEL_REQUESTED
        dispatch.batch.state = AgentBatchState.CANCEL_REQUESTED
        dispatch.remote_status = remote_status
        dispatch.next_attempt_at = now + timedelta(seconds=settings.agent_poll_interval_seconds)
        _release_lease(dispatch)
        session.commit()
        return
    if remote_status == "COMPLETED":
        _poll(
            session,
            dispatch,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
        return
    dispatch.state = AgentDispatchState.CANCELLED
    dispatch.batch.state = AgentBatchState.CANCELLED
    dispatch.remote_status = "CANCELLED"
    dispatch.completed_at = now
    dispatch.batch.completed_at = now
    dispatch.batch.cancelled_at = now
    dispatch.next_attempt_at = None
    _release_lease(dispatch)
    refresh_campaign_day_review_state(
        session,
        analysis_window_id=dispatch.batch.analysis_window_id,
    )
    session.commit()


def process_claimed_dispatch(
    session: Session,
    *,
    dispatch_id: int,
    worker_id: str,
    settings: Settings,
    client: RunPodTransport,
) -> None:
    dispatch = _load_dispatch(session, dispatch_id)
    if dispatch.lease_owner != worker_id:
        return
    now = utcnow()
    if dispatch.deadline_at is not None and as_utc(dispatch.deadline_at) <= now:
        if dispatch.remote_job_id is not None:
            with suppress(httpx.HTTPError):
                client.cancel(dispatch.remote_job_id)
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="deadline",
            error_code="agent_batch_deadline_exceeded",
            detail="The agent batch deadline elapsed before a validated result was persisted.",
        )
        return
    if dispatch.state == AgentDispatchState.SUBMITTING:
        _dead_letter(
            session,
            dispatch,
            worker_id=worker_id,
            failure_class="ambiguous_submission",
            error_code="agent_stale_submitting_state",
            detail=(
                "A prior dispatcher stopped after crossing the submission fence; "
                "automatic resubmission is forbidden."
            ),
        )
    elif dispatch.state == AgentDispatchState.QUEUED:
        _submit(
            session,
            dispatch,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
    elif dispatch.state == AgentDispatchState.CANCEL_REQUESTED:
        _cancel(
            session,
            dispatch,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
    else:
        _poll(
            session,
            dispatch,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )


def run_dispatcher_once(
    factory: sessionmaker[Session],
    *,
    worker_id: str,
    settings: Settings,
    client: RunPodTransport,
) -> bool:
    with factory() as session:
        if not _try_global_dispatch_lock(session):
            return False
        try:
            purge_due_agent_media(session)

            # Always finish or cancel the one remote operation already in
            # flight before claiming any queued work for another fire.
            research_row_id = claim_next_source_research(
                session,
                worker_id=worker_id,
                settings=settings,
                states=ACTIVE_RESEARCH_STATES,
            )
            if research_row_id is not None:
                process_claimed_source_research(
                    session,
                    research_row_id=research_row_id,
                    worker_id=worker_id,
                    settings=settings,
                    client=client,
                )
                return True
            dispatch_id = claim_next_dispatch(
                session,
                worker_id=worker_id,
                settings=settings,
                states=ACTIVE_DISPATCH_STATES,
            )
            if dispatch_id is not None:
                process_claimed_dispatch(
                    session,
                    dispatch_id=dispatch_id,
                    worker_id=worker_id,
                    settings=settings,
                    client=client,
                )
                return True
            if _has_active_agent_work(session):
                return False

            research_row_id = claim_next_source_research(
                session,
                worker_id=worker_id,
                settings=settings,
                states=(AgentSourceResearchState.QUEUED,),
            )
            if research_row_id is not None:
                process_claimed_source_research(
                    session,
                    research_row_id=research_row_id,
                    worker_id=worker_id,
                    settings=settings,
                    client=client,
                )
                return True
            dispatch_id = claim_next_dispatch(
                session,
                worker_id=worker_id,
                settings=settings,
                states=(AgentDispatchState.QUEUED,),
            )
            if dispatch_id is None:
                return False
            process_claimed_dispatch(
                session,
                dispatch_id=dispatch_id,
                worker_id=worker_id,
                settings=settings,
                client=client,
            )
            return True
        finally:
            _release_global_dispatch_lock(session)
