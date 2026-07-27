"""bind incident manifests and model assets to uploaded spatial packages

Revision ID: e6f3a1b8c420
Revises: c2e8f4a6b910
Create Date: 2026-07-15 19:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f3a1b8c420"
down_revision = "c2e8f4a6b910"
branch_labels = None
depends_on = None

_FK_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}


def _create_sqlite_contract() -> None:
    op.execute(
        "ALTER TABLE model_asset ADD COLUMN spatial_package_file_id INTEGER "
        "REFERENCES spatial_package_file(id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_model_asset_spatial_package_file_id "
        "ON model_asset(spatial_package_file_id)"
    )
    op.execute(
        "ALTER TABLE manifest_revision ADD COLUMN spatial_package_id INTEGER "
        "REFERENCES spatial_package(id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX ix_manifest_revision_spatial_package_id "
        "ON manifest_revision(spatial_package_id)"
    )
    op.execute(
        "CREATE TRIGGER model_asset_validate_package_file_insert "
        "BEFORE INSERT ON model_asset WHEN NEW.spatial_package_file_id IS NOT NULL "
        "BEGIN SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM spatial_package_file AS file "
        "JOIN spatial_package AS package ON package.id = file.spatial_package_id "
        "WHERE file.id = NEW.spatial_package_file_id "
        "AND file.kind = 'GLB' "
        "AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id) "
        "THEN RAISE(ABORT, 'model asset package file must match its spatial revision') END; END"
    )
    op.execute(
        "CREATE TRIGGER model_asset_validate_package_file_update "
        "BEFORE UPDATE OF spatial_package_file_id, spatial_zone_revision_id ON model_asset "
        "WHEN NEW.spatial_package_file_id IS NOT NULL "
        "BEGIN SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM spatial_package_file AS file "
        "JOIN spatial_package AS package ON package.id = file.spatial_package_id "
        "WHERE file.id = NEW.spatial_package_file_id "
        "AND file.kind = 'GLB' "
        "AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id) "
        "THEN RAISE(ABORT, 'model asset package file must match its spatial revision') END; END"
    )
    package_validation = (
        "WHEN NEW.spatial_package_id IS NOT NULL "
        "BEGIN SELECT CASE WHEN NOT EXISTS ("
        "SELECT 1 FROM spatial_package AS package "
        "JOIN model_asset AS asset ON asset.id = NEW.asset_id "
        "JOIN spatial_package_file AS file ON file.id = asset.spatial_package_file_id "
        "WHERE package.id = NEW.spatial_package_id "
        "AND package.id = file.spatial_package_id "
        "AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id "
        "AND package.state IN ('PREVIEWABLE', 'PUBLISHED')) "
        "THEN RAISE(ABORT, 'manifest package must match its asset and spatial revision') END; END"
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_package_insert "
        "BEFORE INSERT ON manifest_revision " + package_validation
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_package_update "
        "BEFORE UPDATE OF spatial_package_id ON manifest_revision " + package_validation
    )


def _create_postgresql_contract() -> None:
    op.add_column(
        "model_asset",
        sa.Column(
            "spatial_package_file_id",
            sa.Integer(),
            sa.ForeignKey("spatial_package_file.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_model_asset_spatial_package_file_id",
        "model_asset",
        ["spatial_package_file_id"],
        unique=True,
    )
    op.add_column(
        "manifest_revision",
        sa.Column(
            "spatial_package_id",
            sa.Integer(),
            sa.ForeignKey("spatial_package.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_manifest_revision_spatial_package_id",
        "manifest_revision",
        ["spatial_package_id"],
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_model_asset_package_valid() RETURNS trigger AS $$
        BEGIN
            IF NEW.spatial_package_file_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM spatial_package_file AS file
                JOIN spatial_package AS package ON package.id = file.spatial_package_id
                WHERE file.id = NEW.spatial_package_file_id
                  AND file.kind = 'GLB'
                  AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id
            ) THEN
                RAISE EXCEPTION 'model asset package file must match its spatial revision';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER model_asset_validate_package_file "
        "BEFORE INSERT OR UPDATE OF spatial_package_file_id, spatial_zone_revision_id "
        "ON model_asset FOR EACH ROW EXECUTE FUNCTION fire_viewer_model_asset_package_valid()"
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_manifest_package_valid() RETURNS trigger AS $$
        BEGIN
            IF NEW.spatial_package_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM spatial_package AS package
                JOIN model_asset AS asset ON asset.id = NEW.asset_id
                JOIN spatial_package_file AS file ON file.id = asset.spatial_package_file_id
                WHERE package.id = NEW.spatial_package_id
                  AND package.id = file.spatial_package_id
                  AND package.spatial_zone_revision_id = NEW.spatial_zone_revision_id
                  AND package.state IN ('PREVIEWABLE', 'PUBLISHED')
            ) THEN
                RAISE EXCEPTION 'manifest package must match its asset and spatial revision';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_package "
        "BEFORE INSERT OR UPDATE OF spatial_package_id ON manifest_revision "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_manifest_package_valid()"
    )


def _sqlite_trigger_definitions(*tables: str) -> list[tuple[str, str]]:
    target_tables = {table.casefold() for table in tables}
    definitions: list[tuple[str, str]] = []
    rows = op.get_bind().execute(
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


def _drop_sqlite_triggers(definitions: list[tuple[str, str]]) -> None:
    for name, _statement in definitions:
        quoted_name = name.replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{quoted_name}"')


def _restore_sqlite_triggers(definitions: list[tuple[str, str]]) -> None:
    for _name, statement in definitions:
        op.execute(statement)


def _downgrade_sqlite_contract() -> None:
    op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_package_update")
    op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_package_insert")
    op.execute("DROP TRIGGER IF EXISTS model_asset_validate_package_file_update")
    op.execute("DROP TRIGGER IF EXISTS model_asset_validate_package_file_insert")

    trigger_definitions = _sqlite_trigger_definitions(
        "model_asset", "manifest_revision"
    )
    _drop_sqlite_triggers(trigger_definitions)

    op.drop_index("ix_manifest_revision_spatial_package_id", table_name="manifest_revision")
    with op.batch_alter_table(
        "manifest_revision",
        recreate="always",
        naming_convention=_FK_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_manifest_revision_spatial_package_id_spatial_package",
            type_="foreignkey",
        )
        batch_op.drop_column("spatial_package_id")

    op.drop_index("ix_model_asset_spatial_package_file_id", table_name="model_asset")
    with op.batch_alter_table(
        "model_asset",
        recreate="always",
        naming_convention=_FK_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_model_asset_spatial_package_file_id_spatial_package_file",
            type_="foreignkey",
        )
        batch_op.drop_column("spatial_package_file_id")

    _restore_sqlite_triggers(trigger_definitions)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _create_sqlite_contract()
    else:
        _create_postgresql_contract()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS manifest_revision_validate_package ON manifest_revision")
        op.execute("DROP FUNCTION IF EXISTS fire_viewer_manifest_package_valid()")
        op.execute("DROP TRIGGER IF EXISTS model_asset_validate_package_file ON model_asset")
        op.execute("DROP FUNCTION IF EXISTS fire_viewer_model_asset_package_valid()")
        op.drop_index("ix_manifest_revision_spatial_package_id", table_name="manifest_revision")
        op.drop_column("manifest_revision", "spatial_package_id")
        op.drop_index("ix_model_asset_spatial_package_file_id", table_name="model_asset")
        op.drop_column("model_asset", "spatial_package_file_id")
        return

    _downgrade_sqlite_contract()
