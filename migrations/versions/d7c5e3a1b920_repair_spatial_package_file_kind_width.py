"""repair the spatial package file kind width on deployed databases

Revision ID: d7c5e3a1b920
Revises: d2a6e8f1b430
Create Date: 2026-07-17 20:00:00.000000

The Unity file-kind migration intended to widen this column, but the production
PostgreSQL schema can still contain the historical ``VARCHAR(3)`` definition.
This additive repair is deliberately explicit so already-stamped databases are
corrected without rewriting a migration that has been deployed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "d7c5e3a1b920"
down_revision: str | None = "d2a6e8f1b430"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_LENGTH = 9
_FK_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
_CURRENT_MEDIA_CHECK = (
    "(kind = 'COG' AND media_type IN "
    "('image/tiff', 'image/geotiff', 'application/octet-stream')) "
    "OR (kind = 'JPEG' AND media_type = 'image/jpeg') "
    "OR (kind = 'PNG' AND media_type = 'image/png') "
    "OR (kind = 'GLB' AND media_type IN ('model/gltf-binary', 'application/octet-stream')) "
    "OR (kind = 'FWTILE' AND media_type = 'application/vnd.fireviewer.tile') "
    "OR (kind = 'FWTERRAIN' AND media_type = 'application/vnd.fireviewer.terrain')"
)


def _sqlite_trigger_definitions() -> list[tuple[str, str]]:
    if context.is_offline_mode():
        # The online repair retains every SQLite trigger.  Offline generation
        # cannot query sqlite_master and renders the static table only.
        return []

    rows = op.get_bind().execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND (tbl_name = 'spatial_package_file' "
            "OR lower(sql) LIKE '%spatial_package_file%') ORDER BY name"
        )
    )
    return [(name, statement) for name, statement in rows]


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _sqlite_spatial_package_file_v1() -> sa.Table:
    """Unity remote-tile schema used as the offline batch source."""

    metadata = sa.MetaData()
    table = sa.Table(
        "spatial_package_file",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spatial_package_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=9), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_hex_check("sha256"), name="ck_spatial_package_file_sha256"),
        sa.CheckConstraint("size_bytes > 0", name="ck_spatial_package_file_size"),
        sa.CheckConstraint("length(uri) > 0", name="ck_spatial_package_file_uri"),
        sa.CheckConstraint(_CURRENT_MEDIA_CHECK, name="ck_spatial_package_file_media_type"),
        sa.ForeignKeyConstraint(["spatial_package_id"], ["spatial_package.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_package_id", "kind", "uri", name="uq_spatial_package_file"),
    )
    sa.Index("ix_spatial_package_file_spatial_package_id", table.c.spatial_package_id)
    return table


def _widen_sqlite_column() -> None:
    triggers = _sqlite_trigger_definitions()
    for name, _statement in triggers:
        quoted_name = name.replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{quoted_name}"')
    with op.batch_alter_table(
        "spatial_package_file",
        recreate="always",
        naming_convention=_FK_NAMING_CONVENTION,
        copy_from=_sqlite_spatial_package_file_v1(),
    ) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(length=3),
            type_=sa.String(length=_CURRENT_LENGTH),
            existing_nullable=False,
        )
    for _name, statement in triggers:
        op.execute(statement)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _widen_sqlite_column()
        return
    if dialect == "postgresql":
        # Keep this SQL explicit: the historical Enum-to-Enum alteration was
        # stamped in production without changing the VARCHAR width.
        op.execute(
            "ALTER TABLE spatial_package_file "
            "ALTER COLUMN kind TYPE VARCHAR(9)"
        )
        return
    op.alter_column(
        "spatial_package_file",
        "kind",
        existing_type=sa.String(length=3),
        type_=sa.String(length=_CURRENT_LENGTH),
        existing_nullable=False,
    )


def downgrade() -> None:
    # d2a6e8f1b430 already models the Unity kinds and therefore also requires
    # VARCHAR(9). Re-introducing VARCHAR(3) would corrupt that revision's
    # contract, so this repair intentionally has no destructive downgrade.
    return
