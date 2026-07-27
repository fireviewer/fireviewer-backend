"""add administrator-verified public bulletin entries

Revision ID: d8f3a1c5b720
Revises: c9a2d4e7f150
Create Date: 2026-07-27 10:30:00.000000

The table is deliberately limited to public-bulletin facts and timeline
entries.  It does not contain gallery media, private contribution payloads or
spatial package metadata.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f3a1c5b720"
down_revision: str | None = "c9a2d4e7f150"
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
    )
    op.create_index(
        "ix_incident_bulletin_entry_entry_id",
        "incident_bulletin_entry",
        ["entry_id"],
        unique=True,
    )
    op.create_index(
        "ix_incident_bulletin_entry_incident_id", "incident_bulletin_entry", ["incident_id"]
    )
    op.create_index(
        "ix_incident_bulletin_entry_episode_id", "incident_bulletin_entry", ["episode_id"]
    )
    op.create_index(
        "ix_incident_bulletin_entry_source_id", "incident_bulletin_entry", ["source_id"]
    )
    op.create_index("ix_incident_bulletin_entry_kind", "incident_bulletin_entry", ["kind"])
    op.create_index("ix_incident_bulletin_entry_state", "incident_bulletin_entry", ["state"])
    op.create_index(
        "ix_bulletin_entry_incident_state",
        "incident_bulletin_entry",
        ["incident_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_bulletin_entry_incident_state", table_name="incident_bulletin_entry")
    op.drop_index("ix_incident_bulletin_entry_state", table_name="incident_bulletin_entry")
    op.drop_index("ix_incident_bulletin_entry_kind", table_name="incident_bulletin_entry")
    op.drop_index("ix_incident_bulletin_entry_source_id", table_name="incident_bulletin_entry")
    op.drop_index("ix_incident_bulletin_entry_episode_id", table_name="incident_bulletin_entry")
    op.drop_index("ix_incident_bulletin_entry_incident_id", table_name="incident_bulletin_entry")
    op.drop_index("ix_incident_bulletin_entry_entry_id", table_name="incident_bulletin_entry")
    op.drop_table("incident_bulletin_entry")
