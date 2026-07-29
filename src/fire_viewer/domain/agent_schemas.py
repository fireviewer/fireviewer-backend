from __future__ import annotations

import json
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, JsonValue, model_validator

from fire_viewer.domain.enums import (
    AgentBatchPriority,
    AgentBatchState,
    AgentBatchType,
    AgentConsentBasis,
    AgentConsentState,
    AgentDispatchState,
    AgentMediaType,
    AgentSourceCandidateState,
    AgentSourcePackageKind,
    AgentSourcePackageState,
    AgentSourceResearchState,
)
from fire_viewer.domain.geometry_contract import validate_geojson_geometry


class StrictAgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


SafeIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
AgentOperationType = Literal["user_media", "source_research", "satellite_media"]


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _json_sha256(value: JsonValue) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class AgentLocationMetadata(StrictAgentModel):
    captured_at: datetime | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    gps_accuracy_m: float | None = Field(default=None, gt=0, le=100_000)
    location_origin: (
        Literal["METADATA", "USER_DECLARED", "EXPLICIT_SOURCE_GEOMETRY", "HUMAN_CONFIRMED"] | None
    ) = None

    @model_validator(mode="after")
    def validate_location(self) -> AgentLocationMetadata:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        if self.latitude is not None and self.location_origin is None:
            raise ValueError("coordinates require location_origin")
        if self.latitude is None and (self.gps_accuracy_m is not None or self.location_origin):
            raise ValueError("location metadata requires coordinates")
        return self


class AgentFrameInput(StrictAgentModel):
    frame_id: SafeIdentifier
    timestamp_s: float = Field(ge=0)
    working_file_url: AnyHttpUrl


class AgentConsentInput(StrictAgentModel):
    basis: AgentConsentBasis
    scopes: list[
        Literal[
            "temporary_storage",
            "agent_analysis",
            "human_review",
            "retain_evidence",
            "display_media",
            "display_spatial_marker",
            "contact_contributor",
        ]
    ] = Field(min_length=3, max_length=7)
    terms_version: str = Field(min_length=1, max_length=64)
    evidence_sha256: Sha256Hex
    subject_reference_hash: Sha256Hex | None = None
    source_reference_url: AnyHttpUrl | None = None
    license_identifier: str | None = Field(default=None, min_length=1, max_length=128)
    granted_at: datetime
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_basis_and_scope(self) -> AgentConsentInput:
        required = {"temporary_storage", "agent_analysis", "human_review"}
        if not required.issubset(set(self.scopes)):
            raise ValueError("temporary storage, agent analysis, and human review are required")
        if self.basis == AgentConsentBasis.SOURCE_LICENSE and (
            self.source_reference_url is None or self.license_identifier is None
        ):
            raise ValueError("source_license requires its HTTPS reference and identifier")
        if (
            self.basis == AgentConsentBasis.PUBLIC_SOURCE_ANALYSIS
            and self.source_reference_url is None
        ):
            raise ValueError("public_source_analysis requires its HTTPS source reference")
        if self.expires_at is not None and self.expires_at <= self.granted_at:
            raise ValueError("consent expires_at must follow granted_at")
        return self


class AgentMediaItemInput(StrictAgentModel):
    input_id: SafeIdentifier
    media_type: AgentMediaType
    working_file_url: AnyHttpUrl | None = None
    media_sha256: Sha256Hex | None = None
    size_bytes: int | None = Field(default=None, gt=0, le=2_147_483_648)
    metadata: AgentLocationMetadata = Field(default_factory=AgentLocationMetadata)
    frames: list[AgentFrameInput] = Field(default_factory=list, max_length=64)
    audio_url: AnyHttpUrl | None = None
    article_text: str | None = Field(default=None, max_length=100_000)
    consent: AgentConsentInput

    @model_validator(mode="after")
    def validate_processable_input(self) -> AgentMediaItemInput:
        if not any((self.working_file_url, self.frames, self.audio_url, self.article_text)):
            raise ValueError("a media item requires processable content")
        if self.media_type == AgentMediaType.AUDIO and self.audio_url is None:
            raise ValueError("audio items require audio_url")
        if self.working_file_url is not None and (
            self.media_sha256 is None or self.size_bytes is None
        ):
            raise ValueError("working_file_url requires media_sha256 and size_bytes")
        return self


class AgentBatchCreateRequest(StrictAgentModel):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: SafeIdentifier
    batch_type: AgentBatchType
    priority: AgentBatchPriority
    fire_id: str | None = Field(default=None, pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier | None = None
    deadline_at: datetime | None = None
    purge_after: datetime
    items: list[AgentMediaItemInput] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_batch(self) -> AgentBatchCreateRequest:
        if (self.fire_id is None) != (self.episode_id is None):
            raise ValueError("fire_id and episode_id must be supplied together")
        input_ids = [item.input_id for item in self.items]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("input_id values must be unique")
        if sum(len(item.frames) for item in self.items) > 256:
            raise ValueError("a batch may contain at most 256 frames")
        return self


class AgentBatchItemResponse(StrictAgentModel):
    input_id: str
    media_type: AgentMediaType
    media_sha256: str | None
    size_bytes: int | None
    consent_state: AgentConsentState
    purge_after: datetime
    purged_at: datetime | None


class AgentDispatchResponse(StrictAgentModel):
    dispatch_id: str
    state: AgentDispatchState
    payload_hash: str
    attempt: int
    poll_count: int
    remote_job_id: str | None
    remote_status: str | None
    next_attempt_at: datetime | None
    submitted_at: datetime | None
    completed_at: datetime | None
    last_error_code: str | None


class AgentBatchResponse(StrictAgentModel):
    batch_id: str
    fire_id: str | None = None
    episode_id: str | None = None
    analysis_id: str | None = None
    schema_version: str
    batch_type: AgentBatchType
    priority: AgentBatchPriority
    state: AgentBatchState
    payload_hash: str | None
    deadline_at: datetime | None
    purge_after: datetime
    submitted_at: datetime | None
    completed_at: datetime | None
    items: list[AgentBatchItemResponse]
    dispatch: AgentDispatchResponse | None = None


class AgentBatchCreateOutcome(StrictAgentModel):
    replayed: bool
    batch: AgentBatchResponse


class AgentConsentWithdrawRequest(StrictAgentModel):
    reason: str = Field(min_length=8, max_length=500)


class AgentConsentWithdrawResponse(StrictAgentModel):
    batch_id: str
    input_id: str
    consent_state: AgentConsentState
    batch_state: AgentBatchState
    dispatch_state: AgentDispatchState | None


class AgentOperationStatus(StrictAgentModel):
    operation_type: AgentOperationType
    schedule_state: Literal["required", "declared_absent", "not_scheduled"]
    pending_files: int = Field(ge=0)
    pending_analyses: int = Field(ge=0)
    running_analyses: int = Field(ge=0)
    last_run_at: datetime | None = None
    can_run: bool
    blocked_reason: (
        Literal[
            "dispatch_disabled",
            "research_disabled",
            "operation_declared_absent",
            "operation_not_scheduled",
            "input_not_ready",
            "already_completed",
            "already_running",
        ]
        | None
    ) = None


class AgentOperationWindow(StrictAgentModel):
    """One manifest-bound operational window exposed to the admin client."""

    analysis_window_id: SafeIdentifier
    local_date: date
    campaign_day_state: (
        Literal["locked", "ready", "running", "review", "published", "failed"] | None
    )
    actions: list[AgentOperationStatus]


class AgentOperationsOverview(StrictAgentModel):
    fire_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier
    analysis_window_id: SafeIdentifier
    local_date: date
    campaign_day_state: (
        Literal["locked", "ready", "running", "review", "published", "failed"] | None
    )
    actions: list[AgentOperationStatus]
    available_windows: list[AgentOperationWindow] = Field(default_factory=list)


class AgentOperationRunRequest(StrictAgentModel):
    expected_analysis_window_id: SafeIdentifier


class AgentOperationRunResponse(StrictAgentModel):
    fire_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier
    analysis_window_id: SafeIdentifier
    operation_type: AgentOperationType
    operation_ids: list[SafeIdentifier]
    queued_files: int = Field(ge=0)


class AgentDispatcherTickResponse(StrictAgentModel):
    processed: bool


class AgentSourceFileDateMetadata(StrictAgentModel):
    """Reliable, operator-supplied date evidence for one uploaded file."""

    filename: str = Field(min_length=1, max_length=500)
    effective_at: datetime
    basis: Literal["captured_at", "observed_at", "published_at", "acquired_at"]

    @model_validator(mode="after")
    def validate_date_metadata(self) -> AgentSourceFileDateMetadata:
        if not _is_timezone_aware(self.effective_at):
            raise ValueError("source file date metadata must include a timezone")
        if "/" in self.filename or "\\" in self.filename or self.filename in {".", ".."}:
            raise ValueError("source file date metadata filenames must be basenames")
        return self


class AgentSourcePackageOpenRequest(StrictAgentModel):
    file_count: int = Field(gt=0, le=5_000)
    total_size_bytes: int = Field(gt=0, le=4_294_967_296)
    known_start_date: date | None = None
    known_end_date: date | None = None
    location_hint: str | None = Field(default=None, min_length=2, max_length=500)
    authorize_private_analysis: Literal[True]
    file_date_metadata: list[AgentSourceFileDateMetadata] = Field(
        default_factory=list,
        max_length=5_000,
    )

    @model_validator(mode="after")
    def validate_period(self) -> AgentSourcePackageOpenRequest:
        if self.known_end_date is not None and self.known_start_date is None:
            raise ValueError("known_end_date requires known_start_date")
        if self.known_start_date is not None:
            end_date = self.known_end_date or self.known_start_date
            if end_date < self.known_start_date:
                raise ValueError("known_end_date must not precede known_start_date")
            if (end_date - self.known_start_date).days > 31:
                raise ValueError("one source package may cover at most 32 days")
        if len(self.file_date_metadata) > self.file_count:
            raise ValueError("file_date_metadata cannot exceed file_count")
        filenames = [item.filename.casefold() for item in self.file_date_metadata]
        if len(filenames) != len(set(filenames)):
            raise ValueError("file_date_metadata filenames must be unique")
        return self


class AgentSourcePackageOpenResponse(StrictAgentModel):
    package_id: SafeIdentifier
    upload_id: str
    pathname_prefix: str
    upload_grant: str
    expires_at: datetime
    maximum_file_size_bytes: int = Field(gt=0)
    allowed_content_types: list[str]
    already_uploaded_filenames: list[str] = Field(default_factory=list)


class AgentDailySatellitePackageOpenRequest(StrictAgentModel):
    expected_analysis_window_id: SafeIdentifier
    file_count: int = Field(gt=1, le=5_000)
    total_size_bytes: int = Field(gt=0, le=4_294_967_296)


class AgentDailySatelliteImageManifestItem(StrictAgentModel):
    kind: Literal["satellite_image"]
    filename: str = Field(min_length=1, max_length=500)
    sha256: Sha256Hex
    product_id: SafeIdentifier
    provider: str = Field(min_length=1, max_length=128)
    acquired_at: datetime
    bbox_wgs84: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0, le=100_000)
    bands: list[str] = Field(min_length=1, max_length=32)
    cloud_cover_percent: float | None = Field(default=None, ge=0, le=100)
    source_reference_url: AnyHttpUrl
    license_identifier: str = Field(min_length=1, max_length=128)
    attribution: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_image_product(self) -> AgentDailySatelliteImageManifestItem:
        if not _is_timezone_aware(self.acquired_at):
            raise ValueError("satellite acquisition time must include a timezone")
        min_lon, min_lat, max_lon, max_lat = self.bbox_wgs84
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError("satellite bbox must be an ordered WGS84 extent")
        if len(self.bands) != len(set(self.bands)):
            raise ValueError("satellite band names must be unique")
        return self


class AgentDailyHotspotManifestItem(StrictAgentModel):
    kind: Literal["hotspot_geojson"]
    filename: str = Field(min_length=1, max_length=500)
    sha256: Sha256Hex
    product_id: SafeIdentifier
    provider: str = Field(min_length=1, max_length=128)
    acquired_at: datetime
    bbox_wgs84: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0, le=100_000)
    sensor_names: list[str] = Field(min_length=1, max_length=16)
    source_reference_url: AnyHttpUrl
    license_identifier: str = Field(min_length=1, max_length=128)
    attribution: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_hotspot_product(self) -> AgentDailyHotspotManifestItem:
        if not _is_timezone_aware(self.acquired_at):
            raise ValueError("hotspot acquisition time must include a timezone")
        min_lon, min_lat, max_lon, max_lat = self.bbox_wgs84
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError("hotspot bbox must be an ordered WGS84 extent")
        if len(self.sensor_names) != len(set(self.sensor_names)):
            raise ValueError("hotspot sensor names must be unique")
        return self


AgentDailySatelliteManifestItem = Annotated[
    AgentDailySatelliteImageManifestItem | AgentDailyHotspotManifestItem,
    Field(discriminator="kind"),
]


class AgentDailySatelliteManifest(StrictAgentModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_analysis_window_id: SafeIdentifier
    items: list[AgentDailySatelliteManifestItem] = Field(min_length=1, max_length=4_999)

    @model_validator(mode="after")
    def validate_unique_files(self) -> AgentDailySatelliteManifest:
        filenames = [item.filename for item in self.items]
        if len(filenames) != len(set(filenames)):
            raise ValueError("daily satellite manifest filenames must be unique")
        return self


class AgentSourcePackageItemResponse(StrictAgentModel):
    item_id: SafeIdentifier
    original_filename: str
    content_type: str
    media_type: AgentMediaType
    sha256: Sha256Hex
    size_bytes: int = Field(gt=0)
    captured_at: datetime | None = None
    date_classification: Literal["CLASSIFIED", "TO_CLASSIFY"]
    date_evidence: (
        Literal[
            "EXPLICIT_CAPTURED_AT",
            "EXPLICIT_OBSERVED_AT",
            "EXPLICIT_PUBLISHED_AT",
            "EXPLICIT_ACQUIRED_AT",
            "EXIF_DATETIME_ORIGINAL",
            "EXIF_DATETIME",
            "FILENAME_ISO_DATE",
            "PACKAGE_SINGLE_DATE",
            "PUBLIC_OBSERVED_AT",
            "CONFLICTING_METADATA",
            "LEGACY_BATCH_WINDOW",
        ]
        | None
    ) = None
    classified_local_date: date | None = None
    analysis_window_id: SafeIdentifier | None = None
    batch_id: SafeIdentifier | None = None
    input_id: SafeIdentifier | None = None


class AgentSourcePackageDateGroupResponse(StrictAgentModel):
    local_date: date
    analysis_window_id: SafeIdentifier
    item_count: int = Field(gt=0)
    batch_ids: list[SafeIdentifier]


class AgentSourcePackageResponse(StrictAgentModel):
    package_id: SafeIdentifier
    package_kind: AgentSourcePackageKind
    fire_id: str | None = Field(default=None, pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier | None = None
    state: AgentSourcePackageState
    known_start_date: date | None
    known_end_date: date | None
    location_hint: str | None
    analysis_authorized: bool
    publication_authorized: bool
    purge_after: datetime
    finalized_at: datetime | None
    classified_item_count: int = Field(ge=0)
    to_classify_item_count: int = Field(ge=0)
    analysis_window_count: int = Field(ge=0)
    date_groups: list[AgentSourcePackageDateGroupResponse]
    batch_ids: list[SafeIdentifier]
    items: list[AgentSourcePackageItemResponse]


class AgentSourceCandidateResponse(StrictAgentModel):
    candidate_id: SafeIdentifier
    state: AgentSourceCandidateState
    canonical_url: AnyHttpUrl
    source_domain: str
    title: str | None
    published_at: datetime | None
    acquired_at: datetime | None
    media_type: AgentMediaType | None
    media_sha256: Sha256Hex | None
    cutoff_eligible: bool
    license_identifier: str | None
    attribution: str | None


class AgentSourceResearchResponse(StrictAgentModel):
    research_id: SafeIdentifier
    fire_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier
    analysis_id: SafeIdentifier
    local_date: date
    state: AgentSourceResearchState
    progress_percent: int = Field(ge=0, le=100)
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    retryable: bool
    last_error_code: str | None
    last_error_detail: str | None
    candidates: list[AgentSourceCandidateResponse]


class WorkerResearchUploadV1(StrictAgentModel):
    pathname_prefix: str = Field(min_length=3, max_length=512)
    upload_grant: str = Field(min_length=64, max_length=4_096)
    token_endpoint: AnyHttpUrl
    resource_id: SafeIdentifier
    maximum_file_size_bytes: int = Field(gt=0)
    allowed_content_types: list[str] = Field(min_length=1, max_length=32)


class WorkerResearchSourcePolicyV1(StrictAgentModel):
    source_name: str = Field(min_length=1, max_length=255)
    kind: Literal[
        "authority",
        "emergency_service",
        "satellite",
        "weather",
        "air_quality",
        "context",
        "directory",
        "press",
    ]
    scope: Literal["national", "regional", "departmental", "local", "global"]
    confidence_level: Literal["A+", "A", "B", "lead"]
    claim_types: list[str] = Field(min_length=1, max_length=32)
    publication_policy: Literal[
        "facts_with_attribution",
        "dataset_license_required",
        "per_item_license_check",
        "private_analysis_only",
    ]
    minimum_refresh_minutes: int = Field(ge=1, le=43_200)


class WorkerResearchInputV1(StrictAgentModel):
    schema_version: Literal["research-1.0"] = "research-1.0"
    operation: Literal["source_research"] = "source_research"
    research_id: SafeIdentifier
    analysis_window: AnalysisWindowV2
    incident_name: str | None = Field(default=None, max_length=255)
    incident_reference: tuple[float, float]
    cutoff_at: datetime
    location_hint: str | None = Field(default=None, max_length=500)
    source_registry_version: str = Field(min_length=3, max_length=64)
    allowed_domains: list[str] = Field(min_length=1, max_length=200)
    source_policies: dict[str, WorkerResearchSourcePolicyV1]
    search_templates: dict[str, AnyHttpUrl]
    max_fetch_bytes: int = Field(gt=0, le=104_857_600)
    request_timeout_seconds: int = Field(ge=2, le=120)
    private_upload: WorkerResearchUploadV1

    @model_validator(mode="after")
    def validate_research(self) -> WorkerResearchInputV1:
        if not _is_timezone_aware(self.cutoff_at):
            raise ValueError("research cutoff_at must include a timezone")
        if self.cutoff_at != self.analysis_window.window_end_at:
            raise ValueError("research cutoff must equal the analysis window end")
        if len(self.allowed_domains) != len(set(self.allowed_domains)):
            raise ValueError("research domains must be unique")
        if set(self.source_policies) != set(self.allowed_domains):
            raise ValueError("every research domain requires one source policy")
        if not self.search_templates:
            raise ValueError("at least one search provider is required")
        if set(self.search_templates) & set(self.allowed_domains):
            raise ValueError("search providers must be separate from source domains")
        longitude, latitude = self.incident_reference
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("incident reference must be WGS84")
        return self


class WorkerResearchCandidateV1(StrictAgentModel):
    candidate_id: SafeIdentifier
    canonical_url: AnyHttpUrl
    source_domain: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=500)
    published_at: datetime | None = None
    acquired_at: datetime | None = None
    media_type: AgentMediaType | None = None
    blob_pathname: str | None = Field(default=None, min_length=3, max_length=1_024)
    media_sha256: Sha256Hex | None = None
    size_bytes: int | None = Field(default=None, gt=0, le=1_073_741_824)
    excerpt: str | None = Field(default=None, max_length=100_000)
    license_identifier: str | None = Field(default=None, max_length=128)
    attribution: str | None = Field(default=None, max_length=500)
    provenance: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_candidate(self) -> WorkerResearchCandidateV1:
        for timestamp in (self.published_at, self.acquired_at):
            if timestamp is not None and not _is_timezone_aware(timestamp):
                raise ValueError("candidate timestamps must include a timezone")
        stored_fields = (self.blob_pathname, self.media_sha256, self.size_bytes)
        if any(value is not None for value in stored_fields) and not all(
            value is not None for value in stored_fields
        ):
            raise ValueError("stored candidate media requires path, hash, and size")
        if self.media_type == AgentMediaType.ARTICLE and not self.excerpt:
            raise ValueError("article candidates require extracted text")
        if self.media_type not in {None, AgentMediaType.ARTICLE} and self.blob_pathname is None:
            raise ValueError("media candidates require a private uploaded object")
        return self


class WorkerResearchOutputV1(StrictAgentModel):
    schema_version: Literal["research-1.0"] = "research-1.0"
    research_id: SafeIdentifier
    status: Literal["succeeded", "partial_failure", "failed"]
    retryable: bool
    model_run: WorkerModelRun
    queries: list[str] = Field(default_factory=list, max_length=100)
    candidates: list[WorkerResearchCandidateV1] = Field(default_factory=list, max_length=500)
    validation_errors: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_output(self) -> WorkerResearchOutputV1:
        if self.model_run.model_role != "source_research":
            raise ValueError("research output requires the source_research model role")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("research candidate ids must be unique")
        return self


class WorkerFrameInput(StrictAgentModel):
    frame_id: SafeIdentifier
    timestamp_s: float = Field(ge=0)
    working_file_url: AnyHttpUrl


class WorkerBatchItem(StrictAgentModel):
    input_id: SafeIdentifier
    media_type: AgentMediaType
    working_file_url: AnyHttpUrl | None = None
    metadata: AgentLocationMetadata
    frames: list[WorkerFrameInput] = Field(default_factory=list, max_length=64)
    audio_url: AnyHttpUrl | None = None
    article_text: str | None = Field(default=None, max_length=100_000)


class WorkerInput(StrictAgentModel):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: SafeIdentifier
    batch_type: AgentBatchType
    priority: AgentBatchPriority
    deadline_at: datetime | None = None
    items: list[WorkerBatchItem] = Field(min_length=1, max_length=32)


class WorkerTranscriptSegment(StrictAgentModel):
    segment_id: SafeIdentifier
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=10_000)
    uncertain: bool = False


class WorkerTranscript(StrictAgentModel):
    language: str | None = Field(default=None, max_length=16)
    segments: list[WorkerTranscriptSegment] = Field(default_factory=list, max_length=10_000)


class WorkerPixelRegion(StrictAgentModel):
    region_id: SafeIdentifier
    evidence_id: str
    label: str = Field(min_length=1, max_length=128)
    bbox_normalized: tuple[float, float, float, float]
    task: Literal["fire_detection", "phrase_grounding", "ocr"]
    model_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_box(self) -> WorkerPixelRegion:
        x1, y1, x2, y2 = self.bbox_normalized
        if not all(0 <= coordinate <= 1 for coordinate in self.bbox_normalized):
            raise ValueError("bbox coordinates must be normalized")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bbox must have positive area")
        return self


class WorkerVisualEvidenceSelection(StrictAgentModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    selected_for_grounding: bool
    selection_reason: Literal[
        "single_image",
        "target_detection",
        "temporal_coverage",
        "detector_fallback",
        "capacity_limit",
    ]
    max_detection_score: float | None = Field(default=None, ge=0, le=1)


class WorkerFactualObservation(StrictAgentModel):
    type: str = Field(min_length=1, max_length=128)
    evidence_kind: Literal["frame", "image", "transcript_segment", "article_text", "metadata"]
    evidence_id: str = Field(min_length=1, max_length=128)
    region_id: str | None = Field(default=None, max_length=128)
    description: str = Field(min_length=1, max_length=1_000)
    certainty: Literal["directly_visible", "explicitly_written", "explicitly_spoken"]


class WorkerExplicitLiteral(StrictAgentModel):
    literal: str = Field(min_length=1, max_length=500)
    evidence_kind: Literal["frame", "image", "transcript_segment", "article_text", "metadata"]
    evidence_id: str = Field(min_length=1, max_length=128)


class WorkerMetadataResult(StrictAgentModel):
    capture_location_available: bool
    capture_location_origin: (
        Literal["METADATA", "USER_DECLARED", "EXPLICIT_SOURCE_GEOMETRY", "HUMAN_CONFIRMED"] | None
    ) = None


class WorkerGeographicMarker(StrictAgentModel):
    type: Literal["media_capture"]
    geometry_origin: Literal[
        "METADATA", "USER_DECLARED", "EXPLICIT_SOURCE_GEOMETRY", "HUMAN_CONFIRMED"
    ]


class WorkerItemResult(StrictAgentModel):
    input_id: str
    metadata_result: WorkerMetadataResult
    transcript: WorkerTranscript = Field(default_factory=WorkerTranscript)
    pixel_regions: list[WorkerPixelRegion] = Field(default_factory=list)
    visual_evidence_selection: list[WorkerVisualEvidenceSelection] = Field(default_factory=list)
    factual_observations: list[WorkerFactualObservation] = Field(default_factory=list)
    explicit_places: list[WorkerExplicitLiteral] = Field(default_factory=list)
    explicit_times: list[WorkerExplicitLiteral] = Field(default_factory=list)
    location_status: Literal[
        "NO_LOCATION",
        "CAPTURE_LOCATION_ONLY",
        "USER_DECLARED_OBSERVATION_LOCATION",
        "EXPLICIT_SOURCE_GEOMETRY",
        "HUMAN_CONFIRMED_OBSERVATION_LOCATION",
    ]
    geographic_marker_candidate: WorkerGeographicMarker | None = None
    observed_phenomenon_marker: None = None
    requires_human_review: Literal[True]


class WorkerModelRun(StrictAgentModel):
    model_role: Literal[
        "asr",
        "fire_detection",
        "visual_grounding",
        "multimodal_extraction",
        "source_research",
    ]
    model_id: str
    revision: str
    status: Literal["succeeded", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    load_ms: int = Field(ge=0)
    inference_ms: int = Field(ge=0)
    peak_vram_bytes: int | None = Field(default=None, ge=0)
    error_code: str | None = None


class WorkerOutput(StrictAgentModel):
    schema_version: Literal["1.0"] = "1.0"
    batch_id: str
    status: Literal["succeeded", "partial_failure", "failed"]
    retryable: bool
    model_runs: list[WorkerModelRun]
    items: list[WorkerItemResult]
    validation_errors: list[str] = Field(default_factory=list)
    boot_ms: int = Field(ge=0)


class AnalysisWindowV2(StrictAgentModel):
    analysis_id: SafeIdentifier
    fire_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier
    window_start_at: datetime
    window_end_at: datetime
    local_date: date
    timezone: str = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def validate_window(self) -> AnalysisWindowV2:
        if not all(
            _is_timezone_aware(value) for value in (self.window_start_at, self.window_end_at)
        ):
            raise ValueError("analysis window datetimes must include a timezone")
        if self.window_end_at <= self.window_start_at:
            raise ValueError("analysis window end must follow its start")
        return self


class DeclaredObservationV2(StrictAgentModel):
    observed_at: datetime
    observation_type: str = Field(min_length=2, max_length=128)
    direct_observation: bool
    description: str = Field(min_length=20, max_length=4_000)
    location_mode: Literal["place", "device", "manual"]
    location_label: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    uncertainty_m: float | None = Field(default=None, gt=0, le=100_000, allow_inf_nan=False)
    media_captured_at: datetime | None = None
    media_direction: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_declared_observation(self) -> DeclaredObservationV2:
        if not _is_timezone_aware(self.observed_at):
            raise ValueError("declared observation time must include a timezone")
        if self.media_captured_at is not None and not _is_timezone_aware(self.media_captured_at):
            raise ValueError("declared media capture time must include a timezone")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("declared location coordinates must be supplied together")
        if self.uncertainty_m is not None and self.latitude is None:
            raise ValueError("declared location uncertainty requires coordinates")
        return self


class SourceProvenanceV2(StrictAgentModel):
    source_key: SafeIdentifier
    source_reference_url: AnyHttpUrl | None = None
    license_identifier: str = Field(min_length=1, max_length=128)
    attribution: str | None = Field(default=None, max_length=500)
    trust: Literal["unverified", "partner", "institutional", "operator"]
    source_registry_version: str | None = Field(default=None, min_length=3, max_length=64)
    source_policy_domain: str | None = Field(default=None, min_length=3, max_length=253)
    source_kind: (
        Literal[
            "authority",
            "emergency_service",
            "satellite",
            "weather",
            "air_quality",
            "context",
            "directory",
            "press",
        ]
        | None
    ) = None
    source_confidence: Literal["A+", "A", "B", "lead"] | None = None
    publication_policy: (
        Literal[
            "facts_with_attribution",
            "dataset_license_required",
            "per_item_license_check",
            "private_analysis_only",
        ]
        | None
    ) = None
    claim_types: list[str] = Field(default_factory=list, max_length=32)
    declared_observation: DeclaredObservationV2 | None = None


class CameraMetadataV2(StrictAgentModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    orthometric_height_m: float | None = Field(default=None, allow_inf_nan=False)
    horizontal_accuracy_m: float | None = Field(default=None, gt=0, le=100_000)
    yaw_deg: float | None = Field(default=None, ge=0, lt=360)
    pitch_deg: float | None = Field(default=None, ge=-90, le=90)
    roll_deg: float | None = Field(default=None, ge=-180, le=180)
    horizontal_fov_deg: float | None = Field(default=None, gt=0, lt=180)
    image_width_px: int | None = Field(default=None, gt=0, le=200_000)
    image_height_px: int | None = Field(default=None, gt=0, le=200_000)
    pose_origin: (
        Literal["METADATA", "USER_DECLARED", "CROSS_VIEW_ESTIMATE", "HUMAN_CONFIRMED"] | None
    ) = None

    @model_validator(mode="after")
    def validate_camera(self) -> CameraMetadataV2:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("camera latitude and longitude must be supplied together")
        position_details = (
            self.orthometric_height_m,
            self.horizontal_accuracy_m,
            self.pose_origin,
        )
        if self.latitude is None and any(value is not None for value in position_details):
            raise ValueError("camera position details require coordinates")
        if self.latitude is not None and self.pose_origin is None:
            raise ValueError("camera coordinates require pose_origin")
        orientation = (self.yaw_deg, self.pitch_deg, self.roll_deg)
        if any(value is not None for value in orientation) and not all(
            value is not None for value in orientation
        ):
            raise ValueError("camera orientation must provide yaw, pitch, and roll together")
        intrinsics = (self.horizontal_fov_deg, self.image_width_px, self.image_height_px)
        if any(value is not None for value in intrinsics) and not all(
            value is not None for value in intrinsics
        ):
            raise ValueError("camera intrinsics require field of view and image dimensions")
        return self


class SatelliteMetadataV2(StrictAgentModel):
    product_id: SafeIdentifier
    provider: str = Field(min_length=1, max_length=128)
    acquired_at: datetime
    crs: str = Field(min_length=3, max_length=128)
    raster_width_px: int = Field(gt=0, le=500_000)
    raster_height_px: int = Field(gt=0, le=500_000)
    geotransform: tuple[float, float, float, float, float, float]
    bbox_wgs84: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0, le=100_000)
    bands: list[str] = Field(min_length=1, max_length=32)
    cloud_cover_percent: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_bbox(self) -> SatelliteMetadataV2:
        if not _is_timezone_aware(self.acquired_at):
            raise ValueError("satellite acquisition time must include a timezone")
        if not all(isfinite(value) for value in self.geotransform):
            raise ValueError("satellite geotransform values must be finite")
        min_lon, min_lat, max_lon, max_lat = self.bbox_wgs84
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError("satellite bbox must be an ordered WGS84 extent")
        if len(self.bands) != len(set(self.bands)):
            raise ValueError("satellite band names must be unique")
        return self


class HotspotMetadataV2(StrictAgentModel):
    product_id: SafeIdentifier
    provider: str = Field(min_length=1, max_length=128)
    acquired_at: datetime
    sensor_names: list[str] = Field(min_length=1, max_length=16)
    resolution_m: float = Field(gt=0, le=100_000)
    bbox_wgs84: tuple[float, float, float, float]

    @model_validator(mode="after")
    def validate_hotspots(self) -> HotspotMetadataV2:
        if not _is_timezone_aware(self.acquired_at):
            raise ValueError("hotspot acquisition time must include a timezone")
        if len(self.sensor_names) != len(set(self.sensor_names)):
            raise ValueError("hotspot sensor names must be unique")
        min_lon, min_lat, max_lon, max_lat = self.bbox_wgs84
        if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
            raise ValueError("hotspot bbox must be an ordered WGS84 extent")
        return self


class SpatialReferenceAssetV2(StrictAgentModel):
    kind: Literal[
        "terrain_mnt",
        "surface_dsm",
        "orthophoto",
        "scene_catalog",
        "source_manifest",
    ]
    working_file_url: AnyHttpUrl
    sha256: Sha256Hex
    crs: str = Field(min_length=3, max_length=128)
    resolution_m: float | None = Field(default=None, gt=0, le=100_000)


class SpatialReferenceBundleV2(StrictAgentModel):
    reference_id: SafeIdentifier
    manifest_sha256: Sha256Hex
    assets: list[SpatialReferenceAssetV2] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_assets(self) -> SpatialReferenceBundleV2:
        kinds = [asset.kind for asset in self.assets]
        if len(kinds) != len(set(kinds)):
            raise ValueError("spatial reference asset kinds must be unique")
        return self


class WorkerBatchItemV2(StrictAgentModel):
    input_id: SafeIdentifier
    media_type: AgentMediaType
    working_file_url: AnyHttpUrl | None = None
    provenance: SourceProvenanceV2
    captured_at: datetime | None = None
    camera: CameraMetadataV2 | None = None
    satellite: SatelliteMetadataV2 | None = None
    hotspot: HotspotMetadataV2 | None = None
    frames: list[WorkerFrameInput] = Field(default_factory=list, max_length=64)
    audio_url: AnyHttpUrl | None = None
    article_text: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def validate_media_shape(self) -> WorkerBatchItemV2:
        if self.captured_at is not None and not _is_timezone_aware(self.captured_at):
            raise ValueError("media capture time must include a timezone")
        if not any((self.working_file_url, self.frames, self.audio_url, self.article_text)):
            raise ValueError("a v2 media item requires processable content")
        if self.media_type == AgentMediaType.AUDIO and self.audio_url is None:
            raise ValueError("audio items require audio_url")
        if self.media_type == AgentMediaType.SATELLITE_IMAGE:
            if self.satellite is None:
                raise ValueError("satellite images require satellite metadata")
            if self.camera is not None:
                raise ValueError("satellite images cannot carry terrestrial camera metadata")
            if self.hotspot is not None:
                raise ValueError("satellite images cannot carry hotspot metadata")
        elif self.media_type == AgentMediaType.SATELLITE_DATA:
            if self.hotspot is None or self.article_text is None:
                raise ValueError("satellite data require hotspot metadata and GeoJSON text")
            if self.camera is not None or self.satellite is not None:
                raise ValueError("satellite data cannot carry image or camera metadata")
        elif self.satellite is not None:
            raise ValueError("satellite metadata is reserved for satellite images")
        elif self.hotspot is not None:
            raise ValueError("hotspot metadata is reserved for satellite data")
        if self.camera is not None and self.media_type not in {
            AgentMediaType.IMAGE,
            AgentMediaType.VIDEO,
        }:
            raise ValueError("camera metadata is reserved for images and videos")
        return self


class AgentMediaItemInputV2(WorkerBatchItemV2):
    media_sha256: Sha256Hex | None = None
    size_bytes: int | None = Field(default=None, gt=0, le=2_147_483_648)
    consent: AgentConsentInput

    @model_validator(mode="after")
    def validate_stored_media(self) -> AgentMediaItemInputV2:
        if self.working_file_url is not None and (
            self.media_sha256 is None or self.size_bytes is None
        ):
            raise ValueError("working_file_url requires media_sha256 and size_bytes")
        return self


class AgentBatchCreateRequestV2(StrictAgentModel):
    schema_version: Literal["2.0"] = "2.0"
    batch_id: SafeIdentifier
    batch_type: AgentBatchType
    priority: AgentBatchPriority
    analysis_window: AnalysisWindowV2
    deadline_at: datetime | None = None
    purge_after: datetime
    reference_bundle: SpatialReferenceBundleV2 | None = None
    items: list[AgentMediaItemInputV2] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_batch(self) -> AgentBatchCreateRequestV2:
        if not _is_timezone_aware(self.purge_after):
            raise ValueError("purge_after must include a timezone")
        if self.deadline_at is not None and not _is_timezone_aware(self.deadline_at):
            raise ValueError("deadline_at must include a timezone")
        input_ids = [item.input_id for item in self.items]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("input_id values must be unique")
        if sum(len(item.frames) for item in self.items) > 256:
            raise ValueError("a batch may contain at most 256 frames")
        satellite_types = {AgentMediaType.SATELLITE_IMAGE, AgentMediaType.SATELLITE_DATA}
        has_satellite = any(item.media_type in satellite_types for item in self.items)
        if self.batch_type == AgentBatchType.SATELLITE_MEDIA and not all(
            item.media_type in satellite_types for item in self.items
        ):
            raise ValueError("satellite batches may contain only satellite images or data")
        if self.batch_type != AgentBatchType.SATELLITE_MEDIA and has_satellite:
            raise ValueError("satellite inputs require a satellite batch")
        return self


AgentBatchCreatePayload = Annotated[
    AgentBatchCreateRequest | AgentBatchCreateRequestV2,
    Field(discriminator="schema_version"),
]


class WorkerInputV2(StrictAgentModel):
    schema_version: Literal["2.0"] = "2.0"
    batch_id: SafeIdentifier
    batch_type: AgentBatchType
    priority: AgentBatchPriority
    analysis_window: AnalysisWindowV2
    deadline_at: datetime | None = None
    reference_bundle: SpatialReferenceBundleV2 | None = None
    items: list[WorkerBatchItemV2] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_items(self) -> WorkerInputV2:
        if self.deadline_at is not None and not _is_timezone_aware(self.deadline_at):
            raise ValueError("deadline_at must include a timezone")
        input_ids = [item.input_id for item in self.items]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("input_id values must be unique")
        if sum(len(item.frames) for item in self.items) > 256:
            raise ValueError("a batch may contain at most 256 frames")
        satellite_types = {AgentMediaType.SATELLITE_IMAGE, AgentMediaType.SATELLITE_DATA}
        has_satellite = any(item.media_type in satellite_types for item in self.items)
        if self.batch_type == AgentBatchType.SATELLITE_MEDIA and not all(
            item.media_type in satellite_types for item in self.items
        ):
            raise ValueError("satellite batches may contain only satellite images or data")
        if self.batch_type != AgentBatchType.SATELLITE_MEDIA and has_satellite:
            raise ValueError("satellite inputs require a satellite batch")
        return self


SourceSemanticAnchorV2 = Literal[
    "active_fire_point",
    "visible_fire_front_point",
    "visible_fire_front",
    "smoke_column_base",
    "smoke_origin_point",
    "burned_area_polygon",
]
SpatialProposalKindV2 = Literal[
    "active_fire_point",
    "smoke_origin_point",
    "visible_fire_front",
    "probable_activity_envelope",
    "burned_area_polygon",
    "legacy_ground_point",
]


class SourceAnnotationV2(StrictAgentModel):
    annotation_id: SafeIdentifier
    evidence_id: SafeIdentifier
    evidence_kind: Literal["image", "frame", "satellite_image"]
    semantic_anchor: SourceSemanticAnchorV2
    source_point_normalized: tuple[float, float] | None = None
    source_geometry_normalized: dict[str, object] | None = None
    model_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_source_geometry(self) -> SourceAnnotationV2:
        point_semantics = {
            "active_fire_point",
            "visible_fire_front_point",
            "smoke_column_base",
            "smoke_origin_point",
        }
        allowed_types = (
            {"Point"}
            if self.semantic_anchor in point_semantics
            else (
                {"LineString", "MultiLineString"}
                if self.semantic_anchor == "visible_fire_front"
                else {"Polygon", "MultiPolygon"}
            )
        )
        geometry = self.source_geometry_normalized
        if geometry is None:
            if self.source_point_normalized is None:
                raise ValueError("source annotation requires normalized source geometry")
            geometry = {
                "type": "Point",
                "coordinates": list(self.source_point_normalized),
            }
            self.source_geometry_normalized = geometry
        validated = validate_geojson_geometry(
            geometry,
            allowed_types=allowed_types,
            normalized=True,
        )
        if self.source_point_normalized is not None:
            if validated["type"] != "Point":
                raise ValueError("source_point_normalized is only compatible with Point geometry")
            coordinates = validated["coordinates"]
            assert isinstance(coordinates, list | tuple)
            if tuple(float(value) for value in coordinates[:2]) != self.source_point_normalized:
                raise ValueError(
                    "source point and source geometry must reference the same position"
                )
        elif validated["type"] == "Point":
            coordinates = validated["coordinates"]
            assert isinstance(coordinates, list | tuple)
            self.source_point_normalized = (float(coordinates[0]), float(coordinates[1]))
        return self


class SpatialProposalV2(StrictAgentModel):
    proposal_id: SafeIdentifier
    annotation_id: SafeIdentifier | None = None
    status: Literal["ground_point", "projected_geometry", "insufficient_geometry"]
    proposal_kind: SpatialProposalKindV2 | None = None
    observed_at: datetime | None = None
    geometry_origin: (
        Literal[
            "SATELLITE_GEOTRANSFORM",
            "CAMERA_RAYCAST",
            "CROSS_VIEW_RAYCAST",
            "EXPLICIT_SOURCE_GEOMETRY",
        ]
        | None
    ) = None
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    altitude_m: float | None = Field(default=None, allow_inf_nan=False)
    geometry_geojson: dict[str, object] | None = None
    horizontal_accuracy_m: float | None = Field(default=None, gt=0, le=100_000)
    reference_bundle_sha256: Sha256Hex | None = None
    uncertainty_codes: list[SafeIdentifier] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_projection(self) -> SpatialProposalV2:
        if self.observed_at is not None and not _is_timezone_aware(self.observed_at):
            raise ValueError("spatial observation time must include a timezone")
        if self.status == "ground_point":
            projected = (
                self.geometry_origin,
                self.longitude,
                self.latitude,
                self.horizontal_accuracy_m,
                self.reference_bundle_sha256,
            )
            if self.annotation_id is None or not all(value is not None for value in projected):
                raise ValueError("ground_point requires sourced coordinates and accuracy")
            if self.proposal_kind not in {None, "legacy_ground_point"}:
                raise ValueError("ground_point is reserved for the legacy point contract")
            self.proposal_kind = "legacy_ground_point"
            geometry = self.geometry_geojson or {
                "type": "Point",
                "coordinates": [self.longitude, self.latitude],
            }
            validate_geojson_geometry(geometry, allowed_types={"Point"})
            self.geometry_geojson = geometry
        elif self.status == "projected_geometry":
            if (
                self.proposal_kind is None
                or self.proposal_kind == "legacy_ground_point"
                or self.geometry_origin is None
                or self.horizontal_accuracy_m is None
                or self.reference_bundle_sha256 is None
                or self.observed_at is None
                or self.geometry_geojson is None
            ):
                raise ValueError(
                    "projected_geometry requires kind, geometry, observation time, "
                    "origin, accuracy and reference"
                )
            if self.geometry_origin != "EXPLICIT_SOURCE_GEOMETRY" and self.annotation_id is None:
                raise ValueError("projected media geometry requires a source annotation")
            allowed_types = {
                "active_fire_point": {"Point"},
                "smoke_origin_point": {"Point"},
                "visible_fire_front": {"LineString", "MultiLineString"},
                "probable_activity_envelope": {"Polygon", "MultiPolygon"},
                "burned_area_polygon": {"Polygon", "MultiPolygon"},
            }[self.proposal_kind]
            geometry = validate_geojson_geometry(
                self.geometry_geojson,
                allowed_types=allowed_types,
            )
            if geometry["type"] == "Point":
                coordinates = geometry["coordinates"]
                assert isinstance(coordinates, list | tuple)
                point_longitude = float(coordinates[0])
                point_latitude = float(coordinates[1])
                if self.longitude is not None and self.longitude != point_longitude:
                    raise ValueError("point longitude must match geometry_geojson")
                if self.latitude is not None and self.latitude != point_latitude:
                    raise ValueError("point latitude must match geometry_geojson")
                self.longitude = point_longitude
                self.latitude = point_latitude
            elif any(
                value is not None for value in (self.longitude, self.latitude, self.altitude_m)
            ):
                raise ValueError("non-point geometries cannot use legacy point coordinates")
        else:
            if any(
                value is not None
                for value in (
                    self.proposal_kind,
                    self.geometry_origin,
                    self.longitude,
                    self.latitude,
                    self.altitude_m,
                    self.geometry_geojson,
                    self.horizontal_accuracy_m,
                )
            ):
                raise ValueError("insufficient_geometry cannot contain projected coordinates")
            if not self.uncertainty_codes:
                raise ValueError("insufficient_geometry requires an uncertainty code")
        return self


class SpatialProposalTraceSourceV2(StrictAgentModel):
    batch_id: SafeIdentifier
    input_id: SafeIdentifier
    media_type: AgentMediaType
    media_sha256: Sha256Hex | None = None
    source_key: str | None = Field(default=None, max_length=255)
    source_reference_url: str | None = Field(default=None, max_length=2_048)
    license_identifier: str | None = Field(default=None, max_length=255)
    attribution: str | None = Field(default=None, max_length=500)
    trust: str | None = Field(default=None, max_length=64)


class SpatialProposalTraceAnnotationV2(StrictAgentModel):
    annotation_id: SafeIdentifier
    evidence_id: SafeIdentifier
    evidence_kind: Literal["image", "frame", "satellite_image"]
    semantic_anchor: SourceSemanticAnchorV2
    source_geometry_normalized: dict[str, object]
    model_score: float | None = Field(default=None, ge=0, le=1)


class SpatialProposalTraceWindowV2(StrictAgentModel):
    analysis_id: SafeIdentifier
    fire_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    episode_id: SafeIdentifier
    local_date: date
    window_start_at: datetime
    window_end_at: datetime
    timezone: str = Field(min_length=3, max_length=64)


class SpatialProposalTraceV2(StrictAgentModel):
    proposal_id: SafeIdentifier
    status: Literal["ground_point", "projected_geometry", "insufficient_geometry"]
    proposal_kind: SpatialProposalKindV2 | None = None
    geometry_geojson: dict[str, object] | None = None
    geometry_origin: str | None = Field(default=None, max_length=64)
    horizontal_accuracy_m: float | None = Field(default=None, gt=0)
    reference_bundle_sha256: Sha256Hex | None = None
    uncertainty_codes: list[SafeIdentifier] = Field(default_factory=list)
    review_state: Literal["PENDING", "VALIDATED", "REJECTED", "INVALIDATED"]
    version: int = Field(ge=1)
    analysis_window: SpatialProposalTraceWindowV2
    source: SpatialProposalTraceSourceV2
    annotation: SpatialProposalTraceAnnotationV2 | None = None


class FactProposalV2(StrictAgentModel):
    fact_id: SafeIdentifier
    input_id: SafeIdentifier
    category: Literal[
        "fire_activity",
        "burned_area",
        "resources",
        "evacuation",
        "access",
        "infrastructure",
        "weather",
        "other",
    ]
    fact_key: SafeIdentifier
    as_of: datetime
    evidence_kind: Literal[
        "frame", "image", "satellite_image", "transcript_segment", "article_text", "metadata"
    ]
    evidence_id: SafeIdentifier
    certainty: Literal["directly_visible", "explicitly_written", "explicitly_spoken"]
    value_number: float | None = Field(default=None, allow_inf_nan=False)
    value_text: str | None = Field(default=None, min_length=1, max_length=2_000)
    value_boolean: bool | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=1_000)
    conflict_group_id: SafeIdentifier | None = None

    @model_validator(mode="after")
    def validate_value(self) -> FactProposalV2:
        if not _is_timezone_aware(self.as_of):
            raise ValueError("fact as_of must include a timezone")
        supplied = sum(
            value is not None for value in (self.value_number, self.value_text, self.value_boolean)
        )
        if supplied != 1:
            raise ValueError("a fact requires exactly one typed value")
        if self.unit is not None and self.value_number is None:
            raise ValueError("fact units are reserved for numeric values")
        return self


class ReportSectionV2(StrictAgentModel):
    key: Literal[
        "situation",
        "observed_activity",
        "probable_activity_zone",
        "resources",
        "impacts",
        "sources_and_freshness",
        "limitations",
    ]
    heading: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=5_000)
    fact_ids: list[SafeIdentifier] = Field(default_factory=list, max_length=200)
    basis_codes: list[SafeIdentifier] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_basis(self) -> ReportSectionV2:
        if not self.fact_ids and not self.basis_codes:
            raise ValueError("report sections require a fact or an explicit basis code")
        return self


class SituationReportDraftV2(StrictAgentModel):
    title: str = Field(min_length=1, max_length=255)
    body_markdown: str = Field(min_length=1, max_length=30_000)
    sections: list[ReportSectionV2] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_sections(self) -> SituationReportDraftV2:
        keys = [section.key for section in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("report section keys must be unique")
        for section in self.sections:
            if len(section.fact_ids) != len(set(section.fact_ids)):
                raise ValueError("report section fact references must be unique")
        return self


class WorkerItemResultV2(StrictAgentModel):
    input_id: SafeIdentifier
    transcript: WorkerTranscript = Field(default_factory=WorkerTranscript)
    pixel_regions: list[WorkerPixelRegion] = Field(default_factory=list, max_length=512)
    visual_evidence_selection: list[WorkerVisualEvidenceSelection] = Field(
        default_factory=list, max_length=256
    )
    source_annotations: list[SourceAnnotationV2] = Field(default_factory=list, max_length=512)
    spatial_proposals: list[SpatialProposalV2] = Field(default_factory=list, max_length=512)
    fact_proposals: list[FactProposalV2] = Field(default_factory=list, max_length=512)
    explicit_places: list[WorkerExplicitLiteral] = Field(default_factory=list, max_length=512)
    explicit_times: list[WorkerExplicitLiteral] = Field(default_factory=list, max_length=512)
    requires_human_review: Literal[True] = True

    @model_validator(mode="after")
    def validate_references(self) -> WorkerItemResultV2:
        annotation_ids = [item.annotation_id for item in self.source_annotations]
        proposal_ids = [item.proposal_id for item in self.spatial_proposals]
        fact_ids = [item.fact_id for item in self.fact_proposals]
        for label, values in (
            ("annotation", annotation_ids),
            ("spatial proposal", proposal_ids),
            ("fact", fact_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} identifier")
        known_annotations = set(annotation_ids)
        if any(
            item.annotation_id is not None and item.annotation_id not in known_annotations
            for item in self.spatial_proposals
        ):
            raise ValueError("spatial proposal references an unknown annotation")
        if any(item.input_id != self.input_id for item in self.fact_proposals):
            raise ValueError("fact proposal input_id must match its item result")
        return self


StageRoleV2 = Literal[
    "asr",
    "fire_detection",
    "visual_grounding",
    "multimodal_extraction",
    "fire_pointing",
    "cross_view_registration",
    "spatial_projection",
    "evidence_fusion",
    "situation_report",
]
WorkerModelRoleV2 = Literal[
    "asr",
    "visual_filtering",
    "visual_grounding",
    "multimodal_extraction",
    "fire_pointing",
    "cross_view_registration",
    "consensus_judge",
]


class WorkerModelRunV2(StrictAgentModel):
    model_role: WorkerModelRoleV2
    model_id: str
    revision: str
    status: Literal["succeeded", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    load_ms: int = Field(ge=0)
    inference_ms: int = Field(ge=0)
    peak_vram_bytes: int | None = Field(default=None, ge=0)
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> WorkerModelRunV2:
        if not all(_is_timezone_aware(value) for value in (self.started_at, self.finished_at)):
            raise ValueError("model run datetimes must include a timezone")
        if self.finished_at < self.started_at:
            raise ValueError("model run finish must not precede its start")
        return self


class WorkerModelCandidateRunV2(StrictAgentModel):
    candidate_id: SafeIdentifier
    candidate_rank: int = Field(ge=1, le=8)
    stage_role: StageRoleV2
    model_role: WorkerModelRoleV2
    model_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    load_ms: int = Field(ge=0)
    inference_ms: int = Field(ge=0)
    peak_vram_bytes: int | None = Field(default=None, ge=0)
    repaired: bool = False
    output_digest: Sha256Hex | None = None
    output_payload: dict[str, JsonValue] | None = None
    error_code: SafeIdentifier | None = None

    @model_validator(mode="after")
    def validate_candidate_run(self) -> WorkerModelCandidateRunV2:
        if not all(_is_timezone_aware(value) for value in (self.started_at, self.finished_at)):
            raise ValueError("candidate run datetimes must include a timezone")
        if self.finished_at < self.started_at:
            raise ValueError("candidate run finish must not precede its start")
        if self.status == "succeeded":
            if self.output_payload is None or self.output_digest is None:
                raise ValueError("a successful candidate run requires its private output")
            if _json_sha256(self.output_payload) != self.output_digest:
                raise ValueError("candidate output digest does not match its payload")
            if self.error_code is not None:
                raise ValueError("a successful candidate run cannot contain an error")
        elif self.output_payload is not None or self.output_digest is not None:
            raise ValueError("an unsuccessful candidate run cannot publish an output")
        if self.repaired and self.status != "succeeded":
            raise ValueError("only a successful candidate run can be repaired")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("a failed candidate run requires an error code")
        return self


class WorkerConsensusResultV2(StrictAgentModel):
    consensus_id: SafeIdentifier
    stage_role: StageRoleV2
    strategy: Literal["single_with_rules", "cascade", "quorum"]
    decision: Literal["pass", "repair", "adjudicated", "abstain", "human_review"]
    candidate_ids: list[SafeIdentifier] = Field(min_length=1, max_length=8)
    selected_candidate_id: SafeIdentifier | None = None
    adjudicator_candidate_id: SafeIdentifier | None = None
    reason_codes: list[SafeIdentifier] = Field(min_length=1, max_length=16)
    successful_candidates: int = Field(ge=0, le=8)
    required_successful: int = Field(ge=1, le=8)
    agreement_score: float | None = Field(default=None, ge=0, le=1)
    agreement_threshold: float = Field(ge=0, le=1)
    downstream_allowed: bool
    comparison_digest: Sha256Hex
    comparison_payload: dict[str, JsonValue]
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_consensus(self) -> WorkerConsensusResultV2:
        if not _is_timezone_aware(self.evaluated_at):
            raise ValueError("consensus evaluation time must include a timezone")
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("consensus candidate ids must be unique")
        if self.adjudicator_candidate_id in self.candidate_ids:
            raise ValueError("the adjudicator cannot be a stage output candidate")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("consensus reason codes must be unique")
        if self.successful_candidates > len(self.candidate_ids):
            raise ValueError("successful candidate count exceeds evaluated candidates")
        if self.required_successful > len(self.candidate_ids):
            raise ValueError("required candidate count exceeds evaluated candidates")
        if _json_sha256(self.comparison_payload) != self.comparison_digest:
            raise ValueError("consensus comparison digest does not match its payload")
        if self.decision in {"pass", "repair", "adjudicated"}:
            if self.selected_candidate_id not in self.candidate_ids:
                raise ValueError("an accepted consensus requires a selected candidate")
            if not self.downstream_allowed:
                raise ValueError("an accepted consensus must allow downstream execution")
            if self.decision == "adjudicated" and self.adjudicator_candidate_id is None:
                raise ValueError("an adjudicated consensus requires its judge run")
            if self.decision != "adjudicated" and self.adjudicator_candidate_id is not None:
                raise ValueError("a direct consensus cannot reference a judge run")
        elif self.selected_candidate_id is not None or self.downstream_allowed:
            raise ValueError("a blocked consensus cannot select or release a candidate")
        return self


class WorkerStageGateV2(StrictAgentModel):
    phase: Literal["preflight", "postflight"]
    decision: Literal[
        "pass",
        "not_applicable",
        "abstain",
        "human_review",
        "failed_retryable",
        "failed_terminal",
    ]
    reason_codes: list[SafeIdentifier] = Field(min_length=1, max_length=16)
    available_capabilities: list[SafeIdentifier] = Field(default_factory=list, max_length=32)
    missing_capabilities: list[SafeIdentifier] = Field(default_factory=list, max_length=32)
    downstream_possible: bool

    @model_validator(mode="after")
    def validate_unique_values(self) -> WorkerStageGateV2:
        for values in (
            self.reason_codes,
            self.available_capabilities,
            self.missing_capabilities,
        ):
            if len(values) != len(set(values)):
                raise ValueError("stage gate values must be unique")
        return self


class WorkerStageAttemptV2(StrictAgentModel):
    attempt: int = Field(ge=1, le=2)
    kind: Literal["initial", "repair"]
    status: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    inference_ms: int = Field(ge=0)
    peak_vram_bytes: int | None = Field(default=None, ge=0)
    error_code: SafeIdentifier | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> WorkerStageAttemptV2:
        if not all(_is_timezone_aware(value) for value in (self.started_at, self.finished_at)):
            raise ValueError("stage attempt datetimes must include a timezone")
        if self.finished_at < self.started_at:
            raise ValueError("stage attempt finish must not precede its start")
        if self.kind == "initial" and self.attempt != 1:
            raise ValueError("the initial stage attempt must be attempt 1")
        if self.kind == "repair" and self.attempt != 2:
            raise ValueError("a stage repair must be attempt 2")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("a failed stage attempt requires an error_code")
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("a succeeded stage attempt cannot contain an error_code")
        return self


class WorkerStageTraceV2(StrictAgentModel):
    stage_role: StageRoleV2
    contract_id: Annotated[str, Field(pattern=r"^stage\.[a-z0-9_]+\.v[0-9]+$")]
    sequence: int = Field(ge=1, le=10)
    status: Literal["succeeded", "failed", "skipped"]
    retryable: bool
    preflight: WorkerStageGateV2
    postflight: WorkerStageGateV2 | None = None
    attempts: list[WorkerStageAttemptV2] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_trace(self) -> WorkerStageTraceV2:
        if not self.contract_id.startswith(f"stage.{self.stage_role}.v"):
            raise ValueError("stage trace contract_id must match its stage_role")
        if self.preflight.phase != "preflight":
            raise ValueError("stage trace preflight must use the preflight phase")
        if self.postflight is not None and self.postflight.phase != "postflight":
            raise ValueError("stage trace postflight must use the postflight phase")
        if [attempt.attempt for attempt in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError("stage attempts must be consecutive and ordered")
        if len(self.attempts) == 2:
            first, repair = self.attempts
            if first.status != "failed" or repair.kind != "repair":
                raise ValueError("a repair requires one failed initial attempt")
        if self.status == "skipped" and self.attempts:
            raise ValueError("a skipped stage cannot contain inference attempts")
        if self.status == "succeeded" and (
            not self.attempts or self.attempts[-1].status != "succeeded"
        ):
            raise ValueError("a succeeded stage requires a final successful attempt")
        if self.status == "failed" and self.attempts and self.attempts[-1].status != "failed":
            raise ValueError("a failed stage cannot end with a successful attempt")
        expected_retryable = self.preflight.decision == "failed_retryable" or (
            self.postflight is not None and self.postflight.decision == "failed_retryable"
        )
        if self.retryable != expected_retryable:
            raise ValueError("stage retryable must match its gate decisions")
        return self


class WorkerOutputV2(StrictAgentModel):
    schema_version: Literal["2.0"] = "2.0"
    batch_id: SafeIdentifier
    analysis_id: SafeIdentifier
    status: Literal["succeeded", "partial_failure", "failed"]
    retryable: bool
    orchestration_contract_digest: Sha256Hex
    stage_traces: list[WorkerStageTraceV2] = Field(default_factory=list, max_length=10)
    model_runs: list[WorkerModelRunV2] = Field(max_length=8)
    candidate_runs: list[WorkerModelCandidateRunV2] = Field(default_factory=list, max_length=32)
    consensus_results: list[WorkerConsensusResultV2] = Field(default_factory=list, max_length=10)
    items: list[WorkerItemResultV2] = Field(min_length=1, max_length=32)
    report_draft: SituationReportDraftV2 | None = None
    validation_errors: list[str] = Field(default_factory=list, max_length=64)
    boot_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_output_references(self) -> WorkerOutputV2:
        if self.status != "failed" and not self.stage_traces:
            raise ValueError("a non-failed worker v2 output requires stage traces")
        stage_roles = [trace.stage_role for trace in self.stage_traces]
        stage_sequences = [trace.sequence for trace in self.stage_traces]
        if len(stage_roles) != len(set(stage_roles)):
            raise ValueError("worker v2 output contains duplicate stage roles")
        if len(stage_sequences) != len(set(stage_sequences)):
            raise ValueError("worker v2 output contains duplicate stage sequences")
        if stage_sequences != list(range(1, len(stage_sequences) + 1)):
            raise ValueError("worker v2 stage traces must be consecutive and ordered")
        candidate_ids = [run.candidate_id for run in self.candidate_runs]
        consensus_ids = [result.consensus_id for result in self.consensus_results]
        consensus_stages = [result.stage_role for result in self.consensus_results]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("worker v2 output contains duplicate candidate ids")
        if len(consensus_ids) != len(set(consensus_ids)):
            raise ValueError("worker v2 output contains duplicate consensus ids")
        if len(consensus_stages) != len(set(consensus_stages)):
            raise ValueError("worker v2 output contains duplicate consensus stages")
        if bool(self.candidate_runs) != bool(self.consensus_results):
            raise ValueError("candidate runs and consensus results must be supplied together")
        candidates_by_id = {run.candidate_id: run for run in self.candidate_runs}
        covered_candidates: set[str] = set()
        traces_by_stage = {trace.stage_role: trace for trace in self.stage_traces}
        model_runs_by_role = {run.model_role: run for run in self.model_runs}
        if len(model_runs_by_role) != len(self.model_runs):
            raise ValueError("worker v2 output contains duplicate selected model roles")
        model_role_by_stage: dict[StageRoleV2, WorkerModelRoleV2] = {
            "asr": "asr",
            "fire_detection": "visual_filtering",
            "visual_grounding": "visual_grounding",
            "multimodal_extraction": "multimodal_extraction",
            "fire_pointing": "fire_pointing",
            "cross_view_registration": "cross_view_registration",
        }
        for result in self.consensus_results:
            candidates = [
                candidates_by_id.get(candidate_id) for candidate_id in result.candidate_ids
            ]
            if any(candidate is None for candidate in candidates):
                raise ValueError("consensus references an unknown candidate run")
            if any(
                candidate.stage_role != result.stage_role for candidate in candidates if candidate
            ):
                raise ValueError("consensus candidate belongs to another stage")
            adjudicator = (
                candidates_by_id.get(result.adjudicator_candidate_id)
                if result.adjudicator_candidate_id is not None
                else None
            )
            if result.adjudicator_candidate_id is not None and adjudicator is None:
                raise ValueError("consensus references an unknown adjudicator run")
            if adjudicator is not None and (
                adjudicator.stage_role != result.stage_role
                or adjudicator.model_role != "consensus_judge"
            ):
                raise ValueError("consensus adjudicator has an invalid role")
            if (
                result.decision == "adjudicated"
                and adjudicator is not None
                and adjudicator.status != "succeeded"
            ):
                raise ValueError("an adjudicated consensus requires a successful judge")
            covered_ids = set(result.candidate_ids)
            if result.adjudicator_candidate_id is not None:
                covered_ids.add(result.adjudicator_candidate_id)
            if covered_candidates.intersection(covered_ids):
                raise ValueError("a candidate run cannot belong to multiple consensus results")
            covered_candidates.update(covered_ids)
            trace = traces_by_stage.get(result.stage_role)
            if trace is None:
                raise ValueError("consensus result has no matching stage trace")
            if result.downstream_allowed and trace.status != "succeeded":
                raise ValueError("accepted consensus requires a successful stage trace")
            if not result.downstream_allowed and trace.status == "succeeded":
                raise ValueError("blocked consensus cannot have a successful stage trace")
            if result.selected_candidate_id is not None:
                selected = candidates_by_id[result.selected_candidate_id]
                if selected.status != "succeeded":
                    raise ValueError("consensus selected an unsuccessful candidate")
                model_role = model_role_by_stage.get(result.stage_role)
                selected_run = model_runs_by_role.get(model_role) if model_role else None
                if (
                    selected_run is None
                    or selected_run.model_id != selected.model_id
                    or selected_run.revision != selected.revision
                ):
                    raise ValueError("selected model run does not match consensus candidate")
        if covered_candidates != set(candidate_ids):
            raise ValueError("every candidate run must be covered by one consensus result")
        input_ids = [item.input_id for item in self.items]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("worker v2 output contains duplicate input_id values")
        all_fact_ids = [fact.fact_id for item in self.items for fact in item.fact_proposals]
        if len(all_fact_ids) != len(set(all_fact_ids)):
            raise ValueError("worker v2 output contains duplicate fact identifiers")
        if self.report_draft is not None:
            referenced = {
                fact_id for section in self.report_draft.sections for fact_id in section.fact_ids
            }
            unknown = referenced - set(all_fact_ids)
            if unknown:
                raise ValueError(f"report references an unknown fact: {sorted(unknown)[0]}")
        return self
