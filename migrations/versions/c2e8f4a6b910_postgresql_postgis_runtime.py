"""enable PostgreSQL/PostGIS runtime invariants

Revision ID: c2e8f4a6b910
Revises: a8c1d4e7f920
Create Date: 2026-07-15 20:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2e8f4a6b910"
down_revision: str | None = "a8c1d4e7f920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _upgrade_postgresql() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute(
        "ALTER TABLE incident_series "
        "ADD COLUMN reference_geog geography(Point, 4326), "
        "ADD COLUMN reference_geom_l93 geometry(Point, 2154)"
    )
    op.execute(
        "ALTER TABLE observation "
        "ADD COLUMN geometry_geog geography(Point, 4326), "
        "ADD COLUMN geometry_l93 geometry(Point, 2154)"
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_sync_incident_spatial() RETURNS trigger AS $$
        BEGIN
          NEW.reference_geog := ST_SetSRID(
            ST_MakePoint(NEW.reference_lon, NEW.reference_lat), 4326
          )::geography;
          NEW.reference_geom_l93 := ST_Transform(
            ST_SetSRID(ST_MakePoint(NEW.reference_lon, NEW.reference_lat), 4326), 2154
          );
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_sync_observation_spatial() RETURNS trigger AS $$
        BEGIN
          NEW.geometry_geog := ST_SetSRID(
            ST_MakePoint(NEW.longitude, NEW.latitude), 4326
          )::geography;
          NEW.geometry_l93 := ST_Transform(
            ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326), 2154
          );
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER incident_series_sync_spatial "
        "BEFORE INSERT OR UPDATE OF reference_lon, reference_lat ON incident_series "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_sync_incident_spatial()"
    )
    op.execute(
        "CREATE TRIGGER observation_sync_spatial "
        "BEFORE INSERT OR UPDATE OF longitude, latitude ON observation "
        "FOR EACH ROW EXECUTE FUNCTION fire_viewer_sync_observation_spatial()"
    )
    op.execute(
        "UPDATE incident_series SET reference_lon = reference_lon, reference_lat = reference_lat"
    )
    op.execute("UPDATE observation SET longitude = longitude, latitude = latitude")
    op.execute(
        "ALTER TABLE incident_series "
        "ALTER COLUMN reference_geog SET NOT NULL, "
        "ALTER COLUMN reference_geom_l93 SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE observation "
        "ALTER COLUMN geometry_geog SET NOT NULL, "
        "ALTER COLUMN geometry_l93 SET NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_incident_series_reference_geog_gist "
        "ON incident_series USING gist (reference_geog)"
    )
    op.execute(
        "CREATE INDEX ix_incident_series_reference_geom_l93_gist "
        "ON incident_series USING gist (reference_geom_l93)"
    )
    op.execute(
        "CREATE INDEX ix_observation_geometry_geog_gist "
        "ON observation USING gist (geometry_geog)"
    )
    op.execute(
        "CREATE INDEX ix_observation_geometry_l93_gist "
        "ON observation USING gist (geometry_l93)"
    )
    op.execute(
        """
        CREATE FUNCTION fire_viewer_forbid_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("audit_event", "zone_publication_event"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION fire_viewer_forbid_mutation()"
        )


def _downgrade_postgresql() -> None:
    for table in ("zone_publication_event", "audit_event"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS fire_viewer_forbid_mutation()")
    op.execute("DROP INDEX IF EXISTS ix_observation_geometry_l93_gist")
    op.execute("DROP INDEX IF EXISTS ix_observation_geometry_geog_gist")
    op.execute("DROP INDEX IF EXISTS ix_incident_series_reference_geom_l93_gist")
    op.execute("DROP INDEX IF EXISTS ix_incident_series_reference_geog_gist")
    op.execute("DROP TRIGGER IF EXISTS observation_sync_spatial ON observation")
    op.execute("DROP TRIGGER IF EXISTS incident_series_sync_spatial ON incident_series")
    op.execute("DROP FUNCTION IF EXISTS fire_viewer_sync_observation_spatial()")
    op.execute("DROP FUNCTION IF EXISTS fire_viewer_sync_incident_spatial()")
    op.execute("ALTER TABLE observation DROP COLUMN geometry_l93, DROP COLUMN geometry_geog")
    op.execute(
        "ALTER TABLE incident_series "
        "DROP COLUMN reference_geom_l93, DROP COLUMN reference_geog"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgresql()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _downgrade_postgresql()
