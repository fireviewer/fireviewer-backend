import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEGREE_LAT = 111_320.0


@dataclass(frozen=True, slots=True)
class BoundingBox:
    min_lon: float
    max_lon: float
    min_lat: float
    max_lat: float


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def combine_uncertainties_m(first_m: float, second_m: float) -> float:
    return math.hypot(first_m, second_m)


def bbox_for_point(lon: float, lat: float, radius_m: float) -> BoundingBox:
    lat_delta = radius_m / METERS_PER_DEGREE_LAT
    cos_lat = max(abs(math.cos(math.radians(lat))), 0.01)
    lon_delta = radius_m / (METERS_PER_DEGREE_LAT * cos_lat)
    return BoundingBox(
        min_lon=max(-180.0, lon - lon_delta),
        max_lon=min(180.0, lon + lon_delta),
        min_lat=max(-90.0, lat - lat_delta),
        max_lat=min(90.0, lat + lat_delta),
    )
