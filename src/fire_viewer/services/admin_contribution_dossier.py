"""Decision-oriented private projections for public contribution evidence.

This module deliberately maps stored proposals to human language. It never
returns tracking material, contact hashes, Blob URIs, prompt text or worker
payloads.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentFactProposal,
    AgentMediaBatch,
    AgentSituationReportRevision,
    AgentSpatialProposal,
    IncidentGalleryItem,
    PublicContributionSubmission,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.contribution_schemas import (
    AdminContributionGalleryProposalRequest,
    AdminContributionGalleryState,
    AdminContributionProposal,
    AdminContributionProposalReviewRequest,
    AdminPublicContributionDetail,
    AdminPublicContributionDetailEnvelope,
)
from fire_viewer.domain.enums import (
    AgentBatchState,
    AgentConsentState,
    AgentProposalReviewState,
    AgentReportReviewState,
    PublicContributionState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.services.agent_validation_campaigns import (
    refresh_campaign_day_publication_state,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.public_contributions import _admin_status, _load_contribution


def _batches_for(contribution: PublicContributionSubmission) -> list[AgentMediaBatch]:
    package = contribution.source_package
    if package is None:
        return []
    return [
        item.agent_media_item.batch
        for item in package.items
        if item.agent_media_item is not None and item.agent_media_item.batch is not None
    ]


def _analysis_state(batches: list[AgentMediaBatch]) -> str:
    if not batches:
        return "not_scheduled"
    states = {batch.state for batch in batches}
    if states.intersection(
        {AgentBatchState.QUEUED, AgentBatchState.SUBMITTING, AgentBatchState.RUNNING}
    ):
        return "running"
    if states == {AgentBatchState.DRAFT}:
        return "scheduled"
    if states.issubset(
        {
            AgentBatchState.CANCELLED,
            AgentBatchState.CANCEL_REQUESTED,
            AgentBatchState.FAILED,
            AgentBatchState.DEAD_LETTER,
        }
    ):
        return "blocked"
    return "completed"


def _proposals(session: Session, batches: list[AgentMediaBatch]) -> list[AdminContributionProposal]:
    media_ids = [item.id for batch in batches for item in batch.items]
    windows = [
        batch.analysis_window_id for batch in batches if batch.analysis_window_id is not None
    ]
    if not media_ids:
        return []
    facts = session.scalars(
        select(AgentFactProposal).where(AgentFactProposal.source_media_item_id.in_(media_ids))
    ).all()
    spatial = session.scalars(
        select(AgentSpatialProposal).where(AgentSpatialProposal.source_media_item_id.in_(media_ids))
    ).all()
    reports = (
        session.scalars(
            select(AgentSituationReportRevision).where(
                AgentSituationReportRevision.analysis_window_id.in_(windows)
            )
        ).all()
        if windows
        else []
    )
    result: list[AdminContributionProposal] = [
        *[
            AdminContributionProposal(
                proposal_id=item.fact_id,
                kind="fact",
                state=item.review_state.value,
                title="Fait proposé",
                summary=item.summary,
                confidence=item.certainty,
                observed_at=as_utc(item.as_of),
                proposed_at=as_utc(item.created_at),
                version=item.version,
            )
            for item in facts
        ],
        *[
            AdminContributionProposal(
                proposal_id=item.proposal_id,
                kind="spatial",
                state=item.review_state.value,
                title="Abstention spatiale"
                if item.status == "insufficient_geometry"
                else "Repère spatial proposé",
                summary=(
                    "La preuve ne permet pas de produire un repère spatial fiable."
                    if item.status == "insufficient_geometry"
                    else (
                        "Repère proposé avec une précision horizontale de "
                        f"{item.horizontal_accuracy_m:g} m."
                    )
                ),
                confidence=item.geometry_origin,
                observed_at=as_utc(item.observed_at) if item.observed_at else None,
                proposed_at=as_utc(item.created_at),
                version=item.version,
            )
            for item in spatial
        ],
        *[
            AdminContributionProposal(
                proposal_id=item.report_revision_id,
                kind="report",
                state=item.review_state.value,
                title="Rapport de situation proposé",
                summary=item.title,
                confidence=None,
                observed_at=None,
                proposed_at=as_utc(item.created_at),
                version=item.revision,
            )
            for item in reports
        ],
    ]
    return sorted(result, key=lambda item: (item.proposed_at, item.proposal_id), reverse=True)


def _gallery_state(contribution: PublicContributionSubmission) -> AdminContributionGalleryState:
    existing = next((item for item in contribution.gallery_items if item.state != "REJECTED"), None)
    package = contribution.source_package
    display_allowed = bool(
        package
        and any(
            item.agent_media_item
            and item.agent_media_item.purged_at is None
            and item.agent_media_item.consent is not None
            and item.agent_media_item.consent.state == AgentConsentState.GRANTED
            and "display_media" in item.agent_media_item.consent.scopes
            for item in package.items
        )
    )
    if existing is not None:
        return AdminContributionGalleryState(
            eligible=False,
            reason="Une proposition galerie existe déjà pour cette preuve.",
            gallery_item_id=existing.gallery_item_id,
            state=existing.state,
        )
    if contribution.state != PublicContributionState.ACCEPTED:
        return AdminContributionGalleryState(
            eligible=False,
            reason="La preuve doit être qualifiée avant toute proposition éditoriale.",
        )
    if not display_allowed:
        return AdminContributionGalleryState(
            eligible=False, reason="Le consentement d'affichage du média n'a pas été accordé."
        )
    return AdminContributionGalleryState(eligible=True)


def get_admin_public_contribution_detail(
    session: Session, *, contribution_id: str, settings: Settings, trace_id: str
) -> AdminPublicContributionDetailEnvelope:
    contribution = _load_contribution(session, contribution_id)
    batches = _batches_for(contribution)
    base = _admin_status(contribution, settings)
    episode_id = next(
        (batch.episode.episode_id for batch in batches if batch.episode is not None), None
    )
    return AdminPublicContributionDetailEnvelope(
        contribution=AdminPublicContributionDetail(
            **base.model_dump(),
            episode_id=episode_id,
            analysis_state=_analysis_state(batches),
            proposals=_proposals(session, batches),
            gallery=_gallery_state(contribution),
        ),
        trace_id=trace_id,
    )


def review_contribution_proposal(
    session: Session,
    *,
    contribution_id: str,
    kind: str,
    proposal_id: str,
    payload: AdminContributionProposalReviewRequest,
    actor: Actor,
    trace_id: str,
) -> None:
    contribution = _load_contribution(session, contribution_id, for_update=True)
    batches = _batches_for(contribution)
    media_ids = [item.id for batch in batches for item in batch.items]
    if kind == "fact":
        fact = session.scalar(
            select(AgentFactProposal).where(
                AgentFactProposal.fact_id == proposal_id,
                AgentFactProposal.source_media_item_id.in_(media_ids),
            )
        )
        if fact is None:
            raise NotFoundError("contribution_proposal", proposal_id)
        if fact.version != payload.expected_version:
            raise ConflictError(
                "agent_proposal_version_conflict",
                "La proposition a changé depuis son chargement.",
            )
        before = {"state": fact.review_state.value, "version": fact.version}
        fact.review_state = {
            "validate": AgentProposalReviewState.VALIDATED,
            "reject": AgentProposalReviewState.REJECTED,
            "invalidate": AgentProposalReviewState.INVALIDATED,
        }[payload.action]
        fact.reviewed_by = actor.actor_id
        fact.reviewed_at = utcnow()
        fact.review_reason = payload.reason
        fact.version += 1
        after_state = fact.review_state.value
        after_version = fact.version
    elif kind == "spatial":
        spatial = session.scalar(
            select(AgentSpatialProposal).where(
                AgentSpatialProposal.proposal_id == proposal_id,
                AgentSpatialProposal.source_media_item_id.in_(media_ids),
            )
        )
        if spatial is None:
            raise NotFoundError("contribution_proposal", proposal_id)
        if spatial.version != payload.expected_version:
            raise ConflictError(
                "agent_proposal_version_conflict",
                "La proposition a changé depuis son chargement.",
            )
        before = {"state": spatial.review_state.value, "version": spatial.version}
        spatial.review_state = {
            "validate": AgentProposalReviewState.VALIDATED,
            "reject": AgentProposalReviewState.REJECTED,
            "invalidate": AgentProposalReviewState.INVALIDATED,
        }[payload.action]
        spatial.reviewed_by = actor.actor_id
        spatial.reviewed_at = utcnow()
        spatial.review_reason = payload.reason
        spatial.version += 1
        after_state = spatial.review_state.value
        after_version = spatial.version
    elif kind == "report":
        windows = [
            batch.analysis_window_id for batch in batches if batch.analysis_window_id is not None
        ]
        report = (
            session.scalar(
                select(AgentSituationReportRevision).where(
                    AgentSituationReportRevision.report_revision_id == proposal_id,
                    AgentSituationReportRevision.analysis_window_id.in_(windows),
                )
            )
            if windows
            else None
        )
        if report is None:
            raise NotFoundError("contribution_proposal", proposal_id)
        if report.revision != payload.expected_version:
            raise ConflictError(
                "agent_proposal_version_conflict",
                "La proposition a changé depuis son chargement.",
            )
        before = {"state": report.review_state.value, "version": report.revision}
        report.review_state = {
            "validate": AgentReportReviewState.VALIDATED,
            "reject": AgentReportReviewState.REJECTED,
            "invalidate": AgentReportReviewState.INVALIDATED,
        }[payload.action]
        report.reviewed_by = actor.actor_id
        report.reviewed_at = utcnow()
        report.review_reason = payload.reason
        after_state = report.review_state.value
        after_version = report.revision
        if payload.action == "validate":
            refresh_campaign_day_publication_state(
                session,
                analysis_window_id=report.analysis_window_id,
            )
    else:
        raise NotFoundError("contribution_proposal", proposal_id)
    record_operator_audit(
        session,
        actor=actor,
        action=f"agent.proposal.{payload.action}",
        target_type=f"agent_{kind}_proposal",
        target_id=proposal_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after={"state": after_state, "version": after_version},
        payload={"published": False},
    )
    session.commit()


def propose_contribution_gallery_item(
    session: Session,
    *,
    contribution_id: str,
    payload: AdminContributionGalleryProposalRequest,
    actor: Actor,
    trace_id: str,
) -> AdminContributionGalleryState:
    begin_write_transaction(session)
    contribution = _load_contribution(session, contribution_id, for_update=True)
    state = _gallery_state(contribution)
    if not state.eligible:
        raise ConflictError(
            "contribution_gallery_ineligible", state.reason or "Cette preuve n'est pas éligible."
        )
    if contribution.incident is None:
        raise ConflictError(
            "contribution_gallery_incident_required",
            "Une preuve sans incident ne peut pas être proposée à la galerie.",
        )
    item = IncidentGalleryItem(
        gallery_item_id=new_prefixed_id("gallery"),
        incident_id=contribution.incident.id,
        source_contribution_id=contribution.id,
        title=payload.title,
        caption=payload.caption,
        alt_text=payload.alt_text,
        media_url=None,
        media_kind="image",
        credit=payload.credit,
        license_label=payload.license_label,
        state="PROPOSED",
        source_reference_url=None,
        proposal_reason=payload.proposal_reason,
        proposed_by=actor.actor_id,
    )
    session.add(item)
    session.flush()
    record_operator_audit(
        session,
        actor=actor,
        action="contribution.gallery.proposed",
        target_type="incident_gallery_item",
        target_id=item.gallery_item_id,
        reason=payload.proposal_reason,
        trace_id=trace_id,
        after={"state": item.state, "source_contribution_id": contribution.contribution_id},
        payload={"published": False},
    )
    session.commit()
    return AdminContributionGalleryState(
        eligible=False,
        reason=(
            "Proposition éditoriale créée ; le média reste privé jusqu'à la préparation éditoriale."
        ),
        gallery_item_id=item.gallery_item_id,
        state=item.state,
    )
