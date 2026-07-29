"""classify generic admin source packages across historical days

Revision ID: f4b7d2c9a610
Revises: e8c4a7d2f610
Create Date: 2026-07-29 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "f4b7d2c9a610"
down_revision: str | None = "e8c4a7d2f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        return

    with op.batch_alter_table("agent_source_package") as batch_op:
        batch_op.drop_constraint("ck_agent_source_package_kind", type_="check")
        batch_op.drop_constraint("ck_agent_source_package_date_order", type_="check")
        batch_op.alter_column(
            "known_start_date",
            existing_type=sa.Date(),
            nullable=True,
        )
        batch_op.alter_column(
            "known_end_date",
            existing_type=sa.Date(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "file_date_metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.create_check_constraint(
            "ck_agent_source_package_kind",
            "package_kind IN ('USER_SOURCES', 'ADMIN_SATELLITE', 'ADMIN_SOURCES')",
        )
        batch_op.create_check_constraint(
            "ck_agent_source_package_date_order",
            "(known_start_date IS NULL AND known_end_date IS NULL) OR "
            "(known_start_date IS NOT NULL AND known_end_date IS NOT NULL "
            "AND known_end_date >= known_start_date)",
        )

    with op.batch_alter_table("agent_source_package_item") as batch_op:
        batch_op.add_column(
            sa.Column(
                "date_classification",
                sa.String(length=20),
                nullable=False,
                server_default="TO_CLASSIFY",
            )
        )
        batch_op.add_column(sa.Column("date_evidence", sa.String(length=40)))
        batch_op.add_column(sa.Column("classified_local_date", sa.Date()))
        batch_op.add_column(sa.Column("analysis_window_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_agent_source_package_item_analysis_window",
            "agent_analysis_window",
            ["analysis_window_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE agent_source_package_item
            SET date_classification = 'CLASSIFIED',
                date_evidence = 'LEGACY_BATCH_WINDOW',
                classified_local_date = (
                    SELECT agent_analysis_window.local_date
                    FROM agent_media_item
                    JOIN agent_media_batch
                      ON agent_media_batch.id = agent_media_item.batch_id
                    JOIN agent_analysis_window
                      ON agent_analysis_window.id = agent_media_batch.analysis_window_id
                    WHERE agent_media_item.id =
                          agent_source_package_item.agent_media_item_id
                ),
                analysis_window_id = (
                    SELECT agent_media_batch.analysis_window_id
                    FROM agent_media_item
                    JOIN agent_media_batch
                      ON agent_media_batch.id = agent_media_item.batch_id
                    WHERE agent_media_item.id =
                          agent_source_package_item.agent_media_item_id
                )
            WHERE agent_media_item_id IS NOT NULL
              AND EXISTS (
                    SELECT 1
                    FROM agent_media_item
                    JOIN agent_media_batch
                      ON agent_media_batch.id = agent_media_item.batch_id
                    WHERE agent_media_item.id =
                          agent_source_package_item.agent_media_item_id
                      AND agent_media_batch.analysis_window_id IS NOT NULL
                )
            """
        )
    )

    with op.batch_alter_table("agent_source_package_item") as batch_op:
        batch_op.create_check_constraint(
            "ck_agent_source_package_item_date_classification",
            "date_classification IN ('CLASSIFIED', 'TO_CLASSIFY')",
        )
        batch_op.create_check_constraint(
            "ck_agent_source_package_item_date_assignment",
            "(date_classification = 'CLASSIFIED' "
            "AND classified_local_date IS NOT NULL AND date_evidence IS NOT NULL) OR "
            "(date_classification = 'TO_CLASSIFY' "
            "AND classified_local_date IS NULL AND analysis_window_id IS NULL)",
        )

    op.create_index(
        "ix_agent_source_package_item_date_classification",
        "agent_source_package_item",
        ["date_classification"],
    )
    op.create_index(
        "ix_agent_source_package_item_classified_local_date",
        "agent_source_package_item",
        ["classified_local_date"],
    )
    op.create_index(
        "ix_agent_source_package_item_analysis_window_id",
        "agent_source_package_item",
        ["analysis_window_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    admin_packages = connection.scalar(
        sa.text(
            "SELECT count(*) FROM agent_source_package "
            "WHERE package_kind = 'ADMIN_SOURCES'"
        )
    )
    if admin_packages:
        raise RuntimeError(
            "cannot downgrade while multi-day admin source packages exist"
        )

    op.drop_index(
        "ix_agent_source_package_item_analysis_window_id",
        table_name="agent_source_package_item",
    )
    op.drop_index(
        "ix_agent_source_package_item_classified_local_date",
        table_name="agent_source_package_item",
    )
    op.drop_index(
        "ix_agent_source_package_item_date_classification",
        table_name="agent_source_package_item",
    )
    with op.batch_alter_table("agent_source_package_item") as batch_op:
        batch_op.drop_constraint(
            "ck_agent_source_package_item_date_assignment",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_agent_source_package_item_date_classification",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_agent_source_package_item_analysis_window",
            type_="foreignkey",
        )
        batch_op.drop_column("analysis_window_id")
        batch_op.drop_column("classified_local_date")
        batch_op.drop_column("date_evidence")
        batch_op.drop_column("date_classification")

    with op.batch_alter_table("agent_source_package") as batch_op:
        batch_op.drop_constraint("ck_agent_source_package_kind", type_="check")
        batch_op.drop_constraint("ck_agent_source_package_date_order", type_="check")
        batch_op.drop_column("file_date_metadata")
        batch_op.alter_column(
            "known_end_date",
            existing_type=sa.Date(),
            nullable=False,
        )
        batch_op.alter_column(
            "known_start_date",
            existing_type=sa.Date(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_source_package_date_order",
            "known_end_date >= known_start_date",
        )
        batch_op.create_check_constraint(
            "ck_agent_source_package_kind",
            "package_kind IN ('USER_SOURCES', 'ADMIN_SATELLITE')",
        )
