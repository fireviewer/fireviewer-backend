"""add daily satellite source package kind

Revision ID: e8c4a7d2f610
Revises: e5b7c9d2a410
Create Date: 2026-07-29 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8c4a7d2f610"
down_revision: str | None = "e5b7c9d2a410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_source_package") as batch_op:
        batch_op.add_column(
            sa.Column(
                "package_kind",
                sa.String(length=15),
                nullable=False,
                server_default="USER_SOURCES",
            )
        )
        batch_op.create_check_constraint(
            "ck_agent_source_package_kind",
            "package_kind IN ('USER_SOURCES', 'ADMIN_SATELLITE')",
        )
    op.create_index(
        "ix_agent_source_package_package_kind",
        "agent_source_package",
        ["package_kind"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    admin_packages = connection.scalar(
        sa.text(
            "SELECT count(*) FROM agent_source_package "
            "WHERE package_kind = 'ADMIN_SATELLITE'"
        )
    )
    if admin_packages:
        raise RuntimeError(
            "cannot downgrade while daily satellite source packages exist"
        )
    op.drop_index(
        "ix_agent_source_package_package_kind",
        table_name="agent_source_package",
    )
    with op.batch_alter_table("agent_source_package") as batch_op:
        batch_op.drop_constraint("ck_agent_source_package_kind", type_="check")
        batch_op.drop_column("package_kind")
