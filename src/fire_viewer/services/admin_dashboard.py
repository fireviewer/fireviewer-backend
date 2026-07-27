"""Bounded administration dashboard projection built from persisted workflow state."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    Episode,
    IncidentPublicReport,
    IncidentSeries,
    Job,
    ManifestRevision,
    Observation,
    SpatialPackage,
    SpatialZoneRevision,
    ZonePublication,
)
from fire_viewer.domain.enums import (
    IncidentStatus,
    JobState,
    PublicReportCategory,
    PublicReportState,
    SpatialPackageState,
    VerificationState,
)
from fire_viewer.domain.schemas import (
    AdminDashboardPriorityItem,
    AdminDashboardQueueSummary,
    AdminDashboardRecentPublication,
    AdminDashboardResponse,
    AdminDashboardWatchIncident,
)
from fire_viewer.services.admin_incidents import get_admin_work_queue
from fire_viewer.services.admin_observability import get_system_status
from fire_viewer.services.admin_operational_map import get_operational_map

_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2}


def _count(
    session: Session,
    model: type[Episode] | type[IncidentPublicReport] | type[Observation],
    *conditions: ColumnElement[bool],
) -> int:
    statement = select(func.count()).select_from(model)
    if conditions:
        statement = statement.where(*conditions)
    return int(session.scalar(statement) or 0)


def _recent_publications(session: Session) -> list[AdminDashboardRecentPublication]:
    publications = list(
        session.execute(
            select(ZonePublication)
            .options(
                selectinload(ZonePublication.zone),
                selectinload(ZonePublication.package),
            )
            .order_by(ZonePublication.updated_at.desc(), ZonePublication.id.desc())
            .limit(10)
        ).scalars()
    )
    revision_ids = {item.spatial_zone_revision_id for item in publications}
    fire_ids_by_revision: dict[int, list[str]] = defaultdict(list)
    if revision_ids:
        for revision_id, fire_id in session.execute(
            select(ManifestRevision.spatial_zone_revision_id, IncidentSeries.fire_id)
            .join(IncidentSeries, ManifestRevision.incident_id == IncidentSeries.id)
            .where(ManifestRevision.spatial_zone_revision_id.in_(revision_ids))
            .distinct()
        ):
            if revision_id is not None:
                fire_ids_by_revision[revision_id].append(fire_id)

    return [
        AdminDashboardRecentPublication(
            publication_id=item.publication_id,
            zone_id=item.zone.zone_id,
            package_id=item.package.package_id,
            state=item.state,
            is_active=item.is_active,
            updated_at=as_utc(item.updated_at),
            actor_id=item.actor_id,
            linked_fire_ids=sorted(fire_ids_by_revision[item.spatial_zone_revision_id]),
        )
        for item in publications
    ]


def get_admin_dashboard(session: Session, *, settings: Settings) -> AdminDashboardResponse:
    generated_at = as_utc(utcnow())
    work_queue = get_admin_work_queue(session)
    system = get_system_status(session, settings)
    operational_map = get_operational_map(session)

    quarantined_jobs = list(
        session.execute(
            select(Job)
            .where(Job.state == JobState.QUARANTINED)
            .options(selectinload(Job.incident))
            .order_by(Job.updated_at.asc(), Job.job_id.asc())
            .limit(20)
        ).scalars()
    )
    packages_to_review = list(
        session.execute(
            select(SpatialPackage)
            .where(SpatialPackage.state == SpatialPackageState.PREVIEWABLE)
            .options(
                selectinload(SpatialPackage.spatial_zone_revision).selectinload(
                    SpatialZoneRevision.zone
                )
            )
            .order_by(SpatialPackage.created_at.asc(), SpatialPackage.package_id.asc())
            .limit(20)
        ).scalars()
    )

    priorities: list[AdminDashboardPriorityItem] = []
    priorities.extend(
        AdminDashboardPriorityItem(
            kind="job",
            priority="critical",
            target_id=job.job_id,
            fire_id=job.incident.fire_id,
            title="Traitement en quarantaine",
            detail=job.last_error or f"Le traitement {job.kind.value} exige une intervention.",
            created_at=as_utc(job.updated_at),
        )
        for job in quarantined_jobs
    )
    priorities.extend(
        AdminDashboardPriorityItem(
            kind="report",
            priority=("critical" if report.category == PublicReportCategory.PRIVACY else "high"),
            target_id=report.report_id,
            fire_id=report.fire_id,
            title=(
                "Donnée personnelle signalée"
                if report.category == PublicReportCategory.PRIVACY
                else "Correction publique à examiner"
            ),
            detail=report.category.value,
            created_at=report.submitted_at,
        )
        for report in work_queue.reports
    )
    priorities.extend(
        AdminDashboardPriorityItem(
            kind="observation",
            priority="high",
            target_id=observation.observation_id,
            fire_id=observation.proposed_fire_id,
            title="Observation à qualifier",
            detail=(
                ", ".join(observation.review_reasons)
                if observation.review_reasons
                else f"Source {observation.source_key} en attente de revue humaine."
            ),
            created_at=observation.observed_at,
        )
        for observation in work_queue.observations
    )
    priorities.extend(
        AdminDashboardPriorityItem(
            kind="incident",
            priority=("high" if incident.status == IncidentStatus.ACTIVE_CONFIRMED else "medium"),
            target_id=incident.episode_id,
            fire_id=incident.fire_id,
            title="Incident à réexaminer",
            detail=f"Épisode {incident.episode_id} · {incident.status.value}",
            created_at=incident.last_observed_at,
        )
        for incident in work_queue.incidents
    )
    priorities.extend(
        AdminDashboardPriorityItem(
            kind="model_package",
            priority="high",
            target_id=package.package_id,
            title="Représentation 3D à revoir",
            detail=(
                package.spatial_zone_revision.zone.zone_id
                if package.spatial_zone_revision is not None
                else "Paquet sans révision spatiale liée"
            ),
            created_at=as_utc(package.created_at),
        )
        for package in packages_to_review
    )
    priorities.sort(
        key=lambda item: (
            _PRIORITY_ORDER[item.priority],
            item.created_at,
            item.kind,
            item.target_id,
        )
    )

    observations_pending = _count(
        session, Observation, Observation.verification_state == VerificationState.PENDING_REVIEW
    )
    reports_pending = system.queues.reports_pending
    incidents_requiring_review = _count(
        session, Episode, Episode.is_current.is_(True), Episode.review_required.is_(True)
    )
    active_incidents_requiring_review = _count(
        session,
        Episode,
        Episode.is_current.is_(True),
        Episode.review_required.is_(True),
        Episode.status == IncidentStatus.ACTIVE_CONFIRMED,
    )
    privacy_reports = _count(
        session,
        IncidentPublicReport,
        IncidentPublicReport.state == PublicReportState.PENDING,
        IncidentPublicReport.category == PublicReportCategory.PRIVACY,
    )
    critical = system.queues.jobs_quarantined + privacy_reports
    high = (
        observations_pending
        + reports_pending
        - privacy_reports
        + active_incidents_requiring_review
        + system.assets.packages_previewable
    )
    medium = incidents_requiring_review - active_incidents_requiring_review

    watchlist = [
        AdminDashboardWatchIncident(
            fire_id=item.fire_id,
            canonical_name=item.canonical_name,
            status=item.status,
            verification_state=item.verification_state,
            last_observed_at=item.last_observed_at,
            review_required=item.review_required,
            pending_observation_count=item.pending_observation_count,
            model_update_available=item.model_update_available,
        )
        for item in sorted(
            operational_map.incidents,
            key=lambda incident: (
                not incident.review_required,
                not incident.model_update_available,
                -incident.pending_observation_count,
                incident.last_observed_at,
                incident.fire_id,
            ),
        )
        if item.review_required
        or item.pending_observation_count
        or item.model_update_available
        or item.status in (IncidentStatus.ACTIVE_CONFIRMED, IncidentStatus.MONITORING)
    ][:20]

    return AdminDashboardResponse(
        generated_at=generated_at,
        queue=AdminDashboardQueueSummary(
            total=(
                observations_pending
                + reports_pending
                + incidents_requiring_review
                + system.queues.jobs_quarantined
                + system.assets.packages_previewable
            ),
            critical=critical,
            high=high,
            medium=medium,
            observations_pending=observations_pending,
            reports_pending=reports_pending,
            incidents_requiring_review=incidents_requiring_review,
            jobs_quarantined=system.queues.jobs_quarantined,
            models_to_review=system.assets.packages_previewable,
        ),
        priorities=priorities[:20],
        watchlist=watchlist,
        recent_publications=_recent_publications(session),
        map_summary=operational_map.summary,
        system=system,
    )
