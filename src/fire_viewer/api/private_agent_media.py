"""Signed, consent-aware access to private media for the GPU worker only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from fire_viewer.api.dependencies import SessionDep, SettingsDep
from fire_viewer.services.agent_source_packages import read_private_source_media

router = APIRouter(prefix="/api/v2/private-agent-media", tags=["private-agent-media"])


@router.get("/{item_id}", include_in_schema=False)
def download_private_agent_media(
    item_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")],
    token: Annotated[str, Query(min_length=64, max_length=4_096)],
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    payload = read_private_source_media(
        session,
        item_id=item_id,
        token=token,
        settings=settings,
    )
    filename = payload.filename.replace('"', "")
    return Response(
        content=payload.content,
        media_type=payload.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
