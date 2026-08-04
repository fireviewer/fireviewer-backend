"""Targeted retention cleanup for unattached event evidence."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import EvidenceAsset
from fire_viewer.domain.enums import ActorType, EvidenceAssetState
from fire_viewer.services.common import record_audit
from fire_viewer.storage import ObjectStorageError, build_object_store


@dataclass(frozen=True, slots=True)
class EventEvidenceCleanupReport:
    uploads_removed: int = 0
    assets_removed: int = 0
    bytes_removed: int = 0
    failures: int = 0


def purge_due_event_evidence(
    session: Session,
    *,
    settings: Settings,
    limit: int = 100,
) -> EventEvidenceCleanupReport:
    """Delete only expired upload groups that were never attached to a candidate."""

    now = utcnow()
    due_upload_ids = list(
        session.scalars(
            select(EvidenceAsset.upload_id)
            .where(
                EvidenceAsset.event_candidate_id.is_(None),
                EvidenceAsset.purged_at.is_(None),
                EvidenceAsset.purge_after.is_not(None),
                EvidenceAsset.purge_after <= now,
            )
            .distinct()
            .order_by(EvidenceAsset.upload_id)
            .limit(limit)
        )
    )
    store = build_object_store(settings)
    uploads_removed = 0
    assets_removed = 0
    bytes_removed = 0
    failures = 0
    for upload_id in due_upload_ids:
        rows = list(
            session.scalars(
                select(EvidenceAsset)
                .where(EvidenceAsset.upload_id == upload_id)
                .order_by(EvidenceAsset.id)
                .with_for_update()
            )
        )
        if not rows or any(
            row.event_candidate_id is not None
            or row.purged_at is not None
            or row.purge_after is None
            or as_utc(row.purge_after) > now
            for row in rows
        ):
            continue
        group_bytes = 0
        for row in rows:
            try:
                group_bytes += store.head(row.object_uri).size_bytes
            except ObjectStorageError:
                # An absent object is already physically clean; the registry
                # still needs an auditable terminal purge marker.
                continue
        try:
            store.delete_tree(f"source-packages/{upload_id}")
        except ObjectStorageError:
            failures += 1
            record_audit(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id="event-retention",
                action="event.evidence_cleanup.failed",
                target_type="evidence_upload",
                target_id=upload_id,
                reason="Expired unattached evidence cleanup failed and remains retryable.",
                trace_id=f"event-cleanup:{upload_id}",
                after={"asset_count": len(rows), "size_bytes": group_bytes},
            )
            session.commit()
            continue
        for row in rows:
            row.purged_at = now
            row.purge_after = None
            row.state = EvidenceAssetState.REJECTED
            row.metadata_payload = {
                **row.metadata_payload,
                "cleanup": {
                    "at": now.isoformat(),
                    "reason": "expired_unattached_upload",
                    "size_bytes": row.size_bytes,
                },
            }
        uploads_removed += 1
        assets_removed += len(rows)
        bytes_removed += group_bytes
        record_audit(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id="event-retention",
            action="event.evidence_cleanup.completed",
            target_type="evidence_upload",
            target_id=upload_id,
            reason="Expired unattached evidence was removed after the retention window.",
            trace_id=f"event-cleanup:{upload_id}",
            after={"asset_count": len(rows), "size_bytes": group_bytes},
        )
        session.commit()
    return EventEvidenceCleanupReport(
        uploads_removed=uploads_removed,
        assets_removed=assets_removed,
        bytes_removed=bytes_removed,
        failures=failures,
    )
