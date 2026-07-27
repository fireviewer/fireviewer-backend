"""Allow a manifest revision to reference a tiled package without a monolithic asset.

Revision ID: d2a6e8f1b430
Revises: c9f1a7d4e620
"""
# ruff: noqa: S608 -- migration SQL is composed exclusively from fixed expressions.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "d2a6e8f1b430"
down_revision: str | None = "c9f1a7d4e620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sqlite_triggers() -> list[tuple[str, str]]:
    if context.is_offline_mode():
        # Trigger bodies are recovered only by the real online migration from
        # sqlite_master.  SQL export uses the static table definition below.
        return []

    return [
        (name, statement)
        for name, statement in op.get_bind().execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND sql IS NOT NULL AND lower(sql) LIKE '%manifest_revision%'"
            )
        )
    ]


def _sqlite_manifest_revision_v1() -> sa.Table:
    """Manifest schema directly before tiled packages became asset-optional."""

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
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["model_asset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["spatial_zone_revision_id"],
            ["spatial_zone_revision.id"],
            name="fk_manifest_revision_spatial_zone_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_package_id"],
            ["spatial_package.id"],
            name="fk_manifest_revision_spatial_package_id_spatial_package",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "revision", name="uq_manifest_revision"),
    )
    sa.Index("ix_manifest_revision_incident_id", table.c.incident_id)
    sa.Index("ix_manifest_revision_spatial_package_id", table.c.spatial_package_id)
    sa.Index("ix_manifest_revision_spatial_zone_revision_id", table.c.spatial_zone_revision_id)
    sa.Index(
        "uq_manifest_one_current",
        table.c.incident_id,
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )
    return table


def _replace_constraint(expression: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect != "sqlite":
        op.drop_constraint(
            "ck_manifest_zone_requires_asset",
            "manifest_revision",
            type_="check",
        )
        op.create_check_constraint(
            "ck_manifest_zone_requires_asset",
            "manifest_revision",
            expression,
        )
        return

    triggers = _sqlite_triggers()
    for name, _statement in triggers:
        op.execute(f'DROP TRIGGER IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')
    with op.batch_alter_table(
        "manifest_revision",
        recreate="always",
        copy_from=_sqlite_manifest_revision_v1(),
    ) as batch:
        batch.drop_constraint("ck_manifest_zone_requires_asset", type_="check")
        batch.create_check_constraint("ck_manifest_zone_requires_asset", expression)
    for _name, statement in triggers:
        op.execute(statement)


def _replace_package_validation(*, allow_package_without_asset: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_package_insert")
        op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_package_update")
        asset_clause = (
            "AND (NEW.asset_id IS NULL OR EXISTS ("
            if allow_package_without_asset
            else "AND EXISTS ("
        )
        asset_end = ")" if allow_package_without_asset else ""
        package_validation = (
            "WHEN NEW.spatial_package_id IS NOT NULL "
            "BEGIN SELECT CASE WHEN NOT EXISTS ("
            "SELECT 1 FROM spatial_package AS package "
            "WHERE package.id = NEW.spatial_package_id "
            "AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id "
            "AND package.state IN ('PREVIEWABLE', 'PUBLISHED') "
            + asset_clause
            + "SELECT 1 FROM model_asset AS asset "
            "JOIN spatial_package_file AS file ON file.id = asset.spatial_package_file_id "
            "WHERE asset.id = NEW.asset_id AND file.spatial_package_id = package.id"
            + asset_end
            + ")) THEN RAISE(ABORT, "
            "'manifest package must match its asset and spatial revision') END; END"
        )
        op.execute(
            "CREATE TRIGGER manifest_revision_validate_package_insert "
            "BEFORE INSERT ON manifest_revision " + package_validation
        )
        op.execute(
            "CREATE TRIGGER manifest_revision_validate_package_update "
            "BEFORE UPDATE OF spatial_package_id, asset_id, spatial_zone_revision_id "
            "ON manifest_revision " + package_validation
        )
        return

    if dialect == "postgresql":
        optional_asset = "NEW.asset_id IS NULL OR " if allow_package_without_asset else ""
        validation_sql = f"""
            CREATE OR REPLACE FUNCTION fire_viewer_manifest_package_valid() RETURNS trigger AS $$
            BEGIN
                IF NEW.spatial_package_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM spatial_package AS package
                    WHERE package.id = NEW.spatial_package_id
                      AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id
                      AND package.state IN ('PREVIEWABLE', 'PUBLISHED')
                      AND ({optional_asset}EXISTS (
                          SELECT 1 FROM model_asset AS asset
                          JOIN spatial_package_file AS file
                            ON file.id = asset.spatial_package_file_id
                          WHERE asset.id = NEW.asset_id
                            AND file.spatial_package_id = package.id
                      ))
                ) THEN
                    RAISE EXCEPTION 'manifest package must match its asset and spatial revision';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        op.execute(validation_sql)


def _replace_spatial_link_validation(*, allow_package_without_asset: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_spatial_link_insert")
        op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_spatial_link_update")
        if allow_package_without_asset:
            validation = (
                "(NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NULL) "
                "OR (NEW.asset_id IS NULL AND NEW.spatial_zone_revision_id IS NOT NULL "
                "AND NEW.spatial_package_id IS NULL) "
                "OR (NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM model_asset AS asset "
                "WHERE asset.id = NEW.asset_id "
                "AND asset.spatial_zone_revision_id = NEW.spatial_zone_revision_id))"
            )
        else:
            validation = (
                "(NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NULL) "
                "OR (NEW.asset_id IS NULL AND NEW.spatial_zone_revision_id IS NOT NULL) "
                "OR (NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM model_asset AS asset "
                "WHERE asset.id = NEW.asset_id "
                "AND asset.spatial_zone_revision_id = NEW.spatial_zone_revision_id))"
            )
        op.execute(
            "CREATE TRIGGER manifest_revision_validate_spatial_link_insert "
            "BEFORE INSERT ON manifest_revision WHEN " + validation + " BEGIN SELECT RAISE(ABORT, "
            "'manifest asset and spatial zone revision must match'); END"
        )
        op.execute(
            "CREATE TRIGGER manifest_revision_validate_spatial_link_update "
            "BEFORE UPDATE OF asset_id, spatial_zone_revision_id ON manifest_revision "
            "BEGIN SELECT CASE WHEN NEW.asset_id IS NOT OLD.asset_id "
            "OR NEW.spatial_zone_revision_id IS NOT OLD.spatial_zone_revision_id "
            "THEN RAISE(ABORT, "
            "'manifest asset and spatial zone revision are immutable') END; "
            "SELECT CASE WHEN " + validation + " THEN RAISE(ABORT, "
            "'manifest asset and spatial zone revision must match') END; END"
        )
        return

    if dialect == "postgresql":
        missing_pair = (
            "(NEW.asset_id IS NULL AND NEW.spatial_zone_revision_id IS NOT NULL "
            "AND NEW.spatial_package_id IS NULL) OR "
            if allow_package_without_asset
            else "(NEW.asset_id IS NULL AND NEW.spatial_zone_revision_id IS NOT NULL) OR "
        )
        validation_sql = f"""
            CREATE OR REPLACE FUNCTION fire_viewer_manifest_spatial_link_valid()
            RETURNS trigger AS $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM episode AS episode
                WHERE episode.id = NEW.episode_id
                  AND episode.incident_id = NEW.incident_id
              ) THEN
                RAISE EXCEPTION 'manifest episode must belong to its incident';
              END IF;
              IF TG_OP = 'UPDATE' AND (
                NEW.asset_id IS DISTINCT FROM OLD.asset_id
                OR NEW.spatial_zone_revision_id IS DISTINCT FROM OLD.spatial_zone_revision_id
              ) THEN
                RAISE EXCEPTION 'manifest asset and spatial zone revision are immutable';
              END IF;
              IF (NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NULL)
                 OR {missing_pair}FALSE THEN
                RAISE EXCEPTION 'manifest asset and spatial zone revision must be supplied together';
              END IF;
              IF NEW.asset_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM model_asset AS asset
                WHERE asset.id = NEW.asset_id
                  AND asset.spatial_zone_revision_id = NEW.spatial_zone_revision_id
              ) THEN
                RAISE EXCEPTION 'manifest asset and spatial zone revision must match';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        op.execute(validation_sql)


def upgrade() -> None:
    _replace_constraint(
        "spatial_zone_revision_id IS NULL OR asset_id IS NOT NULL OR spatial_package_id IS NOT NULL"
    )
    _replace_spatial_link_validation(allow_package_without_asset=True)
    _replace_package_validation(allow_package_without_asset=True)


def downgrade() -> None:
    _replace_package_validation(allow_package_without_asset=False)
    _replace_spatial_link_validation(allow_package_without_asset=False)
    _replace_constraint("spatial_zone_revision_id IS NULL OR asset_id IS NOT NULL")
