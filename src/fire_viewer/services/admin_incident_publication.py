"""Explicit, incident-scoped publication status and direct bulletin editing.

The public bulletin is the only domain that may be updated directly by an
administrator. Gallery media and spatial packages keep their independent
lifecycles; this module only reports their readiness and destinations.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    IncidentBulletinEntry as BulletinEntry,
)
from fire_viewer.db.models import (
    IncidentGalleryItem,
    IncidentSeries,
    Source,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import VerificationState
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.schemas import (
    AdminIncidentBulletinEntriesResponse,
    AdminIncidentBulletinEntry,
    AdminIncidentBulletinEntryCreateRequest,
    AdminIncidentBulletinEntryRetireRequest,
    AdminIncidentBulletinUpdateRequest,
    AdminIncidentBulletinUpdateResponse,
    AdminIncidentPublicationDomain,
    AdminIncidentPublicationStatus,
    AdminPublicationCheck,
)
from fire_viewer.services.common import incident_snapshot, record_operator_audit
from fire_viewer.services.incident_spatial_review import get_spatial_review_workspace


def _incident(session: Session, fire_id: str, *, for_update: bool = False) -> IncidentSeries:
    statement = (
        select(IncidentSeries)
        .where(IncidentSeries.fire_id == fire_id)
        .options(selectinload(IncidentSeries.episodes))
    )
    if for_update:
        statement = statement.with_for_update()
    incident = session.execute(statement).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    return incident


def _active_source(session: Session, source_key: str) -> Source:
    source = session.execute(
        select(Source).where(Source.source_key == source_key, Source.enabled.is_(True))
    ).scalar_one_or_none()
    if source is None:
        raise ConflictError(
            "admin_source_unavailable",
            "La source Admin choisie est absente ou désactivée.",
        )
    return source


def _entry_response(entry: BulletinEntry) -> AdminIncidentBulletinEntry:
    return AdminIncidentBulletinEntry(
        entry_id=entry.entry_id,
        episode_id=entry.episode.episode_id if entry.episode else None,
        source_key=entry.source.source_key,
        kind=entry.kind,
        body=entry.body,
        effective_at=as_utc(entry.effective_at),
        published_at=as_utc(entry.published_at),
        retired_at=as_utc(entry.retired_at) if entry.retired_at else None,
        state=entry.state,
        reason=entry.reason,
        created_by=entry.created_by,
        retired_by=entry.retired_by,
        retirement_reason=entry.retirement_reason,
        version=entry.version,
    )


def list_admin_incident_bulletin_entries(
    session: Session, *, fire_id: str
) -> AdminIncidentBulletinEntriesResponse:
    incident = _incident(session, fire_id)
    rows = (
        session.execute(
            select(BulletinEntry)
            .where(BulletinEntry.incident_id == incident.id)
            .options(selectinload(BulletinEntry.episode), selectinload(BulletinEntry.source))
            .order_by(BulletinEntry.effective_at.desc(), BulletinEntry.entry_id.desc())
        )
        .scalars()
        .all()
    )
    return AdminIncidentBulletinEntriesResponse(
        fire_id=fire_id, entries=[_entry_response(entry) for entry in rows]
    )


def update_admin_incident_bulletin(
    session: Session,
    *,
    fire_id: str,
    payload: AdminIncidentBulletinUpdateRequest,
    actor: Actor,
    trace_id: str,
) -> AdminIncidentBulletinUpdateResponse:
    begin_write_transaction(session)
    incident = _incident(session, fire_id, for_update=True)
    if incident.version != payload.expected_version:
        raise ConflictError(
            "incident_version_conflict",
            "Le bulletin a été modifié par un autre opérateur.",
        )
    source = _active_source(session, payload.source_key)
    before = incident_snapshot(incident)
    if payload.canonical_name is not None:
        incident.canonical_name = payload.canonical_name
    if payload.public_note is not None:
        incident.public_note = payload.public_note
    incident.version += 1
    current = next((episode for episode in incident.episodes if episode.is_current), None)
    now = utcnow()
    if current is not None:
        current.verification_state = VerificationState.VERIFIED
        current.review_required = False
        current.validated_at = now
        current.version += 1
    record_operator_audit(
        session,
        actor=actor,
        action="incident_bulletin.updated",
        target_type="incident_series",
        target_id=incident.fire_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after=incident_snapshot(incident),
        payload={"source_key": source.source_key, "direct_publication": True},
    )
    session.commit()
    return AdminIncidentBulletinUpdateResponse(
        fire_id=incident.fire_id,
        canonical_name=incident.canonical_name,
        public_note=incident.public_note,
        source_key=source.source_key,
        validated_at=as_utc(now),
        version=incident.version,
    )


def create_admin_incident_bulletin_entry(
    session: Session,
    *,
    fire_id: str,
    payload: AdminIncidentBulletinEntryCreateRequest,
    actor: Actor,
    trace_id: str,
) -> AdminIncidentBulletinEntry:
    begin_write_transaction(session)
    incident = _incident(session, fire_id, for_update=True)
    source = _active_source(session, payload.source_key)
    episode = None
    if payload.episode_id is not None:
        episode = next(
            (item for item in incident.episodes if item.episode_id == payload.episode_id),
            None,
        )
        if episode is None:
            raise ConflictError(
                "incident_episode_mismatch",
                "L'episode ne correspond pas à cet incident.",
            )
    now = utcnow()
    entry = BulletinEntry(
        entry_id=new_prefixed_id("bulletin"),
        incident_id=incident.id,
        episode_id=episode.id if episode else None,
        source_id=source.id,
        kind=payload.kind,
        body=payload.body,
        effective_at=payload.effective_at,
        published_at=now,
        state="PUBLISHED",
        reason=payload.reason,
        created_by=actor.actor_id,
    )
    # Keep the already-resolved relationships available after the commit.  This
    # avoids a lazy relationship refresh on a just-written entry and keeps the
    # response strictly scoped to the selected incident and source.
    entry.episode = episode
    entry.source = source
    session.add(entry)
    incident.version += 1
    record_operator_audit(
        session,
        actor=actor,
        action="incident_bulletin_entry.published",
        target_type="incident_bulletin_entry",
        target_id=entry.entry_id,
        reason=payload.reason,
        trace_id=trace_id,
        payload={
            "incident_fire_id": incident.fire_id,
            "episode_id": episode.episode_id if episode else None,
            "source_key": source.source_key,
            "kind": entry.kind,
            "direct_publication": True,
        },
    )
    session.commit()
    return _entry_response(entry)


def retire_admin_incident_bulletin_entry(
    session: Session,
    *,
    fire_id: str,
    entry_id: str,
    payload: AdminIncidentBulletinEntryRetireRequest,
    actor: Actor,
    trace_id: str,
) -> AdminIncidentBulletinEntry:
    begin_write_transaction(session)
    incident = _incident(session, fire_id, for_update=True)
    entry = session.execute(
        select(BulletinEntry)
        .where(BulletinEntry.incident_id == incident.id, BulletinEntry.entry_id == entry_id)
        .options(selectinload(BulletinEntry.episode), selectinload(BulletinEntry.source))
        .with_for_update()
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("incident_bulletin_entry", entry_id)
    if entry.version != payload.expected_version:
        raise ConflictError("bulletin_entry_version_conflict", "Cette entrée a été modifiée.")
    if entry.state == "RETIRED":
        raise ConflictError("bulletin_entry_retired", "Cette entrée est déjà retirée.")
    before = {"state": entry.state, "version": entry.version}
    entry.state = "RETIRED"
    entry.retired_at = utcnow()
    entry.retired_by = actor.actor_id
    entry.retirement_reason = payload.reason
    entry.version += 1
    incident.version += 1
    record_operator_audit(
        session,
        actor=actor,
        action="incident_bulletin_entry.retired",
        target_type="incident_bulletin_entry",
        target_id=entry.entry_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after={"state": entry.state, "version": entry.version},
        payload={"incident_fire_id": incident.fire_id, "source_key": entry.source.source_key},
    )
    session.commit()
    return _entry_response(entry)


def get_admin_incident_publication_status(
    session: Session, *, fire_id: str
) -> AdminIncidentPublicationStatus:
    incident = _incident(session, fire_id)
    current = next((episode for episode in incident.episodes if episode.is_current), None)
    gallery = (
        session.execute(
            select(IncidentGalleryItem).where(IncidentGalleryItem.incident_id == incident.id)
        )
        .scalars()
        .all()
    )
    published_gallery = [item for item in gallery if item.state == "PUBLISHED"]
    pending_gallery = [item for item in gallery if item.state == "PROPOSED"]
    spatial = get_spatial_review_workspace(session, fire_id=fire_id).scene
    bulletin_public = incident.public_visibility.value == "PUBLIC"
    bulletin = AdminIncidentPublicationDomain(
        domain="bulletin",
        state="PUBLISHED" if bulletin_public else "PRIVATE",
        preview_available=bulletin_public,
        destination=f"/admin/incidents/{fire_id}",
        action="edit_directly",
        checks=[
            AdminPublicationCheck(
                code="incident_public",
                label="Incident rendu public",
                satisfied=bulletin_public,
            ),
            AdminPublicationCheck(
                code="current_episode_verified",
                label="Épisode courant vérifié",
                satisfied=(
                    current is not None and current.verification_state == VerificationState.VERIFIED
                ),
            ),
        ],
        blockers=([] if bulletin_public else ["L'incident n'est pas encore visible publiquement."]),
    )
    gallery_domain = AdminIncidentPublicationDomain(
        domain="gallery",
        state="PUBLISHED" if published_gallery else "PENDING" if pending_gallery else "EMPTY",
        preview_available=bool(gallery),
        destination=f"/admin/incidents/{fire_id}/galerie",
        action="review_gallery" if pending_gallery else None,
        checks=[
            AdminPublicationCheck(
                code="editorial_items_ready",
                label="Média, crédit, licence et provenance renseignés",
                satisfied=all(
                    bool(item.media_url and item.alt_text and item.source_reference_url)
                    for item in pending_gallery
                ),
            ),
            AdminPublicationCheck(
                code="independent_review",
                label="Décision éditoriale distincte de l'agent",
                satisfied=True,
            ),
        ],
        blockers=(
            ["Aucun élément de galerie proposé."]
            if not gallery
            else ["Des éléments éditoriaux attendent une décision."]
            if pending_gallery
            else []
        ),
    )
    if spatial is None:
        spatial_domain = AdminIncidentPublicationDomain(
            domain="spatial",
            state="NOT_AVAILABLE",
            preview_available=False,
            destination=f"/admin/incidents/{fire_id}/revue-spatiale",
            checks=[
                AdminPublicationCheck(
                    code="scene_linked",
                    label="Scène 3D liée",
                    satisfied=False,
                )
            ],
            blockers=["Aucune scène 3D n'est liée à cet incident."],
        )
    else:
        package_ready = spatial.package_state in {"PREVIEWABLE", "PUBLISHED"}
        publication_ready = spatial.publication_id is not None
        spatial_domain = AdminIncidentPublicationDomain(
            domain="spatial",
            state=spatial.publication_state or spatial.package_state or "DRAFT",
            preview_available=package_ready,
            destination=f"/admin/incidents/{fire_id}/revue-spatiale",
            action="open_spatial_review",
            checks=[
                AdminPublicationCheck(
                    code="package_previewable",
                    label="Package vérifié et prévisualisable",
                    satisfied=package_ready,
                ),
                AdminPublicationCheck(
                    code="publication_registered",
                    label="Cycle de publication enregistré",
                    satisfied=publication_ready,
                ),
                AdminPublicationCheck(
                    code="unity_review",
                    label="Validation Unity manuelle à confirmer dans la revue spatiale",
                    satisfied=bool(spatial.publication_active),
                ),
            ],
            blockers=(
                []
                if spatial.publication_active
                else ["La publication spatiale reste soumise aux contrôles spatiaux et Unity."]
            ),
        )
    return AdminIncidentPublicationStatus(
        fire_id=fire_id,
        generated_at=as_utc(utcnow()),
        bulletin=bulletin,
        gallery=gallery_domain,
        spatial=spatial_domain,
    )
