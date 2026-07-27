"""normalize asset LOD and spatial package foreign-key contracts

Revision ID: a4e9c2f7d610
Revises: f3b8c1d7a920
Create Date: 2026-07-16 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "a4e9c2f7d610"
down_revision = "f3b8c1d7a920"
branch_labels = None
depends_on = None

_FK_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
_CURRENT_LODS = ("MOBILE", "DESKTOP", "CLOSE", "LOCAL", "EXTENDED")
_LEGACY_LODS = ("MOBILE", "DESKTOP")


def _asset_lod_type(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="asset_lod", native_enum=False)


def _sqlite_trigger_definitions(*tables: str) -> list[tuple[str, str]]:
    # Offline generation has no sqlite_master to inspect.  The online migration
    # still snapshots and restores every trigger around the table recreation.
    # The deployment gate deliberately runs that real migration on a restored
    # database; this branch only makes SQL rendering deterministic.
    if context.is_offline_mode():
        return []

    bind = op.get_bind()
    definitions: list[tuple[str, str]] = []
    target_tables = {table.casefold() for table in tables}
    rows = bind.execute(
        sa.text(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL ORDER BY name"
        )
    )
    for name, owning_table, statement in rows:
        normalized_statement = statement.casefold()
        if owning_table.casefold() in target_tables or any(
            table in normalized_statement for table in target_tables
        ):
            definitions.append((name, statement))
    return definitions


def _sqlite_model_asset_v1() -> sa.Table:
    """Schema immediately before this migration's SQLite table copy."""

    metadata = sa.MetaData()
    table = sa.Table(
        "model_asset",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lod", sa.String(length=7), nullable=False),
        sa.Column("state", sa.String(length=17), nullable=False),
        sa.Column("glb_url", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("terrain_source_year", sa.Integer()),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("legacy_incident_id", sa.Integer()),
        sa.Column("legacy_episode_id", sa.Integer()),
        sa.Column("legacy_origin_lon", sa.Float()),
        sa.Column("legacy_origin_lat", sa.Float()),
        sa.Column("legacy_origin_altitude_m", sa.Float()),
        sa.Column("legacy_local_frame", sa.String(length=16)),
        sa.Column("legacy_meters_per_unit", sa.Float()),
        sa.Column("legacy_vertical_datum", sa.String(length=128)),
        sa.Column("spatial_zone_revision_id", sa.Integer()),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column("purge_requested_at", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("retention_hold_reason", sa.String(length=500)),
        sa.Column("spatial_package_file_id", sa.Integer()),
        sa.CheckConstraint("size_bytes > 0", name="ck_asset_size"),
        sa.CheckConstraint("version >= 1", name="ck_asset_version"),
        sa.CheckConstraint(
            "spatial_zone_revision_id IS NOT NULL OR state IN ('QUARANTINED', 'DELETED_TOMBSTONE')",
            name="ck_asset_zone_revision_required",
        ),
        sa.CheckConstraint(
            "(legacy_incident_id IS NULL AND legacy_episode_id IS NULL AND legacy_origin_lon IS NULL "
            "AND legacy_origin_lat IS NULL AND legacy_origin_altitude_m IS NULL "
            "AND legacy_local_frame IS NULL AND legacy_meters_per_unit IS NULL "
            "AND legacy_vertical_datum IS NULL) OR (legacy_incident_id IS NOT NULL "
            "AND legacy_episode_id IS NOT NULL AND legacy_origin_lon IS NOT NULL "
            "AND legacy_origin_lat IS NOT NULL AND legacy_origin_altitude_m IS NOT NULL "
            "AND legacy_local_frame IS NOT NULL AND legacy_meters_per_unit IS NOT NULL "
            "AND legacy_vertical_datum IS NOT NULL)",
            name="ck_asset_legacy_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_incident_id"], ["incident_series.id"],
            name="fk_model_asset_legacy_incident", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_episode_id"], ["episode.id"],
            name="fk_model_asset_legacy_episode", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_zone_revision_id"], ["spatial_zone_revision.id"],
            name="fk_model_asset_spatial_zone_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_package_file_id"], ["spatial_package_file.id"],
            name="fk_model_asset_spatial_package_file_id_spatial_package_file", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_zone_revision_id", "version", "lod", name="uq_asset_version_lod"),
    )
    sa.Index("ix_model_asset_asset_id", table.c.asset_id, unique=True)
    sa.Index("ix_model_asset_legacy_episode_id", table.c.legacy_episode_id)
    sa.Index("ix_model_asset_legacy_incident_id", table.c.legacy_incident_id)
    sa.Index("ix_model_asset_purge_after", table.c.purge_after)
    sa.Index("ix_model_asset_spatial_package_file_id", table.c.spatial_package_file_id, unique=True)
    sa.Index("ix_model_asset_spatial_zone_revision_id", table.c.spatial_zone_revision_id)
    return table


def _sqlite_manifest_revision_v1() -> sa.Table:
    """Schema immediately before this migration's SQLite table copy."""

    metadata = sa.MetaData()
    table = sa.Table(
        "manifest_revision",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spatial_zone_revision_id", sa.Integer()),
        sa.Column("spatial_package_id", sa.Integer()),
        sa.CheckConstraint("revision >= 1", name="ck_manifest_revision"),
        sa.CheckConstraint(
            "spatial_zone_revision_id IS NULL OR asset_id IS NOT NULL",
            name="ck_manifest_zone_requires_asset",
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["model_asset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["spatial_zone_revision_id"], ["spatial_zone_revision.id"],
            name="fk_manifest_revision_spatial_zone_revision", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_package_id"], ["spatial_package.id"],
            name="fk_manifest_revision_spatial_package_id_spatial_package", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "revision", name="uq_manifest_revision"),
    )
    sa.Index("ix_manifest_revision_incident_id", table.c.incident_id)
    sa.Index("ix_manifest_revision_spatial_package_id", table.c.spatial_package_id)
    sa.Index("ix_manifest_revision_spatial_zone_revision_id", table.c.spatial_zone_revision_id)
    sa.Index("uq_manifest_one_current", table.c.incident_id, unique=True, sqlite_where=sa.text("is_current = 1"))
    return table


def _drop_sqlite_triggers(definitions: list[tuple[str, str]]) -> None:
    for name, _statement in definitions:
        quoted_name = name.replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{quoted_name}"')


def _restore_sqlite_triggers(definitions: list[tuple[str, str]]) -> None:
    for _name, statement in definitions:
        op.execute(statement)


def _normalize_sqlite_contract(*, lod_type: sa.Enum, existing_lod_length: int) -> None:
    trigger_definitions = _sqlite_trigger_definitions("model_asset", "manifest_revision")
    _drop_sqlite_triggers(trigger_definitions)

    with op.batch_alter_table(
        "model_asset",
        recreate="always",
        naming_convention=_FK_NAMING_CONVENTION,
        copy_from=_sqlite_model_asset_v1(),
    ) as batch_op:
        batch_op.alter_column(
            "lod",
            existing_type=sa.String(length=existing_lod_length),
            type_=lod_type,
            existing_nullable=False,
        )
        batch_op.drop_constraint(
            "fk_model_asset_spatial_package_file_id_spatial_package_file",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_model_asset_spatial_package_file_id_spatial_package_file",
            "spatial_package_file",
            ["spatial_package_file_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table(
        "manifest_revision",
        recreate="always",
        naming_convention=_FK_NAMING_CONVENTION,
        copy_from=_sqlite_manifest_revision_v1(),
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_manifest_revision_spatial_package_id_spatial_package",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_manifest_revision_spatial_package_id_spatial_package",
            "spatial_package",
            ["spatial_package_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    _restore_sqlite_triggers(trigger_definitions)


def _ensure_legacy_lod_values() -> None:
    unsupported = (
        op.get_bind()
        .execute(
            sa.text("SELECT lod FROM model_asset WHERE lod NOT IN ('MOBILE', 'DESKTOP') LIMIT 1")
        )
        .scalar_one_or_none()
    )
    if unsupported is not None:
        raise RuntimeError(
            "Cannot downgrade asset LOD schema while model_asset contains "
            f"the non-legacy value {unsupported!r}"
        )


def upgrade() -> None:
    current_type = _asset_lod_type(_CURRENT_LODS)
    if op.get_bind().dialect.name == "sqlite":
        _normalize_sqlite_contract(lod_type=current_type, existing_lod_length=7)
        return

    op.alter_column(
        "model_asset",
        "lod",
        existing_type=sa.String(length=7),
        type_=current_type,
        existing_nullable=False,
    )


def downgrade() -> None:
    _ensure_legacy_lod_values()
    legacy_type = _asset_lod_type(_LEGACY_LODS)
    if op.get_bind().dialect.name == "sqlite":
        _normalize_sqlite_contract(lod_type=legacy_type, existing_lod_length=8)
        return

    op.alter_column(
        "model_asset",
        "lod",
        existing_type=sa.String(length=8),
        type_=legacy_type,
        existing_nullable=False,
    )
