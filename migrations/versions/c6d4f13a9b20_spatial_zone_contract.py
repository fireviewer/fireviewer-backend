"""add immutable reusable spatial zones

Revision ID: c6d4f13a9b20
Revises: ab7fe6f3a550
Create Date: 2026-07-12 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d4f13a9b20"
down_revision: str | None = "ab7fe6f3a550"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAF20_GRID_SHA256 = "dc0cc2a38f0ea1029fe72cca3b5b7ed6dfe7e1db2a8d8482b7326ce3d6f25605"


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def _create_sqlite_triggers() -> None:
    op.execute(
        "CREATE TRIGGER spatial_zone_zone_id_immutable "
        "BEFORE UPDATE OF zone_id ON spatial_zone "
        "WHEN NEW.zone_id IS NOT OLD.zone_id "
        "BEGIN SELECT RAISE(ABORT, 'spatial zone identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER spatial_zone_revision_no_update "
        "BEFORE UPDATE ON spatial_zone_revision "
        "BEGIN SELECT RAISE(ABORT, 'spatial zone revisions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER spatial_zone_revision_no_delete "
        "BEFORE DELETE ON spatial_zone_revision "
        "BEGIN SELECT RAISE(ABORT, 'spatial zone revisions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER model_asset_zone_revision_immutable "
        "BEFORE UPDATE OF spatial_zone_revision_id ON model_asset "
        "WHEN OLD.spatial_zone_revision_id IS NOT NULL "
        "AND NEW.spatial_zone_revision_id IS NOT OLD.spatial_zone_revision_id "
        "BEGIN SELECT RAISE(ABORT, 'model asset spatial zone revision is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER model_asset_sha256_immutable "
        "BEFORE UPDATE OF sha256 ON model_asset "
        "WHEN NEW.sha256 IS NOT OLD.sha256 "
        "BEGIN SELECT RAISE(ABORT, 'model asset SHA-256 is immutable'); END"
    )
    legacy_validation = (
        "NEW.legacy_incident_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM episode AS legacy_episode "
        "WHERE legacy_episode.id = NEW.legacy_episode_id "
        "AND legacy_episode.incident_id = NEW.legacy_incident_id)"
    )
    op.execute(
        "CREATE TRIGGER model_asset_validate_legacy_link_insert "
        "BEFORE INSERT ON model_asset WHEN "
        f"{legacy_validation} "
        "BEGIN SELECT RAISE(ABORT, 'legacy asset episode must belong to its legacy incident'); END"
    )
    op.execute(
        "CREATE TRIGGER model_asset_validate_legacy_link_update "
        "BEFORE UPDATE OF legacy_incident_id, legacy_episode_id ON model_asset WHEN "
        f"{legacy_validation} "
        "BEGIN SELECT RAISE(ABORT, 'legacy asset episode must belong to its legacy incident'); END"
    )
    op.execute(
        "CREATE TRIGGER model_asset_legacy_provenance_immutable "
        "BEFORE UPDATE OF legacy_incident_id, legacy_episode_id, legacy_origin_lon, "
        "legacy_origin_lat, legacy_origin_altitude_m, legacy_local_frame, "
        "legacy_meters_per_unit, legacy_vertical_datum ON model_asset "
        "WHEN NEW.legacy_incident_id IS NOT OLD.legacy_incident_id "
        "OR NEW.legacy_episode_id IS NOT OLD.legacy_episode_id "
        "OR NEW.legacy_origin_lon IS NOT OLD.legacy_origin_lon "
        "OR NEW.legacy_origin_lat IS NOT OLD.legacy_origin_lat "
        "OR NEW.legacy_origin_altitude_m IS NOT OLD.legacy_origin_altitude_m "
        "OR NEW.legacy_local_frame IS NOT OLD.legacy_local_frame "
        "OR NEW.legacy_meters_per_unit IS NOT OLD.legacy_meters_per_unit "
        "OR NEW.legacy_vertical_datum IS NOT OLD.legacy_vertical_datum "
        "BEGIN SELECT RAISE(ABORT, 'legacy asset provenance is immutable'); END"
    )
    validation = (
        "(NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NULL) "
        "OR (NEW.asset_id IS NULL AND NEW.spatial_zone_revision_id IS NOT NULL) "
        "OR (NEW.asset_id IS NOT NULL AND NEW.spatial_zone_revision_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM model_asset AS asset "
        "WHERE asset.id = NEW.asset_id "
        "AND asset.spatial_zone_revision_id = NEW.spatial_zone_revision_id))"
    )
    episode_validation = (
        "NOT EXISTS (SELECT 1 FROM episode AS episode "
        "WHERE episode.id = NEW.episode_id AND episode.incident_id = NEW.incident_id)"
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_episode_insert "
        "BEFORE INSERT ON manifest_revision WHEN "
        f"{episode_validation} "
        "BEGIN SELECT RAISE(ABORT, 'manifest episode must belong to its incident'); END"
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_episode_update "
        "BEFORE UPDATE OF incident_id, episode_id ON manifest_revision WHEN "
        f"{episode_validation} "
        "BEGIN SELECT RAISE(ABORT, 'manifest episode must belong to its incident'); END"
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_spatial_link_insert "
        "BEFORE INSERT ON manifest_revision WHEN "
        f"{validation} "
        "BEGIN SELECT RAISE(ABORT, 'manifest asset and spatial zone revision must match'); END"
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_validate_spatial_link_update "
        "BEFORE UPDATE OF asset_id, spatial_zone_revision_id ON manifest_revision "
        "BEGIN "
        "SELECT CASE WHEN NEW.asset_id IS NOT OLD.asset_id "
        "OR NEW.spatial_zone_revision_id IS NOT OLD.spatial_zone_revision_id "
        "THEN RAISE(ABORT, 'manifest asset and spatial zone revision are immutable') END; "
        f"SELECT CASE WHEN {validation} "
        "THEN RAISE(ABORT, 'manifest asset and spatial zone revision must match') END; "
        "END"
    )
    closed_episode_validation = (
        "NOT EXISTS (SELECT 1 FROM manifest_revision AS manifest "
        "JOIN episode AS episode ON episode.id = manifest.episode_id "
        "WHERE manifest.id = NEW.manifest_revision_id "
        "AND manifest.incident_id = NEW.incident_id "
        "AND episode.incident_id = manifest.incident_id "
        "AND manifest.is_current = 1 AND episode.is_current = 1 "
        "AND episode.status = 'CLOSED')"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_requires_closed_episode "
        "BEFORE INSERT ON zone_archive_snapshot WHEN "
        f"{closed_episode_validation} "
        "BEGIN SELECT RAISE(ABORT, 'zone archive snapshot requires a CLOSED episode'); END"
    )
    snapshot_validation = (
        "NOT EXISTS (SELECT 1 FROM manifest_revision AS manifest "
        "JOIN model_asset AS asset ON asset.id = manifest.asset_id "
        "WHERE manifest.id = NEW.manifest_revision_id "
        "AND manifest.incident_id = NEW.incident_id "
        "AND manifest.asset_id = NEW.asset_id "
        "AND manifest.spatial_zone_revision_id = NEW.spatial_zone_revision_id "
        "AND asset.spatial_zone_revision_id = NEW.spatial_zone_revision_id "
        "AND asset.sha256 = NEW.asset_sha256)"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_validate_source_insert "
        "BEFORE INSERT ON zone_archive_snapshot WHEN "
        f"{snapshot_validation} "
        "BEGIN SELECT RAISE(ABORT, 'archive snapshot must match manifest asset and zone revision'); END"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_no_update "
        "BEFORE UPDATE ON zone_archive_snapshot "
        "BEGIN SELECT RAISE(ABORT, 'zone archive snapshots are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_no_delete "
        "BEFORE DELETE ON zone_archive_snapshot "
        "BEGIN SELECT RAISE(ABORT, 'zone archive snapshots are immutable'); END"
    )


def _create_postgresql_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION fire_viewer_spatial_zone_id_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.zone_id IS DISTINCT FROM OLD.zone_id THEN
            RAISE EXCEPTION 'spatial zone identity is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_spatial_zone_revision_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'spatial zone revisions are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_model_asset_spatial_immutable() RETURNS trigger AS $$
        BEGIN
          IF OLD.spatial_zone_revision_id IS NOT NULL
             AND NEW.spatial_zone_revision_id IS DISTINCT FROM OLD.spatial_zone_revision_id THEN
            RAISE EXCEPTION 'model asset spatial zone revision is immutable';
          END IF;
          IF NEW.sha256 IS DISTINCT FROM OLD.sha256 THEN
            RAISE EXCEPTION 'model asset SHA-256 is immutable';
          END IF;
          IF NEW.legacy_incident_id IS DISTINCT FROM OLD.legacy_incident_id
             OR NEW.legacy_episode_id IS DISTINCT FROM OLD.legacy_episode_id
             OR NEW.legacy_origin_lon IS DISTINCT FROM OLD.legacy_origin_lon
             OR NEW.legacy_origin_lat IS DISTINCT FROM OLD.legacy_origin_lat
             OR NEW.legacy_origin_altitude_m IS DISTINCT FROM OLD.legacy_origin_altitude_m
             OR NEW.legacy_local_frame IS DISTINCT FROM OLD.legacy_local_frame
             OR NEW.legacy_meters_per_unit IS DISTINCT FROM OLD.legacy_meters_per_unit
             OR NEW.legacy_vertical_datum IS DISTINCT FROM OLD.legacy_vertical_datum THEN
            RAISE EXCEPTION 'legacy asset provenance is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_model_asset_legacy_link_valid() RETURNS trigger AS $$
        BEGIN
          IF NEW.legacy_incident_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM episode AS legacy_episode
            WHERE legacy_episode.id = NEW.legacy_episode_id
              AND legacy_episode.incident_id = NEW.legacy_incident_id
          ) THEN
            RAISE EXCEPTION 'legacy asset episode must belong to its legacy incident';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_manifest_spatial_link_valid() RETURNS trigger AS $$
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
          IF (NEW.asset_id IS NULL) <> (NEW.spatial_zone_revision_id IS NULL) THEN
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
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_zone_archive_source_valid() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM manifest_revision AS manifest
            JOIN model_asset AS asset ON asset.id = manifest.asset_id
            JOIN episode AS episode ON episode.id = manifest.episode_id
            WHERE manifest.id = NEW.manifest_revision_id
              AND manifest.incident_id = NEW.incident_id
              AND manifest.asset_id = NEW.asset_id
              AND manifest.spatial_zone_revision_id = NEW.spatial_zone_revision_id
              AND asset.spatial_zone_revision_id = NEW.spatial_zone_revision_id
              AND asset.sha256 = NEW.asset_sha256
              AND episode.incident_id = manifest.incident_id
              AND manifest.is_current
              AND episode.is_current
              AND episode.status = 'CLOSED'
          ) THEN
            RAISE EXCEPTION 'archive snapshot must match manifest asset and zone revision';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_zone_archive_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'zone archive snapshots are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER spatial_zone_zone_id_immutable BEFORE UPDATE OF zone_id ON spatial_zone "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_spatial_zone_id_immutable()"
    )
    op.execute(
        "CREATE TRIGGER spatial_zone_revision_no_update BEFORE UPDATE ON spatial_zone_revision "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_spatial_zone_revision_immutable()"
    )
    op.execute(
        "CREATE TRIGGER spatial_zone_revision_no_delete BEFORE DELETE ON spatial_zone_revision "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_spatial_zone_revision_immutable()"
    )
    op.execute(
        "CREATE TRIGGER model_asset_spatial_immutable BEFORE UPDATE ON model_asset "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_model_asset_spatial_immutable()"
    )
    op.execute(
        "CREATE TRIGGER model_asset_legacy_link_valid "
        "BEFORE INSERT OR UPDATE OF legacy_incident_id, legacy_episode_id ON model_asset "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_model_asset_legacy_link_valid()"
    )
    op.execute(
        "CREATE TRIGGER manifest_revision_spatial_link_valid "
        "BEFORE INSERT OR UPDATE OF incident_id, episode_id, asset_id, spatial_zone_revision_id "
        "ON manifest_revision "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_manifest_spatial_link_valid()"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_source_valid BEFORE INSERT ON zone_archive_snapshot "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_zone_archive_source_valid()"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_no_update BEFORE UPDATE ON zone_archive_snapshot "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_zone_archive_immutable()"
    )
    op.execute(
        "CREATE TRIGGER zone_archive_snapshot_no_delete BEFORE DELETE ON zone_archive_snapshot "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_zone_archive_immutable()"
    )


def _drop_immutable_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger in (
            "zone_archive_snapshot_no_delete",
            "zone_archive_snapshot_no_update",
            "zone_archive_snapshot_validate_source_insert",
            "zone_archive_snapshot_requires_closed_episode",
            "manifest_revision_validate_spatial_link_update",
            "manifest_revision_validate_spatial_link_insert",
            "manifest_revision_validate_episode_update",
            "manifest_revision_validate_episode_insert",
            "model_asset_legacy_provenance_immutable",
            "model_asset_validate_legacy_link_update",
            "model_asset_validate_legacy_link_insert",
            "model_asset_sha256_immutable",
            "model_asset_zone_revision_immutable",
            "spatial_zone_revision_no_delete",
            "spatial_zone_revision_no_update",
            "spatial_zone_zone_id_immutable",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif dialect == "postgresql":
        for trigger, table in (
            ("zone_archive_snapshot_no_delete", "zone_archive_snapshot"),
            ("zone_archive_snapshot_no_update", "zone_archive_snapshot"),
            ("zone_archive_snapshot_source_valid", "zone_archive_snapshot"),
            ("manifest_revision_spatial_link_valid", "manifest_revision"),
            ("model_asset_legacy_link_valid", "model_asset"),
            ("model_asset_spatial_immutable", "model_asset"),
            ("spatial_zone_revision_no_delete", "spatial_zone_revision"),
            ("spatial_zone_revision_no_update", "spatial_zone_revision"),
            ("spatial_zone_zone_id_immutable", "spatial_zone"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        for function in (
            "fire_viewer_zone_archive_immutable",
            "fire_viewer_zone_archive_source_valid",
            "fire_viewer_manifest_spatial_link_valid",
            "fire_viewer_model_asset_legacy_link_valid",
            "fire_viewer_model_asset_spatial_immutable",
            "fire_viewer_spatial_zone_revision_immutable",
            "fire_viewer_spatial_zone_id_immutable",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")


def _sqlite_model_asset_v1() -> sa.Table:
    """Exact FV-003 schema for SQLite offline batch SQL generation.

    In online mode Alembic still takes its normal reflected-table path.  The
    static definition only lets `alembic upgrade --sql` render the historical
    SQLite table-copy operation without a live connection.
    """

    metadata = sa.MetaData()
    table = sa.Table(
        "model_asset",
        metadata,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lod", sa.Enum("MOBILE", "DESKTOP", name="asset_lod", native_enum=False), nullable=False),
        sa.Column("state", sa.Enum("GENERATED", "VALIDATED", "PUBLISHED", "SUPERSEDED", "QUARANTINED", "DELETED_TOMBSTONE", name="asset_state", native_enum=False), nullable=False),
        sa.Column("glb_url", sa.String(length=2048), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("origin_lon", sa.Float(), nullable=False),
        sa.Column("origin_lat", sa.Float(), nullable=False),
        sa.Column("origin_altitude_m", sa.Float(), nullable=False),
        sa.Column("local_frame", sa.String(length=16), nullable=False),
        sa.Column("meters_per_unit", sa.Float(), nullable=False),
        sa.Column("vertical_datum", sa.String(length=128), nullable=False),
        sa.Column("terrain_source_year", sa.Integer()),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Added immediately before the SQLite batch recreation below.  They
        # coexist with the original fields at that exact point in history.
        sa.Column("legacy_incident_id", sa.Integer()),
        sa.Column("legacy_episode_id", sa.Integer()),
        sa.Column("legacy_origin_lon", sa.Float()),
        sa.Column("legacy_origin_lat", sa.Float()),
        sa.Column("legacy_origin_altitude_m", sa.Float()),
        sa.Column("legacy_local_frame", sa.String(length=16)),
        sa.Column("legacy_meters_per_unit", sa.Float()),
        sa.Column("legacy_vertical_datum", sa.String(length=128)),
        sa.CheckConstraint("meters_per_unit > 0", name="ck_asset_scale"),
        sa.CheckConstraint("size_bytes > 0", name="ck_asset_size"),
        sa.CheckConstraint("version >= 1", name="ck_asset_version"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "episode_id", "version", "lod", name="uq_asset_version_lod"),
    )
    sa.Index("ix_model_asset_asset_id", table.c.asset_id, unique=True)
    sa.Index("ix_model_asset_episode_id", table.c.episode_id)
    sa.Index("ix_model_asset_incident_id", table.c.incident_id)
    return table


def _sqlite_manifest_revision_v1() -> sa.Table:
    """Exact FV-003 manifest table before the spatial-link batch migration."""

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
        sa.CheckConstraint("revision >= 1", name="ck_manifest_revision"),
        sa.ForeignKeyConstraint(["asset_id"], ["model_asset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "revision", name="uq_manifest_revision"),
    )
    sa.Index("ix_manifest_revision_incident_id", table.c.incident_id)
    sa.Index("uq_manifest_one_current", table.c.incident_id, unique=True, sqlite_where=sa.text("is_current = 1"))
    return table


def _upgrade_model_asset() -> None:
    """Detach spatial truth while retaining FV-003 ownership as audit-only provenance."""

    dialect = op.get_bind().dialect.name
    op.execute(
        "UPDATE model_asset SET state = 'QUARANTINED', published_at = NULL "
        "WHERE state <> 'DELETED_TOMBSTONE'"
    )
    if dialect == "postgresql":
        # Native ALTERs keep manifest_revision.asset_id valid.  Recreating model_asset would
        # otherwise require dropping an external PostgreSQL foreign key.
        op.drop_constraint("uq_asset_version_lod", "model_asset", type_="unique")
        op.drop_constraint("ck_asset_scale", "model_asset", type_="check")
        op.drop_index("ix_model_asset_incident_id", table_name="model_asset")
        op.drop_index("ix_model_asset_episode_id", table_name="model_asset")
        op.alter_column("model_asset", "incident_id", new_column_name="legacy_incident_id")
        op.alter_column("model_asset", "episode_id", new_column_name="legacy_episode_id")
        op.alter_column("model_asset", "origin_lon", new_column_name="legacy_origin_lon")
        op.alter_column("model_asset", "origin_lat", new_column_name="legacy_origin_lat")
        op.alter_column(
            "model_asset", "origin_altitude_m", new_column_name="legacy_origin_altitude_m"
        )
        op.alter_column("model_asset", "local_frame", new_column_name="legacy_local_frame")
        op.alter_column("model_asset", "meters_per_unit", new_column_name="legacy_meters_per_unit")
        op.alter_column("model_asset", "vertical_datum", new_column_name="legacy_vertical_datum")
        op.alter_column("model_asset", "legacy_incident_id", nullable=True)
        op.alter_column("model_asset", "legacy_episode_id", nullable=True)
        for column in (
            "legacy_origin_lon",
            "legacy_origin_lat",
            "legacy_origin_altitude_m",
            "legacy_local_frame",
            "legacy_meters_per_unit",
            "legacy_vertical_datum",
        ):
            op.alter_column("model_asset", column, nullable=True)
        op.add_column(
            "model_asset", sa.Column("spatial_zone_revision_id", sa.Integer(), nullable=True)
        )
        op.create_foreign_key(
            "fk_model_asset_spatial_zone_revision",
            "model_asset",
            "spatial_zone_revision",
            ["spatial_zone_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            "ix_model_asset_legacy_incident_id",
            "model_asset",
            ["legacy_incident_id"],
            unique=False,
        )
        op.create_index(
            "ix_model_asset_legacy_episode_id",
            "model_asset",
            ["legacy_episode_id"],
            unique=False,
        )
        op.create_index(
            "ix_model_asset_spatial_zone_revision_id",
            "model_asset",
            ["spatial_zone_revision_id"],
            unique=False,
        )
        op.create_unique_constraint(
            "uq_asset_version_lod",
            "model_asset",
            ["spatial_zone_revision_id", "version", "lod"],
        )
        op.create_check_constraint(
            "ck_asset_zone_revision_required",
            "model_asset",
            "spatial_zone_revision_id IS NOT NULL OR state IN ('QUARANTINED', 'DELETED_TOMBSTONE')",
        )
        op.create_check_constraint(
            "ck_asset_legacy_provenance",
            "model_asset",
            "(legacy_incident_id IS NULL AND legacy_episode_id IS NULL "
            "AND legacy_origin_lon IS NULL AND legacy_origin_lat IS NULL "
            "AND legacy_origin_altitude_m IS NULL AND legacy_local_frame IS NULL "
            "AND legacy_meters_per_unit IS NULL AND legacy_vertical_datum IS NULL) "
            "OR (legacy_incident_id IS NOT NULL AND legacy_episode_id IS NOT NULL "
            "AND legacy_origin_lon IS NOT NULL AND legacy_origin_lat IS NOT NULL "
            "AND legacy_origin_altitude_m IS NOT NULL AND legacy_local_frame IS NOT NULL "
            "AND legacy_meters_per_unit IS NOT NULL AND legacy_vertical_datum IS NOT NULL)",
        )
        return

    # SQLite needs a table recreation to drop the old spatial columns.  Copy the two historic
    # ownership values first, then make them nullable audit references in the recreated table.
    op.add_column("model_asset", sa.Column("legacy_incident_id", sa.Integer(), nullable=True))
    op.add_column("model_asset", sa.Column("legacy_episode_id", sa.Integer(), nullable=True))
    op.add_column("model_asset", sa.Column("legacy_origin_lon", sa.Float(), nullable=True))
    op.add_column("model_asset", sa.Column("legacy_origin_lat", sa.Float(), nullable=True))
    op.add_column("model_asset", sa.Column("legacy_origin_altitude_m", sa.Float(), nullable=True))
    op.add_column(
        "model_asset", sa.Column("legacy_local_frame", sa.String(length=16), nullable=True)
    )
    op.add_column("model_asset", sa.Column("legacy_meters_per_unit", sa.Float(), nullable=True))
    op.add_column(
        "model_asset", sa.Column("legacy_vertical_datum", sa.String(length=128), nullable=True)
    )
    op.execute(
        "UPDATE model_asset SET legacy_incident_id = incident_id, "
        "legacy_episode_id = episode_id, legacy_origin_lon = origin_lon, "
        "legacy_origin_lat = origin_lat, legacy_origin_altitude_m = origin_altitude_m, "
        "legacy_local_frame = local_frame, legacy_meters_per_unit = meters_per_unit, "
        "legacy_vertical_datum = vertical_datum"
    )
    with op.batch_alter_table(
        "model_asset", schema=None, recreate="always", copy_from=_sqlite_model_asset_v1()
    ) as batch_op:
        batch_op.add_column(sa.Column("spatial_zone_revision_id", sa.Integer(), nullable=True))
        batch_op.drop_index(batch_op.f("ix_model_asset_incident_id"))
        batch_op.drop_index(batch_op.f("ix_model_asset_episode_id"))
        batch_op.drop_constraint("uq_asset_version_lod", type_="unique")
        batch_op.drop_constraint("ck_asset_scale", type_="check")
        batch_op.drop_column("incident_id")
        batch_op.drop_column("episode_id")
        batch_op.drop_column("origin_lon")
        batch_op.drop_column("origin_lat")
        batch_op.drop_column("origin_altitude_m")
        batch_op.drop_column("local_frame")
        batch_op.drop_column("meters_per_unit")
        batch_op.drop_column("vertical_datum")
        batch_op.create_foreign_key(
            "fk_model_asset_legacy_incident",
            "incident_series",
            ["legacy_incident_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_model_asset_legacy_episode",
            "episode",
            ["legacy_episode_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_model_asset_spatial_zone_revision",
            "spatial_zone_revision",
            ["spatial_zone_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            batch_op.f("ix_model_asset_legacy_incident_id"), ["legacy_incident_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_model_asset_legacy_episode_id"), ["legacy_episode_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_model_asset_spatial_zone_revision_id"),
            ["spatial_zone_revision_id"],
            unique=False,
        )
        batch_op.create_unique_constraint(
            "uq_asset_version_lod", ["spatial_zone_revision_id", "version", "lod"]
        )
        batch_op.create_check_constraint(
            "ck_asset_zone_revision_required",
            "spatial_zone_revision_id IS NOT NULL OR state IN ('QUARANTINED', 'DELETED_TOMBSTONE')",
        )
        batch_op.create_check_constraint(
            "ck_asset_legacy_provenance",
            "(legacy_incident_id IS NULL AND legacy_episode_id IS NULL "
            "AND legacy_origin_lon IS NULL AND legacy_origin_lat IS NULL "
            "AND legacy_origin_altitude_m IS NULL AND legacy_local_frame IS NULL "
            "AND legacy_meters_per_unit IS NULL AND legacy_vertical_datum IS NULL) "
            "OR (legacy_incident_id IS NOT NULL AND legacy_episode_id IS NOT NULL "
            "AND legacy_origin_lon IS NOT NULL AND legacy_origin_lat IS NOT NULL "
            "AND legacy_origin_altitude_m IS NOT NULL AND legacy_local_frame IS NOT NULL "
            "AND legacy_meters_per_unit IS NOT NULL AND legacy_vertical_datum IS NOT NULL)",
        )


def _downgrade_model_asset() -> None:
    """Restore the FV-003 table only for an empty model_asset table."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.drop_index("ix_model_asset_spatial_zone_revision_id", table_name="model_asset")
        op.drop_index("ix_model_asset_legacy_episode_id", table_name="model_asset")
        op.drop_index("ix_model_asset_legacy_incident_id", table_name="model_asset")
        op.drop_constraint("ck_asset_legacy_provenance", "model_asset", type_="check")
        op.drop_constraint("ck_asset_zone_revision_required", "model_asset", type_="check")
        op.drop_constraint("uq_asset_version_lod", "model_asset", type_="unique")
        op.drop_constraint(
            "fk_model_asset_spatial_zone_revision", "model_asset", type_="foreignkey"
        )
        op.drop_column("model_asset", "spatial_zone_revision_id")
        op.alter_column("model_asset", "legacy_incident_id", new_column_name="incident_id")
        op.alter_column("model_asset", "legacy_episode_id", new_column_name="episode_id")
        op.alter_column("model_asset", "legacy_origin_lon", new_column_name="origin_lon")
        op.alter_column("model_asset", "legacy_origin_lat", new_column_name="origin_lat")
        op.alter_column(
            "model_asset", "legacy_origin_altitude_m", new_column_name="origin_altitude_m"
        )
        op.alter_column("model_asset", "legacy_local_frame", new_column_name="local_frame")
        op.alter_column("model_asset", "legacy_meters_per_unit", new_column_name="meters_per_unit")
        op.alter_column("model_asset", "legacy_vertical_datum", new_column_name="vertical_datum")
        op.alter_column("model_asset", "incident_id", nullable=False)
        op.alter_column("model_asset", "episode_id", nullable=False)
        for column in (
            "origin_lon",
            "origin_lat",
            "origin_altitude_m",
            "local_frame",
            "meters_per_unit",
            "vertical_datum",
        ):
            op.alter_column("model_asset", column, nullable=False)
        op.create_index("ix_model_asset_incident_id", "model_asset", ["incident_id"], unique=False)
        op.create_index("ix_model_asset_episode_id", "model_asset", ["episode_id"], unique=False)
        op.create_unique_constraint(
            "uq_asset_version_lod", "model_asset", ["incident_id", "episode_id", "version", "lod"]
        )
        op.create_check_constraint("ck_asset_scale", "model_asset", "meters_per_unit > 0")
        return

    with op.batch_alter_table("model_asset", schema=None, recreate="always") as batch_op:
        batch_op.drop_index(batch_op.f("ix_model_asset_spatial_zone_revision_id"))
        batch_op.drop_index(batch_op.f("ix_model_asset_legacy_episode_id"))
        batch_op.drop_index(batch_op.f("ix_model_asset_legacy_incident_id"))
        batch_op.drop_constraint("ck_asset_legacy_provenance", type_="check")
        batch_op.drop_constraint("ck_asset_zone_revision_required", type_="check")
        batch_op.drop_constraint("fk_model_asset_spatial_zone_revision", type_="foreignkey")
        batch_op.drop_constraint("fk_model_asset_legacy_episode", type_="foreignkey")
        batch_op.drop_constraint("fk_model_asset_legacy_incident", type_="foreignkey")
        batch_op.drop_constraint("uq_asset_version_lod", type_="unique")
        batch_op.add_column(sa.Column("incident_id", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("episode_id", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("origin_lon", sa.Float(), nullable=False))
        batch_op.add_column(sa.Column("origin_lat", sa.Float(), nullable=False))
        batch_op.add_column(sa.Column("origin_altitude_m", sa.Float(), nullable=False))
        batch_op.add_column(sa.Column("local_frame", sa.String(length=16), nullable=False))
        batch_op.add_column(sa.Column("meters_per_unit", sa.Float(), nullable=False))
        batch_op.add_column(sa.Column("vertical_datum", sa.String(length=128), nullable=False))
        batch_op.drop_column("spatial_zone_revision_id")
        batch_op.drop_column("legacy_incident_id")
        batch_op.drop_column("legacy_episode_id")
        batch_op.drop_column("legacy_origin_lon")
        batch_op.drop_column("legacy_origin_lat")
        batch_op.drop_column("legacy_origin_altitude_m")
        batch_op.drop_column("legacy_local_frame")
        batch_op.drop_column("legacy_meters_per_unit")
        batch_op.drop_column("legacy_vertical_datum")
        batch_op.create_foreign_key(
            "fk_model_asset_incident",
            "incident_series",
            ["incident_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_model_asset_episode", "episode", ["episode_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_index(
            batch_op.f("ix_model_asset_incident_id"), ["incident_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_model_asset_episode_id"), ["episode_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_asset_version_lod", ["incident_id", "episode_id", "version", "lod"]
        )
        batch_op.create_check_constraint("ck_asset_scale", "meters_per_unit > 0")


def upgrade() -> None:
    op.create_table(
        "spatial_zone",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("spatial_zone", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_spatial_zone_zone_id"), ["zone_id"], unique=True)

    op.create_table(
        "spatial_zone_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spatial_zone_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin_lon", sa.Float(), nullable=False),
        sa.Column("origin_lat", sa.Float(), nullable=False),
        sa.Column("source_orthometric_height_m", sa.Float(), nullable=False),
        sa.Column("geoid_undulation_m", sa.Float(), nullable=False),
        sa.Column("origin_ellipsoid_height_m", sa.Float(), nullable=False),
        sa.Column("source_vertical_datum", sa.String(length=128), nullable=False),
        sa.Column("vertical_transform_id", sa.String(length=64), nullable=False),
        sa.Column("vertical_grid_filename", sa.String(length=255), nullable=False),
        sa.Column("vertical_grid_sha256", sa.String(length=64), nullable=False),
        sa.Column("vertical_datum", sa.String(length=128), nullable=False),
        sa.Column("local_frame", sa.String(length=16), nullable=False),
        sa.Column("meters_per_unit", sa.Float(), nullable=False),
        sa.Column("unity_profile", sa.String(length=64), nullable=False),
        sa.Column("gltf_to_unity_profile", sa.String(length=64), nullable=False),
        sa.Column("min_east_m", sa.Float(), nullable=False),
        sa.Column("max_east_m", sa.Float(), nullable=False),
        sa.Column("min_north_m", sa.Float(), nullable=False),
        sa.Column("max_north_m", sa.Float(), nullable=False),
        sa.Column("min_up_m", sa.Float(), nullable=False),
        sa.Column("max_up_m", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_spatial_zone_revision_positive"),
        sa.CheckConstraint("origin_lon >= -5.5 AND origin_lon <= 10.0", name="ck_spatial_zone_lon"),
        sa.CheckConstraint("origin_lat >= 42.0 AND origin_lat <= 51.5", name="ck_spatial_zone_lat"),
        sa.CheckConstraint(
            "origin_lon > -1e308 AND origin_lon < 1e308 "
            "AND origin_lat > -1e308 AND origin_lat < 1e308 "
            "AND source_orthometric_height_m > -1e308 "
            "AND source_orthometric_height_m < 1e308 "
            "AND geoid_undulation_m > -1e308 AND geoid_undulation_m < 1e308 "
            "AND origin_ellipsoid_height_m > -1e308 "
            "AND origin_ellipsoid_height_m < 1e308",
            name="ck_spatial_zone_origin_finite",
        ),
        sa.CheckConstraint(
            "abs(origin_ellipsoid_height_m - source_orthometric_height_m "
            "- geoid_undulation_m) <= 0.001",
            name="ck_spatial_zone_vertical_derivation",
        ),
        sa.CheckConstraint(
            "NOT (origin_lon >= 8.3 AND origin_lon <= 9.8 "
            "AND origin_lat >= 41.0 AND origin_lat <= 43.3)",
            name="ck_spatial_zone_not_corsica",
        ),
        sa.CheckConstraint(
            "source_vertical_datum = 'NGF-IGN69'", name="ck_spatial_zone_source_datum"
        ),
        sa.CheckConstraint("vertical_transform_id = 'RAF20'", name="ck_spatial_zone_transform"),
        sa.CheckConstraint(
            "vertical_grid_filename = 'fr_ign_RAF20.tif'", name="ck_spatial_zone_grid_filename"
        ),
        sa.CheckConstraint(
            f"vertical_grid_sha256 = '{RAF20_GRID_SHA256}'", name="ck_spatial_zone_grid_hash"
        ),
        sa.CheckConstraint("vertical_datum = 'EPSG:4979'", name="ck_spatial_zone_datum"),
        sa.CheckConstraint("local_frame = 'ENU'", name="ck_spatial_zone_frame"),
        sa.CheckConstraint("meters_per_unit = 0.01", name="ck_spatial_zone_scale"),
        sa.CheckConstraint(
            "unity_profile = 'unity-eun-100-v1'", name="ck_spatial_zone_unity_profile"
        ),
        sa.CheckConstraint(
            "gltf_to_unity_profile = 'gltf-eun-negz-metric-v1'",
            name="ck_spatial_zone_gltf_profile",
        ),
        sa.CheckConstraint(
            "min_east_m < max_east_m AND min_east_m <= 0 AND max_east_m >= 0",
            name="ck_spatial_zone_east_bounds",
        ),
        sa.CheckConstraint(
            "min_east_m > -1e308 AND min_east_m < 1e308 "
            "AND max_east_m > -1e308 AND max_east_m < 1e308 "
            "AND min_north_m > -1e308 AND min_north_m < 1e308 "
            "AND max_north_m > -1e308 AND max_north_m < 1e308 "
            "AND min_up_m > -1e308 AND min_up_m < 1e308 "
            "AND max_up_m > -1e308 AND max_up_m < 1e308",
            name="ck_spatial_zone_bounds_finite",
        ),
        sa.CheckConstraint(
            "min_north_m < max_north_m AND min_north_m <= 0 AND max_north_m >= 0",
            name="ck_spatial_zone_north_bounds",
        ),
        sa.CheckConstraint(
            "min_up_m < max_up_m AND min_up_m <= 0 AND max_up_m >= 0",
            name="ck_spatial_zone_up_bounds",
        ),
        sa.ForeignKeyConstraint(["spatial_zone_id"], ["spatial_zone.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spatial_zone_id", "revision", name="uq_spatial_zone_revision"),
    )
    with op.batch_alter_table("spatial_zone_revision", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_spatial_zone_revision_spatial_zone_id"),
            ["spatial_zone_id"],
            unique=False,
        )

    _upgrade_model_asset()

    with op.batch_alter_table(
        "manifest_revision", schema=None, recreate="always", copy_from=_sqlite_manifest_revision_v1()
    ) as batch_op:
        batch_op.add_column(sa.Column("spatial_zone_revision_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_manifest_revision_spatial_zone_revision",
            "spatial_zone_revision",
            ["spatial_zone_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            batch_op.f("ix_manifest_revision_spatial_zone_revision_id"),
            ["spatial_zone_revision_id"],
            unique=False,
        )
        batch_op.create_check_constraint(
            "ck_manifest_zone_requires_asset",
            "spatial_zone_revision_id IS NULL OR asset_id IS NOT NULL",
        )

    op.create_table(
        "zone_archive_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("archive_id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("manifest_revision_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("spatial_zone_revision_id", sa.Integer(), nullable=False),
        sa.Column("image_url", sa.String(length=2048), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("asset_sha256", sa.String(length=64), nullable=False),
        sa.Column("render_profile", sa.String(length=128), nullable=False),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("media_type = 'image/png'", name="ck_zone_archive_png"),
        sa.CheckConstraint(_sha256_hex_check("sha256"), name="ck_zone_archive_sha256"),
        sa.CheckConstraint(_sha256_hex_check("asset_sha256"), name="ck_zone_archive_asset_sha256"),
        sa.CheckConstraint("lower(image_url) NOT LIKE '%.glb%'", name="ck_zone_archive_not_glb"),
        sa.CheckConstraint("lower(image_url) LIKE '%.png'", name="ck_zone_archive_png_url"),
        sa.ForeignKeyConstraint(["asset_id"], ["model_asset.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["manifest_revision_id"], ["manifest_revision.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["spatial_zone_revision_id"], ["spatial_zone_revision.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("archive_id"),
        sa.UniqueConstraint("incident_id"),
        sa.UniqueConstraint("manifest_revision_id"),
    )
    with op.batch_alter_table("zone_archive_snapshot", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_zone_archive_snapshot_archive_id"), ["archive_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_zone_archive_snapshot_asset_id"), ["asset_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_zone_archive_snapshot_incident_id"), ["incident_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_zone_archive_snapshot_manifest_revision_id"),
            ["manifest_revision_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_zone_archive_snapshot_spatial_zone_revision_id"),
            ["spatial_zone_revision_id"],
            unique=False,
        )

    if op.get_bind().dialect.name == "sqlite":
        _create_sqlite_triggers()
    elif op.get_bind().dialect.name == "postgresql":
        _create_postgresql_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM model_asset")).scalar_one():
        raise RuntimeError(
            "Cannot downgrade FV-004 while model asset rows exist: their former incident and "
            "asset-local spatial provenance cannot be reconstructed safely."
        )
    if bind.execute(sa.text("SELECT COUNT(*) FROM zone_archive_snapshot")).scalar_one():
        raise RuntimeError("Cannot downgrade FV-004 after creating an archive snapshot")
    if bind.execute(
        sa.text("SELECT COUNT(*) FROM model_asset WHERE spatial_zone_revision_id IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("Cannot downgrade FV-004 after assigning model assets to spatial zones")
    if bind.execute(
        sa.text("SELECT COUNT(*) FROM manifest_revision WHERE spatial_zone_revision_id IS NOT NULL")
    ).scalar_one():
        raise RuntimeError("Cannot downgrade FV-004 after publishing a spatial manifest revision")

    _drop_immutable_triggers()

    with op.batch_alter_table("zone_archive_snapshot", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_zone_archive_snapshot_spatial_zone_revision_id"))
        batch_op.drop_index(batch_op.f("ix_zone_archive_snapshot_manifest_revision_id"))
        batch_op.drop_index(batch_op.f("ix_zone_archive_snapshot_incident_id"))
        batch_op.drop_index(batch_op.f("ix_zone_archive_snapshot_asset_id"))
        batch_op.drop_index(batch_op.f("ix_zone_archive_snapshot_archive_id"))
    op.drop_table("zone_archive_snapshot")

    with op.batch_alter_table("manifest_revision", schema=None, recreate="always") as batch_op:
        batch_op.drop_index(batch_op.f("ix_manifest_revision_spatial_zone_revision_id"))
        batch_op.drop_constraint("ck_manifest_zone_requires_asset", type_="check")
        batch_op.drop_constraint("fk_manifest_revision_spatial_zone_revision", type_="foreignkey")
        batch_op.drop_column("spatial_zone_revision_id")

    _downgrade_model_asset()

    with op.batch_alter_table("spatial_zone_revision", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_spatial_zone_revision_spatial_zone_id"))
    op.drop_table("spatial_zone_revision")
    with op.batch_alter_table("spatial_zone", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_spatial_zone_zone_id"))
    op.drop_table("spatial_zone")
