"""Restore reviewed incident retrospectives into the public daily-layer contract.

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
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values
from shapely.geometry import shape
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

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{2,95}$")


@dataclass(frozen=True)
class RetrospectiveIdentity:
    """Immutable identity carried by a reviewed retrospective manifest."""

    dataset_id: str
    fire_id: str
    episode_id: str
    campaign_id: str
    created_by: str
    identifier_suffix: str


_LEGACY_DIE_IDENTITY = RetrospectiveIdentity(
    dataset_id="die-2026-retrospective-v1",
    fire_id="FR-26-00001",
    episode_id="E01",
    campaign_id="campaign-die-retrospective-v2",
    created_by="fireviewer-retrospective-builder",
    identifier_suffix="retrospective-v2",
)


class RetrospectiveConflictError(RuntimeError):
    """A deterministic identifier is already attached to another entity."""


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _identity_from_payload(payload: dict[str, Any]) -> RetrospectiveIdentity:
    incident = payload.get("incident")
    if not isinstance(incident, dict):
        raise RetrospectiveConflictError("A retrospective manifest requires an incident object.")
    dataset_id = payload.get("dataset_id")
    fire_id = incident.get("fire_id")
    episode_id = incident.get("episode_id")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (dataset_id, fire_id, episode_id)
    ):
        raise RetrospectiveConflictError("A retrospective manifest identity is incomplete.")

    if (
        dataset_id == _LEGACY_DIE_IDENTITY.dataset_id
        and fire_id == _LEGACY_DIE_IDENTITY.fire_id
        and episode_id == _LEGACY_DIE_IDENTITY.episode_id
        and "campaign_id" not in payload
    ):
        return _LEGACY_DIE_IDENTITY

    campaign_id = payload.get("campaign_id")
    identifier_suffix = payload.get("identifier_suffix")
    created_by = payload.get("created_by", "fireviewer-retrospective-builder")
    if not all(
        isinstance(value, str) and _IDENTIFIER.fullmatch(value)
        for value in (dataset_id, campaign_id, identifier_suffix)
    ):
        raise RetrospectiveConflictError(
            "A retrospective dataset_id, campaign_id and identifier_suffix must be "
            "stable identifiers."
        )
    if not isinstance(created_by, str) or not created_by.strip():
        raise RetrospectiveConflictError("A retrospective manifest created_by is invalid.")
    return RetrospectiveIdentity(
        dataset_id=cast(str, dataset_id),
        fire_id=cast(str, fire_id),
        episode_id=cast(str, episode_id),
        campaign_id=cast(str, campaign_id),
        created_by=created_by,
        identifier_suffix=cast(str, identifier_suffix),
    )


def _load_payload(path: Path) -> dict[str, Any]:
    raw_payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise RetrospectiveConflictError("The retrospective manifest must be an object.")
    payload = cast(dict[str, Any], raw_payload)
    identity = _identity_from_payload(payload)
    activity_zones = payload.get("activity_zones")
    reports = payload.get("reports")
    if (
        payload.get("schema_version") != "1.0"
        or not isinstance(activity_zones, list)
        or not isinstance(reports, list)
    ):
        raise RetrospectiveConflictError("Unexpected retrospective dataset contract.")
    if not activity_zones or len(activity_zones) > 366 or len(reports) != len(activity_zones):
        raise RetrospectiveConflictError(
            "A retrospective must contain one bounded report and layer pair per day."
        )
    activity_dates = [item.get("local_date") for item in activity_zones]
    report_dates = [item.get("local_date") for item in reports]
    if (
        activity_dates != report_dates
        or activity_dates != sorted(activity_dates)
        or len(set(activity_dates)) != len(activity_dates)
    ):
        raise RetrospectiveConflictError(
            "Retrospective day pairs must be unique and chronologically ordered."
        )
    for activity, report in zip(activity_zones, reports, strict=True):
        try:
            date.fromisoformat(cast(str, activity.get("local_date")))
            datetime.fromisoformat(
                cast(str, activity.get("valid_at")).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise RetrospectiveConflictError(
                f"Invalid retrospective timestamp for {identity.dataset_id}."
            ) from exc
        active_geometry = activity.get("geometry_geojson")
        explicit_burned_geometry = activity.get("burned_geometry_geojson")
        burned_geometry = explicit_burned_geometry
        if not isinstance(burned_geometry, dict) and isinstance(active_geometry, dict):
            burned_geometry = active_geometry.get("global_footprint_geojson")
        is_legacy_die = identity == _LEGACY_DIE_IDENTITY
        if (
            not isinstance(active_geometry, dict)
            or not active_geometry.get("coordinates")
            or (
                not is_legacy_die
                and (
                    not isinstance(burned_geometry, dict)
                    or not burned_geometry.get("coordinates")
                )
            )
            or not isinstance(activity.get("source_revision_ids"), list)
            or not activity["source_revision_ids"]
            or not isinstance(report.get("sections"), list)
            or not isinstance(report.get("summary"), str)
        ):
            raise RetrospectiveConflictError(
                f"Incomplete retrospective day {activity.get('local_date')!r}."
            )
        if is_legacy_die and not isinstance(burned_geometry, dict):
            continue
        try:
            active_shape = shape(active_geometry)
            burned_shape = shape(burned_geometry)
        except (TypeError, ValueError) as exc:
            raise RetrospectiveConflictError(
                f"Invalid retrospective geometry for {activity.get('local_date')!r}."
            ) from exc
        # The legacy Die snapshot predates distinct daily burned geometries, so
        # it remains compatible with its prior global-footprint arrangement.
        # New manifests must supply a daily cumulative footprint and keep the
        # active zone within it.  The one-nanodegree tolerance only absorbs
        # GeoJSON floating-point seams introduced by the EPSG:2154 round trip.
        if (
            active_shape.is_empty
            or burned_shape.is_empty
            or (
                isinstance(explicit_burned_geometry, dict)
                and not burned_shape.buffer(1e-9).covers(active_shape)
            )
        ):
            raise RetrospectiveConflictError(
                "A daily active zone must be wholly contained in its cumulative burned footprint."
            )
    return payload


def _incident_and_episode(
    session: Session, identity: RetrospectiveIdentity
) -> tuple[IncidentSeries, Episode]:
    incident = session.scalar(
        select(IncidentSeries).where(IncidentSeries.fire_id == identity.fire_id)
    )
    if incident is None:
        raise RetrospectiveConflictError(f"Incident {identity.fire_id} does not exist.")
    episode = session.scalar(
        select(Episode).where(
            Episode.incident_id == incident.id, Episode.episode_id == identity.episode_id
        )
    )
    if episode is None:
        raise RetrospectiveConflictError(
            f"Episode {identity.fire_id}/{identity.episode_id} does not exist."
        )
    return incident, episode


def _ensure_windows(
    session: Session,
    incident: IncidentSeries,
    episode: Episode,
    payload: dict[str, Any],
    identity: RetrospectiveIdentity,
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
                analysis_id=(
                    f"analysis-{identity.fire_id.lower()}-{local_date.isoformat()}-"
                    f"{identity.identifier_suffix}"
                ),
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
    identity: RetrospectiveIdentity,
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
                report_revision_id=(
                    f"report-{identity.fire_id.lower()}-{local_date.isoformat()}-"
                    f"{identity.identifier_suffix}"
                ),
                analysis_window_id=window.id,
                incident_id=incident.id,
                episode_id=episode.id,
                revision=revision,
                title=report["title"],
                body_markdown=report["summary"],
                sections_payload=report["sections"],
                review_state=AgentReportReviewState.VALIDATED,
                created_by=identity.created_by,
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
    identity: RetrospectiveIdentity,
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
        embedded_burned_geometry = activity.get("burned_geometry_geojson")
        if not isinstance(embedded_burned_geometry, dict):
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
                activity.get("burned_geometry_origin", "AGENT_DERIVED"),
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
                    zone_revision_id=(
                        f"azr-{identity.fire_id.lower()}-{local_date.isoformat()}-"
                        f"{zone_kind}-{identity.identifier_suffix}"
                    ),
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
                    created_by=identity.created_by,
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
    identity: RetrospectiveIdentity,
    reviewed_at: datetime,
) -> int:
    manifest_sha256 = _sha256(payload)
    campaign = session.scalar(
        select(AgentValidationCampaign).where(
            AgentValidationCampaign.campaign_id == identity.campaign_id
        )
    )
    if campaign is None:
        campaign = AgentValidationCampaign(
            campaign_id=identity.campaign_id,
            manifest_sha256=manifest_sha256,
            is_active=False,
            created_by=identity.created_by,
            version=1,
        )
        session.add(campaign)
        session.flush()
    elif campaign.manifest_sha256 != manifest_sha256:
        raise RetrospectiveConflictError(
            "The existing retrospective campaign has another manifest hash."
        )

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
                campaign_day_id=(
                    f"campaign-day-{identity.fire_id.lower()}-{local_date.isoformat()}-"
                    f"{identity.identifier_suffix}"
                ),
                campaign_id=campaign.id,
                analysis_window_id=window.id,
                ordinal=ordinal,
                cutoff_at=window.window_end_at,
                manifest_sha256=_sha256(
                    {
                        "dataset": identity.dataset_id,
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
    identity = _identity_from_payload(payload)
    incident, episode = _incident_and_episode(session, identity)
    if not apply:
        return {
            "mode": "dry-run",
            "fire_id": incident.fire_id,
            "reports": len(payload["reports"]),
            "daily_active_zones": len(payload["activity_zones"]),
            "daily_burned_zones": len(payload["activity_zones"]),
        }
    reviewed_at = utcnow()
    windows, windows_created = _ensure_windows(session, incident, episode, payload, identity)
    reports_created = _ensure_reports(
        session, incident, episode, windows, payload, identity, actor, reviewed_at
    )
    zones_created = _ensure_zones(
        session, incident, episode, windows, payload, identity, actor, reviewed_at
    )
    campaign_days_created = _ensure_campaign_days(
        session, windows, payload, identity, reviewed_at
    )
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
