from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.security import Actor
from fire_viewer.db.models import Source
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import SourceTrust
from fire_viewer.domain.errors import ConflictError
from fire_viewer.domain.hashing import sha256_text
from fire_viewer.domain.schemas import SourceResponse, SourceUpsertRequest
from fire_viewer.services.common import record_operator_audit, source_snapshot


def upsert_source(
    session: Session,
    *,
    source_key: str,
    payload: SourceUpsertRequest,
    actor: Actor,
    trace_id: str,
) -> SourceResponse:
    begin_write_transaction(session)
    source = session.execute(
        select(Source).where(Source.source_key == source_key).with_for_update()
    ).scalar_one_or_none()
    before = source_snapshot(source) if source else None
    existing_credential_hash = source.credential_hash if source else None
    credential_hash = (
        sha256_text(payload.ingest_token) if payload.ingest_token else existing_credential_hash
    )

    if payload.trust != SourceTrust.UNVERIFIED and credential_hash is None:
        raise ConflictError(
            "source_credential_required",
            "A trusted source must have an ingest_token before it can be enabled.",
        )

    if source is None:
        source = Source(
            source_key=source_key,
            source_type=payload.type,
            trust=payload.trust,
            display_name=payload.display_name,
            public_display_name=payload.public_display_name,
            public_license=payload.public_license,
            public_reference_url=payload.public_reference_url,
            public_transformations=list(payload.public_transformations),
            credential_hash=credential_hash,
            enabled=payload.enabled,
        )
        session.add(source)
    else:
        source.source_type = payload.type
        source.trust = payload.trust
        source.display_name = payload.display_name
        source.public_display_name = payload.public_display_name
        source.public_license = payload.public_license
        source.public_reference_url = payload.public_reference_url
        source.public_transformations = list(payload.public_transformations)
        source.credential_hash = credential_hash
        source.enabled = payload.enabled
    session.flush()
    after = source_snapshot(source)
    if before != after or payload.ingest_token is not None:
        record_operator_audit(
            session,
            actor=actor,
            action="source.registry.upserted",
            target_type="source",
            target_id=source.source_key,
            reason=payload.reason,
            trace_id=trace_id,
            before=before,
            after=after,
            payload={"credential_rotated": payload.ingest_token is not None},
        )
    session.commit()
    return SourceResponse(
        id=source.source_key,
        type=source.source_type,
        trust=source.trust,
        display_name=source.display_name,
        public_display_name=source.public_display_name,
        public_license=source.public_license,
        public_reference_url=source.public_reference_url,
        public_transformations=list(source.public_transformations),
        enabled=source.enabled,
        credential_configured=source.credential_hash is not None,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )
