from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Never
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from fire_viewer.core.time import utcnow
from fire_viewer.domain.enums import ExternalArtifactStatus, ExternalSemanticRole
from fire_viewer.domain.external_source_schemas import (
    ExternalArtifactInput,
    ExternalCollectionInput,
    ExternalProviderInput,
)

if TYPE_CHECKING:
    from fire_viewer.core.config import Settings
    from fire_viewer.services.external_source_scheduler import (
        ExternalCollectionContext,
        ExternalConnectorRegistry,
        ExternalFetchResult,
    )


_CDSE_STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
_CDSE_STAC_HOST = "stac.dataspace.copernicus.eu"
_CDSE_STAC_COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,126}[a-z0-9]$")

_IGN_WFS_URL = "https://data.geopf.fr/wfs/ows"
_IGN_WFS_HOST = "data.geopf.fr"
_IGN_TYPE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

_METEO_FRANCE_SYNOPTIC_URL = "https://public-api.meteofrance.fr/public/DPObs/v1/synop"
_METEO_FRANCE_HOST = "public-api.meteofrance.fr"
_SYNOP_STATION_RE = re.compile(r"^[0-9]{5}$")

_SAFE_HEADER_RE = re.compile(r"^[\x20-\x7e]{1,512}$")
_WATERMARK_PREFIX = "http-v1."
_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "apikey",
        "api_key",
        "authorization",
        "key",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


class OfficialConnectorError(RuntimeError):
    """Base error with a bounded, non-sensitive diagnostic code."""


class OfficialConnectorConfigurationError(OfficialConnectorError, ValueError):
    """The connector or incident-scoped source plan is incomplete or invalid."""


class OfficialConnectorFetchError(OfficialConnectorError):
    """The official service failed or returned an unsafe response."""


class OfficialConnectorAuthenticationError(OfficialConnectorFetchError):
    """The official service rejected the configured server-side credential."""


@dataclass(frozen=True, slots=True)
class _HttpPayload:
    status_code: int
    headers: Mapping[str, str]
    request_url: str
    body: bytes


@dataclass(frozen=True, slots=True)
class _QueryWindow:
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True, slots=True)
class _ConditionalState:
    query_digest: str
    etag: str | None
    last_modified: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class OfficialSourceDefinition:
    """Pure bootstrap metadata; providers remain disabled until explicitly activated."""

    provider: ExternalProviderInput
    collections: tuple[ExternalCollectionInput, ...]


def official_source_definitions() -> tuple[OfficialSourceDefinition, ...]:
    """Return reviewed non-secret metadata for the three implemented official interfaces.

    The IGN entry is intentionally a template: a concrete open-data WFS layer and its
    exact licence must be selected before an adapter route is enabled.
    """

    ign_provider = ExternalProviderInput(
        provider_key="ign-geoplateforme",
        display_name="IGN Géoplateforme",
        allowed_domains=[_IGN_WFS_HOST],
        authentication_kind="none",
        attribution="Institut national de l'information géographique et forestière (IGN)",
        enabled=False,
    )
    ign_collection = ExternalCollectionInput(
        provider_key=ign_provider.provider_key,
        collection_key="wfs-geospatial-reference-template",
        product_name="Géoplateforme WFS open-data layer (explicit selection required)",
        sensor=None,
        platform="Géoplateforme",
        license=(
            "Licence Ouverte / Open Licence Etalab 2.0; activation is restricted to a "
            "layer whose product metadata confirms this licence"
        ),
        cadence_seconds=86_400,
        semantic_role=ExternalSemanticRole.GEOSPATIAL_REFERENCE,
        configuration={
            "connector_kind": "ign_geoplateforme_wfs",
            "type_name_required": True,
        },
    )

    meteo_provider = ExternalProviderInput(
        provider_key="meteo-france",
        display_name="Météo-France",
        allowed_domains=[_METEO_FRANCE_HOST],
        authentication_kind="oauth2",
        attribution="Météo-France",
        enabled=False,
    )
    meteo_collection = ExternalCollectionInput(
        provider_key=meteo_provider.provider_key,
        collection_key="dpobs-synop",
        product_name="Observations essentielles SYNOP",
        sensor="SYNOP surface observation network",
        platform="Météo-France DPObs",
        license="Licence Ouverte / Open Licence Etalab 2.0",
        cadence_seconds=10_800,
        semantic_role=ExternalSemanticRole.WEATHER_OBSERVATION,
        configuration={
            "connector_kind": "meteo_france_synop",
            "station_ids_scope": "incident_source_plan",
        },
    )

    cdse_provider = ExternalProviderInput(
        provider_key="copernicus-data-space",
        display_name="Copernicus Data Space Ecosystem",
        allowed_domains=[_CDSE_STAC_HOST],
        authentication_kind="none",
        attribution="European Union, Copernicus Sentinel data",
        enabled=False,
    )
    cdse_collections = tuple(
        ExternalCollectionInput(
            provider_key=cdse_provider.provider_key,
            collection_key=collection_key,
            product_name=product_name,
            sensor=sensor,
            platform=platform,
            license="Copernicus Sentinel Data Legal Notice",
            cadence_seconds=cadence,
            semantic_role=semantic_role,
            configuration={
                "connector_kind": "cdse_stac",
                "maximum_items": 100,
                "stac_collection_id": collection_key,
            },
        )
        for collection_key, product_name, sensor, platform, cadence, semantic_role in (
            (
                "sentinel-3-sl-2-frp-nrt",
                "Sentinel-3 SLSTR Fire Radiative Power NRT",
                "SLSTR",
                "Sentinel-3",
                3_600,
                ExternalSemanticRole.SENSOR_DETECTION,
            ),
            (
                "sentinel-3-sl-2-frp-ntc",
                "Sentinel-3 SLSTR Fire Radiative Power NTC",
                "SLSTR",
                "Sentinel-3",
                86_400,
                ExternalSemanticRole.SENSOR_DETECTION,
            ),
            (
                "sentinel-2-l2a",
                "Sentinel-2 Level-2A",
                "MSI",
                "Sentinel-2",
                21_600,
                ExternalSemanticRole.RAW_EARTH_OBSERVATION,
            ),
            (
                "sentinel-1-grd",
                "Sentinel-1 Ground Range Detected",
                "SAR",
                "Sentinel-1",
                21_600,
                ExternalSemanticRole.RAW_EARTH_OBSERVATION,
            ),
        )
    )
    return (
        OfficialSourceDefinition(provider=ign_provider, collections=(ign_collection,)),
        OfficialSourceDefinition(provider=meteo_provider, collections=(meteo_collection,)),
        OfficialSourceDefinition(provider=cdse_provider, collections=cdse_collections),
    )


def build_official_connector_registry(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
) -> ExternalConnectorRegistry:
    """Build exact configured routes without reading credentials from collection metadata."""

    from fire_viewer.services.external_source_scheduler import ExternalConnectorRegistry

    registry = ExternalConnectorRegistry()
    if not settings.official_connectors_enabled:
        return registry
    routes = settings.official_connector_collections
    enabled_routes = [
        (route, configuration)
        for route, configuration in routes.items()
        if configuration.get("enabled") is True
    ]
    if not enabled_routes:
        return registry
    shared_client = client or httpx.Client(trust_env=False)
    for route, configuration in sorted(enabled_routes):
        provider_key, collection_key = route.split("/", maxsplit=1)
        kind = configuration.get("kind")
        if kind == "cdse_stac":
            connector: Any = CdseStacConnector(shared_client)
        elif kind == "ign_geoplateforme_wfs":
            connector = IgnGeoplateformeWfsConnector(shared_client)
        elif kind == "meteo_france_synop":
            token = settings.meteo_france_access_token
            if token is None:
                raise OfficialConnectorConfigurationError("meteo_france_access_token_required")
            connector = MeteoFranceSynopConnector(shared_client, access_token=token)
        else:
            raise OfficialConnectorConfigurationError("official_connector_kind_invalid")
        registry.register(
            provider_key=provider_key,
            collection_key=collection_key,
            connector=connector,
        )
    return registry


def _raise_configuration(code: str) -> Never:
    raise OfficialConnectorConfigurationError(code)


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _raise_configuration(f"{field}_timezone_required")
    return value.astimezone(UTC)


def _query_window(
    context: ExternalCollectionContext,
    *,
    maximum: timedelta,
) -> _QueryWindow:
    if context.observed_start_at is None:
        _raise_configuration("observed_start_at_required")
    start_at = _utc(context.observed_start_at, field="observed_start_at")
    end_at = (
        _utc(context.observed_end_at, field="observed_end_at")
        if context.observed_end_at is not None
        else start_at
    )
    if end_at < start_at:
        _raise_configuration("observed_window_invalid")

    lookback = _bounded_seconds(context.plan_configuration, "lookback_seconds")
    lookahead = _bounded_seconds(context.plan_configuration, "lookahead_seconds")
    start_at -= timedelta(seconds=lookback)
    end_at += timedelta(seconds=lookahead)
    if end_at - start_at > maximum:
        _raise_configuration("observed_window_too_large")
    return _QueryWindow(start_at=start_at, end_at=end_at)


def _bounded_seconds(configuration: Mapping[str, object], key: str) -> int:
    raw = configuration.get(key, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 604_800:
        _raise_configuration(f"{key}_invalid")
    return raw


def _bbox(context: ExternalCollectionContext) -> tuple[float, float, float, float]:
    if len(context.bbox_wgs84) != 4:
        _raise_configuration("bbox_wgs84_invalid")
    values = tuple(float(value) for value in context.bbox_wgs84)
    if not all(math.isfinite(value) for value in values):
        _raise_configuration("bbox_wgs84_invalid")
    min_lon, min_lat, max_lon, max_lat = values
    if (
        not -180 <= min_lon < max_lon <= 180
        or not -90 <= min_lat < max_lat <= 90
        or max_lon - min_lon > 20
        or max_lat - min_lat > 20
    ):
        _raise_configuration("bbox_wgs84_invalid")
    return min_lon, min_lat, max_lon, max_lat


def _bbox_polygon(bbox: tuple[float, float, float, float]) -> dict[str, object]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat],
            ]
        ],
    }


def _reference_point(context: ExternalCollectionContext) -> tuple[float, float]:
    if len(context.reference_point_wgs84) != 2:
        _raise_configuration("reference_point_wgs84_invalid")
    longitude, latitude = (float(value) for value in context.reference_point_wgs84)
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        _raise_configuration("reference_point_wgs84_invalid")
    return longitude, latitude


def _haversine_metres(left: tuple[float, float], right: tuple[float, float]) -> float:
    left_lon, left_lat = (math.radians(value) for value in left)
    right_lon, right_lat = (math.radians(value) for value in right)
    delta_lon = right_lon - left_lon
    delta_lat = right_lat - left_lat
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(left_lat) * math.cos(right_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _require_semantic_role(
    context: ExternalCollectionContext,
    allowed: frozenset[ExternalSemanticRole],
) -> None:
    if context.semantic_role not in allowed:
        _raise_configuration("external_semantic_role_mismatch")


def _configuration_string(
    configuration: Mapping[str, object],
    key: str,
    *,
    maximum_length: int = 512,
) -> str:
    raw = configuration.get(key)
    if not isinstance(raw, str):
        _raise_configuration(f"{key}_required")
    value = raw.strip()
    if not value or len(value) > maximum_length:
        _raise_configuration(f"{key}_invalid")
    return value


def _configuration_int(
    configuration: Mapping[str, object],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = configuration.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        _raise_configuration(f"{key}_invalid")
    return raw


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise OfficialConnectorFetchError("official_response_json_invalid") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode_json(body: bytes) -> object:
    try:
        return json.loads(body, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise OfficialConnectorFetchError("official_response_json_invalid") from exc


def _safe_response_header(headers: Mapping[str, str], key: str) -> str | None:
    raw = headers.get(key)
    if raw is None:
        return None
    value = raw.strip()
    if not _SAFE_HEADER_RE.fullmatch(value) or "\r" in value or "\n" in value:
        raise OfficialConnectorFetchError("official_response_validator_invalid")
    if key.casefold() == "last-modified":
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise OfficialConnectorFetchError("official_response_validator_invalid") from exc
        if parsed.tzinfo is None:
            raise OfficialConnectorFetchError("official_response_validator_invalid")
    return value


def _validate_https_url(
    url: str,
    *,
    exact_host: str,
    exact_path: str | None = None,
    path_prefix: str | None = None,
) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise OfficialConnectorFetchError("official_response_url_invalid") from exc
    hostname = (parts.hostname or "").casefold().rstrip(".")
    path_valid = exact_path is None or parts.path == exact_path
    if path_prefix is not None:
        path_valid = path_valid and parts.path.startswith(path_prefix)
    if (
        parts.scheme.casefold() != "https"
        or hostname != exact_host
        or port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
        or bool(parts.fragment)
        or not path_valid
    ):
        raise OfficialConnectorFetchError("official_response_url_invalid")
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        if key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS:
            raise OfficialConnectorFetchError("official_response_url_contains_secret")
    return urlunsplit(("https", exact_host, parts.path, parts.query, ""))


def _bounded_request(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    exact_host: str,
    exact_path: str,
    timeout_seconds: float,
    maximum_bytes: int,
    headers: Mapping[str, str],
    params: Mapping[str, str | int] | None = None,
    json_body: Mapping[str, object] | None = None,
) -> _HttpPayload:
    _validate_https_url(url, exact_host=exact_host, exact_path=exact_path)
    request_headers = {
        "Accept-Encoding": "identity",
        "User-Agent": "FireViewer-official-connectors/1",
        **headers,
    }
    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )
    try:
        with client.stream(
            method,
            url,
            params=params,
            json=json_body,
            headers=request_headers,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            request_url = _validate_https_url(
                str(response.request.url),
                exact_host=exact_host,
                exact_path=exact_path,
            )
            if response.status_code in {401, 403}:
                raise OfficialConnectorAuthenticationError("official_source_authentication_failed")
            if response.status_code == 304:
                return _HttpPayload(
                    status_code=304,
                    headers=response.headers,
                    request_url=request_url,
                    body=b"",
                )
            if response.status_code == 429:
                raise OfficialConnectorFetchError("official_source_rate_limited")
            if not 200 <= response.status_code < 300:
                raise OfficialConnectorFetchError("official_source_http_error")
            declared_length = response.headers.get("content-length")
            if declared_length is not None:
                try:
                    if int(declared_length) > maximum_bytes:
                        raise OfficialConnectorFetchError("official_response_too_large")
                except ValueError as exc:
                    raise OfficialConnectorFetchError(
                        "official_response_content_length_invalid"
                    ) from exc
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > maximum_bytes:
                    raise OfficialConnectorFetchError("official_response_too_large")
                chunks.append(chunk)
            return _HttpPayload(
                status_code=response.status_code,
                headers=response.headers,
                request_url=request_url,
                body=b"".join(chunks),
            )
    except OfficialConnectorError:
        raise
    except httpx.TimeoutException as exc:
        raise OfficialConnectorFetchError("official_source_timeout") from exc
    except httpx.HTTPError as exc:
        raise OfficialConnectorFetchError("official_source_network_error") from exc


def _require_json_content_type(payload: _HttpPayload) -> None:
    raw = payload.headers.get("content-type", "")
    media_type = raw.partition(";")[0].strip().casefold()
    if media_type not in {
        "application/geo+json",
        "application/json",
        "application/vnd.api+json",
    }:
        raise OfficialConnectorFetchError("official_response_content_type_invalid")


def _encode_watermark(state: _ConditionalState) -> str:
    payload = {
        "content_hash": state.content_hash,
        "etag": state.etag,
        "last_modified": state.last_modified,
        "query_digest": state.query_digest,
        "version": 1,
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).decode("ascii").rstrip("=")
    watermark = f"{_WATERMARK_PREFIX}{encoded}"
    if len(watermark) > 1_000:
        raise OfficialConnectorFetchError("official_watermark_too_large")
    return watermark


def _decode_watermark(value: str | None) -> _ConditionalState | None:
    if value is None:
        return None
    if not value.startswith(_WATERMARK_PREFIX):
        _raise_configuration("official_watermark_invalid")
    encoded = value.removeprefix(_WATERMARK_PREFIX)
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = _decode_json(decoded)
    except (ValueError, OfficialConnectorFetchError) as exc:
        raise OfficialConnectorConfigurationError("official_watermark_invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        _raise_configuration("official_watermark_invalid")
    query_digest = payload.get("query_digest")
    etag = payload.get("etag")
    last_modified = payload.get("last_modified")
    content_hash = payload.get("content_hash")
    if not isinstance(query_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", query_digest):
        _raise_configuration("official_watermark_invalid")
    for item in (etag, last_modified, content_hash):
        if item is not None and not isinstance(item, str):
            _raise_configuration("official_watermark_invalid")
    if content_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        _raise_configuration("official_watermark_invalid")
    if etag is not None and not _SAFE_HEADER_RE.fullmatch(etag):
        _raise_configuration("official_watermark_invalid")
    if last_modified is not None and not _SAFE_HEADER_RE.fullmatch(last_modified):
        _raise_configuration("official_watermark_invalid")
    return _ConditionalState(
        query_digest=query_digest,
        etag=etag,
        last_modified=last_modified,
        content_hash=content_hash,
    )


def _conditional_headers(
    context: ExternalCollectionContext,
    *,
    query_digest: str,
) -> dict[str, str]:
    previous = _decode_watermark(context.watermark)
    if previous is None or previous.query_digest != query_digest:
        return {}
    headers: dict[str, str] = {}
    if previous.etag:
        headers["If-None-Match"] = previous.etag
    if previous.last_modified:
        headers["If-Modified-Since"] = previous.last_modified
    return headers


def _result(
    artifacts: Sequence[ExternalArtifactInput],
    *,
    query_digest: str,
    payload: _HttpPayload,
    content_hash: str | None,
    unchanged_watermark: str | None = None,
) -> ExternalFetchResult:
    from fire_viewer.services.external_source_scheduler import ExternalFetchResult

    if payload.status_code == 304:
        if unchanged_watermark is None:
            raise OfficialConnectorFetchError("official_source_unexpected_not_modified")
        return ExternalFetchResult(artifacts=(), watermark=unchanged_watermark)
    etag = _safe_response_header(payload.headers, "etag")
    last_modified = _safe_response_header(payload.headers, "last-modified")
    watermark = _encode_watermark(
        _ConditionalState(
            query_digest=query_digest,
            etag=etag,
            last_modified=last_modified,
            content_hash=content_hash,
        )
    )
    return ExternalFetchResult(artifacts=tuple(artifacts), watermark=watermark)


def _response_validators(headers: Mapping[str, str]) -> dict[str, str]:
    validators: dict[str, str] = {}
    etag = _safe_response_header(headers, "etag")
    last_modified = _safe_response_header(headers, "last-modified")
    if etag is not None:
        validators["response_etag"] = etag
    if last_modified is not None:
        validators["response_last_modified"] = last_modified
    return validators


def _parse_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise OfficialConnectorFetchError(f"official_{field}_invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OfficialConnectorFetchError(f"official_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OfficialConnectorFetchError(f"official_{field}_timezone_required")
    return parsed.astimezone(UTC)


def _stac_acquisition(properties: Mapping[str, object]) -> tuple[datetime, datetime | None]:
    instant = properties.get("datetime")
    if instant is not None:
        return _parse_datetime(instant, field="acquisition_time"), None
    start = _parse_datetime(properties.get("start_datetime"), field="acquisition_start")
    end = _parse_datetime(properties.get("end_datetime"), field="acquisition_end")
    if end < start:
        raise OfficialConnectorFetchError("official_acquisition_window_invalid")
    return start, end


def _positive_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OfficialConnectorFetchError("official_resolution_invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise OfficialConnectorFetchError("official_resolution_invalid")
    return parsed


class CdseStacConnector:
    """Bounded CDSE STAC 1.1 discovery for one exact configured collection."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        timeout_seconds: float = 20.0,
        maximum_response_bytes: int = 8_388_608,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds_out_of_range")
        if not 65_536 <= maximum_response_bytes <= 33_554_432:
            raise ValueError("maximum_response_bytes_out_of_range")
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._clock = clock

    def fetch(self, context: ExternalCollectionContext) -> ExternalFetchResult:
        _require_semantic_role(
            context,
            frozenset(
                {
                    ExternalSemanticRole.RAW_EARTH_OBSERVATION,
                    ExternalSemanticRole.SENSOR_DETECTION,
                }
            ),
        )
        bbox = _bbox(context)
        window = _query_window(context, maximum=timedelta(days=31))
        stac_collection = _configuration_string(
            context.collection_configuration, "stac_collection_id", maximum_length=128
        ).casefold()
        if not _CDSE_STAC_COLLECTION_RE.fullmatch(stac_collection):
            _raise_configuration("stac_collection_id_invalid")
        maximum_items = _configuration_int(
            context.collection_configuration,
            "maximum_items",
            default=100,
            minimum=1,
            maximum=250,
        )
        query: dict[str, object] = {
            "collections": [stac_collection],
            "datetime": f"{_iso_z(window.start_at)}/{_iso_z(window.end_at)}",
            "intersects": _bbox_polygon(bbox),
            "limit": maximum_items,
            "sortby": [{"field": "datetime", "direction": "asc"}],
        }
        query_digest = _sha256(_canonical_json(query))
        headers = {"Accept": "application/geo+json, application/json"}
        headers.update(_conditional_headers(context, query_digest=query_digest))
        response = _bounded_request(
            self._client,
            method="POST",
            url=_CDSE_STAC_SEARCH_URL,
            exact_host=_CDSE_STAC_HOST,
            exact_path="/v1/search",
            timeout_seconds=self._timeout_seconds,
            maximum_bytes=self._maximum_response_bytes,
            headers=headers,
            json_body=query,
        )
        if response.status_code == 304:
            return _result(
                (),
                query_digest=query_digest,
                payload=response,
                content_hash=None,
                unchanged_watermark=context.watermark,
            )
        _require_json_content_type(response)
        document = _decode_json(response.body)
        if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
            raise OfficialConnectorFetchError("cdse_stac_feature_collection_invalid")
        features = document.get("features")
        links = document.get("links", [])
        if not isinstance(features, list) or len(features) > maximum_items:
            raise OfficialConnectorFetchError("cdse_stac_features_invalid")
        if not isinstance(links, list):
            raise OfficialConnectorFetchError("cdse_stac_links_invalid")
        if any(isinstance(link, dict) and link.get("rel") == "next" for link in links):
            raise OfficialConnectorFetchError("cdse_stac_result_window_truncated")

        retrieved_at = _utc(self._clock(), field="connector_clock")
        validators = _response_validators(response.headers)
        artifacts: list[ExternalArtifactInput] = []
        for raw_item in features:
            if not isinstance(raw_item, dict) or raw_item.get("type") != "Feature":
                raise OfficialConnectorFetchError("cdse_stac_item_invalid")
            item_id = raw_item.get("id")
            item_collection = raw_item.get("collection")
            properties = raw_item.get("properties")
            geometry = raw_item.get("geometry")
            if (
                not isinstance(item_id, str)
                or not item_id.strip()
                or len(item_id) > 512
                or item_collection != stac_collection
                or not isinstance(properties, dict)
                or not isinstance(geometry, dict)
            ):
                raise OfficialConnectorFetchError("cdse_stac_item_invalid")
            try:
                parsed_geometry = shape(geometry)
            except Exception as exc:
                raise OfficialConnectorFetchError("cdse_stac_geometry_invalid") from exc
            if parsed_geometry.is_empty or not parsed_geometry.is_valid:
                raise OfficialConnectorFetchError("cdse_stac_geometry_invalid")
            min_x, min_y, max_x, max_y = parsed_geometry.bounds
            if not (-180 <= min_x <= max_x <= 180 and -90 <= min_y <= max_y <= 90):
                raise OfficialConnectorFetchError("cdse_stac_geometry_invalid")

            acquisition_start, acquisition_end = _stac_acquisition(properties)
            created = properties.get("created")
            processed_at = (
                _parse_datetime(created, field="processed_at") if created is not None else None
            )
            processing_baseline_raw = properties.get(
                "processing:version", properties.get("s2:processing_baseline")
            )
            if processing_baseline_raw is not None and not isinstance(
                processing_baseline_raw, str | int | float
            ):
                raise OfficialConnectorFetchError("cdse_stac_processing_baseline_invalid")
            processing_baseline = (
                str(processing_baseline_raw)[:128] if processing_baseline_raw is not None else None
            )
            item_bytes = _canonical_json(raw_item)
            encoded_collection = quote(stac_collection, safe="")
            encoded_item = quote(item_id, safe="")
            source_url = _validate_https_url(
                f"https://{_CDSE_STAC_HOST}/v1/collections/"
                f"{encoded_collection}/items/{encoded_item}",
                exact_host=_CDSE_STAC_HOST,
                path_prefix=f"/v1/collections/{encoded_collection}/items/",
            )
            quality_flags: dict[str, object] = {
                "catalog": "CDSE_STAC_1_1",
                "stac_collection": stac_collection,
                "stac_version": raw_item.get("stac_version"),
                **validators,
            }
            for source_name, target_name in (
                ("platform", "platform"),
                ("instruments", "instruments"),
                ("eo:cloud_cover", "cloud_cover_percent"),
            ):
                value = properties.get(source_name)
                if value is not None:
                    quality_flags[target_name] = value
            artifacts.append(
                ExternalArtifactInput(
                    collection_id=context.collection_id,
                    external_product_id=item_id,
                    source_url=source_url,
                    content_hash=_sha256(item_bytes),
                    processing_baseline=processing_baseline,
                    acquisition_granule_id=item_id,
                    acquisition_start_at=acquisition_start,
                    acquisition_end_at=acquisition_end,
                    processed_at=processed_at,
                    retrieved_at=retrieved_at,
                    native_crs="EPSG:4326",
                    footprint_geojson=mapping(parsed_geometry),
                    resolution_m=_positive_float(properties.get("gsd")),
                    quality_flags=quality_flags,
                    status=ExternalArtifactStatus.PROVISIONAL,
                )
            )
        response_hash = _sha256(_canonical_json(document))
        return _result(
            artifacts,
            query_digest=query_digest,
            payload=response,
            content_hash=response_hash,
        )


class IgnGeoplateformeWfsConnector:
    """Bounded incident-AOI read of one explicit public IGN WFS layer."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        timeout_seconds: float = 20.0,
        maximum_response_bytes: int = 16_777_216,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds_out_of_range")
        if not 65_536 <= maximum_response_bytes <= 33_554_432:
            raise ValueError("maximum_response_bytes_out_of_range")
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._clock = clock

    def fetch(self, context: ExternalCollectionContext) -> ExternalFetchResult:
        _require_semantic_role(context, frozenset({ExternalSemanticRole.GEOSPATIAL_REFERENCE}))
        bbox = _bbox(context)
        window = _query_window(context, maximum=timedelta(days=31))
        type_name = _configuration_string(
            context.collection_configuration, "type_name", maximum_length=200
        )
        if not _IGN_TYPE_NAME_RE.fullmatch(type_name):
            _raise_configuration("type_name_invalid")
        maximum_features = _configuration_int(
            context.collection_configuration,
            "maximum_features",
            default=500,
            minimum=1,
            maximum=1_000,
        )
        params: dict[str, str | int] = {
            "SERVICE": "WFS",
            "VERSION": "2.0.0",
            "REQUEST": "GetFeature",
            "TYPENAMES": type_name,
            "OUTPUTFORMAT": "application/json",
            "SRSNAME": "EPSG:4326",
            "BBOX": ",".join(str(value) for value in (*bbox, "EPSG:4326")),
            "COUNT": maximum_features,
        }
        query_identity = {
            "bbox": bbox,
            "endpoint": _IGN_WFS_URL,
            "maximum_features": maximum_features,
            "type_name": type_name,
        }
        query_digest = _sha256(_canonical_json(query_identity))
        headers = {"Accept": "application/geo+json, application/json"}
        headers.update(_conditional_headers(context, query_digest=query_digest))
        response = _bounded_request(
            self._client,
            method="GET",
            url=_IGN_WFS_URL,
            exact_host=_IGN_WFS_HOST,
            exact_path="/wfs/ows",
            timeout_seconds=self._timeout_seconds,
            maximum_bytes=self._maximum_response_bytes,
            headers=headers,
            params=params,
        )
        if response.status_code == 304:
            return _result(
                (),
                query_digest=query_digest,
                payload=response,
                content_hash=None,
                unchanged_watermark=context.watermark,
            )
        _require_json_content_type(response)
        document = _decode_json(response.body)
        if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
            raise OfficialConnectorFetchError("ign_wfs_feature_collection_invalid")
        features = document.get("features")
        if not isinstance(features, list) or len(features) > maximum_features:
            raise OfficialConnectorFetchError("ign_wfs_features_invalid")
        number_matched = document.get("numberMatched")
        if isinstance(number_matched, int) and number_matched > len(features):
            raise OfficialConnectorFetchError("ign_wfs_result_window_truncated")
        number_returned = document.get("numberReturned")
        if isinstance(number_returned, int) and number_returned != len(features):
            raise OfficialConnectorFetchError("ign_wfs_feature_count_invalid")
        if len(features) == maximum_features and not isinstance(number_matched, int):
            raise OfficialConnectorFetchError("ign_wfs_result_window_truncated")
        crs = document.get("crs")
        if crs is not None:
            if not isinstance(crs, dict):
                raise OfficialConnectorFetchError("ign_wfs_crs_invalid")
            properties = crs.get("properties")
            crs_name = properties.get("name") if isinstance(properties, dict) else None
            if crs_name not in {
                "EPSG:4326",
                "urn:ogc:def:crs:EPSG::4326",
                "urn:ogc:def:crs:OGC:1.3:CRS84",
            }:
                raise OfficialConnectorFetchError("ign_wfs_crs_invalid")
        if not features:
            return _result(
                (),
                query_digest=query_digest,
                payload=response,
                content_hash=_sha256(_canonical_json(document)),
            )

        geometries = []
        for raw_feature in features:
            if not isinstance(raw_feature, dict) or raw_feature.get("type") != "Feature":
                raise OfficialConnectorFetchError("ign_wfs_feature_invalid")
            geometry = raw_feature.get("geometry")
            if not isinstance(geometry, dict):
                raise OfficialConnectorFetchError("ign_wfs_geometry_invalid")
            try:
                parsed = shape(geometry)
            except Exception as exc:
                raise OfficialConnectorFetchError("ign_wfs_geometry_invalid") from exc
            if parsed.is_empty or not parsed.is_valid:
                raise OfficialConnectorFetchError("ign_wfs_geometry_invalid")
            min_x, min_y, max_x, max_y = parsed.bounds
            if not (-180 <= min_x <= max_x <= 180 and -90 <= min_y <= max_y <= 90):
                raise OfficialConnectorFetchError("ign_wfs_geometry_invalid")
            geometries.append(parsed)
        footprint = unary_union(geometries).envelope
        response_hash = _sha256(_canonical_json(document))
        validators = _response_validators(response.headers)
        source_digest = _sha256(response.request_url.encode("utf-8"))
        artifact = ExternalArtifactInput(
            collection_id=context.collection_id,
            external_product_id=f"ign-wfs:{type_name}:{source_digest[:32]}",
            source_url=response.request_url,
            content_hash=response_hash,
            etag=_safe_response_header(response.headers, "etag"),
            retrieved_at=_utc(self._clock(), field="connector_clock"),
            native_crs="EPSG:4326",
            footprint_geojson=mapping(footprint),
            quality_flags={
                "feature_count": len(features),
                "provider_interface": "IGN_GEOPLATEFORME_WFS_2_0_0",
                "query_context_end_at": _iso_z(window.end_at),
                "query_context_start_at": _iso_z(window.start_at),
                "type_name": type_name,
                **validators,
            },
            status=ExternalArtifactStatus.PROVISIONAL,
        )
        return _result(
            (artifact,),
            query_digest=query_digest,
            payload=response,
            content_hash=response_hash,
        )


class MeteoFranceSynopConnector:
    """Météo-France DPObs SYNOP observations for explicit incident station IDs."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        access_token: SecretStr | str | None,
        timeout_seconds: float = 20.0,
        maximum_response_bytes: int = 8_388_608,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds_out_of_range")
        if not 65_536 <= maximum_response_bytes <= 33_554_432:
            raise ValueError("maximum_response_bytes_out_of_range")
        self._client = client
        self._access_token = (
            access_token
            if isinstance(access_token, SecretStr)
            else SecretStr(access_token)
            if access_token is not None
            else None
        )
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._clock = clock

    def fetch(self, context: ExternalCollectionContext) -> ExternalFetchResult:
        _require_semantic_role(context, frozenset({ExternalSemanticRole.WEATHER_OBSERVATION}))
        bbox = _bbox(context)
        window = _query_window(context, maximum=timedelta(days=5))
        api_start_at = window.start_at.replace(minute=0, second=0, microsecond=0)
        api_end_at = window.end_at.replace(minute=0, second=0, microsecond=0)
        if api_end_at < window.end_at:
            api_end_at += timedelta(hours=1)
        if api_end_at - api_start_at > timedelta(days=5):
            _raise_configuration("observed_window_too_large_after_api_alignment")
        reference_point = _reference_point(context)
        maximum_station_distance_m = _configuration_int(
            context.plan_configuration,
            "station_max_distance_m",
            default=50_000,
            minimum=1_000,
            maximum=100_000,
        )
        if self._access_token is None:
            _raise_configuration("meteo_france_access_token_required")
        token = self._access_token.get_secret_value().strip()
        if len(token) < 16 or len(token) > 8_192 or "\r" in token or "\n" in token:
            _raise_configuration("meteo_france_access_token_invalid")
        station_ids_raw = context.plan_configuration.get("station_ids")
        if (
            not isinstance(station_ids_raw, list)
            or not station_ids_raw
            or len(station_ids_raw) > 100
        ):
            _raise_configuration("station_ids_required")
        station_ids: list[str] = []
        for raw_station_id in station_ids_raw:
            if not isinstance(raw_station_id, str) or not _SYNOP_STATION_RE.fullmatch(
                raw_station_id
            ):
                _raise_configuration("station_ids_invalid")
            station_ids.append(raw_station_id)
        if len(station_ids) != len(set(station_ids)):
            _raise_configuration("station_ids_invalid")
        station_ids.sort()

        params = {
            "format": "json",
            "id_station": ",".join(station_ids),
            "date_debut": _iso_z(api_start_at),
            "date_fin": _iso_z(api_end_at),
        }
        query_identity = {
            "bbox": bbox,
            "endpoint": _METEO_FRANCE_SYNOPTIC_URL,
            **params,
        }
        query_digest = _sha256(_canonical_json(query_identity))
        headers = {
            "Accept": "application/geo+json, application/json",
            "Authorization": f"Bearer {token}",
        }
        headers.update(_conditional_headers(context, query_digest=query_digest))
        response = _bounded_request(
            self._client,
            method="GET",
            url=_METEO_FRANCE_SYNOPTIC_URL,
            exact_host=_METEO_FRANCE_HOST,
            exact_path="/public/DPObs/v1/synop",
            timeout_seconds=self._timeout_seconds,
            maximum_bytes=self._maximum_response_bytes,
            headers=headers,
            params=params,
        )
        if response.status_code == 304:
            return _result(
                (),
                query_digest=query_digest,
                payload=response,
                content_hash=None,
                unchanged_watermark=context.watermark,
            )
        _require_json_content_type(response)
        document = _decode_json(response.body)
        records = _weather_records(document)
        if not records:
            return _result(
                (),
                query_digest=query_digest,
                payload=response,
                content_hash=_sha256(_canonical_json(document)),
            )

        observed_times: list[datetime] = []
        points: list[tuple[float, float]] = []
        stations_with_observations: set[str] = set()
        for properties, point in records:
            station_id = _weather_station_id(properties)
            if station_id not in station_ids:
                raise OfficialConnectorFetchError("meteo_france_station_id_invalid")
            stations_with_observations.add(station_id)
            observation_time = _weather_observation_time(properties)
            if not api_start_at <= observation_time <= api_end_at:
                raise OfficialConnectorFetchError("meteo_france_observation_outside_query_window")
            observed_times.append(observation_time)
            if point is None:
                raise OfficialConnectorFetchError("meteo_france_station_location_missing")
            if _haversine_metres(reference_point, point) > maximum_station_distance_m:
                raise OfficialConnectorFetchError("meteo_france_station_outside_incident_aoi")
            points.append(point)
        unique_points = sorted(set(points))
        footprint: dict[str, object]
        if len(unique_points) == 1:
            footprint = {"type": "Point", "coordinates": list(unique_points[0])}
        else:
            footprint = {
                "type": "MultiPoint",
                "coordinates": [list(point) for point in unique_points],
            }

        response_hash = _sha256(_canonical_json(document))
        validators = _response_validators(response.headers)
        source_digest = _sha256(response.request_url.encode("utf-8"))
        artifact = ExternalArtifactInput(
            collection_id=context.collection_id,
            external_product_id=f"meteo-france-dpobs-synop:{source_digest[:40]}",
            source_url=response.request_url,
            content_hash=response_hash,
            etag=_safe_response_header(response.headers, "etag"),
            acquisition_start_at=min(observed_times),
            acquisition_end_at=max(observed_times),
            retrieved_at=_utc(self._clock(), field="connector_clock"),
            native_crs="EPSG:4326",
            footprint_geojson=footprint,
            quality_flags={
                "observation_count": len(records),
                "provider_interface": "METEO_FRANCE_DPOBS_V1_SYNOPTIC",
                "station_count": len(unique_points),
                "station_ids": station_ids,
                "station_max_distance_m": maximum_station_distance_m,
                "stations_with_observations": sorted(stations_with_observations),
                **validators,
            },
            status=ExternalArtifactStatus.PROVISIONAL,
        )
        return _result(
            (artifact,),
            query_digest=query_digest,
            payload=response,
            content_hash=response_hash,
        )


def _weather_records(
    document: object,
) -> list[tuple[Mapping[str, object], tuple[float, float] | None]]:
    if isinstance(document, list):
        source_records: list[object] = document
    elif isinstance(document, dict) and document.get("type") == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list):
            raise OfficialConnectorFetchError("meteo_france_response_invalid")
        source_records = features
    elif isinstance(document, dict):
        source_records = [document]
    else:
        raise OfficialConnectorFetchError("meteo_france_response_invalid")
    if len(source_records) > 50_000:
        raise OfficialConnectorFetchError("meteo_france_response_record_limit_exceeded")

    records: list[tuple[Mapping[str, object], tuple[float, float] | None]] = []
    for raw_record in source_records:
        if not isinstance(raw_record, dict):
            raise OfficialConnectorFetchError("meteo_france_response_invalid")
        if raw_record.get("type") == "Feature":
            properties = raw_record.get("properties")
            geometry = raw_record.get("geometry")
            if not isinstance(properties, dict) or not isinstance(geometry, dict):
                raise OfficialConnectorFetchError("meteo_france_response_invalid")
            if geometry.get("type") != "Point":
                raise OfficialConnectorFetchError("meteo_france_station_location_invalid")
            point = _point_coordinates(geometry.get("coordinates"))
            records.append((properties, point))
            continue
        record_point = _record_point(raw_record)
        records.append((raw_record, record_point))
    return records


def _record_point(record: Mapping[str, object]) -> tuple[float, float] | None:
    latitude = record.get("lat", record.get("latitude"))
    longitude = record.get("lon", record.get("longitude"))
    if latitude is None and longitude is None:
        return None
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, int | float)
        or isinstance(longitude, bool)
        or not isinstance(longitude, int | float)
    ):
        raise OfficialConnectorFetchError("meteo_france_station_location_invalid")
    lat = float(latitude)
    lon = float(longitude)
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise OfficialConnectorFetchError("meteo_france_station_location_invalid")
    return lon, lat


def _point_coordinates(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) < 2:
        raise OfficialConnectorFetchError("meteo_france_station_location_invalid")
    longitude, latitude = value[0], value[1]
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, int | float)
        or isinstance(longitude, bool)
        or not isinstance(longitude, int | float)
    ):
        raise OfficialConnectorFetchError("meteo_france_station_location_invalid")
    lat = float(latitude)
    lon = float(longitude)
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise OfficialConnectorFetchError("meteo_france_station_location_invalid")
    return lon, lat


def _weather_observation_time(properties: Mapping[str, object]) -> datetime:
    for field in ("validity_time", "reference_time", "datetime", "time"):
        value = properties.get(field)
        if value is not None:
            return _parse_datetime(value, field="weather_observation_time")
    raise OfficialConnectorFetchError("meteo_france_observation_time_missing")


def _weather_station_id(properties: Mapping[str, object]) -> str:
    station_id = properties.get("id_station")
    if isinstance(station_id, str) and _SYNOP_STATION_RE.fullmatch(station_id):
        return station_id
    wmo_id = properties.get("geo_id_wmo")
    if isinstance(wmo_id, bool):
        raise OfficialConnectorFetchError("meteo_france_station_id_invalid")
    if isinstance(wmo_id, int) and 0 <= wmo_id <= 99_999:
        return str(wmo_id).zfill(5)
    if isinstance(wmo_id, str) and re.fullmatch(r"[0-9]{1,5}", wmo_id):
        return wmo_id.zfill(5)
    raise OfficialConnectorFetchError("meteo_france_station_id_invalid")
