"""add admin spatial package registry

Revision ID: f2a8d7c9b410
Revises: e7a4c9d8f2b1
Create Date: 2026-07-14 17:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a8d7c9b410"
down_revision: str | None = "e7a4c9d8f2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _create_sqlite_triggers() -> None:
    op.execute(
        "CREATE TRIGGER spatial_package_identity_immutable "
        "BEFORE UPDATE OF package_id, manifest_uri, manifest_sha256, manifest_size_bytes, "
        "storage_uri, provenance ON spatial_package "
        "BEGIN SELECT RAISE(ABORT, 'spatial package identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER spatial_package_file_no_update "
        "BEFORE UPDATE ON spatial_package_file "
        "BEGIN SELECT RAISE(ABORT, 'spatial package files are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER spatial_package_file_no_delete "
        "BEFORE DELETE ON spatial_package_file "
        "BEGIN SELECT RAISE(ABORT, 'spatial package files are immutable'); END"
    )


def _drop_sqlite_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS spatial_package_file_no_delete")
    op.execute("DROP TRIGGER IF EXISTS spatial_package_file_no_update")
    op.execute("DROP TRIGGER IF EXISTS spatial_package_identity_immutable")


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "spatial_package",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.String(length=96), nullable=False),
        sa.Column("manifest_uri", sa.String(length=2048), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_uri", sa.String(length=2048), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "DRAFT",
                "VERIFIED",
                "PREVIEWABLE",
                "PUBLISHED",
                "WITHDRAWN",
                "REVOKED",
                "ARCHIVED",
                name="spatial_package_state",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("verification_report", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("spatial_zone_revision_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _sha256_hex_check("manifest_sha256"), name="ck_spatial_package_manifest_sha256"
        ),
        sa.CheckConstraint("manifest_size_bytes > 0", name="ck_spatial_package_manifest_size"),
        sa.CheckConstraint("length(manifest_uri) > 0", name="ck_spatial_package_manifest_uri"),
        sa.CheckConstraint("length(storage_uri) > 0", name="ck_spatial_package_storage_uri"),
        sa.CheckConstraint(
            "(state IN ('VERIFIED', 'PREVIEWABLE', 'PUBLISHED', 'WITHDRAWN', 'REVOKED', 'ARCHIVED') "
            "AND verified_at IS NOT NULL) OR state = 'DRAFT'",
            name="ck_spatial_package_verified_states_timestamp",
        ),
        sa.CheckConstraint(
            "spatial_zone_revision_id IS NULL OR state IN "
            "('VERIFIED', 'PREVIEWABLE', 'PUBLISHED', 'WITHDRAWN', 'REVOKED', 'ARCHIVED')",
            name="ck_spatial_package_revision_requires_validated_state",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_zone_revision_id"], ["spatial_zone_revision.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id"),
    )
    op.create_index(
        op.f("ix_spatial_package_package_id"), "spatial_package", ["package_id"], unique=True
    )
    op.create_index(
        op.f("ix_spatial_package_spatial_zone_revision_id"),
        "spatial_package",
        ["spatial_zone_revision_id"],
        unique=False,
    )
    op.create_table(
        "spatial_package_file",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spatial_package_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "COG",
                "PNG",
                "GLB",
                name="spatial_package_file_kind",
                native_enum=False,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("uri", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_hex_check("sha256"), name="ck_spatial_package_file_sha256"),
        sa.CheckConstraint("size_bytes > 0", name="ck_spatial_package_file_size"),
        sa.CheckConstraint("length(uri) > 0", name="ck_spatial_package_file_uri"),
        sa.CheckConstraint(
            "(kind = 'COG' AND media_type IN ('image/tiff', 'image/geotiff', 'application/octet-stream')) "
            "OR (kind = 'PNG' AND media_type = 'image/png') "
            "OR (kind = 'GLB' AND media_type IN ('model/gltf-binary', 'application/octet-stream'))",
            name="ck_spatial_package_file_media_type",
        ),
        sa.ForeignKeyConstraint(["spatial_package_id"], ["spatial_package.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_package_id", "kind", "uri", name="uq_spatial_package_file"),
    )
    op.create_index(
        op.f("ix_spatial_package_file_spatial_package_id"),
        "spatial_package_file",
        ["spatial_package_id"],
        unique=False,
    )
    if bind.dialect.name == "sqlite":
        _create_sqlite_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _drop_sqlite_triggers()
    op.drop_index(
        op.f("ix_spatial_package_file_spatial_package_id"), table_name="spatial_package_file"
    )
    op.drop_table("spatial_package_file")
    op.drop_index(op.f("ix_spatial_package_spatial_zone_revision_id"), table_name="spatial_package")
    op.drop_index(op.f("ix_spatial_package_package_id"), table_name="spatial_package")
    op.drop_table("spatial_package")
