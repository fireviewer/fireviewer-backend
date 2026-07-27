"""add reviewed official incident resources

Revision ID: e2b8c4d9f730
Revises: e1c7a9b4d620
Create Date: 2026-07-26 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b8c4d9f730"
down_revision: str | None = "e1c7a9b4d620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_official_resource",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("episode_id", sa.Integer(), sa.ForeignKey("episode.id", ondelete="RESTRICT")),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("publisher", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="PROPOSED"),
        sa.Column("source_reference_url", sa.String(length=2048), nullable=False),
        sa.Column("proposal_reason", sa.String(length=1000), nullable=False),
        sa.Column("proposed_by", sa.String(length=255), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_reason", sa.String(length=500)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('safety', 'press', 'official_update', 'authority')",
            name="ck_official_resource_kind",
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')",
            name="ck_official_resource_state",
        ),
        sa.CheckConstraint("url LIKE 'https://%'", name="ck_official_resource_https"),
        sa.CheckConstraint(
            "source_reference_url LIKE 'https://%'", name="ck_official_resource_source_https"
        ),
        sa.CheckConstraint("version >= 1", name="ck_official_resource_version"),
    )
    op.create_index(
        "ix_incident_official_resource_resource_id",
        "incident_official_resource",
        ["resource_id"],
        unique=True,
    )
    op.create_index(
        "ix_incident_official_resource_incident_id",
        "incident_official_resource",
        ["incident_id"],
    )
    op.create_index(
        "ix_incident_official_resource_episode_id",
        "incident_official_resource",
        ["episode_id"],
    )
    op.create_index("ix_incident_official_resource_kind", "incident_official_resource", ["kind"])
    op.create_index("ix_incident_official_resource_state", "incident_official_resource", ["state"])
    op.create_index(
        "ix_official_resource_incident_state",
        "incident_official_resource",
        ["incident_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_official_resource_incident_state", table_name="incident_official_resource")
    op.drop_index("ix_incident_official_resource_state", table_name="incident_official_resource")
    op.drop_index("ix_incident_official_resource_kind", table_name="incident_official_resource")
    op.drop_index(
        "ix_incident_official_resource_episode_id", table_name="incident_official_resource"
    )
    op.drop_index(
        "ix_incident_official_resource_incident_id", table_name="incident_official_resource"
    )
    op.drop_index(
        "ix_incident_official_resource_resource_id", table_name="incident_official_resource"
    )
    op.drop_table("incident_official_resource")
