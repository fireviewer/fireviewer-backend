"""add direct local zone uploads and reviewed information

Revision ID: d9e4c2a8f610
Revises: b4d1e6f9a730
Create Date: 2026-07-14 23:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9e4c2a8f610"
down_revision: str | None = "b4d1e6f9a730"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def upgrade() -> None:
    op.create_table(
        "zone_profile",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spatial_zone_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=4_000), nullable=False),
        sa.Column(
            "visibility",
            sa.Enum(
                "DRAFT",
                "PUBLISHED",
                "HIDDEN",
                "ARCHIVED",
                name="zone_visibility",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("min_easting_l93", sa.Float(), nullable=False),
        sa.Column("min_northing_l93", sa.Float(), nullable=False),
        sa.Column("max_easting_l93", sa.Float(), nullable=False),
        sa.Column("max_northing_l93", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "min_easting_l93 < max_easting_l93 AND min_northing_l93 < max_northing_l93",
            name="ck_zone_profile_l93_bounds",
        ),
        sa.ForeignKeyConstraint(["spatial_zone_id"], ["spatial_zone.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_zone_profile_spatial_zone_id"),
        "zone_profile",
        ["spatial_zone_id"],
        unique=True,
    )
    op.create_index(op.f("ix_zone_profile_visibility"), "zone_profile", ["visibility"])

    op.create_table(
        "zone_upload",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("upload_id", sa.String(length=96), nullable=False),
        sa.Column("spatial_zone_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("package_id", sa.String(length=96), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_size_bytes", sa.Integer(), nullable=False),
        sa.Column("catalog_sha256", sa.String(length=64), nullable=False),
        sa.Column("catalog_size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "RECEIVED",
                "VALIDATING",
                "VALIDATED",
                "REJECTED",
                name="zone_upload_state",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("validation_summary", sa.String(length=1_000), nullable=False),
        sa.Column("asset_catalog", sa.JSON(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _sha256_hex_check("archive_sha256"), name="ck_zone_upload_archive_sha256"
        ),
        sa.CheckConstraint(
            _sha256_hex_check("catalog_sha256"), name="ck_zone_upload_catalog_sha256"
        ),
        sa.CheckConstraint("archive_size_bytes > 0", name="ck_zone_upload_archive_size"),
        sa.CheckConstraint("catalog_size_bytes > 0", name="ck_zone_upload_catalog_size"),
        sa.CheckConstraint("revision >= 1", name="ck_zone_upload_revision_positive"),
        sa.CheckConstraint(
            "NOT is_active OR state = 'VALIDATED'",
            name="ck_zone_upload_active_requires_validated",
        ),
        sa.ForeignKeyConstraint(["spatial_zone_id"], ["spatial_zone.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_zone_id", "revision", name="uq_zone_upload_revision"),
    )
    op.create_index(op.f("ix_zone_upload_upload_id"), "zone_upload", ["upload_id"], unique=True)
    op.create_index(op.f("ix_zone_upload_spatial_zone_id"), "zone_upload", ["spatial_zone_id"])
    op.create_index(op.f("ix_zone_upload_state"), "zone_upload", ["state"])
    op.create_index(
        "uq_zone_upload_one_active",
        "zone_upload",
        ["spatial_zone_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "zone_information",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("information_id", sa.String(length=96), nullable=False),
        sa.Column("spatial_zone_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("easting_l93", sa.Float(), nullable=False),
        sa.Column("northing_l93", sa.Float(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "DRAFT",
                "PENDING_REVIEW",
                "PUBLISHED",
                "HIDDEN",
                "REJECTED",
                name="zone_information_state",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("review_note", sa.String(length=1_000), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["spatial_zone_id"], ["spatial_zone.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_zone_information_information_id"),
        "zone_information",
        ["information_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_zone_information_spatial_zone_id"),
        "zone_information",
        ["spatial_zone_id"],
    )
    op.create_index(op.f("ix_zone_information_state"), "zone_information", ["state"])

    op.create_table(
        "zone_contribution",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("contribution_id", sa.String(length=96), nullable=False),
        sa.Column("spatial_zone_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("easting_l93", sa.Float(), nullable=True),
        sa.Column("northing_l93", sa.Float(), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "REJECTED",
                name="zone_contribution_state",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("review_reason", sa.String(length=1_000), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(easting_l93 IS NULL AND northing_l93 IS NULL) "
            "OR (easting_l93 IS NOT NULL AND northing_l93 IS NOT NULL)",
            name="ck_zone_contribution_l93_pair",
        ),
        sa.ForeignKeyConstraint(["spatial_zone_id"], ["spatial_zone.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_zone_contribution_contribution_id"),
        "zone_contribution",
        ["contribution_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_zone_contribution_spatial_zone_id"),
        "zone_contribution",
        ["spatial_zone_id"],
    )
    op.create_index(op.f("ix_zone_contribution_state"), "zone_contribution", ["state"])


def downgrade() -> None:
    op.drop_index(op.f("ix_zone_contribution_state"), table_name="zone_contribution")
    op.drop_index(op.f("ix_zone_contribution_spatial_zone_id"), table_name="zone_contribution")
    op.drop_index(op.f("ix_zone_contribution_contribution_id"), table_name="zone_contribution")
    op.drop_table("zone_contribution")
    op.drop_index(op.f("ix_zone_information_state"), table_name="zone_information")
    op.drop_index(op.f("ix_zone_information_spatial_zone_id"), table_name="zone_information")
    op.drop_index(op.f("ix_zone_information_information_id"), table_name="zone_information")
    op.drop_table("zone_information")
    op.drop_index("uq_zone_upload_one_active", table_name="zone_upload")
    op.drop_index(op.f("ix_zone_upload_state"), table_name="zone_upload")
    op.drop_index(op.f("ix_zone_upload_spatial_zone_id"), table_name="zone_upload")
    op.drop_index(op.f("ix_zone_upload_upload_id"), table_name="zone_upload")
    op.drop_table("zone_upload")
    op.drop_index(op.f("ix_zone_profile_visibility"), table_name="zone_profile")
    op.drop_index(op.f("ix_zone_profile_spatial_zone_id"), table_name="zone_profile")
    op.drop_table("zone_profile")
