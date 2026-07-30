"""distinguish active fire zones from cumulative burned areas

Revision ID: a6c9d1e4f720
Revises: f4b7d2c9a610
Create Date: 2026-07-30 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "a6c9d1e4f720"
down_revision: str | None = "f4b7d2c9a610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        return

    with op.batch_alter_table("active_fire_zone_revision") as batch_op:
        batch_op.add_column(
            sa.Column(
                "zone_kind",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.drop_constraint("uq_active_zone_revision", type_="unique")
        batch_op.create_unique_constraint(
            "uq_zone_revision_by_kind",
            ["incident_id", "episode_id", "zone_kind", "revision"],
        )
        batch_op.create_check_constraint(
            "ck_active_zone_kind",
            "zone_kind IN ('active', 'burned')",
        )


def downgrade() -> None:
    if context.is_offline_mode():
        return

    with op.batch_alter_table("active_fire_zone_revision") as batch_op:
        batch_op.drop_constraint("ck_active_zone_kind", type_="check")
        batch_op.drop_constraint("uq_zone_revision_by_kind", type_="unique")
        batch_op.create_unique_constraint(
            "uq_active_zone_revision",
            ["incident_id", "episode_id", "revision"],
        )
        batch_op.drop_column("zone_kind")
