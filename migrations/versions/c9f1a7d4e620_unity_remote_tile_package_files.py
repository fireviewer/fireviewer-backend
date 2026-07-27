"""allow Unity remote-tile assets in spatial packages

Revision ID: c9f1a7d4e620
Revises: b8d4f6a9c210
Create Date: 2026-07-17 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "c9f1a7d4e620"
down_revision = "b8d4f6a9c210"
branch_labels = None
depends_on = None

_FK_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
_LEGACY_VALUES = ("COG", "PNG", "GLB")
_CURRENT_VALUES = ("COG", "JPEG", "PNG", "GLB", "FWTILE", "FWTERRAIN")
_LEGACY_MEDIA_CHECK = (
    "(kind = 'COG' AND media_type IN "
    "('image/tiff', 'image/geotiff', 'application/octet-stream')) "
    "OR (kind = 'PNG' AND media_type = 'image/png') "
    "OR (kind = 'GLB' AND media_type IN ('model/gltf-binary', 'application/octet-stream'))"
)
_CURRENT_MEDIA_CHECK = (
    "(kind = 'COG' AND media_type IN "
    "('image/tiff', 'image/geotiff', 'application/octet-stream')) "
    "OR (kind = 'JPEG' AND media_type = 'image/jpeg') "
    "OR (kind = 'PNG' AND media_type = 'image/png') "
    "OR (kind = 'GLB' AND media_type IN ('model/gltf-binary', 'application/octet-stream')) "
    "OR (kind = 'FWTILE' AND media_type = 'application/vnd.fireviewer.tile') "
    "OR (kind = 'FWTERRAIN' AND media_type = 'application/vnd.fireviewer.terrain')"
)


def _kind_type(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(
        *values,
        name="spatial_package_file_kind",
        native_enum=False,
        validate_strings=True,
    )


def _sqlite_trigger_definitions() -> list[tuple[str, str]]:
    if context.is_offline_mode():
        # The online path preserves existing trigger bodies.  Offline SQL has
        # no sqlite_master to inspect, so it renders only the table contract.
        return []

    rows = op.get_bind().execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND (tbl_name = 'spatial_package_file' OR lower(sql) LIKE '%spatial_package_file%') "
            "ORDER BY name"
        )
    )
    return [(name, statement) for name, statement in rows]


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _sqlite_spatial_package_file_v1() -> sa.Table:
    """Schema immediately before the Unity-kind table recreation."""

    metadata = sa.MetaData()
    table = sa.Table(
        "spatial_package_file",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spatial_package_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=3), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_hex_check("sha256"), name="ck_spatial_package_file_sha256"),
        sa.CheckConstraint("size_bytes > 0", name="ck_spatial_package_file_size"),
        sa.CheckConstraint("length(uri) > 0", name="ck_spatial_package_file_uri"),
        sa.CheckConstraint(_LEGACY_MEDIA_CHECK, name="ck_spatial_package_file_media_type"),
        sa.ForeignKeyConstraint(["spatial_package_id"], ["spatial_package.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_package_id", "kind", "uri", name="uq_spatial_package_file"),
    )
    sa.Index("ix_spatial_package_file_spatial_package_id", table.c.spatial_package_id)
    return table


def _sqlite_spatial_package_file_v2() -> sa.Table:
    """Current Unity-kind schema used only when rendering a downgrade."""

    table = _sqlite_spatial_package_file_v1()
    table.c.kind.type = sa.String(length=9)
    legacy_constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
        and constraint.name == "ck_spatial_package_file_media_type"
    )
    table.constraints.remove(legacy_constraint)
    table.append_constraint(
        sa.CheckConstraint(_CURRENT_MEDIA_CHECK, name="ck_spatial_package_file_media_type")
    )
    return table


def _drop_sqlite_triggers(definitions: list[tuple[str, str]]) -> None:
    for name, _statement in definitions:
        op.execute(f'DROP TRIGGER IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')


def _restore_sqlite_triggers(definitions: list[tuple[str, str]]) -> None:
    for _name, statement in definitions:
        op.execute(statement)


def _alter_kind_and_media_check(
    *,
    values: tuple[str, ...],
    existing_length: int,
    media_check: str,
) -> None:
    kind_type = _kind_type(values)
    if op.get_bind().dialect.name == "sqlite":
        trigger_definitions = _sqlite_trigger_definitions()
        _drop_sqlite_triggers(trigger_definitions)
        with op.batch_alter_table(
            "spatial_package_file",
            recreate="always",
            naming_convention=_FK_NAMING_CONVENTION,
            copy_from=_sqlite_spatial_package_file_v1()
            if existing_length == 3
            else _sqlite_spatial_package_file_v2(),
        ) as batch_op:
            batch_op.drop_constraint("ck_spatial_package_file_media_type", type_="check")
            batch_op.alter_column(
                "kind",
                existing_type=sa.String(length=existing_length),
                type_=kind_type,
                existing_nullable=False,
            )
            batch_op.create_check_constraint(
                "ck_spatial_package_file_media_type",
                media_check,
            )
        _restore_sqlite_triggers(trigger_definitions)
        return

    op.drop_constraint(
        "ck_spatial_package_file_media_type",
        "spatial_package_file",
        type_="check",
    )
    op.alter_column(
        "spatial_package_file",
        "kind",
        existing_type=sa.String(length=existing_length),
        type_=kind_type,
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_spatial_package_file_media_type",
        "spatial_package_file",
        media_check,
    )


def upgrade() -> None:
    _alter_kind_and_media_check(
        values=_CURRENT_VALUES,
        existing_length=3,
        media_check=_CURRENT_MEDIA_CHECK,
    )


def downgrade() -> None:
    unsupported = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT kind FROM spatial_package_file "
                "WHERE kind IN ('JPEG', 'FWTILE', 'FWTERRAIN') LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if unsupported is not None:
        raise RuntimeError(
            "Cannot downgrade while spatial_package_file contains the remote-tile kind "
            f"{unsupported!r}"
        )
    _alter_kind_and_media_check(
        values=_LEGACY_VALUES,
        existing_length=9,
        media_check=_LEGACY_MEDIA_CHECK,
    )
