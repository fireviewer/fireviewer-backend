"""add local admin sessions and job controls

Revision ID: f7a2c3d4e5f6
Revises: e4b7c9d1a830
Create Date: 2026-07-15 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a2c3d4e5f6"
down_revision: str | None = "e4b7c9d1a830"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_local_session",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("csrf_token", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_local_session_expires", "admin_local_session", ["expires_at"])
    op.create_index("ix_admin_local_session_idle", "admin_local_session", ["idle_expires_at"])
    op.create_table(
        "admin_login_attempt",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("origin_hash", sa.String(length=64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_login_attempt_origin", "admin_login_attempt", ["origin_hash"])
    op.create_index("ix_admin_login_attempt_when", "admin_login_attempt", ["attempted_at"])
    with op.batch_alter_table("job") as batch:
        batch.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("cancel_reason", sa.String(length=500)))


def downgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.drop_column("cancel_reason")
        batch.drop_column("cancel_requested_at")
    op.drop_index("ix_admin_login_attempt_when", table_name="admin_login_attempt")
    op.drop_index("ix_admin_login_attempt_origin", table_name="admin_login_attempt")
    op.drop_table("admin_login_attempt")
    op.drop_index("ix_admin_local_session_idle", table_name="admin_local_session")
    op.drop_index("ix_admin_local_session_expires", table_name="admin_local_session")
    op.drop_table("admin_local_session")
