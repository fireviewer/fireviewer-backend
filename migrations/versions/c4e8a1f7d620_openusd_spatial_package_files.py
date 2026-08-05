"""allow OpenUSD map and perimeter package files

Revision ID: c4e8a1f7d620
Revises: b7f2e4a9c810
Create Date: 2026-08-05 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "c4e8a1f7d620"
down_revision: str | None = "b7f2e4a9c810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("COG", "JPEG", "PNG", "GLB", "FWTILE", "FWTERRAIN")
_NEW_VALUES = (*_OLD_VALUES, "JSON", "OPENUSD", "AUXILIARY")
_OLD_CHECK = (
    "(kind = 'COG' AND media_type IN "
    "('image/tiff', 'image/geotiff', 'application/octet-stream')) "
    "OR (kind = 'JPEG' AND media_type = 'image/jpeg') "
    "OR (kind = 'PNG' AND media_type = 'image/png') "
    "OR (kind = 'GLB' AND media_type IN ('model/gltf-binary', 'application/octet-stream')) "
    "OR (kind = 'FWTILE' AND media_type = 'application/vnd.fireviewer.tile') "
    "OR (kind = 'FWTERRAIN' AND media_type = 'application/vnd.fireviewer.terrain')"
)
_NEW_CHECK = (
    _OLD_CHECK
    + " OR (kind = 'JSON' AND media_type = 'application/json')"
    + " OR (kind = 'OPENUSD' AND media_type IN "
    "('model/vnd.usd', 'model/vnd.usdz+zip', 'application/octet-stream'))"
    + " OR (kind = 'AUXILIARY' AND media_type IN "
    "('application/octet-stream', 'image/vnd.radiance', 'text/plain'))"
)
_FK_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}


def _kind_type(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(
        *values,
        name="spatial_package_file_kind",
        native_enum=False,
        validate_strings=True,
    )


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _table(*, values: tuple[str, ...], media_check: str) -> sa.Table:
    metadata = sa.MetaData()
    table = sa.Table(
        "spatial_package_file",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spatial_package_id", sa.Integer(), nullable=False),
        sa.Column("kind", _kind_type(values), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_hex_check("sha256"), name="ck_spatial_package_file_sha256"),
        sa.CheckConstraint("size_bytes > 0", name="ck_spatial_package_file_size"),
        sa.CheckConstraint("length(uri) > 0", name="ck_spatial_package_file_uri"),
        sa.CheckConstraint(media_check, name="ck_spatial_package_file_media_type"),
        sa.ForeignKeyConstraint(
            ["spatial_package_id"], ["spatial_package.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "spatial_package_id", "kind", "uri", name="uq_spatial_package_file"
        ),
    )
    sa.Index("ix_spatial_package_file_spatial_package_id", table.c.spatial_package_id)
    return table


def _sqlite_triggers() -> list[tuple[str, str]]:
    if context.is_offline_mode():
        return []
    rows = op.get_bind().execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND sql IS NOT NULL AND (tbl_name = 'spatial_package_file' "
            "OR lower(sql) LIKE '%spatial_package_file%') ORDER BY name"
        )
    )
    return [(name, statement) for name, statement in rows]


def _alter(*, values: tuple[str, ...], check: str, source: sa.Table) -> None:
    if op.get_bind().dialect.name == "sqlite":
        triggers = _sqlite_triggers()
        for name, _statement in triggers:
            quoted = name.replace('"', '""')
            op.execute(f'DROP TRIGGER IF EXISTS "{quoted}"')
        with op.batch_alter_table(
            "spatial_package_file",
            recreate="always",
            naming_convention=_FK_NAMING_CONVENTION,
            copy_from=source,
        ) as batch_op:
            batch_op.drop_constraint("ck_spatial_package_file_media_type", type_="check")
            batch_op.alter_column(
                "kind",
                existing_type=sa.String(length=9),
                type_=_kind_type(values),
                existing_nullable=False,
            )
            batch_op.create_check_constraint("ck_spatial_package_file_media_type", check)
        for _name, statement in triggers:
            op.execute(statement)
        return
    op.drop_constraint(
        "ck_spatial_package_file_media_type", "spatial_package_file", type_="check"
    )
    op.alter_column(
        "spatial_package_file",
        "kind",
        existing_type=sa.String(length=9),
        type_=_kind_type(values),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_spatial_package_file_media_type", "spatial_package_file", check
    )


def upgrade() -> None:
    _alter(
        values=_NEW_VALUES,
        check=_NEW_CHECK,
        source=_table(values=_OLD_VALUES, media_check=_OLD_CHECK),
    )
    op.create_table(
        "incident_perimeter_package",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("spatial_package_id", sa.Integer(), nullable=False),
        sa.Column("base_spatial_package_id", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "spatial_package_id <> base_spatial_package_id",
            name="ck_incident_perimeter_package_distinct_base",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incident_series.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["spatial_package_id"], ["spatial_package.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["base_spatial_package_id"], ["spatial_package.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_package_id"),
        sa.UniqueConstraint(
            "incident_id",
            "spatial_package_id",
            name="uq_incident_perimeter_package_attachment",
        ),
    )
    op.create_index(
        op.f("ix_incident_perimeter_package_incident_id"),
        "incident_perimeter_package",
        ["incident_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_incident_perimeter_package_base_spatial_package_id"),
        "incident_perimeter_package",
        ["base_spatial_package_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_incident_perimeter_package_base_spatial_package_id"),
        table_name="incident_perimeter_package",
    )
    op.drop_index(
        op.f("ix_incident_perimeter_package_incident_id"),
        table_name="incident_perimeter_package",
    )
    op.drop_table("incident_perimeter_package")
    unsupported = op.get_bind().execute(
        sa.text(
            "SELECT kind FROM spatial_package_file "
            "WHERE kind IN ('JSON', 'OPENUSD', 'AUXILIARY') LIMIT 1"
        )
    ).scalar_one_or_none()
    if unsupported is not None:
        raise RuntimeError(
            "Cannot downgrade while spatial_package_file contains OpenUSD package files"
        )
    _alter(
        values=_OLD_VALUES,
        check=_OLD_CHECK,
        source=_table(values=_NEW_VALUES, media_check=_NEW_CHECK),
    )
