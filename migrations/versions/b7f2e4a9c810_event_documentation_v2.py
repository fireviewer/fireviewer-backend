"""add the private event-documentation v2 domain

Revision ID: b7f2e4a9c810
Revises: a6c9d1e4f720
Create Date: 2026-08-03 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f2e4a9c810"
down_revision: str | None = "a6c9d1e4f720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

metadata = sa.MetaData()

# Frozen FK targets created by earlier revisions. They are metadata-only and are
# deliberately excluded from TABLES below.
sa.Table("incident_series", metadata, sa.Column("id", sa.Integer(), primary_key=True))
sa.Table("episode", metadata, sa.Column("id", sa.Integer(), primary_key=True))


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


external_provider = sa.Table(
    "external_provider",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("provider_key", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column("display_name", sa.String(255), nullable=False),
    sa.Column("allowed_domains", sa.JSON(), nullable=False),
    sa.Column("authentication_kind", sa.String(32), nullable=False),
    sa.Column("attribution", sa.String(1000), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    *_timestamps(),
    sa.CheckConstraint("length(trim(attribution)) > 0", name="ck_external_provider_attribution"),
)

external_collection = sa.Table(
    "external_collection",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column(
        "provider_id",
        sa.ForeignKey("external_provider.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("collection_key", sa.String(128), nullable=False),
    sa.Column("product_name", sa.String(255), nullable=False),
    sa.Column("sensor", sa.String(128)),
    sa.Column("platform", sa.String(128)),
    sa.Column("license", sa.String(1000), nullable=False),
    sa.Column("cadence_seconds", sa.Integer()),
    sa.Column(
        "semantic_role",
        sa.Enum(
            "RAW_EARTH_OBSERVATION",
            "SENSOR_DETECTION",
            "INTERPRETED_OBSERVATION",
            "OFFICIAL_INCIDENT_STATEMENT",
            "WEATHER_OBSERVATION",
            "WEATHER_FORECAST",
            "GEOSPATIAL_REFERENCE",
            "HISTORICAL_REGISTRY",
            "SIMULATION",
            name="external_semantic_role",
            native_enum=False,
        ),
        nullable=False,
    ),
    sa.Column("configuration", sa.JSON(), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint("provider_id", "collection_key", name="uq_external_collection_key"),
    sa.CheckConstraint("length(trim(license)) > 0", name="ck_external_collection_license"),
    sa.CheckConstraint(
        "cadence_seconds IS NULL OR cadence_seconds > 0", name="ck_external_collection_cadence"
    ),
)

external_artifact_revision = sa.Table(
    "external_artifact_revision",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("artifact_revision_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "collection_id",
        sa.ForeignKey("external_collection.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("external_product_id", sa.String(512), nullable=False),
    sa.Column("source_url", sa.String(2048), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False, index=True),
    sa.Column("etag", sa.String(512)),
    sa.Column("processing_baseline", sa.String(128)),
    sa.Column("acquisition_granule_id", sa.String(512)),
    sa.Column("acquisition_pixel_id", sa.String(255)),
    sa.Column("evidence_family_key", sa.String(64), index=True),
    sa.Column("acquisition_start_at", sa.DateTime(timezone=True)),
    sa.Column("acquisition_end_at", sa.DateTime(timezone=True)),
    sa.Column("effective_start_at", sa.DateTime(timezone=True)),
    sa.Column("effective_end_at", sa.DateTime(timezone=True)),
    sa.Column("processed_at", sa.DateTime(timezone=True)),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("forecast_run_at", sa.DateTime(timezone=True)),
    sa.Column("forecast_valid_at", sa.DateTime(timezone=True)),
    sa.Column("native_crs", sa.String(128)),
    sa.Column("footprint_geojson", sa.JSON(none_as_null=True)),
    sa.Column("resolution_m", sa.Float()),
    sa.Column("quality_flags", sa.JSON(), nullable=False),
    sa.Column("license", sa.String(1000), nullable=False),
    sa.Column("attribution", sa.String(1000), nullable=False),
    sa.Column("status", sa.String(11), nullable=False),
    sa.Column(
        "semantic_role",
        sa.Enum(
            "RAW_EARTH_OBSERVATION",
            "SENSOR_DETECTION",
            "INTERPRETED_OBSERVATION",
            "OFFICIAL_INCIDENT_STATEMENT",
            "WEATHER_OBSERVATION",
            "WEATHER_FORECAST",
            "GEOSPATIAL_REFERENCE",
            "HISTORICAL_REGISTRY",
            "SIMULATION",
            name="external_artifact_semantic_role",
            native_enum=False,
        ),
        nullable=False,
    ),
    *_timestamps(),
    sa.UniqueConstraint(
        "collection_id", "external_product_id", "revision", name="uq_external_artifact_revision"
    ),
    sa.CheckConstraint("revision >= 1", name="ck_external_artifact_revision"),
    sa.CheckConstraint(_sha256_hex_check("content_hash"), name="ck_external_artifact_hash"),
    sa.CheckConstraint("length(trim(license)) > 0", name="ck_external_artifact_license"),
    sa.CheckConstraint("length(trim(attribution)) > 0", name="ck_external_artifact_attribution"),
    sa.CheckConstraint(
        "resolution_m IS NULL OR resolution_m > 0", name="ck_external_artifact_resolution"
    ),
    sa.CheckConstraint(
        "acquisition_end_at IS NULL OR "
        "(acquisition_start_at IS NOT NULL AND acquisition_end_at >= acquisition_start_at)",
        name="ck_external_artifact_acquisition_time",
    ),
    sa.CheckConstraint(
        "effective_end_at IS NULL OR "
        "(effective_start_at IS NOT NULL AND effective_end_at >= effective_start_at)",
        name="ck_external_artifact_effective_time",
    ),
    sa.CheckConstraint(
        "(semantic_role = 'WEATHER_FORECAST' AND forecast_run_at IS NOT NULL AND "
        "forecast_valid_at IS NOT NULL AND forecast_valid_at >= forecast_run_at) OR "
        "(semantic_role != 'WEATHER_FORECAST' AND forecast_run_at IS NULL AND "
        "forecast_valid_at IS NULL)",
        name="ck_external_artifact_forecast_semantics",
    ),
    sa.CheckConstraint(
        "(footprint_geojson IS NULL AND native_crs IS NULL) OR "
        "(footprint_geojson IS NOT NULL AND native_crs IS NOT NULL)",
        name="ck_external_artifact_geometry_crs",
    ),
    sa.CheckConstraint(
        _sha256_hex_check("evidence_family_key"),
        name="ck_external_artifact_family_hash",
    ),
)

incident_candidate = sa.Table(
    "incident_candidate",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("candidate_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column("state", sa.String(16), nullable=False, index=True),
    sa.Column("origin_kind", sa.String(32), nullable=False),
    sa.Column("created_by_subject", sa.String(255), nullable=False, index=True),
    sa.Column(
        "matched_incident_id", sa.ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True
    ),
    sa.Column("reference_lon", sa.Float()),
    sa.Column("reference_lat", sa.Float()),
    sa.Column("horizontal_accuracy_m", sa.Float()),
    sa.Column(
        "source_statement_revision_id",
        sa.ForeignKey("external_artifact_revision.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    ),
    sa.Column("resolution_reason", sa.String(1000)),
    sa.Column("resolved_by", sa.String(255)),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.Column("version", sa.Integer(), nullable=False),
    *_timestamps(),
    sa.CheckConstraint(
        "origin_kind IN ('CONTRIBUTION', 'OFFICIAL_STATEMENT')", name="ck_incident_candidate_origin"
    ),
    sa.CheckConstraint(
        "(reference_lon IS NULL AND reference_lat IS NULL) OR (reference_lon BETWEEN -180 AND 180 AND reference_lat BETWEEN -90 AND 90)",
        name="ck_incident_candidate_coordinates",
    ),
    sa.CheckConstraint(
        "horizontal_accuracy_m IS NULL OR horizontal_accuracy_m > 0",
        name="ck_incident_candidate_accuracy",
    ),
    sa.CheckConstraint(
        "origin_kind != 'OFFICIAL_STATEMENT' OR source_statement_revision_id IS NOT NULL",
        name="ck_incident_candidate_official_source",
    ),
    sa.CheckConstraint("version >= 1", name="ck_incident_candidate_version"),
)

viewpoint = sa.Table(
    "viewpoint",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("viewpoint_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column("owner_subject", sa.String(255), nullable=False, index=True),
    sa.Column("longitude", sa.Float(), nullable=False),
    sa.Column("latitude", sa.Float(), nullable=False),
    sa.Column("horizontal_accuracy_m", sa.Float(), nullable=False),
    sa.Column("altitude_m", sa.Float()),
    sa.Column("label", sa.String(255)),
    sa.Column("yaw_deg", sa.Float()),
    sa.Column("fov_deg", sa.Float()),
    sa.Column("origin", sa.String(15), nullable=False),
    sa.Column("public_derivative_allowed", sa.Boolean(), nullable=False),
    *_timestamps(),
    sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_viewpoint_lon"),
    sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_viewpoint_lat"),
    sa.CheckConstraint("horizontal_accuracy_m > 0", name="ck_viewpoint_accuracy"),
    sa.CheckConstraint(
        "yaw_deg IS NULL OR yaw_deg >= 0 AND yaw_deg < 360", name="ck_viewpoint_yaw"
    ),
    sa.CheckConstraint("fov_deg IS NULL OR fov_deg > 0 AND fov_deg < 180", name="ck_viewpoint_fov"),
)

event_candidate = sa.Table(
    "event_candidate",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("candidate_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column("owner_subject", sa.String(255), nullable=False, index=True),
    sa.Column("incident_id", sa.ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True),
    sa.Column(
        "incident_candidate_id",
        sa.ForeignKey("incident_candidate.id", ondelete="RESTRICT"),
        index=True,
    ),
    sa.Column(
        "viewpoint_id",
        sa.ForeignKey("viewpoint.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    ),
    sa.Column("state", sa.String(12), nullable=False, index=True),
    sa.Column("observed_start_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("observed_end_at", sa.DateTime(timezone=True)),
    sa.Column("message", sa.Text()),
    sa.Column("consent_analysis", sa.Boolean(), nullable=False),
    sa.Column("consent_retention", sa.Boolean(), nullable=False),
    sa.Column("consent_public_derivative", sa.Boolean(), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("analysis_outbox_event_id", sa.String(64), nullable=False, unique=True),
    sa.Column("state_history", sa.JSON(), nullable=False),
    sa.Column("review_message", sa.Text()),
    sa.Column("review_context", sa.JSON(), nullable=False),
    sa.Column("failure_code", sa.String(128)),
    sa.Column("version", sa.Integer(), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint(
        "owner_subject", "idempotency_key", name="uq_event_candidate_owner_idempotency"
    ),
    sa.CheckConstraint(
        "(incident_id IS NOT NULL AND incident_candidate_id IS NULL) OR (incident_id IS NULL AND incident_candidate_id IS NOT NULL)",
        name="ck_event_candidate_incident_target",
    ),
    sa.CheckConstraint(
        "observed_end_at IS NULL OR observed_end_at >= observed_start_at",
        name="ck_event_candidate_time_window",
    ),
    sa.CheckConstraint("consent_analysis", name="ck_event_candidate_analysis_consent"),
    sa.CheckConstraint("consent_retention", name="ck_event_candidate_retention_consent"),
    sa.CheckConstraint("version >= 1", name="ck_event_candidate_version"),
)

event_analysis_job = sa.Table(
    "event_analysis_job",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("job_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "event_candidate_id",
        sa.ForeignKey("event_candidate.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    ),
    sa.Column("outbox_event_id", sa.String(64), nullable=False, unique=True),
    sa.Column(
        "state",
        sa.Enum(
            "QUEUED",
            "SUBMITTING",
            "AWAITING_REMOTE",
            "COMPLETED",
            "ABSTAINED",
            "FAILED",
            name="event_analysis_job_state",
            native_enum=False,
        ),
        nullable=False,
        index=True,
    ),
    sa.Column("remote_job_id", sa.String(255), unique=True),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("lease_owner", sa.String(255), index=True),
    sa.Column("lease_until", sa.DateTime(timezone=True), index=True),
    sa.Column("next_poll_at", sa.DateTime(timezone=True), index=True),
    sa.Column("submission_started_at", sa.DateTime(timezone=True)),
    sa.Column("submitted_at", sa.DateTime(timezone=True)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("result_sha256", sa.String(64)),
    sa.Column("result_summary", sa.JSON(), nullable=False),
    sa.Column("last_error_code", sa.String(128)),
    sa.Column("last_error_detail", sa.String(1000)),
    *_timestamps(),
    sa.CheckConstraint("attempts >= 0", name="ck_event_analysis_job_attempts"),
    sa.CheckConstraint(
        "result_sha256 IS NULL OR length(result_sha256) = 64",
        name="ck_event_analysis_job_result_hash",
    ),
    sa.CheckConstraint(
        "state != 'AWAITING_REMOTE' OR remote_job_id IS NOT NULL",
        name="ck_event_analysis_job_remote_id",
    ),
)

evidence_asset = sa.Table(
    "evidence_asset",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("asset_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column("owner_subject", sa.String(255), nullable=False, index=True),
    sa.Column(
        "event_candidate_id", sa.ForeignKey("event_candidate.id", ondelete="RESTRICT"), index=True
    ),
    sa.Column("upload_id", sa.String(64), nullable=False, index=True),
    sa.Column("file_name", sa.String(255), nullable=False),
    sa.Column("object_uri", sa.String(2048), nullable=False, unique=True),
    sa.Column("declared_media_type", sa.String(128), nullable=False),
    sa.Column("detected_media_type", sa.String(128)),
    sa.Column("size_bytes", sa.Integer(), nullable=False),
    sa.Column("sha256", sa.String(64)),
    sa.Column("state", sa.String(14), nullable=False, index=True),
    sa.Column("malware_scan_state", sa.String(8), nullable=False),
    sa.Column("metadata_payload", sa.JSON(), nullable=False),
    sa.Column("purge_after", sa.DateTime(timezone=True), index=True),
    sa.Column("purged_at", sa.DateTime(timezone=True), index=True),
    *_timestamps(),
    sa.CheckConstraint("size_bytes > 0", name="ck_evidence_asset_size"),
    sa.CheckConstraint(
        "sha256 IS NULL OR length(sha256) = 64", name="ck_evidence_asset_sha256_length"
    ),
)

localization_attempt = sa.Table(
    "localization_attempt",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("attempt_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "event_candidate_id",
        sa.ForeignKey("event_candidate.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "state",
        sa.Enum(
            "PROPOSED",
            "SHADOW",
            "SECTOR",
            "ABSTAINED",
            "FAILED",
            "REVIEWED",
            name="localization_attempt_state",
            native_enum=False,
        ),
        nullable=False,
    ),
    sa.Column("method", sa.String(128), nullable=False),
    sa.Column("model_id", sa.String(255)),
    sa.Column("model_revision", sa.String(255)),
    sa.Column("view_profile", sa.String(64), nullable=False),
    sa.Column("anchor_payload", sa.JSON(), nullable=False),
    sa.Column("geometry_geojson", sa.JSON()),
    sa.Column("uncertainty_geojson", sa.JSON()),
    sa.Column("horizontal_uncertainty_m", sa.Float()),
    sa.Column("abstention_reason", sa.String(1000)),
    sa.Column("provenance", sa.JSON(), nullable=False),
    *_timestamps(),
    sa.CheckConstraint(
        "horizontal_uncertainty_m IS NULL OR horizontal_uncertainty_m > 0",
        name="ck_localization_attempt_uncertainty",
    ),
    sa.CheckConstraint(
        "state != 'PROPOSED' OR geometry_geojson IS NOT NULL", name="ck_localization_attempt_result"
    ),
    sa.CheckConstraint(
        "state != 'ABSTAINED' OR abstention_reason IS NOT NULL",
        name="ck_localization_attempt_abstention",
    ),
)

fire_activity_event = sa.Table(
    "fire_activity_event",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("event_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "incident_id",
        sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "episode_id", sa.ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    ),
    sa.Column(
        "source_candidate_id", sa.ForeignKey("event_candidate.id", ondelete="RESTRICT"), index=True
    ),
    sa.Column(
        "localization_attempt_id",
        sa.ForeignKey("localization_attempt.id", ondelete="RESTRICT"),
        index=True,
    ),
    sa.Column("state", sa.String(17), nullable=False, index=True),
    sa.Column("phenomenon_kind", sa.String(32), nullable=False),
    sa.Column("observed_start_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("observed_end_at", sa.DateTime(timezone=True)),
    sa.Column("geometry_geojson", sa.JSON(), nullable=False),
    sa.Column("uncertainty_geojson", sa.JSON(), nullable=False),
    sa.Column("method", sa.String(128), nullable=False),
    sa.Column("analyst_validated_by", sa.String(255)),
    sa.Column("analyst_validated_at", sa.DateTime(timezone=True)),
    sa.Column("editor_published_by", sa.String(255)),
    sa.Column("editor_published_at", sa.DateTime(timezone=True)),
    sa.Column(
        "supersedes_event_id",
        sa.ForeignKey("fire_activity_event.id", ondelete="RESTRICT"),
        index=True,
    ),
    sa.Column("version", sa.Integer(), nullable=False),
    *_timestamps(),
    sa.CheckConstraint(
        "phenomenon_kind IN ('active_fire', 'visible_front', 'smoke_origin', 'thermal_hotspot')",
        name="ck_fire_activity_event_kind",
    ),
    sa.CheckConstraint(
        "observed_end_at IS NULL OR observed_end_at >= observed_start_at",
        name="ck_fire_activity_event_time_window",
    ),
    sa.CheckConstraint("version >= 1", name="ck_fire_activity_event_version"),
)

fire_activity_event_evidence = sa.Table(
    "fire_activity_event_evidence",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column(
        "fire_activity_event_id",
        sa.ForeignKey("fire_activity_event.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "evidence_asset_id",
        sa.ForeignKey("evidence_asset.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("role", sa.String(32), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint(
        "fire_activity_event_id", "evidence_asset_id", name="uq_fire_activity_event_evidence"
    ),
)

event_relation = sa.Table(
    "event_relation",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("relation_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "source_event_id",
        sa.ForeignKey("fire_activity_event.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "target_event_id",
        sa.ForeignKey("fire_activity_event.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("kind", sa.String(13), nullable=False),
    sa.Column("reason", sa.String(1000), nullable=False),
    sa.Column("created_by", sa.String(255), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint(
        "source_event_id", "target_event_id", "kind", name="uq_event_relation_pair_kind"
    ),
    sa.CheckConstraint("source_event_id != target_event_id", name="ck_event_relation_distinct"),
)

activity_envelope_revision = sa.Table(
    "activity_envelope_revision",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("envelope_id", sa.String(96), nullable=False, index=True),
    sa.Column(
        "incident_id",
        sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "episode_id", sa.ForeignKey("episode.id", ondelete="RESTRICT"), nullable=False, index=True
    ),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("effective_start_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("effective_end_at", sa.DateTime(timezone=True)),
    sa.Column("geometry_geojson", sa.JSON(), nullable=False),
    sa.Column("uncertainty_geojson", sa.JSON(), nullable=False),
    sa.Column("method", sa.String(128), nullable=False),
    sa.Column("resolution_m", sa.Float(), nullable=False),
    sa.Column("review_state", sa.String(32), nullable=False),
    sa.Column("created_by", sa.String(255), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint("envelope_id", "revision", name="uq_activity_envelope_revision"),
    sa.CheckConstraint("revision >= 1", name="ck_activity_envelope_revision"),
    sa.CheckConstraint("resolution_m > 0", name="ck_activity_envelope_resolution"),
)

activity_envelope_support = sa.Table(
    "activity_envelope_support",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column(
        "envelope_revision_id",
        sa.ForeignKey("activity_envelope_revision.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "fire_activity_event_id",
        sa.ForeignKey("fire_activity_event.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("support_role", sa.String(32), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint(
        "envelope_revision_id", "fire_activity_event_id", name="uq_activity_envelope_support"
    ),
)

progression_delta = sa.Table(
    "progression_delta",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("delta_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "incident_id",
        sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "from_envelope_revision_id",
        sa.ForeignKey("activity_envelope_revision.id", ondelete="RESTRICT"),
        index=True,
    ),
    sa.Column(
        "to_envelope_revision_id",
        sa.ForeignKey("activity_envelope_revision.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("kind", sa.String(12), nullable=False),
    sa.Column("geometry_geojson", sa.JSON()),
    sa.Column("observed_start_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("observed_end_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("method", sa.String(128), nullable=False),
    *_timestamps(),
    sa.CheckConstraint("observed_end_at >= observed_start_at", name="ck_progression_delta_time"),
    sa.CheckConstraint(
        "from_envelope_revision_id IS NULL OR from_envelope_revision_id != to_envelope_revision_id",
        name="ck_progression_delta_distinct",
    ),
)

publication_snapshot = sa.Table(
    "publication_snapshot",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("snapshot_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "incident_id",
        sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("public_payload", sa.JSON(), nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("published_by", sa.String(255), nullable=False),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "supersedes_snapshot_id",
        sa.ForeignKey("publication_snapshot.id", ondelete="RESTRICT"),
        index=True,
    ),
    sa.Column("retracted_at", sa.DateTime(timezone=True)),
    sa.Column("retracted_by", sa.String(255)),
    sa.Column("retraction_reason", sa.String(1000)),
    sa.UniqueConstraint("incident_id", "revision", name="uq_publication_snapshot_revision"),
    sa.CheckConstraint("revision >= 1", name="ck_publication_snapshot_revision"),
    sa.CheckConstraint("length(payload_sha256) = 64", name="ck_publication_snapshot_sha256"),
)

external_claim = sa.Table(
    "external_claim",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("claim_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column(
        "artifact_revision_id",
        sa.ForeignKey("external_artifact_revision.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("incident_id", sa.ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True),
    sa.Column("assertion_kind", sa.String(128), nullable=False, index=True),
    sa.Column("assertion_payload", sa.JSON(), nullable=False),
    sa.Column("geometry_geojson", sa.JSON()),
    sa.Column("confidence", sa.Float()),
    sa.Column("independent_family_key", sa.String(255), nullable=False, index=True),
    *_timestamps(),
    sa.CheckConstraint(
        "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="ck_external_claim_confidence"
    ),
)

artifact_lineage = sa.Table(
    "artifact_lineage",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column(
        "parent_revision_id",
        sa.ForeignKey("external_artifact_revision.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "child_revision_id",
        sa.ForeignKey("external_artifact_revision.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("relation", sa.String(21), nullable=False),
    sa.Column("reason", sa.String(1000), nullable=False),
    *_timestamps(),
    sa.UniqueConstraint(
        "parent_revision_id", "child_revision_id", "relation", name="uq_artifact_lineage"
    ),
    sa.CheckConstraint(
        "parent_revision_id != child_revision_id", name="ck_artifact_lineage_distinct"
    ),
)

incident_source_plan = sa.Table(
    "incident_source_plan",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("plan_id", sa.String(96), nullable=False, unique=True, index=True),
    sa.Column("incident_id", sa.ForeignKey("incident_series.id", ondelete="RESTRICT"), index=True),
    sa.Column(
        "incident_candidate_id",
        sa.ForeignKey("incident_candidate.id", ondelete="RESTRICT"),
        index=True,
    ),
    sa.Column(
        "collection_id",
        sa.ForeignKey("external_collection.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("cadence_seconds", sa.Integer(), nullable=False),
    sa.Column("watermark", sa.String(1000)),
    sa.Column("next_poll_at", sa.DateTime(timezone=True), index=True),
    sa.Column("last_success_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.String(1000)),
    sa.Column("backoff_seconds", sa.Integer(), nullable=False),
    sa.Column("lease_owner", sa.String(255), index=True),
    sa.Column("lease_token_hash", sa.String(64), unique=True),
    sa.Column("lease_acquired_at", sa.DateTime(timezone=True)),
    sa.Column("lease_until", sa.DateTime(timezone=True), index=True),
    sa.Column("configuration", sa.JSON(), nullable=False),
    *_timestamps(),
    sa.CheckConstraint(
        "(incident_id IS NOT NULL AND incident_candidate_id IS NULL) OR (incident_id IS NULL AND incident_candidate_id IS NOT NULL)",
        name="ck_incident_source_plan_target",
    ),
    sa.CheckConstraint("cadence_seconds > 0", name="ck_incident_source_plan_cadence"),
    sa.CheckConstraint("backoff_seconds >= 0", name="ck_incident_source_plan_backoff"),
    sa.CheckConstraint(
        "(lease_owner IS NULL AND lease_token_hash IS NULL AND lease_acquired_at IS NULL AND "
        "lease_until IS NULL) OR (lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL AND "
        "lease_acquired_at IS NOT NULL AND lease_until IS NOT NULL AND "
        "lease_until > lease_acquired_at)",
        name="ck_incident_source_plan_lease",
    ),
    sa.CheckConstraint(
        _sha256_hex_check("lease_token_hash"),
        name="ck_incident_source_plan_lease_token_hash",
    ),
    sa.Index(
        "uq_incident_source_plan_incident_collection",
        "incident_id",
        "collection_id",
        unique=True,
        sqlite_where=sa.text("incident_id IS NOT NULL"),
        postgresql_where=sa.text("incident_id IS NOT NULL"),
    ),
    sa.Index(
        "uq_incident_source_plan_candidate_collection",
        "incident_candidate_id",
        "collection_id",
        unique=True,
        sqlite_where=sa.text("incident_candidate_id IS NOT NULL"),
        postgresql_where=sa.text("incident_candidate_id IS NOT NULL"),
    ),
)

LEGACY_TABLES = {"incident_series", "episode"}
TABLES = tuple(table for name, table in metadata.tables.items() if name not in LEGACY_TABLES)


def _upgrade_postgis() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    columns = {
        "incident_candidate": (("reference_geom", "Point"),),
        "viewpoint": (("point_geom", "Point"),),
        "localization_attempt": (("result_geom", "Geometry"), ("uncertainty_geom", "Geometry")),
        "fire_activity_event": (("event_geom", "Geometry"), ("uncertainty_geom", "Geometry")),
        "activity_envelope_revision": (
            ("envelope_geom", "MultiPolygon"),
            ("uncertainty_geom", "Geometry"),
        ),
        "progression_delta": (("delta_geom", "Geometry"),),
        "external_artifact_revision": (("footprint_geom", "Geometry"),),
        "external_claim": (("claim_geom", "Geometry"),),
    }
    for table_name, definitions in columns.items():
        for column_name, geometry_type in definitions:
            op.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} geometry({geometry_type}, 4326)"
            )
            op.execute(
                f"CREATE INDEX ix_{table_name}_{column_name}_gist ON {table_name} USING gist ({column_name})"
            )
    op.execute(
        "UPDATE viewpoint SET point_geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)"
    )
    op.execute("ALTER TABLE viewpoint ALTER COLUMN point_geom SET NOT NULL")
    op.execute(
        "CREATE FUNCTION fire_viewer_sync_v2_geometry() RETURNS trigger AS $$ "
        "BEGIN "
        "IF TG_TABLE_NAME = 'viewpoint' THEN NEW.point_geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326); "
        "ELSIF TG_TABLE_NAME = 'incident_candidate' THEN NEW.reference_geom := CASE WHEN NEW.reference_lon IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(NEW.reference_lon, NEW.reference_lat), 4326) END; "
        "ELSIF TG_TABLE_NAME = 'localization_attempt' THEN NEW.result_geom := CASE WHEN NEW.geometry_geojson IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(NEW.geometry_geojson::text), 4326) END; NEW.uncertainty_geom := CASE WHEN NEW.uncertainty_geojson IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(NEW.uncertainty_geojson::text), 4326) END; "
        "ELSIF TG_TABLE_NAME = 'fire_activity_event' THEN NEW.event_geom := ST_SetSRID(ST_GeomFromGeoJSON(NEW.geometry_geojson::text), 4326); NEW.uncertainty_geom := ST_SetSRID(ST_GeomFromGeoJSON(NEW.uncertainty_geojson::text), 4326); "
        "ELSIF TG_TABLE_NAME = 'activity_envelope_revision' THEN NEW.envelope_geom := ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(NEW.geometry_geojson::text), 4326)); NEW.uncertainty_geom := ST_SetSRID(ST_GeomFromGeoJSON(NEW.uncertainty_geojson::text), 4326); "
        "ELSIF TG_TABLE_NAME = 'progression_delta' THEN NEW.delta_geom := CASE WHEN NEW.geometry_geojson IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(NEW.geometry_geojson::text), 4326) END; "
        "ELSIF TG_TABLE_NAME = 'external_artifact_revision' THEN NEW.footprint_geom := CASE WHEN NEW.footprint_geojson IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(NEW.footprint_geojson::text), 4326) END; "
        "ELSIF TG_TABLE_NAME = 'external_claim' THEN NEW.claim_geom := CASE WHEN NEW.geometry_geojson IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON(NEW.geometry_geojson::text), 4326) END; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    trigger_columns = {
        "viewpoint": "longitude, latitude",
        "incident_candidate": "reference_lon, reference_lat",
        "localization_attempt": "geometry_geojson, uncertainty_geojson",
        "fire_activity_event": "geometry_geojson, uncertainty_geojson",
        "activity_envelope_revision": "geometry_geojson, uncertainty_geojson",
        "progression_delta": "geometry_geojson",
        "external_artifact_revision": "footprint_geojson",
        "external_claim": "geometry_geojson",
    }
    for table_name, source_columns in trigger_columns.items():
        op.execute(
            f"CREATE TRIGGER {table_name}_sync_v2_geometry BEFORE INSERT OR UPDATE OF {source_columns} ON {table_name} FOR EACH ROW EXECUTE FUNCTION fire_viewer_sync_v2_geometry()"
        )


def _upgrade_external_registry_immutability() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in ("external_artifact_revision", "external_claim", "artifact_lineage"):
            op.execute(
                f"CREATE TRIGGER {table_name}_no_update BEFORE UPDATE ON {table_name} "
                f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table_name}_no_delete BEFORE DELETE ON {table_name} "
                f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
            )
        return
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION fire_viewer_external_registry_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; "
            "$$ LANGUAGE plpgsql"
        )
        for table_name in ("external_artifact_revision", "external_claim", "artifact_lineage"):
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION fire_viewer_external_registry_immutable()"
            )


def _downgrade_external_registry_immutability() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in ("external_artifact_revision", "external_claim", "artifact_lineage"):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_no_delete")
        return
    if dialect == "postgresql":
        for table_name in ("external_artifact_revision", "external_claim", "artifact_lineage"):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_immutable ON {table_name}")
        op.execute("DROP FUNCTION IF EXISTS fire_viewer_external_registry_immutable()")


def _upgrade_publication_snapshot_immutability() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER publication_snapshot_payload_no_update "
            "BEFORE UPDATE ON publication_snapshot WHEN "
            "NEW.incident_id IS NOT OLD.incident_id OR "
            "NEW.revision IS NOT OLD.revision OR "
            "NEW.public_payload IS NOT OLD.public_payload OR "
            "NEW.payload_sha256 IS NOT OLD.payload_sha256 OR "
            "NEW.published_by IS NOT OLD.published_by OR "
            "NEW.published_at IS NOT OLD.published_at "
            "BEGIN SELECT RAISE(ABORT, 'publication_snapshot payload is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER publication_snapshot_retraction_guard "
            "BEFORE UPDATE ON publication_snapshot WHEN "
            "(NEW.retracted_at IS NOT OLD.retracted_at OR "
            "NEW.retracted_by IS NOT OLD.retracted_by OR "
            "NEW.retraction_reason IS NOT OLD.retraction_reason) AND ("
            "OLD.retracted_at IS NOT NULL OR NEW.retracted_at IS NULL OR "
            "NEW.retracted_by IS NULL OR length(trim(NEW.retracted_by)) = 0 OR "
            "NEW.retraction_reason IS NULL OR length(trim(NEW.retraction_reason)) = 0) "
            "BEGIN SELECT RAISE(ABORT, 'publication_snapshot retraction is invalid'); END"
        )
        op.execute(
            "CREATE TRIGGER publication_snapshot_no_delete BEFORE DELETE ON publication_snapshot "
            "BEGIN SELECT RAISE(ABORT, 'publication_snapshot is append-only'); END"
        )
        return
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION fire_viewer_publication_snapshot_guard() RETURNS trigger AS $$ "
            "BEGIN IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'publication_snapshot is append-only'; "
            "END IF; IF NEW.incident_id IS DISTINCT FROM OLD.incident_id OR "
            "NEW.revision IS DISTINCT FROM OLD.revision OR "
            "NEW.public_payload IS DISTINCT FROM OLD.public_payload OR "
            "NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256 OR "
            "NEW.published_by IS DISTINCT FROM OLD.published_by OR "
            "NEW.published_at IS DISTINCT FROM OLD.published_at THEN "
            "RAISE EXCEPTION 'publication_snapshot payload is immutable'; END IF; "
            "IF OLD.retracted_at IS NOT NULL OR NEW.retracted_at IS NULL OR "
            "NEW.retracted_by IS NULL OR length(trim(NEW.retracted_by)) = 0 OR "
            "NEW.retraction_reason IS NULL OR length(trim(NEW.retraction_reason)) = 0 THEN "
            "RAISE EXCEPTION 'publication_snapshot retraction is invalid'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER publication_snapshot_guard BEFORE UPDATE OR DELETE "
            "ON publication_snapshot FOR EACH ROW "
            "EXECUTE FUNCTION fire_viewer_publication_snapshot_guard()"
        )


def _downgrade_publication_snapshot_immutability() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger_name in (
            "publication_snapshot_payload_no_update",
            "publication_snapshot_retraction_guard",
            "publication_snapshot_no_delete",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        return
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS publication_snapshot_guard ON publication_snapshot")
        op.execute("DROP FUNCTION IF EXISTS fire_viewer_publication_snapshot_guard()")


def upgrade() -> None:
    metadata.create_all(op.get_bind(), tables=list(TABLES), checkfirst=False)
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgis()
    _upgrade_external_registry_immutability()
    _upgrade_publication_snapshot_immutability()


def downgrade() -> None:
    _downgrade_publication_snapshot_immutability()
    _downgrade_external_registry_immutability()
    if op.get_bind().dialect.name == "postgresql":
        for table_name in (
            "external_claim",
            "external_artifact_revision",
            "progression_delta",
            "activity_envelope_revision",
            "fire_activity_event",
            "localization_attempt",
            "incident_candidate",
            "viewpoint",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_sync_v2_geometry ON {table_name}")
        op.execute("DROP FUNCTION IF EXISTS fire_viewer_sync_v2_geometry()")
    metadata.drop_all(op.get_bind(), tables=list(reversed(TABLES)), checkfirst=True)
