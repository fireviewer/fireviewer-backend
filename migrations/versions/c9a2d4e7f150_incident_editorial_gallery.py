"""add separately reviewed incident editorial gallery

Revision ID: c9a2d4e7f150
Revises: f1a4c8e2b630
Create Date: 2026-07-26 17:52:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a2d4e7f150"
down_revision: str | None = "f1a4c8e2b630"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_gallery_item",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("gallery_item_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incident_series.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("episode_id", sa.Integer(), sa.ForeignKey("episode.id", ondelete="RESTRICT")),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("caption", sa.String(length=1000)),
        sa.Column("alt_text", sa.String(length=500), nullable=False),
        sa.Column("media_url", sa.String(length=2048), nullable=False),
        sa.Column("media_kind", sa.String(length=16), nullable=False, server_default="image"),
        sa.Column("credit", sa.String(length=255)),
        sa.Column("license_label", sa.String(length=255)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint("media_kind IN ('image', 'video')", name="ck_gallery_item_media_kind"),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')",
            name="ck_gallery_item_state",
        ),
        sa.CheckConstraint("media_url LIKE 'https://%'", name="ck_gallery_item_media_https"),
        sa.CheckConstraint(
            "source_reference_url LIKE 'https://%'", name="ck_gallery_item_source_https"
        ),
        sa.CheckConstraint("version >= 1", name="ck_gallery_item_version"),
    )
    op.create_index(
        "ix_incident_gallery_item_gallery_item_id",
        "incident_gallery_item",
        ["gallery_item_id"],
        unique=True,
    )
    op.create_index(
        "ix_incident_gallery_item_incident_id",
        "incident_gallery_item",
        ["incident_id"],
    )
    op.create_index(
        "ix_incident_gallery_item_episode_id",
        "incident_gallery_item",
        ["episode_id"],
    )
    op.create_index("ix_incident_gallery_item_state", "incident_gallery_item", ["state"])
    op.create_index(
        "ix_gallery_item_incident_state", "incident_gallery_item", ["incident_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_gallery_item_incident_state", table_name="incident_gallery_item")
    op.drop_index("ix_incident_gallery_item_state", table_name="incident_gallery_item")
    op.drop_index("ix_incident_gallery_item_episode_id", table_name="incident_gallery_item")
    op.drop_index("ix_incident_gallery_item_incident_id", table_name="incident_gallery_item")
    op.drop_index("ix_incident_gallery_item_gallery_item_id", table_name="incident_gallery_item")
    op.drop_table("incident_gallery_item")
