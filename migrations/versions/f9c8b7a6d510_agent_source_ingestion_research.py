"""add private source packages and persistent source research

Revision ID: f9c8b7a6d510
Revises: d8f3a1c5b720
Create Date: 2026-07-19 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9c8b7a6d510"
down_revision: str | None = "d8f3a1c5b720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def _sha256_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _agent_media_consent_table(*, public_source: bool) -> sa.Table:
    """Describe the exact historical table for SQLite batch/offline mode."""
    basis_values = [
        "explicit_upload",
        "source_license",
        "institutional_mandate",
    ]
    if public_source:
        basis_values.append("public_source_analysis")
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            _sha256_check("evidence_sha256"),
            name="ck_agent_consent_evidence_hash",
        ),
        sa.CheckConstraint(
            "subject_reference_hash IS NULL OR (" + _sha256_check("subject_reference_hash") + ")",
            name="ck_agent_consent_subject_hash",
        ),
        sa.CheckConstraint(
            "basis != 'source_license' OR "
            "(source_reference_url IS NOT NULL AND license_identifier IS NOT NULL)",
            name="ck_agent_consent_source_license",
        ),
        sa.CheckConstraint(
            "source_reference_url IS NULL OR source_reference_url LIKE 'https://%'",
            name="ck_agent_consent_reference_https",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["agent_media_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_agent_media_consent_item_id"),
    ]
    if public_source:
        constraints.append(
            sa.CheckConstraint(
                "basis != 'public_source_analysis' OR source_reference_url IS NOT NULL",
                name="ck_agent_consent_public_source",
            )
        )
    table = sa.Table(
        "agent_media_consent",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column(
            "basis",
            _enum(*basis_values, name="agent_consent_basis"),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum("GRANTED", "WITHDRAWN", "EXPIRED", name="agent_consent_state"),
            nullable=False,
        ),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("terms_version", sa.String(length=64), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("subject_reference_hash", sa.String(length=64), nullable=True),
        sa.Column("source_reference_url", sa.String(length=2048), nullable=True),
        sa.Column("license_identifier", sa.String(length=128), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawal_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *constraints,
    )
    sa.Index("ix_agent_media_consent_state", table.c.state)
    sa.Index("ix_agent_media_consent_expires_at", table.c.expires_at)
    return table


def upgrade() -> None:
    # SQLAlchemy's historical mapping stored enum member names while the original
    # migration and constraints declared their lowercase values. Normalize the
    # existing rows before making the database contract match AgentConsentBasis.
    op.execute(
        sa.text(
            "UPDATE agent_media_consent SET basis = lower(basis) "
            "WHERE basis IN "
            "('EXPLICIT_UPLOAD', 'SOURCE_LICENSE', 'INSTITUTIONAL_MANDATE')"
        )
    )
    with op.batch_alter_table(
        "agent_media_consent",
        copy_from=_agent_media_consent_table(public_source=False),
    ) as batch_op:
        batch_op.drop_constraint("ck_agent_consent_source_license", type_="check")
        batch_op.alter_column(
            "basis",
            existing_type=_enum(
                "explicit_upload",
                "source_license",
                "institutional_mandate",
                name="agent_consent_basis",
            ),
            type_=_enum(
                "explicit_upload",
                "source_license",
                "institutional_mandate",
                "public_source_analysis",
                name="agent_consent_basis",
            ),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_consent_source_license",
            "basis != 'source_license' OR "
            "(source_reference_url IS NOT NULL AND license_identifier IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_agent_consent_public_source",
            "basis != 'public_source_analysis' OR source_reference_url IS NOT NULL",
        )
    op.create_table(
        "agent_source_package",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.String(length=128), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            _enum(
                "OPEN",
                "FINALIZING",
                "CONVERTED",
                "FAILED",
                "PURGED",
                name="agent_source_package_state",
            ),
            nullable=False,
        ),
        sa.Column("upload_id", sa.String(length=64), nullable=False),
        sa.Column("pathname_prefix", sa.String(length=512), nullable=False),
        sa.Column("declared_file_count", sa.Integer(), nullable=False),
        sa.Column("declared_total_size_bytes", sa.Integer(), nullable=False),
        sa.Column("known_start_date", sa.Date(), nullable=False),
        sa.Column("known_end_date", sa.Date(), nullable=False),
        sa.Column("location_hint", sa.String(length=500), nullable=True),
        sa.Column("analysis_authorized", sa.Boolean(), nullable=False),
        sa.Column("publication_authorized", sa.Boolean(), nullable=False),
        sa.Column("terms_version", sa.String(length=64), nullable=False),
        sa.Column("consent_evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_detail", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("declared_file_count > 0", name="ck_agent_source_package_file_count"),
        sa.CheckConstraint(
            "declared_total_size_bytes > 0", name="ck_agent_source_package_total_size"
        ),
        sa.CheckConstraint(
            "known_end_date >= known_start_date", name="ck_agent_source_package_date_order"
        ),
        sa.CheckConstraint(
            "analysis_authorized", name="ck_agent_source_package_analysis_authorized"
        ),
        sa.CheckConstraint("NOT publication_authorized", name="ck_agent_source_package_not_public"),
        sa.CheckConstraint(
            _sha256_check("consent_evidence_sha256"),
            name="ck_agent_source_package_consent_hash",
        ),
        sa.CheckConstraint(
            _sha256_check("request_hash"), name="ck_agent_source_package_request_hash"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_window_id"], ["agent_analysis_window.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("pathname_prefix"),
        sa.UniqueConstraint("upload_id"),
    )
    for column in (
        "package_id",
        "incident_id",
        "episode_id",
        "analysis_window_id",
        "state",
        "known_start_date",
        "known_end_date",
        "trace_id",
        "purge_after",
    ):
        op.create_index(
            f"ix_agent_source_package_{column}",
            "agent_source_package",
            [column],
            unique=column == "package_id",
        )

    op.create_table(
        "agent_source_package_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("agent_media_item_id", sa.Integer(), nullable=True),
        sa.Column("pathname", sa.String(length=1024), nullable=False),
        sa.Column("object_uri", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column(
            "media_type",
            _enum(
                "image",
                "video",
                "audio",
                "article",
                "satellite_image",
                name="agent_source_package_media_type",
            ),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_check("sha256"), name="ck_agent_source_package_item_hash"),
        sa.CheckConstraint("size_bytes > 0", name="ck_agent_source_package_item_size"),
        sa.ForeignKeyConstraint(
            ["agent_media_item_id"], ["agent_media_item.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["package_id"], ["agent_source_package.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_uri"),
        sa.UniqueConstraint("package_id", "pathname", name="uq_agent_source_package_item_path"),
        sa.UniqueConstraint("pathname"),
    )
    for column in (
        "item_id",
        "package_id",
        "agent_media_item_id",
        "sha256",
        "captured_at",
    ):
        op.create_index(
            f"ix_agent_source_package_item_{column}",
            "agent_source_package_item",
            [column],
            unique=column in {"item_id", "agent_media_item_id"},
        )

    op.create_table(
        "agent_source_research_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("research_id", sa.String(length=128), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            _enum(
                "QUEUED",
                "SUBMITTING",
                "RUNNING",
                "SUCCEEDED",
                "PARTIAL_FAILURE",
                "FAILED",
                "DEAD_LETTER",
                "CANCEL_REQUESTED",
                "CANCELLED",
                name="agent_source_research_state",
            ),
            nullable=False,
        ),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location_hint", sa.String(length=500), nullable=True),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("source_registry_version", sa.String(length=64), nullable=False),
        sa.Column("upload_id", sa.String(length=64), nullable=False),
        sa.Column("pathname_prefix", sa.String(length=512), nullable=False),
        sa.Column("query_plan", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("remote_job_id", sa.String(length=255), nullable=True),
        sa.Column("remote_status", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_count", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_detail", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_agent_source_research_progress",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_agent_source_research_attempt"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_agent_source_research_max_attempts"),
        sa.CheckConstraint("poll_count >= 0", name="ck_agent_source_research_poll_count"),
        sa.CheckConstraint(
            "payload_hash IS NULL OR (" + _sha256_check("payload_hash") + ")",
            name="ck_agent_source_research_payload_hash",
        ),
        sa.CheckConstraint(
            "output_hash IS NULL OR (" + _sha256_check("output_hash") + ")",
            name="ck_agent_source_research_output_hash",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_window_id"], ["agent_analysis_window.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_job_id"),
        sa.UniqueConstraint("upload_id"),
        sa.UniqueConstraint("pathname_prefix"),
    )
    for column in (
        "research_id",
        "incident_id",
        "episode_id",
        "analysis_window_id",
        "state",
        "cutoff_at",
        "trace_id",
        "purge_after",
        "lease_owner",
        "lease_until",
        "next_attempt_at",
    ):
        op.create_index(
            f"ix_agent_source_research_run_{column}",
            "agent_source_research_run",
            [column],
            unique=column == "research_id",
        )

    op.create_table(
        "agent_source_candidate",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=False),
        sa.Column("research_run_id", sa.Integer(), nullable=False),
        sa.Column("agent_media_item_id", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            _enum(
                "DISCOVERED",
                "ACCEPTED",
                "REJECTED",
                "DUPLICATE",
                name="agent_source_candidate_state",
            ),
            nullable=False,
        ),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("canonical_url_hash", sa.String(length=64), nullable=False),
        sa.Column("source_domain", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "media_type",
            _enum(
                "image",
                "video",
                "audio",
                "article",
                "satellite_image",
                name="agent_source_candidate_media_type",
            ),
            nullable=True,
        ),
        sa.Column("media_sha256", sa.String(length=64), nullable=True),
        sa.Column("object_uri", sa.String(length=1024), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("license_identifier", sa.String(length=128), nullable=True),
        sa.Column("attribution", sa.String(length=500), nullable=True),
        sa.Column("provenance_payload", sa.JSON(), nullable=False),
        sa.Column("cutoff_eligible", sa.Boolean(), nullable=False),
        sa.Column("duplicate_of_candidate_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _sha256_check("canonical_url_hash"), name="ck_agent_source_candidate_url_hash"
        ),
        sa.CheckConstraint(
            "media_sha256 IS NULL OR (" + _sha256_check("media_sha256") + ")",
            name="ck_agent_source_candidate_media_hash",
        ),
        sa.CheckConstraint(
            "canonical_url LIKE 'https://%'", name="ck_agent_source_candidate_https"
        ),
        sa.ForeignKeyConstraint(
            ["agent_media_item_id"], ["agent_media_item.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_candidate_id"], ["agent_source_candidate.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["research_run_id"], ["agent_source_research_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_run_id",
            "canonical_url_hash",
            name="uq_agent_source_candidate_run_url",
        ),
    )
    for column in (
        "candidate_id",
        "research_run_id",
        "agent_media_item_id",
        "state",
        "canonical_url_hash",
        "source_domain",
        "published_at",
        "acquired_at",
        "media_sha256",
        "duplicate_of_candidate_id",
    ):
        op.create_index(
            f"ix_agent_source_candidate_{column}",
            "agent_source_candidate",
            [column],
            unique=column in {"candidate_id", "agent_media_item_id"},
        )


def downgrade() -> None:
    for column in reversed(
        (
            "candidate_id",
            "research_run_id",
            "agent_media_item_id",
            "state",
            "canonical_url_hash",
            "source_domain",
            "published_at",
            "acquired_at",
            "media_sha256",
            "duplicate_of_candidate_id",
        )
    ):
        op.drop_index(f"ix_agent_source_candidate_{column}", table_name="agent_source_candidate")
    op.drop_table("agent_source_candidate")
    for column in reversed(
        (
            "research_id",
            "incident_id",
            "episode_id",
            "analysis_window_id",
            "state",
            "cutoff_at",
            "trace_id",
            "purge_after",
            "lease_owner",
            "lease_until",
            "next_attempt_at",
        )
    ):
        op.drop_index(
            f"ix_agent_source_research_run_{column}", table_name="agent_source_research_run"
        )
    op.drop_table("agent_source_research_run")
    for column in reversed(
        ("item_id", "package_id", "agent_media_item_id", "sha256", "captured_at")
    ):
        op.drop_index(
            f"ix_agent_source_package_item_{column}", table_name="agent_source_package_item"
        )
    op.drop_table("agent_source_package_item")
    for column in reversed(
        (
            "package_id",
            "incident_id",
            "episode_id",
            "analysis_window_id",
            "state",
            "known_start_date",
            "known_end_date",
            "trace_id",
            "purge_after",
        )
    ):
        op.drop_index(f"ix_agent_source_package_{column}", table_name="agent_source_package")
    op.drop_table("agent_source_package")
    # Research-only media cannot be represented by the previous consent enum.
    # Downgrading this feature already removes its research/package records, so
    # remove the derived media rows as part of the same destructive downgrade.
    op.execute(
        sa.text(
            "DELETE FROM agent_media_item WHERE id IN "
            "(SELECT item_id FROM agent_media_consent "
            "WHERE basis IN ('public_source_analysis', 'PUBLIC_SOURCE_ANALYSIS'))"
        )
    )
    with op.batch_alter_table(
        "agent_media_consent",
        copy_from=_agent_media_consent_table(public_source=True),
    ) as batch_op:
        batch_op.drop_constraint("ck_agent_consent_public_source", type_="check")
        batch_op.drop_constraint("ck_agent_consent_source_license", type_="check")
        batch_op.alter_column(
            "basis",
            existing_type=_enum(
                "explicit_upload",
                "source_license",
                "institutional_mandate",
                "public_source_analysis",
                name="agent_consent_basis",
            ),
            type_=_enum(
                "explicit_upload",
                "source_license",
                "institutional_mandate",
                name="agent_consent_basis",
            ),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_consent_source_license",
            "basis != 'source_license' OR "
            "(source_reference_url IS NOT NULL AND license_identifier IS NOT NULL)",
        )
    op.execute(
        sa.text(
            "UPDATE agent_media_consent SET basis = upper(basis) "
            "WHERE basis IN "
            "('explicit_upload', 'source_license', 'institutional_mandate')"
        )
    )
