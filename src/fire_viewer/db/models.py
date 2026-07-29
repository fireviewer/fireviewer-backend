from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fire_viewer.core.time import utcnow
from fire_viewer.db.base import Base
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    ActorType,
    AgentAnalysisState,
    AgentBatchPriority,
    AgentBatchState,
    AgentBatchType,
    AgentConsentBasis,
    AgentConsentState,
    AgentDeadLetterState,
    AgentDispatchState,
    AgentMediaType,
    AgentModelRunState,
    AgentProposalReviewState,
    AgentReportReviewState,
    AgentReviewState,
    AgentSourceCandidateState,
    AgentSourcePackageKind,
    AgentSourcePackageState,
    AgentSourceResearchState,
    AgentValidationCampaignDayState,
    AssetLod,
    AssetState,
    EvidenceSpatialMode,
    IncidentMarkerReviewState,
    IncidentStatus,
    JobKind,
    JobState,
    MatchDecision,
    PublicContributionKind,
    PublicContributionState,
    PublicReportCategory,
    PublicReportState,
    PublicVisibility,
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


def enum_column(enum_type: type, *, name: str) -> Enum:
    return Enum(enum_type, name=name, native_enum=False, validate_strings=True)


def sha256_hex_check(column: str) -> str:
    """Portable SQL predicate for a lowercase hexadecimal SHA-256 digest."""

    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Source(Base, TimestampMixin):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    source_type: Mapped[SourceType] = mapped_column(
        enum_column(SourceType, name="source_type"), nullable=False
    )
    trust: Mapped[SourceTrust] = mapped_column(
        enum_column(SourceTrust, name="source_trust"), nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(String(255))
    public_display_name: Mapped[str | None] = mapped_column(String(255))
    public_license: Mapped[str | None] = mapped_column(String(255))
    public_reference_url: Mapped[str | None] = mapped_column(String(2_048))
    public_transformations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    credential_hash: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    observations: Mapped[list[Observation]] = relationship(back_populates="source")


class FireIdCounter(Base):
    __tablename__ = "fire_id_counter"

    territory_code: Mapped[str] = mapped_column(String(3), primary_key=True)
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (CheckConstraint("next_sequence >= 1", name="ck_fire_id_counter_positive"),)


class IncidentSeries(Base, TimestampMixin):
    __tablename__ = "incident_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fire_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    territory_code: Mapped[str] = mapped_column(String(3), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_name: Mapped[str | None] = mapped_column(String(255))
    reference_lon: Mapped[float] = mapped_column(Float, nullable=False)
    reference_lat: Mapped[float] = mapped_column(Float, nullable=False)
    horizontal_uncertainty_m: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_min_lon: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_max_lon: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_min_lat: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_max_lat: Mapped[float] = mapped_column(Float, nullable=False)
    public_visibility: Mapped[PublicVisibility] = mapped_column(
        enum_column(PublicVisibility, name="public_visibility"),
        nullable=False,
        default=PublicVisibility.LIMITED,
    )
    public_note: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    episodes: Mapped[list[Episode]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", order_by="Episode.ordinal"
    )
    observations: Mapped[list[Observation]] = relationship(
        back_populates="attached_incident",
        foreign_keys="Observation.attached_incident_id",
    )
    proposed_observations: Mapped[list[Observation]] = relationship(
        back_populates="proposed_incident",
        foreign_keys="Observation.proposed_incident_id",
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="incident")
    manifest_revisions: Mapped[list[ManifestRevision]] = relationship(back_populates="incident")
    archive_snapshot: Mapped[ZoneArchiveSnapshot | None] = relationship(
        back_populates="incident", uselist=False
    )
    public_reports: Mapped[list[IncidentPublicReport]] = relationship(back_populates="incident")
    public_contributions: Mapped[list[PublicContributionSubmission]] = relationship(
        back_populates="incident"
    )
    official_resources: Mapped[list[IncidentOfficialResource]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    operational_information: Mapped[list[IncidentOperationalInformation]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    gallery_items: Mapped[list[IncidentGalleryItem]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    bulletin_entries: Mapped[list[IncidentBulletinEntry]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("territory_code", "sequence", name="uq_incident_territory_sequence"),
        CheckConstraint("reference_lon >= -180 AND reference_lon <= 180", name="ck_incident_lon"),
        CheckConstraint("reference_lat >= -90 AND reference_lat <= 90", name="ck_incident_lat"),
        CheckConstraint("horizontal_uncertainty_m > 0", name="ck_incident_uncertainty"),
        CheckConstraint("version >= 1", name="ck_incident_version"),
        Index("ix_incident_bbox", "bbox_min_lon", "bbox_max_lon", "bbox_min_lat", "bbox_max_lat"),
    )


class Episode(Base, TimestampMixin):
    __tablename__ = "episode"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[IncidentStatus] = mapped_column(
        enum_column(IncidentStatus, name="incident_status"), nullable=False
    )
    verification_state: Mapped[VerificationState] = mapped_column(
        enum_column(VerificationState, name="episode_verification_state"),
        nullable=False,
        default=VerificationState.UNVERIFIED,
    )
    corroborating_source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_basis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_area_ha: Mapped[float | None] = mapped_column(Float)
    evacuation_established: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evacuation_basis: Mapped[str | None] = mapped_column(String(1_000))
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confidence_policy: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(back_populates="episodes")
    observations: Mapped[list[Observation]] = relationship(
        back_populates="attached_episode", foreign_keys="Observation.attached_episode_id"
    )
    proposed_observations: Mapped[list[Observation]] = relationship(
        back_populates="proposed_episode", foreign_keys="Observation.proposed_episode_id"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="episode")
    operational_information: Mapped[list[IncidentOperationalInformation]] = relationship(
        back_populates="episode"
    )
    gallery_items: Mapped[list[IncidentGalleryItem]] = relationship(back_populates="episode")
    bulletin_entries: Mapped[list[IncidentBulletinEntry]] = relationship(back_populates="episode")

    __table_args__ = (
        UniqueConstraint("incident_id", "episode_id", name="uq_episode_public_id"),
        UniqueConstraint("incident_id", "ordinal", name="uq_episode_ordinal"),
        CheckConstraint("ordinal >= 1", name="ck_episode_ordinal"),
        CheckConstraint("corroborating_source_count >= 0", name="ck_episode_corroboration_count"),
        CheckConstraint(
            "estimated_area_ha IS NULL OR estimated_area_ha >= 0",
            name="ck_episode_estimated_area",
        ),
        CheckConstraint(
            "evacuation_established = 0 OR evacuation_basis IS NOT NULL",
            name="ck_episode_evacuation_basis",
        ),
        CheckConstraint("version >= 1", name="ck_episode_version"),
        Index(
            "uq_episode_one_current",
            "incident_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class Observation(Base):
    __tablename__ = "observation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(16), nullable=False, default="Point")
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    vertical_datum: Mapped[str | None] = mapped_column(String(128))
    horizontal_uncertainty_m: Mapped[float] = mapped_column(Float, nullable=False)
    territory_code: Mapped[str] = mapped_column(String(3), nullable=False)
    toponyms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    canonical_name_hint: Mapped[str | None] = mapped_column(String(255))
    evidence_hash: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    evidence_license: Mapped[str] = mapped_column(String(255), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(512))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_state: Mapped[VerificationState] = mapped_column(
        enum_column(VerificationState, name="verification_state"), nullable=False
    )
    public_spatial_mode: Mapped[EvidenceSpatialMode] = mapped_column(
        enum_column(EvidenceSpatialMode, name="evidence_spatial_mode"),
        nullable=False,
        default=EvidenceSpatialMode.WITHHELD,
    )
    raw_purge_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    raw_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_retention_hold_reason: Mapped[str | None] = mapped_column(String(500))
    attached_incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True
    )
    attached_episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    proposed_incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True
    )
    proposed_episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    match_decision: Mapped[MatchDecision] = mapped_column(
        enum_column(MatchDecision, name="match_decision"), nullable=False
    )
    match_score: Mapped[float | None] = mapped_column(Float)
    margin_to_second_candidate: Mapped[float | None] = mapped_column(Float)
    match_factors: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="observations")
    attached_incident: Mapped[IncidentSeries | None] = relationship(
        back_populates="observations", foreign_keys=[attached_incident_id]
    )
    attached_episode: Mapped[Episode | None] = relationship(
        back_populates="observations", foreign_keys=[attached_episode_id]
    )
    proposed_incident: Mapped[IncidentSeries | None] = relationship(
        back_populates="proposed_observations", foreign_keys=[proposed_incident_id]
    )
    proposed_episode: Mapped[Episode | None] = relationship(
        back_populates="proposed_observations", foreign_keys=[proposed_episode_id]
    )

    __table_args__ = (
        CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_observation_lon"),
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_observation_lat"),
        CheckConstraint("horizontal_uncertainty_m > 0", name="ck_observation_uncertainty"),
        CheckConstraint("version >= 1", name="ck_observation_version"),
        CheckConstraint(
            "(attached_incident_id IS NULL AND attached_episode_id IS NULL) "
            "OR (attached_incident_id IS NOT NULL AND attached_episode_id IS NOT NULL)",
            name="ck_observation_attached_pair_complete",
        ),
        CheckConstraint(
            "(proposed_incident_id IS NULL AND proposed_episode_id IS NULL) "
            "OR (proposed_incident_id IS NOT NULL AND proposed_episode_id IS NOT NULL)",
            name="ck_observation_proposed_pair_complete",
        ),
        Index("ix_observation_episode_time", "attached_episode_id", "observed_at"),
    )


class IncidentPublicReport(Base):
    """Anonymous, moderated public correction request. No network identity is persisted."""

    __tablename__ = "incident_public_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    category: Mapped[PublicReportCategory] = mapped_column(
        enum_column(PublicReportCategory, name="public_report_category"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    origin_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    state: Mapped[PublicReportState] = mapped_column(
        enum_column(PublicReportState, name="public_report_state"),
        nullable=False,
        default=PublicReportState.PENDING,
        index=True,
    )
    closure_reason: Mapped[str | None] = mapped_column(String(500))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(back_populates="public_reports")

    __table_args__ = (
        UniqueConstraint(
            "incident_id",
            "origin_fingerprint",
            "content_hash",
            "submitted_day",
            name="uq_public_report_origin_content_day",
        ),
        CheckConstraint("version >= 1", name="ck_public_report_version"),
        CheckConstraint(
            sha256_hex_check("origin_fingerprint"), name="ck_public_report_origin_hash"
        ),
        CheckConstraint(sha256_hex_check("content_hash"), name="ck_public_report_content_hash"),
        Index("ix_public_report_origin_day", "origin_fingerprint", "submitted_day"),
    )


class PublicContributionSubmission(Base, TimestampMixin):
    """Anonymous public evidence kept private until an operator reviews it."""

    __tablename__ = "public_contribution_submission"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contribution_id: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, index=True
    )
    kind: Mapped[PublicContributionKind] = mapped_column(
        enum_column(PublicContributionKind, name="public_contribution_kind"), nullable=False
    )
    state: Mapped[PublicContributionState] = mapped_column(
        enum_column(PublicContributionState, name="public_contribution_state"),
        nullable=False,
        default=PublicContributionState.OPEN,
        index=True,
    )
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True
    )
    source_package_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_source_package.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    submission_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    consent_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    contact_reference_hash: Mapped[str | None] = mapped_column(String(64))
    origin_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    submitted_day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    purge_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_reason: Mapped[str | None] = mapped_column(String(1_000))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries | None] = relationship(back_populates="public_contributions")
    source_package: Mapped[AgentSourcePackage | None] = relationship(
        back_populates="public_contribution", foreign_keys=[source_package_id]
    )
    gallery_items: Mapped[list[IncidentGalleryItem]] = relationship(
        back_populates="source_contribution",
        foreign_keys="IncidentGalleryItem.source_contribution_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "origin_fingerprint",
            "submitted_day",
            "idempotency_key",
            name="uq_public_contribution_origin_day_idempotency",
        ),
        CheckConstraint(
            sha256_hex_check("origin_fingerprint"), name="ck_public_contribution_origin_hash"
        ),
        CheckConstraint(
            sha256_hex_check("request_hash"), name="ck_public_contribution_request_hash"
        ),
        CheckConstraint(
            sha256_hex_check("tracking_token_hash"),
            name="ck_public_contribution_tracking_hash",
        ),
        CheckConstraint(
            "contact_reference_hash IS NULL OR ("
            + sha256_hex_check("contact_reference_hash")
            + ")",
            name="ck_public_contribution_contact_hash",
        ),
        CheckConstraint("version >= 1", name="ck_public_contribution_version"),
    )


class AgentScheduleRun(Base, TimestampMixin):
    """Durable lease and watermark for low-frequency private agent intake jobs."""

    __tablename__ = "agent_schedule_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class IncidentOfficialResource(Base, TimestampMixin):
    """A human-reviewed official link. Candidate provenance is never public."""

    __tablename__ = "incident_official_resource"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    publisher: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PROPOSED", index=True)
    source_reference_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    proposal_reason: Mapped[str] = mapped_column(String(1_000), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(back_populates="official_resources")
    episode: Mapped[Episode | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('safety', 'press', 'official_update', 'authority')",
            name="ck_official_resource_kind",
        ),
        CheckConstraint(
            "state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')",
            name="ck_official_resource_state",
        ),
        CheckConstraint("url LIKE 'https://%'", name="ck_official_resource_https"),
        CheckConstraint(
            "source_reference_url LIKE 'https://%'",
            name="ck_official_resource_source_https",
        ),
        CheckConstraint("version >= 1", name="ck_official_resource_version"),
        Index("ix_official_resource_incident_state", "incident_id", "state"),
    )


class IncidentOperationalInformation(Base, TimestampMixin):
    """Qualified operational information, private until an authorised human publishes it."""

    __tablename__ = "incident_operational_information"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    information_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    value_text: Mapped[str | None] = mapped_column(String(500))
    value_number: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(64))
    locality: Mapped[str | None] = mapped_column(String(255))
    authority_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    authority_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PROPOSED", index=True)
    source_reference_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    proposal_reason: Mapped[str] = mapped_column(String(1_000), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(back_populates="operational_information")
    episode: Mapped[Episode | None] = relationship(back_populates="operational_information")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('affected_place', 'evacuated_people', 'mobilized_personnel', "
            "'mobilized_vehicles', 'road_status', 'access_status', 'shelter', "
            "'public_service', 'utility', 'other')",
            name="ck_operational_information_kind",
        ),
        CheckConstraint(
            "(value_text IS NOT NULL) OR (value_number IS NOT NULL)",
            name="ck_operational_information_value",
        ),
        CheckConstraint(
            "value_number IS NULL OR value_number >= 0",
            name="ck_operational_information_value_number",
        ),
        CheckConstraint(
            "authority_kind IN ('mairie', 'prefecture', 'police')",
            name="ck_operational_information_authority",
        ),
        CheckConstraint(
            "state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')",
            name="ck_operational_information_state",
        ),
        CheckConstraint("source_url LIKE 'https://%'", name="ck_operational_information_https"),
        CheckConstraint(
            "source_reference_url LIKE 'https://%'",
            name="ck_operational_information_source_https",
        ),
        CheckConstraint("version >= 1", name="ck_operational_information_version"),
        Index("ix_operational_information_incident_state", "incident_id", "state"),
    )


class IncidentGalleryItem(Base, TimestampMixin):
    """Editorial gallery item, reviewed independently from agent media and contributions."""

    __tablename__ = "incident_gallery_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gallery_item_id: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, index=True
    )
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    source_contribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("public_contribution_submission.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    caption: Mapped[str | None] = mapped_column(String(1_000))
    alt_text: Mapped[str] = mapped_column(String(500), nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(2_048))
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="image")
    credit: Mapped[str | None] = mapped_column(String(255))
    license_label: Mapped[str | None] = mapped_column(String(255))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PROPOSED", index=True)
    # A contribution-backed proposal has no public provenance URL until an
    # editor materialises a safe editorial copy. Publication remains blocked
    # until both the media and source reference are present.
    source_reference_url: Mapped[str | None] = mapped_column(String(2_048))
    proposal_reason: Mapped[str] = mapped_column(String(1_000), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(back_populates="gallery_items")
    episode: Mapped[Episode | None] = relationship(back_populates="gallery_items")
    source_contribution: Mapped[PublicContributionSubmission | None] = relationship(
        back_populates="gallery_items", foreign_keys=[source_contribution_id]
    )

    __table_args__ = (
        CheckConstraint("media_kind IN ('image', 'video')", name="ck_gallery_item_media_kind"),
        CheckConstraint(
            "state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')",
            name="ck_gallery_item_state",
        ),
        CheckConstraint(
            "media_url IS NULL OR media_url LIKE 'https://%'", name="ck_gallery_item_media_https"
        ),
        CheckConstraint(
            "state != 'PUBLISHED' OR media_url IS NOT NULL",
            name="ck_gallery_item_published_media",
        ),
        CheckConstraint(
            "source_reference_url IS NULL OR source_reference_url LIKE 'https://%'",
            name="ck_gallery_item_source_https",
        ),
        CheckConstraint(
            "state != 'PUBLISHED' OR source_reference_url IS NOT NULL",
            name="ck_gallery_item_published_source",
        ),
        CheckConstraint("version >= 1", name="ck_gallery_item_version"),
        Index("ix_gallery_item_incident_state", "incident_id", "state"),
    )


class IncidentBulletinEntry(Base, TimestampMixin):
    """Administrator-verified public fact or timeline entry."""

    __tablename__ = "incident_bulletin_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    body: Mapped[str] = mapped_column(String(1_000), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PUBLISHED", index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    retired_by: Mapped[str | None] = mapped_column(String(255))
    retirement_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(back_populates="bulletin_entries")
    episode: Mapped[Episode | None] = relationship(back_populates="bulletin_entries")
    source: Mapped[Source] = relationship()

    __table_args__ = (
        CheckConstraint("kind IN ('fact', 'timeline')", name="ck_bulletin_entry_kind"),
        CheckConstraint("state IN ('PUBLISHED', 'RETIRED')", name="ck_bulletin_entry_state"),
        CheckConstraint("version >= 1", name="ck_bulletin_entry_version"),
        Index("ix_bulletin_entry_incident_state", "incident_id", "state"),
    )


class SpatialZone(Base, TimestampMixin):
    """Stable identity for a reusable, local rural 3D zone."""

    __tablename__ = "spatial_zone"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(255))

    revisions: Mapped[list[SpatialZoneRevision]] = relationship(
        back_populates="zone", order_by="SpatialZoneRevision.revision"
    )
    publications: Mapped[list[ZonePublication]] = relationship(back_populates="zone")
    profile: Mapped[ZoneProfile | None] = relationship(back_populates="zone", uselist=False)
    uploads: Mapped[list[ZoneUpload]] = relationship(back_populates="zone")
    information: Mapped[list[ZoneInformation]] = relationship(back_populates="zone")
    contributions: Mapped[list[ZoneContribution]] = relationship(back_populates="zone")


class ZoneProfile(Base, TimestampMixin):
    """Editable MVP presentation and L93 envelope for one independently published zone."""

    __tablename__ = "zone_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spatial_zone_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    description: Mapped[str] = mapped_column(String(4_000), nullable=False)
    visibility: Mapped[ZoneVisibility] = mapped_column(
        enum_column(ZoneVisibility, name="zone_visibility"),
        nullable=False,
        default=ZoneVisibility.DRAFT,
        index=True,
    )
    min_easting_l93: Mapped[float] = mapped_column(Float, nullable=False)
    min_northing_l93: Mapped[float] = mapped_column(Float, nullable=False)
    max_easting_l93: Mapped[float] = mapped_column(Float, nullable=False)
    max_northing_l93: Mapped[float] = mapped_column(Float, nullable=False)

    zone: Mapped[SpatialZone] = relationship(back_populates="profile")

    __table_args__ = (
        CheckConstraint(
            "min_easting_l93 < max_easting_l93 AND min_northing_l93 < max_northing_l93",
            name="ck_zone_profile_l93_bounds",
        ),
    )


class ZoneUpload(Base):
    """A locally stored, verified archive.  Only one validated upload can be active per zone."""

    __tablename__ = "zone_upload"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    spatial_zone_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    package_id: Mapped[str] = mapped_column(String(96), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[ZoneUploadState] = mapped_column(
        enum_column(ZoneUploadState, name="zone_upload_state"),
        nullable=False,
        default=ZoneUploadState.RECEIVED,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    asset_catalog: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # This is a server-controlled relative key, never an API value and never a client path.
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    zone: Mapped[SpatialZone] = relationship(back_populates="uploads")

    __table_args__ = (
        UniqueConstraint("spatial_zone_id", "revision", name="uq_zone_upload_revision"),
        Index(
            "uq_zone_upload_one_active",
            "spatial_zone_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(sha256_hex_check("archive_sha256"), name="ck_zone_upload_archive_sha256"),
        CheckConstraint(sha256_hex_check("catalog_sha256"), name="ck_zone_upload_catalog_sha256"),
        CheckConstraint("archive_size_bytes > 0", name="ck_zone_upload_archive_size"),
        CheckConstraint("catalog_size_bytes > 0", name="ck_zone_upload_catalog_size"),
        CheckConstraint("revision >= 1", name="ck_zone_upload_revision_positive"),
        CheckConstraint(
            "NOT is_active OR state = 'VALIDATED'",
            name="ck_zone_upload_active_requires_validated",
        ),
    )


class ZoneInformation(Base, TimestampMixin):
    """Reviewed, coordinate-bearing data shown only when its parent zone is public."""

    __tablename__ = "zone_information"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    information_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    spatial_zone_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    easting_l93: Mapped[float] = mapped_column(Float, nullable=False)
    northing_l93: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[ZoneInformationState] = mapped_column(
        enum_column(ZoneInformationState, name="zone_information_state"),
        nullable=False,
        default=ZoneInformationState.DRAFT,
        index=True,
    )
    review_note: Mapped[str | None] = mapped_column(String(1_000))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    zone: Mapped[SpatialZone] = relationship(back_populates="information")


class ZoneContribution(Base):
    """Untrusted public submission.  Pending rows are never returned by public reads."""

    __tablename__ = "zone_contribution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contribution_id: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, index=True
    )
    spatial_zone_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    easting_l93: Mapped[float | None] = mapped_column(Float)
    northing_l93: Mapped[float | None] = mapped_column(Float)
    state: Mapped[ZoneContributionState] = mapped_column(
        enum_column(ZoneContributionState, name="zone_contribution_state"),
        nullable=False,
        default=ZoneContributionState.PENDING,
        index=True,
    )
    review_reason: Mapped[str | None] = mapped_column(String(1_000))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    zone: Mapped[SpatialZone] = relationship(back_populates="contributions")

    __table_args__ = (
        CheckConstraint(
            "(easting_l93 IS NULL AND northing_l93 IS NULL) "
            "OR (easting_l93 IS NOT NULL AND northing_l93 IS NOT NULL)",
            name="ck_zone_contribution_l93_pair",
        ),
    )


class SpatialZoneRevision(Base):
    """Immutable spatial reference and local envelope for one zone revision.

    The persisted frame is deliberately independent from a model asset: a zone can be
    shared by multiple incidents, while an extension creates a new revision instead of
    moving existing incidents.
    """

    __tablename__ = "spatial_zone_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spatial_zone_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    spatial_profile_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    origin_easting_l93: Mapped[float | None] = mapped_column(Float)
    origin_northing_l93: Mapped[float | None] = mapped_column(Float)
    horizontal_crs: Mapped[str | None] = mapped_column(String(32))
    vertical_crs: Mapped[str | None] = mapped_column(String(32))
    ground_model: Mapped[str | None] = mapped_column(String(64))
    ground_resolution_m: Mapped[float | None] = mapped_column(Float)
    surface_height_reference: Mapped[str | None] = mapped_column(String(64))
    origin_lon: Mapped[float] = mapped_column(Float, nullable=False)
    origin_lat: Mapped[float] = mapped_column(Float, nullable=False)
    source_orthometric_height_m: Mapped[float] = mapped_column(Float, nullable=False)
    geoid_undulation_m: Mapped[float] = mapped_column(Float, nullable=False)
    origin_ellipsoid_height_m: Mapped[float] = mapped_column(Float, nullable=False)
    source_vertical_datum: Mapped[str] = mapped_column(
        String(128), nullable=False, default="NGF-IGN69"
    )
    vertical_transform_id: Mapped[str] = mapped_column(String(64), nullable=False, default="RAF20")
    vertical_grid_filename: Mapped[str] = mapped_column(
        String(255), nullable=False, default="fr_ign_RAF20.tif"
    )
    vertical_grid_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="dc0cc2a38f0ea1029fe72cca3b5b7ed6dfe7e1db2a8d8482b7326ce3d6f25605",
    )
    vertical_datum: Mapped[str] = mapped_column(String(128), nullable=False, default="EPSG:4979")
    local_frame: Mapped[str] = mapped_column(String(16), nullable=False, default="ENU")
    meters_per_unit: Mapped[float] = mapped_column(Float, nullable=False, default=0.01)
    unity_profile: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unity-eun-100-v1"
    )
    gltf_to_unity_profile: Mapped[str] = mapped_column(
        String(64), nullable=False, default="gltf-eun-negz-metric-v1"
    )
    min_east_m: Mapped[float] = mapped_column(Float, nullable=False)
    max_east_m: Mapped[float] = mapped_column(Float, nullable=False)
    min_north_m: Mapped[float] = mapped_column(Float, nullable=False)
    max_north_m: Mapped[float] = mapped_column(Float, nullable=False)
    min_up_m: Mapped[float] = mapped_column(Float, nullable=False)
    max_up_m: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    zone: Mapped[SpatialZone] = relationship(back_populates="revisions")
    assets: Mapped[list[ModelAsset]] = relationship(back_populates="spatial_zone_revision")
    spatial_packages: Mapped[list[SpatialPackage]] = relationship(
        back_populates="spatial_zone_revision"
    )
    zone_publications: Mapped[list[ZonePublication]] = relationship(
        back_populates="spatial_zone_revision"
    )
    manifest_revisions: Mapped[list[ManifestRevision]] = relationship(
        back_populates="spatial_zone_revision"
    )
    archive_snapshots: Mapped[list[ZoneArchiveSnapshot]] = relationship(
        back_populates="spatial_zone_revision"
    )

    __table_args__ = (
        UniqueConstraint("spatial_zone_id", "revision", name="uq_spatial_zone_revision"),
        CheckConstraint("revision >= 1", name="ck_spatial_zone_revision_positive"),
        CheckConstraint(
            "ground_resolution_m IS NULL OR ground_resolution_m > 0",
            name="ck_spatial_zone_ground_resolution",
        ),
        CheckConstraint("origin_lon >= -5.5 AND origin_lon <= 10.0", name="ck_spatial_zone_lon"),
        CheckConstraint("origin_lat >= 42.0 AND origin_lat <= 51.5", name="ck_spatial_zone_lat"),
        CheckConstraint(
            "origin_lon > -1e308 AND origin_lon < 1e308 "
            "AND origin_lat > -1e308 AND origin_lat < 1e308 "
            "AND source_orthometric_height_m > -1e308 "
            "AND source_orthometric_height_m < 1e308 "
            "AND geoid_undulation_m > -1e308 AND geoid_undulation_m < 1e308 "
            "AND origin_ellipsoid_height_m > -1e308 "
            "AND origin_ellipsoid_height_m < 1e308",
            name="ck_spatial_zone_origin_finite",
        ),
        CheckConstraint(
            "abs(origin_ellipsoid_height_m - source_orthometric_height_m "
            "- geoid_undulation_m) <= 0.001",
            name="ck_spatial_zone_vertical_derivation",
        ),
        CheckConstraint(
            "NOT (origin_lon >= 8.3 AND origin_lon <= 9.8 "
            "AND origin_lat >= 41.0 AND origin_lat <= 43.3)",
            name="ck_spatial_zone_not_corsica",
        ),
        CheckConstraint("source_vertical_datum = 'NGF-IGN69'", name="ck_spatial_zone_source_datum"),
        CheckConstraint("vertical_transform_id = 'RAF20'", name="ck_spatial_zone_transform"),
        CheckConstraint(
            "vertical_grid_filename = 'fr_ign_RAF20.tif'", name="ck_spatial_zone_grid_filename"
        ),
        CheckConstraint(
            "vertical_grid_sha256 = "
            "'dc0cc2a38f0ea1029fe72cca3b5b7ed6dfe7e1db2a8d8482b7326ce3d6f25605'",
            name="ck_spatial_zone_grid_hash",
        ),
        CheckConstraint("vertical_datum = 'EPSG:4979'", name="ck_spatial_zone_datum"),
        CheckConstraint("local_frame = 'ENU'", name="ck_spatial_zone_frame"),
        CheckConstraint("meters_per_unit = 0.01", name="ck_spatial_zone_scale"),
        CheckConstraint("unity_profile = 'unity-eun-100-v1'", name="ck_spatial_zone_unity_profile"),
        CheckConstraint(
            "gltf_to_unity_profile = 'gltf-eun-negz-metric-v1'",
            name="ck_spatial_zone_gltf_profile",
        ),
        CheckConstraint(
            "min_east_m < max_east_m AND min_east_m <= 0 AND max_east_m >= 0",
            name="ck_spatial_zone_east_bounds",
        ),
        CheckConstraint(
            "min_east_m > -1e308 AND min_east_m < 1e308 "
            "AND max_east_m > -1e308 AND max_east_m < 1e308 "
            "AND min_north_m > -1e308 AND min_north_m < 1e308 "
            "AND max_north_m > -1e308 AND max_north_m < 1e308 "
            "AND min_up_m > -1e308 AND min_up_m < 1e308 "
            "AND max_up_m > -1e308 AND max_up_m < 1e308",
            name="ck_spatial_zone_bounds_finite",
        ),
        CheckConstraint(
            "min_north_m < max_north_m AND min_north_m <= 0 AND max_north_m >= 0",
            name="ck_spatial_zone_north_bounds",
        ),
        CheckConstraint(
            "min_up_m < max_up_m AND min_up_m <= 0 AND max_up_m >= 0",
            name="ck_spatial_zone_up_bounds",
        ),
    )


class SpatialPackage(Base):
    """Immutable admin registry entry for a Unity-produced spatial package.

    The package stores controlled object locations, hashes and verification
    metadata only. COG/PNG/GLB binaries stay outside SQLite.
    """

    __tablename__ = "spatial_package"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    manifest_uri: Mapped[str] = mapped_column(String(2_048), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(2_048), nullable=False)
    state: Mapped[SpatialPackageState] = mapped_column(
        enum_column(SpatialPackageState, name="spatial_package_state"),
        nullable=False,
        default=SpatialPackageState.DRAFT,
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    verification_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    spatial_zone_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("spatial_zone_revision.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    spatial_zone_revision: Mapped[SpatialZoneRevision | None] = relationship(
        back_populates="spatial_packages"
    )
    files: Mapped[list[SpatialPackageFile]] = relationship(
        back_populates="package", cascade="all, delete-orphan"
    )
    zone_publications: Mapped[list[ZonePublication]] = relationship(back_populates="package")
    manifest_revisions: Mapped[list[ManifestRevision]] = relationship(back_populates="package")

    __table_args__ = (
        CheckConstraint(
            sha256_hex_check("manifest_sha256"),
            name="ck_spatial_package_manifest_sha256",
        ),
        CheckConstraint("manifest_size_bytes > 0", name="ck_spatial_package_manifest_size"),
        CheckConstraint("length(manifest_uri) > 0", name="ck_spatial_package_manifest_uri"),
        CheckConstraint("length(storage_uri) > 0", name="ck_spatial_package_storage_uri"),
        CheckConstraint(
            "(state IN ('VERIFIED', 'PREVIEWABLE', 'PUBLISHED', 'WITHDRAWN', "
            "'REVOKED', 'ARCHIVED') "
            "AND verified_at IS NOT NULL) OR state = 'DRAFT'",
            name="ck_spatial_package_verified_states_timestamp",
        ),
        CheckConstraint(
            "spatial_zone_revision_id IS NULL OR state IN "
            "('VERIFIED', 'PREVIEWABLE', 'PUBLISHED', 'WITHDRAWN', 'REVOKED', 'ARCHIVED')",
            name="ck_spatial_package_revision_requires_validated_state",
        ),
    )


class SpatialPackageFile(Base):
    """Object-store reference for one file that belongs to an admin package."""

    __tablename__ = "spatial_package_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spatial_package_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_package.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[SpatialPackageFileKind] = mapped_column(
        enum_column(SpatialPackageFileKind, name="spatial_package_file_kind"), nullable=False
    )
    uri: Mapped[str] = mapped_column(String(2_048), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    package: Mapped[SpatialPackage] = relationship(back_populates="files")
    model_assets: Mapped[list[ModelAsset]] = relationship(back_populates="spatial_package_file")

    __table_args__ = (
        UniqueConstraint("spatial_package_id", "kind", "uri", name="uq_spatial_package_file"),
        CheckConstraint(sha256_hex_check("sha256"), name="ck_spatial_package_file_sha256"),
        CheckConstraint("size_bytes > 0", name="ck_spatial_package_file_size"),
        CheckConstraint("length(uri) > 0", name="ck_spatial_package_file_uri"),
        CheckConstraint(
            "(kind = 'COG' AND media_type IN "
            "('image/tiff', 'image/geotiff', 'application/octet-stream')) "
            "OR (kind = 'JPEG' AND media_type = 'image/jpeg') "
            "OR (kind = 'PNG' AND media_type = 'image/png') "
            "OR (kind = 'GLB' AND media_type IN ('model/gltf-binary', 'application/octet-stream')) "
            "OR (kind = 'FWTILE' AND media_type = 'application/vnd.fireviewer.tile') "
            "OR (kind = 'FWTERRAIN' AND media_type = 'application/vnd.fireviewer.terrain')",
            name="ck_spatial_package_file_media_type",
        ),
    )


class ZonePublication(Base):
    """Administrative publication lifecycle for one explicit zone revision choice."""

    __tablename__ = "zone_publication"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publication_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    spatial_zone_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    spatial_zone_revision_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone_revision.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    spatial_package_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_package.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    state: Mapped[ZonePublicationState] = mapped_column(
        enum_column(ZonePublicationState, name="zone_publication_state"),
        nullable=False,
        default=ZonePublicationState.DRAFT,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    zone: Mapped[SpatialZone] = relationship(back_populates="publications")
    spatial_zone_revision: Mapped[SpatialZoneRevision] = relationship(
        back_populates="zone_publications"
    )
    package: Mapped[SpatialPackage] = relationship(back_populates="zone_publications")
    events: Mapped[list[ZonePublicationEvent]] = relationship(
        back_populates="publication", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "uq_zone_publication_one_active",
            "spatial_zone_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "(is_active AND state = 'PUBLISHED') OR (NOT is_active AND state != 'PUBLISHED')",
            name="ck_zone_publication_active_state",
        ),
    )


class ZonePublicationEvent(Base):
    """Append-only audit event for publication state transitions."""

    __tablename__ = "zone_publication_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    zone_publication_id: Mapped[int] = mapped_column(
        ForeignKey("zone_publication.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    from_state: Mapped[ZonePublicationState | None] = mapped_column(
        enum_column(ZonePublicationState, name="zone_publication_from_state")
    )
    to_state: Mapped[ZonePublicationState] = mapped_column(
        enum_column(ZonePublicationState, name="zone_publication_to_state"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    publication: Mapped[ZonePublication] = relationship(back_populates="events")


class ModelAsset(Base):
    __tablename__ = "model_asset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    legacy_incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True
    )
    legacy_episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    legacy_origin_lon: Mapped[float | None] = mapped_column(Float)
    legacy_origin_lat: Mapped[float | None] = mapped_column(Float)
    legacy_origin_altitude_m: Mapped[float | None] = mapped_column(Float)
    legacy_local_frame: Mapped[str | None] = mapped_column(String(16))
    legacy_meters_per_unit: Mapped[float | None] = mapped_column(Float)
    legacy_vertical_datum: Mapped[str | None] = mapped_column(String(128))
    spatial_zone_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("spatial_zone_revision.id", ondelete="RESTRICT"), index=True
    )
    spatial_package_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("spatial_package_file.id", ondelete="RESTRICT"), unique=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    lod: Mapped[AssetLod] = mapped_column(enum_column(AssetLod, name="asset_lod"), nullable=False)
    state: Mapped[AssetState] = mapped_column(
        enum_column(AssetState, name="asset_state"), nullable=False
    )
    glb_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    terrain_source_year: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    purge_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_hold_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    spatial_zone_revision: Mapped[SpatialZoneRevision | None] = relationship(
        back_populates="assets"
    )
    spatial_package_file: Mapped[SpatialPackageFile | None] = relationship(
        back_populates="model_assets"
    )
    manifest_revisions: Mapped[list[ManifestRevision]] = relationship(back_populates="asset")
    archive_snapshots: Mapped[list[ZoneArchiveSnapshot]] = relationship(back_populates="asset")

    __table_args__ = (
        UniqueConstraint("spatial_zone_revision_id", "version", "lod", name="uq_asset_version_lod"),
        CheckConstraint("version >= 1", name="ck_asset_version"),
        CheckConstraint("size_bytes > 0", name="ck_asset_size"),
        CheckConstraint(
            "spatial_zone_revision_id IS NOT NULL OR state IN ('QUARANTINED', 'DELETED_TOMBSTONE')",
            name="ck_asset_zone_revision_required",
        ),
        CheckConstraint(
            "(legacy_incident_id IS NULL AND legacy_episode_id IS NULL "
            "AND legacy_origin_lon IS NULL AND legacy_origin_lat IS NULL "
            "AND legacy_origin_altitude_m IS NULL AND legacy_local_frame IS NULL "
            "AND legacy_meters_per_unit IS NULL AND legacy_vertical_datum IS NULL) "
            "OR (legacy_incident_id IS NOT NULL AND legacy_episode_id IS NOT NULL "
            "AND legacy_origin_lon IS NOT NULL AND legacy_origin_lat IS NOT NULL "
            "AND legacy_origin_altitude_m IS NOT NULL AND legacy_local_frame IS NOT NULL "
            "AND legacy_meters_per_unit IS NOT NULL AND legacy_vertical_datum IS NOT NULL)",
            name="ck_asset_legacy_provenance",
        ),
    )


class Job(Base, TimestampMixin):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    kind: Mapped[JobKind] = mapped_column(enum_column(JobKind, name="job_kind"), nullable=False)
    state: Mapped[JobState] = mapped_column(
        enum_column(JobState, name="job_state"), nullable=False, index=True
    )
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))

    incident: Mapped[IncidentSeries] = relationship(back_populates="jobs")
    episode: Mapped[Episode] = relationship(back_populates="jobs")

    __table_args__ = (
        UniqueConstraint("kind", "idempotency_key", name="uq_job_kind_idempotency"),
        CheckConstraint("attempt >= 0", name="ck_job_attempt_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="ck_job_max_attempts_positive"),
    )


class AdminLocalSession(Base, TimestampMixin):
    """Opaque, revocable local-admin browser session. Only a SHA-256 digest is persisted."""

    __tablename__ = "admin_local_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AdminLoginAttempt(Base):
    __tablename__ = "admin_login_attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class ManifestRevision(Base):
    __tablename__ = "manifest_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False
    )
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("model_asset.id", ondelete="RESTRICT"))
    spatial_zone_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("spatial_zone_revision.id", ondelete="RESTRICT"), index=True
    )
    spatial_package_id: Mapped[int | None] = mapped_column(
        ForeignKey("spatial_package.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    incident: Mapped[IncidentSeries] = relationship(back_populates="manifest_revisions")
    asset: Mapped[ModelAsset | None] = relationship(back_populates="manifest_revisions")
    spatial_zone_revision: Mapped[SpatialZoneRevision | None] = relationship(
        back_populates="manifest_revisions"
    )
    package: Mapped[SpatialPackage | None] = relationship(back_populates="manifest_revisions")
    archive_snapshot: Mapped[ZoneArchiveSnapshot | None] = relationship(
        back_populates="manifest_revision", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("incident_id", "revision", name="uq_manifest_revision"),
        Index(
            "uq_manifest_one_current",
            "incident_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
        CheckConstraint("revision >= 1", name="ck_manifest_revision"),
        CheckConstraint(
            "spatial_zone_revision_id IS NULL OR asset_id IS NOT NULL "
            "OR spatial_package_id IS NOT NULL",
            name="ck_manifest_zone_requires_asset",
        ),
    )


class AgentAnalysisWindow(Base, TimestampMixin):
    """One private, local-day analysis workspace for an incident episode."""

    __tablename__ = "agent_analysis_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[AgentAnalysisState] = mapped_column(
        enum_column(AgentAnalysisState, name="agent_analysis_state"),
        nullable=False,
        default=AgentAnalysisState.COLLECTING,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    incident: Mapped[IncidentSeries] = relationship(foreign_keys=[incident_id])
    episode: Mapped[Episode] = relationship(foreign_keys=[episode_id])
    batches: Mapped[list[AgentMediaBatch]] = relationship(
        back_populates="analysis_window",
        foreign_keys=(
            "[AgentMediaBatch.analysis_window_id, AgentMediaBatch.incident_id, "
            "AgentMediaBatch.episode_id]"
        ),
        overlaps="incident,episode",
    )
    source_annotations: Mapped[list[AgentSourceAnnotation]] = relationship(
        back_populates="analysis_window"
    )
    spatial_proposals: Mapped[list[AgentSpatialProposal]] = relationship(
        back_populates="analysis_window"
    )
    fact_proposals: Mapped[list[AgentFactProposal]] = relationship(back_populates="analysis_window")
    report_revisions: Mapped[list[AgentSituationReportRevision]] = relationship(
        back_populates="analysis_window",
        foreign_keys=(
            "[AgentSituationReportRevision.analysis_window_id, "
            "AgentSituationReportRevision.incident_id, AgentSituationReportRevision.episode_id]"
        ),
        order_by="AgentSituationReportRevision.revision",
        overlaps="incident,episode",
    )
    source_packages: Mapped[list[AgentSourcePackage]] = relationship(
        back_populates="analysis_window"
    )
    source_research_runs: Mapped[list[AgentSourceResearchRun]] = relationship(
        back_populates="analysis_window"
    )

    __table_args__ = (
        UniqueConstraint(
            "incident_id", "episode_id", "local_date", name="uq_agent_analysis_local_day"
        ),
        UniqueConstraint(
            "id", "incident_id", "episode_id", name="uq_agent_analysis_window_identity"
        ),
        CheckConstraint("window_end_at > window_start_at", name="ck_agent_analysis_window_order"),
        CheckConstraint("length(timezone) >= 3", name="ck_agent_analysis_timezone"),
        CheckConstraint("version >= 1", name="ck_agent_analysis_version"),
    )


class AgentValidationCampaign(Base, TimestampMixin):
    """Internal ordered campaign; it is intentionally absent from the HTTP API."""

    __tablename__ = "agent_validation_campaign"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    days: Mapped[list[AgentValidationCampaignDay]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="AgentValidationCampaignDay.ordinal",
    )

    __table_args__ = (
        UniqueConstraint("manifest_sha256", name="uq_agent_validation_campaign_manifest"),
        CheckConstraint(
            sha256_hex_check("manifest_sha256"),
            name="ck_agent_validation_campaign_manifest_hash",
        ),
        CheckConstraint("version >= 1", name="ck_agent_validation_campaign_version"),
        Index(
            "uq_agent_validation_campaign_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
    )


class AgentValidationCampaignDay(Base, TimestampMixin):
    """One immutable manifest/window gate in an internal validation campaign."""

    __tablename__ = "agent_validation_campaign_day"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_day_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("agent_validation_campaign.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analysis_window_id: Mapped[int] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_media_sha256: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_operations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    declared_absences: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[AgentValidationCampaignDayState] = mapped_column(
        enum_column(
            AgentValidationCampaignDayState,
            name="agent_validation_campaign_day_state",
        ),
        nullable=False,
        default=AgentValidationCampaignDayState.LOCKED,
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    campaign: Mapped[AgentValidationCampaign] = relationship(back_populates="days")
    analysis_window: Mapped[AgentAnalysisWindow] = relationship()

    __table_args__ = (
        UniqueConstraint("campaign_id", "ordinal", name="uq_agent_validation_campaign_day_order"),
        CheckConstraint("ordinal >= 1", name="ck_agent_validation_campaign_day_ordinal"),
        CheckConstraint(
            sha256_hex_check("manifest_sha256"),
            name="ck_agent_validation_campaign_day_manifest_hash",
        ),
        CheckConstraint("version >= 1", name="ck_agent_validation_campaign_day_version"),
    )


class AgentSourcePackage(Base, TimestampMixin):
    """Private source transfer before conversion to a normal media batch."""

    __tablename__ = "agent_source_package"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    analysis_window_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), index=True
    )
    package_kind: Mapped[AgentSourcePackageKind] = mapped_column(
        enum_column(AgentSourcePackageKind, name="agent_source_package_kind"),
        nullable=False,
        default=AgentSourcePackageKind.USER_SOURCES,
        index=True,
    )
    state: Mapped[AgentSourcePackageState] = mapped_column(
        enum_column(AgentSourcePackageState, name="agent_source_package_state"),
        nullable=False,
        default=AgentSourcePackageState.OPEN,
        index=True,
    )
    upload_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    pathname_prefix: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    declared_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    declared_total_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    known_start_date: Mapped[date | None] = mapped_column(Date, index=True)
    known_end_date: Mapped[date | None] = mapped_column(Date, index=True)
    location_hint: Mapped[str | None] = mapped_column(String(500))
    file_date_metadata: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    analysis_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    publication_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    terms_version: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_scopes: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: ["temporary_storage", "agent_analysis", "human_review"],
    )
    subject_reference_hash: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    purge_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    failure_detail: Mapped[str | None] = mapped_column(String(1_000))

    incident: Mapped[IncidentSeries | None] = relationship(foreign_keys=[incident_id])
    episode: Mapped[Episode | None] = relationship(foreign_keys=[episode_id])
    analysis_window: Mapped[AgentAnalysisWindow | None] = relationship(
        back_populates="source_packages"
    )
    items: Mapped[list[AgentSourcePackageItem]] = relationship(
        back_populates="package", cascade="all, delete-orphan", order_by="AgentSourcePackageItem.id"
    )
    public_contribution: Mapped[PublicContributionSubmission | None] = relationship(
        back_populates="source_package",
        uselist=False,
        foreign_keys="PublicContributionSubmission.source_package_id",
    )

    __table_args__ = (
        CheckConstraint("declared_file_count > 0", name="ck_agent_source_package_file_count"),
        CheckConstraint("declared_total_size_bytes > 0", name="ck_agent_source_package_total_size"),
        CheckConstraint(
            "(known_start_date IS NULL AND known_end_date IS NULL) OR "
            "(known_start_date IS NOT NULL AND known_end_date IS NOT NULL "
            "AND known_end_date >= known_start_date)",
            name="ck_agent_source_package_date_order",
        ),
        CheckConstraint("analysis_authorized", name="ck_agent_source_package_analysis_authorized"),
        CheckConstraint("NOT publication_authorized", name="ck_agent_source_package_not_public"),
        CheckConstraint(
            "(incident_id IS NULL AND episode_id IS NULL) OR "
            "(incident_id IS NOT NULL AND episode_id IS NOT NULL)",
            name="ck_agent_source_package_incident_episode_pair",
        ),
        CheckConstraint(
            sha256_hex_check("consent_evidence_sha256"),
            name="ck_agent_source_package_consent_hash",
        ),
        CheckConstraint(
            sha256_hex_check("request_hash"), name="ck_agent_source_package_request_hash"
        ),
        CheckConstraint(
            "subject_reference_hash IS NULL OR ("
            + sha256_hex_check("subject_reference_hash")
            + ")",
            name="ck_agent_source_package_subject_hash",
        ),
    )


class AgentSourcePackageItem(Base, TimestampMixin):
    __tablename__ = "agent_source_package_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("agent_source_package.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="RESTRICT"), unique=True, index=True
    )
    pathname: Mapped[str] = mapped_column(String(1_024), nullable=False, unique=True)
    object_uri: Mapped[str] = mapped_column(String(1_024), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[AgentMediaType] = mapped_column(
        enum_column(AgentMediaType, name="agent_source_package_media_type"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    date_classification: Mapped[str] = mapped_column(
        String(20), nullable=False, default="TO_CLASSIFY", index=True
    )
    date_evidence: Mapped[str | None] = mapped_column(String(40))
    classified_local_date: Mapped[date | None] = mapped_column(Date, index=True)
    analysis_window_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), index=True
    )
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    package: Mapped[AgentSourcePackage] = relationship(back_populates="items")
    analysis_window: Mapped[AgentAnalysisWindow | None] = relationship()
    agent_media_item: Mapped[AgentMediaItem | None] = relationship(
        foreign_keys=[agent_media_item_id]
    )

    __table_args__ = (
        UniqueConstraint("package_id", "pathname", name="uq_agent_source_package_item_path"),
        CheckConstraint(sha256_hex_check("sha256"), name="ck_agent_source_package_item_hash"),
        CheckConstraint("size_bytes > 0", name="ck_agent_source_package_item_size"),
        CheckConstraint(
            "date_classification IN ('CLASSIFIED', 'TO_CLASSIFY')",
            name="ck_agent_source_package_item_date_classification",
        ),
        CheckConstraint(
            "(date_classification = 'CLASSIFIED' "
            "AND classified_local_date IS NOT NULL AND date_evidence IS NOT NULL) OR "
            "(date_classification = 'TO_CLASSIFY' "
            "AND classified_local_date IS NULL AND analysis_window_id IS NULL)",
            name="ck_agent_source_package_item_date_assignment",
        ),
    )


class AgentSourceResearchRun(Base, TimestampMixin):
    """Persistent web-research operation, independent from historical job rows."""

    __tablename__ = "agent_source_research_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    analysis_window_id: Mapped[int] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    state: Mapped[AgentSourceResearchState] = mapped_column(
        enum_column(AgentSourceResearchState, name="agent_source_research_state"),
        nullable=False,
        default=AgentSourceResearchState.QUEUED,
        index=True,
    )
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    location_hint: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    source_registry_version: Mapped[str] = mapped_column(String(64), nullable=False)
    upload_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    pathname_prefix: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    query_plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    remote_job_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    remote_status: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(255), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    last_error_detail: Mapped[str | None] = mapped_column(String(1_000))

    incident: Mapped[IncidentSeries] = relationship(foreign_keys=[incident_id])
    episode: Mapped[Episode] = relationship(foreign_keys=[episode_id])
    analysis_window: Mapped[AgentAnalysisWindow] = relationship(
        back_populates="source_research_runs"
    )
    candidates: Mapped[list[AgentSourceCandidate]] = relationship(
        back_populates="research_run",
        cascade="all, delete-orphan",
        order_by="AgentSourceCandidate.id",
    )

    __table_args__ = (
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_agent_source_research_progress",
        ),
        CheckConstraint("attempt >= 0", name="ck_agent_source_research_attempt"),
        CheckConstraint("max_attempts >= 1", name="ck_agent_source_research_max_attempts"),
        CheckConstraint("poll_count >= 0", name="ck_agent_source_research_poll_count"),
        CheckConstraint(
            "payload_hash IS NULL OR (" + sha256_hex_check("payload_hash") + ")",
            name="ck_agent_source_research_payload_hash",
        ),
        CheckConstraint(
            "output_hash IS NULL OR (" + sha256_hex_check("output_hash") + ")",
            name="ck_agent_source_research_output_hash",
        ),
    )


class AgentSourceCandidate(Base, TimestampMixin):
    __tablename__ = "agent_source_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_source_research_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="RESTRICT"), unique=True, index=True
    )
    state: Mapped[AgentSourceCandidateState] = mapped_column(
        enum_column(AgentSourceCandidateState, name="agent_source_candidate_state"),
        nullable=False,
        default=AgentSourceCandidateState.DISCOVERED,
        index=True,
    )
    canonical_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    media_type: Mapped[AgentMediaType | None] = mapped_column(
        enum_column(AgentMediaType, name="agent_source_candidate_media_type")
    )
    media_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    object_uri: Mapped[str | None] = mapped_column(String(1_024))
    excerpt: Mapped[str | None] = mapped_column(Text)
    license_identifier: Mapped[str | None] = mapped_column(String(128))
    attribution: Mapped[str | None] = mapped_column(String(500))
    provenance_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    cutoff_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duplicate_of_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_source_candidate.id", ondelete="RESTRICT"), index=True
    )

    research_run: Mapped[AgentSourceResearchRun] = relationship(back_populates="candidates")
    duplicate_of: Mapped[AgentSourceCandidate | None] = relationship(
        remote_side=[id], foreign_keys=[duplicate_of_candidate_id]
    )
    agent_media_item: Mapped[AgentMediaItem | None] = relationship(
        foreign_keys=[agent_media_item_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "research_run_id", "canonical_url_hash", name="uq_agent_source_candidate_run_url"
        ),
        CheckConstraint(
            sha256_hex_check("canonical_url_hash"), name="ck_agent_source_candidate_url_hash"
        ),
        CheckConstraint(
            "media_sha256 IS NULL OR (" + sha256_hex_check("media_sha256") + ")",
            name="ck_agent_source_candidate_media_hash",
        ),
        CheckConstraint("canonical_url LIKE 'https://%'", name="ck_agent_source_candidate_https"),
    )


class AgentMediaBatch(Base, TimestampMixin):
    """Private media-analysis batch; deliberately independent from terrain/publication jobs."""

    __tablename__ = "agent_media_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    batch_type: Mapped[AgentBatchType] = mapped_column(
        enum_column(AgentBatchType, name="agent_batch_type"), nullable=False
    )
    priority: Mapped[AgentBatchPriority] = mapped_column(
        enum_column(AgentBatchPriority, name="agent_batch_priority"), nullable=False
    )
    state: Mapped[AgentBatchState] = mapped_column(
        enum_column(AgentBatchState, name="agent_batch_state"), nullable=False, index=True
    )
    incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), index=True
    )
    analysis_window_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reference_bundle_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    purge_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[AgentMediaItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="AgentMediaItem.id"
    )
    dispatch: Mapped[AgentDispatch | None] = relationship(
        back_populates="batch", cascade="all, delete-orphan", uselist=False
    )
    review_task: Mapped[AgentReviewTask | None] = relationship(
        back_populates="batch", cascade="all, delete-orphan", uselist=False
    )
    incident: Mapped[IncidentSeries | None] = relationship(foreign_keys=[incident_id])
    episode: Mapped[Episode | None] = relationship(foreign_keys=[episode_id])
    analysis_window: Mapped[AgentAnalysisWindow | None] = relationship(
        back_populates="batches",
        foreign_keys=[analysis_window_id, incident_id, episode_id],
        overlaps="incident,episode",
    )

    __table_args__ = (
        CheckConstraint("schema_version IN ('1.0', '2.0')", name="ck_agent_batch_schema_version"),
        CheckConstraint(
            "(schema_version = '1.0' AND analysis_window_id IS NULL "
            "AND reference_bundle_payload IS NULL) OR "
            "(schema_version = '2.0' AND analysis_window_id IS NOT NULL "
            "AND incident_id IS NOT NULL AND episode_id IS NOT NULL)",
            name="ck_agent_batch_analysis_window_version",
        ),
        CheckConstraint(
            "(incident_id IS NULL AND episode_id IS NULL) OR "
            "(incident_id IS NOT NULL AND episode_id IS NOT NULL)",
            name="ck_agent_batch_incident_episode_pair",
        ),
        CheckConstraint(sha256_hex_check("request_hash"), name="ck_agent_batch_request_hash"),
        CheckConstraint(
            "payload_hash IS NULL OR (" + sha256_hex_check("payload_hash") + ")",
            name="ck_agent_batch_payload_hash",
        ),
        ForeignKeyConstraint(
            ["analysis_window_id", "incident_id", "episode_id"],
            [
                "agent_analysis_window.id",
                "agent_analysis_window.incident_id",
                "agent_analysis_window.episode_id",
            ],
            name="fk_agent_batch_analysis_window_identity",
            ondelete="RESTRICT",
        ),
    )


class AgentMediaItem(Base, TimestampMixin):
    __tablename__ = "agent_media_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_batch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_id: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[AgentMediaType] = mapped_column(
        enum_column(AgentMediaType, name="agent_media_type"), nullable=False
    )
    working_file_url: Mapped[str | None] = mapped_column(String(2_048))
    media_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    processable_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    preprocessing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="validated"
    )
    purge_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    batch: Mapped[AgentMediaBatch] = relationship(back_populates="items")
    consent: Mapped[AgentMediaConsent] = relationship(
        back_populates="item", cascade="all, delete-orphan", uselist=False
    )
    source_annotations: Mapped[list[AgentSourceAnnotation]] = relationship(
        back_populates="source_media_item"
    )
    spatial_proposals: Mapped[list[AgentSpatialProposal]] = relationship(
        back_populates="source_media_item"
    )
    fact_proposals: Mapped[list[AgentFactProposal]] = relationship(
        back_populates="source_media_item"
    )

    __table_args__ = (
        UniqueConstraint("batch_id", "input_id", name="uq_agent_media_item_batch_input"),
        CheckConstraint(
            "media_sha256 IS NULL OR (" + sha256_hex_check("media_sha256") + ")",
            name="ck_agent_media_item_hash",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes > 0", name="ck_agent_media_item_size"),
        CheckConstraint(
            "working_file_url IS NULL OR working_file_url LIKE 'https://%'",
            name="ck_agent_media_item_https",
        ),
    )


class AgentMediaConsent(Base, TimestampMixin):
    __tablename__ = "agent_media_consent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    basis: Mapped[AgentConsentBasis] = mapped_column(
        Enum(
            AgentConsentBasis,
            name="agent_consent_basis",
            native_enum=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
        ),
        nullable=False,
    )
    state: Mapped[AgentConsentState] = mapped_column(
        enum_column(AgentConsentState, name="agent_consent_state"), nullable=False, index=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    terms_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_reference_hash: Mapped[str | None] = mapped_column(String(64))
    source_reference_url: Mapped[str | None] = mapped_column(String(2_048))
    license_identifier: Mapped[str | None] = mapped_column(String(128))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawal_reason: Mapped[str | None] = mapped_column(String(500))

    item: Mapped[AgentMediaItem] = relationship(back_populates="consent")

    __table_args__ = (
        CheckConstraint(sha256_hex_check("evidence_sha256"), name="ck_agent_consent_evidence_hash"),
        CheckConstraint(
            "subject_reference_hash IS NULL OR ("
            + sha256_hex_check("subject_reference_hash")
            + ")",
            name="ck_agent_consent_subject_hash",
        ),
        CheckConstraint(
            "basis != 'source_license' OR "
            "(source_reference_url IS NOT NULL AND license_identifier IS NOT NULL)",
            name="ck_agent_consent_source_license",
        ),
        CheckConstraint(
            "basis != 'public_source_analysis' OR source_reference_url IS NOT NULL",
            name="ck_agent_consent_public_source",
        ),
        CheckConstraint(
            "source_reference_url IS NULL OR source_reference_url LIKE 'https://%'",
            name="ck_agent_consent_reference_https",
        ),
    )


class AgentSourceAnnotation(Base, TimestampMixin):
    """A model-proposed anchor in source pixels, never a geographic point by itself."""

    __tablename__ = "agent_source_annotation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    annotation_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    analysis_window_id: Mapped[int] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_media_item_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_anchor: Mapped[str] = mapped_column(String(64), nullable=False)
    source_x_normalized: Mapped[float | None] = mapped_column(Float)
    source_y_normalized: Mapped[float | None] = mapped_column(Float)
    source_geometry_normalized: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_score: Mapped[float | None] = mapped_column(Float)

    analysis_window: Mapped[AgentAnalysisWindow] = relationship(back_populates="source_annotations")
    source_media_item: Mapped[AgentMediaItem] = relationship(back_populates="source_annotations")
    spatial_proposals: Mapped[list[AgentSpatialProposal]] = relationship(
        back_populates="source_annotation"
    )

    __table_args__ = (
        CheckConstraint(
            "evidence_kind IN ('image', 'frame', 'satellite_image')",
            name="ck_agent_annotation_evidence_kind",
        ),
        CheckConstraint(
            "semantic_anchor IN ('active_fire_point', 'visible_fire_front_point', "
            "'visible_fire_front', 'smoke_column_base', 'smoke_origin_point', "
            "'burned_area_polygon')",
            name="ck_agent_annotation_semantic_anchor",
        ),
        CheckConstraint(
            "source_x_normalized IS NULL OR "
            "(source_x_normalized >= 0 AND source_x_normalized <= 1)",
            name="ck_agent_annotation_x",
        ),
        CheckConstraint(
            "source_y_normalized IS NULL OR "
            "(source_y_normalized >= 0 AND source_y_normalized <= 1)",
            name="ck_agent_annotation_y",
        ),
        CheckConstraint(
            "(source_x_normalized IS NULL AND source_y_normalized IS NULL) OR "
            "(source_x_normalized IS NOT NULL AND source_y_normalized IS NOT NULL)",
            name="ck_agent_annotation_point_shape",
        ),
        CheckConstraint(
            "model_score IS NULL OR (model_score >= 0 AND model_score <= 1)",
            name="ck_agent_annotation_score",
        ),
    )


class AgentSpatialProposal(Base, TimestampMixin):
    """Private geographic proposal or explicit abstention awaiting human review."""

    __tablename__ = "agent_spatial_proposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    analysis_window_id: Mapped[int] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_media_item_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_annotation_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_source_annotation.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    proposal_kind: Mapped[str | None] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    geometry_origin: Mapped[str | None] = mapped_column(String(64))
    longitude: Mapped[float | None] = mapped_column(Float)
    latitude: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    geometry_geojson: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    horizontal_accuracy_m: Mapped[float | None] = mapped_column(Float)
    reference_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    uncertainty_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    review_state: Mapped[AgentProposalReviewState] = mapped_column(
        enum_column(AgentProposalReviewState, name="agent_proposal_review_state"),
        nullable=False,
        default=AgentProposalReviewState.PENDING,
        index=True,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    analysis_window: Mapped[AgentAnalysisWindow] = relationship(back_populates="spatial_proposals")
    source_media_item: Mapped[AgentMediaItem] = relationship(back_populates="spatial_proposals")
    source_annotation: Mapped[AgentSourceAnnotation | None] = relationship(
        back_populates="spatial_proposals"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ground_point', 'projected_geometry', 'insufficient_geometry')",
            name="ck_agent_spatial_proposal_status",
        ),
        CheckConstraint(
            "proposal_kind IS NULL OR proposal_kind IN "
            "('active_fire_point', 'smoke_origin_point', 'visible_fire_front', "
            "'probable_activity_envelope', 'burned_area_polygon', 'legacy_ground_point')",
            name="ck_agent_spatial_proposal_kind",
        ),
        CheckConstraint(
            "geometry_origin IS NULL OR geometry_origin IN "
            "('SATELLITE_GEOTRANSFORM', 'CAMERA_RAYCAST', 'CROSS_VIEW_RAYCAST', "
            "'EXPLICIT_SOURCE_GEOMETRY')",
            name="ck_agent_spatial_proposal_origin",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="ck_agent_spatial_proposal_longitude",
        ),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="ck_agent_spatial_proposal_latitude",
        ),
        CheckConstraint(
            "horizontal_accuracy_m IS NULL OR horizontal_accuracy_m > 0",
            name="ck_agent_spatial_proposal_accuracy",
        ),
        CheckConstraint(
            "reference_bundle_sha256 IS NULL OR ("
            + sha256_hex_check("reference_bundle_sha256")
            + ")",
            name="ck_agent_spatial_proposal_reference_hash",
        ),
        CheckConstraint(
            "(status = 'ground_point' AND source_annotation_id IS NOT NULL "
            "AND proposal_kind = 'legacy_ground_point' AND geometry_geojson IS NOT NULL "
            "AND geometry_origin IS NOT NULL AND longitude IS NOT NULL AND latitude IS NOT NULL "
            "AND horizontal_accuracy_m IS NOT NULL AND reference_bundle_sha256 IS NOT NULL) OR "
            "(status = 'projected_geometry' AND proposal_kind IS NOT NULL "
            "AND proposal_kind != 'legacy_ground_point' AND geometry_geojson IS NOT NULL "
            "AND observed_at IS NOT NULL AND geometry_origin IS NOT NULL "
            "AND horizontal_accuracy_m IS NOT NULL AND reference_bundle_sha256 IS NOT NULL "
            "AND (geometry_origin = 'EXPLICIT_SOURCE_GEOMETRY' "
            "OR source_annotation_id IS NOT NULL)) OR "
            "(status = 'insufficient_geometry' AND geometry_origin IS NULL "
            "AND proposal_kind IS NULL AND geometry_geojson IS NULL "
            "AND longitude IS NULL AND latitude IS NULL AND altitude_m IS NULL "
            "AND horizontal_accuracy_m IS NULL)",
            name="ck_agent_spatial_proposal_geometry_shape",
        ),
        CheckConstraint(
            "(review_state = 'PENDING' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'PENDING' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_spatial_proposal_review",
        ),
        CheckConstraint("version >= 1", name="ck_agent_spatial_proposal_version"),
    )


class AgentFactProposal(Base, TimestampMixin):
    """One typed, sourced operational fact kept private until human validation."""

    __tablename__ = "agent_fact_proposal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    analysis_window_id: Mapped[int] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_media_item_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(128), nullable=False)
    certainty: Mapped[str] = mapped_column(String(32), nullable=False)
    value_number: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    unit: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(1_000), nullable=False)
    conflict_group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    review_state: Mapped[AgentProposalReviewState] = mapped_column(
        enum_column(AgentProposalReviewState, name="agent_proposal_review_state"),
        nullable=False,
        default=AgentProposalReviewState.PENDING,
        index=True,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    analysis_window: Mapped[AgentAnalysisWindow] = relationship(back_populates="fact_proposals")
    source_media_item: Mapped[AgentMediaItem] = relationship(back_populates="fact_proposals")
    report_links: Mapped[list[AgentSituationReportFact]] = relationship(back_populates="fact")

    __table_args__ = (
        CheckConstraint(
            "category IN ('fire_activity', 'burned_area', 'resources', 'evacuation', "
            "'access', 'infrastructure', 'weather', 'other')",
            name="ck_agent_fact_category",
        ),
        CheckConstraint(
            "evidence_kind IN ('frame', 'image', 'satellite_image', 'transcript_segment', "
            "'article_text', 'metadata')",
            name="ck_agent_fact_evidence_kind",
        ),
        CheckConstraint(
            "certainty IN ('directly_visible', 'explicitly_written', 'explicitly_spoken')",
            name="ck_agent_fact_certainty",
        ),
        CheckConstraint(
            "((CASE WHEN value_number IS NULL THEN 0 ELSE 1 END) + "
            "(CASE WHEN value_text IS NULL THEN 0 ELSE 1 END) + "
            "(CASE WHEN value_boolean IS NULL THEN 0 ELSE 1 END)) = 1",
            name="ck_agent_fact_one_typed_value",
        ),
        CheckConstraint(
            "unit IS NULL OR value_number IS NOT NULL", name="ck_agent_fact_numeric_unit"
        ),
        CheckConstraint(
            "(review_state = 'PENDING' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'PENDING' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_fact_review",
        ),
        CheckConstraint("version >= 1", name="ck_agent_fact_version"),
    )


class AgentSituationReportRevision(Base, TimestampMixin):
    """Versioned private situation report; validation is distinct from publication."""

    __tablename__ = "agent_situation_report_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_revision_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    analysis_window_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    sections_payload: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    review_state: Mapped[AgentReportReviewState] = mapped_column(
        enum_column(AgentReportReviewState, name="agent_report_review_state"),
        nullable=False,
        default=AgentReportReviewState.DRAFT,
        index=True,
    )
    supersedes_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_situation_report_revision.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))

    analysis_window: Mapped[AgentAnalysisWindow] = relationship(
        back_populates="report_revisions",
        foreign_keys=[analysis_window_id, incident_id, episode_id],
        overlaps="incident,episode",
    )
    incident: Mapped[IncidentSeries] = relationship(foreign_keys=[incident_id])
    episode: Mapped[Episode] = relationship(foreign_keys=[episode_id])
    fact_links: Mapped[list[AgentSituationReportFact]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "analysis_window_id", "revision", name="uq_agent_situation_report_revision"
        ),
        ForeignKeyConstraint(
            ["analysis_window_id", "incident_id", "episode_id"],
            [
                "agent_analysis_window.id",
                "agent_analysis_window.incident_id",
                "agent_analysis_window.episode_id",
            ],
            name="fk_agent_report_analysis_window_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision >= 1", name="ck_agent_report_revision_positive"),
        CheckConstraint("length(reason) >= 10", name="ck_agent_report_reason"),
        CheckConstraint(
            "(review_state = 'DRAFT' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'DRAFT' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_report_review",
        ),
    )


class AgentSituationReportFact(Base):
    """Relational proof that a report revision only depends on stored fact proposals."""

    __tablename__ = "agent_situation_report_fact"

    report_id: Mapped[int] = mapped_column(
        ForeignKey("agent_situation_report_revision.id", ondelete="CASCADE"), primary_key=True
    )
    fact_id: Mapped[int] = mapped_column(
        ForeignKey("agent_fact_proposal.id", ondelete="RESTRICT"), primary_key=True, index=True
    )

    report: Mapped[AgentSituationReportRevision] = relationship(back_populates="fact_links")
    fact: Mapped[AgentFactProposal] = relationship(back_populates="report_links")


class AgentDispatch(Base, TimestampMixin):
    __tablename__ = "agent_dispatch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispatch_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_batch.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[AgentDispatchState] = mapped_column(
        enum_column(AgentDispatchState, name="agent_dispatch_state"), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_models: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remote_job_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    remote_status: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    delay_ms: Mapped[int | None] = mapped_column(Integer)
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    last_error_detail: Mapped[str | None] = mapped_column(String(1_000))

    batch: Mapped[AgentMediaBatch] = relationship(back_populates="dispatch")
    model_runs: Mapped[list[AgentModelRun]] = relationship(
        back_populates="dispatch", cascade="all, delete-orphan"
    )
    dead_letter: Mapped[AgentDeadLetter | None] = relationship(
        back_populates="dispatch", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        CheckConstraint(sha256_hex_check("payload_hash"), name="ck_agent_dispatch_payload_hash"),
        CheckConstraint("attempt >= 0", name="ck_agent_dispatch_attempt"),
        CheckConstraint("max_attempts >= 1", name="ck_agent_dispatch_max_attempts"),
        CheckConstraint("poll_count >= 0", name="ck_agent_dispatch_poll_count"),
        CheckConstraint(
            "execution_ms IS NULL OR execution_ms >= 0", name="ck_agent_dispatch_execution_ms"
        ),
        CheckConstraint("delay_ms IS NULL OR delay_ms >= 0", name="ck_agent_dispatch_delay_ms"),
        Index(
            "ix_agent_dispatch_claim",
            "state",
            "next_attempt_at",
            "lease_until",
        ),
    )


class AgentModelRun(Base):
    __tablename__ = "agent_model_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("agent_dispatch.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_role: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(512), nullable=False)
    revision: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[AgentModelRunState] = mapped_column(
        enum_column(AgentModelRunState, name="agent_model_run_state"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    load_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    inference_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    peak_vram_bytes: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(128))

    dispatch: Mapped[AgentDispatch] = relationship(back_populates="model_runs")

    __table_args__ = (
        UniqueConstraint("dispatch_id", "model_role", name="uq_agent_model_run_role"),
        CheckConstraint("load_ms >= 0", name="ck_agent_model_run_load_ms"),
        CheckConstraint("inference_ms >= 0", name="ck_agent_model_run_inference_ms"),
        CheckConstraint(
            "peak_vram_bytes IS NULL OR peak_vram_bytes >= 0",
            name="ck_agent_model_run_vram",
        ),
    )


class AgentDeadLetter(Base, TimestampMixin):
    __tablename__ = "agent_dead_letter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dead_letter_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    dispatch_id: Mapped[int] = mapped_column(
        ForeignKey("agent_dispatch.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[AgentDeadLetterState] = mapped_column(
        enum_column(AgentDeadLetterState, name="agent_dead_letter_state"),
        nullable=False,
        index=True,
    )
    failure_class: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str] = mapped_column(String(128), nullable=False)
    error_detail: Mapped[str] = mapped_column(String(1_000), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_job_id: Mapped[str | None] = mapped_column(String(255))
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(255))

    dispatch: Mapped[AgentDispatch] = relationship(back_populates="dead_letter")

    __table_args__ = (
        CheckConstraint(sha256_hex_check("payload_hash"), name="ck_agent_dead_letter_payload_hash"),
    )


class AgentReviewTask(Base, TimestampMixin):
    __tablename__ = "agent_review_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("agent_media_batch.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[AgentReviewState] = mapped_column(
        enum_column(AgentReviewState, name="agent_review_state"), nullable=False, index=True
    )
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(500))

    batch: Mapped[AgentMediaBatch] = relationship(back_populates="review_task")


class IncidentSpatialMarker(Base, TimestampMixin):
    """Private evidence marker projected into the incident model only at read time."""

    __tablename__ = "incident_spatial_marker"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    marker_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_media_item.id", ondelete="RESTRICT"), unique=True, index=True
    )
    marker_type: Mapped[str] = mapped_column(String(64), nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    horizontal_accuracy_m: Mapped[float | None] = mapped_column(Float)
    geometry_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    review_state: Mapped[IncidentMarkerReviewState] = mapped_column(
        enum_column(IncidentMarkerReviewState, name="incident_marker_review_state"),
        nullable=False,
        default=IncidentMarkerReviewState.PENDING,
        index=True,
    )
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    spatial_display_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_marker_longitude"),
        CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_marker_latitude"),
        CheckConstraint(
            "horizontal_accuracy_m IS NULL OR horizontal_accuracy_m > 0",
            name="ck_marker_accuracy",
        ),
        CheckConstraint(
            "geometry_origin IN ('METADATA', 'USER_DECLARED', "
            "'EXPLICIT_SOURCE_GEOMETRY', 'HUMAN_CONFIRMED')",
            name="ck_marker_geometry_origin",
        ),
        CheckConstraint("version >= 1", name="ck_marker_version"),
    )


class ActiveFireZoneRevision(Base, TimestampMixin):
    """Immutable geographic revision of the observed active-fire zone."""

    __tablename__ = "active_fire_zone_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_revision_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    analysis_window_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    geometry_geojson: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    geometry_origin: Mapped[str] = mapped_column(String(64), nullable=False)
    supporting_marker_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_revision_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    review_state: Mapped[ActiveFireZoneReviewState] = mapped_column(
        enum_column(ActiveFireZoneReviewState, name="active_fire_zone_review_state"),
        nullable=False,
        default=ActiveFireZoneReviewState.DRAFT,
        index=True,
    )
    supersedes_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("active_fire_zone_revision.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(String(500))

    analysis_window: Mapped[AgentAnalysisWindow | None] = relationship()

    __table_args__ = (
        UniqueConstraint("incident_id", "episode_id", "revision", name="uq_active_zone_revision"),
        CheckConstraint("revision >= 1", name="ck_active_zone_revision_positive"),
        CheckConstraint(
            "geometry_origin IN ('HUMAN_AUTHORED', 'DETERMINISTIC_UNION', "
            "'SATELLITE_PRODUCT', 'AGENT_DERIVED')",
            name="ck_active_zone_geometry_origin",
        ),
        CheckConstraint("length(reason) >= 10", name="ck_active_zone_reason"),
    )


class IncidentMapCapture(Base, TimestampMixin):
    """Human-published 3D map capture tied to one reviewed geographic layer."""

    __tablename__ = "incident_map_capture"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capture_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    active_zone_revision_id: Mapped[int] = mapped_column(
        ForeignKey("active_fire_zone_revision.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    object_uri: Mapped[str] = mapped_column(String(2_048), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    active_zone_revision: Mapped[ActiveFireZoneRevision] = relationship()

    __table_args__ = (
        CheckConstraint(sha256_hex_check("sha256"), name="ck_map_capture_sha256"),
        CheckConstraint("size_bytes > 0", name="ck_map_capture_size"),
        CheckConstraint(
            "media_type IN ('image/jpeg', 'image/png')", name="ck_map_capture_media_type"
        ),
        CheckConstraint("width_px >= 640 AND height_px >= 360", name="ck_map_capture_dimensions"),
    )


class ZoneArchiveSnapshot(Base):
    """The one immutable PNG capture retained when an incident is archived."""

    __tablename__ = "zone_archive_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    archive_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incident_series.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    manifest_revision_id: Mapped[int] = mapped_column(
        ForeignKey("manifest_revision.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("model_asset.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    spatial_zone_revision_id: Mapped[int] = mapped_column(
        ForeignKey("spatial_zone_revision.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    image_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/png")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    render_profile: Mapped[str] = mapped_column(String(128), nullable=False)
    rendered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    incident: Mapped[IncidentSeries] = relationship(back_populates="archive_snapshot")
    manifest_revision: Mapped[ManifestRevision] = relationship(back_populates="archive_snapshot")
    asset: Mapped[ModelAsset] = relationship(back_populates="archive_snapshots")
    spatial_zone_revision: Mapped[SpatialZoneRevision] = relationship(
        back_populates="archive_snapshots"
    )

    __table_args__ = (
        CheckConstraint("media_type = 'image/png'", name="ck_zone_archive_png"),
        CheckConstraint(sha256_hex_check("sha256"), name="ck_zone_archive_sha256"),
        CheckConstraint(sha256_hex_check("asset_sha256"), name="ck_zone_archive_asset_sha256"),
        CheckConstraint("lower(image_url) NOT LIKE '%.glb%'", name="ck_zone_archive_not_glb"),
        CheckConstraint("lower(image_url) LIKE '%.png'", name="ck_zone_archive_png_url"),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("endpoint", "idempotency_key", name="uq_idempotency_endpoint_key"),
        Index("ix_idempotency_expires", "expires_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    actor_type: Mapped[ActorType] = mapped_column(
        enum_column(ActorType, name="actor_type"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class OutboxEvent(Base):
    __tablename__ = "outbox_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),)
