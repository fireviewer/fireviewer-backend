"""Restore the reviewed Die retrospective into the current public-day contract.

This CLI accepts an already reviewed JSON snapshot, creates the missing
immutable analysis windows/campaign-day gates, and preserves the two separate
layers for each date: daily active area and cumulative burned footprint.  The
production migration gate invokes it only with the immutable packaged manifest.

Dry-run is the default.  Applying requires an explicit actor and a database
URL supplied through the normal environment or an untracked env file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.time import utcnow
from fire_viewer.db.engine import create_db_engine, create_session_factory
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentSituationReportRevision,
    AgentValidationCampaign,
    AgentValidationCampaignDay,
    Episode,
    IncidentSeries,
)
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    AgentAnalysisState,
    AgentReportReviewState,
    AgentValidationCampaignDayState,
)

DATASET_ID = "die-2026-retrospective-v1"
FIRE_ID = "FR-26-00001"
EPISODE_ID = "E01"
CAMPAIGN_ID = "campaign-die-retrospective-v2"
CREATED_BY = "fireviewer-retrospective-builder"


class RetrospectiveConflictError(RuntimeError):
    """A deterministic identifier is already attached to another entity."""


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_payload(path: Path) -> dict[str, Any]:
    raw_payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise RetrospectiveConflictError("The Die retrospective manifest must be an object.")
    payload = cast(dict[str, Any], raw_payload)
    activity_zones = payload.get("activity_zones")
    reports = payload.get("reports")
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("dataset_id") != DATASET_ID
        or payload.get("incident") != {"fire_id": FIRE_ID, "episode_id": EPISODE_ID}
        or not isinstance(activity_zones, list)
        or not isinstance(reports, list)
        or len(activity_zones) != 21
        or len(reports) != 21
        or [item.get("local_date") for item in activity_zones]
        != [item.get("local_date") for item in reports]
    ):
        raise RetrospectiveConflictError("Unexpected Die retrospective dataset contract.")
    for activity, report in zip(activity_zones, reports, strict=True):
        if (
            not isinstance(activity.get("geometry_geojson"), dict)
            or not activity["geometry_geojson"].get("coordinates")
            or not isinstance(report.get("sections"), list)
            or not isinstance(report.get("summary"), str)
        ):
            raise RetrospectiveConflictError(
                f"Incomplete retrospective day {activity.get('local_date')!r}."
            )
    return payload


def _incident_and_episode(session: Session) -> tuple[IncidentSeries, Episode]:
    incident = session.scalar(select(IncidentSeries).where(IncidentSeries.fire_id == FIRE_ID))
    if incident is None:
        raise RetrospectiveConflictError(f"Incident {FIRE_ID} does not exist.")
    episode = session.scalar(
        select(Episode).where(Episode.incident_id == incident.id, Episode.episode_id == EPISODE_ID)
    )
    if episode is None:
        raise RetrospectiveConflictError(f"Episode {FIRE_ID}/{EPISODE_ID} does not exist.")
    return incident, episode


def _ensure_windows(
    session: Session, incident: IncidentSeries, episode: Episode, payload: dict[str, Any]
) -> tuple[dict[date, AgentAnalysisWindow], int]:
    windows: dict[date, AgentAnalysisWindow] = {}
    created = 0
    for report in payload["reports"]:
        local_date = date.fromisoformat(report["local_date"])
        window = session.scalar(
            select(AgentAnalysisWindow).where(
                AgentAnalysisWindow.incident_id == incident.id,
                AgentAnalysisWindow.episode_id == episode.id,
                AgentAnalysisWindow.local_date == local_date,
            )
        )
        if window is None:
            start = datetime.combine(local_date, time.min, tzinfo=UTC)
            window = AgentAnalysisWindow(
                analysis_id=f"analysis-{FIRE_ID.lower()}-{local_date.isoformat()}-retrospective-v2",
                incident_id=incident.id,
                episode_id=episode.id,
                window_start_at=start,
                window_end_at=start + timedelta(days=1),
                local_date=local_date,
                timezone="Europe/Paris",
                state=AgentAnalysisState.COMPLETED,
                version=1,
            )
            session.add(window)
            session.flush()
            created += 1
        windows[local_date] = window
    return windows, created


def _ensure_reports(
    session: Session,
    incident: IncidentSeries,
    episode: Episode,
    windows: dict[date, AgentAnalysisWindow],
    payload: dict[str, Any],
    actor: str,
    reviewed_at: datetime,
) -> int:
    created = 0
    for report in payload["reports"]:
        local_date = date.fromisoformat(report["local_date"])
        window = windows[local_date]
        existing = session.scalar(
            select(AgentSituationReportRevision)
            .where(
                AgentSituationReportRevision.analysis_window_id == window.id,
                AgentSituationReportRevision.review_state == AgentReportReviewState.VALIDATED,
            )
            .order_by(AgentSituationReportRevision.revision.desc())
            .limit(1)
        )
        if existing is not None:
            continue
        revision = (
            session.scalar(
                select(func.max(AgentSituationReportRevision.revision)).where(
                    AgentSituationReportRevision.analysis_window_id == window.id
                )
            )
            or 0
        ) + 1
        session.add(
            AgentSituationReportRevision(
                report_revision_id=f"report-{FIRE_ID.lower()}-{local_date.isoformat()}-retrospective-v2",
                analysis_window_id=window.id,
                incident_id=incident.id,
                episode_id=episode.id,
                revision=revision,
                title=report["title"],
                body_markdown=report["summary"],
                sections_payload=report["sections"],
                review_state=AgentReportReviewState.VALIDATED,
                created_by=CREATED_BY,
                reason="Reconstitution documentaire post-incident relue avant publication.",
                reviewed_by=actor,
                reviewed_at=reviewed_at,
                review_reason="Chronologie, libellés, sources et limites rétrospectives validés.",
            )
        )
        created += 1
    return created


def _next_revision(session: Session, incident_id: int, episode_id: int, zone_kind: str) -> int:
    return (
        session.scalar(
            select(func.max(ActiveFireZoneRevision.revision)).where(
                ActiveFireZoneRevision.incident_id == incident_id,
                ActiveFireZoneRevision.episode_id == episode_id,
                ActiveFireZoneRevision.zone_kind == zone_kind,
            )
        )
        or 0
    ) + 1


def _has_ready_zone(session: Session, *, window_id: int, zone_kind: str) -> bool:
    return session.scalar(
        select(ActiveFireZoneRevision.id).where(
            ActiveFireZoneRevision.analysis_window_id == window_id,
            ActiveFireZoneRevision.zone_kind == zone_kind,
            ActiveFireZoneRevision.review_state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
        ).limit(1)
    ) is not None


def _active_reason(activity: dict[str, Any]) -> str:
    reason = "Zone active datée reconstituée depuis les références de la journée."
    basis = activity.get("basis")
    if isinstance(basis, str) and basis.strip():
        reason += f" Méthode : {basis.strip()}."
    confidence = activity.get("confidence")
    if isinstance(confidence, str) and confidence.strip():
        reason += f" Confiance : {confidence.strip()}."
    return reason


def _ensure_zones(
    session: Session,
    incident: IncidentSeries,
    episode: Episode,
    windows: dict[date, AgentAnalysisWindow],
    payload: dict[str, Any],
    actor: str,
    reviewed_at: datetime,
) -> dict[str, int]:
    created = {"active": 0, "burned": 0}
    next_revisions = {
        "active": _next_revision(session, incident.id, episode.id, "active"),
        "burned": _next_revision(session, incident.id, episode.id, "burned"),
    }
    carried_burned_geometry: dict[str, Any] | None = None
    for activity in payload["activity_zones"]:
        local_date = date.fromisoformat(activity["local_date"])
        window = windows[local_date]
        daily_geometry = activity["geometry_geojson"]
        embedded_burned_geometry = daily_geometry.get("global_footprint_geojson")
        if (
            isinstance(embedded_burned_geometry, dict)
            and embedded_burned_geometry.get("coordinates")
        ):
            carried_burned_geometry = embedded_burned_geometry
        if carried_burned_geometry is None:
            raise RetrospectiveConflictError(
                f"No cumulative footprint is available before {local_date.isoformat()}."
            )
        for zone_kind, geometry, geometry_origin, reason in (
            (
                "active",
                daily_geometry,
                activity["geometry_origin"],
                _active_reason(activity),
            ),
            (
                "burned",
                carried_burned_geometry,
                "AGENT_DERIVED",
                (
                    "Zone parcourue cumulée reconstituée à partir de l'empreinte de référence "
                    "et des sources datées."
                ),
            ),
        ):
            if _has_ready_zone(session, window_id=window.id, zone_kind=zone_kind):
                continue
            session.add(
                ActiveFireZoneRevision(
                    zone_revision_id=f"azr-{FIRE_ID.lower()}-{local_date.isoformat()}-{zone_kind}-retrospective-v2",
                    incident_id=incident.id,
                    episode_id=episode.id,
                    analysis_window_id=window.id,
                    zone_kind=zone_kind,
                    revision=next_revisions[zone_kind],
                    valid_at=datetime.fromisoformat(activity["valid_at"].replace("Z", "+00:00")),
                    geometry_geojson=geometry,
                    geometry_origin=geometry_origin,
                    supporting_marker_ids=[],
                    source_revision_ids=activity["source_revision_ids"],
                    review_state=ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
                    created_by=CREATED_BY,
                    reason=reason[:500],
                    reviewed_by=actor,
                    reviewed_at=reviewed_at,
                    review_reason=(
                        "Couche datée et nature du calque contrôlées lors de la restauration."
                    ),
                )
            )
            next_revisions[zone_kind] += 1
            created[zone_kind] += 1
    return created


def _ensure_campaign_days(
    session: Session,
    windows: dict[date, AgentAnalysisWindow],
    payload: dict[str, Any],
    reviewed_at: datetime,
) -> int:
    manifest_sha256 = _sha256(payload)
    campaign = session.scalar(
        select(AgentValidationCampaign).where(
            AgentValidationCampaign.campaign_id == CAMPAIGN_ID
        )
    )
    if campaign is None:
        campaign = AgentValidationCampaign(
            campaign_id=CAMPAIGN_ID,
            manifest_sha256=manifest_sha256,
            is_active=False,
            created_by=CREATED_BY,
            version=1,
        )
        session.add(campaign)
        session.flush()
    elif campaign.manifest_sha256 != manifest_sha256:
        raise RetrospectiveConflictError("The existing Die campaign has another manifest hash.")

    created = 0
    for ordinal, activity in enumerate(payload["activity_zones"], start=1):
        local_date = date.fromisoformat(activity["local_date"])
        window = windows[local_date]
        day = session.scalar(select(AgentValidationCampaignDay).where(
            AgentValidationCampaignDay.analysis_window_id == window.id
        ))
        if day is not None:
            if day.campaign_id != campaign.id:
                raise RetrospectiveConflictError(
                    f"Window {local_date.isoformat()} already belongs to another campaign."
                )
            if day.state != AgentValidationCampaignDayState.PUBLISHED:
                day.state = AgentValidationCampaignDayState.PUBLISHED
                day.finished_at = reviewed_at
                day.version += 1
            continue
        session.add(
            AgentValidationCampaignDay(
                campaign_day_id=f"campaign-day-{FIRE_ID.lower()}-{local_date.isoformat()}-retrospective-v2",
                campaign_id=campaign.id,
                analysis_window_id=window.id,
                ordinal=ordinal,
                cutoff_at=window.window_end_at,
                manifest_sha256=_sha256(
                    {
                        "dataset": DATASET_ID,
                        "date": local_date.isoformat(),
                        "activity": activity,
                    }
                ),
                allowed_media_sha256=[],
                required_operations=[],
                declared_absences=[],
                state=AgentValidationCampaignDayState.PUBLISHED,
                activated_at=reviewed_at,
                finished_at=reviewed_at,
                version=1,
            )
        )
        created += 1
    return created


def restore(
    session: Session, payload: dict[str, Any], *, actor: str, apply: bool
) -> dict[str, Any]:
    incident, episode = _incident_and_episode(session)
    if not apply:
        return {
            "mode": "dry-run",
            "fire_id": incident.fire_id,
            "reports": len(payload["reports"]),
            "daily_active_zones": len(payload["activity_zones"]),
            "daily_burned_zones": len(payload["activity_zones"]),
        }
    reviewed_at = utcnow()
    windows, windows_created = _ensure_windows(session, incident, episode, payload)
    reports_created = _ensure_reports(
        session, incident, episode, windows, payload, actor, reviewed_at
    )
    zones_created = _ensure_zones(
        session, incident, episode, windows, payload, actor, reviewed_at
    )
    campaign_days_created = _ensure_campaign_days(session, windows, payload, reviewed_at)
    session.commit()
    return {
        "mode": "applied",
        "fire_id": incident.fire_id,
        "windows_created": windows_created,
        "reports_created": reports_created,
        "zones_created": zones_created,
        "campaign_days_created": campaign_days_created,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor")
    args = parser.parse_args()
    if args.apply and (not args.actor or len(args.actor.strip()) < 5):
        parser.error("--actor is required with --apply")
    payload = _load_payload(args.dataset.resolve())
    if args.env_file:
        database_url = dotenv_values(args.env_file).get("FV_DATABASE_URL")
        if not database_url:
            parser.error("FV_DATABASE_URL is missing from --env-file")
        settings = Settings(database_url=database_url, environment="production")
    else:
        settings = Settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            result = restore(
                session,
                payload,
                actor=args.actor or "dry-run",
                apply=args.apply,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
