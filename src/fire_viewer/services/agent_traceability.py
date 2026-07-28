"""Read-only source-to-geometry trace for private spatial proposals."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fire_viewer.db.models import AgentAnalysisWindow, AgentMediaItem, AgentSpatialProposal
from fire_viewer.domain.agent_schemas import (
    SpatialProposalTraceAnnotationV2,
    SpatialProposalTraceSourceV2,
    SpatialProposalTraceV2,
    SpatialProposalTraceWindowV2,
)


def get_spatial_proposal_trace(
    session: Session,
    *,
    proposal_id: str,
) -> SpatialProposalTraceV2 | None:
    """Return the complete private lineage without exposing a signed media URL."""

    proposal = session.scalar(
        select(AgentSpatialProposal)
        .where(AgentSpatialProposal.proposal_id == proposal_id)
        .options(
            joinedload(AgentSpatialProposal.analysis_window).joinedload(
                AgentAnalysisWindow.incident
            ),
            joinedload(AgentSpatialProposal.analysis_window).joinedload(
                AgentAnalysisWindow.episode
            ),
            joinedload(AgentSpatialProposal.source_media_item).joinedload(
                AgentMediaItem.batch
            ),
            joinedload(AgentSpatialProposal.source_annotation),
        )
    )
    if proposal is None:
        return None

    analysis_window = proposal.analysis_window
    source_item = proposal.source_media_item
    provenance = source_item.metadata_payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    annotation = proposal.source_annotation

    return SpatialProposalTraceV2(
        proposal_id=proposal.proposal_id,
        status=proposal.status,
        proposal_kind=proposal.proposal_kind,
        geometry_geojson=proposal.geometry_geojson,
        geometry_origin=proposal.geometry_origin,
        horizontal_accuracy_m=proposal.horizontal_accuracy_m,
        reference_bundle_sha256=proposal.reference_bundle_sha256,
        uncertainty_codes=list(proposal.uncertainty_codes),
        review_state=proposal.review_state.value,
        version=proposal.version,
        analysis_window=SpatialProposalTraceWindowV2(
            analysis_id=analysis_window.analysis_id,
            fire_id=analysis_window.incident.fire_id,
            episode_id=analysis_window.episode.episode_id,
            local_date=analysis_window.local_date,
            window_start_at=analysis_window.window_start_at,
            window_end_at=analysis_window.window_end_at,
            timezone=analysis_window.timezone,
        ),
        source=SpatialProposalTraceSourceV2(
            batch_id=source_item.batch.batch_id,
            input_id=source_item.input_id,
            media_type=source_item.media_type,
            media_sha256=source_item.media_sha256,
            source_key=provenance.get("source_key"),
            source_reference_url=provenance.get("source_reference_url"),
            license_identifier=provenance.get("license_identifier"),
            attribution=provenance.get("attribution"),
            trust=provenance.get("trust"),
        ),
        annotation=(
            SpatialProposalTraceAnnotationV2(
                annotation_id=annotation.annotation_id,
                evidence_id=annotation.evidence_id,
                evidence_kind=annotation.evidence_kind,
                semantic_anchor=annotation.semantic_anchor,
                source_geometry_normalized=annotation.source_geometry_normalized,
                model_score=annotation.model_score,
            )
            if annotation is not None
            else None
        ),
    )
