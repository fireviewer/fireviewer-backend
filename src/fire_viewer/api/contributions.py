from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from fire_viewer.api.dependencies import (
    ActorDep,
    IdempotencyKeyDep,
    SessionDep,
    SettingsDep,
    TraceIdDep,
)
from fire_viewer.core.security import require_role
from fire_viewer.domain.contribution_schemas import (
    AdminPublicContributionEnvelope,
    AdminPublicContributionListResponse,
    AdminPublicContributionReviewRequest,
    PublicContributionEnvelope,
    PublicContributionOpenRequest,
    PublicContributionOpenResponse,
)
from fire_viewer.domain.enums import PublicContributionState
from fire_viewer.domain.errors import UnauthorizedError
from fire_viewer.domain.schemas import AdminBlobUploadTokenRequest, AdminBlobUploadTokenResponse
from fire_viewer.services.blob_uploads import issue_blob_client_token
from fire_viewer.services.public_contributions import (
    finalize_public_contribution,
    get_public_contribution,
    list_public_contributions,
    open_public_contribution,
    review_public_contribution,
    withdraw_public_contribution,
)

router = APIRouter(tags=["public-contributions"])


def _tracking_token(
    authorization: Annotated[str | None, Header(alias="Authorization", max_length=512)] = None,
) -> str:
    if authorization is None:
        raise UnauthorizedError("A contribution tracking token is required.")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or len(token) < 32:
        raise UnauthorizedError("The contribution tracking token is invalid.")
    return token


TrackingTokenDep = Annotated[str, Depends(_tracking_token)]


@router.post("/contributions/blob-upload-token", response_model=AdminBlobUploadTokenResponse)
def grant_public_contribution_blob_token(
    payload: AdminBlobUploadTokenRequest,
    settings: SettingsDep,
    upload_grant: Annotated[
        str, Header(alias="X-Blob-Upload-Grant", min_length=64, max_length=4096)
    ],
) -> AdminBlobUploadTokenResponse:
    return AdminBlobUploadTokenResponse(
        clientToken=issue_blob_client_token(
            pathname=payload.payload.pathname,
            client_payload=payload.payload.clientPayload,
            upload_grant=upload_grant,
            settings=settings,
        )
    )


@router.post(
    "/contributions/open",
    response_model=PublicContributionOpenResponse,
    status_code=status.HTTP_201_CREATED,
)
def open_contribution(
    payload: PublicContributionOpenRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> PublicContributionOpenResponse:
    origin = request.client.host if request.client is not None else "unknown"
    result = open_public_contribution(
        session,
        payload=payload,
        idempotency_key=idempotency_key,
        origin=origin,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    if result.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return result


@router.post(
    "/contributions/{contribution_id}/finalize",
    response_model=PublicContributionEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
def finalize_contribution(
    contribution_id: str,
    response: Response,
    tracking_token: TrackingTokenDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> PublicContributionEnvelope:
    result = finalize_public_contribution(
        session,
        contribution_id=contribution_id,
        tracking_token=tracking_token,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/contributions/{contribution_id}", response_model=PublicContributionEnvelope)
def read_contribution(
    contribution_id: str,
    response: Response,
    tracking_token: TrackingTokenDep,
    session: SessionDep,
    trace_id: TraceIdDep,
) -> PublicContributionEnvelope:
    result = get_public_contribution(
        session,
        contribution_id=contribution_id,
        tracking_token=tracking_token,
        trace_id=trace_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/contributions/{contribution_id}/withdraw",
    response_model=PublicContributionEnvelope,
)
def withdraw_contribution(
    contribution_id: str,
    response: Response,
    tracking_token: TrackingTokenDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
) -> PublicContributionEnvelope:
    result = withdraw_public_contribution(
        session,
        contribution_id=contribution_id,
        tracking_token=tracking_token,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/admin/public-contributions", response_model=AdminPublicContributionListResponse)
def list_admin_contributions(
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    state: Annotated[PublicContributionState | None, Query()] = None,
) -> AdminPublicContributionListResponse:
    require_role(actor, "administrator", "validator")
    response.headers["Cache-Control"] = "no-store"
    return list_public_contributions(session, state=state, settings=settings)


@router.post(
    "/admin/public-contributions/{contribution_id}/review",
    response_model=AdminPublicContributionEnvelope,
)
def review_admin_contribution(
    contribution_id: str,
    payload: AdminPublicContributionReviewRequest,
    response: Response,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    trace_id: TraceIdDep,
    idempotency_key: IdempotencyKeyDep,
) -> AdminPublicContributionEnvelope:
    require_role(actor, "administrator", "validator")
    _ = idempotency_key
    result = review_public_contribution(
        session,
        contribution_id=contribution_id,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
