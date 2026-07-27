"""add private incident intelligence windows, proposals, facts and reports

Revision ID: e1c7a9b4d620
Revises: d7c5e3a1b920
Create Date: 2026-07-18 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1c7a9b4d620"
down_revision: str | None = "d7c5e3a1b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def _sha256_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _sqlite_agent_media_batch_v1() -> sa.Table:
    """Agent batch schema before the analysis-window relation was added."""

    metadata = sa.MetaData()
    table = sa.Table(
        "agent_media_batch",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("batch_type", _enum("user_media", "external_media", "satellite_media", name="agent_batch_type"), nullable=False),
        sa.Column("priority", _enum("user_deadline", "scheduled_combined", "scheduled", name="agent_batch_priority"), nullable=False),
        sa.Column("state", _enum("DRAFT", "QUEUED", "SUBMITTING", "RUNNING", "SUCCEEDED", "PARTIAL_FAILURE", "FAILED", "DEAD_LETTER", "CANCEL_REQUESTED", "CANCELLED", name="agent_batch_state"), nullable=False),
        sa.Column("incident_id", sa.Integer()),
        sa.Column("episode_id", sa.Integer()),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64)),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = '1.0'", name="ck_agent_batch_schema_version"),
        sa.CheckConstraint("(incident_id IS NULL AND episode_id IS NULL) OR (incident_id IS NOT NULL AND episode_id IS NOT NULL)", name="ck_agent_batch_incident_episode_pair"),
        sa.CheckConstraint(_sha256_check("request_hash"), name="ck_agent_batch_request_hash"),
        sa.CheckConstraint("payload_hash IS NULL OR (" + _sha256_check("payload_hash") + ")", name="ck_agent_batch_payload_hash"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_media_batch_idempotency_key"),
    )
    for name, column, unique in (
        ("ix_agent_media_batch_batch_id", table.c.batch_id, True),
        ("ix_agent_media_batch_state", table.c.state, False),
        ("ix_agent_media_batch_incident_id", table.c.incident_id, False),
        ("ix_agent_media_batch_episode_id", table.c.episode_id, False),
        ("ix_agent_media_batch_trace_id", table.c.trace_id, False),
        ("ix_agent_media_batch_deadline_at", table.c.deadline_at, False),
        ("ix_agent_media_batch_purge_after", table.c.purge_after, False),
    ):
        sa.Index(name, column, unique=unique)
    return table


def upgrade() -> None:
    op.create_table(
        "agent_analysis_window",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            _enum(
                "COLLECTING",
                "PROCESSING",
                "REVIEW_PENDING",
                "COMPLETED",
                "CANCELLED",
                name="agent_analysis_state",
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "window_end_at > window_start_at", name="ck_agent_analysis_window_order"
        ),
        sa.CheckConstraint("length(timezone) >= 3", name="ck_agent_analysis_timezone"),
        sa.CheckConstraint("version >= 1", name="ck_agent_analysis_version"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incident_series.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "incident_id", "episode_id", "local_date", name="uq_agent_analysis_local_day"
        ),
        sa.UniqueConstraint(
            "id", "incident_id", "episode_id", name="uq_agent_analysis_window_identity"
        ),
    )
    op.create_index(
        "ix_agent_analysis_window_analysis_id",
        "agent_analysis_window",
        ["analysis_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_analysis_window_incident_id", "agent_analysis_window", ["incident_id"]
    )
    op.create_index(
        "ix_agent_analysis_window_episode_id", "agent_analysis_window", ["episode_id"]
    )
    op.create_index(
        "ix_agent_analysis_window_local_date", "agent_analysis_window", ["local_date"]
    )
    op.create_index("ix_agent_analysis_window_state", "agent_analysis_window", ["state"])

    with op.batch_alter_table(
        "agent_media_batch",
        recreate="always",
        naming_convention=_FK_NAMING_CONVENTION,
        copy_from=_sqlite_agent_media_batch_v1(),
    ) as batch_op:
        batch_op.add_column(sa.Column("analysis_window_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "reference_bundle_payload",
                sa.JSON(none_as_null=True),
                nullable=True,
            )
        )
        batch_op.drop_constraint("ck_agent_batch_schema_version", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_batch_schema_version", "schema_version IN ('1.0', '2.0')"
        )
        batch_op.create_check_constraint(
            "ck_agent_batch_analysis_window_version",
            "(schema_version = '1.0' AND analysis_window_id IS NULL "
            "AND reference_bundle_payload IS NULL) OR "
            "(schema_version = '2.0' AND analysis_window_id IS NOT NULL "
            "AND incident_id IS NOT NULL AND episode_id IS NOT NULL)",
        )
        batch_op.create_foreign_key(
            "fk_agent_batch_analysis_window_identity",
            "agent_analysis_window",
            ["analysis_window_id", "incident_id", "episode_id"],
            ["id", "incident_id", "episode_id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_agent_media_batch_analysis_window_id",
        "agent_media_batch",
        ["analysis_window_id"],
    )

    op.create_table(
        "agent_source_annotation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("annotation_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=False),
        sa.Column("source_media_item_id", sa.Integer(), nullable=False),
        sa.Column("evidence_id", sa.String(length=128), nullable=False),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("semantic_anchor", sa.String(length=64), nullable=False),
        sa.Column("source_x_normalized", sa.Float(), nullable=False),
        sa.Column("source_y_normalized", sa.Float(), nullable=False),
        sa.Column("model_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "evidence_kind IN ('image', 'frame', 'satellite_image')",
            name="ck_agent_annotation_evidence_kind",
        ),
        sa.CheckConstraint(
            "semantic_anchor IN ('active_fire_point', 'visible_fire_front_point', "
            "'smoke_column_base')",
            name="ck_agent_annotation_semantic_anchor",
        ),
        sa.CheckConstraint(
            "source_x_normalized >= 0 AND source_x_normalized <= 1",
            name="ck_agent_annotation_x",
        ),
        sa.CheckConstraint(
            "source_y_normalized >= 0 AND source_y_normalized <= 1",
            name="ck_agent_annotation_y",
        ),
        sa.CheckConstraint(
            "model_score IS NULL OR (model_score >= 0 AND model_score <= 1)",
            name="ck_agent_annotation_score",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_window_id"], ["agent_analysis_window.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_media_item_id"], ["agent_media_item.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_source_annotation_annotation_id",
        "agent_source_annotation",
        ["annotation_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_source_annotation_analysis_window_id",
        "agent_source_annotation",
        ["analysis_window_id"],
    )
    op.create_index(
        "ix_agent_source_annotation_source_media_item_id",
        "agent_source_annotation",
        ["source_media_item_id"],
    )

    op.create_table(
        "agent_spatial_proposal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=False),
        sa.Column("source_media_item_id", sa.Integer(), nullable=False),
        sa.Column("source_annotation_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("geometry_origin", sa.String(length=64), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("altitude_m", sa.Float(), nullable=True),
        sa.Column("horizontal_accuracy_m", sa.Float(), nullable=True),
        sa.Column("reference_bundle_sha256", sa.String(length=64), nullable=True),
        sa.Column("uncertainty_codes", sa.JSON(), nullable=False),
        sa.Column(
            "review_state",
            _enum(
                "PENDING",
                "VALIDATED",
                "REJECTED",
                "INVALIDATED",
                name="agent_proposal_review_state",
            ),
            nullable=False,
        ),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ground_point', 'insufficient_geometry')",
            name="ck_agent_spatial_proposal_status",
        ),
        sa.CheckConstraint(
            "geometry_origin IS NULL OR geometry_origin IN "
            "('SATELLITE_GEOTRANSFORM', 'CAMERA_RAYCAST', 'CROSS_VIEW_RAYCAST', "
            "'EXPLICIT_SOURCE_GEOMETRY')",
            name="ck_agent_spatial_proposal_origin",
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="ck_agent_spatial_proposal_longitude",
        ),
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="ck_agent_spatial_proposal_latitude",
        ),
        sa.CheckConstraint(
            "horizontal_accuracy_m IS NULL OR horizontal_accuracy_m > 0",
            name="ck_agent_spatial_proposal_accuracy",
        ),
        sa.CheckConstraint(
            "reference_bundle_sha256 IS NULL OR ("
            + _sha256_check("reference_bundle_sha256")
            + ")",
            name="ck_agent_spatial_proposal_reference_hash",
        ),
        sa.CheckConstraint(
            "(status = 'ground_point' AND source_annotation_id IS NOT NULL "
            "AND geometry_origin IS NOT NULL AND longitude IS NOT NULL AND latitude IS NOT NULL "
            "AND horizontal_accuracy_m IS NOT NULL AND reference_bundle_sha256 IS NOT NULL) OR "
            "(status = 'insufficient_geometry' AND geometry_origin IS NULL "
            "AND longitude IS NULL AND latitude IS NULL AND altitude_m IS NULL "
            "AND horizontal_accuracy_m IS NULL)",
            name="ck_agent_spatial_proposal_geometry_shape",
        ),
        sa.CheckConstraint(
            "(review_state = 'PENDING' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'PENDING' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_spatial_proposal_review",
        ),
        sa.CheckConstraint("version >= 1", name="ck_agent_spatial_proposal_version"),
        sa.ForeignKeyConstraint(
            ["analysis_window_id"], ["agent_analysis_window.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_annotation_id"], ["agent_source_annotation.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_media_item_id"], ["agent_media_item.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "analysis_window_id",
        "source_media_item_id",
        "source_annotation_id",
        "status",
        "observed_at",
        "review_state",
    ):
        op.create_index(
            f"ix_agent_spatial_proposal_{column}", "agent_spatial_proposal", [column]
        )
    op.create_index(
        "ix_agent_spatial_proposal_proposal_id",
        "agent_spatial_proposal",
        ["proposal_id"],
        unique=True,
    )

    op.create_table(
        "agent_fact_proposal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fact_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=False),
        sa.Column("source_media_item_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("fact_key", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("evidence_id", sa.String(length=128), nullable=False),
        sa.Column("certainty", sa.String(length=32), nullable=False),
        sa.Column("value_number", sa.Float(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("conflict_group_id", sa.String(length=128), nullable=True),
        sa.Column(
            "review_state",
            _enum(
                "PENDING",
                "VALIDATED",
                "REJECTED",
                "INVALIDATED",
                name="agent_proposal_review_state",
            ),
            nullable=False,
        ),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('fire_activity', 'burned_area', 'resources', 'evacuation', "
            "'access', 'infrastructure', 'weather', 'other')",
            name="ck_agent_fact_category",
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('frame', 'image', 'satellite_image', 'transcript_segment', "
            "'article_text', 'metadata')",
            name="ck_agent_fact_evidence_kind",
        ),
        sa.CheckConstraint(
            "certainty IN ('directly_visible', 'explicitly_written', 'explicitly_spoken')",
            name="ck_agent_fact_certainty",
        ),
        sa.CheckConstraint(
            "((CASE WHEN value_number IS NULL THEN 0 ELSE 1 END) + "
            "(CASE WHEN value_text IS NULL THEN 0 ELSE 1 END) + "
            "(CASE WHEN value_boolean IS NULL THEN 0 ELSE 1 END)) = 1",
            name="ck_agent_fact_one_typed_value",
        ),
        sa.CheckConstraint(
            "unit IS NULL OR value_number IS NOT NULL", name="ck_agent_fact_numeric_unit"
        ),
        sa.CheckConstraint(
            "(review_state = 'PENDING' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'PENDING' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_fact_review",
        ),
        sa.CheckConstraint("version >= 1", name="ck_agent_fact_version"),
        sa.ForeignKeyConstraint(
            ["analysis_window_id"], ["agent_analysis_window.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_media_item_id"], ["agent_media_item.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "analysis_window_id",
        "source_media_item_id",
        "category",
        "as_of",
        "conflict_group_id",
        "review_state",
    ):
        op.create_index(f"ix_agent_fact_proposal_{column}", "agent_fact_proposal", [column])
    op.create_index(
        "ix_agent_fact_proposal_fact_id",
        "agent_fact_proposal",
        ["fact_id"],
        unique=True,
    )

    op.create_table(
        "agent_situation_report_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_revision_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("sections_payload", sa.JSON(), nullable=False),
        sa.Column(
            "review_state",
            _enum(
                "DRAFT",
                "VALIDATED",
                "REJECTED",
                "INVALIDATED",
                name="agent_report_review_state",
            ),
            nullable=False,
        ),
        sa.Column("supersedes_report_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_agent_report_revision_positive"),
        sa.CheckConstraint("length(reason) >= 10", name="ck_agent_report_reason"),
        sa.CheckConstraint(
            "(review_state = 'DRAFT' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'DRAFT' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_report_review",
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incident_series.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_window_id", "incident_id", "episode_id"],
            [
                "agent_analysis_window.id",
                "agent_analysis_window.incident_id",
                "agent_analysis_window.episode_id",
            ],
            name="fk_agent_report_analysis_window_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_report_id"],
            ["agent_situation_report_revision.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_window_id", "revision", name="uq_agent_situation_report_revision"
        ),
    )
    for column in (
        "analysis_window_id",
        "incident_id",
        "episode_id",
        "review_state",
        "supersedes_report_id",
    ):
        op.create_index(
            f"ix_agent_situation_report_revision_{column}",
            "agent_situation_report_revision",
            [column],
        )
    op.create_index(
        "ix_agent_situation_report_revision_report_revision_id",
        "agent_situation_report_revision",
        ["report_revision_id"],
        unique=True,
    )

    op.create_table(
        "agent_situation_report_fact",
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["fact_id"], ["agent_fact_proposal.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["report_id"], ["agent_situation_report_revision.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("report_id", "fact_id"),
    )
    op.create_index(
        "ix_agent_situation_report_fact_fact_id",
        "agent_situation_report_fact",
        ["fact_id"],
    )


def downgrade() -> None:
    incompatible_batch = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT batch_id FROM agent_media_batch "
                "WHERE schema_version != '1.0' OR analysis_window_id IS NOT NULL LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if incompatible_batch is not None:
        raise RuntimeError(
            "Cannot downgrade incident intelligence persistence while v2 batch "
            f"{incompatible_batch!r} exists"
        )

    op.drop_index("ix_agent_situation_report_fact_fact_id", table_name="agent_situation_report_fact")
    op.drop_table("agent_situation_report_fact")
    op.drop_index(
        "ix_agent_situation_report_revision_report_revision_id",
        table_name="agent_situation_report_revision",
    )
    for column in reversed(
        (
            "analysis_window_id",
            "incident_id",
            "episode_id",
            "review_state",
            "supersedes_report_id",
        )
    ):
        op.drop_index(
            f"ix_agent_situation_report_revision_{column}",
            table_name="agent_situation_report_revision",
        )
    op.drop_table("agent_situation_report_revision")

    op.drop_index("ix_agent_fact_proposal_fact_id", table_name="agent_fact_proposal")
    for column in reversed(
        (
            "analysis_window_id",
            "source_media_item_id",
            "category",
            "as_of",
            "conflict_group_id",
            "review_state",
        )
    ):
        op.drop_index(f"ix_agent_fact_proposal_{column}", table_name="agent_fact_proposal")
    op.drop_table("agent_fact_proposal")

    op.drop_index("ix_agent_spatial_proposal_proposal_id", table_name="agent_spatial_proposal")
    for column in reversed(
        (
            "analysis_window_id",
            "source_media_item_id",
            "source_annotation_id",
            "status",
            "observed_at",
            "review_state",
        )
    ):
        op.drop_index(f"ix_agent_spatial_proposal_{column}", table_name="agent_spatial_proposal")
    op.drop_table("agent_spatial_proposal")

    op.drop_index(
        "ix_agent_source_annotation_source_media_item_id", table_name="agent_source_annotation"
    )
    op.drop_index(
        "ix_agent_source_annotation_analysis_window_id", table_name="agent_source_annotation"
    )
    op.drop_index(
        "ix_agent_source_annotation_annotation_id", table_name="agent_source_annotation"
    )
    op.drop_table("agent_source_annotation")

    op.drop_index("ix_agent_media_batch_analysis_window_id", table_name="agent_media_batch")
    with op.batch_alter_table(
        "agent_media_batch", naming_convention=_FK_NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint("fk_agent_batch_analysis_window_identity", type_="foreignkey")
        batch_op.drop_constraint("ck_agent_batch_analysis_window_version", type_="check")
        batch_op.drop_constraint("ck_agent_batch_schema_version", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_batch_schema_version", "schema_version = '1.0'"
        )
        batch_op.drop_column("reference_bundle_payload")
        batch_op.drop_column("analysis_window_id")

    op.drop_index("ix_agent_analysis_window_state", table_name="agent_analysis_window")
    op.drop_index("ix_agent_analysis_window_local_date", table_name="agent_analysis_window")
    op.drop_index("ix_agent_analysis_window_episode_id", table_name="agent_analysis_window")
    op.drop_index("ix_agent_analysis_window_incident_id", table_name="agent_analysis_window")
    op.drop_index("ix_agent_analysis_window_analysis_id", table_name="agent_analysis_window")
    op.drop_table("agent_analysis_window")
