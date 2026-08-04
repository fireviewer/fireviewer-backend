from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from fire_viewer.core.config import Settings
from fire_viewer.domain.enums import ExternalSemanticRole
from fire_viewer.services.external_source_scheduler import ExternalCollectionContext
from fire_viewer.services.official_connectors import (
    CdseStacConnector,
    IgnGeoplateformeWfsConnector,
    MeteoFranceSynopConnector,
    OfficialConnectorAuthenticationError,
    OfficialConnectorConfigurationError,
    OfficialConnectorFetchError,
    build_official_connector_registry,
    official_source_definitions,
)

_NOW = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)


def _context(
    *,
    role: ExternalSemanticRole,
    collection_configuration: dict[str, object] | None = None,
    plan_configuration: dict[str, object] | None = None,
    watermark: str | None = None,
) -> ExternalCollectionContext:
    return ExternalCollectionContext(
        plan_id="ISP_TEST",
        provider_key="provider-test",
        collection_id=17,
        collection_key="collection-test",
        semantic_role=role,
        target_kind="incident",
        target_public_id="FR-2026-TEST",
        bbox_wgs84=(5.0, 43.0, 5.5, 43.5),
        reference_point_wgs84=(5.25, 43.25),
        observed_start_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        observed_end_at=datetime(2026, 8, 3, 14, 0, tzinfo=UTC),
        watermark=watermark,
        collection_configuration=collection_configuration or {},
        plan_configuration=plan_configuration or {},
    )


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _stac_document(*, next_link: bool = False) -> dict[str, object]:
    links: list[dict[str, str]] = []
    if next_link:
        links.append(
            {
                "rel": "next",
                "href": "https://stac.dataspace.copernicus.eu/v1/search?page=2",
            }
        )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "stac_version": "1.1.0",
                "id": "S3A_SL_2_FRP____20260803T120000_TEST",
                "collection": "sentinel-3-sl-2-frp-nrt",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [5.1, 43.1],
                            [5.4, 43.1],
                            [5.4, 43.4],
                            [5.1, 43.4],
                            [5.1, 43.1],
                        ]
                    ],
                },
                "properties": {
                    "datetime": "2026-08-03T12:05:00Z",
                    "created": "2026-08-03T13:00:00Z",
                    "gsd": 1_000,
                    "platform": "sentinel-3a",
                    "instruments": ["slstr"],
                    "processing:version": "003",
                },
            }
        ],
        "links": links,
    }


def test_cdse_stac_builds_bounded_incident_query_and_artifact() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json=_stac_document(),
            headers={
                "Content-Type": "application/geo+json",
                "ETag": '"cdse-revision-1"',
                "Last-Modified": "Mon, 03 Aug 2026 13:00:00 GMT",
            },
        )

    with _client(handler) as client:
        result = CdseStacConnector(client, clock=lambda: _NOW).fetch(
            _context(
                role=ExternalSemanticRole.SENSOR_DETECTION,
                collection_configuration={
                    "stac_collection_id": "sentinel-3-sl-2-frp-nrt",
                    "maximum_items": 25,
                },
            )
        )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://stac.dataspace.copernicus.eu/v1/search"
    body = httpx.Response(200, content=captured["body"]).json()
    assert body["collections"] == ["sentinel-3-sl-2-frp-nrt"]
    assert body["datetime"] == "2026-08-03T12:00:00Z/2026-08-03T14:00:00Z"
    assert body["limit"] == 25
    assert body["intersects"]["coordinates"][0][0] == [5.0, 43.0]
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.collection_id == 17
    assert artifact.external_product_id.startswith("S3A_SL_2_FRP")
    assert artifact.source_url.startswith("https://stac.dataspace.copernicus.eu/v1/collections/")
    assert artifact.acquisition_start_at == datetime(2026, 8, 3, 12, 5, tzinfo=UTC)
    assert artifact.resolution_m == 1_000
    assert artifact.etag is None
    assert artifact.quality_flags["response_etag"] == '"cdse-revision-1"'
    assert artifact.quality_flags["catalog"] == "CDSE_STAC_1_1"
    assert result.watermark.startswith("http-v1.")


def test_cdse_stac_reuses_validators_only_for_same_query() -> None:
    first_calls = 0

    def first_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal first_calls
        first_calls += 1
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": [], "links": []},
            headers={
                "Content-Type": "application/json",
                "ETag": '"same-query"',
                "Last-Modified": "Mon, 03 Aug 2026 13:00:00 GMT",
            },
        )

    base_context = _context(
        role=ExternalSemanticRole.RAW_EARTH_OBSERVATION,
        collection_configuration={"stac_collection_id": "sentinel-2-l2a"},
    )
    with _client(first_handler) as client:
        first = CdseStacConnector(client, clock=lambda: _NOW).fetch(base_context)
    assert first_calls == 1

    def second_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"same-query"'
        assert request.headers["if-modified-since"] == "Mon, 03 Aug 2026 13:00:00 GMT"
        return httpx.Response(304)

    with _client(second_handler) as client:
        second = CdseStacConnector(client, clock=lambda: _NOW).fetch(
            _context(
                role=ExternalSemanticRole.RAW_EARTH_OBSERVATION,
                collection_configuration={"stac_collection_id": "sentinel-2-l2a"},
                watermark=first.watermark,
            )
        )
    assert second.artifacts == ()
    assert second.watermark == first.watermark


def test_cdse_stac_fails_closed_on_truncated_window() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_stac_document(next_link=True),
            headers={"Content-Type": "application/json"},
        )

    with (
        _client(handler) as client,
        pytest.raises(OfficialConnectorFetchError, match="cdse_stac_result_window_truncated"),
    ):
        CdseStacConnector(client).fetch(
            _context(
                role=ExternalSemanticRole.SENSOR_DETECTION,
                collection_configuration={"stac_collection_id": "sentinel-3-sl-2-frp-nrt"},
            )
        )


def test_ign_wfs_uses_exact_endpoint_and_incident_bbox() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "numberMatched": 1,
                "numberReturned": 1,
                "features": [
                    {
                        "type": "Feature",
                        "id": "reference.1",
                        "geometry": {"type": "Point", "coordinates": [5.2, 43.2]},
                        "properties": {"name": "Reference"},
                    }
                ],
            },
            headers={"Content-Type": "application/geo+json", "ETag": '"ign-1"'},
        )

    with _client(handler) as client:
        result = IgnGeoplateformeWfsConnector(client, clock=lambda: _NOW).fetch(
            _context(
                role=ExternalSemanticRole.GEOSPATIAL_REFERENCE,
                collection_configuration={
                    "type_name": "ADMINEXPRESS-COG-CARTO.LATEST:departement",
                    "maximum_features": 50,
                },
            )
        )

    parsed = httpx.URL(captured["url"])
    assert parsed.host == "data.geopf.fr"
    assert parsed.path == "/wfs/ows"
    assert parsed.params["SERVICE"] == "WFS"
    assert parsed.params["REQUEST"] == "GetFeature"
    assert parsed.params["BBOX"] == "5.0,43.0,5.5,43.5,EPSG:4326"
    assert "apikey" not in parsed.params
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.external_product_id.startswith("ign-wfs:")
    assert artifact.native_crs == "EPSG:4326"
    assert artifact.acquisition_start_at is None
    assert artifact.quality_flags["feature_count"] == 1


def test_ign_wfs_rejects_incomplete_or_non_json_response() -> None:
    context = _context(role=ExternalSemanticRole.GEOSPATIAL_REFERENCE)
    with (
        _client(lambda _request: httpx.Response(500)) as client,
        pytest.raises(OfficialConnectorConfigurationError, match="type_name_required"),
    ):
        IgnGeoplateformeWfsConnector(client).fetch(context)

    def html_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )

    with (
        _client(html_handler) as client,
        pytest.raises(OfficialConnectorFetchError, match="official_response_content_type_invalid"),
    ):
        IgnGeoplateformeWfsConnector(client).fetch(
            _context(
                role=ExternalSemanticRole.GEOSPATIAL_REFERENCE,
                collection_configuration={"type_name": "reference:layer"},
            )
        )


def test_meteo_france_token_stays_in_header_and_output_is_spatially_bounded() -> None:
    secret = "server-side-meteo-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        assert secret not in str(request.url)
        assert request.url.host == "public-api.meteofrance.fr"
        assert request.url.params["id_station"] == "07650,07651"
        assert request.url.params["date_debut"] == "2026-08-03T12:00:00Z"
        assert request.url.params["date_fin"] == "2026-08-03T14:00:00Z"
        return httpx.Response(
            200,
            json=[
                {
                    "id_station": "07650",
                    "lon": 5.2,
                    "lat": 43.2,
                    "validity_time": "2026-08-03T12:00:00Z",
                    "t": 302.15,
                },
                {
                    "geo_id_wmo": 7651,
                    "lon": 5.3,
                    "lat": 43.3,
                    "validity_time": "2026-08-03T13:00:00Z",
                    "ff": 8.1,
                },
            ],
            headers={"Content-Type": "application/json", "ETag": '"meteo-1"'},
        )

    with _client(handler) as client:
        result = MeteoFranceSynopConnector(
            client,
            access_token=SecretStr(secret),
            clock=lambda: _NOW,
        ).fetch(
            _context(
                role=ExternalSemanticRole.WEATHER_OBSERVATION,
                plan_configuration={"station_ids": ["07651", "07650"]},
            )
        )

    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert secret not in artifact.source_url
    assert artifact.acquisition_start_at == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    assert artifact.acquisition_end_at == datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    assert artifact.footprint_geojson == {
        "type": "MultiPoint",
        "coordinates": [[5.2, 43.2], [5.3, 43.3]],
    }
    assert artifact.quality_flags["station_ids"] == ["07650", "07651"]


def test_meteo_france_requires_credential_and_rejects_station_outside_aoi() -> None:
    context = _context(
        role=ExternalSemanticRole.WEATHER_OBSERVATION,
        plan_configuration={"station_ids": ["07650"]},
    )
    with (
        _client(lambda _request: httpx.Response(500)) as client,
        pytest.raises(
            OfficialConnectorConfigurationError, match="meteo_france_access_token_required"
        ),
    ):
        MeteoFranceSynopConnector(client, access_token=None).fetch(context)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id_station": "07650",
                    "lon": 7.0,
                    "lat": 45.0,
                    "validity_time": "2026-08-03T12:00:00Z",
                }
            ],
            headers={"Content-Type": "application/json"},
        )

    with (
        _client(handler) as client,
        pytest.raises(
            OfficialConnectorFetchError, match="meteo_france_station_outside_incident_aoi"
        ),
    ):
        MeteoFranceSynopConnector(client, access_token="long-enough-token-value").fetch(context)


def test_http_failures_are_bounded_and_do_not_expose_credentials() -> None:
    secret = "server-side-meteo-token-value"

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"credential={secret}")

    with (
        _client(unauthorized) as client,
        pytest.raises(
            OfficialConnectorAuthenticationError,
            match=r"^official_source_authentication_failed$",
        ) as error,
    ):
        MeteoFranceSynopConnector(client, access_token=secret).fetch(
            _context(
                role=ExternalSemanticRole.WEATHER_OBSERVATION,
                plan_configuration={"station_ids": ["07650"]},
            )
        )
    assert secret not in str(error.value)

    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "9000000"},
        )

    with (
        _client(oversized) as client,
        pytest.raises(OfficialConnectorFetchError, match="official_response_too_large"),
    ):
        CdseStacConnector(client).fetch(
            _context(
                role=ExternalSemanticRole.RAW_EARTH_OBSERVATION,
                collection_configuration={"stac_collection_id": "sentinel-2-l2a"},
            )
        )


def test_missing_time_wrong_role_and_invalid_watermark_fail_before_network() -> None:
    network_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    missing_time = _context(
        role=ExternalSemanticRole.RAW_EARTH_OBSERVATION,
        collection_configuration={"stac_collection_id": "sentinel-2-l2a"},
    )
    missing_time = replace(missing_time, observed_start_at=None)
    with (
        _client(handler) as client,
        pytest.raises(OfficialConnectorConfigurationError, match="observed_start_at_required"),
    ):
        CdseStacConnector(client).fetch(missing_time)

    with (
        _client(handler) as client,
        pytest.raises(OfficialConnectorConfigurationError, match="external_semantic_role_mismatch"),
    ):
        CdseStacConnector(client).fetch(
            _context(
                role=ExternalSemanticRole.WEATHER_FORECAST,
                collection_configuration={"stac_collection_id": "sentinel-2-l2a"},
            )
        )

    with (
        _client(handler) as client,
        pytest.raises(OfficialConnectorConfigurationError, match="official_watermark_invalid"),
    ):
        CdseStacConnector(client).fetch(
            _context(
                role=ExternalSemanticRole.RAW_EARTH_OBSERVATION,
                collection_configuration={"stac_collection_id": "sentinel-2-l2a"},
                watermark="not-an-official-watermark",
            )
        )
    assert network_calls == 0


def test_registry_builder_is_explicit_and_settings_reject_secret_like_route_data() -> None:
    empty = Settings(_env_file=None)
    empty_registry = build_official_connector_registry(empty)
    assert (
        empty_registry.resolve(
            provider_key="copernicus-data-space",
            collection_key="sentinel-2-l2a",
        )
        is None
    )

    configured = Settings(
        _env_file=None,
        official_connectors_enabled=True,
        official_connector_collections={
            "copernicus-data-space/sentinel-2-l2a": {
                "enabled": True,
                "kind": "cdse_stac",
            },
            "ign-geoplateforme/reference-layer": {
                "enabled": True,
                "kind": "ign_geoplateforme_wfs",
            },
        },
    )
    with _client(lambda _request: httpx.Response(500)) as client:
        registry = build_official_connector_registry(configured, client=client)
        assert (
            registry.resolve(
                provider_key="copernicus-data-space",
                collection_key="sentinel-2-l2a",
            )
            is not None
        )
        assert (
            registry.resolve(
                provider_key="ign-geoplateforme",
                collection_key="reference-layer",
            )
            is not None
        )

    with pytest.raises(ValidationError, match="accepts only enabled and kind"):
        Settings(
            _env_file=None,
            official_connector_collections={
                "meteo-france/dpobs-synop": {
                    "enabled": True,
                    "kind": "meteo_france_synop",
                    "api_key": "must-not-live-here",
                }
            },
        )
    with pytest.raises(ValidationError, match="meteo_france_access_token is required"):
        Settings(
            _env_file=None,
            official_connector_collections={
                "meteo-france/dpobs-synop": {
                    "enabled": True,
                    "kind": "meteo_france_synop",
                }
            },
        )


def test_official_source_definitions_are_disabled_non_secret_and_exact() -> None:
    definitions = official_source_definitions()
    assert {definition.provider.provider_key for definition in definitions} == {
        "copernicus-data-space",
        "ign-geoplateforme",
        "meteo-france",
    }
    collection_keys = {
        collection.collection_key
        for definition in definitions
        for collection in definition.collections
    }
    assert "sentinel-3-sl-2-frp-nrt" in collection_keys
    assert "dpobs-synop" in collection_keys
    for definition in definitions:
        assert definition.provider.enabled is False
        assert definition.provider.attribution
        assert all(
            "*" not in domain and "/" not in domain
            for domain in definition.provider.allowed_domains
        )
        for collection in definition.collections:
            assert collection.license
            serialized_configuration = str(collection.configuration).casefold()
            assert "token" not in serialized_configuration
            assert "secret" not in serialized_configuration
