"""add qualified incident operational information

Revision ID: f1a4c8e2b630
Revises: e2b8c4d9f730
Create Date: 2026-07-26 17:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a4c8e2b630"
down_revision: str | None = "e2b8c4d9f730"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_operational_information",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("information_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incident_series.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("episode_id", sa.Integer(), sa.ForeignKey("episode.id", ondelete="RESTRICT")),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("value_text", sa.String(length=500)),
        sa.Column("value_number", sa.Float()),
        sa.Column("unit", sa.String(length=64)),
        sa.Column("locality", sa.String(length=255)),
        sa.Column("authority_kind", sa.String(length=16), nullable=False),
        sa.Column("authority_name", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint("kind IN ('affected_place', 'evacuated_people', 'mobilized_personnel', 'mobilized_vehicles', 'road_status', 'access_status', 'shelter', 'public_service', 'utility', 'other')", name="ck_operational_information_kind"),
        sa.CheckConstraint("(value_text IS NOT NULL) OR (value_number IS NOT NULL)", name="ck_operational_information_value"),
        sa.CheckConstraint("value_number IS NULL OR value_number >= 0", name="ck_operational_information_value_number"),
        sa.CheckConstraint("authority_kind IN ('mairie', 'prefecture', 'police')", name="ck_operational_information_authority"),
        sa.CheckConstraint("state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')", name="ck_operational_information_state"),
        sa.CheckConstraint("source_url LIKE 'https://%'", name="ck_operational_information_https"),
        sa.CheckConstraint("source_reference_url LIKE 'https://%'", name="ck_operational_information_source_https"),
        sa.CheckConstraint("version >= 1", name="ck_operational_information_version"),
    )
    op.create_index("ix_incident_operational_information_information_id", "incident_operational_information", ["information_id"], unique=True)
    op.create_index("ix_incident_operational_information_incident_id", "incident_operational_information", ["incident_id"])
    op.create_index("ix_incident_operational_information_episode_id", "incident_operational_information", ["episode_id"])
    op.create_index("ix_incident_operational_information_kind", "incident_operational_information", ["kind"])
    op.create_index("ix_incident_operational_information_state", "incident_operational_information", ["state"])
    op.create_index("ix_operational_information_incident_state", "incident_operational_information", ["incident_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_operational_information_incident_state", table_name="incident_operational_information")
    op.drop_index("ix_incident_operational_information_state", table_name="incident_operational_information")
    op.drop_index("ix_incident_operational_information_kind", table_name="incident_operational_information")
    op.drop_index("ix_incident_operational_information_episode_id", table_name="incident_operational_information")
    op.drop_index("ix_incident_operational_information_incident_id", table_name="incident_operational_information")
    op.drop_index("ix_incident_operational_information_information_id", table_name="incident_operational_information")
    op.drop_table("incident_operational_information")
