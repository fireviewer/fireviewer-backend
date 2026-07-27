"""Bounded public discovery endpoints; no spatial catalogue or map surface."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from fire_viewer.api.dependencies import SessionDep
from fire_viewer.domain.errors import BadRequestError
from fire_viewer.domain.schemas import IncidentDiscoveryResponse
from fire_viewer.services.discovery import (
    PUBLIC_DISCOVERY_LIMIT,
    list_recent_public_incidents,
    search_public_incidents,
)

router = APIRouter(prefix="/incidents", tags=["public-discovery"])

DiscoveryLimit = Annotated[int, Query(ge=1, le=PUBLIC_DISCOVERY_LIMIT)]
SearchQuery = Annotated[str | None, Query(min_length=2, max_length=160)]
Longitude = Annotated[float | None, Query(ge=-180.0, le=180.0)]
Latitude = Annotated[float | None, Query(ge=-90.0, le=90.0)]
RadiusKm = Annotated[float | None, Query(ge=1.0, le=100.0)]


def _set_public_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "public, max-age=15, must-revalidate"


@router.get("/recent", response_model=IncidentDiscoveryResponse)
def recent_public_incidents(
    response: Response,
    session: SessionDep,
    limit: DiscoveryLimit = PUBLIC_DISCOVERY_LIMIT,
) -> IncidentDiscoveryResponse:
    _set_public_headers(response)
    return list_recent_public_incidents(session, limit=limit)


@router.get("/search", response_model=IncidentDiscoveryResponse)
def search_incidents(
    response: Response,
    session: SessionDep,
    q: SearchQuery = None,
    longitude: Longitude = None,
    latitude: Latitude = None,
    radius_km: RadiusKm = None,
    limit: DiscoveryLimit = PUBLIC_DISCOVERY_LIMIT,
) -> IncidentDiscoveryResponse:
    text_query = q.strip() if q is not None else None
    has_coordinates = longitude is not None or latitude is not None or radius_km is not None
    coordinates_complete = longitude is not None and latitude is not None and radius_km is not None
    if text_query and has_coordinates:
        raise BadRequestError(
            "discovery_search_mode", "Use text or approximate coordinates, not both."
        )
    if text_query is None and not coordinates_complete:
        raise BadRequestError(
            "discovery_search_input",
            "Provide a text query or longitude, latitude and radius_km together.",
        )
    if text_query is not None and not text_query:
        raise BadRequestError("discovery_search_input", "The text query cannot be blank.")
    _set_public_headers(response)
    return search_public_incidents(
        session,
        query=text_query,
        longitude=longitude,
        latitude=latitude,
        radius_km=radius_km,
        limit=limit,
    )
