"""add private incident markers and active-fire zone revisions

Revision ID: b8d4f6a9c210
Revises: a4e9c2f7d610
Create Date: 2026-07-16 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d4f6a9c210"
down_revision = "a4e9c2f7d610"
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    op.create_table(
        "incident_spatial_marker",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("marker_id", sa.String(length=128), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("source_media_item_id", sa.Integer(), nullable=True),
        sa.Column("marker_type", sa.String(length=64), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("altitude_m", sa.Float(), nullable=True),
        sa.Column("horizontal_accuracy_m", sa.Float(), nullable=True),
        sa.Column("geometry_origin", sa.String(length=64), nullable=False),
        sa.Column(
            "review_state",
            _enum("PENDING", "VALIDATED", "REJECTED", name="incident_marker_review_state"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("spatial_display_allowed", sa.Boolean(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("longitude >= -180 AND longitude <= 180", name="ck_marker_longitude"),
        sa.CheckConstraint("latitude >= -90 AND latitude <= 90", name="ck_marker_latitude"),
        sa.CheckConstraint(
            "horizontal_accuracy_m IS NULL OR horizontal_accuracy_m > 0",
            name="ck_marker_accuracy",
        ),
        sa.CheckConstraint(
            "geometry_origin IN ('METADATA', 'USER_DECLARED', "
            "'EXPLICIT_SOURCE_GEOMETRY', 'HUMAN_CONFIRMED')",
            name="ck_marker_geometry_origin",
        ),
        sa.CheckConstraint("version >= 1", name="ck_marker_version"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_media_item_id"], ["agent_media_item.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_incident_spatial_marker_marker_id",
        "incident_spatial_marker",
        ["marker_id"],
        unique=True,
    )
    op.create_index(
        "ix_incident_spatial_marker_incident_id", "incident_spatial_marker", ["incident_id"]
    )
    op.create_index(
        "ix_incident_spatial_marker_episode_id", "incident_spatial_marker", ["episode_id"]
    )
    op.create_index(
        "ix_incident_spatial_marker_source_media_item_id",
        "incident_spatial_marker",
        ["source_media_item_id"],
        unique=True,
    )
    op.create_index(
        "ix_incident_spatial_marker_review_state",
        "incident_spatial_marker",
        ["review_state"],
    )
    op.create_index(
        "ix_incident_spatial_marker_observed_at",
        "incident_spatial_marker",
        ["observed_at"],
    )

    op.create_table(
        "active_fire_zone_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("zone_revision_id", sa.String(length=128), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("geometry_geojson", sa.JSON(), nullable=False),
        sa.Column("geometry_origin", sa.String(length=64), nullable=False),
        sa.Column("supporting_marker_ids", sa.JSON(), nullable=False),
        sa.Column("source_revision_ids", sa.JSON(), nullable=False),
        sa.Column(
            "review_state",
            _enum(
                "DRAFT",
                "READY_FOR_PUBLICATION",
                "REJECTED",
                name="active_fire_zone_review_state",
            ),
            nullable=False,
        ),
        sa.Column("supersedes_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_active_zone_revision_positive"),
        sa.CheckConstraint(
            "geometry_origin IN ('HUMAN_AUTHORED', 'DETERMINISTIC_UNION', 'SATELLITE_PRODUCT')",
            name="ck_active_zone_geometry_origin",
        ),
        sa.CheckConstraint("length(reason) >= 10", name="ck_active_zone_reason"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_revision_id"],
            ["active_fire_zone_revision.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "incident_id", "episode_id", "revision", name="uq_active_zone_revision"
        ),
    )
    op.create_index(
        "ix_active_fire_zone_revision_zone_revision_id",
        "active_fire_zone_revision",
        ["zone_revision_id"],
        unique=True,
    )
    op.create_index(
        "ix_active_fire_zone_revision_incident_id",
        "active_fire_zone_revision",
        ["incident_id"],
    )
    op.create_index(
        "ix_active_fire_zone_revision_episode_id", "active_fire_zone_revision", ["episode_id"]
    )
    op.create_index(
        "ix_active_fire_zone_revision_valid_at", "active_fire_zone_revision", ["valid_at"]
    )
    op.create_index(
        "ix_active_fire_zone_revision_review_state",
        "active_fire_zone_revision",
        ["review_state"],
    )
    op.create_index(
        "ix_active_fire_zone_revision_supersedes_revision_id",
        "active_fire_zone_revision",
        ["supersedes_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_active_fire_zone_revision_supersedes_revision_id",
        table_name="active_fire_zone_revision",
    )
    op.drop_index(
        "ix_active_fire_zone_revision_review_state", table_name="active_fire_zone_revision"
    )
    op.drop_index("ix_active_fire_zone_revision_valid_at", table_name="active_fire_zone_revision")
    op.drop_index("ix_active_fire_zone_revision_episode_id", table_name="active_fire_zone_revision")
    op.drop_index(
        "ix_active_fire_zone_revision_incident_id", table_name="active_fire_zone_revision"
    )
    op.drop_index(
        "ix_active_fire_zone_revision_zone_revision_id", table_name="active_fire_zone_revision"
    )
    op.drop_table("active_fire_zone_revision")
    op.drop_index("ix_incident_spatial_marker_observed_at", table_name="incident_spatial_marker")
    op.drop_index("ix_incident_spatial_marker_review_state", table_name="incident_spatial_marker")
    op.drop_index(
        "ix_incident_spatial_marker_source_media_item_id",
        table_name="incident_spatial_marker",
    )
    op.drop_index("ix_incident_spatial_marker_episode_id", table_name="incident_spatial_marker")
    op.drop_index("ix_incident_spatial_marker_incident_id", table_name="incident_spatial_marker")
    op.drop_index("ix_incident_spatial_marker_marker_id", table_name="incident_spatial_marker")
    op.drop_table("incident_spatial_marker")
