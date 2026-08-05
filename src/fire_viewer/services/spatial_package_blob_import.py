"""Finalize a locally prepared package after direct browser upload to private Blob storage."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.config import Settings
from fire_viewer.core.security import Actor
from fire_viewer.db.models import (
    SpatialPackage,
    SpatialPackageFile,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import SpatialPackageFileKind, SpatialPackageState
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminBlobObjectReference,
    AdminSpatialPackageFromBlobRequest,
    AdminSpatialPackageImportEnvelope,
    AdminSpatialPackageImportResponse,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.idempotency import find_replay, store_response
from fire_viewer.storage import ObjectMetadata, ObjectStore, build_object_store
from fire_viewer.storage.object_store import ObjectStorageError

_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,95}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_REQUIRED_PATHS = frozenset({"package-manifest.json", "catalog.json"})
_OMNIVERSE_COMMON_PATHS = frozenset({"manifest.json", "dependency-inventory.json"})
_OMNIVERSE_MAP_CONTRACT = "contracts/map-contract.json"
_OMNIVERSE_PERIMETER_CONTRACT = "contracts/perimeter-contract.json"
_ASSET_PREFIXES = ("assets/", "terrain/", "vectors/")
_CONTENT_TYPES = {
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".glb": "model/gltf-binary",
    ".fwtile": "application/vnd.fireviewer.tile",
    ".fwterrain": "application/vnd.fireviewer.terrain",
    ".usd": "model/vnd.usd",
    ".usda": "model/vnd.usd",
    ".usdc": "model/vnd.usd",
    ".usdz": "model/vnd.usdz+zip",
    ".hdr": "image/vnd.radiance",
    ".npz": "application/octet-stream",
    ".jgw": "text/plain",
}


@dataclass(frozen=True, slots=True)
class ValidatedSpatialProfile:
    origin_easting_l93: float
    origin_northing_l93: float
    source_orthometric_height_m: float
    min_easting_l93: float
    min_northing_l93: float
    max_easting_l93: float
    max_northing_l93: float
    min_east_m: float
    max_east_m: float
    min_north_m: float
    max_north_m: float
    min_up_m: float
    max_up_m: float


@dataclass(frozen=True, slots=True)
class ValidatedBlobPackage:
    upload_id: str
    package_id: str
    storage_key: str
    manifest_sha256: str
    manifest_size_bytes: int
    catalog_sha256: str
    catalog_size_bytes: int
    asset_catalog: list[dict[str, Any]]
    object_count: int
    total_size_bytes: int
    spatial_profile: ValidatedSpatialProfile | None
    package_role: str = "legacy_map"
    manifest_path: str = "package-manifest.json"
    contract_path: str | None = None
    contract_sha256: str | None = None
    acceptance_sha256: str | None = None
    package_revision: int | None = None
    state_count: int | None = None
    base_map: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BlobSpatialImportOutcome:
    response: AdminSpatialPackageImportEnvelope
    replayed: bool


def _safe_path(value: str, *, asset: bool = False) -> str:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or value.startswith(("/", "\\"))
        or any(character in value for character in ("?", "#", ":"))
    ):
        raise BadRequestError("unsafe_package_path", "The package contains an unsafe path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BadRequestError("unsafe_package_path", "The package contains an unsafe path.")
    normalized = path.as_posix()
    if asset and not normalized.startswith(_ASSET_PREFIXES):
        raise BadRequestError(
            "unsupported_package_path",
            "Catalog assets must use assets/, terrain/ or vectors/.",
        )
    return normalized


def _positive_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BadRequestError("invalid_package_catalog", f"{label} must be a positive integer.")
    return value


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise BadRequestError("invalid_package_catalog", f"{label} must be a SHA-256 digest.")
    return value


def _json_document(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequestError("invalid_package_json", f"{label} must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise BadRequestError("invalid_package_json", f"{label} must be a JSON object.")
    return value


def _finite_number(value: object, *, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BadRequestError("invalid_package_catalog", f"{label} must be numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise BadRequestError("invalid_package_catalog", f"{label} must be finite.")
    return result


def _finite_tuple(value: object, *, size: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise BadRequestError("invalid_package_catalog", f"{label} must contain {size} numbers.")
    return tuple(
        _finite_number(component, label=f"{label}[{index}]")
        for index, component in enumerate(value)
    )


def _far_terrain_frame(
    terrain_raw: bytes,
    *,
    expected_origin: tuple[float, ...],
    expected_bounds: tuple[float, ...],
) -> tuple[float, float]:
    if len(terrain_raw) < 16 or terrain_raw[:8] != b"FWTILE1\0":
        raise BadRequestError(
            "invalid_package_spatial_profile", "The FAR terrain container is invalid."
        )
    version = int.from_bytes(terrain_raw[8:10], "little")
    header_size = int.from_bytes(terrain_raw[12:16], "little")
    if version != 1 or header_size <= 0 or 16 + header_size > len(terrain_raw):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The FAR terrain header is invalid."
        )
    header = _json_document(terrain_raw[16 : 16 + header_size], label="FAR terrain header")
    origin = _finite_tuple(header.get("origin_l93_m"), size=3, label="FAR terrain origin_l93_m")
    bounds = _finite_tuple(header.get("bounds_l93_m"), size=4, label="FAR terrain bounds_l93_m")
    if (
        header.get("schema") != "fireviewer.fwtile.v1"
        or header.get("kind") != "global_far_terrain"
        or header.get("crs") != "EPSG:2154"
        or origin != expected_origin
        or bounds != expected_bounds
    ):
        raise BadRequestError(
            "invalid_package_spatial_profile",
            "The FAR terrain frame does not match catalog.json.",
        )
    sections = header.get("sections")
    section = (
        next(
            (item for item in sections if isinstance(item, dict) and item.get("name") == "terrain"),
            None,
        )
        if isinstance(sections, list)
        else None
    )
    metadata = section.get("metadata") if isinstance(section, dict) else None
    quantization = metadata.get("elevation_quantization") if isinstance(metadata, dict) else None
    if not isinstance(quantization, dict):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The FAR elevation range is missing."
        )
    minimum = _finite_number(quantization.get("minimum_m"), label="FAR minimum elevation")
    maximum = _finite_number(quantization.get("maximum_m"), label="FAR maximum elevation")
    if minimum >= maximum or not minimum <= 0 <= maximum:
        raise BadRequestError(
            "invalid_package_spatial_profile",
            "The FAR elevation range must contain the package origin.",
        )
    return minimum, maximum


def _remote_tile_spatial_profile(
    catalog: dict[str, Any],
    *,
    by_path: dict[str, AdminBlobObjectReference],
    object_store: ObjectStore,
    storage_key: str,
) -> ValidatedSpatialProfile | None:
    """Derive the immutable zone frame from a real FireViewer remote-tile package."""

    if catalog.get("schema") != "fireviewer.remote-tile-catalog.v1":
        return None
    # Legacy packages can still target a pre-created revision. Automatic incident setup
    # is enabled only when the complete production spatial contract is present.
    if "origin_l93_m" not in catalog and "lod_policy" not in catalog:
        return None
    if catalog.get("crs") != "EPSG:2154" or catalog.get("linear_unit") != "metre":
        raise BadRequestError(
            "invalid_package_spatial_profile",
            "The remote-tile package must use Lambert-93 metres.",
        )
    origin = _finite_tuple(catalog.get("origin_l93_m"), size=3, label="origin_l93_m")
    lod_policy = catalog.get("lod_policy")
    far = lod_policy.get("far") if isinstance(lod_policy, dict) else None
    if not isinstance(far, dict):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The remote-tile FAR profile is missing."
        )
    bounds = _finite_tuple(far.get("bounds_l93_m"), size=4, label="far.bounds_l93_m")
    if not (bounds[0] < bounds[2] and bounds[1] < bounds[3]):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The remote-tile FAR bounds are invalid."
        )
    if not (bounds[0] <= origin[0] <= bounds[2] and bounds[1] <= origin[1] <= bounds[3]):
        raise BadRequestError(
            "invalid_package_spatial_profile",
            "The remote-tile origin is outside the FAR bounds.",
        )
    terrain = far.get("terrain")
    path_value = terrain.get("path") if isinstance(terrain, dict) else None
    terrain_path = _safe_path(path_value, asset=True) if isinstance(path_value, str) else ""
    if not terrain_path or terrain_path not in by_path or not isinstance(terrain, dict):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The remote-tile FAR terrain is not uploaded."
        )
    terrain_sha256 = _sha256(terrain.get("sha256"), label="FAR terrain")
    terrain_size = _positive_int(terrain.get("byte_count"), label="FAR terrain size")
    if by_path[terrain_path].size_bytes != terrain_size:
        raise BadRequestError("blob_metadata_mismatch", "The remote-tile FAR terrain size changed.")
    terrain_raw = object_store.read_bytes(object_store.uri_for(f"{storage_key}/{terrain_path}"))
    if (
        len(terrain_raw) != terrain_size
        or hashlib.sha256(terrain_raw).hexdigest() != terrain_sha256
    ):
        raise BadRequestError(
            "blob_metadata_mismatch", "The remote-tile FAR terrain digest changed."
        )
    minimum_up, maximum_up = _far_terrain_frame(
        terrain_raw, expected_origin=origin, expected_bounds=bounds
    )
    return ValidatedSpatialProfile(
        origin_easting_l93=origin[0],
        origin_northing_l93=origin[1],
        source_orthometric_height_m=origin[2],
        min_easting_l93=bounds[0],
        min_northing_l93=bounds[1],
        max_easting_l93=bounds[2],
        max_northing_l93=bounds[3],
        min_east_m=bounds[0] - origin[0],
        max_east_m=bounds[2] - origin[0],
        min_north_m=bounds[1] - origin[1],
        max_north_m=bounds[3] - origin[1],
        min_up_m=minimum_up,
        max_up_m=maximum_up,
    )


def _catalog_entries(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        path = node.get("path")
        if isinstance(path, str) and path.startswith(_ASSET_PREFIXES):
            entries.append(
                {
                    "path": _safe_path(path, asset=True),
                    "sha256": _sha256(node.get("sha256"), label="catalog asset"),
                    "size_bytes": _positive_int(
                        node.get("byte_count", node.get("size_bytes")),
                        label="catalog asset size",
                    ),
                }
            )
        for child in node.values():
            visit(child)

    visit(catalog)
    if not entries:
        raise BadRequestError(
            "empty_package_catalog",
            "catalog.json must declare at least one supported spatial asset.",
        )
    return entries


def _content_type(path: str) -> str:
    result = _CONTENT_TYPES.get(PurePosixPath(path).suffix.casefold())
    if result is None:
        raise BadRequestError(
            "unsupported_package_file_type",
            "The package contains a file type that is not supported.",
        )
    return result


def _list_objects(
    store: ObjectStore,
    storage_key: str,
    *,
    limit: int,
) -> dict[str, ObjectMetadata]:
    try:
        prefix = f"{store.pathname_for(storage_key).rstrip('/')}/"
        result: dict[str, ObjectMetadata] = {}
        for item in store.list_prefix(storage_key, limit=limit):
            if not item.pathname.startswith(prefix):
                raise ObjectStorageError("Object inventory escaped the package prefix.")
            path = item.pathname.removeprefix(prefix)
            if not path or path in result:
                raise ObjectStorageError("Object inventory contains an invalid pathname.")
            result[path] = item
        return result
    except ObjectStorageError as exc:
        raise BadRequestError(
            "missing_blob_object",
            "The declared Blob object inventory is missing or inaccessible.",
        ) from exc


def _read_small_json(
    store: ObjectStore,
    *,
    storage_key: str,
    path: str,
    declared: AdminBlobObjectReference,
    settings: Settings,
) -> tuple[bytes, dict[str, Any]]:
    if declared.size_bytes > settings.zone_upload_max_manifest_bytes:
        raise BadRequestError(
            "package_metadata_too_large", f"{path} exceeds the configured metadata limit."
        )
    raw = store.read_bytes(store.uri_for(f"{storage_key}/{path}"))
    if len(raw) != declared.size_bytes:
        raise BadRequestError("blob_metadata_mismatch", f"{path} size changed.")
    return raw, _json_document(raw, label=path)


def _omniverse_inventory(document: dict[str, Any]) -> list[dict[str, Any]]:
    files = document.get("files")
    declared_count = document.get("file_count")
    if not isinstance(files, list) or not files or declared_count != len(files):
        raise BadRequestError(
            "invalid_dependency_inventory",
            "dependency-inventory.json must declare its complete file list.",
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BadRequestError("invalid_dependency_inventory", "An inventory entry is invalid.")
        path_value = item.get("path")
        path = _safe_path(path_value) if isinstance(path_value, str) else ""
        if not path or path in seen:
            raise BadRequestError(
                "invalid_dependency_inventory", "The inventory contains a duplicate path."
            )
        seen.add(path)
        _content_type(path)
        result.append(
            {
                "path": path,
                "sha256": _sha256(item.get("sha256"), label=f"inventory entry {path}"),
                "size_bytes": _positive_int(
                    item.get("byte_count"), label=f"inventory entry size {path}"
                ),
            }
        )
    return result


def _omniverse_spatial_profile(
    *,
    contract: dict[str, Any],
    source_catalog: dict[str, Any],
) -> ValidatedSpatialProfile:
    spatial = contract.get("spatial_reference")
    source_spatial = source_catalog.get("spatial_contract")
    if not isinstance(spatial, dict) or not isinstance(source_spatial, dict):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The OpenUSD spatial reference is missing."
        )
    if (
        spatial.get("horizontal_crs") != "EPSG:2154"
        or spatial.get("vertical_datum") != "NGF-IGN69"
        or spatial.get("up_axis") != "Z"
        or _finite_number(spatial.get("meters_per_unit"), label="meters_per_unit") != 1.0
        or source_spatial.get("grid_crs") != "EPSG:2154"
        or source_spatial.get("vertical_datum") != "NGF-IGN69"
    ):
        raise BadRequestError(
            "invalid_package_spatial_profile",
            "The OpenUSD map must use Lambert-93, NGF-IGN69 and metre units.",
        )
    bounds = _finite_tuple(spatial.get("bounds_l93_m"), size=4, label="bounds_l93_m")
    origin = _finite_tuple(
        spatial.get("local_origin_l93_m"), size=3, label="local_origin_l93_m"
    )
    if not (bounds[0] < bounds[2] and bounds[1] < bounds[3]):
        raise BadRequestError("invalid_package_spatial_profile", "The map bounds are invalid.")
    if not (bounds[0] <= origin[0] <= bounds[2] and bounds[1] <= origin[1] <= bounds[3]):
        raise BadRequestError(
            "invalid_package_spatial_profile", "The map origin is outside its bounds."
        )
    source_height = _finite_number(
        source_spatial.get("height_origin_ngf_ign69_m"), label="height origin"
    )
    maximum_height = _finite_number(
        source_spatial.get("height_maximum_ngf_ign69_m"), label="height maximum"
    )
    if maximum_height <= source_height:
        raise BadRequestError(
            "invalid_package_spatial_profile", "The map elevation range is invalid."
        )
    return ValidatedSpatialProfile(
        origin_easting_l93=origin[0],
        origin_northing_l93=origin[1],
        source_orthometric_height_m=source_height,
        min_easting_l93=bounds[0],
        min_northing_l93=bounds[1],
        max_easting_l93=bounds[2],
        max_northing_l93=bounds[3],
        min_east_m=bounds[0] - origin[0],
        max_east_m=bounds[2] - origin[0],
        min_north_m=bounds[1] - origin[1],
        max_north_m=bounds[3] - origin[1],
        min_up_m=0.0,
        max_up_m=maximum_height - source_height,
    )


def _validate_omniverse_blob_package(
    *,
    zone_id: str,
    revision: int,
    payload: AdminSpatialPackageFromBlobRequest,
    settings: Settings,
    store: ObjectStore | None,
    expected_role: str | None,
) -> ValidatedBlobPackage:
    object_store = store or build_object_store(settings)
    storage_key = f"packages/{payload.upload_id}"
    by_path: dict[str, AdminBlobObjectReference] = {}
    total_size_bytes = 0
    for item in payload.objects:
        path = _safe_path(item.path)
        if path in by_path:
            raise BadRequestError("duplicate_blob_path", "A Blob path is declared twice.")
        if item.pathname != object_store.pathname_for(f"{storage_key}/{path}"):
            raise BadRequestError("blob_path_mismatch", "A Blob object escaped its upload grant.")
        if item.content_type != _content_type(path):
            raise BadRequestError(
                "unexpected_blob_content_type", "A Blob object has an unexpected content type."
            )
        if item.size_bytes > settings.zone_upload_max_bytes:
            raise BadRequestError("package_file_too_large", "A package file exceeds the limit.")
        total_size_bytes += item.size_bytes
        if total_size_bytes > settings.zone_upload_max_unpacked_bytes:
            raise BadRequestError("package_too_large", "The package exceeds the configured limit.")
        by_path[path] = item

    contract_paths = {
        path
        for path in (_OMNIVERSE_MAP_CONTRACT, _OMNIVERSE_PERIMETER_CONTRACT)
        if path in by_path
    }
    if not _OMNIVERSE_COMMON_PATHS.issubset(by_path) or len(contract_paths) != 1:
        raise BadRequestError(
            "missing_package_metadata",
            "An OpenUSD package requires manifest.json, dependency-inventory.json "
            "and one role contract.",
        )
    contract_path = next(iter(contract_paths))
    role = "omniverse_map" if contract_path == _OMNIVERSE_MAP_CONTRACT else "omniverse_perimeter"
    if expected_role == "map" and role != "omniverse_map":
        raise BadRequestError("unexpected_package_role", "The map upload requires a map package.")
    if expected_role == "perimeter" and role != "omniverse_perimeter":
        raise BadRequestError(
            "unexpected_package_role", "The perimeter upload requires a perimeter package."
        )

    metadata = _list_objects(
        object_store, storage_key, limit=settings.zone_upload_max_files + 1
    )
    if len(metadata) > settings.zone_upload_max_files:
        raise BadRequestError("too_many_package_files", "The package contains too many files.")
    if set(metadata) != set(by_path):
        raise BadRequestError(
            "missing_blob_object",
            "The declared Blob objects do not match the stored package inventory.",
        )
    metadata_paths = {"manifest.json", "dependency-inventory.json", contract_path}
    for path in metadata_paths:
        try:
            metadata[path] = object_store.head(
                object_store.uri_for(f"{storage_key}/{path}")
            )
        except ObjectStorageError as exc:
            raise BadRequestError(
                "missing_blob_object", "The package metadata is inaccessible."
            ) from exc
    for path, item in by_path.items():
        actual = metadata[path]
        if (
            actual.pathname != item.pathname
            or actual.size_bytes != item.size_bytes
            or (actual.content_type is not None and actual.content_type != item.content_type)
        ):
            raise BadRequestError(
                "blob_metadata_mismatch", "A Blob object does not match its declaration."
            )

    manifest_raw, manifest = _read_small_json(
        object_store,
        storage_key=storage_key,
        path="manifest.json",
        declared=by_path["manifest.json"],
        settings=settings,
    )
    inventory_raw, inventory = _read_small_json(
        object_store,
        storage_key=storage_key,
        path="dependency-inventory.json",
        declared=by_path["dependency-inventory.json"],
        settings=settings,
    )
    contract_raw, contract = _read_small_json(
        object_store,
        storage_key=storage_key,
        path=contract_path,
        declared=by_path[contract_path],
        settings=settings,
    )
    inventory_sha256 = hashlib.sha256(inventory_raw).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    inventory_reference = manifest.get("dependency_inventory")
    if not isinstance(inventory_reference, dict):
        raise BadRequestError("invalid_package_manifest", "The dependency inventory is missing.")
    if (
        inventory_reference.get("path") != "dependency-inventory.json"
        or _sha256(inventory_reference.get("sha256"), label="dependency inventory")
        != inventory_sha256
    ):
        raise BadRequestError(
            "dependency_inventory_digest_mismatch",
            "dependency-inventory.json does not match manifest.json.",
        )
    assets = _omniverse_inventory(inventory)
    expected_paths = metadata_paths.union(item["path"] for item in assets)
    if set(by_path) != expected_paths:
        raise BadRequestError(
            "package_inventory_mismatch",
            "The OpenUSD package and dependency inventory do not match exactly.",
        )
    if inventory_reference.get("file_count") != len(assets):
        raise BadRequestError(
            "package_inventory_mismatch", "The manifest file count is inconsistent."
        )
    for inventory_item in assets:
        if by_path[inventory_item["path"]].size_bytes != inventory_item["size_bytes"]:
            raise BadRequestError(
                "package_asset_size_mismatch", "An OpenUSD dependency has an unexpected size."
            )

    if contract.get("contract_status") != "active" or manifest.get("status") != "active":
        raise BadRequestError(
            "inactive_package_contract", "Only an active contract can be uploaded."
        )
    contract_sha256 = hashlib.sha256(contract_raw).hexdigest()
    package_revision: int
    acceptance_sha256: str
    state_count: int | None = None
    base_map: dict[str, Any] | None = None
    spatial_profile: ValidatedSpatialProfile | None = None

    if role == "omniverse_map":
        package = contract.get("package")
        release = contract.get("release")
        simulation = contract.get("simulation")
        if (
            contract.get("schema") != "fireviewer.omniverse-map-upload-contract.v1"
            or manifest.get("schema") != "fireviewer.omniverse-pure-map-package.v1"
            or not isinstance(package, dict)
            or not isinstance(release, dict)
            or release.get("upload_allowed") is not True
            or release.get("automatic_publication") is not False
            or not isinstance(simulation, dict)
            or any(bool(value) for value in simulation.values())
        ):
            raise BadRequestError("invalid_map_contract", "The Omniverse map contract is invalid.")
        acceptance_sha256 = _sha256(
            release.get("acceptance_receipt_sha256"), label="map acceptance"
        )
        manifest_acceptance = manifest.get("acceptance")
        if (
            not isinstance(manifest_acceptance, dict)
            or manifest_acceptance.get("decision") != "accepted"
            or manifest_acceptance.get("sha256") != acceptance_sha256
        ):
            raise BadRequestError("map_acceptance_mismatch", "The map acceptance receipt differs.")
        package_id = package.get("package_id")
        package_revision = _positive_int(package.get("revision"), label="map revision")
        entry_path = package.get("entry_stage")
        entry_sha256 = _sha256(package.get("entry_stage_sha256"), label="map entry stage")
        if (
            package.get("manifest_sha256") != manifest_sha256
            or manifest.get("package_id") != package_id
            or manifest.get("revision") != package_revision
            or manifest.get("entry_stage") != entry_path
            or manifest.get("entry_stage_sha256") != entry_sha256
        ):
            raise BadRequestError("map_contract_mismatch", "The map contract and manifest differ.")
        source_manifest_path = "source-usd/source/package-manifest.json"
        source_manifest_entry = next(
            (item for item in assets if item["path"] == source_manifest_path), None
        )
        if source_manifest_entry is None:
            raise BadRequestError(
                "map_source_manifest_missing", "The map source manifest is not included."
            )
        source_manifest_raw, source_manifest = _read_small_json(
            object_store,
            storage_key=storage_key,
            path=source_manifest_path,
            declared=by_path[source_manifest_path],
            settings=settings,
        )
        if hashlib.sha256(source_manifest_raw).hexdigest() != source_manifest_entry["sha256"]:
            raise BadRequestError("map_source_manifest_changed", "The source manifest changed.")
        zones = source_manifest.get("zones")
        source_zone = zones[0] if isinstance(zones, list) and len(zones) == 1 else None
        if not isinstance(source_zone, dict) or source_zone.get("zone_id") != zone_id:
            raise BadRequestError(
                "package_zone_mismatch", "The map source does not target the requested zone."
            )
        source_catalog_reference = source_manifest.get("catalog")
        if not isinstance(source_catalog_reference, dict):
            raise BadRequestError("map_source_catalog_missing", "The source catalog is missing.")
        source_catalog_path = "source-usd/source/catalog.json"
        source_catalog_entry = next(
            (item for item in assets if item["path"] == source_catalog_path), None
        )
        if source_catalog_entry is None:
            raise BadRequestError(
                "map_source_catalog_missing", "The source catalog is not included."
            )
        source_catalog_raw, source_catalog = _read_small_json(
            object_store,
            storage_key=storage_key,
            path=source_catalog_path,
            declared=by_path[source_catalog_path],
            settings=settings,
        )
        source_catalog_sha256 = hashlib.sha256(source_catalog_raw).hexdigest()
        if (
            source_catalog_sha256 != source_catalog_entry["sha256"]
            or source_catalog_reference.get("path") != "catalog.json"
            or source_catalog_reference.get("sha256") != source_catalog_sha256
        ):
            raise BadRequestError("map_source_catalog_changed", "The source catalog changed.")
        spatial_profile = _omniverse_spatial_profile(
            contract=contract, source_catalog=source_catalog
        )
    else:
        package = contract.get("layer_package")
        release = contract.get("release")
        progression = contract.get("progression")
        base_contract = contract.get("base_map")
        manifest_base = manifest.get("base_map")
        if (
            contract.get("schema")
            != "fireviewer.omniverse-progressive-perimeter-layer-contract.v1"
            or manifest.get("schema")
            != "fireviewer.omniverse-progressive-perimeter-package.v1"
            or not isinstance(package, dict)
            or not isinstance(release, dict)
            or release.get("layer_attachment_allowed") is not True
            or release.get("automatic_publication") is not False
            or not isinstance(progression, dict)
            or progression.get("layer_crs") != "EPSG:2154"
            or not isinstance(base_contract, dict)
            or not isinstance(manifest_base, dict)
        ):
            raise BadRequestError(
                "invalid_perimeter_contract", "The Omniverse perimeter contract is invalid."
            )
        acceptance_sha256 = _sha256(
            release.get("acceptance_receipt_sha256"), label="perimeter acceptance"
        )
        manifest_acceptance = manifest.get("acceptance")
        if (
            not isinstance(manifest_acceptance, dict)
            or manifest_acceptance.get("decision") != "accepted"
            or manifest_acceptance.get("sha256") != acceptance_sha256
        ):
            raise BadRequestError(
                "perimeter_acceptance_mismatch", "The perimeter acceptance receipt differs."
            )
        state_count = _positive_int(progression.get("state_count"), label="perimeter states")
        records = progression.get("state_records")
        if not isinstance(records, list) or len(records) != state_count:
            raise BadRequestError(
                "perimeter_state_count_mismatch", "The perimeter state list is incomplete."
            )
        manifest_states = manifest.get("states")
        if not isinstance(manifest_states, list) or len(manifest_states) != state_count:
            raise BadRequestError(
                "perimeter_state_count_mismatch", "The perimeter manifest is incomplete."
            )
        previous_valid_at = ""
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                raise BadRequestError(
                    "invalid_perimeter_state", "A perimeter state record is invalid."
                )
            state_path_value = record.get("layer_path")
            state_path = (
                _safe_path(state_path_value) if isinstance(state_path_value, str) else ""
            )
            state_sha256 = _sha256(
                record.get("layer_sha256"), label=f"perimeter state {index}"
            )
            state_entry = next(
                (item for item in assets if item["path"] == state_path), None
            )
            valid_at = record.get("valid_at")
            if (
                record.get("append_order") != index
                or state_entry is None
                or state_entry["sha256"] != state_sha256
                or not isinstance(valid_at, str)
                or valid_at <= previous_valid_at
            ):
                raise BadRequestError(
                    "invalid_perimeter_state",
                    "Perimeter states must be complete, ordered and match the inventory.",
                )
            manifest_state = manifest_states[index - 1]
            if (
                not isinstance(manifest_state, dict)
                or manifest_state.get("append_order") != index
                or manifest_state.get("layer_path") != state_path
                or manifest_state.get("layer_sha256") != state_sha256
                or manifest_state.get("valid_at") != valid_at
            ):
                raise BadRequestError(
                    "perimeter_state_mismatch",
                    "The perimeter contract and manifest states differ.",
                )
            previous_valid_at = valid_at
        package_id = package.get("layer_package_id")
        package_revision = _positive_int(package.get("revision"), label="perimeter revision")
        entry_path = package.get("entry_layer")
        entry_sha256 = _sha256(package.get("entry_layer_sha256"), label="perimeter entry")
        if (
            package.get("manifest_sha256") != manifest_sha256
            or manifest.get("layer_package_id") != package_id
            or manifest.get("revision") != package_revision
            or manifest.get("entry_layer") != entry_path
            or manifest.get("entry_layer_sha256") != entry_sha256
        ):
            raise BadRequestError(
                "perimeter_contract_mismatch", "The perimeter contract and manifest differ."
            )
        base_map = {
            "package_id": base_contract.get("package_id"),
            "revision": _positive_int(base_contract.get("revision"), label="base map revision"),
            "contract_sha256": _sha256(
                base_contract.get("contract_record_sha256"), label="base map contract"
            ),
            "acceptance_receipt_sha256": _sha256(
                base_contract.get("acceptance_receipt_sha256"), label="base map acceptance"
            ),
            "horizontal_crs": progression.get("layer_crs"),
        }
        if (
            manifest_base.get("package_id") != base_map["package_id"]
            or manifest_base.get("revision") != base_map["revision"]
            or manifest_base.get("contract_sha256") != base_map["contract_sha256"]
            or manifest_base.get("acceptance_receipt_sha256")
            != base_map["acceptance_receipt_sha256"]
            or revision != base_map["revision"]
        ):
            raise BadRequestError(
                "perimeter_base_map_mismatch", "The perimeter does not target this map revision."
            )

    acceptance_path = manifest_acceptance.get("receipt")
    acceptance_entry = next(
        (item for item in assets if item["path"] == acceptance_path), None
    )
    if acceptance_entry is None or acceptance_entry["sha256"] != acceptance_sha256:
        raise BadRequestError(
            "acceptance_receipt_mismatch", "The accepted package receipt is missing or changed."
        )
    if not isinstance(package_id, str) or not _PACKAGE_ID_RE.fullmatch(package_id):
        raise BadRequestError("invalid_package_id", "The OpenUSD package id is invalid.")
    if package_id != payload.package_id:
        raise BadRequestError("package_id_mismatch", "The requested package id differs.")
    if not isinstance(entry_path, str):
        raise BadRequestError("package_entry_missing", "The OpenUSD entry stage is missing.")
    entry = next((item for item in assets if item["path"] == entry_path), None)
    if entry is None or entry["sha256"] != entry_sha256:
        raise BadRequestError("package_entry_mismatch", "The OpenUSD entry stage differs.")

    return ValidatedBlobPackage(
        upload_id=payload.upload_id,
        package_id=package_id,
        storage_key=storage_key,
        manifest_sha256=manifest_sha256,
        manifest_size_bytes=len(manifest_raw),
        catalog_sha256=inventory_sha256,
        catalog_size_bytes=len(inventory_raw),
        asset_catalog=assets,
        object_count=len(by_path),
        total_size_bytes=total_size_bytes,
        spatial_profile=spatial_profile,
        package_role=role,
        manifest_path="manifest.json",
        contract_path=contract_path,
        contract_sha256=contract_sha256,
        acceptance_sha256=acceptance_sha256,
        package_revision=package_revision,
        state_count=state_count,
        base_map=base_map,
    )


def validate_blob_package(
    *,
    zone_id: str,
    revision: int,
    payload: AdminSpatialPackageFromBlobRequest,
    settings: Settings,
    store: ObjectStore | None = None,
    expected_role: str | None = None,
) -> ValidatedBlobPackage:
    object_paths = {item.path for item in payload.objects}
    if _OMNIVERSE_COMMON_PATHS.issubset(object_paths):
        return _validate_omniverse_blob_package(
            zone_id=zone_id,
            revision=revision,
            payload=payload,
            settings=settings,
            store=store,
            expected_role=expected_role,
        )
    if expected_role == "perimeter":
        raise BadRequestError(
            "unexpected_package_role", "The perimeter upload requires an OpenUSD perimeter package."
        )
    object_store = store or build_object_store(settings)
    if len(payload.objects) > settings.zone_upload_max_files:
        raise BadRequestError("too_many_package_files", "The package declares too many files.")
    storage_key = f"packages/{payload.upload_id}"
    expected_prefix = object_store.pathname_for(storage_key)
    by_path: dict[str, AdminBlobObjectReference] = {}
    total_size_bytes = 0
    for item in payload.objects:
        path = _safe_path(item.path)
        if path in by_path:
            raise BadRequestError(
                "duplicate_package_path", "The package declares a duplicate path."
            )
        if item.pathname != f"{expected_prefix}/{path}":
            raise BadRequestError(
                "unexpected_blob_pathname",
                "A Blob object is outside the immutable package prefix.",
            )
        if item.content_type != _content_type(path):
            raise BadRequestError(
                "unexpected_blob_content_type",
                "A Blob object has an unexpected content type.",
            )
        total_size_bytes += item.size_bytes
        if total_size_bytes > settings.zone_upload_max_unpacked_bytes:
            raise BadRequestError("package_too_large", "The package exceeds the configured limit.")
        by_path[path] = item
    if not _REQUIRED_PATHS.issubset(by_path):
        raise BadRequestError(
            "missing_package_metadata",
            "package-manifest.json and catalog.json are required.",
        )

    metadata = _list_objects(
        object_store,
        storage_key,
        limit=settings.zone_upload_max_files + 1,
    )
    if len(metadata) > settings.zone_upload_max_files:
        raise BadRequestError("too_many_package_files", "The package contains too many files.")
    if set(metadata) != set(by_path):
        raise BadRequestError(
            "missing_blob_object",
            "The declared Blob objects do not match the stored package inventory.",
        )

    # Vercel Blob exposes pathname and size in one paginated inventory request, but not the
    # stored content type. Only the two small metadata documents need authoritative HEAD calls;
    # every declared asset type is already constrained by its safe suffix and upload token.
    for path in _REQUIRED_PATHS:
        try:
            metadata[path] = object_store.head(object_store.uri_for(f"{storage_key}/{path}"))
        except ObjectStorageError as exc:
            raise BadRequestError(
                "missing_blob_object",
                "The package metadata objects are missing or inaccessible.",
            ) from exc
    for path, item in by_path.items():
        actual = metadata[path]
        if (
            actual.pathname != item.pathname
            or actual.size_bytes != item.size_bytes
            or (actual.content_type is not None and actual.content_type != item.content_type)
        ):
            raise BadRequestError(
                "blob_metadata_mismatch",
                "A Blob object does not match its declared size, pathname or content type.",
            )

    for path in _REQUIRED_PATHS:
        if by_path[path].size_bytes > settings.zone_upload_max_manifest_bytes:
            raise BadRequestError(
                "package_metadata_too_large", f"{path} exceeds the configured limit."
            )
    manifest_raw = object_store.read_bytes(
        object_store.uri_for(f"{storage_key}/package-manifest.json")
    )
    catalog_raw = object_store.read_bytes(object_store.uri_for(f"{storage_key}/catalog.json"))
    if len(manifest_raw) != by_path["package-manifest.json"].size_bytes:
        raise BadRequestError("blob_metadata_mismatch", "package-manifest.json size changed.")
    if len(catalog_raw) != by_path["catalog.json"].size_bytes:
        raise BadRequestError("blob_metadata_mismatch", "catalog.json size changed.")
    manifest = _json_document(manifest_raw, label="package-manifest.json")
    catalog = _json_document(catalog_raw, label="catalog.json")

    package_id = manifest.get("package_id")
    if not isinstance(package_id, str) or not _PACKAGE_ID_RE.fullmatch(package_id):
        raise BadRequestError(
            "invalid_package_id", "package-manifest.json has an invalid package_id."
        )
    if package_id != payload.package_id:
        raise BadRequestError(
            "package_id_mismatch", "The requested package_id does not match the manifest."
        )
    catalog_reference = manifest.get("catalog")
    if not isinstance(catalog_reference, dict) or catalog_reference.get("path") != "catalog.json":
        raise BadRequestError(
            "invalid_package_manifest", "The manifest must reference catalog.json."
        )
    catalog_sha256 = hashlib.sha256(catalog_raw).hexdigest()
    if _sha256(catalog_reference.get("sha256"), label="catalog reference") != catalog_sha256:
        raise BadRequestError(
            "catalog_digest_mismatch", "catalog.json does not match the manifest."
        )
    if _positive_int(catalog_reference.get("byte_count"), label="catalog reference size") != len(
        catalog_raw
    ):
        raise BadRequestError("catalog_size_mismatch", "catalog.json does not match the manifest.")
    zones = manifest.get("zones")
    declared_zone = (
        next(
            (item for item in zones if isinstance(item, dict) and item.get("zone_id") == zone_id),
            None,
        )
        if isinstance(zones, list)
        else None
    )
    if declared_zone is None or declared_zone.get("revision_id") != f"R{revision}":
        raise BadRequestError(
            "package_revision_mismatch",
            "The manifest does not declare the requested zone revision.",
        )

    entries = _catalog_entries(catalog)
    catalog_by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry["path"] in catalog_by_path:
            raise BadRequestError(
                "duplicate_catalog_path", "catalog.json declares a duplicate path."
            )
        _content_type(entry["path"])
        catalog_by_path[entry["path"]] = entry
    if set(by_path) != _REQUIRED_PATHS.union(catalog_by_path):
        raise BadRequestError(
            "package_inventory_mismatch",
            "The uploaded files and catalog.json inventory do not match exactly.",
        )
    for path, entry in catalog_by_path.items():
        if by_path[path].size_bytes != entry["size_bytes"]:
            raise BadRequestError(
                "package_asset_size_mismatch", "A catalog asset has an unexpected size."
            )

    spatial_profile = _remote_tile_spatial_profile(
        catalog,
        by_path=by_path,
        object_store=object_store,
        storage_key=storage_key,
    )

    return ValidatedBlobPackage(
        upload_id=payload.upload_id,
        package_id=package_id,
        storage_key=storage_key,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        manifest_size_bytes=len(manifest_raw),
        catalog_sha256=catalog_sha256,
        catalog_size_bytes=len(catalog_raw),
        asset_catalog=[catalog_by_path[path] for path in sorted(catalog_by_path)],
        object_count=len(by_path),
        total_size_bytes=total_size_bytes,
        spatial_profile=spatial_profile,
    )


def recover_blob_package_request(
    *,
    upload_id: str,
    package_id: str,
    reason: str,
    settings: Settings,
    store: ObjectStore | None = None,
) -> AdminSpatialPackageFromBlobRequest:
    """Rebuild the client inventory for an interrupted, fully stored upload."""

    object_store = store or build_object_store(settings)
    storage_key = f"packages/{upload_id}"
    metadata = _list_objects(
        object_store,
        storage_key,
        limit=settings.zone_upload_max_files + 1,
    )
    if len(metadata) > settings.zone_upload_max_files:
        raise BadRequestError("too_many_package_files", "The package contains too many files.")
    return AdminSpatialPackageFromBlobRequest(
        upload_id=upload_id,
        package_id=package_id,
        reason=reason,
        objects=[
            AdminBlobObjectReference(
                path=path,
                pathname=item.pathname,
                size_bytes=item.size_bytes,
                content_type=_content_type(path),
            )
            for path, item in sorted(metadata.items())
        ],
    )


def _kind_and_media_type(path: str) -> tuple[SpatialPackageFileKind, str]:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in {".jpg", ".jpeg"}:
        return SpatialPackageFileKind.JPEG, "image/jpeg"
    if suffix == ".png":
        return SpatialPackageFileKind.PNG, "image/png"
    if suffix == ".glb":
        return SpatialPackageFileKind.GLB, "model/gltf-binary"
    if suffix in {".tif", ".tiff"}:
        return SpatialPackageFileKind.COG, "image/tiff"
    if suffix == ".fwtile":
        return SpatialPackageFileKind.FWTILE, "application/vnd.fireviewer.tile"
    if suffix == ".fwterrain":
        return SpatialPackageFileKind.FWTERRAIN, "application/vnd.fireviewer.terrain"
    if suffix == ".json":
        return SpatialPackageFileKind.JSON, "application/json"
    if suffix in {".usd", ".usda", ".usdc"}:
        return SpatialPackageFileKind.OPENUSD, "model/vnd.usd"
    if suffix == ".usdz":
        return SpatialPackageFileKind.OPENUSD, "model/vnd.usdz+zip"
    if suffix == ".hdr":
        return SpatialPackageFileKind.AUXILIARY, "image/vnd.radiance"
    if suffix == ".jgw":
        return SpatialPackageFileKind.AUXILIARY, "text/plain"
    if suffix == ".npz":
        return SpatialPackageFileKind.AUXILIARY, "application/octet-stream"
    raise BadRequestError("unsupported_package_file_type", "The package asset type is unsupported.")


def persist_validated_blob_package(
    session: Session,
    *,
    zone_id: str,
    revision: int,
    validated: ValidatedBlobPackage,
    actor: Actor,
    settings: Settings,
) -> SpatialPackage:
    """Persist one validated Blob inventory inside an existing transaction."""

    if (
        session.execute(
            select(SpatialPackage.id).where(SpatialPackage.package_id == validated.package_id)
        ).scalar_one_or_none()
        is not None
    ):
        raise ConflictError(
            "spatial_package_already_exists",
            "The package identifier is already registered.",
        )

    store = build_object_store(settings)
    package = SpatialPackage(
        package_id=validated.package_id,
        manifest_uri=store.uri_for(f"{validated.storage_key}/{validated.manifest_path}"),
        manifest_sha256=validated.manifest_sha256,
        manifest_size_bytes=validated.manifest_size_bytes,
        storage_uri=store.uri_for(validated.storage_key),
        state=SpatialPackageState.DRAFT,
        provenance={
            "upload_id": validated.upload_id,
            "zone_id": zone_id,
            "revision": revision,
            "catalog_sha256": validated.catalog_sha256,
            "catalog_size_bytes": validated.catalog_size_bytes,
            "package_role": validated.package_role,
            "manifest_path": validated.manifest_path,
            "contract_path": validated.contract_path,
            "contract_sha256": validated.contract_sha256,
            "acceptance_sha256": validated.acceptance_sha256,
            "package_revision": validated.package_revision,
            "state_count": validated.state_count,
            "base_map": validated.base_map,
        },
        verification_report={
            "status": "finalized_from_blob",
            "summary": (
                "Stored Blob pathnames and sizes match the immutable package inventory; "
                "declared content types match supported file extensions."
            ),
            "object_count": validated.object_count,
        },
        created_by=actor.actor_id,
    )
    session.add(package)
    session.flush()
    files = []
    for entry in validated.asset_catalog:
        kind, media_type = _kind_and_media_type(entry["path"])
        files.append(
            SpatialPackageFile(
                spatial_package_id=package.id,
                kind=kind,
                uri=store.uri_for(f"{validated.storage_key}/{entry['path']}"),
                sha256=entry["sha256"],
                size_bytes=entry["size_bytes"],
                media_type=media_type,
                provenance={"catalog_path": entry["path"], "upload_id": validated.upload_id},
            )
        )
    session.add_all(files)
    session.flush()
    package.files = files
    return package


def import_blob_package(
    session: Session,
    *,
    zone_id: str,
    revision: int,
    payload: AdminSpatialPackageFromBlobRequest,
    validated: ValidatedBlobPackage,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> BlobSpatialImportOutcome:
    endpoint = f"POST /api/v1/admin/zones/{zone_id}/revisions/{revision}/packages/from-blob"
    request_hash = sha256_hex(
        {"actor_id": actor.actor_id, "payload": payload.model_dump(mode="json")}
    )
    begin_write_transaction(session)
    replay = find_replay(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay:
        session.rollback()
        return BlobSpatialImportOutcome(
            AdminSpatialPackageImportEnvelope.model_validate(replay.response_body),
            True,
        )
    revision_row = session.execute(
        select(SpatialZoneRevision)
        .join(SpatialZone)
        .where(SpatialZone.zone_id == zone_id, SpatialZoneRevision.revision == revision)
        .with_for_update()
    ).scalar_one_or_none()
    if revision_row is None:
        raise NotFoundError("spatial_zone_revision", f"{zone_id}/revisions/{revision}")
    package = persist_validated_blob_package(
        session,
        zone_id=zone_id,
        revision=revision,
        validated=validated,
        actor=actor,
        settings=settings,
    )
    files = package.files
    response = AdminSpatialPackageImportEnvelope(
        package=AdminSpatialPackageImportResponse(
            package_id=package.package_id,
            state=package.state,
            upload_id=validated.upload_id,
            object_count=validated.object_count,
            total_size_bytes=validated.total_size_bytes,
            asset_count=len(files),
            validation_summary="Stored Blob inventory and package metadata were verified.",
        ),
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="spatial_package.finalized_from_blob",
        target_type="spatial_package",
        target_id=package.package_id,
        reason=payload.reason,
        trace_id=trace_id,
        after=response.package.model_dump(mode="json"),
        payload={"zone_id": zone_id, "revision": revision},
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=201,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return BlobSpatialImportOutcome(response, False)
