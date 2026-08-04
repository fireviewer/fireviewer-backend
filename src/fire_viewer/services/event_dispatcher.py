"""At-most-once dispatch and private persistence for event-analysis jobs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol, cast

from pydantic import ValidationError
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    ArtifactLineage,
    Episode,
    EventAnalysisJob,
    EventCandidate,
    EvidenceAsset,
    ExternalArtifactRevision,
    ExternalClaim,
    FireActivityEvent,
    FireActivityEventEvidence,
    IncidentCandidate,
    LocalizationAttempt,
    OutboxEvent,
)
from fire_viewer.domain.enums import (
    ActorType,
    EventAnalysisJobState,
    EventCandidateState,
    ExternalArtifactStatus,
    ExternalLineageRelation,
    FireActivityEventState,
    LocalizationAttemptState,
)
from fire_viewer.domain.event_schemas import (
    EventWorkerOutput,
    EventWorkerPerceptionAnchor,
    EventWorkerSpatialEvidence,
)
from fire_viewer.domain.hashing import json_safe, sha256_hex
from fire_viewer.services.common import record_audit
from fire_viewer.services.event_evidence_access import create_event_evidence_worker_url

ACTIVE_REMOTE_STATES = frozenset({"IN_QUEUE", "IN_PROGRESS", "RUNNING"})
TERMINAL_REMOTE_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})
CLAIMABLE_JOB_STATES = (
    EventAnalysisJobState.QUEUED,
    EventAnalysisJobState.SUBMITTING,
    EventAnalysisJobState.AWAITING_REMOTE,
)
_EXTERNAL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CLAIM_PHENOMENA = {
    "active_fire_point": "active_fire_point",
    "visible_front": "visible_fire_front",
    "smoke_origin": "smoke_origin",
    "thermal_hotspot": "thermal_hotspot",
    "burned_area": "burned_area",
    "simulation_output": "simulation",
}


def _transient_worker_payload(
    session: Session,
    *,
    candidate: EventCandidate,
    persisted_payload: Mapping[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Attach expiring evidence URLs without persisting them in the outbox."""

    payload = dict(persisted_payload)
    raw_bundle = payload.get("bundle")
    if not isinstance(raw_bundle, dict):
        raise ValueError("event bundle is not an object")
    bundle = dict(raw_bundle)
    raw_evidence = bundle.get("evidence_assets", [])
    if not isinstance(raw_evidence, list):
        raise ValueError("event evidence list is invalid")
    rows = session.execute(
        select(EvidenceAsset).where(EvidenceAsset.event_candidate_id == candidate.id)
    ).scalars()
    assets = {row.asset_id: row for row in rows}
    transient_evidence: list[dict[str, Any]] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            raise ValueError("event evidence item is invalid")
        item = dict(raw_item)
        asset_id = item.get("evidence_asset_id")
        asset = assets.get(asset_id) if isinstance(asset_id, str) else None
        if asset is None or asset.sha256 is None or item.get("sha256") != asset.sha256:
            raise ValueError("event evidence revision is unavailable")
        item["object_uri"] = f"private://{asset.asset_id}"
        item["working_file_url"] = create_event_evidence_worker_url(
            candidate_id=candidate.candidate_id,
            asset_id=asset.asset_id,
            sha256=asset.sha256,
            settings=settings,
        )
        transient_evidence.append(item)
    bundle["evidence_assets"] = transient_evidence
    payload["bundle"] = bundle
    return cast(dict[str, Any], json_safe(payload))


class EventAnalysisTransport(Protocol):
    def submit(self, payload: Mapping[str, object]) -> dict[str, Any]: ...

    def status(self, remote_job_id: str) -> dict[str, Any]: ...


def _append_candidate_state(
    candidate: EventCandidate,
    state: EventCandidateState,
    *,
    actor_id: str,
    reason: str,
) -> None:
    now = utcnow()
    candidate.state = state
    candidate.state_history = [
        *candidate.state_history,
        {
            "state": state.value,
            "at": now.isoformat(),
            "actor": actor_id,
            "reason": reason,
        },
    ]
    candidate.version += 1


def claim_next_event_analysis_job(
    session: Session,
    *,
    worker_id: str,
    settings: Settings,
) -> int | None:
    now = utcnow()
    due_id = (
        select(EventAnalysisJob.id)
        .where(
            EventAnalysisJob.state.in_(CLAIMABLE_JOB_STATES),
            or_(
                EventAnalysisJob.next_poll_at.is_(None),
                EventAnalysisJob.next_poll_at <= now,
            ),
            or_(
                EventAnalysisJob.lease_until.is_(None),
                EventAnalysisJob.lease_until <= now,
            ),
        )
        .order_by(EventAnalysisJob.next_poll_at, EventAnalysisJob.id)
        .limit(1)
        .scalar_subquery()
    )
    job_id = session.execute(
        update(EventAnalysisJob)
        .where(EventAnalysisJob.id == due_id)
        .values(
            lease_owner=worker_id,
            lease_until=now + timedelta(seconds=settings.agent_dispatch_lease_seconds),
        )
        .returning(EventAnalysisJob.id)
    ).scalar_one_or_none()
    session.commit()
    return job_id


def _load(session: Session, job_id: int) -> tuple[EventAnalysisJob, EventCandidate, OutboxEvent]:
    job = session.get(EventAnalysisJob, job_id)
    if job is None:
        raise RuntimeError("Claimed event analysis job disappeared")
    candidate = session.get(EventCandidate, job.event_candidate_id)
    if candidate is None:
        raise RuntimeError("Event analysis candidate invariant is broken")
    outbox = session.execute(
        select(OutboxEvent).where(OutboxEvent.event_id == job.outbox_event_id)
    ).scalar_one()
    return job, candidate, outbox


def _release(job: EventAnalysisJob) -> None:
    job.lease_owner = None
    job.lease_until = None


def _external_observations_for_candidate(
    session: Session,
    candidate: EventCandidate,
) -> list[dict[str, Any]]:
    statement_revision_id: int | None = None
    if candidate.incident_candidate_id is not None:
        private_incident = session.get(IncidentCandidate, candidate.incident_candidate_id)
        if private_incident is not None:
            statement_revision_id = private_incident.source_statement_revision_id
    query = (
        select(ExternalClaim, ExternalArtifactRevision)
        .join(
            ExternalArtifactRevision,
            ExternalArtifactRevision.id == ExternalClaim.artifact_revision_id,
        )
        .where(
            ExternalArtifactRevision.status != ExternalArtifactStatus.RETRACTED,
            ~select(ArtifactLineage.id)
            .where(
                ArtifactLineage.parent_revision_id == ExternalArtifactRevision.id,
                ArtifactLineage.relation.in_(
                    {
                        ExternalLineageRelation.SUPERSEDES,
                        ExternalLineageRelation.RETRACTS,
                    }
                ),
            )
            .exists(),
        )
    )
    if candidate.incident_id is not None:
        query = query.where(ExternalClaim.incident_id == candidate.incident_id)
    elif statement_revision_id is not None:
        query = query.where(ExternalClaim.artifact_revision_id == statement_revision_id)
    else:
        return []
    rows = session.execute(
        query.order_by(ExternalArtifactRevision.retrieved_at.desc(), ExternalClaim.id).limit(256)
    ).all()
    observations: list[dict[str, Any]] = []
    for claim, artifact in rows:
        conflicts_raw = claim.assertion_payload.get("conflicts_with")
        conflicts = (
            tuple(
                value
                for value in conflicts_raw[:64]
                if isinstance(value, str) and _EXTERNAL_IDENTIFIER_RE.fullmatch(value)
            )
            if isinstance(conflicts_raw, list)
            else ()
        )
        observed_at = (
            artifact.effective_start_at or artifact.acquisition_start_at or artifact.published_at
        )
        observations.append(
            {
                "observation_id": claim.claim_id,
                "artifact_revision_id": artifact.artifact_revision_id,
                "lineage_family_id": claim.independent_family_key,
                "semantic_role": artifact.semantic_role.value,
                "phenomenon": _CLAIM_PHENOMENA.get(claim.assertion_kind),
                "observed_at": as_utc(observed_at).isoformat() if observed_at else None,
                "geometry_geojson": claim.geometry_geojson,
                "resolution_m": artifact.resolution_m,
                "conflicts_with": conflicts,
            }
        )
    return observations


def _fail(
    session: Session,
    job: EventAnalysisJob,
    candidate: EventCandidate,
    outbox: OutboxEvent,
    *,
    worker_id: str,
    code: str,
    detail: str,
) -> None:
    now = utcnow()
    before = {"candidate_state": candidate.state.value, "job_state": job.state.value}
    job.state = EventAnalysisJobState.FAILED
    job.completed_at = now
    job.next_poll_at = None
    job.last_error_code = code[:128]
    job.last_error_detail = detail[:1_000]
    outbox.last_error = code[:128]
    candidate.failure_code = code[:128]
    _append_candidate_state(
        candidate,
        EventCandidateState.FAILED,
        actor_id=worker_id,
        reason=detail[:500],
    )
    _release(job)
    record_audit(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=worker_id,
        action="event_candidate.analysis_failed",
        target_type="event_candidate",
        target_id=candidate.candidate_id,
        reason=detail[:500],
        trace_id=outbox.trace_id,
        before=before,
        after={"candidate_state": candidate.state.value, "job_state": job.state.value},
        payload={"error_code": code[:128]},
    )
    session.commit()


def _uncertainty_geometry(geometry: dict[str, Any], radius_m: float) -> dict[str, Any]:
    source = shape(geometry)
    centre = source.centroid
    local_crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={centre.y:.12f} +lon_0={centre.x:.12f} +datum=WGS84 +units=m +no_defs"
    )
    to_local = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(local_crs, "EPSG:4326", always_xy=True)
    local = transform(to_local.transform, source)
    buffered = transform(to_wgs84.transform, local.buffer(radius_m, quad_segs=8))
    value = json_safe(mapping(buffered))
    if not isinstance(value, dict):
        raise ValueError("Uncertainty geometry serialization failed")
    return value


def _validate_localized_attempt_provenance(
    outbox: OutboxEvent,
    output: EventWorkerOutput,
) -> None:
    raw_bundle = outbox.payload.get("bundle")
    raw_anchors = outbox.payload.get("perception_anchors", [])
    raw_spatial = outbox.payload.get("spatial_evidence", [])
    if (
        not isinstance(raw_bundle, dict)
        or not isinstance(raw_anchors, list)
        or not isinstance(raw_spatial, list)
    ):
        raise ValueError("persisted event perception provenance is invalid")
    raw_evidence = raw_bundle.get("evidence_assets", [])
    if not isinstance(raw_evidence, list):
        raise ValueError("persisted event evidence provenance is invalid")
    evidence_asset_ids = {
        item.get("evidence_asset_id")
        for item in raw_evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_asset_id"), str)
    }
    if len(evidence_asset_ids) != len(raw_evidence):
        raise ValueError("persisted event evidence provenance is not uniquely keyed")
    persisted_anchors = {
        item.get("anchor_id"): item
        for item in raw_anchors
        if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
    }
    persisted_spatial = {
        item.get("anchor_id"): item
        for item in raw_spatial
        if isinstance(item, dict) and isinstance(item.get("anchor_id"), str)
    }
    if len(persisted_anchors) != len(raw_anchors) or len(persisted_spatial) != len(raw_spatial):
        raise ValueError("persisted event perception provenance is not uniquely keyed")
    validated_persisted_anchors = {
        anchor_id: EventWorkerPerceptionAnchor.model_validate(item)
        for anchor_id, item in persisted_anchors.items()
    }
    validated_persisted_spatial = {
        anchor_id: EventWorkerSpatialEvidence.model_validate(item)
        for anchor_id, item in persisted_spatial.items()
    }
    if not set(validated_persisted_spatial).issubset(validated_persisted_anchors):
        raise ValueError("persisted spatial provenance references an unknown perception anchor")
    if any(
        anchor.evidence_asset_id not in evidence_asset_ids
        for anchor in validated_persisted_anchors.values()
    ):
        raise ValueError("persisted perception anchor references unknown private evidence")
    for anchor in output.perception_anchors:
        if anchor.evidence_asset_id not in evidence_asset_ids:
            raise ValueError("worker perception anchor references unknown private evidence")
        persisted_anchor = validated_persisted_anchors.get(anchor.anchor_id)
        if persisted_anchor is not None and persisted_anchor.model_dump(
            mode="json"
        ) != anchor.model_dump(mode="json"):
            raise ValueError("worker altered persisted perception provenance")
    for spatial in output.spatial_evidence:
        persisted_spatial_result = validated_persisted_spatial.get(spatial.anchor_id)
        if persisted_spatial_result is None:
            raise ValueError("worker introduced untrusted spatial provenance")
        if persisted_spatial_result.model_dump(mode="json") != spatial.model_dump(mode="json"):
            raise ValueError("worker altered persisted spatial provenance")


def _persist_worker_output(
    session: Session,
    job: EventAnalysisJob,
    candidate: EventCandidate,
    outbox: OutboxEvent,
    output: EventWorkerOutput,
    *,
    worker_id: str,
) -> None:
    if output.candidate_id != candidate.candidate_id:
        raise ValueError("worker candidate identifier mismatch")
    if output.status != "needs_review" and output.event_proposals:
        raise ValueError("non-review worker output cannot create event proposals")
    _validate_localized_attempt_provenance(outbox, output)
    for proposal in output.event_proposals:
        if as_utc(proposal.observed_time.start_at) != as_utc(candidate.observed_start_at):
            raise ValueError("worker proposal start time differs from the candidate evidence")
        proposal_end = (
            as_utc(proposal.observed_time.end_at) if proposal.observed_time.end_at else None
        )
        candidate_end = as_utc(candidate.observed_end_at) if candidate.observed_end_at else None
        if proposal_end != candidate_end:
            raise ValueError("worker proposal end time differs from the candidate evidence")

    anchors_by_id = {anchor.anchor_id: anchor for anchor in output.perception_anchors}
    spatial_by_anchor = {item.anchor_id: item for item in output.spatial_evidence}
    attempt_rows: dict[str, LocalizationAttempt] = {}
    for attempt in output.localization_attempts:
        state = (
            LocalizationAttemptState.SHADOW
            if attempt.shadow_only
            else {
                "localized": LocalizationAttemptState.PROPOSED,
                "sector": LocalizationAttemptState.SECTOR,
                "abstained": LocalizationAttemptState.ABSTAINED,
            }[attempt.status]
        )
        uncertainty = None
        if attempt.geometry_geojson is not None and attempt.horizontal_accuracy_m is not None:
            uncertainty = _uncertainty_geometry(
                attempt.geometry_geojson,
                attempt.horizontal_accuracy_m,
            )
        anchor = anchors_by_id.get(attempt.anchor_id) if attempt.anchor_id else None
        spatial = spatial_by_anchor.get(attempt.anchor_id) if attempt.anchor_id else None
        row = LocalizationAttempt(
            attempt_id=attempt.attempt_id,
            event_candidate_id=candidate.id,
            state=state,
            method=attempt.method or "none",
            model_id=attempt.model_id,
            model_revision=attempt.model_revision,
            view_profile=output.view_profile or "unclassified",
            anchor_payload={
                "anchor_id": attempt.anchor_id,
                "phenomenon": attempt.phenomenon,
                "sector": attempt.sector.model_dump(mode="json") if attempt.sector else None,
                "perception": anchor.model_dump(mode="json") if anchor else None,
            },
            geometry_geojson=attempt.geometry_geojson,
            uncertainty_geojson=uncertainty,
            horizontal_uncertainty_m=attempt.horizontal_accuracy_m,
            abstention_reason=(
                ",".join(attempt.reason_codes)[:1_000] if attempt.status == "abstained" else None
            ),
            provenance={
                "reference_revision": attempt.reference_revision,
                "direction_uncertainty_deg": attempt.direction_uncertainty_deg,
                "distance_uncertainty_m": attempt.distance_uncertainty_m,
                "shadow_only": attempt.shadow_only,
                "spatial_evidence": spatial.model_dump(mode="json") if spatial else None,
            },
        )
        session.add(row)
        session.flush()
        attempt_rows[attempt.attempt_id] = row

    current_episode = None
    if candidate.incident_id is not None:
        current_episode = session.execute(
            select(Episode)
            .where(Episode.incident_id == candidate.incident_id, Episode.is_current.is_(True))
            .order_by(Episode.ordinal.desc())
            .limit(1)
        ).scalar_one_or_none()
    evidence_rows = list(
        session.scalars(
            select(EvidenceAsset)
            .where(EvidenceAsset.event_candidate_id == candidate.id)
            .order_by(EvidenceAsset.id)
        )
    )
    kind_map = {
        "active_fire_point": "active_fire",
        "visible_fire_front": "visible_front",
        "smoke_origin": "smoke_origin",
    }
    if candidate.incident_id is not None and current_episode is not None:
        for proposal in output.event_proposals:
            attempt_row = attempt_rows[proposal.attempt_id]
            event_row = FireActivityEvent(
                event_id=proposal.proposal_id,
                incident_id=candidate.incident_id,
                episode_id=current_episode.id,
                source_candidate_id=candidate.id,
                localization_attempt_id=attempt_row.id,
                state=FireActivityEventState.DRAFT,
                phenomenon_kind=kind_map[proposal.phenomenon],
                observed_start_at=as_utc(proposal.observed_time.start_at),
                observed_end_at=(
                    as_utc(proposal.observed_time.end_at) if proposal.observed_time.end_at else None
                ),
                geometry_geojson=proposal.geometry_geojson,
                uncertainty_geojson=_uncertainty_geometry(
                    proposal.geometry_geojson,
                    proposal.horizontal_accuracy_m,
                ),
                method=attempt_row.method,
                version=1,
            )
            session.add(event_row)
            session.flush()
            session.add_all(
                [
                    FireActivityEventEvidence(
                        fire_activity_event_id=event_row.id,
                        evidence_asset_id=evidence.id,
                        role="support",
                    )
                    for evidence in evidence_rows
                ]
            )

    now = utcnow()
    result_payload = output.model_dump(mode="json")
    job.result_sha256 = sha256_hex(result_payload)
    job.result_summary = {
        "view_profile": output.view_profile,
        "attempt_count": len(output.localization_attempts),
        "proposal_count": len(output.event_proposals),
        "external_family_ids": output.independent_external_families,
        "contradictions": output.contradictions,
        "reason_codes": output.reason_codes,
    }
    job.completed_at = now
    job.next_poll_at = None
    candidate.failure_code = None
    if output.status == "abstained":
        job.state = EventAnalysisJobState.ABSTAINED
        candidate_state = EventCandidateState.ABSTAINED
    elif output.status == "needs_review":
        job.state = EventAnalysisJobState.COMPLETED
        candidate_state = EventCandidateState.NEEDS_REVIEW
    else:
        job.state = EventAnalysisJobState.FAILED
        candidate_state = EventCandidateState.FAILED
        candidate.failure_code = "worker_reported_failure"
    _append_candidate_state(
        candidate,
        candidate_state,
        actor_id=worker_id,
        reason=f"Event worker completed with {output.status}.",
    )
    _release(job)
    record_audit(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=worker_id,
        action="event_candidate.analysis_completed",
        target_type="event_candidate",
        target_id=candidate.candidate_id,
        reason="Validated event worker output persisted for human review.",
        trace_id=outbox.trace_id,
        after={
            "candidate_state": candidate.state.value,
            "job_state": job.state.value,
            "result_sha256": job.result_sha256,
        },
    )
    session.commit()


def _consume_remote_result(
    session: Session,
    job: EventAnalysisJob,
    candidate: EventCandidate,
    outbox: OutboxEvent,
    response: dict[str, Any],
    *,
    worker_id: str,
) -> None:
    raw_output = response.get("output")
    if not isinstance(raw_output, dict):
        _fail(
            session,
            job,
            candidate,
            outbox,
            worker_id=worker_id,
            code="event_worker_output_missing",
            detail="The remote worker completed without an object output.",
        )
        return
    try:
        output = EventWorkerOutput.model_validate(raw_output)
        _persist_worker_output(
            session,
            job,
            candidate,
            outbox,
            output,
            worker_id=worker_id,
        )
    except (ValidationError, ValueError) as exc:
        _fail(
            session,
            job,
            candidate,
            outbox,
            worker_id=worker_id,
            code="event_worker_output_invalid",
            detail=f"The event worker output failed closed validation ({type(exc).__name__}).",
        )


def _process_claimed(
    session: Session,
    job_id: int,
    *,
    worker_id: str,
    settings: Settings,
    client: EventAnalysisTransport,
) -> None:
    job, candidate, outbox = _load(session, job_id)
    if job.lease_owner != worker_id:
        return
    if job.state == EventAnalysisJobState.SUBMITTING:
        _fail(
            session,
            job,
            candidate,
            outbox,
            worker_id=worker_id,
            code="event_submission_outcome_ambiguous",
            detail=(
                "A previous event submission stopped before its remote identifier was persisted."
            ),
        )
        return
    if job.state == EventAnalysisJobState.QUEUED:
        now = utcnow()
        before = {"candidate_state": candidate.state.value, "job_state": job.state.value}
        job.state = EventAnalysisJobState.SUBMITTING
        job.submission_started_at = now
        job.attempts += 1
        outbox.attempts += 1
        _append_candidate_state(
            candidate,
            EventCandidateState.ANALYZING,
            actor_id=worker_id,
            reason="Event analysis submission started.",
        )
        record_audit(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=worker_id,
            action="event_candidate.analysis_started",
            target_type="event_candidate",
            target_id=candidate.candidate_id,
            reason="At-most-once worker submission fence persisted.",
            trace_id=outbox.trace_id,
            before=before,
            after={"candidate_state": candidate.state.value, "job_state": job.state.value},
        )
        submission_payload = dict(outbox.payload)
        bundle = submission_payload.get("bundle")
        if not isinstance(bundle, dict):
            _fail(
                session,
                job,
                candidate,
                outbox,
                worker_id=worker_id,
                code="event_bundle_invalid",
                detail="The persisted event analysis bundle is invalid.",
            )
            return
        submission_bundle = dict(bundle)
        submission_bundle["external_observations"] = _external_observations_for_candidate(
            session, candidate
        )
        submission_payload["bundle"] = submission_bundle
        persisted_payload = json_safe(submission_payload)
        outbox.payload = persisted_payload
        session.commit()
        try:
            response = client.submit(
                _transient_worker_payload(
                    session,
                    candidate=candidate,
                    persisted_payload=persisted_payload,
                    settings=settings,
                )
            )
            remote_job_id = response.get("id")
            if not isinstance(remote_job_id, str) or not remote_job_id:
                raise ValueError("Remote event submission returned no job identifier")
        except Exception as exc:
            _fail(
                session,
                job,
                candidate,
                outbox,
                worker_id=worker_id,
                code="event_submission_failed_closed",
                detail=(
                    f"Event submission failed at the at-most-once boundary ({type(exc).__name__})."
                ),
            )
            return
        job.remote_job_id = remote_job_id
        job.state = EventAnalysisJobState.AWAITING_REMOTE
        job.submitted_at = utcnow()
        job.next_poll_at = utcnow()
        outbox.published_at = utcnow()
        outbox.last_error = None
        _release(job)
        session.commit()
        if str(response.get("status", "")).upper() == "COMPLETED":
            _consume_remote_result(
                session,
                job,
                candidate,
                outbox,
                response,
                worker_id=worker_id,
            )
        return

    if job.remote_job_id is None:
        _fail(
            session,
            job,
            candidate,
            outbox,
            worker_id=worker_id,
            code="event_remote_job_id_missing",
            detail="An awaiting event job has no remote identifier.",
        )
        return
    try:
        response = client.status(job.remote_job_id)
    except Exception as exc:
        job.attempts += 1
        job.last_error_code = "event_status_poll_failed"
        job.last_error_detail = f"Remote status poll failed ({type(exc).__name__})."
        if job.attempts >= settings.agent_dispatch_max_attempts:
            _fail(
                session,
                job,
                candidate,
                outbox,
                worker_id=worker_id,
                code="event_status_poll_exhausted",
                detail="Remote event status could not be confirmed after bounded retries.",
            )
            return
        job.next_poll_at = utcnow() + timedelta(seconds=settings.agent_poll_interval_seconds)
        _release(job)
        session.commit()
        return

    remote_state = str(response.get("status", "")).upper()
    if remote_state in ACTIVE_REMOTE_STATES:
        job.next_poll_at = utcnow() + timedelta(seconds=settings.agent_poll_interval_seconds)
        job.last_error_code = None
        job.last_error_detail = None
        _release(job)
        session.commit()
        return
    if remote_state == "COMPLETED":
        _consume_remote_result(
            session,
            job,
            candidate,
            outbox,
            response,
            worker_id=worker_id,
        )
        return
    if remote_state in TERMINAL_REMOTE_STATES:
        _fail(
            session,
            job,
            candidate,
            outbox,
            worker_id=worker_id,
            code=f"event_remote_{remote_state.casefold()}",
            detail=f"The remote event job ended in state {remote_state}.",
        )
        return
    _fail(
        session,
        job,
        candidate,
        outbox,
        worker_id=worker_id,
        code="event_remote_state_unknown",
        detail="The remote event job returned an unknown state.",
    )


def run_event_dispatcher_once(
    factory: sessionmaker[Session],
    *,
    worker_id: str,
    settings: Settings,
    client: EventAnalysisTransport,
) -> bool:
    if not settings.agent_event_pipeline_enabled:
        return False
    with factory() as claim_session:
        job_id = claim_next_event_analysis_job(
            claim_session,
            worker_id=worker_id,
            settings=settings,
        )
    if job_id is None:
        return False
    with factory() as process_session:
        _process_claimed(
            process_session,
            job_id,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
    return True
