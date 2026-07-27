"""Validation and private persistence for the incident-intelligence v2 contract."""

from __future__ import annotations

from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.ops import transform, unary_union
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentDispatch,
    AgentFactProposal,
    AgentMediaItem,
    AgentSituationReportFact,
    AgentSituationReportRevision,
    AgentSourceAnnotation,
    AgentSpatialProposal,
)
from fire_viewer.domain.agent_schemas import WorkerItemResultV2, WorkerOutputV2
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    ActorType,
    AgentAnalysisState,
    AgentMediaType,
    AgentProposalReviewState,
    AgentReportReviewState,
)
from fire_viewer.domain.hashing import json_safe
from fire_viewer.services.common import record_operator_audit

_WGS84_TO_L93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
_L93_TO_WGS84 = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)


def _refresh_daily_activity_zone(
    session: Session,
    dispatch: AgentDispatch,
    *,
    worker_id: str,
) -> ActiveFireZoneRevision | None:
    """Build one private, editable 2D activity-zone draft from all grounded daily points."""

    batch = dispatch.batch
    analysis_window = batch.analysis_window
    if analysis_window is None or batch.incident_id is None or batch.episode_id is None:
        return None
    points = list(
        session.scalars(
            select(AgentSpatialProposal)
            .where(
                AgentSpatialProposal.analysis_window_id == analysis_window.id,
                AgentSpatialProposal.status == "ground_point",
                AgentSpatialProposal.review_state.notin_(
                    [AgentProposalReviewState.REJECTED, AgentProposalReviewState.INVALIDATED]
                ),
            )
            .order_by(AgentSpatialProposal.proposal_id.asc())
        )
    )
    grounded = [
        item
        for item in points
        if item.longitude is not None
        and item.latitude is not None
        and item.horizontal_accuracy_m is not None
    ]
    if not grounded:
        return None

    footprints = []
    for item in grounded:
        assert item.longitude is not None
        assert item.latitude is not None
        assert item.horizontal_accuracy_m is not None
        east, north = _WGS84_TO_L93.transform(float(item.longitude), float(item.latitude))
        radius_m = min(max(float(item.horizontal_accuracy_m), 25.0), 5_000.0)
        footprints.append(Point(east, north).buffer(radius_m, quad_segs=4))
    activity_l93 = unary_union(footprints).simplify(5.0, preserve_topology=True)
    activity_wgs84 = transform(_L93_TO_WGS84.transform, activity_l93)
    if isinstance(activity_wgs84, Polygon):
        activity_wgs84 = MultiPolygon([activity_wgs84])
    if not isinstance(activity_wgs84, MultiPolygon) or activity_wgs84.is_empty:
        raise ValueError("grounded activity points did not produce a valid daily zone")
    geometry = json_safe(mapping(activity_wgs84))
    assert isinstance(geometry, dict)
    support_ids = [f"proposal:{item.proposal_id}" for item in grounded]

    latest = session.execute(
        select(ActiveFireZoneRevision)
        .where(
            ActiveFireZoneRevision.incident_id == batch.incident_id,
            ActiveFireZoneRevision.episode_id == batch.episode_id,
        )
        .order_by(ActiveFireZoneRevision.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None and latest.analysis_window_id == analysis_window.id:
        if latest.geometry_origin != "AGENT_DERIVED" or (
            latest.review_state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION
        ):
            return latest
        if latest.geometry_geojson == geometry and latest.supporting_marker_ids == support_ids:
            return latest
        if latest.review_state == ActiveFireZoneReviewState.DRAFT:
            latest.review_state = ActiveFireZoneReviewState.REJECTED
            latest.reviewed_by = worker_id
            latest.reviewed_at = analysis_window.window_end_at
            latest.review_reason = "Superseded by a later source batch for the same daily window."

    revision = ActiveFireZoneRevision(
        zone_revision_id=new_prefixed_id("azr"),
        incident_id=batch.incident_id,
        episode_id=batch.episode_id,
        analysis_window_id=analysis_window.id,
        revision=(latest.revision if latest is not None else 0) + 1,
        valid_at=analysis_window.window_end_at,
        geometry_geojson=geometry,
        geometry_origin="AGENT_DERIVED",
        supporting_marker_ids=support_ids,
        source_revision_ids=[],
        review_state=ActiveFireZoneReviewState.DRAFT,
        supersedes_revision_id=latest.id if latest is not None else None,
        created_by=worker_id,
        reason=(
            f"Zone d'activité 2D dérivée de {len(grounded)} point(s) géolocalisé(s) "
            f"pour le {analysis_window.local_date.isoformat()}; validation humaine obligatoire."
        ),
    )
    session.add(revision)
    record_operator_audit(
        session,
        actor=Actor(actor_id=worker_id, roles=frozenset(), actor_type=ActorType.SYSTEM),
        action="agent.daily_activity_zone_drafted",
        target_type="active_fire_zone_revision",
        target_id=revision.zone_revision_id,
        reason=revision.reason,
        trace_id=batch.trace_id,
        after={
            "analysis_id": analysis_window.analysis_id,
            "revision": revision.revision,
            "review_state": revision.review_state.value,
            "supporting_marker_ids": support_ids,
        },
    )
    return revision


def _expected_evidence_ids(
    item: AgentMediaItem,
    result: WorkerItemResultV2,
) -> dict[str, set[str]]:
    processable = item.processable_payload
    frame_ids = {
        str(frame["frame_id"])
        for frame in processable.get("frames", [])
        if isinstance(frame, dict) and isinstance(frame.get("frame_id"), str)
    }
    transcript_ids = {segment.segment_id for segment in result.transcript.segments}
    return {
        "frame": frame_ids,
        "image": {item.input_id} if item.media_type == AgentMediaType.IMAGE else set(),
        "satellite_image": (
            {item.input_id} if item.media_type == AgentMediaType.SATELLITE_IMAGE else set()
        ),
        "transcript_segment": transcript_ids,
        "article_text": (
            {item.input_id} if isinstance(processable.get("article_text"), str) else set()
        ),
        "metadata": {item.input_id} if item.metadata_payload else set(),
    }


def _validate_item_evidence(item: AgentMediaItem, result: WorkerItemResultV2) -> None:
    expected = _expected_evidence_ids(item, result)
    for annotation in result.source_annotations:
        if annotation.evidence_id not in expected[annotation.evidence_kind]:
            raise ValueError(
                f"unknown {annotation.evidence_kind} annotation evidence for "
                f"{item.input_id}: {annotation.evidence_id}"
            )
    for fact in result.fact_proposals:
        if fact.evidence_id not in expected[fact.evidence_kind]:
            raise ValueError(
                f"unknown {fact.evidence_kind} fact evidence for "
                f"{item.input_id}: {fact.evidence_id}"
            )


def validate_worker_output_v2(dispatch: AgentDispatch, raw_output: object) -> WorkerOutputV2:
    output = WorkerOutputV2.model_validate(raw_output)
    batch = dispatch.batch
    analysis_window = batch.analysis_window
    if batch.schema_version != "2.0" or analysis_window is None:
        raise ValueError("v2 worker output requires a persisted v2 analysis window")
    if output.batch_id != batch.batch_id:
        raise ValueError("worker batch_id does not match the persisted batch")
    if output.analysis_id != analysis_window.analysis_id:
        raise ValueError("worker analysis_id does not match the persisted analysis window")

    persisted_items = {item.input_id: item for item in batch.items}
    result_items = {item.input_id: item for item in output.items}
    if set(result_items) != set(persisted_items):
        raise ValueError("worker output input_id set does not match the persisted batch")
    for input_id, result in result_items.items():
        _validate_item_evidence(persisted_items[input_id], result)

    reference_manifest = None
    if batch.reference_bundle_payload is not None:
        candidate = batch.reference_bundle_payload.get("manifest_sha256")
        if isinstance(candidate, str):
            reference_manifest = candidate
    for result in output.items:
        for proposal in result.spatial_proposals:
            if proposal.status == "ground_point" and (
                reference_manifest is None or proposal.reference_bundle_sha256 != reference_manifest
            ):
                raise ValueError(
                    f"spatial proposal {proposal.proposal_id} does not use the persisted "
                    "reference bundle"
                )

    model_runs = {run.model_role: run for run in output.model_runs}
    if len(model_runs) != len(output.model_runs):
        raise ValueError("worker output contains duplicate model roles")
    for role, revision in dispatch.expected_models.items():
        run = next(
            (candidate for candidate in output.model_runs if candidate.model_role == role),
            None,
        )
        if run is None:
            raise ValueError(f"worker output is missing expected model role: {role}")
        if run.revision != revision:
            raise ValueError(f"worker model revision mismatch for {role}")
    return output


def persist_worker_output_v2(
    session: Session,
    dispatch: AgentDispatch,
    output: WorkerOutputV2,
    *,
    worker_id: str,
) -> None:
    """Persist only private proposals and a versioned report draft.

    This function deliberately does not create public markers, zones, notes, or publications.
    """

    batch = dispatch.batch
    analysis_window = batch.analysis_window
    if analysis_window is None or batch.incident_id is None or batch.episode_id is None:
        raise ValueError("v2 persistence requires an incident analysis window")

    items_by_input = {item.input_id: item for item in batch.items}
    annotations_by_public_id: dict[str, AgentSourceAnnotation] = {}
    facts_by_public_id: dict[str, AgentFactProposal] = {}

    for result in output.items:
        source_item = items_by_input[result.input_id]
        for annotation in result.source_annotations:
            source_x, source_y = annotation.source_point_normalized
            stored = AgentSourceAnnotation(
                annotation_id=annotation.annotation_id,
                analysis_window_id=analysis_window.id,
                source_media_item_id=source_item.id,
                evidence_id=annotation.evidence_id,
                evidence_kind=annotation.evidence_kind,
                semantic_anchor=annotation.semantic_anchor,
                source_x_normalized=source_x,
                source_y_normalized=source_y,
                model_score=annotation.model_score,
            )
            session.add(stored)
            annotations_by_public_id[annotation.annotation_id] = stored
    session.flush()

    for result in output.items:
        source_item = items_by_input[result.input_id]
        for proposal in result.spatial_proposals:
            source_annotation = (
                annotations_by_public_id[proposal.annotation_id]
                if proposal.annotation_id is not None
                else None
            )
            session.add(
                AgentSpatialProposal(
                    proposal_id=proposal.proposal_id,
                    analysis_window_id=analysis_window.id,
                    source_media_item_id=source_item.id,
                    source_annotation_id=(source_annotation.id if source_annotation else None),
                    status=proposal.status,
                    observed_at=proposal.observed_at,
                    geometry_origin=proposal.geometry_origin,
                    longitude=proposal.longitude,
                    latitude=proposal.latitude,
                    altitude_m=proposal.altitude_m,
                    horizontal_accuracy_m=proposal.horizontal_accuracy_m,
                    reference_bundle_sha256=proposal.reference_bundle_sha256,
                    uncertainty_codes=list(proposal.uncertainty_codes),
                    review_state=AgentProposalReviewState.PENDING,
                    version=1,
                )
            )
        for fact in result.fact_proposals:
            stored_fact = AgentFactProposal(
                fact_id=fact.fact_id,
                analysis_window_id=analysis_window.id,
                source_media_item_id=source_item.id,
                category=fact.category,
                fact_key=fact.fact_key,
                as_of=fact.as_of,
                evidence_kind=fact.evidence_kind,
                evidence_id=fact.evidence_id,
                certainty=fact.certainty,
                value_number=fact.value_number,
                value_text=fact.value_text,
                value_boolean=fact.value_boolean,
                unit=fact.unit,
                summary=fact.summary,
                conflict_group_id=fact.conflict_group_id,
                review_state=AgentProposalReviewState.PENDING,
                version=1,
            )
            session.add(stored_fact)
            facts_by_public_id[fact.fact_id] = stored_fact
    session.flush()

    _refresh_daily_activity_zone(session, dispatch, worker_id=worker_id)

    if output.report_draft is not None:
        latest_revision = session.scalar(
            select(func.max(AgentSituationReportRevision.revision)).where(
                AgentSituationReportRevision.analysis_window_id == analysis_window.id
            )
        )
        report = AgentSituationReportRevision(
            report_revision_id=new_prefixed_id("SITREP"),
            analysis_window_id=analysis_window.id,
            incident_id=batch.incident_id,
            episode_id=batch.episode_id,
            revision=int(latest_revision or 0) + 1,
            title=output.report_draft.title,
            body_markdown=output.report_draft.body_markdown,
            sections_payload=json_safe(output.report_draft.sections),
            review_state=AgentReportReviewState.DRAFT,
            created_by=worker_id,
            reason="Private worker v2 situation report persisted for human review.",
        )
        referenced_fact_ids = {
            fact_id for section in output.report_draft.sections for fact_id in section.fact_ids
        }
        report.fact_links = [
            AgentSituationReportFact(fact=facts_by_public_id[fact_id])
            for fact_id in sorted(referenced_fact_ids)
        ]
        session.add(report)

    analysis_window.state = AgentAnalysisState.REVIEW_PENDING
