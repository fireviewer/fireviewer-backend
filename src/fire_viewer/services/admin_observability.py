"""Read-only administrative projections for audit, roles, system state and configuration."""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import AuditEvent, IncidentPublicReport, Job, OutboxEvent, SpatialPackage
from fire_viewer.domain.enums import JobState, PublicReportState, SpatialPackageState
from fire_viewer.domain.schemas import (
    AdminAuditListResponse,
    AdminConfigurationResponse,
    AdminGlobalAuditEvent,
    AdminMatchingConfiguration,
    AdminPublicConfiguration,
    AdminRoleDefinition,
    AdminRolesResponse,
    AdminStorageConfiguration,
    AdminSystemApplicationStatus,
    AdminSystemAssetStatus,
    AdminSystemDatabaseStatus,
    AdminSystemQueueStatus,
    AdminSystemStatus,
)

ROLE_CATALOG = (
    AdminRoleDefinition(
        role="administrator",
        description="Accès à l'espace d'administration et aux lectures globales.",
        capabilities=[
            "consulter les dossiers incidents",
            "consulter l'audit global",
            "consulter l'état système",
        ],
    ),
    AdminRoleDefinition(
        role="analyst",
        description="Qualification des observations et analyse de rapprochement spatial.",
        capabilities=["examiner les candidats spatiaux", "proposer un rattachement"],
    ),
    AdminRoleDefinition(
        role="validator",
        description="Validation des décisions et publications soumises au workflow.",
        capabilities=["valider une transition", "approuver une publication"],
    ),
    AdminRoleDefinition(
        role="security_operator",
        description="Opérations de sécurité contrôlées et revues des incidents sensibles.",
        capabilities=["examiner les journaux de sécurité", "appliquer les procédures de retrait"],
    ),
)


def _identity_management_label(settings: Settings) -> str:
    if settings.auth_mode == "local_admin":
        return "identifiant et mot de passe locaux"
    if settings.auth_mode == "jwt":
        return "OIDC/JWT"
    return "authentification désactivée"


def _count(
    session: Session,
    model: type[AuditEvent]
    | type[IncidentPublicReport]
    | type[Job]
    | type[OutboxEvent]
    | type[SpatialPackage],
    *conditions: ColumnElement[bool],
) -> int:
    statement = select(func.count()).select_from(model)
    if conditions:
        statement = statement.where(*conditions)
    return int(session.scalar(statement) or 0)


def list_global_audit(
    session: Session,
    *,
    limit: int,
    action: str | None = None,
    target_id: str | None = None,
) -> AdminAuditListResponse:
    statement = select(AuditEvent)
    if action:
        statement = statement.where(AuditEvent.action == action)
    if target_id:
        statement = statement.where(AuditEvent.target_id == target_id)
    rows = (
        session.execute(
            statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    return AdminAuditListResponse(
        events=[
            AdminGlobalAuditEvent(
                event_id=row.event_id,
                occurred_at=as_utc(row.occurred_at),
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                actor_type=row.actor_type.value,
                actor_id=row.actor_id,
                reason=row.reason,
                trace_id=row.trace_id,
            )
            for row in rows
        ]
    )


def get_admin_roles(actor: Actor, settings: Settings) -> AdminRolesResponse:
    return AdminRolesResponse(
        actor_id=actor.actor_id,
        actor_type=actor.actor_type.value,
        assigned_roles=sorted(actor.roles),
        identity_management=_identity_management_label(settings),
        catalog=list(ROLE_CATALOG),
    )


def get_system_status(session: Session, settings: Settings) -> AdminSystemStatus:
    # The query is intentional: status must report the real database connection, not an assumption.
    session.execute(select(1)).scalar_one()
    active_job_states = (
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.VALIDATING,
        JobState.UPLOADING,
        JobState.VERIFYING,
        JobState.PUBLISHING,
        JobState.RETRY_WAIT,
    )
    return AdminSystemStatus(
        checked_at=as_utc(utcnow()),
        application=AdminSystemApplicationStatus(
            name=settings.app_name,
            version=settings.app_version,
            environment=settings.environment,
            authentication_mode=settings.auth_mode,
        ),
        database=AdminSystemDatabaseStatus(
            dialect=session.get_bind().dialect.name,
            reachable=True,
        ),
        queues=AdminSystemQueueStatus(
            jobs_active=_count(session, Job, Job.state.in_(active_job_states)),
            jobs_quarantined=_count(session, Job, Job.state == JobState.QUARANTINED),
            outbox_pending=_count(session, OutboxEvent, OutboxEvent.published_at.is_(None)),
            outbox_with_error=_count(session, OutboxEvent, OutboxEvent.last_error.is_not(None)),
            reports_pending=_count(
                session,
                IncidentPublicReport,
                IncidentPublicReport.state == PublicReportState.PENDING,
            ),
        ),
        assets=AdminSystemAssetStatus(
            packages_draft=_count(
                session, SpatialPackage, SpatialPackage.state == SpatialPackageState.DRAFT
            ),
            packages_verified=_count(
                session, SpatialPackage, SpatialPackage.state == SpatialPackageState.VERIFIED
            ),
            packages_previewable=_count(
                session, SpatialPackage, SpatialPackage.state == SpatialPackageState.PREVIEWABLE
            ),
            packages_published=_count(
                session, SpatialPackage, SpatialPackage.state == SpatialPackageState.PUBLISHED
            ),
            packages_withdrawn_or_revoked=_count(
                session,
                SpatialPackage,
                SpatialPackage.state.in_(
                    (SpatialPackageState.WITHDRAWN, SpatialPackageState.REVOKED)
                ),
            ),
        ),
        audit_event_count=_count(session, AuditEvent),
        worker_heartbeat="not_persisted",
    )


def get_safe_configuration(settings: Settings) -> AdminConfigurationResponse:
    return AdminConfigurationResponse(
        environment=settings.environment,
        authentication_mode=settings.auth_mode,
        identity_management=_identity_management_label(settings),
        matching=AdminMatchingConfiguration(
            policy_id=settings.matching_policy_id,
            create_below=settings.matching_create_below,
            auto_attach_above=settings.matching_auto_attach_above,
            min_margin=settings.matching_min_margin,
            max_candidate_distance_m=settings.matching_max_candidate_distance_m,
            max_incident_uncertainty_m=settings.matching_max_incident_uncertainty_m,
            max_candidates=settings.matching_max_candidates,
        ),
        public=AdminPublicConfiguration(
            report_rate_limit_per_day=settings.public_report_rate_limit_per_day,
            idempotency_retention_hours=settings.idempotency_retention_hours,
            public_notice=settings.public_notice,
        ),
        storage=AdminStorageConfiguration(
            archive_max_bytes=settings.zone_upload_max_bytes,
            unpacked_max_bytes=settings.zone_upload_max_unpacked_bytes,
            archive_max_files=settings.zone_upload_max_files,
            manifest_max_bytes=settings.zone_upload_max_manifest_bytes,
        ),
    )
