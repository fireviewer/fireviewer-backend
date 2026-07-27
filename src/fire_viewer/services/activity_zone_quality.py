"""Advisory comparison of a private activity-zone draft with same-day official geometry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import transform, unary_union

_WGS84_TO_L93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)


@dataclass(frozen=True, slots=True)
class ActivityZoneComparison:
    local_date: date
    assessment: Literal["coherent", "a_revoir"]
    intersection_over_union: float
    predicted_area_ha: float
    official_area_ha: float
    predicted_covered_percent: float
    official_covered_percent: float
    centroid_distance_m: float
    advisory_only: Literal[True] = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _polygonal_geometry(payload: dict[str, Any]) -> MultiPolygon:
    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        geometries = [
            shape(feature["geometry"])
            for feature in payload.get("features", [])
            if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
        ]
        geometry = unary_union(
            [item for item in geometries if isinstance(item, Polygon | MultiPolygon)]
        )
    elif payload_type == "Feature":
        geometry_payload = payload.get("geometry")
        if not isinstance(geometry_payload, dict):
            raise ValueError("GeoJSON feature has no geometry")
        geometry = shape(geometry_payload)
    else:
        geometry = shape(payload)
    if isinstance(geometry, Polygon):
        geometry = MultiPolygon([geometry])
    if not isinstance(geometry, MultiPolygon) or geometry.is_empty or not geometry.is_valid:
        raise ValueError("activity-zone comparison requires a valid Polygon or MultiPolygon")
    return geometry


def compare_activity_zones(
    predicted_geojson: dict[str, Any],
    official_geojson: dict[str, Any],
    *,
    predicted_local_date: date,
    official_local_date: date,
) -> ActivityZoneComparison:
    """Measure plausibility only; this result can never approve or publish a layer."""

    if predicted_local_date != official_local_date:
        raise ValueError("predicted and official activity zones must describe the same local date")
    predicted = transform(_WGS84_TO_L93.transform, _polygonal_geometry(predicted_geojson))
    official = transform(_WGS84_TO_L93.transform, _polygonal_geometry(official_geojson))
    intersection_area = predicted.intersection(official).area
    union_area = predicted.union(official).area
    predicted_area = predicted.area
    official_area = official.area
    iou = intersection_area / union_area if union_area else 0.0
    predicted_covered = intersection_area / predicted_area if predicted_area else 0.0
    official_covered = intersection_area / official_area if official_area else 0.0
    centroid_distance = predicted.centroid.distance(official.centroid)
    area_ratio = predicted_area / official_area if official_area else float("inf")
    coherent = (
        centroid_distance <= 2_000
        and 0.05 <= area_ratio <= 20
        and (iou >= 0.15 or predicted_covered >= 0.2 or official_covered >= 0.2)
    )
    return ActivityZoneComparison(
        local_date=predicted_local_date,
        assessment="coherent" if coherent else "a_revoir",
        intersection_over_union=round(iou, 6),
        predicted_area_ha=round(predicted_area / 10_000, 3),
        official_area_ha=round(official_area / 10_000, 3),
        predicted_covered_percent=round(predicted_covered * 100, 2),
        official_covered_percent=round(official_covered * 100, 2),
        centroid_distance_m=round(centroid_distance, 1),
    )
