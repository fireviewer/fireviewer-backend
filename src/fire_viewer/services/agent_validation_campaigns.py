"""Internal, fail-closed ordering for historical validation campaigns.

The worker never receives a campaign flag or an arbitrary date.  It receives the
same immutable analysis-window identifier used by normal production batches.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentMediaBatch,
    AgentSituationReportRevision,
    AgentSourceResearchRun,
    AgentValidationCampaign,
    AgentValidationCampaignDay,
    Episode,
    IncidentMapCapture,
    IncidentSeries,
)
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    AgentAnalysisState,
    AgentBatchState,
    AgentBatchType,
    AgentReportReviewState,
    AgentSourceResearchState,
    AgentValidationCampaignDayState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.services.agent_source_packages import ensure_daily_analysis_window

_PARIS = ZoneInfo("Europe/Paris")
_ACTIVE_DAY_STATES = {
    AgentValidationCampaignDayState.READY,
    AgentValidationCampaignDayState.RUNNING,
    AgentValidationCampaignDayState.REVIEW,
}
_OPERATIONS = {"user_media", "source_research", "satellite_media"}
_TERMINAL_BATCH_STATES = {
    AgentBatchState.SUCCEEDED,
    AgentBatchState.PARTIAL_FAILURE,
    AgentBatchState.FAILED,
    AgentBatchState.DEAD_LETTER,
    AgentBatchState.CANCELLED,
}
_TERMINAL_RESEARCH_STATES = {
    AgentSourceResearchState.SUCCEEDED,
    AgentSourceResearchState.PARTIAL_FAILURE,
    AgentSourceResearchState.FAILED,
    AgentSourceResearchState.DEAD_LETTER,
    AgentSourceResearchState.CANCELLED,
}
_FAILED_STATE_VALUES = {
    AgentBatchState.FAILED.value,
    AgentBatchState.DEAD_LETTER.value,
    AgentSourceResearchState.FAILED.value,
    AgentSourceResearchState.DEAD_LETTER.value,
}
_PARTIAL_STATE_VALUES = {
    AgentBatchState.PARTIAL_FAILURE.value,
    AgentSourceResearchState.PARTIAL_FAILURE.value,
}
_CANCELLED_STATE_VALUES = {
    AgentBatchState.CANCELLED.value,
    AgentSourceResearchState.CANCELLED.value,
}


@dataclass(frozen=True, slots=True)
class ActiveAnalysisWindow:
    window: AgentAnalysisWindow
    campaign_day: AgentValidationCampaignDay | None


def _canonical_digest(payload: dict[str, Any], *, excluded_key: str) -> str:
    normalized = {key: value for key, value in payload.items() if key != excluded_key}
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _incident_episode(session: Session, fire_id: str) -> tuple[IncidentSeries, Episode]:
    incident = session.scalar(select(IncidentSeries).where(IncidentSeries.fire_id == fire_id))
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = session.scalar(
        select(Episode).where(
            Episode.incident_id == incident.id,
            Episode.is_current.is_(True),
        )
    )
    if episode is None:
        raise ConflictError("incident_without_current_episode", "Incident has no current episode.")
    return incident, episode


def active_campaign(session: Session) -> AgentValidationCampaign | None:
    return session.scalar(
        select(AgentValidationCampaign)
        .where(AgentValidationCampaign.is_active.is_(True))
        .options(
            selectinload(AgentValidationCampaign.days).selectinload(
                AgentValidationCampaignDay.analysis_window
            )
        )
    )


def resolve_active_analysis_window(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
) -> ActiveAnalysisWindow:
    campaign = active_campaign(session)
    if campaign is not None:
        active_days = [day for day in campaign.days if day.state in _ACTIVE_DAY_STATES]
        if len(active_days) != 1:
            raise ConflictError(
                "agent_campaign_active_window_invalid",
                "The internal campaign must expose exactly one active analysis window.",
            )
        day = active_days[0]
        window = day.analysis_window
        if window.incident_id != incident.id or window.episode_id != episode.id:
            raise ConflictError(
                "agent_campaign_incident_locked",
                "Another incident owns the only active campaign window.",
            )
        return ActiveAnalysisWindow(window=window, campaign_day=day)

    local_date = utcnow().astimezone(_PARIS).date()
    window = ensure_daily_analysis_window(
        session,
        incident=incident,
        episode=episode,
        local_date=local_date,
    )
    return ActiveAnalysisWindow(window=window, campaign_day=None)


def require_expected_window(
    active: ActiveAnalysisWindow,
    *,
    expected_analysis_window_id: str,
) -> None:
    if active.window.analysis_id != expected_analysis_window_id:
        raise ConflictError(
            "agent_analysis_window_stale",
            "The selected analysis window is no longer active.",
        )
    day = active.campaign_day
    if day is not None and day.state not in {
        AgentValidationCampaignDayState.READY,
        AgentValidationCampaignDayState.RUNNING,
    }:
        raise ConflictError(
            "agent_campaign_day_not_runnable",
            "The active campaign day is not runnable in its current state.",
        )


def mark_running(session: Session, active: ActiveAnalysisWindow) -> None:
    day = active.campaign_day
    if day is None or day.state == AgentValidationCampaignDayState.RUNNING:
        return
    if day.state != AgentValidationCampaignDayState.READY:
        raise ConflictError(
            "agent_campaign_day_not_ready",
            "The active campaign day is not ready.",
        )
    day.state = AgentValidationCampaignDayState.RUNNING
    day.activated_at = day.activated_at or utcnow()
    day.version += 1
    session.flush()


def batch_is_allowed_for_active_campaign(
    batch: AgentMediaBatch,
    active: ActiveAnalysisWindow,
) -> bool:
    day = active.campaign_day
    if day is None:
        return True
    expected = set(day.allowed_media_sha256)
    actual = {item.media_sha256 for item in batch.items if item.media_sha256 is not None}
    return bool(actual) and actual.issubset(expected)


def refresh_campaign_day_review_state(
    session: Session,
    *,
    analysis_window_id: int,
) -> bool:
    """Move a day to review once every required operation is terminal.

    The gate never depends on media or proposal counts and never requires success.
    """

    day = session.scalar(
        select(AgentValidationCampaignDay)
        .where(AgentValidationCampaignDay.analysis_window_id == analysis_window_id)
        .options(selectinload(AgentValidationCampaignDay.analysis_window))
    )
    if day is None or day.state != AgentValidationCampaignDayState.RUNNING:
        return False
    batches = list(
        session.scalars(
            select(AgentMediaBatch).where(AgentMediaBatch.analysis_window_id == analysis_window_id)
        )
    )
    research_runs = list(
        session.scalars(
            select(AgentSourceResearchRun).where(
                AgentSourceResearchRun.analysis_window_id == analysis_window_id
            )
        )
    )

    declared_absences = set(day.declared_absences)

    def summarize(
        operation: str,
        states: list[str],
        *,
        present: bool,
        terminal: bool,
    ) -> dict[str, Any]:
        if operation in declared_absences:
            return {"outcome": "absent", "terminal": True, "states": []}
        if not present:
            return {"outcome": "not_started", "terminal": False, "states": []}
        if not terminal:
            return {"outcome": "running", "terminal": False, "states": states}
        if any(state in _FAILED_STATE_VALUES for state in states):
            outcome = "failed"
        elif any(state in _PARTIAL_STATE_VALUES for state in states):
            outcome = "partial_failure"
        elif states and all(state in _CANCELLED_STATE_VALUES for state in states):
            outcome = "cancelled"
        elif any(state in _CANCELLED_STATE_VALUES for state in states):
            outcome = "partial_failure"
        else:
            outcome = "succeeded"
        return {"outcome": outcome, "terminal": True, "states": states}

    def batch_outcome(operation: str, batch_type: AgentBatchType) -> dict[str, Any]:
        matching = [batch for batch in batches if batch.batch_type == batch_type]
        states = [batch.state.value for batch in matching]
        return summarize(
            operation,
            states,
            present=bool(matching),
            terminal=bool(matching)
            and all(batch.state in _TERMINAL_BATCH_STATES for batch in matching),
        )

    external_batches = [
        batch for batch in batches if batch.batch_type == AgentBatchType.EXTERNAL_MEDIA
    ]
    research_states = [run.state.value for run in research_runs]
    external_states = [batch.state.value for batch in external_batches]
    operation_outcomes = {
        "user_media": batch_outcome("user_media", AgentBatchType.USER_MEDIA),
        "satellite_media": batch_outcome("satellite_media", AgentBatchType.SATELLITE_MEDIA),
        "source_research": summarize(
            "source_research",
            research_states + external_states,
            present=bool(research_runs),
            terminal=bool(research_runs)
            and all(run.state in _TERMINAL_RESEARCH_STATES for run in research_runs)
            and all(batch.state in _TERMINAL_BATCH_STATES for batch in external_batches),
        ),
    }
    if not all(
        bool(operation_outcomes[operation]["terminal"]) for operation in day.required_operations
    ):
        return False

    from fire_viewer.services.agent_daily_consolidation import (
        consolidate_daily_intelligence,
    )

    consolidate_daily_intelligence(
        session,
        analysis_window_id=analysis_window_id,
        operation_outcomes=operation_outcomes,
    )
    day.state = AgentValidationCampaignDayState.REVIEW
    day.analysis_window.state = AgentAnalysisState.REVIEW_PENDING
    day.version += 1
    session.flush()
    return True


def refresh_campaign_day_publication_state(
    session: Session,
    *,
    analysis_window_id: int,
) -> bool:
    """Publish the campaign gate and unlock its successor after all human gates."""

    day = session.scalar(
        select(AgentValidationCampaignDay)
        .where(AgentValidationCampaignDay.analysis_window_id == analysis_window_id)
        .options(
            selectinload(AgentValidationCampaignDay.analysis_window),
            selectinload(AgentValidationCampaignDay.campaign).selectinload(
                AgentValidationCampaign.days
            ),
        )
    )
    if day is None or day.state != AgentValidationCampaignDayState.REVIEW:
        return False
    zone = session.scalar(
        select(ActiveFireZoneRevision)
        .where(
            ActiveFireZoneRevision.analysis_window_id == analysis_window_id,
            ActiveFireZoneRevision.review_state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
        )
        .order_by(ActiveFireZoneRevision.revision.desc())
        .limit(1)
    )
    report = session.scalar(
        select(AgentSituationReportRevision)
        .where(
            AgentSituationReportRevision.analysis_window_id == analysis_window_id,
            AgentSituationReportRevision.review_state == AgentReportReviewState.VALIDATED,
        )
        .order_by(AgentSituationReportRevision.revision.desc())
        .limit(1)
    )
    capture = (
        session.scalar(
            select(IncidentMapCapture)
            .where(
                IncidentMapCapture.active_zone_revision_id == zone.id,
                IncidentMapCapture.local_date == day.analysis_window.local_date,
            )
            .limit(1)
        )
        if zone is not None
        else None
    )
    if zone is None or report is None or capture is None:
        return False

    now = utcnow()
    day.state = AgentValidationCampaignDayState.PUBLISHED
    day.finished_at = now
    day.analysis_window.state = AgentAnalysisState.COMPLETED
    day.version += 1
    next_day = next(
        (
            candidate
            for candidate in sorted(day.campaign.days, key=lambda item: item.ordinal)
            if candidate.ordinal == day.ordinal + 1
        ),
        None,
    )
    if next_day is None:
        day.campaign.is_active = False
        day.campaign.version += 1
    else:
        next_day.state = AgentValidationCampaignDayState.READY
        next_day.activated_at = now
        next_day.version += 1
    session.flush()
    return True


def create_campaign_from_manifest(
    session: Session,
    *,
    manifest_path: Path,
    created_by: str,
) -> AgentValidationCampaign:
    """Persist an ordered campaign from one already-built immutable V2 manifest."""

    raw = manifest_path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") != "2.0":
        raise ValueError("campaign manifest must use schema_version 2.0")
    days = payload.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError("campaign manifest must contain at least one day")
    declared_campaign_hash = payload.get("manifest_sha256")
    actual_campaign_hash = _canonical_digest(payload, excluded_key="manifest_sha256")
    if declared_campaign_hash != actual_campaign_hash:
        raise ValueError("campaign manifest SHA-256 does not match its canonical payload")
    if active_campaign(session) is not None:
        raise ConflictError(
            "agent_campaign_already_active",
            "An internal validation campaign is already active.",
        )

    campaign_id = str(payload.get("campaign_id") or "")
    if not campaign_id or len(campaign_id) > 128:
        raise ValueError("campaign_id is required and must be at most 128 characters")
    campaign = AgentValidationCampaign(
        campaign_id=campaign_id,
        manifest_sha256=actual_campaign_hash,
        is_active=True,
        created_by=created_by,
        version=1,
    )
    session.add(campaign)
    session.flush()

    seen_hashes: set[str] = set()
    for ordinal, raw_day in enumerate(days, start=1):
        if not isinstance(raw_day, dict):
            raise ValueError("every campaign day must be an object")
        if raw_day.get("ordinal") != ordinal:
            raise ValueError("campaign day ordinals must be contiguous and ordered")
        day_hash = _canonical_digest(raw_day, excluded_key="manifest_sha256")
        if raw_day.get("manifest_sha256") != day_hash:
            raise ValueError(f"campaign day {ordinal} SHA-256 does not match")
        fire_id = str(raw_day.get("fire_id") or "")
        incident, episode = _incident_episode(session, fire_id)
        local_date = date.fromisoformat(str(raw_day["local_date"]))
        cutoff_at = datetime.fromisoformat(str(raw_day["cutoff_at"]).replace("Z", "+00:00"))
        if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
            raise ValueError(f"campaign day {ordinal} cutoff_at must be timezone-aware")
        allowed_hashes = raw_day.get("allowed_media_sha256")
        if not isinstance(allowed_hashes, list) or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in allowed_hashes
        ):
            raise ValueError(f"campaign day {ordinal} has invalid media hashes")
        if seen_hashes.intersection(allowed_hashes):
            raise ValueError("the same media SHA-256 cannot feed two campaign days")
        seen_hashes.update(allowed_hashes)
        required = raw_day.get("required_operations")
        if not isinstance(required, list) or not required or set(required).difference(_OPERATIONS):
            raise ValueError(f"campaign day {ordinal} has invalid required operations")
        absences = raw_day.get("declared_absences", [])
        if (
            not isinstance(absences, list)
            or any(not isinstance(value, str) for value in absences)
            or set(absences).difference(_OPERATIONS)
            or set(absences).intersection(required)
        ):
            raise ValueError(f"campaign day {ordinal} has invalid declared absences")
        window = ensure_daily_analysis_window(
            session,
            incident=incident,
            episode=episode,
            local_date=local_date,
        )
        if as_utc(window.window_end_at) != as_utc(cutoff_at):
            raise ValueError(f"campaign day {ordinal} cutoff must equal the immutable window end")
        session.add(
            AgentValidationCampaignDay(
                campaign_day_id=new_prefixed_id("campaign-day"),
                campaign_id=campaign.id,
                analysis_window_id=window.id,
                ordinal=ordinal,
                cutoff_at=cutoff_at,
                manifest_sha256=day_hash,
                allowed_media_sha256=sorted(set(allowed_hashes)),
                required_operations=list(dict.fromkeys(required)),
                declared_absences=list(dict.fromkeys(absences)),
                state=(
                    AgentValidationCampaignDayState.READY
                    if ordinal == 1
                    else AgentValidationCampaignDayState.LOCKED
                ),
                activated_at=utcnow() if ordinal == 1 else None,
                version=1,
            )
        )
    session.commit()
    session.refresh(campaign)
    return campaign
