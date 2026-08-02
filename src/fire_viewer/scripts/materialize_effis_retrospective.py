"""Materialize a reviewed daily retrospective from an EFFIS seasonal burn-scar layer.

The EFFIS WMS layer gives a satellite-derived final scar, not a daily operational
front.  This tool therefore preserves that distinction: the final footprint is
captured once, while per-day cumulative and active polygons are reconstructed
only from an explicit, reviewed daily area schedule.  The result is a static
manifest for ``restore_die_retrospective``; no remote map is read at runtime.
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import httpx
import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.affinity import scale
from shapely.geometry import box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

EFFIS_WMS_URL = "https://maps.effis.emergency.copernicus.eu/effis"
EFFIS_LAYER = "modis.ba.season"
_TO_L93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True).transform
_FROM_L93 = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform


class MaterializationError(RuntimeError):
    """The reviewed specification cannot be transformed safely."""


def _polygonal(geometry: BaseGeometry) -> BaseGeometry:
    """Return only valid polygonal members after an overlay operation."""

    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    if geometry.geom_type == "GeometryCollection":
        geometry = unary_union(
            [
                member
                for member in geometry.geoms
                if member.geom_type in {"Polygon", "MultiPolygon"}
            ]
        )
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise MaterializationError("A retrospective geometry must remain polygonal.")
    return geometry


@dataclass(frozen=True)
class DailyTarget:
    local_date: str
    valid_at: str
    area_ha: float
    title: str
    summary: str
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class RetrospectiveSpec:
    dataset_id: str
    campaign_id: str
    identifier_suffix: str
    fire_id: str
    episode_id: str
    anchor_lon: float
    anchor_lat: float
    bbox: tuple[float, float, float, float]
    source_label: str
    source_url: str
    daily_targets: tuple[DailyTarget, ...]


def _load_spec(path: Path) -> RetrospectiveSpec:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MaterializationError("The EFFIS retrospective specification must be an object.")
    incident = raw.get("incident")
    anchor = raw.get("anchor")
    bbox = raw.get("bbox")
    days = raw.get("days")
    if not isinstance(incident, dict) or not isinstance(anchor, list) or len(anchor) != 2:
        raise MaterializationError("The EFFIS retrospective incident or anchor is invalid.")
    if not isinstance(bbox, list) or len(bbox) != 4 or not isinstance(days, list) or not days:
        raise MaterializationError("The EFFIS retrospective bbox or day schedule is invalid.")
    daily_targets: list[DailyTarget] = []
    for entry in days:
        if not isinstance(entry, dict):
            raise MaterializationError("A daily target must be an object.")
        source_urls = entry.get("source_urls", [])
        if not isinstance(source_urls, list) or not all(
            isinstance(value, str) and value.startswith("https://") for value in source_urls
        ):
            raise MaterializationError("A daily target source_urls value is invalid.")
        try:
            datetime.fromisoformat(str(entry["valid_at"]).replace("Z", "+00:00"))
            target = DailyTarget(
                local_date=str(entry["local_date"]),
                valid_at=str(entry["valid_at"]),
                area_ha=float(entry["area_ha"]),
                title=str(entry["title"]),
                summary=str(entry["summary"]),
                source_urls=tuple(source_urls),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MaterializationError("A daily target is incomplete.") from exc
        if target.area_ha <= 0 or not target.title.strip() or not target.summary.strip():
            raise MaterializationError("A daily target is invalid.")
        daily_targets.append(target)
    if [item.local_date for item in daily_targets] != sorted(
        item.local_date for item in daily_targets
    ):
        raise MaterializationError("The daily targets must be chronologically ordered.")
    if any(
        later.area_ha < earlier.area_ha
        for earlier, later in pairwise(daily_targets)
    ):
        raise MaterializationError("The cumulative daily areas must not decrease.")
    required = ("dataset_id", "campaign_id", "identifier_suffix", "source_label", "source_url")
    if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required):
        raise MaterializationError("The EFFIS retrospective identity is incomplete.")
    try:
        bbox_values = [float(value) for value in bbox]
        return RetrospectiveSpec(
            dataset_id=cast(str, raw["dataset_id"]),
            campaign_id=cast(str, raw["campaign_id"]),
            identifier_suffix=cast(str, raw["identifier_suffix"]),
            fire_id=str(incident["fire_id"]),
            episode_id=str(incident["episode_id"]),
            anchor_lon=float(anchor[0]),
            anchor_lat=float(anchor[1]),
            bbox=(bbox_values[0], bbox_values[1], bbox_values[2], bbox_values[3]),
            source_label=cast(str, raw["source_label"]),
            source_url=cast(str, raw["source_url"]),
            daily_targets=tuple(daily_targets),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MaterializationError("The EFFIS retrospective specification is invalid.") from exc


def _fetch_seasonal_mask(spec: RetrospectiveSpec, client: httpx.Client) -> Image.Image:
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    response = client.get(
        EFFIS_WMS_URL,
        params={
            "service": "WMS",
            "version": "1.3.0",
            "request": "GetMap",
            "layers": EFFIS_LAYER,
            "styles": "",
            "crs": "EPSG:4326",
            "bbox": f"{min_lat},{min_lon},{max_lat},{max_lon}",
            "width": "2048",
            "height": "2048",
            "format": "image/png",
            "transparent": "TRUE",
        },
    )
    response.raise_for_status()
    if not response.headers.get("content-type", "").startswith("image/png"):
        raise MaterializationError("EFFIS did not return the expected transparent PNG layer.")
    return Image.open(io.BytesIO(response.content)).convert("RGBA")


def _vectorize_mask(mask: Image.Image, bbox: tuple[float, float, float, float]) -> BaseGeometry:
    """Convert contiguous transparent-map pixels into a simplified geographic union."""

    min_lon, min_lat, max_lon, max_lat = bbox
    alpha = np.asarray(mask.getchannel("A"))
    height, width = alpha.shape
    rectangles: list[BaseGeometry] = []
    for row in range(height):
        opaque = alpha[row] > 0
        starts = np.flatnonzero(opaque & np.concatenate(([True], ~opaque[:-1])))
        ends = np.flatnonzero(opaque & np.concatenate((~opaque[1:], [True]))) + 1
        for start, end in zip(starts, ends, strict=True):
            west = min_lon + (max_lon - min_lon) * float(start) / width
            east = min_lon + (max_lon - min_lon) * float(end) / width
            north = max_lat - (max_lat - min_lat) * row / height
            south = max_lat - (max_lat - min_lat) * (row + 1) / height
            rectangles.append(box(west, south, east, north))
    if not rectangles:
        raise MaterializationError(
            "EFFIS returned no burned-area pixels inside the configured bbox."
        )
    # EFFIS seasonal mapping is not a sub-metre source.  A ten-metre geographic
    # simplification removes WMS pixel stair-steps while keeping the useful
    # perimeter and prevents the immutable API payload from becoming needlessly
    # large.
    geometry = unary_union(rectangles).simplify(0.0001, preserve_topology=True)
    return _polygonal(geometry)


def _area_ha(geometry: BaseGeometry) -> float:
    return float(transform(_TO_L93, geometry).area / 10_000.0)


def _scaled_footprint(
    final_geometry: BaseGeometry,
    *,
    anchor_lon: float,
    anchor_lat: float,
    target_area_ha: float,
) -> BaseGeometry:
    """Find an anchor-centred inset whose area matches the documented daily total."""

    final_l93 = transform(_TO_L93, final_geometry)
    anchor_x, anchor_y = _TO_L93(anchor_lon, anchor_lat)
    final_area = final_l93.area / 10_000.0
    if target_area_ha > final_area * 1.10:
        raise MaterializationError(
            f"Daily target {target_area_ha:.1f} ha exceeds captured EFFIS scar {final_area:.1f} ha."
        )
    # The historical source can report a few percent more area than the
    # rasterised EFFIS outline.  In that one direction the outline remains the
    # defensible maximum; otherwise retain the narrow daily growth band.
    if target_area_ha >= final_area:
        return final_geometry
    low, high = 0.001, 1.0
    best = final_l93
    for _ in range(36):
        factor = (low + high) / 2.0
        candidate = scale(final_l93, xfact=factor, yfact=factor, origin=(anchor_x, anchor_y))
        candidate = candidate.intersection(final_l93)
        area = candidate.area / 10_000.0
        if area < target_area_ha:
            low = factor
        else:
            high = factor
            best = candidate
    # Keep narrow late-stage growth bands.  They are small relative to the
    # seasonal raster, but are still the only defensible active-zone evidence
    # for a day whose cumulative perimeter changed by a few hectares.
    result = transform(_FROM_L93, best.simplify(8.0, preserve_topology=True))
    return _polygonal(result)


def _geojson(geometry: BaseGeometry, **properties: Any) -> dict[str, Any]:
    result = dict(mapping(geometry))
    result.update(properties)
    return result


def build_manifest(spec: RetrospectiveSpec, final_geometry: BaseGeometry) -> dict[str, Any]:
    final_area = _area_ha(final_geometry)
    previous: BaseGeometry | None = None
    activity_zones: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for target in spec.daily_targets:
        cumulative = _scaled_footprint(
            final_geometry,
            anchor_lon=spec.anchor_lon,
            anchor_lat=spec.anchor_lat,
            target_area_ha=target.area_ha,
        )
        active = cumulative if previous is None else cumulative.difference(previous)
        active = _polygonal(active.intersection(cumulative))
        if active.is_empty:
            raise MaterializationError(
                f"Daily target {target.local_date} does not add an active zone."
            )
        source_ids = ["EFFIS-RDA-SEASON-2026", f"reported-area-{target.local_date}"]
        activity_zones.append(
            {
                "layer_revision_id": f"{spec.fire_id.lower()}-{target.local_date}-active-zone-v1",
                "local_date": target.local_date,
                "valid_at": target.valid_at,
                "geometry_origin": "AGENT_DERIVED",
                "burned_geometry_origin": "AGENT_DERIVED",
                "source_revision_ids": source_ids,
                "basis": (
                    "Contour final EFFIS rasterisé, contraint par le bilan de surface daté; "
                    "la progression spatiale intermédiaire est une reconstitution géométrique."
                ),
                "confidence": "medium",
                "activity_area_ha": round(_area_ha(active), 1),
                "geometry_geojson": _geojson(
                    active,
                    activity_method="daily_growth_from_effis_final_scar",
                    activity_confidence="medium",
                    activity_uncertainty_m=500.0,
                    source_product=EFFIS_LAYER,
                ),
                "burned_geometry_geojson": _geojson(
                    cumulative,
                    footprint_method="daily_area_constrained_effis_final_scar",
                    footprint_uncertainty_m=500.0,
                    final_effis_area_ha=round(final_area, 1),
                    reported_daily_area_ha=target.area_ha,
                    source_product=EFFIS_LAYER,
                ),
            }
        )
        source_entries = [
            {
                "name": spec.source_label,
                "url": spec.source_url,
                "attribution": "Copernicus EFFIS",
            }
        ] + [
            {
                "name": "Référence opérationnelle journalière",
                "url": url,
                "attribution": "Source publiée",
            }
            for url in target.source_urls
        ]
        reports.append(
            {
                "local_date": target.local_date,
                "title": target.title,
                "summary": target.summary,
                "sections": [
                    {
                        "heading": "Périmètres rétrospectifs",
                        "body": (
                            "La zone parcourue est contrainte par la surface publiée et "
                            "l'empreinte "
                            "EFFIS finale. La zone active représente la progression reconstituée "
                            "pendant cette journée; elle n'est pas une détection temps réel."
                        ),
                        "metrics": [
                            {
                                "label": "Zone parcourue reconstituée",
                                "value": f"≈ {target.area_ha:.0f} ha",
                                "quality": "derived",
                            },
                            {
                                "label": "Erreur spatiale indicative",
                                "value": "± 500 m",
                                "quality": "derived",
                            },
                        ],
                        "sources": source_entries,
                    }
                ],
            }
        )
        previous = cumulative
    return {
        "schema_version": "1.0",
        "dataset_id": spec.dataset_id,
        "campaign_id": spec.campaign_id,
        "identifier_suffix": spec.identifier_suffix,
        "created_by": "fireviewer-retrospective-builder",
        "incident": {"fire_id": spec.fire_id, "episode_id": spec.episode_id},
        "materialization": {
            "source": EFFIS_WMS_URL,
            "layer": EFFIS_LAYER,
            "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "final_geometry_area_ha": round(final_area, 1),
            "temporal_method": "daily_area_constrained_geometric_reconstruction",
        },
        "activity_zones": activity_zones,
        "reports": reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    spec = _load_spec(args.spec.resolve())
    with httpx.Client(timeout=60.0) as client:
        final_geometry = _vectorize_mask(_fetch_seasonal_mask(spec, client), spec.bbox)
    payload = build_manifest(spec, final_geometry)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "dataset_id": spec.dataset_id,
                "fire_id": spec.fire_id,
                "days": len(spec.daily_targets),
                "final_area_ha": payload["materialization"]["final_geometry_area_ha"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
