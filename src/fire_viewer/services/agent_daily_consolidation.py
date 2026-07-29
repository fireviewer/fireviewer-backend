"""Idempotent consolidation of one incident-analysis window.

The gate is contractual, never quantitative: once every required operation has
reached a terminal state (or an origin is explicitly absent), this module turns
the available private evidence into one reviewable report and, when geometry is
available, one editable activity-zone draft. Empty results and failures remain
first-class review material.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentDispatch,
    AgentFactProposal,
    AgentMediaBatch,
    AgentSituationReportFact,
    AgentSituationReportRevision,
    AgentSpatialProposal,
)
from fire_viewer.domain.enums import (
    ActorType,
    AgentProposalReviewState,
    AgentReportReviewState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.services.agent_intelligence import _refresh_daily_activity_zone
from fire_viewer.services.common import record_operator_audit

_CONSOLIDATOR_ID = "daily-intelligence-consolidator"
_CERTAINTY_ORDER = {
    "directly_visible": 0,
    "explicitly_written": 1,
    "explicitly_spoken": 2,
}


@dataclass(frozen=True, slots=True)
class DailyConsolidationResult:
    report: AgentSituationReportRevision
    activity_zone: ActiveFireZoneRevision | None
    fingerprint: str
    contradiction_count: int


def _fact_value(fact: AgentFactProposal) -> dict[str, Any]:
    if fact.value_number is not None:
        return {"kind": "number", "value": fact.value_number, "unit": fact.unit}
    if fact.value_boolean is not None:
        return {"kind": "boolean", "value": fact.value_boolean}
    return {"kind": "text", "value": fact.value_text}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _select_facts(
    facts: list[AgentFactProposal],
) -> tuple[list[AgentFactProposal], list[dict[str, Any]]]:
    """Collapse exact duplicates without hiding contradictory sourced values."""

    exact: dict[str, AgentFactProposal] = {}
    for fact in facts:
        signature = _canonical_json(
            {
                "source_media_item_id": fact.source_media_item_id,
                "evidence_kind": fact.evidence_kind,
                "evidence_id": fact.evidence_id,
                "category": fact.category,
                "fact_key": fact.fact_key,
                "as_of": fact.as_of.isoformat(),
                "value": _fact_value(fact),
                "summary": fact.summary.strip(),
            }
        )
        incumbent = exact.get(signature)
        if incumbent is None or _CERTAINTY_ORDER.get(fact.certainty, 99) < _CERTAINTY_ORDER.get(
            incumbent.certainty, 99
        ):
            exact[signature] = fact

    selected = sorted(exact.values(), key=lambda item: (item.as_of, item.fact_id))
    groups: dict[tuple[str, str, str], list[AgentFactProposal]] = defaultdict(list)
    for fact in selected:
        groups[(fact.category, fact.fact_key, fact.as_of.date().isoformat())].append(fact)

    contradictions: list[dict[str, Any]] = []
    for (category, fact_key, local_date), candidates in sorted(groups.items()):
        distinct_values = {_canonical_json(_fact_value(candidate)) for candidate in candidates}
        if len(distinct_values) <= 1:
            continue
        contradictions.append(
            {
                "category": category,
                "fact_key": fact_key,
                "local_date": local_date,
                "fact_ids": [candidate.fact_id for candidate in candidates],
                "values": [_fact_value(candidate) for candidate in candidates],
                "requires_final_judge": True,
            }
        )
    return selected, contradictions


def _select_spatial_proposals(
    proposals: list[AgentSpatialProposal],
) -> list[AgentSpatialProposal]:
    exact: dict[str, AgentSpatialProposal] = {}
    for proposal in proposals:
        signature = _canonical_json(
            {
                "source_media_item_id": proposal.source_media_item_id,
                "source_annotation_id": proposal.source_annotation_id,
                "status": proposal.status,
                "proposal_kind": proposal.proposal_kind,
                "observed_at": (proposal.observed_at.isoformat() if proposal.observed_at else None),
                "geometry": proposal.geometry_geojson,
                "origin": proposal.geometry_origin,
                "accuracy_m": proposal.horizontal_accuracy_m,
                "uncertainty_codes": sorted(proposal.uncertainty_codes),
                "reference_bundle_sha256": proposal.reference_bundle_sha256,
            }
        )
        exact.setdefault(signature, proposal)
    return sorted(exact.values(), key=lambda item: item.proposal_id)


def _existing_fingerprint(report: AgentSituationReportRevision) -> str | None:
    for section in report.sections_payload:
        if section.get("key") == "_daily_consolidation":
            candidate = section.get("fingerprint")
            return candidate if isinstance(candidate, str) else None
    return None


def _plain_summary(value: str) -> str:
    return " ".join(value.strip().split())


def _operation_lines(operation_outcomes: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for operation, outcome in sorted(operation_outcomes.items()):
        state = str(outcome.get("outcome") or "unknown")
        details = outcome.get("states")
        suffix = (
            f" ({', '.join(str(value) for value in details)})"
            if isinstance(details, list) and details
            else ""
        )
        lines.append(f"- `{operation}` : **{state}**{suffix}")
    return lines


def consolidate_daily_intelligence(
    session: Session,
    *,
    analysis_window_id: int,
    operation_outcomes: dict[str, dict[str, Any]],
    worker_id: str = _CONSOLIDATOR_ID,
) -> DailyConsolidationResult:
    """Create one current consolidation for all evidence currently available."""

    window = session.get(AgentAnalysisWindow, analysis_window_id)
    if window is None:
        raise NotFoundError("agent_analysis_window", str(analysis_window_id))
    batches = list(
        session.scalars(
            select(AgentMediaBatch)
            .where(AgentMediaBatch.analysis_window_id == analysis_window_id)
            .order_by(AgentMediaBatch.id.asc())
        )
    )
    facts = list(
        session.scalars(
            select(AgentFactProposal)
            .where(
                AgentFactProposal.analysis_window_id == analysis_window_id,
                AgentFactProposal.review_state.notin_(
                    [
                        AgentProposalReviewState.REJECTED,
                        AgentProposalReviewState.INVALIDATED,
                    ]
                ),
            )
            .order_by(AgentFactProposal.id.asc())
        )
    )
    proposals = list(
        session.scalars(
            select(AgentSpatialProposal)
            .where(
                AgentSpatialProposal.analysis_window_id == analysis_window_id,
                AgentSpatialProposal.review_state.notin_(
                    [
                        AgentProposalReviewState.REJECTED,
                        AgentProposalReviewState.INVALIDATED,
                    ]
                ),
            )
            .order_by(AgentSpatialProposal.id.asc())
        )
    )
    selected_facts, contradictions = _select_facts(facts)
    selected_proposals = _select_spatial_proposals(proposals)
    spatial_counts = Counter(
        proposal.proposal_kind or proposal.status for proposal in selected_proposals
    )
    fingerprint_payload = {
        "analysis_window_id": analysis_window_id,
        "operation_outcomes": operation_outcomes,
        "batch_ids": sorted(batch.batch_id for batch in batches),
        "fact_ids": [fact.fact_id for fact in selected_facts],
        "proposal_ids": [proposal.proposal_id for proposal in selected_proposals],
        "contradictions": contradictions,
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_payload).encode("utf-8")).hexdigest()

    latest = session.scalar(
        select(AgentSituationReportRevision)
        .where(AgentSituationReportRevision.analysis_window_id == analysis_window_id)
        .order_by(AgentSituationReportRevision.revision.desc())
        .limit(1)
    )
    if latest is not None and _existing_fingerprint(latest) == fingerprint:
        zone = session.scalar(
            select(ActiveFireZoneRevision)
            .where(ActiveFireZoneRevision.analysis_window_id == analysis_window_id)
            .order_by(ActiveFireZoneRevision.revision.desc())
            .limit(1)
        )
        return DailyConsolidationResult(
            report=latest,
            activity_zone=zone,
            fingerprint=fingerprint,
            contradiction_count=len(contradictions),
        )
    if latest is not None and latest.review_state != AgentReportReviewState.DRAFT:
        raise ConflictError(
            "agent_daily_consolidation_already_reviewed",
            "Reviewed daily intelligence cannot be replaced by an automatic retry.",
        )
    if latest is not None:
        latest.review_state = AgentReportReviewState.INVALIDATED
        latest.reviewed_by = worker_id
        latest.reviewed_at = utcnow()
        latest.review_reason = (
            "Superseded by a new idempotent consolidation after a targeted retry."
        )

    operation_lines = _operation_lines(operation_outcomes)
    fact_lines = [f"- {_plain_summary(fact.summary)} (`{fact.fact_id}`)" for fact in selected_facts]
    spatial_lines = [
        (f"- `{proposal.proposal_kind or proposal.status}` (`{proposal.proposal_id}`)")
        for proposal in selected_proposals
    ]
    contradiction_lines = [
        (
            f"- `{item['category']}.{item['fact_key']}` : "
            f"{len(item['fact_ids'])} versions sourcées à arbitrer"
        )
        for item in contradictions
    ]
    body = "\n\n".join(
        [
            "## État des opérations\n"
            + ("\n".join(operation_lines) if operation_lines else "- Aucune opération déclarée."),
            "## Faits sourcés\n"
            + (
                "\n".join(fact_lines)
                if fact_lines
                else "- Aucun fait exploitable produit ; ce vide est conservé."
            ),
            "## Propositions spatiales\n"
            + (
                "\n".join(spatial_lines)
                if spatial_lines
                else "- Aucune géométrie exploitable ; abstention conservée."
            ),
            "## Contradictions\n"
            + (
                "\n".join(contradiction_lines)
                if contradiction_lines
                else "- Aucune contradiction détectée dans les propositions disponibles."
            ),
            (
                "## Limites\n"
                "- Brouillon privé. Une opération échouée, une absence ou une abstention "
                "n'est pas remplacée par une donnée inventée.\n"
                "- Validation humaine obligatoire avant toute publication."
            ),
        ]
    )
    metadata = {
        "key": "_daily_consolidation",
        "heading": "Consolidation quotidienne",
        "fingerprint": fingerprint,
        "analysis_id": window.analysis_id,
        "local_date": window.local_date.isoformat(),
        "operation_outcomes": operation_outcomes,
        "fact_ids": [fact.fact_id for fact in selected_facts],
        "spatial_proposal_ids": [proposal.proposal_id for proposal in selected_proposals],
        "spatial_counts": dict(sorted(spatial_counts.items())),
        "contradictions": contradictions,
        "basis_codes": ["required_operations_terminal"],
    }
    latest_revision = session.scalar(
        select(func.max(AgentSituationReportRevision.revision)).where(
            AgentSituationReportRevision.analysis_window_id == analysis_window_id
        )
    )
    report = AgentSituationReportRevision(
        report_revision_id=new_prefixed_id("SITREP"),
        analysis_window_id=window.id,
        incident_id=window.incident_id,
        episode_id=window.episode_id,
        revision=int(latest_revision or 0) + 1,
        title=f"Situation du {window.local_date.isoformat()}",
        body_markdown=body,
        sections_payload=[metadata],
        review_state=AgentReportReviewState.DRAFT,
        supersedes_report_id=latest.id if latest is not None else None,
        created_by=worker_id,
        reason=(
            "Daily intelligence consolidated after every required operation "
            "reached a terminal outcome."
        ),
    )
    report.fact_links = [AgentSituationReportFact(fact=fact) for fact in selected_facts]
    session.add(report)
    session.flush()

    latest_dispatch = session.scalar(
        select(AgentDispatch)
        .join(AgentMediaBatch, AgentDispatch.batch_id == AgentMediaBatch.id)
        .where(AgentMediaBatch.analysis_window_id == analysis_window_id)
        .order_by(AgentDispatch.id.desc())
        .limit(1)
    )
    zone = (
        _refresh_daily_activity_zone(session, latest_dispatch, worker_id=worker_id)
        if latest_dispatch is not None
        else None
    )
    trace_id = (
        latest_dispatch.batch.trace_id
        if latest_dispatch is not None
        else f"analysis:{window.analysis_id}"
    )
    record_operator_audit(
        session,
        actor=Actor(actor_id=worker_id, roles=frozenset(), actor_type=ActorType.SYSTEM),
        action="agent.daily_intelligence_consolidated",
        target_type="agent_analysis_window",
        target_id=window.analysis_id,
        reason=report.reason,
        trace_id=trace_id,
        after={
            "report_revision_id": report.report_revision_id,
            "fingerprint": fingerprint,
            "operation_outcomes": operation_outcomes,
            "fact_count": len(selected_facts),
            "spatial_proposal_count": len(selected_proposals),
            "contradiction_count": len(contradictions),
            "activity_zone_revision_id": zone.zone_revision_id if zone else None,
        },
    )
    return DailyConsolidationResult(
        report=report,
        activity_zone=zone,
        fingerprint=fingerprint,
        contradiction_count=len(contradictions),
    )
