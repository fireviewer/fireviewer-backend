from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fire_viewer.domain.enums import PublicContributionKind, PublicContributionState


class StrictContributionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicContributionLocation(StrictContributionModel):
    mode: Literal["place", "device", "manual"]
    label: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    uncertainty_m: float | None = Field(default=None, gt=0, le=100_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_location(self) -> PublicContributionLocation:
        if self.mode == "place" and (self.label is None or len(self.label.strip()) < 2):
            raise ValueError("a place name or landmark is required")
        if self.mode != "place" and (self.latitude is None or self.longitude is None):
            raise ValueError("latitude and longitude are required for this location mode")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class PublicContributionObservation(StrictContributionModel):
    observation_type: str = Field(min_length=2, max_length=128)
    observed_at: datetime
    direct_observation: bool
    description: str = Field(min_length=20, max_length=4_000)


class PublicContributionMediaDeclaration(StrictContributionModel):
    filename: str = Field(min_length=1, max_length=500)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: int = Field(gt=0, le=16_777_216)
    captured_at: datetime | None = None
    direction: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_capture_time(self) -> PublicContributionMediaDeclaration:
        if self.captured_at is not None and (
            self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None
        ):
            raise ValueError("media capture date must include a timezone")
        return self


class PublicContributionConsents(StrictContributionModel):
    private_analysis: Literal[True]
    retain_evidence: bool = False
    public_display: bool = False
    spatial_display: bool = False


class PublicContributionOpenRequest(StrictContributionModel):
    kind: PublicContributionKind
    fire_id: str | None = Field(default=None, pattern=r"^FR-[0-9A-Z]{2,3}-[0-9]{5}$")
    location: PublicContributionLocation
    observation: PublicContributionObservation
    media: PublicContributionMediaDeclaration | None = None
    consents: PublicContributionConsents
    contact_email: str | None = Field(default=None, min_length=3, max_length=320)

    @model_validator(mode="after")
    def validate_kind(self) -> PublicContributionOpenRequest:
        if self.kind == PublicContributionKind.INCIDENT_EVIDENCE and self.fire_id is None:
            raise ValueError("incident evidence requires fire_id")
        if self.kind == PublicContributionKind.NEW_FIRE and self.fire_id is not None:
            raise ValueError("a new fire must not already reference an incident")
        return self


class PublicContributionUploadGrant(StrictContributionModel):
    package_id: str
    pathname_prefix: str
    upload_grant: str
    expires_at: datetime
    maximum_file_size_bytes: int = Field(gt=0)
    allowed_content_types: list[str]


class PublicContributionOpenResponse(StrictContributionModel):
    contribution_id: str
    state: PublicContributionState
    tracking_token: str
    upload: PublicContributionUploadGrant | None = None
    purge_after: datetime
    replayed: bool = False


class PublicContributionStatus(StrictContributionModel):
    contribution_id: str
    kind: PublicContributionKind
    fire_id: str | None
    state: PublicContributionState
    received_at: datetime | None
    reviewed_at: datetime | None
    review_reason: str | None
    purge_after: datetime
    media_count: int = Field(ge=0)
    location_label: str | None
    observation_type: str
    observed_at: datetime
    version: int = Field(ge=1)


class PublicContributionEnvelope(StrictContributionModel):
    contribution: PublicContributionStatus
    trace_id: str


class AdminPublicContribution(PublicContributionStatus):
    description: str
    direct_observation: bool
    location: PublicContributionLocation
    consent_scopes: list[str]
    contact_provided: bool
    private_media_urls: list[str]


class AdminContributionProposal(StrictContributionModel):
    """Safe, decision-oriented projection of one private agent proposal."""

    proposal_id: str = Field(min_length=1, max_length=128)
    kind: Literal["fact", "spatial", "report"]
    state: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=1_000)
    confidence: str | None = Field(default=None, max_length=64)
    observed_at: datetime | None = None
    proposed_at: datetime
    episode_id: str | None = Field(default=None, max_length=32)
    version: int = Field(ge=1)


class AdminContributionGalleryState(StrictContributionModel):
    eligible: bool
    reason: str | None = Field(default=None, max_length=500)
    gallery_item_id: str | None = Field(default=None, max_length=96)
    state: str | None = Field(default=None, max_length=32)


class AdminPublicContributionDetail(AdminPublicContribution):
    """Private proof dossier. It intentionally excludes contacts, tokens and job payloads."""

    episode_id: str | None = Field(default=None, max_length=32)
    analysis_state: Literal["not_scheduled", "scheduled", "running", "completed", "blocked"]
    proposals: list[AdminContributionProposal] = Field(default_factory=list, max_length=500)
    gallery: AdminContributionGalleryState


class AdminPublicContributionDetailEnvelope(StrictContributionModel):
    contribution: AdminPublicContributionDetail
    trace_id: str


class AdminContributionProposalReviewRequest(StrictContributionModel):
    action: Literal["validate", "reject", "invalidate"]
    reason: str = Field(min_length=10, max_length=500)
    expected_version: int = Field(ge=1)


class AdminContributionGalleryProposalRequest(StrictContributionModel):
    """Editorial metadata completed by an operator; media stays private until review."""

    title: str = Field(min_length=1, max_length=255)
    caption: str | None = Field(default=None, max_length=1_000)
    alt_text: str = Field(min_length=1, max_length=500)
    credit: str | None = Field(default=None, max_length=255)
    license_label: str | None = Field(default=None, max_length=255)
    proposal_reason: str = Field(min_length=10, max_length=1_000)


class AdminPublicContributionListResponse(StrictContributionModel):
    contributions: list[AdminPublicContribution] = Field(default_factory=list)


class AdminPublicContributionReviewRequest(StrictContributionModel):
    state: Literal[PublicContributionState.ACCEPTED, PublicContributionState.REJECTED]
    reason: str = Field(min_length=10, max_length=1_000)
    expected_version: int = Field(ge=1)


class AdminPublicContributionEnvelope(StrictContributionModel):
    contribution: AdminPublicContribution
    trace_id: str
