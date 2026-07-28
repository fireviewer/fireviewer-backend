from __future__ import annotations

from math import isfinite
from typing import Any

SUPPORTED_GEOMETRY_TYPES = frozenset(
    {"Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
)


def _position(value: object, *, normalized: bool) -> tuple[float, ...]:
    if not isinstance(value, list | tuple) or len(value) not in {2, 3}:
        raise ValueError("GeoJSON positions require two or three coordinates")
    if any(isinstance(item, bool) or not isinstance(item, int | float) for item in value):
        raise ValueError("GeoJSON position coordinates must be finite numbers")
    coordinates = tuple(float(item) for item in value)
    if not all(isfinite(item) for item in coordinates):
        raise ValueError("GeoJSON position coordinates must be finite numbers")
    x, y = coordinates[:2]
    if normalized:
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError("source geometry coordinates must be normalized")
    elif not -180 <= x <= 180 or not -90 <= y <= 90:
        raise ValueError("geographic geometry coordinates must use WGS84 longitude/latitude")
    return coordinates


def _line(value: object, *, normalized: bool) -> list[tuple[float, ...]]:
    if not isinstance(value, list | tuple) or len(value) < 2:
        raise ValueError("GeoJSON lines require at least two positions")
    return [_position(item, normalized=normalized) for item in value]


def _ring(value: object, *, normalized: bool) -> list[tuple[float, ...]]:
    positions = _line(value, normalized=normalized)
    if len(positions) < 4 or positions[0] != positions[-1]:
        raise ValueError("GeoJSON polygon rings must contain four positions and be closed")
    return positions


def validate_geojson_geometry(
    value: object,
    *,
    allowed_types: set[str] | frozenset[str] = SUPPORTED_GEOMETRY_TYPES,
    normalized: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("geometry must be a GeoJSON object")
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")
    if not isinstance(geometry_type, str) or geometry_type not in allowed_types:
        raise ValueError("geometry type is not allowed for this proposal")
    if coordinates is None:
        raise ValueError("GeoJSON geometry requires coordinates")

    if geometry_type == "Point":
        _position(coordinates, normalized=normalized)
    elif geometry_type == "LineString":
        _line(coordinates, normalized=normalized)
    elif geometry_type == "MultiLineString":
        if not isinstance(coordinates, list | tuple) or not coordinates:
            raise ValueError("GeoJSON MultiLineString requires at least one line")
        for line in coordinates:
            _line(line, normalized=normalized)
    elif geometry_type == "Polygon":
        if not isinstance(coordinates, list | tuple) or not coordinates:
            raise ValueError("GeoJSON Polygon requires at least one ring")
        for ring in coordinates:
            _ring(ring, normalized=normalized)
    else:
        if not isinstance(coordinates, list | tuple) or not coordinates:
            raise ValueError("GeoJSON MultiPolygon requires at least one polygon")
        for polygon in coordinates:
            if not isinstance(polygon, list | tuple) or not polygon:
                raise ValueError("GeoJSON MultiPolygon members require at least one ring")
            for ring in polygon:
                _ring(ring, normalized=normalized)
    return dict(value)
