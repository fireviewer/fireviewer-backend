from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import IdempotencyRecord
from fire_viewer.domain.errors import ConflictError


@dataclass(frozen=True, slots=True)
class Replay:
    response_status: int
    response_body: dict[str, Any]
    trace_id: str


def find_replay(
    session: Session,
    *,
    endpoint: str,
    idempotency_key: str,
    request_hash: str,
) -> Replay | None:
    record = session.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    if as_utc(record.expires_at) <= utcnow():
        session.delete(record)
        session.flush()
        return None
    if record.request_hash != request_hash:
        raise ConflictError(
            "idempotency_key_reused",
            "The Idempotency-Key was already used with a different request body.",
        )
    return Replay(
        response_status=record.response_status,
        response_body=record.response_body,
        trace_id=record.trace_id,
    )


def store_response(
    session: Session,
    *,
    endpoint: str,
    idempotency_key: str,
    request_hash: str,
    response_status: int,
    response_body: dict[str, Any],
    trace_id: str,
    settings: Settings,
) -> None:
    session.add(
        IdempotencyRecord(
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
            trace_id=trace_id,
            expires_at=utcnow() + timedelta(hours=settings.idempotency_retention_hours),
        )
    )
