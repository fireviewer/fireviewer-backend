from __future__ import annotations

import math
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from fire_viewer.core.ids import SOURCE_KEY_RE, TERRITORY_CODE_RE
from fire_viewer.domain.enums import (
    AssetLod,
    EvidenceSpatialMode,
    IncidentStatus,
    MatchDecision,
    PublicReportCategory,
    PublicReportState,
    PublicVisibility,
    ReviewResolutionAction,
    SourceTrust,
    SourceType,
    SpatialPackageFileKind,
    SpatialPackageState,
    VerificationState,
    ZoneContributionState,
    ZoneInformationState,
    ZonePublicationState,
    ZoneUploadState,
    ZoneVisibility,
)
from fire_viewer.domain.public_visibility import (
    PUBLIC_LOCATION_STATUSES,
    VIEWER_ASSET_STATUSES,
    WITHHELD_MANIFEST_STATUSES,
)

StrictText = Annotated[str, StringConstraints(strip_whitespace=True)]
ManifestLongitude = Annotated[float, Field(ge=-180.0, le=180.0, allow_inf_nan=False)]
ManifestLatitude = Annotated[float, Field(ge=-90.0, le=90.0, allow_inf_nan=False)]
ManifestEllipsoidHeight = Annotated[float, Field(allow_inf_nan=False)]
ManifestMetersPerUnit = Annotated[
    float,
    Field(ge=0.01, le=0.01, allow_inf_nan=False),
    WithJsonSchema({"const": 0.01, "type": "number"}),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceInput(StrictModel):
    id: str = Field(min_length=3, max_length=128)
    type: SourceType
    trust: SourceTrust = SourceTrust.UNVERIFIED

    @field_validator("id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not SOURCE_KEY_RE.fullmatch(value):
            raise ValueError("source.id contains unsupported characters")
        return value


class PointGeometryInput(StrictModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]
    horizontal_uncertainty_m: float = Field(gt=0.0, le=50_000.0)
    altitude_m: float | None = Field(default=None, ge=-500.0, le=10_000.0)
    vertical_datum: str | None = Field(default=None, min_length=2, max_length=128)

    @model_validator(mode="after")
    def validate_coordinates(self) -> PointGeometryInput:
        longitude, latitude = self.coordinates
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if self.altitude_m is not None and not self.vertical_datum:
            raise ValueError("vertical_datum is required when altitude_m is supplied")
        return self


class EvidenceInput(StrictModel):
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    license: str = Field(min_length=1, max_length=255)
    external_reference: str | None = Field(default=None, max_length=512)


class DetectionContext(StrictModel):
    territory_code: str = Field(min_length=2, max_length=3)
    toponyms: list[str] = Field(default_factory=list, max_length=10)
    canonical_name: str | None = Field(default=None, min_length=2, max_length=255)

    @field_validator("territory_code")
    @classmethod
    def validate_territory_code(cls, value: str) -> str:
        normalized = value.upper()
        if not TERRITORY_CODE_RE.fullmatch(normalized):
            raise ValueError("territory_code must contain 2 or 3 uppercase alphanumerics")
        return normalized

    @field_validator("toponyms")
    @classmethod
    def validate_toponyms(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in values:
            stripped = item.strip()
            if not 2 <= len(stripped) <= 128:
                raise ValueError("each toponym must contain between 2 and 128 characters")
            if stripped not in normalized:
                normalized.append(stripped)
        return normalized


class DetectionRequest(StrictModel):
    source: SourceInput
    observed_at: AwareDatetime
    received_at: AwareDatetime
    geometry: PointGeometryInput
    evidence: EvidenceInput
    context: DetectionContext


class DetectionResponse(StrictModel):
    observation_id: str
    decision: MatchDecision
    fire_id: str | None = None
    episode_id: str | None = None
    proposed_fire_id: str | None = None
    proposed_episode_id: str | None = None
    score: float | None = None
    margin_to_second_candidate: float | None = None
    factors: dict[str, float] = Field(default_factory=dict)
    distance_m: float | None = None
    review_reasons: list[str] = Field(default_factory=list)
    public_confirmation: Literal["pending", "corroborated", "not_applicable"]
    policy_id: str
    trace_id: str


class EpisodeSummary(StrictModel):
    episode_id: str
    ordinal: int
    status: IncidentStatus
    verification_state: VerificationState
    corroborating_source_count: int = Field(ge=0)
    evidence_basis_at: datetime | None = None
    estimated_area_ha: float | None = Field(default=None, ge=0)
    evacuation_established: bool = False
    model_generation_eligible: bool = False
    review_required: bool
    started_at: datetime
    last_observed_at: datetime
    validated_at: datetime | None = None
    ended_at: datetime | None = None
    is_current: bool
    version: int


class IncidentPublicResponse(StrictModel):
    fire_id: str
    canonical_name: str | None = None
    visibility: PublicVisibility
    status: IncidentStatus
    current_episode_id: str
    location: PointGeometryInput | None
    public_note: str | None = None
    last_observed_at: datetime
    created_at: datetime
    episodes: list[EpisodeSummary]


class IncidentDiscoveryItem(StrictModel):
    """Minimal public listing item used by search and recent-incident pages."""

    fire_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    canonical_name: str = Field(min_length=1, max_length=255)
    status: IncidentStatus
    verification: Literal["verified", "corroborated"]
    last_observed_at: datetime


class IncidentDiscoveryResponse(StrictModel):
    """Bounded, text-only public discovery response; never a map index."""

    schema_version: Literal["1.0"] = "1.0"
    incidents: list[IncidentDiscoveryItem] = Field(default_factory=list, max_length=20)


class ManifestStatus(StrictModel):
    code: IncidentStatus
    validated_at: datetime | None = None
    review_required: bool


class ManifestAsset(StrictModel):
    asset_id: str
    version: int
    url: str
    sha256: str
    size_bytes: int
    lod: AssetLod


class ManifestSpatialSceneFile(StrictModel):
    file_id: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=500)
    kind: SpatialPackageFileKind
    url: str = Field(min_length=1, max_length=1_000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    media_type: str = Field(min_length=1, max_length=100)


class ManifestSpatialScene(StrictModel):
    package_id: str = Field(min_length=3, max_length=96)
    catalog_url: str = Field(min_length=1, max_length=1_000)
    # The manifest is the first public response for an incident.  Keep it
    # deliberately small: spatial file URLs are disclosed only by the explicit
    # scene bootstrap endpoint after the visitor asks to open the 3D view.
    files: list[ManifestSpatialSceneFile] = Field(default_factory=list, max_length=2_000)


class ManifestFrame(StrictModel):
    origin_wgs84: tuple[ManifestLongitude, ManifestLatitude, ManifestEllipsoidHeight]
    local_frame: Literal["ENU"]
    meters_per_unit: ManifestMetersPerUnit
    vertical_datum: Literal["EPSG:4979"]

    @field_validator("origin_wgs84")
    @classmethod
    def validate_origin_wgs84(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("origin_wgs84 values must be finite")
        return value


class ManifestFreshness(StrictModel):
    incident_at: datetime
    terrain_source_year: int | None = None
    generated_at: datetime | None = None


class ViewerManifest(StrictModel):
    schema_version: Literal["2.0"]
    fire_id: str
    episode_id: str
    status: ManifestStatus
    location: PointGeometryInput | None
    asset: ManifestAsset | None = None
    scene: ManifestSpatialScene | None = None
    frame: ManifestFrame | None = None
    freshness: ManifestFreshness
    model_state: Literal["available", "not_available", "withheld"]
    public_notice: str

    @model_validator(mode="after")
    def validate_public_projection(self) -> ViewerManifest:
        if self.model_state == "available" and (
            self.location is None
            or self.frame is None
            or (self.asset is None and self.scene is None)
        ):
            raise ValueError("available manifests require location, frame, and an asset or scene")
        if self.model_state == "available" and self.status.code not in VIEWER_ASSET_STATUSES:
            raise ValueError("available manifests require an active public lifecycle status")
        if self.model_state == "not_available" and (
            self.location is None
            or self.asset is not None
            or self.scene is not None
            or self.frame is not None
        ):
            raise ValueError("not_available manifests require location without asset or frame")
        if self.model_state == "not_available" and self.status.code not in PUBLIC_LOCATION_STATUSES:
            raise ValueError("not_available manifests require a public lifecycle status")
        if self.model_state == "withheld" and any(
            value is not None for value in (self.location, self.asset, self.scene, self.frame)
        ):
            raise ValueError("withheld manifests must not include location, asset, or frame")
        if self.model_state == "withheld" and self.status.code not in WITHHELD_MANIFEST_STATUSES:
            raise ValueError("withheld manifests require a non-public lifecycle status")
        return self


class PublicObservationSummary(StrictModel):
    observation_id: str
    episode_id: str
    type: SourceType
    observed_at: datetime
    received_at: datetime
    uncertainty_m: float = Field(gt=0)
    area_label: str | None = Field(default=None, max_length=255)
    verification_state: Literal[
        VerificationState.CORROBORATED,
        VerificationState.VERIFIED,
    ]
    spatial_mode: EvidenceSpatialMode


class PublicEvidenceProjection(StrictModel):
    projection_id: str = Field(min_length=1, max_length=96)
    episode_id: str
    kind: Literal["validated_marker", "generalized_area"]
    verification_state: Literal[
        VerificationState.CORROBORATED,
        VerificationState.VERIFIED,
    ]
    center: PointGeometryInput
    radius_m: float = Field(gt=0, le=100_000)
    label: str = Field(min_length=1, max_length=255)
    observed_at: datetime | None = None


class PublicSourceSummary(StrictModel):
    source_id: str
    type: SourceType
    name: str | None = Field(default=None, max_length=255)
    trust: SourceTrust
    license: str | None = Field(default=None, max_length=255)
    external_reference: str | None = Field(default=None, max_length=2048)
    transformations: list[str] = Field(default_factory=list, max_length=20)
    observation_count: int = Field(ge=1)


class PublicOfficialResource(StrictModel):
    resource_id: str = Field(min_length=1, max_length=96)
    kind: Literal["safety", "press", "official_update", "authority"]
    title: str = Field(min_length=1, max_length=255)
    publisher: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    published_at: datetime | None = None
    episode_id: str | None = None


GalleryMediaKind = Literal["image", "video"]
GalleryItemState = Literal["PROPOSED", "PUBLISHED", "REJECTED", "RETIRED"]


class PublicIncidentGalleryItem(StrictModel):
    """Editorial media explicitly published by an administrator, never an agent-media view."""

    gallery_item_id: str = Field(min_length=1, max_length=96)
    title: str = Field(min_length=1, max_length=255)
    caption: str | None = Field(default=None, max_length=1000)
    alt_text: str = Field(min_length=1, max_length=500)
    media_url: str | None = Field(default=None, min_length=9, max_length=2048, pattern=r"^https://")
    media_kind: GalleryMediaKind
    credit: str | None = Field(default=None, max_length=255)
    license_label: str | None = Field(default=None, max_length=255)
    captured_at: datetime | None = None
    published_at: datetime | None = None
    episode_id: str | None = None


OperationalInformationKind = Literal[
    "affected_place",
    "evacuated_people",
    "mobilized_personnel",
    "mobilized_vehicles",
    "road_status",
    "access_status",
    "shelter",
    "public_service",
    "utility",
    "other",
]
OperationalAuthorityKind = Literal["mairie", "prefecture", "police"]
OperationalInformationState = Literal["PROPOSED", "PUBLISHED", "REJECTED", "RETIRED"]


class PublicOperationalInformation(StrictModel):
    """A reviewed, source-linked operational fact safe for the public bulletin."""

    information_id: str = Field(min_length=1, max_length=96)
    kind: OperationalInformationKind
    title: str = Field(min_length=1, max_length=120)
    value_text: str | None = Field(default=None, max_length=500)
    value_number: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=64)
    locality: str | None = Field(default=None, max_length=255)
    authority_kind: OperationalAuthorityKind
    authority_name: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    effective_at: datetime | None = None
    published_at: datetime | None = None
    episode_id: str | None = None


class PublicTimelineEvent(StrictModel):
    occurred_at: datetime
    kind: Literal["incident", "episode", "observation", "model", "operational"]
    label: str = Field(min_length=1, max_length=255)
    episode_id: str | None = None


class PublicModelMetadata(StrictModel):
    state: Literal["available", "not_available", "withheld"]
    version: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, gt=0)
    lod: AssetLod | None = None
    terrain_source_year: int | None = Field(default=None, ge=1900, le=2200)
    generated_at: datetime | None = None
    public_download_available: bool
    limitations: list[str] = Field(default_factory=list, max_length=12)


class PublicDownload(StrictModel):
    id: Literal["incident-json", "timeline-csv"]
    label: str = Field(min_length=1, max_length=120)
    media_type: Literal["application/json", "text/csv"]
    url: str = Field(min_length=1, max_length=255)


class PublicActiveFireZone(StrictModel):
    zone_revision_id: str = Field(min_length=1, max_length=96)
    zone_kind: Literal["active", "burned"]
    revision: int = Field(ge=1)
    valid_at: datetime
    analysis_id: str | None = Field(default=None, min_length=1, max_length=128)
    geometry_geojson: dict[str, object]


class PublicIncidentMapCapture(StrictModel):
    capture_id: str = Field(min_length=1, max_length=128)
    zone_revision_id: str = Field(min_length=1, max_length=128)
    local_date: date
    captured_at: datetime
    image_url: str = Field(min_length=1, max_length=255)
    width_px: int = Field(ge=640)
    height_px: int = Field(ge=360)


class PublicAgentEvidenceReference(StrictModel):
    """Public provenance for one reviewed agent result, without private media access."""

    evidence_kind: str = Field(min_length=1, max_length=32)
    evidence_id: str = Field(min_length=1, max_length=128)
    source_annotation_id: str | None = Field(default=None, max_length=128)
    source_reference_url: str | None = Field(
        default=None, min_length=9, max_length=2048, pattern=r"^https://"
    )
    license_identifier: str | None = Field(default=None, max_length=128)


class PublicAgentFact(StrictModel):
    fact_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    fact_key: str = Field(min_length=1, max_length=128)
    as_of: datetime
    certainty: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=1000)
    value_number: float | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    unit: str | None = Field(default=None, max_length=64)
    evidence: PublicAgentEvidenceReference


class PublicAgentSpatialResult(StrictModel):
    proposal_id: str = Field(min_length=1, max_length=128)
    kind: Literal[
        "active_fire_point",
        "smoke_origin_point",
        "visible_fire_front",
        "probable_activity_envelope",
        "burned_area_polygon",
    ]
    observed_at: datetime
    geometry_geojson: dict[str, object]
    geometry_origin: str = Field(min_length=1, max_length=64)
    horizontal_accuracy_m: float = Field(gt=0)
    evidence: PublicAgentEvidenceReference


class PublicAgentSituationReport(StrictModel):
    report_revision_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=255)
    body_markdown: str = Field(min_length=1)
    reviewed_at: datetime


class PublicDailyIntelligence(StrictModel):
    """Reviewed agent output exposed only after the campaign day is published."""

    analysis_id: str = Field(min_length=1, max_length=128)
    episode_id: str = Field(min_length=1, max_length=16)
    local_date: date
    published_at: datetime
    report: PublicAgentSituationReport
    facts: list[PublicAgentFact] = Field(default_factory=list, max_length=2_000)
    spatial_results: list[PublicAgentSpatialResult] = Field(default_factory=list, max_length=2_000)


class PublicIncidentView(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    fire_id: str
    canonical_name: str | None = Field(default=None, max_length=255)
    public_note: str | None = Field(default=None, max_length=500)
    status: IncidentStatus
    verification: Literal["verified", "corroborated", "review_required"]
    freshness_at: datetime
    last_human_validation_at: datetime | None = None
    participatory_observation_count: int | None = Field(default=None, ge=1)
    participatory_published_count: int | None = Field(default=None, ge=1)
    participatory_received_count: int | None = Field(default=None, ge=1)
    location: PointGeometryInput | None = None
    facts: list[str] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    episodes: list[EpisodeSummary] = Field(default_factory=list)
    observations: list[PublicObservationSummary] = Field(default_factory=list)
    evidence_projections: list[PublicEvidenceProjection] = Field(default_factory=list)
    active_fire_zone: PublicActiveFireZone | None = None
    active_fire_zones: list[PublicActiveFireZone] = Field(default_factory=list, max_length=500)
    burned_area_zones: list[PublicActiveFireZone] = Field(default_factory=list, max_length=500)
    daily_intelligence: list[PublicDailyIntelligence] = Field(default_factory=list, max_length=500)
    map_gallery: list[PublicIncidentMapCapture] = Field(default_factory=list, max_length=1_000)
    gallery: list[PublicIncidentGalleryItem] = Field(default_factory=list, max_length=120)
    official_resources: list[PublicOfficialResource] = Field(default_factory=list, max_length=80)
    operational_information: list[PublicOperationalInformation] = Field(
        default_factory=list, max_length=120
    )
    sources: list[PublicSourceSummary] = Field(default_factory=list)
    timeline: list[PublicTimelineEvent] = Field(default_factory=list)
    model: PublicModelMetadata
    downloads: list[PublicDownload] = Field(default_factory=list, max_length=2)


class PublicIncidentReportRequest(StrictModel):
    category: PublicReportCategory
    message: str = Field(min_length=12, max_length=2_000)


class PublicIncidentReportReceipt(StrictModel):
    receipt_id: str
    status: Literal["received"] = "received"
    submitted_at: datetime
    replayed: bool = False


class PublicIncidentReport(StrictModel):
    report_id: str
    fire_id: str
    category: PublicReportCategory
    message: str
    state: PublicReportState
    submitted_at: datetime
    reviewed_at: datetime | None = None
    closure_reason: str | None = None
    version: int = Field(ge=1)


class AdminPublicReportListResponse(StrictModel):
    reports: list[PublicIncidentReport] = Field(default_factory=list)


class AdminPublicReportReviewRequest(StrictModel):
    state: Literal[PublicReportState.CORRECTED, PublicReportState.REJECTED]
    reason: str = Field(min_length=10, max_length=500)
    expected_version: int = Field(ge=1)


class AdminPublicReportEnvelope(StrictModel):
    report: PublicIncidentReport
    trace_id: str


class AdminIncidentSummary(StrictModel):
    fire_id: str
    canonical_name: str | None = Field(default=None, max_length=255)
    territory_code: str
    visibility: PublicVisibility
    current_episode_id: str
    status: IncidentStatus
    verification_state: VerificationState
    corroborating_source_count: int = Field(ge=0)
    estimated_area_ha: float | None = Field(default=None, ge=0)
    evacuation_established: bool
    model_generation_eligible: bool
    review_required: bool
    last_observed_at: datetime
    pending_observation_count: int = Field(ge=0)
    version: int = Field(ge=1)


class AdminIncidentCreateRequest(StrictModel):
    """Minimal human input for a private incident created by an administrator."""

    territory_code: str = Field(min_length=2, max_length=3)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    canonical_name: str | None = Field(default=None, min_length=2, max_length=255)

    @field_validator("territory_code")
    @classmethod
    def validate_territory_code(cls, value: str) -> str:
        normalized = value.upper()
        if not TERRITORY_CODE_RE.fullmatch(normalized):
            raise ValueError("territory_code must contain 2 or 3 uppercase alphanumerics")
        return normalized


class AdminIncidentCreateResponse(StrictModel):
    fire_id: str
    episode_id: str
    canonical_name: str | None = None
    territory_code: str
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    status: IncidentStatus
    verification_state: VerificationState
    visibility: PublicVisibility
    created_at: datetime


class AdminIncidentObservation(StrictModel):
    observation_id: str
    source_key: str
    observed_at: datetime
    verification_state: VerificationState
    attached_episode_id: str | None = None
    proposed_fire_id: str | None = None
    proposed_episode_id: str | None = None
    match_score: float | None = None
    review_reasons: list[str] = Field(default_factory=list)
    version: int = Field(ge=1)


class AdminIncidentSource(StrictModel):
    source_key: str
    type: SourceType
    trust: SourceTrust
    enabled: bool
    display_name: str | None = Field(default=None, max_length=255)
    public_display_name: str | None = Field(default=None, max_length=255)


class AdminIncidentModel(StrictModel):
    revision: int = Field(ge=1)
    episode_id: str
    is_current: bool
    asset_id: str | None = None
    asset_state: str | None = None
    asset_version: int | None = Field(default=None, ge=1)
    lod: str | None = None
    size_bytes: int | None = Field(default=None, gt=0)
    generated_at: datetime | None = None
    spatial_zone_id: str | None = None
    spatial_zone_revision: int | None = Field(default=None, ge=1)
    asset_spatial_zone_id: str | None = None
    asset_spatial_zone_revision: int | None = Field(default=None, ge=1)


class AdminIncidentAuditEvent(StrictModel):
    event_id: str
    occurred_at: datetime
    action: str
    target_type: str
    target_id: str
    actor_type: str
    actor_id: str
    reason: str


class AdminIncidentDetail(AdminIncidentSummary):
    episodes: list[EpisodeSummary] = Field(default_factory=list)
    observations: list[AdminIncidentObservation] = Field(default_factory=list)
    sources: list[AdminIncidentSource] = Field(default_factory=list)
    models: list[AdminIncidentModel] = Field(default_factory=list)
    audit: list[AdminIncidentAuditEvent] = Field(default_factory=list)


class AdminIncidentObservationWorkspaceItem(StrictModel):
    """Private observation projection used to resolve one incident dossier."""

    observation_id: str
    source_key: str
    source_type: SourceType
    observed_at: datetime
    received_at: datetime
    longitude: float
    latitude: float
    horizontal_uncertainty_m: float = Field(gt=0)
    verification_state: VerificationState
    match_decision: str
    attached_episode_id: str | None = None
    proposed_fire_id: str | None = None
    proposed_episode_id: str | None = None
    match_score: float | None = None
    margin_to_second_candidate: float | None = None
    review_reasons: list[str] = Field(default_factory=list)
    external_reference: str | None = Field(default=None, max_length=512)
    evidence_license: str = Field(max_length=255)
    version: int = Field(ge=1)


class AdminIncidentObservationsResponse(StrictModel):
    fire_id: str
    observations: list[AdminIncidentObservationWorkspaceItem] = Field(
        default_factory=list, max_length=500
    )


class AdminIncidentSourceWorkspaceItem(StrictModel):
    source_key: str
    type: SourceType
    trust: SourceTrust
    enabled: bool
    display_name: str | None = Field(default=None, max_length=255)
    public_display_name: str | None = Field(default=None, max_length=255)
    public_license: str | None = Field(default=None, max_length=255)
    public_reference_url: str | None = Field(default=None, max_length=2048)
    public_transformations: list[str] = Field(default_factory=list, max_length=20)
    observation_count: int = Field(ge=1)


class AdminIncidentMediaReference(StrictModel):
    """Evidence metadata only; media binaries and contributor identity stay private."""

    observation_id: str
    source_key: str
    source_type: SourceType
    observed_at: datetime
    received_at: datetime
    verification_state: VerificationState
    evidence_hash: str = Field(max_length=80)
    evidence_license: str = Field(max_length=255)
    external_reference: str | None = Field(default=None, max_length=512)


class AdminIncidentSourcesMediaResponse(StrictModel):
    fire_id: str
    sources: list[AdminIncidentSourceWorkspaceItem] = Field(default_factory=list, max_length=200)
    media_references: list[AdminIncidentMediaReference] = Field(
        default_factory=list, max_length=500
    )


class AdminIncidentOfficialResource(StrictModel):
    resource_id: str = Field(min_length=1, max_length=96)
    episode_id: str | None = None
    kind: Literal["safety", "press", "official_update", "authority"]
    title: str = Field(min_length=1, max_length=255)
    publisher: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    published_at: datetime | None = None
    state: Literal["PROPOSED", "PUBLISHED", "REJECTED", "RETIRED"]
    source_reference_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    proposal_reason: str = Field(min_length=1, max_length=1000)
    proposed_by: str = Field(min_length=1, max_length=255)
    proposed_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = Field(default=None, max_length=500)
    version: int = Field(ge=1)


class AdminIncidentOfficialResourcesResponse(StrictModel):
    fire_id: str
    resources: list[AdminIncidentOfficialResource] = Field(default_factory=list, max_length=200)


class AdminIncidentGalleryItem(StrictModel):
    gallery_item_id: str = Field(min_length=1, max_length=96)
    episode_id: str | None = None
    title: str = Field(min_length=1, max_length=255)
    caption: str | None = Field(default=None, max_length=1000)
    alt_text: str = Field(min_length=1, max_length=500)
    media_url: str | None = Field(default=None, min_length=9, max_length=2048, pattern=r"^https://")
    media_kind: GalleryMediaKind
    credit: str | None = Field(default=None, max_length=255)
    license_label: str | None = Field(default=None, max_length=255)
    captured_at: datetime | None = None
    published_at: datetime | None = None
    state: GalleryItemState
    source_reference_url: str | None = Field(
        default=None, min_length=9, max_length=2048, pattern=r"^https://"
    )
    proposal_reason: str = Field(min_length=1, max_length=1000)
    proposed_by: str = Field(min_length=1, max_length=255)
    proposed_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = Field(default=None, max_length=500)
    version: int = Field(ge=1)


class AdminIncidentGalleryResponse(StrictModel):
    fire_id: str
    items: list[AdminIncidentGalleryItem] = Field(default_factory=list, max_length=300)


class AdminIncidentGalleryCreateRequest(StrictModel):
    episode_id: str | None = Field(default=None, min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=255)
    caption: str | None = Field(default=None, max_length=1000)
    alt_text: str = Field(min_length=1, max_length=500)
    media_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    media_kind: GalleryMediaKind
    credit: str | None = Field(default=None, max_length=255)
    license_label: str | None = Field(default=None, max_length=255)
    captured_at: datetime | None = None
    source_reference_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    proposal_reason: str = Field(min_length=10, max_length=1000)


class AdminIncidentGalleryReviewRequest(StrictModel):
    action: Literal["publish", "reject", "retire"]
    reason: str = Field(min_length=4, max_length=500)
    expected_version: int = Field(ge=1)


class AdminIncidentOperationalInformation(StrictModel):
    information_id: str = Field(min_length=1, max_length=96)
    episode_id: str | None = None
    kind: OperationalInformationKind
    title: str = Field(min_length=1, max_length=120)
    value_text: str | None = Field(default=None, max_length=500)
    value_number: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=64)
    locality: str | None = Field(default=None, max_length=255)
    authority_kind: OperationalAuthorityKind
    authority_name: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    effective_at: datetime | None = None
    published_at: datetime | None = None
    state: OperationalInformationState
    source_reference_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    proposal_reason: str = Field(min_length=1, max_length=1000)
    proposed_by: str = Field(min_length=1, max_length=255)
    proposed_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = Field(default=None, max_length=500)
    version: int = Field(ge=1)


class AdminIncidentOperationalInformationResponse(StrictModel):
    fire_id: str
    information: list[AdminIncidentOperationalInformation] = Field(
        default_factory=list, max_length=300
    )


class AdminOperationalInformationCreateRequest(StrictModel):
    episode_id: str | None = Field(default=None, min_length=1, max_length=16)
    kind: OperationalInformationKind
    title: str = Field(min_length=1, max_length=120)
    value_text: str | None = Field(default=None, max_length=500)
    value_number: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=64)
    locality: str | None = Field(default=None, max_length=255)
    authority_kind: OperationalAuthorityKind
    authority_name: str = Field(min_length=1, max_length=255)
    source_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    source_reference_url: str = Field(min_length=9, max_length=2048, pattern=r"^https://")
    effective_at: datetime | None = None
    published_at: datetime | None = None
    proposal_reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def validate_value(self) -> AdminOperationalInformationCreateRequest:
        if self.value_text is None and self.value_number is None:
            raise ValueError("value_text or value_number is required")
        if self.value_number is not None and self.unit is None:
            raise ValueError("unit is required for a numeric operational value")
        return self


class AdminOperationalInformationReviewRequest(StrictModel):
    action: Literal["publish", "reject", "retire"]
    reason: str = Field(min_length=4, max_length=500)
    expected_version: int = Field(ge=1)


class AdminOfficialResourceReviewRequest(StrictModel):
    action: Literal["publish", "reject", "retire"]
    reason: str = Field(min_length=4, max_length=500)
    expected_version: int = Field(ge=1)


class AdminIncidentModelWorkspaceItem(StrictModel):
    revision: int = Field(ge=1)
    episode_id: str
    is_current: bool
    created_at: datetime
    reason: str = Field(max_length=500)
    asset_id: str | None = None
    asset_state: str | None = None
    asset_version: int | None = Field(default=None, ge=1)
    lod: str | None = None
    sha256: str | None = Field(default=None, max_length=64)
    size_bytes: int | None = Field(default=None, gt=0)
    terrain_source_year: int | None = Field(default=None, ge=1900, le=3000)
    generated_at: datetime | None = None
    published_at: datetime | None = None
    superseded_at: datetime | None = None
    spatial_zone_id: str | None = None
    spatial_zone_revision: int | None = Field(default=None, ge=1)
    asset_spatial_zone_id: str | None = None
    asset_spatial_zone_revision: int | None = Field(default=None, ge=1)


class AdminIncidentPipelineJob(StrictModel):
    job_id: str
    kind: str
    state: str
    episode_id: str
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class AdminIncidentModelsPipelineResponse(StrictModel):
    fire_id: str
    models: list[AdminIncidentModelWorkspaceItem] = Field(default_factory=list, max_length=200)
    jobs: list[AdminIncidentPipelineJob] = Field(default_factory=list, max_length=500)


class AdminIncidentListResponse(StrictModel):
    incidents: list[AdminIncidentSummary] = Field(default_factory=list, max_length=200)


class AdminWorkQueueObservation(StrictModel):
    observation_id: str
    source_key: str
    observed_at: datetime
    longitude: float
    latitude: float
    horizontal_uncertainty_m: float = Field(gt=0)
    verification_state: VerificationState
    proposed_fire_id: str | None = None
    proposed_episode_id: str | None = None
    proposed_episode_status: IncidentStatus | None = None
    match_score: float | None = None
    review_reasons: list[str] = Field(default_factory=list)
    version: int = Field(ge=1)


class AdminWorkQueueIncident(StrictModel):
    fire_id: str
    episode_id: str
    status: IncidentStatus
    verification_state: VerificationState
    last_observed_at: datetime
    version: int = Field(ge=1)


class AdminWorkQueueSummary(StrictModel):
    """Canonical, private totals for the operator decision inbox."""

    total: int = Field(ge=0)
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)


class AdminWorkQueueItem(StrictModel):
    """Safe projection of one human decision, deliberately without source payloads."""

    item_id: str = Field(min_length=3, max_length=192)
    target_id: str = Field(min_length=1, max_length=128)
    category: Literal[
        "observation",
        "incident",
        "public_report",
        "public_contribution",
        "agent_review",
        "agent_fact_proposal",
        "agent_spatial_proposal",
        "agent_report_revision",
        "spatial_marker",
        "spatial_revision",
        "official_resource",
        "operational_information",
        "gallery",
        "spatial_package",
        "publication",
    ]
    priority: Literal["critical", "high", "normal"]
    state: str = Field(min_length=1, max_length=64)
    fire_id: str | None = Field(default=None, max_length=32)
    episode_id: str | None = Field(default=None, max_length=16)
    zone_id: str | None = Field(default=None, max_length=64)
    zone_revision: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=255)
    detail: str | None = Field(default=None, max_length=500)
    action_at: datetime


class AdminWorkQueuePage(StrictModel):
    limit: int = Field(ge=1, le=100)
    returned: int = Field(ge=0)
    next_cursor: str | None = Field(default=None, max_length=64)
    total_filtered: int = Field(ge=0)


class AdminWorkQueueResponse(StrictModel):
    """Transition-safe response: legacy lists stay private while the inbox uses items."""

    generated_at: datetime | None = None
    summary: AdminWorkQueueSummary | None = None
    items: list[AdminWorkQueueItem] = Field(default_factory=list, max_length=100)
    page: AdminWorkQueuePage | None = None
    observations: list[AdminWorkQueueObservation] = Field(default_factory=list, max_length=200)
    reports: list[PublicIncidentReport] = Field(default_factory=list, max_length=200)
    incidents: list[AdminWorkQueueIncident] = Field(default_factory=list, max_length=200)


class AdminGlobalAuditEvent(StrictModel):
    """Safe global audit projection: immutable metadata, never snapshots or payloads."""

    event_id: str
    occurred_at: datetime
    action: str
    target_type: str
    target_id: str
    actor_type: str
    actor_id: str
    reason: str
    trace_id: str


class AdminAuditListResponse(StrictModel):
    events: list[AdminGlobalAuditEvent] = Field(default_factory=list, max_length=200)


class AdminRoleDefinition(StrictModel):
    role: str
    description: str
    capabilities: list[str] = Field(default_factory=list)


class AdminRolesResponse(StrictModel):
    actor_id: str
    actor_type: str
    assigned_roles: list[str] = Field(default_factory=list)
    identity_management: str
    catalog: list[AdminRoleDefinition] = Field(default_factory=list)


class AdminSystemApplicationStatus(StrictModel):
    name: str
    version: str
    environment: str
    authentication_mode: str


class AdminSystemDatabaseStatus(StrictModel):
    dialect: str
    reachable: bool


class AdminSystemQueueStatus(StrictModel):
    jobs_active: int = Field(ge=0)
    jobs_quarantined: int = Field(ge=0)
    outbox_pending: int = Field(ge=0)
    outbox_with_error: int = Field(ge=0)
    reports_pending: int = Field(ge=0)


class AdminSystemAssetStatus(StrictModel):
    packages_draft: int = Field(ge=0)
    packages_verified: int = Field(ge=0)
    packages_previewable: int = Field(ge=0)
    packages_published: int = Field(ge=0)
    packages_withdrawn_or_revoked: int = Field(ge=0)


class AdminSystemStatus(StrictModel):
    checked_at: datetime
    application: AdminSystemApplicationStatus
    database: AdminSystemDatabaseStatus
    queues: AdminSystemQueueStatus
    assets: AdminSystemAssetStatus
    audit_event_count: int = Field(ge=0)
    worker_heartbeat: str


class AdminMatchingConfiguration(StrictModel):
    policy_id: str
    create_below: float
    auto_attach_above: float
    min_margin: float
    max_candidate_distance_m: float
    max_incident_uncertainty_m: float
    max_candidates: int


class AdminPublicConfiguration(StrictModel):
    report_rate_limit_per_day: int
    idempotency_retention_hours: int
    public_notice: str


class AdminStorageConfiguration(StrictModel):
    archive_max_bytes: int
    unpacked_max_bytes: int
    archive_max_files: int
    manifest_max_bytes: int


class AdminConfigurationResponse(StrictModel):
    environment: str
    authentication_mode: str
    identity_management: str
    matching: AdminMatchingConfiguration
    public: AdminPublicConfiguration
    storage: AdminStorageConfiguration


class AdminOperationalMapModel(StrictModel):
    """One controlled 3D representation available from an incident map panel."""

    profile: Literal["close", "local", "extended", "mobile", "desktop", "unspecified"]
    source: Literal["model_asset", "spatial_package"]
    state: str
    version: int | None = Field(default=None, ge=1)
    asset_id: str | None = None
    package_id: str | None = None
    package_file_id: int | None = Field(default=None, ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(gt=0)
    is_current: bool
    access_path: str | None = None


class AdminOperationalMapIncident(StrictModel):
    fire_id: str
    canonical_name: str | None = Field(default=None, max_length=255)
    territory_code: str
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    horizontal_uncertainty_m: float = Field(gt=0)
    status: IncidentStatus
    verification_state: VerificationState
    visibility: PublicVisibility
    current_episode_id: str
    last_observed_at: datetime
    review_required: bool
    pending_observation_count: int = Field(ge=0)
    spatial_zone_id: str | None = None
    spatial_zone_revision: int | None = Field(default=None, ge=1)
    current_package_id: str | None = None
    active_package_id: str | None = None
    models: list[AdminOperationalMapModel] = Field(default_factory=list, max_length=20)
    model_update_available: bool


class AdminOperationalMapSignal(StrictModel):
    """One persisted observation projected on the private anticipation map."""

    observation_id: str
    source_key: str
    source_type: SourceType
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    horizontal_uncertainty_m: float = Field(gt=0)
    territory_code: str
    canonical_name_hint: str | None = Field(default=None, max_length=255)
    observed_at: datetime
    received_at: datetime
    verification_state: VerificationState
    match_decision: MatchDecision
    state: Literal["pending", "attached"]
    proposed_fire_id: str | None = None
    attached_fire_id: str | None = None
    version: int = Field(ge=1)


class AdminOperationalMapSummary(StrictModel):
    total_incidents: int = Field(ge=0)
    active_incidents: int = Field(ge=0)
    monitoring_incidents: int = Field(ge=0)
    archived_incidents: int = Field(ge=0)
    incidents_requiring_review: int = Field(ge=0)
    pending_signals: int = Field(ge=0)
    attached_signals: int = Field(ge=0)
    incidents_with_models: int = Field(ge=0)
    model_updates_available: int = Field(ge=0)


class AdminOperationalMapResponse(StrictModel):
    generated_at: datetime
    coordinate_system: Literal["EPSG:4326"] = "EPSG:4326"
    summary: AdminOperationalMapSummary
    incidents: list[AdminOperationalMapIncident] = Field(default_factory=list, max_length=5_000)
    signals: list[AdminOperationalMapSignal] = Field(default_factory=list, max_length=5_000)


class AdminDashboardQueueSummary(StrictModel):
    total: int = Field(ge=0)
    critical: int = Field(ge=0)
    high: int = Field(ge=0)
    medium: int = Field(ge=0)
    observations_pending: int = Field(ge=0)
    reports_pending: int = Field(ge=0)
    incidents_requiring_review: int = Field(ge=0)
    jobs_quarantined: int = Field(ge=0)
    models_to_review: int = Field(ge=0)


class AdminDashboardPriorityItem(StrictModel):
    kind: Literal["observation", "report", "incident", "job", "model_package"]
    priority: Literal["critical", "high", "medium"]
    target_id: str
    fire_id: str | None = None
    title: str
    detail: str
    created_at: datetime


class AdminDashboardWatchIncident(StrictModel):
    fire_id: str
    canonical_name: str | None = Field(default=None, max_length=255)
    status: IncidentStatus
    verification_state: VerificationState
    last_observed_at: datetime
    review_required: bool
    pending_observation_count: int = Field(ge=0)
    model_update_available: bool


class AdminDashboardRecentPublication(StrictModel):
    publication_id: str
    zone_id: str
    package_id: str
    state: ZonePublicationState
    is_active: bool
    updated_at: datetime
    actor_id: str
    linked_fire_ids: list[str] = Field(default_factory=list, max_length=20)


class AdminDashboardResponse(StrictModel):
    generated_at: datetime
    queue: AdminDashboardQueueSummary
    priorities: list[AdminDashboardPriorityItem] = Field(default_factory=list, max_length=20)
    watchlist: list[AdminDashboardWatchIncident] = Field(default_factory=list, max_length=20)
    recent_publications: list[AdminDashboardRecentPublication] = Field(
        default_factory=list, max_length=10
    )
    map_summary: AdminOperationalMapSummary
    system: AdminSystemStatus


class AdminIncidentRepresentationAttachRequest(StrictModel):
    package_id: str = Field(min_length=3, max_length=96)
    expected_incident_version: int = Field(ge=1)
    primary_profile: Literal["close", "local", "extended", "mobile", "desktop"] = "local"
    reason: str = Field(min_length=10, max_length=500)


class AdminIncidentRepresentationAttachResponse(StrictModel):
    fire_id: str
    episode_id: str
    package_id: str
    manifest_revision: int = Field(ge=1)
    primary_asset_id: str | None = None
    model_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    incident_version: int = Field(ge=1)
    trace_id: str


class TransitionRequest(StrictModel):
    target_status: IncidentStatus
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    public_note: str | None = Field(default=None, max_length=500)
    validation_basis: str | None = Field(default=None, max_length=1_000)


class TransitionResponse(StrictModel):
    fire_id: str
    episode_id: str
    previous_status: IncidentStatus
    status: IncidentStatus
    version: int
    review_required: bool
    trace_id: str


class OperationalProfileRequest(StrictModel):
    expected_version: int = Field(ge=1)
    estimated_area_ha: float | None = Field(default=None, ge=0, le=10_000_000)
    evacuation_established: bool
    evacuation_basis: str | None = Field(default=None, max_length=1_000)
    reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def validate_evacuation_basis(self) -> OperationalProfileRequest:
        if self.evacuation_established and not self.evacuation_basis:
            raise ValueError("evacuation_basis is required when evacuation is established")
        if not self.evacuation_established and self.evacuation_basis is not None:
            raise ValueError("evacuation_basis is only valid for an established evacuation")
        return self


class OperationalProfileResponse(StrictModel):
    fire_id: str
    episode_id: str
    version: int
    estimated_area_ha: float | None = None
    evacuation_established: bool
    model_generation_eligible: bool
    eligibility_reasons: list[Literal["area_threshold", "established_evacuation"]]
    terrain_bake_request_id: str | None = None
    trace_id: str


class ReviewResolutionRequest(StrictModel):
    action: ReviewResolutionAction
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    target_fire_id: str | None = Field(default=None, max_length=32)
    publish_spatial_evidence: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> ReviewResolutionRequest:
        if self.action == ReviewResolutionAction.ATTACH and not self.target_fire_id:
            raise ValueError("target_fire_id is required for attach")
        if self.action != ReviewResolutionAction.ATTACH and self.target_fire_id is not None:
            raise ValueError("target_fire_id is only valid for attach")
        if self.action == ReviewResolutionAction.REJECT and self.publish_spatial_evidence:
            raise ValueError("rejected observations cannot publish spatial evidence")
        return self


class ReviewResolutionResponse(StrictModel):
    observation_id: str
    action: ReviewResolutionAction
    verification_state: VerificationState
    fire_id: str | None = None
    episode_id: str | None = None
    version: int
    trace_id: str


class SourceUpsertRequest(StrictModel):
    type: SourceType
    trust: SourceTrust
    display_name: str | None = Field(default=None, max_length=255)
    public_display_name: str | None = Field(default=None, max_length=255)
    public_license: str | None = Field(default=None, max_length=255)
    public_reference_url: str | None = Field(default=None, max_length=2048)
    public_transformations: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True
    ingest_token: str | None = Field(default=None, min_length=32, max_length=256)
    reason: str = Field(min_length=10, max_length=500)


class SourceResponse(StrictModel):
    id: str
    type: SourceType
    trust: SourceTrust
    display_name: str | None = None
    public_display_name: str | None = None
    public_license: str | None = None
    public_reference_url: str | None = None
    public_transformations: list[str] = Field(default_factory=list)
    enabled: bool
    credential_configured: bool
    created_at: datetime
    updated_at: datetime


class HealthResponse(StrictModel):
    status: Literal["ok"]
    version: str


class ReadinessResponse(StrictModel):
    status: Literal["ready"]
    database: Literal["ok"]
    schema_revision: str
    spatial_index: Literal["ok"]


class L93Position(StrictModel):
    easting: float = Field(allow_inf_nan=False)
    northing: float = Field(allow_inf_nan=False)


class AdminZoneCreateRequest(StrictModel):
    zone_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9-]*$")
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=4_000)
    bounds_l93_m: tuple[float, float, float, float]
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("bounds_l93_m")
    @classmethod
    def validate_bounds(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("bounds_l93_m values must be finite")
        if value[0] >= value[2] or value[1] >= value[3]:
            raise ValueError("bounds_l93_m must have increasing easting and northing")
        return value


class AdminZoneUpdateRequest(StrictModel):
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=4_000)
    bounds_l93_m: tuple[float, float, float, float]
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("bounds_l93_m")
    @classmethod
    def validate_bounds(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        return AdminZoneCreateRequest.validate_bounds(value)


class AdminZoneResponse(StrictModel):
    zone_id: str
    label: str
    description: str
    visibility: ZoneVisibility
    bounds_l93_m: tuple[float, float, float, float]
    created_at: datetime
    updated_at: datetime


class AdminZoneEnvelope(StrictModel):
    zone: AdminZoneResponse
    trace_id: str


class AdminZoneListResponse(StrictModel):
    zones: list[AdminZoneResponse]


class AdminZoneUploadResponse(StrictModel):
    upload_id: str
    file_name: str
    archive_sha256: str
    size_bytes: int
    state: ZoneUploadState
    created_at: datetime
    validation_summary: str


class AdminZoneUploadEnvelope(StrictModel):
    upload: AdminZoneUploadResponse
    trace_id: str


class AdminZoneInformationCreateRequest(StrictModel):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=8_000)
    category: str = Field(min_length=1, max_length=64)
    position_l93: tuple[float, float]
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("position_l93")
    @classmethod
    def validate_position(cls, value: tuple[float, float]) -> tuple[float, float]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("position_l93 values must be finite")
        return value


class AdminZoneInformationUpdateRequest(AdminZoneInformationCreateRequest):
    state: ZoneInformationState


class AdminZoneInformationResponse(StrictModel):
    information_id: str
    title: str
    body: str
    category: str
    position_l93: tuple[float, float]
    state: ZoneInformationState
    updated_at: datetime
    review_note: str | None


class AdminZoneInformationEnvelope(StrictModel):
    information: AdminZoneInformationResponse
    trace_id: str


class AdminZoneDetailResponse(StrictModel):
    zone: AdminZoneResponse
    uploads: list[AdminZoneUploadResponse]
    information: list[AdminZoneInformationResponse]


class AdminZoneRevisionCreateRequest(StrictModel):
    origin_lon: float = Field(ge=-5.5, le=10.0, allow_inf_nan=False)
    origin_lat: float = Field(ge=42.0, le=51.5, allow_inf_nan=False)
    source_orthometric_height_m: float = Field(allow_inf_nan=False)
    geoid_undulation_m: float = Field(allow_inf_nan=False)
    bounds_m: tuple[float, float, float, float, float, float]
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("bounds_m")
    @classmethod
    def validate_bounds_m(
        cls, value: tuple[float, float, float, float, float, float]
    ) -> tuple[float, float, float, float, float, float]:
        if not all(math.isfinite(component) for component in value):
            raise ValueError("bounds_m values must be finite")
        if value[0] >= value[1] or value[2] >= value[3] or value[4] >= value[5]:
            raise ValueError("bounds_m must have increasing east, north and up ranges")
        if not (
            value[0] <= 0 <= value[1] and value[2] <= 0 <= value[3] and value[4] <= 0 <= value[5]
        ):
            raise ValueError("bounds_m must include the local origin")
        return value


class AdminZoneRevisionSummary(StrictModel):
    revision: int = Field(ge=1)
    spatial_profile_version: str
    origin_l93_ngf: tuple[float, float, float] | None = None
    horizontal_crs: str | None = None
    vertical_crs: str | None = None
    ground_model: str | None = None
    ground_resolution_m: float | None = Field(default=None, gt=0)
    surface_height_reference: str | None = None
    origin_wgs84: tuple[float, float, float]
    local_frame: Literal["ENU"]
    meters_per_unit: float
    vertical_datum: str
    bounds_m: dict[str, tuple[float, float]]


class AdminZoneRevisionEnvelope(StrictModel):
    revision: AdminZoneRevisionSummary
    trace_id: str


class AdminSpatialPackageImportResponse(StrictModel):
    package_id: str
    state: SpatialPackageState
    upload_id: str
    object_count: int = Field(ge=3)
    total_size_bytes: int = Field(gt=0)
    asset_count: int = Field(ge=1)
    validation_summary: str


class AdminSpatialPackageImportEnvelope(StrictModel):
    package: AdminSpatialPackageImportResponse
    trace_id: str


class AdminBlobUploadGrantRequest(StrictModel):
    package_id: str = Field(min_length=3, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    file_count: int = Field(ge=3, le=100_000)
    total_size_bytes: int = Field(gt=0, le=5_497_558_138_880)


class AdminBlobUploadGrantResponse(StrictModel):
    upload_id: str
    pathname_prefix: str
    upload_grant: str
    expires_at: AwareDatetime
    maximum_file_size_bytes: int = Field(gt=0)
    allowed_content_types: list[str]


class AdminBlobUploadTokenPayload(StrictModel):
    pathname: str = Field(min_length=1, max_length=2_048)
    multipart: Literal[True]
    clientPayload: str | None = Field(default=None, max_length=4_096)


class AdminBlobUploadTokenRequest(StrictModel):
    type: Literal["blob.generate-client-token"]
    payload: AdminBlobUploadTokenPayload


class AdminBlobUploadTokenResponse(StrictModel):
    type: Literal["blob.generate-client-token"] = "blob.generate-client-token"
    clientToken: str


class AdminBlobObjectReference(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    pathname: str = Field(min_length=1, max_length=2_048)
    size_bytes: int = Field(gt=0)
    content_type: str = Field(min_length=1, max_length=128)


class AdminSpatialPackageFromBlobRequest(StrictModel):
    upload_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    package_id: str = Field(min_length=3, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    reason: str = Field(min_length=10, max_length=500)
    objects: list[AdminBlobObjectReference] = Field(min_length=3, max_length=100_000)


class AdminIncidentSpatialPackageFromBlobRequest(AdminSpatialPackageFromBlobRequest):
    zone_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9-]*$")
    revision: int = Field(ge=1)
    expected_incident_version: int = Field(ge=1)
    primary_profile: Literal["close", "local", "extended", "mobile", "desktop"] = "local"


class AdminIncidentSpatialPackageImportResponse(StrictModel):
    fire_id: str
    episode_id: str
    package_id: str
    package_state: SpatialPackageState
    zone_id: str
    revision: int = Field(ge=1)
    manifest_revision: int = Field(ge=1)
    incident_version: int = Field(ge=1)
    object_count: int = Field(ge=3)
    total_size_bytes: int = Field(gt=0)
    asset_count: int = Field(ge=1)
    trace_id: str


class AdminSpatialPackageRecoveryRequest(StrictModel):
    upload_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    package_id: str = Field(min_length=3, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    reason: str = Field(min_length=10, max_length=500)


class ZoneVisibilityRequest(StrictModel):
    visibility: Literal[ZoneVisibility.PUBLISHED, ZoneVisibility.HIDDEN]
    reason: str = Field(min_length=10, max_length=500)


class AdminSpatialPackageActionRequest(StrictModel):
    package_id: str = Field(min_length=3, max_length=96)
    reason: str = Field(min_length=10, max_length=500)


class AdminSpatialPackagePublicationRequest(AdminSpatialPackageActionRequest):
    zone_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9-]*$")
    revision: int = Field(ge=1)


class AdminSpatialPackagePublicationResponse(StrictModel):
    zone_id: str
    revision: int = Field(ge=1)
    package_id: str
    package_state: SpatialPackageState
    publication_id: str
    publication_state: ZonePublicationState
    is_active: bool


class AdminSpatialPackagePublicationEnvelope(StrictModel):
    publication: AdminSpatialPackagePublicationResponse
    trace_id: str


class AdminPublicationSummary(StrictModel):
    publication_id: str
    zone_id: str
    revision: int = Field(ge=1)
    package_id: str
    state: ZonePublicationState
    is_active: bool
    updated_at: datetime
    linked_fire_ids: list[str] = Field(default_factory=list)


class AdminPublicationListResponse(StrictModel):
    publications: list[AdminPublicationSummary]


class AdminPublicationActionRequest(StrictModel):
    reason: str = Field(min_length=10, max_length=500)
    confirm_publication_id: str = Field(min_length=3, max_length=96)


class AdminContributionResponse(StrictModel):
    contribution_id: str
    zone_id: str | None
    title: str
    body: str
    position_l93: tuple[float, float] | None
    state: ZoneContributionState
    submitted_at: datetime


class AdminContributionListResponse(StrictModel):
    contributions: list[AdminContributionResponse]


class AdminContributionReviewRequest(StrictModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=10, max_length=500)


class AdminContributionEnvelope(StrictModel):
    contribution: AdminContributionResponse
    trace_id: str


class PublicZoneResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    zone_id: str
    revision: int = Field(ge=1)
    label: str
    description: str | None
    catalog_url: str
    asset_base_url: str
    public_notice: str


class PublicZoneInformationItem(StrictModel):
    information_id: str
    title: str
    text: str
    category: str
    published_at: datetime


class PublicZoneInformationResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    zone_id: str
    revision: int = Field(ge=1)
    items: list[PublicZoneInformationItem]
    accepts_position_l93: bool


class PublicZoneContributionRequest(StrictModel):
    title: str = Field(min_length=3, max_length=160)
    text: str = Field(min_length=10, max_length=4_000)
    category: Literal["observation", "access", "vegetation", "infrastructure", "other"]
    position_l93: L93Position | None = None


class PublicZoneContributionReceipt(StrictModel):
    contribution_id: str
    status: Literal["received"] = "received"


class AdminPublicationCheck(StrictModel):
    code: str = Field(min_length=1, max_length=96)
    label: str = Field(min_length=1, max_length=255)
    satisfied: bool


class AdminIncidentPublicationDomain(StrictModel):
    domain: Literal["bulletin", "gallery", "spatial"]
    state: str = Field(min_length=1, max_length=64)
    preview_available: bool
    destination: str = Field(min_length=1, max_length=512)
    action: str | None = Field(default=None, max_length=128)
    checks: list[AdminPublicationCheck] = Field(default_factory=list, max_length=20)
    blockers: list[str] = Field(default_factory=list, max_length=20)


class AdminIncidentPublicationStatus(StrictModel):
    fire_id: str
    generated_at: datetime
    bulletin: AdminIncidentPublicationDomain
    gallery: AdminIncidentPublicationDomain
    spatial: AdminIncidentPublicationDomain


class AdminIncidentBulletinUpdateRequest(StrictModel):
    source_key: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    canonical_name: str | None = Field(default=None, min_length=1, max_length=255)
    public_note: str | None = Field(default=None, min_length=1, max_length=500)
    reason: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def require_change(self) -> AdminIncidentBulletinUpdateRequest:
        if self.canonical_name is None and self.public_note is None:
            raise ValueError("canonical_name or public_note is required")
        return self


class AdminIncidentBulletinUpdateResponse(StrictModel):
    fire_id: str
    canonical_name: str | None = None
    public_note: str | None = None
    source_key: str
    validated_at: datetime
    version: int = Field(ge=1)


class AdminIncidentBulletinEntry(StrictModel):
    entry_id: str = Field(min_length=1, max_length=96)
    episode_id: str | None = None
    source_key: str = Field(min_length=1, max_length=128)
    kind: Literal["fact", "timeline"]
    body: str = Field(min_length=1, max_length=1_000)
    effective_at: datetime
    published_at: datetime
    retired_at: datetime | None = None
    state: Literal["PUBLISHED", "RETIRED"]
    reason: str = Field(min_length=1, max_length=500)
    created_by: str = Field(min_length=1, max_length=255)
    retired_by: str | None = None
    retirement_reason: str | None = Field(default=None, max_length=500)
    version: int = Field(ge=1)


class AdminIncidentBulletinEntriesResponse(StrictModel):
    fire_id: str
    entries: list[AdminIncidentBulletinEntry] = Field(default_factory=list, max_length=500)


class AdminIncidentBulletinEntryCreateRequest(StrictModel):
    episode_id: str | None = Field(default=None, min_length=1, max_length=16)
    source_key: str = Field(min_length=1, max_length=128)
    kind: Literal["fact", "timeline"]
    body: str = Field(min_length=1, max_length=1_000)
    effective_at: datetime
    reason: str = Field(min_length=10, max_length=500)


class AdminIncidentBulletinEntryRetireRequest(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
