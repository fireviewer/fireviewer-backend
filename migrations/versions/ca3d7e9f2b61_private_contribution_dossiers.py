"""add private contribution dossier scheduling and editorial provenance

Revision ID: ca3d7e9f2b61
Revises: e9b2c5d7f140
Create Date: 2026-07-26 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ca3d7e9f2b61"
down_revision: str | None = "e9b2c5d7f140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _incident_gallery_item_table(*, contribution_provenance: bool) -> sa.Table:
    """Describe both gallery shapes for SQLite batch/offline mode."""
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("gallery_item_id", sa.String(length=96), nullable=False, unique=True),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("caption", sa.String(length=1000), nullable=True),
        sa.Column("alt_text", sa.String(length=500), nullable=False),
        sa.Column(
            "media_url",
            sa.String(length=2048),
            nullable=contribution_provenance,
        ),
        sa.Column(
            "media_kind",
            sa.String(length=16),
            nullable=False,
            server_default="image",
        ),
        sa.Column("credit", sa.String(length=255), nullable=True),
        sa.Column("license_label", sa.String(length=255), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default="PROPOSED",
        ),
        sa.Column(
            "source_reference_url",
            sa.String(length=2048),
            nullable=contribution_provenance,
        ),
        sa.Column("proposal_reason", sa.String(length=1000), nullable=False),
        sa.Column("proposed_by", sa.String(length=255), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]
    if contribution_provenance:
        columns.append(sa.Column("source_contribution_id", sa.Integer(), nullable=True))
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "media_kind IN ('image', 'video')",
            name="ck_gallery_item_media_kind",
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'PUBLISHED', 'REJECTED', 'RETIRED')",
            name="ck_gallery_item_state",
        ),
        sa.CheckConstraint(
            (
                "media_url IS NULL OR media_url LIKE 'https://%'"
                if contribution_provenance
                else "media_url LIKE 'https://%'"
            ),
            name="ck_gallery_item_media_https",
        ),
        sa.CheckConstraint(
            (
                "source_reference_url IS NULL OR source_reference_url LIKE 'https://%'"
                if contribution_provenance
                else "source_reference_url LIKE 'https://%'"
            ),
            name="ck_gallery_item_source_https",
        ),
        sa.CheckConstraint("version >= 1", name="ck_gallery_item_version"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
    ]
    if contribution_provenance:
        constraints.extend(
            [
                sa.ForeignKeyConstraint(
                    ["source_contribution_id"],
                    ["public_contribution_submission.id"],
                    name="fk_gallery_item_source_contribution",
                    ondelete="RESTRICT",
                ),
                sa.CheckConstraint(
                    "state != 'PUBLISHED' OR media_url IS NOT NULL",
                    name="ck_gallery_item_published_media",
                ),
                sa.CheckConstraint(
                    "state != 'PUBLISHED' OR source_reference_url IS NOT NULL",
                    name="ck_gallery_item_published_source",
                ),
            ]
        )
    table = sa.Table(
        "incident_gallery_item",
        sa.MetaData(),
        *columns,
        *constraints,
    )
    sa.Index(
        "ix_incident_gallery_item_gallery_item_id",
        table.c.gallery_item_id,
        unique=True,
    )
    sa.Index("ix_incident_gallery_item_incident_id", table.c.incident_id)
    sa.Index("ix_incident_gallery_item_episode_id", table.c.episode_id)
    sa.Index("ix_incident_gallery_item_state", table.c.state)
    sa.Index("ix_gallery_item_incident_state", table.c.incident_id, table.c.state)
    return table


def upgrade() -> None:
    op.create_table(
        "agent_schedule_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("schedule_key", sa.String(length=96), nullable=False, unique=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(length=255)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_schedule_run_next_run_at", "agent_schedule_run", ["next_run_at"])
    op.create_index("ix_agent_schedule_run_lease_until", "agent_schedule_run", ["lease_until"])
    with op.batch_alter_table(
        "incident_gallery_item",
        copy_from=_incident_gallery_item_table(contribution_provenance=False),
    ) as batch_op:
        batch_op.drop_constraint("ck_gallery_item_media_https", type_="check")
        batch_op.drop_constraint("ck_gallery_item_source_https", type_="check")
        batch_op.alter_column("media_url", existing_type=sa.String(length=2048), nullable=True)
        batch_op.alter_column(
            "source_reference_url", existing_type=sa.String(length=2048), nullable=True
        )
        batch_op.add_column(sa.Column("source_contribution_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_gallery_item_source_contribution",
            "public_contribution_submission",
            ["source_contribution_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_gallery_item_media_https", "media_url IS NULL OR media_url LIKE 'https://%'"
        )
        batch_op.create_check_constraint(
            "ck_gallery_item_source_https",
            "source_reference_url IS NULL OR source_reference_url LIKE 'https://%'",
        )
        batch_op.create_check_constraint(
            "ck_gallery_item_published_media", "state != 'PUBLISHED' OR media_url IS NOT NULL"
        )
        batch_op.create_check_constraint(
            "ck_gallery_item_published_source",
            "state != 'PUBLISHED' OR source_reference_url IS NOT NULL",
        )
    op.create_index(
        "ix_incident_gallery_item_source_contribution_id",
        "incident_gallery_item",
        ["source_contribution_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_incident_gallery_item_source_contribution_id", table_name="incident_gallery_item"
    )
    with op.batch_alter_table(
        "incident_gallery_item",
        copy_from=_incident_gallery_item_table(contribution_provenance=True),
    ) as batch_op:
        batch_op.drop_constraint("ck_gallery_item_published_source", type_="check")
        batch_op.drop_constraint("ck_gallery_item_published_media", type_="check")
        batch_op.drop_constraint("ck_gallery_item_source_https", type_="check")
        batch_op.drop_constraint("ck_gallery_item_media_https", type_="check")
        batch_op.drop_constraint("fk_gallery_item_source_contribution", type_="foreignkey")
        batch_op.drop_column("source_contribution_id")
        batch_op.alter_column("media_url", existing_type=sa.String(length=2048), nullable=False)
        batch_op.alter_column(
            "source_reference_url", existing_type=sa.String(length=2048), nullable=False
        )
        batch_op.create_check_constraint(
            "ck_gallery_item_media_https", "media_url LIKE 'https://%'"
        )
        batch_op.create_check_constraint(
            "ck_gallery_item_source_https", "source_reference_url LIKE 'https://%'"
        )
    op.drop_index("ix_agent_schedule_run_lease_until", table_name="agent_schedule_run")
    op.drop_index("ix_agent_schedule_run_next_run_at", table_name="agent_schedule_run")
    op.drop_table("agent_schedule_run")
