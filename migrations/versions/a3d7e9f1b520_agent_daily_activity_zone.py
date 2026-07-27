"""link agent daily activity zones to their analysis window

Revision ID: a3d7e9f1b520
Revises: f9c8b7a6d510
Create Date: 2026-07-19 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d7e9f1b520"
down_revision: str | None = "f9c8b7a6d510"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _active_fire_zone_revision_table(*, agent_derived: bool) -> sa.Table:
    """Describe both sides of the SQLite batch migration without reflection."""
    geometry_origins = (
        "'HUMAN_AUTHORED', 'DETERMINISTIC_UNION', 'SATELLITE_PRODUCT', 'AGENT_DERIVED'"
        if agent_derived
        else "'HUMAN_AUTHORED', 'DETERMINISTIC_UNION', 'SATELLITE_PRODUCT'"
    )
    columns: list[sa.SchemaItem] = [
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
            sa.Enum(
                "DRAFT",
                "READY_FOR_PUBLICATION",
                "REJECTED",
                name="active_fire_zone_review_state",
                native_enum=False,
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
    ]
    if agent_derived:
        columns.append(sa.Column("analysis_window_id", sa.Integer(), nullable=True))
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint("revision >= 1", name="ck_active_zone_revision_positive"),
        sa.CheckConstraint(
            f"geometry_origin IN ({geometry_origins})",
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
            "incident_id",
            "episode_id",
            "revision",
            name="uq_active_zone_revision",
        ),
    ]
    if agent_derived:
        constraints.append(
            sa.ForeignKeyConstraint(
                ["analysis_window_id"],
                ["agent_analysis_window.id"],
                name="fk_active_fire_zone_analysis_window",
                ondelete="RESTRICT",
            )
        )
    table = sa.Table(
        "active_fire_zone_revision",
        sa.MetaData(),
        *columns,
        *constraints,
    )
    sa.Index(
        "ix_active_fire_zone_revision_zone_revision_id",
        table.c.zone_revision_id,
        unique=True,
    )
    for name in (
        "incident_id",
        "episode_id",
        "valid_at",
        "review_state",
        "supersedes_revision_id",
    ):
        sa.Index(f"ix_active_fire_zone_revision_{name}", table.c[name])
    if agent_derived:
        sa.Index(
            "ix_active_fire_zone_revision_analysis_window_id",
            table.c.analysis_window_id,
        )
    return table


def upgrade() -> None:
    with op.batch_alter_table(
        "active_fire_zone_revision",
        copy_from=_active_fire_zone_revision_table(agent_derived=False),
    ) as batch:
        batch.add_column(sa.Column("analysis_window_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_active_fire_zone_analysis_window",
            "agent_analysis_window",
            ["analysis_window_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_active_fire_zone_revision_analysis_window_id",
            ["analysis_window_id"],
            unique=False,
        )
        batch.drop_constraint("ck_active_zone_geometry_origin", type_="check")
        batch.create_check_constraint(
            "ck_active_zone_geometry_origin",
            "geometry_origin IN ('HUMAN_AUTHORED', 'DETERMINISTIC_UNION', "
            "'SATELLITE_PRODUCT', 'AGENT_DERIVED')",
        )
    op.create_table(
        "incident_map_capture",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("capture_id", sa.String(128), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("active_zone_revision_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("object_uri", sa.String(2_048), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["active_zone_revision_id"],
            ["active_fire_zone_revision.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("object_uri", name="uq_incident_map_capture_object_uri"),
        sa.CheckConstraint(_sha256_hex_check("sha256"), name="ck_map_capture_sha256"),
        sa.CheckConstraint("size_bytes > 0", name="ck_map_capture_size"),
        sa.CheckConstraint(
            "media_type IN ('image/jpeg', 'image/png')", name="ck_map_capture_media_type"
        ),
        sa.CheckConstraint(
            "width_px >= 640 AND height_px >= 360", name="ck_map_capture_dimensions"
        ),
    )
    op.create_index(
        "ix_incident_map_capture_capture_id", "incident_map_capture", ["capture_id"], unique=True
    )
    op.create_index(
        "ix_incident_map_capture_incident_id", "incident_map_capture", ["incident_id"]
    )
    op.create_index(
        "ix_incident_map_capture_episode_id", "incident_map_capture", ["episode_id"]
    )
    op.create_index(
        "ix_incident_map_capture_active_zone_revision_id",
        "incident_map_capture",
        ["active_zone_revision_id"],
    )
    op.create_index(
        "ix_incident_map_capture_local_date", "incident_map_capture", ["local_date"]
    )


def downgrade() -> None:
    op.drop_table("incident_map_capture")
    with op.batch_alter_table(
        "active_fire_zone_revision",
        copy_from=_active_fire_zone_revision_table(agent_derived=True),
    ) as batch:
        batch.drop_constraint("ck_active_zone_geometry_origin", type_="check")
        batch.create_check_constraint(
            "ck_active_zone_geometry_origin",
            "geometry_origin IN ('HUMAN_AUTHORED', 'DETERMINISTIC_UNION', 'SATELLITE_PRODUCT')",
        )
        batch.drop_index("ix_active_fire_zone_revision_analysis_window_id")
        batch.drop_constraint("fk_active_fire_zone_analysis_window", type_="foreignkey")
        batch.drop_column("analysis_window_id")
