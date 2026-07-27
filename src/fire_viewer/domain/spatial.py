"""Deterministic local spatial-profile primitives for Fire-Viewer.

The first profile is intentionally limited to continental France.  It uses the
NGF-IGN69 orthometric source height and the locally packaged RAF20 grid to derive
the WGS84 ellipsoidal height exposed by ``ViewerManifest``.  Corsica is excluded:
it requires RAC23/NGF-IGN78 and must receive its own profile instead of silently
reusing RAF20.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

from pyproj import Transformer, datadir, network

LOCAL_FRAME = "ENU"
VERTICAL_DATUM = "EPSG:4979"
SOURCE_VERTICAL_DATUM = "NGF-IGN69"
SPATIAL_PROFILE_VERSION = "2.0"
PRODUCTION_HORIZONTAL_CRS = "EPSG:2154"
PRODUCTION_VERTICAL_CRS = "EPSG:5720"
PRODUCTION_GROUND_MODEL = "MNT_LIDAR_HD"
PRODUCTION_GROUND_RESOLUTION_M = 0.5
PRODUCTION_SURFACE_HEIGHT_REFERENCE = "MNS_RELATIVE_TO_MNT"
VERTICAL_TRANSFORM_ID = "RAF20"
RAF20_GRID_FILENAME = "fr_ign_RAF20.tif"
RAF20_GRID_SHA256 = "dc0cc2a38f0ea1029fe72cca3b5b7ed6dfe7e1db2a8d8482b7326ce3d6f25605"
UNITY_UNITS_PER_METER = 100.0
METERS_PER_UNITY_UNIT = 0.01
UNITY_PROFILE = "unity-eun-100-v1"
GLTF_TO_UNITY_PROFILE = "gltf-eun-negz-metric-v1"
RAF20_DERIVATION_TOLERANCE_M = 0.001

_WGS84_TO_LAMBERT93 = Transformer.from_crs("EPSG:4326", PRODUCTION_HORIZONTAL_CRS, always_xy=True)
_LAMBERT93_TO_WGS84 = Transformer.from_crs(PRODUCTION_HORIZONTAL_CRS, "EPSG:4326", always_xy=True)


class SpatialProfileError(ValueError):
    """The requested coordinate or local-grid setup is outside this profile."""


@dataclass(frozen=True, slots=True)
class Raf20DerivedOrigin:
    """Auditable source and derived vertical values for a local zone origin."""

    source_orthometric_height_m: float
    geoid_undulation_m: float
    ellipsoid_height_m: float


def _is_finite(value: float) -> bool:
    return math.isfinite(value)


def validate_wgs84_position(position: tuple[float, float, float]) -> tuple[float, float, float]:
    """Validate ``(longitude_degrees, latitude_degrees, ellipsoid_height_m)``."""

    longitude, latitude, _height = position
    if not all(_is_finite(value) for value in position):
        raise SpatialProfileError("WGS84 longitude, latitude, and height must be finite")
    if not -180.0 <= longitude <= 180.0:
        raise SpatialProfileError("WGS84 longitude must be between -180 and 180 degrees")
    if not -90.0 <= latitude <= 90.0:
        raise SpatialProfileError("WGS84 latitude must be between -90 and 90 degrees")
    return position


def is_france_continentale(longitude: float, latitude: float) -> bool:
    """Return whether a point is in the profile's deliberately conservative extent.

    This is a profile guard, not a national-border service.  The explicit Corsica
    exclusion prevents a RAF20/NGF-IGN69 result being claimed for an RAC23 area.
    """

    if not (-5.5 <= longitude <= 10.0 and 42.0 <= latitude <= 51.5):
        return False
    return not (8.3 <= longitude <= 9.8 and 41.0 <= latitude <= 43.3)


def validate_france_continentale_origin(
    position: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Validate an origin supported by the RAF20/NGF-IGN69 profile."""

    validated = validate_wgs84_position(position)
    if not is_france_continentale(validated[0], validated[1]):
        raise SpatialProfileError(
            "The RAF20/NGF-IGN69 profile supports continental France only; "
            "Corsica requires RAC23/NGF-IGN78."
        )
    return validated


def wgs84_to_lambert93(longitude: float, latitude: float) -> tuple[float, float]:
    """Return the metric production origin in Lambert-93 without network access."""

    validate_france_continentale_origin((longitude, latitude, 0.0))
    easting, northing = _WGS84_TO_LAMBERT93.transform(longitude, latitude)
    if not (_is_finite(easting) and _is_finite(northing)):
        raise SpatialProfileError("Lambert-93 production origin must be finite")
    return float(easting), float(northing)


def lambert93_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Return the continental-France WGS84 position for a Lambert-93 origin."""

    if not (_is_finite(easting) and _is_finite(northing)):
        raise SpatialProfileError("Lambert-93 production origin must be finite")
    longitude, latitude = _LAMBERT93_TO_WGS84.transform(easting, northing)
    result = validate_france_continentale_origin((float(longitude), float(latitude), 0.0))
    return result[0], result[1]


def raf20_grid_path() -> Path:
    """Return the expected, vendored grid path without downloading anything."""

    return Path(__file__).resolve().parents[1] / "data" / "proj" / RAF20_GRID_FILENAME


def verify_raf20_grid() -> Path:
    """Verify the local RAF20 resource before PROJ is allowed to use it."""

    grid_path = raf20_grid_path()
    if not grid_path.is_file():
        raise SpatialProfileError(
            f"RAF20 grid is unavailable at {grid_path}; network grid downloads are disabled"
        )
    checksum = hashlib.sha256(grid_path.read_bytes()).hexdigest()
    if checksum != RAF20_GRID_SHA256:
        raise SpatialProfileError("RAF20 grid SHA-256 does not match the pinned spatial profile")
    return grid_path


def _configure_local_proj_data(grid_path: Path) -> None:
    """Make the vendored grid visible to PROJ and prohibit implicit network access."""

    network.set_network_enabled(False)  # type: ignore[attr-defined]
    data_directory = str(grid_path.parent)
    if data_directory not in datadir.get_data_dir().split(os.pathsep):
        datadir.append_data_dir(data_directory)


def _raf20_transformer(*, inverse: bool = False) -> Transformer:
    grid_path = verify_raf20_grid()
    _configure_local_proj_data(grid_path)
    direction = "+inv " if inverse else ""
    return Transformer.from_pipeline(
        "+proj=pipeline "
        f"+step {direction}+proj=vgridshift +grids={grid_path.as_posix()} +multiplier=1"
    )


def raf20_orthometric_to_ellipsoidal(
    longitude: float,
    latitude: float,
    orthometric_height_m: float,
) -> tuple[float, float, float]:
    """Convert a continental-France NGF-IGN69 height with the verified local grid."""

    validate_france_continentale_origin((longitude, latitude, orthometric_height_m))
    transformed = _raf20_transformer().transform(longitude, latitude, orthometric_height_m)
    result = (float(transformed[0]), float(transformed[1]), float(transformed[2]))
    return validate_wgs84_position(result)


def ellipsoidal_to_raf20_orthometric(
    longitude: float,
    latitude: float,
    ellipsoid_height_m: float,
) -> tuple[float, float, float]:
    """Inverse of :func:`raf20_orthometric_to_ellipsoidal` for audit round-trips."""

    validate_france_continentale_origin((longitude, latitude, ellipsoid_height_m))
    transformed = _raf20_transformer(inverse=True).transform(
        longitude, latitude, ellipsoid_height_m
    )
    result = (float(transformed[0]), float(transformed[1]), float(transformed[2]))
    return validate_wgs84_position(result)


def derive_raf20_origin(
    longitude: float,
    latitude: float,
    source_orthometric_height_m: float,
) -> Raf20DerivedOrigin:
    """Derive the immutable EPSG:4979 origin and RAF20 undulation from NGF-IGN69 H."""

    _, _, ellipsoid_height_m = raf20_orthometric_to_ellipsoidal(
        longitude, latitude, source_orthometric_height_m
    )
    return Raf20DerivedOrigin(
        source_orthometric_height_m=source_orthometric_height_m,
        geoid_undulation_m=ellipsoid_height_m - source_orthometric_height_m,
        ellipsoid_height_m=ellipsoid_height_m,
    )


def validate_raf20_derivation(
    longitude: float,
    latitude: float,
    source_orthometric_height_m: float,
    geoid_undulation_m: float,
    ellipsoid_height_m: float,
) -> Raf20DerivedOrigin:
    """Reject a zone reference whose stored H/N/h values disagree with RAF20.

    The database proves the arithmetic ``h = H + N``.  This function additionally
    proves that ``N`` comes from the pinned, local RAF20 grid before a revision can
    be seeded or projected publicly.
    """

    validate_france_continentale_origin((longitude, latitude, source_orthometric_height_m))
    if not all(
        _is_finite(value)
        for value in (source_orthometric_height_m, geoid_undulation_m, ellipsoid_height_m)
    ):
        raise SpatialProfileError(
            "RAF20 source, undulation, and ellipsoidal heights must be finite"
        )
    derived = derive_raf20_origin(longitude, latitude, source_orthometric_height_m)
    if (
        abs(ellipsoid_height_m - derived.ellipsoid_height_m) > RAF20_DERIVATION_TOLERANCE_M
        or abs(geoid_undulation_m - derived.geoid_undulation_m) > RAF20_DERIVATION_TOLERANCE_M
        or abs(ellipsoid_height_m - source_orthometric_height_m - geoid_undulation_m)
        > RAF20_DERIVATION_TOLERANCE_M
    ):
        raise SpatialProfileError(
            "stored RAF20 H/N/h values do not match the pinned local grid within one millimetre"
        )
    return derived


def wgs84_to_enu(
    position: tuple[float, float, float], origin: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Project WGS84 ellipsoidal coordinates into the local physical ENU frame."""

    longitude, latitude, height = validate_wgs84_position(position)
    origin_lon, origin_lat, origin_height = validate_wgs84_position(origin)
    transformer = Transformer.from_crs(VERTICAL_DATUM, "EPSG:4978", always_xy=True)
    x, y, z = transformer.transform(longitude, latitude, height)
    origin_x, origin_y, origin_z = transformer.transform(origin_lon, origin_lat, origin_height)
    delta_x, delta_y, delta_z = x - origin_x, y - origin_y, z - origin_z
    lon_rad, lat_rad = math.radians(origin_lon), math.radians(origin_lat)
    east = -math.sin(lon_rad) * delta_x + math.cos(lon_rad) * delta_y
    north = (
        -math.sin(lat_rad) * math.cos(lon_rad) * delta_x
        - math.sin(lat_rad) * math.sin(lon_rad) * delta_y
        + math.cos(lat_rad) * delta_z
    )
    up = (
        math.cos(lat_rad) * math.cos(lon_rad) * delta_x
        + math.cos(lat_rad) * math.sin(lon_rad) * delta_y
        + math.sin(lat_rad) * delta_z
    )
    return (east, north, up)


def enu_to_wgs84(
    enu: tuple[float, float, float], origin: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Inverse-project physical local ENU metres to WGS84 ellipsoidal coordinates."""

    east, north, up = enu
    if not all(_is_finite(value) for value in enu):
        raise SpatialProfileError("ENU coordinates must be finite")
    origin_lon, origin_lat, origin_height = validate_wgs84_position(origin)
    transformer = Transformer.from_crs(VERTICAL_DATUM, "EPSG:4978", always_xy=True)
    origin_x, origin_y, origin_z = transformer.transform(origin_lon, origin_lat, origin_height)
    lon_rad, lat_rad = math.radians(origin_lon), math.radians(origin_lat)
    delta_x = (
        -math.sin(lon_rad) * east
        - math.sin(lat_rad) * math.cos(lon_rad) * north
        + math.cos(lat_rad) * math.cos(lon_rad) * up
    )
    delta_y = (
        math.cos(lon_rad) * east
        - math.sin(lat_rad) * math.sin(lon_rad) * north
        + math.cos(lat_rad) * math.sin(lon_rad) * up
    )
    delta_z = math.cos(lat_rad) * north + math.sin(lat_rad) * up
    reverse_transformer = Transformer.from_crs("EPSG:4978", VERTICAL_DATUM, always_xy=True)
    longitude, latitude, height = reverse_transformer.transform(
        origin_x + delta_x, origin_y + delta_y, origin_z + delta_z
    )
    return validate_wgs84_position((float(longitude), float(latitude), float(height)))


def enu_to_unity(enu: tuple[float, float, float]) -> tuple[float, float, float]:
    """Map physical ``(east, north, up)`` metres to Unity ``(x, y, z)`` units."""

    east, north, up = enu
    if not all(_is_finite(value) for value in enu):
        raise SpatialProfileError("ENU coordinates must be finite")
    return (
        east * UNITY_UNITS_PER_METER,
        up * UNITY_UNITS_PER_METER,
        north * UNITY_UNITS_PER_METER,
    )


def unity_to_enu(unity: tuple[float, float, float]) -> tuple[float, float, float]:
    """Map Unity ``(x, y, z)`` units to physical ``(east, north, up)`` metres."""

    x, y, z = unity
    if not all(_is_finite(value) for value in unity):
        raise SpatialProfileError("Unity coordinates must be finite")
    return (
        x * METERS_PER_UNITY_UNIT,
        z * METERS_PER_UNITY_UNIT,
        y * METERS_PER_UNITY_UNIT,
    )


def enu_to_gltf(enu: tuple[float, float, float]) -> tuple[float, float, float]:
    """Map physical ``(east, north, up)`` metres to metric glTF ``(E, U, -N)``."""

    east, north, up = enu
    if not all(_is_finite(value) for value in enu):
        raise SpatialProfileError("ENU coordinates must be finite")
    return (east, up, -north)


def gltf_to_enu(gltf: tuple[float, float, float]) -> tuple[float, float, float]:
    """Map metric glTF ``(E, U, -N)`` coordinates back to physical ENU metres."""

    east, up, negative_north = gltf
    if not all(_is_finite(value) for value in gltf):
        raise SpatialProfileError("glTF coordinates must be finite")
    return (east, -negative_north, up)


def gltf_to_unity(gltf: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply the canonical metric glTF ``(east, up, -north)`` import bridge."""

    east, up, negative_north = gltf
    if not all(_is_finite(value) for value in gltf):
        raise SpatialProfileError("glTF coordinates must be finite")
    return (
        east * UNITY_UNITS_PER_METER,
        up * UNITY_UNITS_PER_METER,
        -negative_north * UNITY_UNITS_PER_METER,
    )
