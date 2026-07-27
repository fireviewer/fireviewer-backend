from __future__ import annotations

from sqlalchemy import and_, case, select, text
from sqlalchemy.orm import Session

from fire_viewer.db.models import Episode, IncidentSeries
from fire_viewer.domain.enums import PublicVisibility
from fire_viewer.domain.geospatial import BoundingBox, haversine_m
from fire_viewer.domain.matching import Candidate


def _to_candidate(incident: IncidentSeries, episode: Episode) -> Candidate:
    return Candidate(
        incident_db_id=incident.id,
        episode_db_id=episode.id,
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        reference_lon=incident.reference_lon,
        reference_lat=incident.reference_lat,
        uncertainty_m=incident.horizontal_uncertainty_m,
        canonical_name=incident.canonical_name,
        status=episode.status,
        started_at=episode.started_at,
        last_observed_at=episode.last_observed_at,
        ended_at=episode.ended_at,
    )


def find_candidates(
    session: Session,
    *,
    bbox: BoundingBox,
    limit: int,
) -> tuple[list[Candidate], bool]:
    bind = session.get_bind()
    fetch_limit = limit + 1

    if bind.dialect.name == "sqlite":
        ids = list(
            session.execute(
                text(
                    """
                    SELECT i.id
                    FROM incident_series_rtree AS r
                    JOIN incident_series AS i ON i.id = r.id
                    WHERE r.max_lon >= :min_lon
                      AND r.min_lon <= :max_lon
                      AND r.max_lat >= :min_lat
                      AND r.min_lat <= :max_lat
                      AND i.public_visibility != :tombstoned
                    ORDER BY i.updated_at DESC, i.fire_id ASC, i.id ASC
                    LIMIT :fetch_limit
                    """
                ),
                {
                    "min_lon": bbox.min_lon,
                    "max_lon": bbox.max_lon,
                    "min_lat": bbox.min_lat,
                    "max_lat": bbox.max_lat,
                    "tombstoned": PublicVisibility.TOMBSTONED.name,
                    "fetch_limit": fetch_limit,
                },
            ).scalars()
        )
        if not ids:
            return [], False
        ordering = case(
            {incident_id: index for index, incident_id in enumerate(ids)},
            value=IncidentSeries.id,
        )
        rows = session.execute(
            select(IncidentSeries, Episode)
            .join(
                Episode,
                and_(Episode.incident_id == IncidentSeries.id, Episode.is_current.is_(True)),
            )
            .where(IncidentSeries.id.in_(ids))
            .order_by(ordering)
        ).all()
        candidates = [_to_candidate(incident, episode) for incident, episode in rows]
    elif bind.dialect.name == "postgresql":
        longitude = (bbox.min_lon + bbox.max_lon) / 2
        latitude = (bbox.min_lat + bbox.max_lat) / 2
        radius_m = max(
            haversine_m(longitude, latitude, bbox.min_lon, bbox.min_lat),
            haversine_m(longitude, latitude, bbox.max_lon, bbox.max_lat),
        )
        ids = list(
            session.execute(
                text(
                    """
                    SELECT i.id
                    FROM incident_series AS i
                    WHERE ST_DWithin(
                        i.reference_geog,
                        ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography,
                        :radius_m
                    )
                      AND i.public_visibility != :tombstoned
                    ORDER BY i.updated_at DESC, i.fire_id ASC, i.id ASC
                    LIMIT :fetch_limit
                    """
                ),
                {
                    "longitude": longitude,
                    "latitude": latitude,
                    "radius_m": radius_m,
                    "tombstoned": PublicVisibility.TOMBSTONED.name,
                    "fetch_limit": fetch_limit,
                },
            ).scalars()
        )
        if not ids:
            return [], False
        ordering = case(
            {incident_id: index for index, incident_id in enumerate(ids)},
            value=IncidentSeries.id,
        )
        rows = session.execute(
            select(IncidentSeries, Episode)
            .join(
                Episode,
                and_(Episode.incident_id == IncidentSeries.id, Episode.is_current.is_(True)),
            )
            .where(IncidentSeries.id.in_(ids))
            .order_by(ordering)
        ).all()
        candidates = [_to_candidate(incident, episode) for incident, episode in rows]
    else:
        statement = (
            select(IncidentSeries, Episode)
            .join(
                Episode,
                and_(Episode.incident_id == IncidentSeries.id, Episode.is_current.is_(True)),
            )
            .where(
                IncidentSeries.bbox_max_lon >= bbox.min_lon,
                IncidentSeries.bbox_min_lon <= bbox.max_lon,
                IncidentSeries.bbox_max_lat >= bbox.min_lat,
                IncidentSeries.bbox_min_lat <= bbox.max_lat,
                IncidentSeries.public_visibility != PublicVisibility.TOMBSTONED,
            )
            .order_by(
                IncidentSeries.updated_at.desc(),
                IncidentSeries.fire_id.asc(),
                IncidentSeries.id.asc(),
                Episode.ordinal.asc(),
                Episode.id.asc(),
            )
            .limit(fetch_limit)
        )
        candidates = [
            _to_candidate(incident, episode)
            for incident, episode in session.execute(statement).all()
        ]

    overflow = len(candidates) > limit
    return candidates[:limit], overflow
