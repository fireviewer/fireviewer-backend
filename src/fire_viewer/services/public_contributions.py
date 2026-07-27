from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    AgentFactProposal,
    AgentMediaItem,
    AgentSituationReportRevision,
    AgentSourcePackage,
    AgentSourcePackageItem,
    AgentSpatialProposal,
    PublicContributionSubmission,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.contribution_schemas import (
    AdminPublicContribution,
    AdminPublicContributionEnvelope,
    AdminPublicContributionListResponse,
    AdminPublicContributionReviewRequest,
    PublicContributionEnvelope,
    PublicContributionOpenRequest,
    PublicContributionOpenResponse,
    PublicContributionStatus,
    PublicContributionUploadGrant,
)
from fire_viewer.domain.enums import (
    ActorType,
    AgentBatchState,
    AgentConsentState,
    AgentProposalReviewState,
    AgentReportReviewState,
    AgentSourcePackageState,
    PublicContributionState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.services.agent_source_packages import (
    _incident_episode,
    create_private_media_url,
    finalize_source_package,
)
from fire_viewer.services.blob_uploads import create_source_blob_upload_grant
from fire_viewer.services.common import record_audit, record_operator_audit
from fire_viewer.services.queries import get_incident_public
from fire_viewer.storage import build_object_store
from fire_viewer.storage.object_store import ObjectStorageError

_TERMS_VERSION = "firewarning-public-contribution-v1"
_PARIS = ZoneInfo("Europe/Paris")
_BASE_SCOPES = ["temporary_storage", "agent_analysis", "human_review"]
_PUBLIC_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _block_rejected_contribution_analysis(
    session: Session, contribution: PublicContributionSubmission, *, actor: Actor, reason: str
) -> None:
    """Stop unstarted intake and invalidate only proposals still awaiting review."""
    package = contribution.source_package
    if package is None:
        return
    batches = {
        item.agent_media_item.batch
        for item in package.items
        if item.agent_media_item is not None and item.agent_media_item.batch is not None
    }
    media_ids = [
        item.agent_media_item.id for item in package.items if item.agent_media_item is not None
    ]
    for batch in batches:
        if batch.state in {AgentBatchState.DRAFT, AgentBatchState.QUEUED}:
            batch.state = AgentBatchState.CANCELLED
            batch.cancelled_at = utcnow()
        elif batch.state in {AgentBatchState.SUBMITTING, AgentBatchState.RUNNING}:
            batch.state = AgentBatchState.CANCEL_REQUESTED
    if not media_ids:
        return
    facts = session.scalars(
        select(AgentFactProposal).where(
            AgentFactProposal.source_media_item_id.in_(media_ids),
            AgentFactProposal.review_state == AgentProposalReviewState.PENDING,
        )
    ).all()
    spatial = session.scalars(
        select(AgentSpatialProposal).where(
            AgentSpatialProposal.source_media_item_id.in_(media_ids),
            AgentSpatialProposal.review_state == AgentProposalReviewState.PENDING,
        )
    ).all()
    for fact in facts:
        fact.review_state = AgentProposalReviewState.INVALIDATED
        fact.reviewed_by = actor.actor_id
        fact.reviewed_at = utcnow()
        fact.review_reason = reason
        fact.version += 1
    for spatial_proposal in spatial:
        spatial_proposal.review_state = AgentProposalReviewState.INVALIDATED
        spatial_proposal.reviewed_by = actor.actor_id
        spatial_proposal.reviewed_at = utcnow()
        spatial_proposal.review_reason = reason
        spatial_proposal.version += 1
    windows = [
        batch.analysis_window_id for batch in batches if batch.analysis_window_id is not None
    ]
    if windows:
        reports = session.scalars(
            select(AgentSituationReportRevision).where(
                AgentSituationReportRevision.analysis_window_id.in_(windows),
                AgentSituationReportRevision.review_state == AgentReportReviewState.DRAFT,
            )
        ).all()
        for report in reports:
            report.review_state = AgentReportReviewState.INVALIDATED
            report.reviewed_by = actor.actor_id
            report.reviewed_at = utcnow()
            report.review_reason = reason


def _public_actor(contribution_id: str) -> Actor:
    return Actor(
        actor_id=f"public-contribution:{contribution_id}",
        roles=frozenset(),
        actor_type=ActorType.PUBLIC_SOURCE,
    )


def _tracking_token(contribution: PublicContributionSubmission, settings: Settings) -> str:
    raw = hmac.digest(
        settings.public_report_hash_secret.encode(),
        f"tracking:{contribution.contribution_id}:{contribution.request_hash}".encode(),
        "sha256",
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify_tracking_token(contribution: PublicContributionSubmission, token: str) -> None:
    if not hmac.compare_digest(contribution.tracking_token_hash, _token_hash(token)):
        raise ForbiddenError("The contribution tracking token is invalid.")


def _origin_fingerprint(origin: str, day: str, settings: Settings) -> str:
    return hmac.digest(
        settings.public_report_hash_secret.encode(),
        f"contribution:{origin}:{day}".encode(),
        "sha256",
    ).hex()


def _contact_hash(contact_email: str | None, settings: Settings) -> str | None:
    if contact_email is None or not contact_email.strip():
        return None
    normalized = contact_email.strip().casefold()
    local, separator, domain = normalized.rpartition("@")
    if (
        not separator
        or not local
        or "." not in domain
        or any(char.isspace() for char in normalized)
    ):
        raise BadRequestError("invalid_contact_email", "The optional contact email is invalid.")
    return hmac.digest(
        settings.public_report_hash_secret.encode(), f"contact:{normalized}".encode(), "sha256"
    ).hex()


def _consent_scopes(payload: PublicContributionOpenRequest) -> list[str]:
    scopes = list(_BASE_SCOPES)
    if payload.consents.retain_evidence:
        scopes.append("retain_evidence")
    if payload.consents.public_display:
        scopes.append("display_media")
    if payload.consents.spatial_display:
        scopes.append("display_spatial_marker")
    return scopes


def _location_hint(payload: PublicContributionOpenRequest) -> str:
    if payload.location.label:
        return payload.location.label.strip()
    return f"{payload.location.latitude:.6f}, {payload.location.longitude:.6f}"


def _load_contribution(
    session: Session, contribution_id: str, *, for_update: bool = False
) -> PublicContributionSubmission:
    statement = (
        select(PublicContributionSubmission)
        .where(PublicContributionSubmission.contribution_id == contribution_id)
        .options(
            selectinload(PublicContributionSubmission.incident),
            selectinload(PublicContributionSubmission.source_package)
            .selectinload(AgentSourcePackage.items)
            .selectinload(AgentSourcePackageItem.agent_media_item)
            .selectinload(AgentMediaItem.consent),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    contribution = session.execute(statement).scalar_one_or_none()
    if contribution is None:
        raise NotFoundError("public_contribution", contribution_id)
    return contribution


def _status(contribution: PublicContributionSubmission) -> PublicContributionStatus:
    payload = contribution.submission_payload
    location = payload["location"]
    observation = payload["observation"]
    package = contribution.source_package
    media_count = package.declared_file_count if package is not None else 0
    label = location.get("label")
    if not label and location.get("latitude") is not None:
        label = f"{location['latitude']:.5f}, {location['longitude']:.5f}"
    return PublicContributionStatus(
        contribution_id=contribution.contribution_id,
        kind=contribution.kind,
        fire_id=contribution.incident.fire_id if contribution.incident is not None else None,
        state=contribution.state,
        received_at=as_utc(contribution.received_at) if contribution.received_at else None,
        reviewed_at=as_utc(contribution.reviewed_at) if contribution.reviewed_at else None,
        review_reason=contribution.review_reason,
        purge_after=as_utc(contribution.purge_after),
        media_count=media_count,
        location_label=label,
        observation_type=observation["observation_type"],
        observed_at=datetime.fromisoformat(observation["observed_at"]),
        version=contribution.version,
    )


def _admin_status(
    contribution: PublicContributionSubmission, settings: Settings
) -> AdminPublicContribution:
    status = _status(contribution)
    package = contribution.source_package
    media_urls: list[str] = []
    if package is not None and package.state == AgentSourcePackageState.CONVERTED:
        for item in package.items:
            media = item.agent_media_item
            if (
                media is not None
                and media.purged_at is None
                and media.consent is not None
                and media.consent.state == AgentConsentState.GRANTED
            ):
                media_urls.append(
                    create_private_media_url(
                        source_kind="source_package",
                        source_id=package.package_id,
                        item_id=item.item_id,
                        purge_after=package.purge_after,
                        settings=settings,
                    )
                )
    return AdminPublicContribution(
        **status.model_dump(),
        description=contribution.submission_payload["observation"]["description"],
        direct_observation=contribution.submission_payload["observation"]["direct_observation"],
        location=contribution.submission_payload["location"],
        consent_scopes=list(contribution.consent_payload["scopes"]),
        contact_provided=contribution.contact_reference_hash is not None,
        private_media_urls=media_urls,
    )


def _replay_upload(
    contribution: PublicContributionSubmission, settings: Settings
) -> PublicContributionUploadGrant | None:
    package = contribution.source_package
    if package is None or package.state != AgentSourcePackageState.OPEN:
        return None
    grant = create_source_blob_upload_grant(
        package_id=package.package_id,
        file_count=package.declared_file_count,
        total_size_bytes=package.declared_total_size_bytes,
        actor=_public_actor(contribution.contribution_id),
        settings=settings,
        upload_id=package.upload_id,
        purpose="public_contribution",
    )
    return PublicContributionUploadGrant(
        package_id=package.package_id,
        pathname_prefix=grant.pathname_prefix,
        upload_grant=grant.token,
        expires_at=grant.expires_at,
        maximum_file_size_bytes=settings.public_contribution_max_image_bytes,
        allowed_content_types=sorted(_PUBLIC_IMAGE_TYPES),
    )


def open_public_contribution(
    session: Session,
    *,
    payload: PublicContributionOpenRequest,
    idempotency_key: str,
    origin: str,
    trace_id: str,
    settings: Settings,
) -> PublicContributionOpenResponse:
    if payload.observation.observed_at.tzinfo is None:
        raise BadRequestError(
            "observation_timezone_required", "The observation date must include a timezone."
        )
    now = utcnow()
    if as_utc(payload.observation.observed_at) > now + timedelta(minutes=10):
        raise BadRequestError("observation_in_future", "The observation date is in the future.")
    if payload.media is not None:
        if settings.object_storage_backend != "vercel_blob":
            raise ConflictError(
                "public_contribution_upload_unavailable",
                "Private public evidence uploads are temporarily unavailable.",
            )
        if payload.media.size_bytes > settings.public_contribution_max_image_bytes:
            raise BadRequestError(
                "public_image_too_large", "The public evidence image is too large."
            )
        if payload.media.content_type not in _PUBLIC_IMAGE_TYPES:
            raise BadRequestError("public_image_type_unsupported", "The image type is unsupported.")

    submitted_day = now.astimezone(UTC).date().isoformat()
    fingerprint = _origin_fingerprint(origin, submitted_day, settings)
    contact_hash = _contact_hash(payload.contact_email, settings)
    request_payload = payload.model_dump(mode="json", exclude={"contact_email"})
    request_hash = sha256_hex({**request_payload, "contact_reference_hash": contact_hash})

    begin_write_transaction(session)
    existing = session.execute(
        select(PublicContributionSubmission)
        .where(
            PublicContributionSubmission.origin_fingerprint == fingerprint,
            PublicContributionSubmission.submitted_day == submitted_day,
            PublicContributionSubmission.idempotency_key == idempotency_key,
        )
        .options(selectinload(PublicContributionSubmission.source_package))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ConflictError(
                "public_contribution_idempotency_conflict",
                "The idempotency key was already used for another contribution.",
            )
        session.rollback()
        return PublicContributionOpenResponse(
            contribution_id=existing.contribution_id,
            state=existing.state,
            tracking_token=_tracking_token(existing, settings),
            upload=_replay_upload(existing, settings),
            purge_after=as_utc(existing.purge_after),
            replayed=True,
        )

    count = session.scalar(
        select(func.count(PublicContributionSubmission.id)).where(
            PublicContributionSubmission.origin_fingerprint == fingerprint,
            PublicContributionSubmission.submitted_day == submitted_day,
        )
    )
    if count is not None and count >= settings.public_contribution_rate_limit_per_day:
        raise ConflictError(
            "public_contribution_rate_limited", "Daily public contribution limit reached."
        )

    incident = None
    episode = None
    if payload.fire_id is not None:
        get_incident_public(session, payload.fire_id, settings)
        incident, episode = _incident_episode(session, payload.fire_id)

    scopes = _consent_scopes(payload)
    contribution_id = new_prefixed_id("PC")
    purge_after = now + timedelta(days=settings.agent_source_package_retention_days)
    contribution = PublicContributionSubmission(
        contribution_id=contribution_id,
        kind=payload.kind,
        state=(
            PublicContributionState.OPEN
            if payload.media is not None
            else PublicContributionState.PENDING
        ),
        incident_id=incident.id if incident is not None else None,
        source_package_id=None,
        submission_payload=request_payload,
        consent_payload={"terms_version": _TERMS_VERSION, "scopes": scopes},
        contact_reference_hash=contact_hash,
        origin_fingerprint=fingerprint,
        submitted_day=submitted_day,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        tracking_token_hash="0" * 64,
        trace_id=trace_id,
        purge_after=purge_after,
        received_at=now if payload.media is None else None,
    )
    contribution.tracking_token_hash = _token_hash(_tracking_token(contribution, settings))
    session.add(contribution)
    session.flush()

    upload = None
    if payload.media is not None:
        package_id = new_prefixed_id("SP")
        grant = create_source_blob_upload_grant(
            package_id=package_id,
            file_count=1,
            total_size_bytes=payload.media.size_bytes,
            actor=_public_actor(contribution_id),
            settings=settings,
            purpose="public_contribution",
        )
        observed_date = as_utc(payload.observation.observed_at).astimezone(_PARIS).date()
        package = AgentSourcePackage(
            package_id=package_id,
            incident_id=incident.id if incident is not None else None,
            episode_id=episode.id if episode is not None else None,
            analysis_window_id=None,
            state=AgentSourcePackageState.OPEN,
            upload_id=grant.upload_id,
            pathname_prefix=grant.pathname_prefix,
            declared_file_count=1,
            declared_total_size_bytes=payload.media.size_bytes,
            known_start_date=observed_date,
            known_end_date=observed_date,
            location_hint=_location_hint(payload),
            analysis_authorized=True,
            publication_authorized=False,
            terms_version=_TERMS_VERSION,
            consent_evidence_sha256=sha256_hex(
                {"contribution_id": contribution_id, "consents": payload.consents.model_dump()}
            ),
            consent_scopes=scopes,
            subject_reference_hash=contact_hash,
            idempotency_key=f"public-contribution:{contribution_id}",
            request_hash=request_hash,
            trace_id=trace_id,
            purge_after=purge_after,
        )
        session.add(package)
        session.flush()
        contribution.source_package_id = package.id
        contribution.source_package = package
        upload = PublicContributionUploadGrant(
            package_id=package_id,
            pathname_prefix=grant.pathname_prefix,
            upload_grant=grant.token,
            expires_at=grant.expires_at,
            maximum_file_size_bytes=settings.public_contribution_max_image_bytes,
            allowed_content_types=sorted(_PUBLIC_IMAGE_TYPES),
        )

    record_audit(
        session,
        actor_type=ActorType.PUBLIC_SOURCE,
        actor_id="anonymous-contributor",
        action="public.contribution.opened",
        target_type="public_contribution",
        target_id=contribution_id,
        reason="Private public contribution received for human review.",
        trace_id=trace_id,
        after={
            "kind": payload.kind.value,
            "state": contribution.state.value,
            "fire_id": payload.fire_id,
            "media_count": 1 if payload.media is not None else 0,
            "publication_authorized": False,
        },
    )
    session.commit()
    return PublicContributionOpenResponse(
        contribution_id=contribution_id,
        state=contribution.state,
        tracking_token=_tracking_token(contribution, settings),
        upload=upload,
        purge_after=as_utc(purge_after),
    )


def finalize_public_contribution(
    session: Session,
    *,
    contribution_id: str,
    tracking_token: str,
    trace_id: str,
    settings: Settings,
) -> PublicContributionEnvelope:
    contribution = _load_contribution(session, contribution_id)
    _verify_tracking_token(contribution, tracking_token)
    if contribution.state == PublicContributionState.PENDING:
        return PublicContributionEnvelope(contribution=_status(contribution), trace_id=trace_id)
    if contribution.state != PublicContributionState.OPEN or contribution.source_package is None:
        raise ConflictError(
            "public_contribution_not_open", "This contribution can no longer be finalized."
        )
    finalize_source_package(
        session,
        package_id=contribution.source_package.package_id,
        actor=_public_actor(contribution_id),
        trace_id=trace_id,
        settings=settings,
    )
    contribution = _load_contribution(session, contribution_id, for_update=True)
    if contribution.state == PublicContributionState.OPEN:
        contribution.state = PublicContributionState.PENDING
        contribution.received_at = utcnow()
        contribution.version += 1
        record_audit(
            session,
            actor_type=ActorType.PUBLIC_SOURCE,
            actor_id="anonymous-contributor",
            action="public.contribution.finalized",
            target_type="public_contribution",
            target_id=contribution_id,
            reason="Private image validated and queued for human review.",
            trace_id=trace_id,
            after={"state": contribution.state.value, "publication_authorized": False},
        )
        session.commit()
    return PublicContributionEnvelope(
        contribution=_status(_load_contribution(session, contribution_id)), trace_id=trace_id
    )


def get_public_contribution(
    session: Session, *, contribution_id: str, tracking_token: str, trace_id: str
) -> PublicContributionEnvelope:
    contribution = _load_contribution(session, contribution_id)
    _verify_tracking_token(contribution, tracking_token)
    return PublicContributionEnvelope(contribution=_status(contribution), trace_id=trace_id)


def withdraw_public_contribution(
    session: Session,
    *,
    contribution_id: str,
    tracking_token: str,
    trace_id: str,
    settings: Settings,
) -> PublicContributionEnvelope:
    begin_write_transaction(session)
    contribution = _load_contribution(session, contribution_id, for_update=True)
    _verify_tracking_token(contribution, tracking_token)
    if contribution.state == PublicContributionState.WITHDRAWN:
        session.rollback()
        return PublicContributionEnvelope(contribution=_status(contribution), trace_id=trace_id)
    now = utcnow()
    package = contribution.source_package
    if package is not None:
        for item in package.items:
            media = item.agent_media_item
            if media is None:
                continue
            if media.consent is not None and media.consent.state == AgentConsentState.GRANTED:
                media.consent.state = AgentConsentState.WITHDRAWN
                media.consent.withdrawn_at = now
                media.consent.withdrawal_reason = "Withdrawn by the public contributor."
            media.purged_at = now
            batch = media.batch
            if batch.state in {AgentBatchState.DRAFT, AgentBatchState.QUEUED}:
                batch.state = AgentBatchState.CANCELLED
                batch.cancelled_at = now
            elif batch.state in {AgentBatchState.SUBMITTING, AgentBatchState.RUNNING}:
                batch.state = AgentBatchState.CANCEL_REQUESTED
        try:
            build_object_store(settings).delete_tree(f"source-packages/{package.upload_id}")
        except ObjectStorageError:
            package.failure_detail = "withdrawal_storage_cleanup_pending"
        package.state = AgentSourcePackageState.PURGED
    contribution.state = PublicContributionState.WITHDRAWN
    contribution.reviewed_at = now
    contribution.review_reason = "Contribution withdrawn by its sender."
    contribution.version += 1
    record_audit(
        session,
        actor_type=ActorType.PUBLIC_SOURCE,
        actor_id="anonymous-contributor",
        action="public.contribution.withdrawn",
        target_type="public_contribution",
        target_id=contribution_id,
        reason="Public contributor withdrew private analysis consent.",
        trace_id=trace_id,
        after={"state": contribution.state.value, "media_access": "blocked"},
    )
    session.commit()
    return PublicContributionEnvelope(
        contribution=_status(_load_contribution(session, contribution_id)), trace_id=trace_id
    )


def list_public_contributions(
    session: Session, *, state: PublicContributionState | None, settings: Settings
) -> AdminPublicContributionListResponse:
    statement = (
        select(PublicContributionSubmission)
        .options(
            selectinload(PublicContributionSubmission.incident),
            selectinload(PublicContributionSubmission.source_package)
            .selectinload(AgentSourcePackage.items)
            .selectinload(AgentSourcePackageItem.agent_media_item)
            .selectinload(AgentMediaItem.consent),
        )
        .order_by(PublicContributionSubmission.created_at.desc())
    )
    if state is not None:
        statement = statement.where(PublicContributionSubmission.state == state)
    rows = session.execute(statement).scalars().all()
    return AdminPublicContributionListResponse(
        contributions=[_admin_status(row, settings) for row in rows]
    )


def review_public_contribution(
    session: Session,
    *,
    contribution_id: str,
    payload: AdminPublicContributionReviewRequest,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AdminPublicContributionEnvelope:
    begin_write_transaction(session)
    contribution = _load_contribution(session, contribution_id, for_update=True)
    if contribution.version != payload.expected_version:
        raise ConflictError(
            "public_contribution_version_conflict",
            "The contribution changed since it was loaded.",
        )
    if contribution.state != PublicContributionState.PENDING:
        raise ConflictError(
            "public_contribution_not_pending", "Only a pending contribution can be reviewed."
        )
    before = {"state": contribution.state.value, "version": contribution.version}
    contribution.state = payload.state
    contribution.reviewed_at = utcnow()
    contribution.reviewed_by = actor.actor_id
    contribution.review_reason = payload.reason
    contribution.version += 1
    if payload.state == PublicContributionState.REJECTED:
        _block_rejected_contribution_analysis(
            session, contribution, actor=actor, reason=payload.reason
        )
    record_operator_audit(
        session,
        actor=actor,
        action="public.contribution.reviewed",
        target_type="public_contribution",
        target_id=contribution_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after={"state": contribution.state.value, "version": contribution.version},
        payload={"publication_authorized": False},
    )
    session.commit()
    contribution = _load_contribution(session, contribution_id)
    return AdminPublicContributionEnvelope(
        contribution=_admin_status(contribution, settings), trace_id=trace_id
    )
