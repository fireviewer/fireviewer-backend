"""add explicit spatial geometry kinds and source geometry trace

Revision ID: e5b7c9d2a410
Revises: dc4a7e2f910b
Create Date: 2026-07-28 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "e5b7c9d2a410"
down_revision: str | None = "dc4a7e2f910b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_source_annotation",
        sa.Column("source_geometry_normalized", sa.JSON(), nullable=True),
    )
    op.add_column(
        "agent_spatial_proposal",
        sa.Column("proposal_kind", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_spatial_proposal",
        sa.Column("geometry_geojson", sa.JSON(none_as_null=True), nullable=True),
    )
    op.create_index(
        "ix_agent_spatial_proposal_proposal_kind",
        "agent_spatial_proposal",
        ["proposal_kind"],
        unique=False,
    )

    # Static SQL rendering has no database to inspect.  The schema contract is
    # still rendered below; this data migration runs only during a real online
    # upgrade, where the legacy rows are available to backfill.
    if not context.is_offline_mode():
        connection = op.get_bind()
        annotation = sa.table(
            "agent_source_annotation",
            sa.column("id", sa.Integer()),
            sa.column("source_x_normalized", sa.Float()),
            sa.column("source_y_normalized", sa.Float()),
            sa.column("source_geometry_normalized", sa.JSON()),
        )
        for row in connection.execute(
            sa.select(
                annotation.c.id,
                annotation.c.source_x_normalized,
                annotation.c.source_y_normalized,
            )
        ).mappings():
            connection.execute(
                annotation.update()
                .where(annotation.c.id == row["id"])
                .values(
                    source_geometry_normalized={
                        "type": "Point",
                        "coordinates": [
                            row["source_x_normalized"],
                            row["source_y_normalized"],
                        ],
                    }
                )
            )

        proposal = sa.table(
            "agent_spatial_proposal",
            sa.column("id", sa.Integer()),
            sa.column("status", sa.String()),
            sa.column("longitude", sa.Float()),
            sa.column("latitude", sa.Float()),
            sa.column("proposal_kind", sa.String()),
            sa.column("geometry_geojson", sa.JSON()),
        )
        for row in connection.execute(
            sa.select(
                proposal.c.id,
                proposal.c.longitude,
                proposal.c.latitude,
            ).where(proposal.c.status == "ground_point")
        ).mappings():
            connection.execute(
                proposal.update()
                .where(proposal.c.id == row["id"])
                .values(
                    proposal_kind="legacy_ground_point",
                    geometry_geojson={
                        "type": "Point",
                        "coordinates": [row["longitude"], row["latitude"]],
                    },
                )
            )

    # SQLite needs a reflected source table to render this copy-and-recreate
    # operation.  Offline Alembic rendering deliberately has no connection;
    # it has already emitted the additive columns/index above, while the real
    # deployment migration executes the complete backfill and table rewrite.
    if context.is_offline_mode():
        return

    with op.batch_alter_table("agent_source_annotation") as batch_op:
        batch_op.drop_constraint("ck_agent_annotation_semantic_anchor", type_="check")
        batch_op.drop_constraint("ck_agent_annotation_x", type_="check")
        batch_op.drop_constraint("ck_agent_annotation_y", type_="check")
        batch_op.alter_column(
            "source_x_normalized",
            existing_type=sa.Float(),
            nullable=True,
        )
        batch_op.alter_column(
            "source_y_normalized",
            existing_type=sa.Float(),
            nullable=True,
        )
        batch_op.alter_column(
            "source_geometry_normalized",
            existing_type=sa.JSON(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_semantic_anchor",
            "semantic_anchor IN ('active_fire_point', 'visible_fire_front_point', "
            "'visible_fire_front', 'smoke_column_base', 'smoke_origin_point', "
            "'burned_area_polygon')",
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_x",
            "source_x_normalized IS NULL OR "
            "(source_x_normalized >= 0 AND source_x_normalized <= 1)",
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_y",
            "source_y_normalized IS NULL OR "
            "(source_y_normalized >= 0 AND source_y_normalized <= 1)",
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_point_shape",
            "(source_x_normalized IS NULL AND source_y_normalized IS NULL) OR "
            "(source_x_normalized IS NOT NULL AND source_y_normalized IS NOT NULL)",
        )

    with op.batch_alter_table("agent_spatial_proposal") as batch_op:
        batch_op.drop_constraint("ck_agent_spatial_proposal_status", type_="check")
        batch_op.drop_constraint("ck_agent_spatial_proposal_geometry_shape", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_spatial_proposal_status",
            "status IN ('ground_point', 'projected_geometry', 'insufficient_geometry')",
        )
        batch_op.create_check_constraint(
            "ck_agent_spatial_proposal_kind",
            "proposal_kind IS NULL OR proposal_kind IN "
            "('active_fire_point', 'smoke_origin_point', 'visible_fire_front', "
            "'probable_activity_envelope', 'burned_area_polygon', 'legacy_ground_point')",
        )
        batch_op.create_check_constraint(
            "ck_agent_spatial_proposal_geometry_shape",
            "(status = 'ground_point' AND source_annotation_id IS NOT NULL "
            "AND proposal_kind = 'legacy_ground_point' AND geometry_geojson IS NOT NULL "
            "AND geometry_origin IS NOT NULL AND longitude IS NOT NULL AND latitude IS NOT NULL "
            "AND horizontal_accuracy_m IS NOT NULL AND reference_bundle_sha256 IS NOT NULL) OR "
            "(status = 'projected_geometry' AND proposal_kind IS NOT NULL "
            "AND proposal_kind != 'legacy_ground_point' AND geometry_geojson IS NOT NULL "
            "AND observed_at IS NOT NULL AND geometry_origin IS NOT NULL "
            "AND horizontal_accuracy_m IS NOT NULL AND reference_bundle_sha256 IS NOT NULL "
            "AND (geometry_origin = 'EXPLICIT_SOURCE_GEOMETRY' "
            "OR source_annotation_id IS NOT NULL)) OR "
            "(status = 'insufficient_geometry' AND geometry_origin IS NULL "
            "AND proposal_kind IS NULL AND geometry_geojson IS NULL "
            "AND longitude IS NULL AND latitude IS NULL AND altitude_m IS NULL "
            "AND horizontal_accuracy_m IS NULL)",
        )


def downgrade() -> None:
    connection = op.get_bind()
    projected_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM agent_spatial_proposal "
            "WHERE status = 'projected_geometry'"
        )
    )
    extended_anchor_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM agent_source_annotation "
            "WHERE semantic_anchor NOT IN "
            "('active_fire_point', 'visible_fire_front_point', 'smoke_column_base')"
        )
    )
    pointless_annotation_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM agent_source_annotation "
            "WHERE source_x_normalized IS NULL OR source_y_normalized IS NULL"
        )
    )
    if projected_count or extended_anchor_count or pointless_annotation_count:
        raise RuntimeError(
            "cannot downgrade the spatial geometry contract while extended geometry data exists"
        )

    with op.batch_alter_table("agent_spatial_proposal") as batch_op:
        batch_op.drop_constraint("ck_agent_spatial_proposal_kind", type_="check")
        batch_op.drop_constraint("ck_agent_spatial_proposal_status", type_="check")
        batch_op.drop_constraint("ck_agent_spatial_proposal_geometry_shape", type_="check")
        batch_op.create_check_constraint(
            "ck_agent_spatial_proposal_status",
            "status IN ('ground_point', 'insufficient_geometry')",
        )
        batch_op.create_check_constraint(
            "ck_agent_spatial_proposal_geometry_shape",
            "(status = 'ground_point' AND source_annotation_id IS NOT NULL "
            "AND geometry_origin IS NOT NULL AND longitude IS NOT NULL AND latitude IS NOT NULL "
            "AND horizontal_accuracy_m IS NOT NULL AND reference_bundle_sha256 IS NOT NULL) OR "
            "(status = 'insufficient_geometry' AND geometry_origin IS NULL "
            "AND longitude IS NULL AND latitude IS NULL AND altitude_m IS NULL "
            "AND horizontal_accuracy_m IS NULL)",
        )
    op.drop_index(
        "ix_agent_spatial_proposal_proposal_kind",
        table_name="agent_spatial_proposal",
    )
    with op.batch_alter_table("agent_spatial_proposal") as batch_op:
        batch_op.drop_column("geometry_geojson")
        batch_op.drop_column("proposal_kind")

    with op.batch_alter_table("agent_source_annotation") as batch_op:
        batch_op.drop_constraint("ck_agent_annotation_point_shape", type_="check")
        batch_op.drop_constraint("ck_agent_annotation_semantic_anchor", type_="check")
        batch_op.drop_constraint("ck_agent_annotation_x", type_="check")
        batch_op.drop_constraint("ck_agent_annotation_y", type_="check")
        batch_op.alter_column(
            "source_x_normalized",
            existing_type=sa.Float(),
            nullable=False,
        )
        batch_op.alter_column(
            "source_y_normalized",
            existing_type=sa.Float(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_semantic_anchor",
            "semantic_anchor IN ('active_fire_point', 'visible_fire_front_point', "
            "'smoke_column_base')",
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_x",
            "source_x_normalized >= 0 AND source_x_normalized <= 1",
        )
        batch_op.create_check_constraint(
            "ck_agent_annotation_y",
            "source_y_normalized >= 0 AND source_y_normalized <= 1",
        )
        batch_op.drop_column("source_geometry_normalized")
