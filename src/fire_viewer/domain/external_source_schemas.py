from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from fire_viewer.domain.enums import ExternalArtifactStatus, ExternalSemanticRole
from fire_viewer.domain.geometry_contract import validate_geojson_geometry


class ExternalStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExternalProviderInput(ExternalStrictModel):
    provider_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,94}[a-z0-9]$")
    display_name: str = Field(min_length=2, max_length=255)
    allowed_domains: list[str] = Field(min_length=1, max_length=50)
    authentication_kind: Literal["none", "api_key", "oauth2", "signed_request"]
    attribution: str = Field(min_length=1, max_length=1_000)
    enabled: bool = False

    @field_validator("allowed_domains")
    @classmethod
    def unique_domains(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().casefold().rstrip(".") for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_domains must be unique")
        return normalized


class ExternalCollectionInput(ExternalStrictModel):
    provider_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,94}[a-z0-9]$")
    collection_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,126}[a-z0-9]$")
    product_name: str = Field(min_length=2, max_length=255)
    sensor: str | None = Field(default=None, min_length=1, max_length=128)
    platform: str | None = Field(default=None, min_length=1, max_length=128)
    license: str = Field(min_length=1, max_length=1_000)
    cadence_seconds: int | None = Field(default=None, gt=0, le=31_536_000)
    semantic_role: ExternalSemanticRole
    configuration: dict[str, Any] = Field(default_factory=dict)


class ExternalArtifactInput(ExternalStrictModel):
    collection_id: int = Field(gt=0)
    external_product_id: str = Field(min_length=1, max_length=512)
    source_url: str = Field(min_length=8, max_length=2_048)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    etag: str | None = Field(default=None, max_length=512)
    processing_baseline: str | None = Field(default=None, max_length=128)
    acquisition_granule_id: str | None = Field(default=None, min_length=1, max_length=512)
    acquisition_pixel_id: str | None = Field(default=None, min_length=1, max_length=255)
    acquisition_start_at: AwareDatetime | None = None
    acquisition_end_at: AwareDatetime | None = None
    effective_start_at: AwareDatetime | None = None
    effective_end_at: AwareDatetime | None = None
    processed_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    forecast_run_at: AwareDatetime | None = None
    forecast_valid_at: AwareDatetime | None = None
    native_crs: str | None = Field(default=None, min_length=3, max_length=128)
    footprint_geojson: dict[str, Any] | None = None
    resolution_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    quality_flags: dict[str, Any] = Field(default_factory=dict)
    license: str | None = Field(default=None, min_length=1, max_length=1_000)
    attribution: str | None = Field(default=None, min_length=1, max_length=1_000)
    status: ExternalArtifactStatus = ExternalArtifactStatus.PROVISIONAL

    @model_validator(mode="after")
    def ordered_intervals(self) -> ExternalArtifactInput:
        for start, end, label in (
            (self.acquisition_start_at, self.acquisition_end_at, "acquisition"),
            (self.effective_start_at, self.effective_end_at, "effective"),
        ):
            if end is not None and start is None:
                raise ValueError(f"{label}_start_at is required when {label}_end_at is supplied")
            if start is not None and end is not None and end < start:
                raise ValueError(f"{label}_end_at must not precede {label}_start_at")
        if (
            self.forecast_run_at is not None
            and self.forecast_valid_at is not None
            and self.forecast_valid_at < self.forecast_run_at
        ):
            raise ValueError("forecast_valid_at must not precede forecast_run_at")
        if (self.footprint_geojson is None) != (self.native_crs is None):
            raise ValueError("footprint_geojson and native_crs must be supplied together")
        return self


class IncidentSourcePlanInput(ExternalStrictModel):
    incident_id: int | None = Field(default=None, gt=0)
    incident_candidate_id: int | None = Field(default=None, gt=0)
    collection_id: int = Field(gt=0)
    cadence_seconds: int | None = Field(default=None, gt=0, le=31_536_000)
    enabled: bool = True
    configuration: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_target(self) -> IncidentSourcePlanInput:
        if (self.incident_id is None) == (self.incident_candidate_id is None):
            raise ValueError("exactly one incident target is required")
        return self


class ExternalClaimInput(ExternalStrictModel):
    artifact_revision_id: str = Field(min_length=3, max_length=96)
    incident_id: str | None = Field(default=None, pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    assertion_kind: Literal[
        "incident_declaration",
        "official_status",
        "active_fire_point",
        "visible_front",
        "smoke_origin",
        "thermal_hotspot",
        "burned_area",
        "weather_observation",
        "weather_forecast",
        "geospatial_reference",
        "historical_record",
        "simulation_output",
    ]
    assertion_payload: dict[str, Any] = Field(default_factory=dict)
    geometry_geojson: dict[str, Any] | None = None
    horizontal_accuracy_m: float | None = Field(default=None, gt=0, le=1_000_000)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("geometry_geojson")
    @classmethod
    def geometry_is_wgs84(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return validate_geojson_geometry(value)

    @model_validator(mode="after")
    def declaration_has_location(self) -> ExternalClaimInput:
        if self.geometry_geojson is not None and self.horizontal_accuracy_m is None:
            raise ValueError("claim geometry requires horizontal_accuracy_m")
        if self.horizontal_accuracy_m is not None and self.geometry_geojson is None:
            raise ValueError("horizontal_accuracy_m requires claim geometry")
        place_name = self.assertion_payload.get("place_name")
        if self.assertion_kind == "incident_declaration" and self.geometry_geojson is None and not (
            isinstance(place_name, str) and place_name.strip()
        ):
            raise ValueError("incident declarations require geometry or a place_name")
        return self
