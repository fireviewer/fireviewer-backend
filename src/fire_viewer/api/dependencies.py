from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import Depends, Header, Path, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import (
    FIRE_ID_RE,
    IDEMPOTENCY_KEY_RE,
    SOURCE_KEY_RE,
    new_trace_id,
)
from fire_viewer.core.security import Actor, actor_from_request, bearer_scheme
from fire_viewer.domain.errors import BadRequestError


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    except BaseException:
        # Services own their successful commits.  A request that exits through an
        # exception must never leave a partially flushed write transaction to be
        # implicitly dealt with by Session.close().  In particular, a failed
        # detection may have discovered a source before later request validation
        # rejects the observation.
        session.rollback()
        raise
    finally:
        # Read paths also open an implicit transaction on SQLite.  Close it
        # explicitly so dependency teardown has one deterministic transaction
        # boundary regardless of how the endpoint returned.
        if session.in_transaction():
            session.rollback()
        session.close()


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or new_trace_id()


def get_source_token(
    value: Annotated[
        str | None,
        Header(alias="X-Source-Token", min_length=32, max_length=256),
    ] = None,
) -> str | None:
    return value


def get_idempotency_key(
    value: Annotated[str, Header(alias="Idempotency-Key")],
) -> str:
    if not IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise BadRequestError(
            "invalid_idempotency_key",
            "Idempotency-Key must contain 8-128 safe characters.",
        )
    return value


def get_fire_id(fire_id: Annotated[str, Path()]) -> str:
    if not FIRE_ID_RE.fullmatch(fire_id):
        raise BadRequestError("invalid_fire_id", "fire_id has an invalid format.")
    return fire_id


def get_source_key(source_key: Annotated[str, Path(min_length=3, max_length=128)]) -> str:
    if not SOURCE_KEY_RE.fullmatch(source_key):
        raise BadRequestError("invalid_source_id", "source id has an invalid format.")
    return source_key


def get_actor(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
    session: Annotated[Session, Depends(get_session)],
) -> Actor:
    return actor_from_request(request, credentials, session)


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
TraceIdDep = Annotated[str, Depends(get_trace_id)]
IdempotencyKeyDep = Annotated[str, Depends(get_idempotency_key)]
SourceTokenDep = Annotated[str | None, Depends(get_source_token)]
FireIdDep = Annotated[str, Depends(get_fire_id)]
SourceKeyDep = Annotated[str, Depends(get_source_key)]
ActorDep = Annotated[Actor, Depends(get_actor)]
