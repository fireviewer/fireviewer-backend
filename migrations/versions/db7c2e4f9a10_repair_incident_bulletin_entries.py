"""repair administrator-verified public bulletin entries

Revision ID: db7c2e4f9a10
Revises: ca3d7e9f2b61
Create Date: 2026-07-27 10:05:00.000000

``d8f3a1c5b720`` was inserted into a migration chain after some databases had
already been stamped at a later revision.  Those databases cannot discover the
new historical step.  This forward-only repair is deliberately idempotent: a
fresh database keeps the table created by ``d8f3a1c5b720`` while an affected
database receives the exact same additive table and indexes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "db7c2e4f9a10"
down_revision: str | None = "ca3d7e9f2b61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_bulletin_entry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "episode_id",
            sa.Integer(),
            sa.ForeignKey("episode.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PUBLISHED"),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("retired_by", sa.String(length=255)),
        sa.Column("retirement_reason", sa.String(length=500)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('fact', 'timeline')", name="ck_bulletin_entry_kind"),
        sa.CheckConstraint("state IN ('PUBLISHED', 'RETIRED')", name="ck_bulletin_entry_state"),
        sa.CheckConstraint("version >= 1", name="ck_bulletin_entry_version"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_entry_id",
        "incident_bulletin_entry",
        ["entry_id"],
        unique=True,
        if_not_exists=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_incident_id",
        "incident_bulletin_entry",
        ["incident_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_episode_id",
        "incident_bulletin_entry",
        ["episode_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_source_id",
        "incident_bulletin_entry",
        ["source_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_kind",
        "incident_bulletin_entry",
        ["kind"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_state",
        "incident_bulletin_entry",
        ["state"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_bulletin_entry_incident_state",
        "incident_bulletin_entry",
        ["incident_id", "state"],
        if_not_exists=True,
    )


def downgrade() -> None:
    # The table may have been created by the historical d8f3a1c5b720 step and
    # may already contain published bulletin data.  A repair downgrade must not
    # destroy it.  The Alembic revision can move back while the additive schema
    # remains compatible with ca3d7e9f2b61.
    return
