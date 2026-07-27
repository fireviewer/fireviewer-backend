"""Public incident discovery with an intentionally small, text-only projection."""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from fire_viewer.core.time import as_utc
from fire_viewer.db.models import Episode, IncidentSeries, Observation
from fire_viewer.domain.enums import PublicVisibility, VerificationState
from fire_viewer.domain.public_visibility import (
    PUBLIC_LOCATION_STATUSES,
    has_canonical_public_visibility,
)
from fire_viewer.domain.schemas import IncidentDiscoveryItem, IncidentDiscoveryResponse

PUBLIC_DISCOVERY_LIMIT = 20


@dataclass(frozen=True)
class PublicIncidentRow:
    incident: IncidentSeries
    episode: Episode


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _base_public_statement() -> Select[tuple[IncidentSeries, Episode]]:
    return (
        select(IncidentSeries, Episode)
        .join(Episode, Episode.incident_id == IncidentSeries.id)
        .where(
            Episode.is_current.is_(True),
            IncidentSeries.public_visibility == PublicVisibility.PUBLIC,
            Episode.status.in_(PUBLIC_LOCATION_STATUSES),
            Episode.verification_state.in_(
                [VerificationState.CORROBORATED, VerificationState.VERIFIED]
            ),
        )
    )


def _is_safe_public_row(row: PublicIncidentRow) -> bool:
    return (
        row.episode.status in PUBLIC_LOCATION_STATUSES
        and row.incident.public_visibility == PublicVisibility.PUBLIC
        and row.episode.verification_state
        in {VerificationState.CORROBORATED, VerificationState.VERIFIED}
        and has_canonical_public_visibility(
            row.episode.status,
            row.incident.public_visibility,
            row.episode.verification_state,
        )
    )


def _to_item(row: PublicIncidentRow) -> IncidentDiscoveryItem:
    return IncidentDiscoveryItem(
        fire_id=row.incident.fire_id,
        canonical_name=(
            row.incident.canonical_name or row.incident.fire_id
            if row.episode.verification_state == VerificationState.VERIFIED
            else row.incident.fire_id
        ),
        status=row.episode.status,
        verification=(
            "verified"
            if row.episode.verification_state == VerificationState.VERIFIED
            else "corroborated"
        ),
        last_observed_at=as_utc(row.episode.last_observed_at),
    )


def _haversine_metres(
    longitude_a: float,
    latitude_a: float,
    longitude_b: float,
    latitude_b: float,
) -> float:
    earth_radius_metres = 6_371_008.8
    delta_latitude = radians(latitude_b - latitude_a)
    delta_longitude = radians(longitude_b - longitude_a)
    latitude_a_rad = radians(latitude_a)
    latitude_b_rad = radians(latitude_b)
    value = (
        sin(delta_latitude / 2) ** 2
        + cos(latitude_a_rad) * cos(latitude_b_rad) * sin(delta_longitude / 2) ** 2
    )
    return 2 * earth_radius_metres * asin(sqrt(value))


def _ordered_items(rows: list[PublicIncidentRow], limit: int) -> IncidentDiscoveryResponse:
    safe_rows = [row for row in rows if _is_safe_public_row(row)]
    safe_rows.sort(key=lambda row: row.episode.last_observed_at, reverse=True)
    return IncidentDiscoveryResponse(incidents=[_to_item(row) for row in safe_rows[:limit]])


def list_recent_public_incidents(
    session: Session, *, limit: int = PUBLIC_DISCOVERY_LIMIT
) -> IncidentDiscoveryResponse:
    """Return only recent, current and canonically public incident summaries."""

    # Fetch a small surplus: a corrupted persisted pair is discarded rather than exposed.
    records = session.execute(
        _base_public_statement().order_by(Episode.last_observed_at.desc()).limit(limit * 5)
    ).all()
    rows = [PublicIncidentRow(incident=incident, episode=episode) for incident, episode in records]
    return _ordered_items(rows, limit)


def search_public_incidents(
    session: Session,
    *,
    query: str | None = None,
    longitude: float | None = None,
    latitude: float | None = None,
    radius_km: float | None = None,
    limit: int = PUBLIC_DISCOVERY_LIMIT,
) -> IncidentDiscoveryResponse:
    """Search published incidents by text or approximate coordinate input.

    Coordinates are used only as an input filter. They are never returned in the
    discovery projection.
    """

    statement = _base_public_statement()
    if query is not None:
        needle = _escape_like(query.casefold())
        pattern = f"%{needle}%"
        verified_toponym_match = exists(
            select(Observation.id).where(
                Observation.attached_incident_id == IncidentSeries.id,
                Observation.verification_state == VerificationState.VERIFIED,
                func.lower(cast(Observation.toponyms, String)).like(pattern, escape="\\"),
            )
        )
        statement = statement.where(
            or_(
                func.lower(IncidentSeries.fire_id).like(pattern, escape="\\"),
                and_(
                    Episode.verification_state == VerificationState.VERIFIED,
                    func.lower(IncidentSeries.canonical_name).like(pattern, escape="\\"),
                ),
                verified_toponym_match,
            )
        )
    elif longitude is not None and latitude is not None and radius_km is not None:
        # First use the persisted envelope to keep the query local; a precise
        # great-circle check below avoids declaring the envelope itself a match.
        generalized_margin_km = 1.5
        latitude_delta = (radius_km + generalized_margin_km) / 110.574
        longitude_delta = (radius_km + generalized_margin_km) / max(
            111.320 * cos(radians(latitude)), 0.001
        )
        statement = statement.where(
            IncidentSeries.reference_lon.between(
                longitude - longitude_delta, longitude + longitude_delta
            ),
            IncidentSeries.reference_lat.between(
                latitude - latitude_delta, latitude + latitude_delta
            ),
        )
    else:
        return IncidentDiscoveryResponse()

    records = session.execute(
        statement.order_by(Episode.last_observed_at.desc()).limit(limit * 5)
    ).all()
    rows = [PublicIncidentRow(incident=incident, episode=episode) for incident, episode in records]
    if longitude is not None and latitude is not None and radius_km is not None:
        radius_metres = radius_km * 1_000
        rows = [
            row
            for row in rows
            if _haversine_metres(
                longitude,
                latitude,
                (
                    row.incident.reference_lon
                    if row.episode.verification_state == VerificationState.VERIFIED
                    else round(row.incident.reference_lon, 2)
                ),
                (
                    row.incident.reference_lat
                    if row.episode.verification_state == VerificationState.VERIFIED
                    else round(row.incident.reference_lat, 2)
                ),
            )
            <= radius_metres
        ]
    return _ordered_items(rows, limit)
