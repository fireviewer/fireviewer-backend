from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from fire_viewer.domain.enums import (
    EventAnalysisJobState,
    EventCandidateState,
    EvidenceAssetState,
    FireActivityEventState,
    LocalizationAttemptState,
    MalwareScanState,
    ViewpointOrigin,
)
from fire_viewer.domain.geometry_contract import validate_geojson_geometry


class EventStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceUploadFileRequest(EventStrictModel):
    file_name: str = Field(min_length=1, max_length=255)
    media_type: Literal[
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("file_name must be a plain file name")
        return value

    @model_validator(mode="after")
    def matching_file_extension(self) -> EvidenceUploadFileRequest:
        suffix = self.file_name.rsplit(".", 1)[-1].casefold() if "." in self.file_name else ""
        expected = {
            "image/jpeg": {"jpg", "jpeg"},
            "image/png": {"png"},
            "image/webp": {"webp"},
            "video/mp4": {"mp4"},
            "video/quicktime": {"mov"},
            "video/webm": {"webm"},
        }
        if suffix not in expected[self.media_type]:
            raise ValueError("file_name extension does not match media_type")
        return self


class EvidenceUploadOpenRequest(EventStrictModel):
    files: list[EvidenceUploadFileRequest] = Field(min_length=1, max_length=20)


class EvidenceUploadAssetResponse(EventStrictModel):
    evidence_asset_id: str
    pathname: str
    upload_state: EvidenceAssetState


class EvidenceUploadOpenResponse(EventStrictModel):
    upload_id: str
    upload_grant: str | None
    client_payload: str
    expires_at: datetime | None
    assets: list[EvidenceUploadAssetResponse]


class EvidenceUploadFinalizeRequest(EventStrictModel):
    evidence_asset_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("evidence_asset_ids")
    @classmethod
    def unique_assets(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_asset_ids must be unique")
        return value


class EvidenceUploadFinalizedAssetResponse(EventStrictModel):
    evidence_asset_id: str
    upload_state: EvidenceAssetState
    scan_state: MalwareScanState
    detected_media_type: str
    sha256: str


class EvidenceUploadFinalizeResponse(EventStrictModel):
    upload_id: str
    assets: list[EvidenceUploadFinalizedAssetResponse]


class ViewpointInput(EventStrictModel):
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    horizontal_accuracy_m: float = Field(gt=0, le=50_000, allow_inf_nan=False)
    altitude_m: float | None = Field(default=None, ge=-500, le=10_000, allow_inf_nan=False)
    label: str | None = Field(default=None, min_length=1, max_length=255)
    yaw_deg: float | None = Field(default=None, ge=0, lt=360, allow_inf_nan=False)
    fov_deg: float | None = Field(default=None, gt=0, lt=180, allow_inf_nan=False)
    origin: ViewpointOrigin = ViewpointOrigin.USER_PLACED


class ObservedTimeInput(EventStrictModel):
    start_at: AwareDatetime
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self) -> ObservedTimeInput:
        if self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("end_at must not precede start_at")
        return self


class EventConsentInput(EventStrictModel):
    analysis: Literal[True]
    retention: Literal[True]
    public_derivative: bool = False


class EventCandidateCreateRequest(EventStrictModel):
    idempotency_key: UUID
    incident_id: str | None = Field(default=None, pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    viewpoint: ViewpointInput
    observed_time: ObservedTimeInput
    message: str | None = Field(default=None, min_length=1, max_length=10_000)
    evidence_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    consent: EventConsentInput

    @model_validator(mode="after")
    def require_evidence(self) -> EventCandidateCreateRequest:
        if not self.message and not self.evidence_asset_ids:
            raise ValueError("at least one message or evidence asset is required")
        if len(self.evidence_asset_ids) != len(set(self.evidence_asset_ids)):
            raise ValueError("evidence_asset_ids must be unique")
        return self


class PrivateViewpointSummary(EventStrictModel):
    horizontal_accuracy_m: float
    origin: ViewpointOrigin
    has_orientation: bool
    exact_position_withheld: Literal[True] = True


class EventCandidateResponse(EventStrictModel):
    candidate_id: str
    analysis_job_id: str
    tracking_id: str
    state: EventCandidateState
    incident_id: str | None
    incident_candidate_id: str | None
    observed_start_at: datetime
    observed_end_at: datetime | None
    message: str | None
    review_message: str | None
    evidence_asset_ids: list[str]
    viewpoint: PrivateViewpointSummary
    created_at: datetime
    updated_at: datetime


class EventCandidateListResponse(EventStrictModel):
    items: list[EventCandidateResponse]
    total: int


class EventTransitionRequest(EventStrictModel):
    reason: str = Field(min_length=10, max_length=1_000)


class EventCandidateReviewRequest(EventStrictModel):
    action: Literal["reject", "request_evidence", "mark_contradictory"]
    reason: str = Field(min_length=10, max_length=1_000)


class EventCandidateAttachIncidentRequest(EventStrictModel):
    incident_id: str = Field(pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    reason: str = Field(min_length=10, max_length=1_000)


class InternalViewpointResponse(EventStrictModel):
    longitude: float
    latitude: float
    horizontal_accuracy_m: float
    altitude_m: float | None
    label: str | None
    yaw_deg: float | None
    fov_deg: float | None
    origin: ViewpointOrigin


class InternalEvidenceAssetResponse(EventStrictModel):
    evidence_asset_id: str
    file_name: str
    media_type: str
    size_bytes: int
    state: EvidenceAssetState
    scan_state: MalwareScanState


class InternalLocalizationAttemptResponse(EventStrictModel):
    attempt_id: str
    state: LocalizationAttemptState
    method: str
    model_id: str | None
    model_revision: str | None
    view_profile: str
    anchor: dict[str, Any]
    geometry: dict[str, Any] | None
    uncertainty: dict[str, Any] | None
    horizontal_uncertainty_m: float | None
    abstention_reason: str | None
    provenance: dict[str, Any]


class InternalFireActivityEventResponse(EventStrictModel):
    event_id: str
    state: FireActivityEventState
    phenomenon_kind: str
    geometry: dict[str, Any]
    uncertainty: dict[str, Any]
    method: str
    version: int


class InternalAnalysisJobResponse(EventStrictModel):
    job_id: str
    state: EventAnalysisJobState
    result_summary: dict[str, Any]
    last_error_code: str | None


class InternalEventCandidateResponse(EventStrictModel):
    candidate_id: str
    state: EventCandidateState
    incident_id: str | None
    incident_candidate_id: str | None
    owner_subject: str
    observed_start_at: datetime
    observed_end_at: datetime | None
    message: str | None
    review_message: str | None
    review_context: dict[str, Any]
    state_history: list[dict[str, Any]]
    viewpoint: InternalViewpointResponse
    evidence_assets: list[InternalEvidenceAssetResponse]
    localization_attempts: list[InternalLocalizationAttemptResponse]
    fire_activity_events: list[InternalFireActivityEventResponse]
    analysis_job: InternalAnalysisJobResponse
    created_at: datetime
    updated_at: datetime


class InternalEventCandidateListResponse(EventStrictModel):
    items: list[InternalEventCandidateResponse]
    total: int
    limit: int
    offset: int


class EventCandidateMutationResponse(EventStrictModel):
    candidate_id: str
    state: EventCandidateState
    version: int


class FireActivityEventMutationResponse(EventStrictModel):
    event_id: str
    state: FireActivityEventState
    version: int


class PublicFireActivityEventResponse(EventStrictModel):
    event_id: str
    state: Literal["EDITOR_PUBLISHED"]
    phenomenon_kind: str
    observed_start_at: datetime
    observed_end_at: datetime | None
    geometry: dict[str, Any]
    uncertainty: dict[str, Any]
    method: str
    publication_revision: int


class PublicIncidentEventTimelineResponse(EventStrictModel):
    incident_id: str
    revision: int
    events: list[PublicFireActivityEventResponse]


class EventWorkerObservedTime(EventStrictModel):
    start_at: AwareDatetime
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self) -> EventWorkerObservedTime:
        if self.end_at is not None and self.end_at < self.start_at:
            raise ValueError("worker event end must not precede its start")
        return self


class EventWorkerSector(EventStrictModel):
    bearing_deg: float = Field(ge=0, lt=360)
    angular_uncertainty_deg: float = Field(gt=0, le=180)
    distance_min_m: float = Field(ge=0)
    distance_max_m: float | None = Field(default=None, gt=0)


class EventWorkerPerceptionAnchor(EventStrictModel):
    anchor_id: str = Field(min_length=1, max_length=96)
    evidence_asset_id: str = Field(min_length=1, max_length=96)
    phenomenon: Literal["active_fire_point", "visible_fire_front", "smoke_column_base"]
    source_point_normalized: tuple[float, float] | None = None
    source_geometry_normalized: dict[str, Any] | None = None
    model_id: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(min_length=1, max_length=255)
    model_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_anchor(self) -> EventWorkerPerceptionAnchor:
        if (self.source_point_normalized is None) == (self.source_geometry_normalized is None):
            raise ValueError("a worker perception anchor requires one pixel point or geometry")
        if self.source_point_normalized is not None and any(
            coordinate < 0 or coordinate > 1 for coordinate in self.source_point_normalized
        ):
            raise ValueError("worker perception points must be normalized")
        if self.source_geometry_normalized is not None:
            allowed = (
                {"LineString", "MultiLineString"}
                if self.phenomenon == "visible_fire_front"
                else {"Point"}
            )
            validate_geojson_geometry(
                self.source_geometry_normalized,
                allowed_types=allowed,
                normalized=True,
            )
        return self


class EventWorkerSpatialEvidence(EventStrictModel):
    anchor_id: str = Field(min_length=1, max_length=96)
    status: Literal["projected", "insufficient_geometry"]
    method: (
        Literal[
            "camera_raycast",
            "triangulation",
            "viewpoint_sector",
            "cross_view_raycast",
            "explicit_source_geometry",
        ]
        | None
    ) = None
    geometry_geojson: dict[str, Any] | None = None
    horizontal_accuracy_m: float | None = Field(default=None, gt=0, le=100_000)
    direction_uncertainty_deg: float | None = Field(default=None, ge=0, le=180)
    distance_uncertainty_m: float | None = Field(default=None, ge=0, le=100_000)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    reference_revision: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_result(self) -> EventWorkerSpatialEvidence:
        if self.status == "projected":
            if (
                self.method is None
                or self.geometry_geojson is None
                or self.horizontal_accuracy_m is None
            ):
                raise ValueError("projected worker spatial evidence is incomplete")
            validate_geojson_geometry(self.geometry_geojson)
        elif not self.reason_codes:
            raise ValueError("insufficient worker spatial evidence requires reason codes")
        return self


class EventWorkerLocalizationAttempt(EventStrictModel):
    attempt_id: str = Field(min_length=1, max_length=96)
    anchor_id: str | None = Field(default=None, max_length=128)
    phenomenon: Literal["active_fire_point", "visible_fire_front", "smoke_origin"] | None = None
    status: Literal["localized", "sector", "abstained"]
    method: (
        Literal[
            "camera_raycast",
            "triangulation",
            "viewpoint_sector",
            "cross_view_raycast",
            "explicit_source_geometry",
        ]
        | None
    ) = None
    geometry_geojson: dict[str, Any] | None = None
    sector: EventWorkerSector | None = None
    horizontal_accuracy_m: float | None = Field(default=None, gt=0, le=100_000)
    direction_uncertainty_deg: float | None = Field(default=None, ge=0, le=180)
    distance_uncertainty_m: float | None = Field(default=None, ge=0, le=100_000)
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    model_id: str | None = Field(default=None, max_length=500)
    model_revision: str | None = Field(default=None, max_length=255)
    reference_revision: str | None = Field(default=None, max_length=255)
    shadow_only: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> EventWorkerLocalizationAttempt:
        if self.status == "localized":
            if (
                self.geometry_geojson is None
                or self.horizontal_accuracy_m is None
                or self.phenomenon is None
                or self.method in {None, "viewpoint_sector"}
                or self.model_id is None
                or self.model_revision is None
                or self.reference_revision is None
            ):
                raise ValueError(
                    "localized worker attempts require sourced geometry, accuracy and revisions"
                )
            validate_geojson_geometry(self.geometry_geojson)
        elif self.status == "sector":
            if (
                self.sector is None
                or self.method != "viewpoint_sector"
                or self.phenomenon is None
                or self.model_id is None
                or self.model_revision is None
            ):
                raise ValueError("sector worker attempts require sourced sector parameters")
        elif not self.reason_codes:
            raise ValueError("worker abstentions require reason codes")
        if self.method == "cross_view_raycast" and not self.shadow_only:
            raise ValueError("cross-view attempts must remain shadow-only")
        if self.shadow_only and self.method != "cross_view_raycast":
            raise ValueError("only cross-view attempts can be shadow-only")
        return self


class EventWorkerActivityProposal(EventStrictModel):
    proposal_id: str = Field(min_length=1, max_length=96)
    attempt_id: str = Field(min_length=1, max_length=96)
    phenomenon: Literal["active_fire_point", "visible_fire_front", "smoke_origin"]
    observed_time: EventWorkerObservedTime
    geometry_geojson: dict[str, Any]
    horizontal_accuracy_m: float = Field(gt=0, le=100_000)
    status: Literal["DRAFT"]
    requires_human_review: Literal[True]

    @model_validator(mode="after")
    def validate_geometry(self) -> EventWorkerActivityProposal:
        allowed = (
            {"LineString", "MultiLineString"}
            if self.phenomenon == "visible_fire_front"
            else {"Point"}
        )
        validate_geojson_geometry(self.geometry_geojson, allowed_types=allowed)
        return self


class EventWorkerOutput(EventStrictModel):
    schema_version: Literal["event-result-2.0"]
    candidate_id: str = Field(min_length=1, max_length=96)
    status: Literal["needs_review", "abstained", "failed"]
    view_profile: (
        Literal[
            "ground_wide_known_viewpoint",
            "ground_wide_named_viewpoint",
            "ground_distant_known_viewpoint",
            "ground_close_known_viewpoint",
            "ground_tight_known_viewpoint",
        ]
        | None
    )
    perception_anchors: list[EventWorkerPerceptionAnchor] = Field(max_length=512)
    spatial_evidence: list[EventWorkerSpatialEvidence] = Field(max_length=512)
    localization_attempts: list[EventWorkerLocalizationAttempt] = Field(max_length=512)
    event_proposals: list[EventWorkerActivityProposal] = Field(max_length=512)
    independent_external_families: list[str] = Field(max_length=256)
    contradictions: list[tuple[str, str]] = Field(max_length=256)
    reason_codes: list[str] = Field(max_length=128)
    requires_human_review: Literal[True]

    @model_validator(mode="after")
    def references_are_closed(self) -> EventWorkerOutput:
        anchor_ids = [anchor.anchor_id for anchor in self.perception_anchors]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("worker perception anchor identifiers must be unique")
        anchor_by_id = {anchor.anchor_id: anchor for anchor in self.perception_anchors}
        spatial_ids = [item.anchor_id for item in self.spatial_evidence]
        if len(spatial_ids) != len(set(spatial_ids)):
            raise ValueError("worker spatial evidence identifiers must be unique")
        if not set(spatial_ids).issubset(anchor_ids):
            raise ValueError("worker spatial evidence references an unknown anchor")
        spatial_by_anchor = {item.anchor_id: item for item in self.spatial_evidence}
        attempt_ids = [attempt.attempt_id for attempt in self.localization_attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("worker localization attempt identifiers must be unique")
        proposal_ids = [proposal.proposal_id for proposal in self.event_proposals]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("worker event proposal identifiers must be unique")
        attempt_by_id = {attempt.attempt_id: attempt for attempt in self.localization_attempts}
        if not {proposal.attempt_id for proposal in self.event_proposals}.issubset(attempt_ids):
            raise ValueError("worker event proposal references an unknown localization attempt")
        if self.status != "needs_review" and self.event_proposals:
            raise ValueError("abstained or failed worker outputs cannot contain proposals")
        phenomenon_map = {
            "active_fire_point": "active_fire_point",
            "visible_fire_front": "visible_fire_front",
            "smoke_column_base": "smoke_origin",
        }
        for attempt in self.localization_attempts:
            if attempt.anchor_id is None:
                if attempt.status in {"localized", "sector"}:
                    raise ValueError("spatial worker attempts require a perception anchor")
                continue
            anchor = anchor_by_id.get(attempt.anchor_id)
            if anchor is None:
                raise ValueError("worker attempt references an unknown perception anchor")
            if (
                phenomenon_map[anchor.phenomenon] != attempt.phenomenon
                or anchor.model_id != attempt.model_id
                or anchor.model_revision != attempt.model_revision
            ):
                raise ValueError("worker attempt differs from its perception anchor")
            if attempt.status == "localized":
                spatial = spatial_by_anchor.get(attempt.anchor_id)
                if spatial is None or spatial.status != "projected":
                    raise ValueError("localized worker attempt has no projected spatial evidence")
                if (
                    spatial.method != attempt.method
                    or spatial.geometry_geojson != attempt.geometry_geojson
                    or spatial.horizontal_accuracy_m != attempt.horizontal_accuracy_m
                    or spatial.reference_revision != attempt.reference_revision
                    or spatial.direction_uncertainty_deg != attempt.direction_uncertainty_deg
                    or spatial.distance_uncertainty_m != attempt.distance_uncertainty_m
                ):
                    raise ValueError("worker attempt differs from its spatial evidence")
        for proposal in self.event_proposals:
            attempt = attempt_by_id[proposal.attempt_id]
            if attempt.status != "localized" or attempt.shadow_only:
                raise ValueError("worker proposals require a non-shadow localized attempt")
            if (
                attempt.phenomenon != proposal.phenomenon
                or attempt.geometry_geojson != proposal.geometry_geojson
                or attempt.horizontal_accuracy_m != proposal.horizontal_accuracy_m
            ):
                raise ValueError("worker proposals must exactly match their localization attempt")
        if self.status == "needs_review" and not (
            self.event_proposals
            or any(attempt.status == "sector" for attempt in self.localization_attempts)
        ):
            raise ValueError("needs_review worker outputs require a proposal or sector")
        if self.status in {"abstained", "failed"} and any(
            attempt.status in {"localized", "sector"} for attempt in self.localization_attempts
        ):
            raise ValueError("abstained or failed outputs cannot contain spatial results")
        return self
