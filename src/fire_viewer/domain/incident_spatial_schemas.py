from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fire_viewer.domain.enums import ActiveFireZoneReviewState


class StrictSpatialReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminIncidentScene(StrictSpatialReviewModel):
    asset_url: str | None = None
    asset_version: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    package_id: str | None = None
    zone_id: str | None = None
    zone_revision: int | None = Field(default=None, ge=1)
    package_state: str | None = None
    publication_id: str | None = None
    publication_state: str | None = None
    publication_active: bool = False
    catalog_url: str | None = None
    files: dict[str, str] = Field(default_factory=dict, max_length=2_000)
    origin_wgs84: tuple[float, float, float]
    local_frame: Literal["ENU"] = "ENU"
    gltf_profile: Literal["gltf-eun-negz-metric-v1"] = "gltf-eun-negz-metric-v1"


class AdminIncidentSpatialMarker(StrictSpatialReviewModel):
    marker_id: str
    source_kind: Literal["observation", "agent_media"]
    marker_type: str
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    altitude_m: float | None = None
    horizontal_accuracy_m: float | None = Field(default=None, gt=0)
    geometry_origin: str
    review_state: str
    observed_at: datetime | None = None
    spatial_display_allowed: bool
    gltf_position: tuple[float, float, float] | None = None
    version: int = Field(ge=1)


class AdminActiveFireZoneRevision(StrictSpatialReviewModel):
    zone_revision_id: str
    revision: int = Field(ge=1)
    valid_at: datetime
    geometry_geojson: dict[str, Any]
    gltf_polygons: list[list[list[tuple[float, float, float]]]] = Field(default_factory=list)
    geometry_origin: str
    analysis_id: str | None = None
    supporting_marker_ids: list[str] = Field(default_factory=list)
    source_revision_ids: list[str] = Field(default_factory=list)
    review_state: ActiveFireZoneReviewState
    supersedes_zone_revision_id: str | None = None
    reason: str
    created_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = None
    created_at: datetime


class AdminAgentReviewPackage(StrictSpatialReviewModel):
    review_id: str
    batch_id: str
    state: str
    reason_codes: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None


class AdminIncidentMapCapture(StrictSpatialReviewModel):
    capture_id: str
    zone_revision_id: str
    local_date: date
    captured_at: datetime
    image_url: str
    width_px: int = Field(ge=640)
    height_px: int = Field(ge=360)


class AdminIncidentSpatialReviewWorkspace(StrictSpatialReviewModel):
    fire_id: str
    episode_id: str
    scene: AdminIncidentScene | None = None
    markers: list[AdminIncidentSpatialMarker] = Field(default_factory=list, max_length=2_000)
    zone_revisions: list[AdminActiveFireZoneRevision] = Field(default_factory=list, max_length=500)
    map_gallery: list[AdminIncidentMapCapture] = Field(default_factory=list, max_length=1_000)
    agent_reviews: list[AdminAgentReviewPackage] = Field(default_factory=list, max_length=200)


class IncidentMapCaptureUploadGrantRequest(StrictSpatialReviewModel):
    zone_revision_id: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(gt=0, le=8 * 1_024 * 1_024)
    media_type: Literal["image/jpeg", "image/png"]


class IncidentMapCaptureObject(StrictSpatialReviewModel):
    path: Literal["capture.jpg", "capture.png"]
    pathname: str = Field(min_length=1, max_length=2_048)
    size_bytes: int = Field(gt=0, le=8 * 1_024 * 1_024)
    content_type: Literal["image/jpeg", "image/png"]


class IncidentMapCaptureFinalizeRequest(StrictSpatialReviewModel):
    upload_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    zone_revision_id: str = Field(min_length=1, max_length=128)
    object: IncidentMapCaptureObject


class IncidentMarkerReviewRequest(StrictSpatialReviewModel):
    action: Literal["validate", "reject"]
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)


class ActiveFireZoneRevisionCreateRequest(StrictSpatialReviewModel):
    expected_latest_revision: int = Field(ge=0)
    valid_at: datetime
    analysis_id: str | None = Field(default=None, min_length=1, max_length=128)
    geometry_geojson: dict[str, Any]
    supporting_marker_ids: list[str] = Field(default_factory=list, max_length=2_000)
    geometry_origin: Literal["HUMAN_AUTHORED", "SATELLITE_PRODUCT"] = "HUMAN_AUTHORED"
    reason: str = Field(min_length=10, max_length=500)


class ActiveFireZoneMergeRequest(StrictSpatialReviewModel):
    expected_latest_revision: int = Field(ge=0)
    source_revision_ids: list[str] = Field(min_length=2, max_length=50)
    valid_at: datetime
    supporting_marker_ids: list[str] = Field(default_factory=list, max_length=2_000)
    reason: str = Field(min_length=10, max_length=500)


class ActiveFireZoneReviewRequest(StrictSpatialReviewModel):
    action: Literal["approve", "reject"]
    expected_state: Literal["DRAFT", "READY_FOR_PUBLICATION"]
    reason: str = Field(min_length=10, max_length=500)


class AgentReviewResolutionRequest(StrictSpatialReviewModel):
    action: Literal["approve", "reject"]
    expected_state: Literal["PENDING", "IN_REVIEW"]
    reason: str = Field(min_length=10, max_length=500)


class IncidentGltfPickRequest(StrictSpatialReviewModel):
    gltf_position: tuple[float, float, float]


class IncidentGltfPickResponse(StrictSpatialReviewModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    altitude_m: float
