"""add an explicit publication gate for daily situation reports

Revision ID: e9b2c5d7f140
Revises: d8a1c4e7f920
Create Date: 2026-07-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9b2c5d7f140"
down_revision: str | None = "d8a1c4e7f920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _agent_situation_report_table(*, publication_fields: bool) -> sa.Table:
    """Describe both report shapes for deterministic SQLite batch SQL."""
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_revision_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_window_id", sa.Integer(), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("episode_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("sections_payload", sa.JSON(), nullable=False),
        sa.Column(
            "review_state",
            sa.Enum(
                "DRAFT",
                "VALIDATED",
                "REJECTED",
                "INVALIDATED",
                name="agent_report_review_state",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("supersedes_report_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]
    if publication_fields:
        columns.extend(
            [
                sa.Column("published_by", sa.String(length=255), nullable=True),
                sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("publication_reason", sa.String(length=500), nullable=True),
            ]
        )
    constraints: list[sa.SchemaItem] = [
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_agent_report_revision_positive",
        ),
        sa.CheckConstraint("length(reason) >= 10", name="ck_agent_report_reason"),
        sa.CheckConstraint(
            "(review_state = 'DRAFT' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(review_state != 'DRAFT' AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL)",
            name="ck_agent_report_review",
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["analysis_window_id", "incident_id", "episode_id"],
            [
                "agent_analysis_window.id",
                "agent_analysis_window.incident_id",
                "agent_analysis_window.episode_id",
            ],
            name="fk_agent_report_analysis_window_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_report_id"],
            ["agent_situation_report_revision.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_window_id",
            "revision",
            name="uq_agent_situation_report_revision",
        ),
    ]
    if publication_fields:
        constraints.append(
            sa.CheckConstraint(
                "(published_at IS NULL AND published_by IS NULL "
                "AND publication_reason IS NULL) OR "
                "(published_at IS NOT NULL AND published_by IS NOT NULL "
                "AND publication_reason IS NOT NULL AND review_state = 'VALIDATED')",
                name="ck_agent_report_publication",
            )
        )
    table = sa.Table(
        "agent_situation_report_revision",
        sa.MetaData(),
        *columns,
        *constraints,
    )
    for name in (
        "analysis_window_id",
        "incident_id",
        "episode_id",
        "review_state",
        "supersedes_report_id",
    ):
        sa.Index(f"ix_agent_situation_report_revision_{name}", table.c[name])
    sa.Index(
        "ix_agent_situation_report_revision_report_revision_id",
        table.c.report_revision_id,
        unique=True,
    )
    if publication_fields:
        sa.Index(
            "ix_agent_situation_report_revision_published_at",
            table.c.published_at,
        )
    return table


def upgrade() -> None:
    with op.batch_alter_table(
        "agent_situation_report_revision",
        copy_from=_agent_situation_report_table(publication_fields=False),
    ) as batch:
        batch.add_column(sa.Column("published_by", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("publication_reason", sa.String(length=500), nullable=True))
        batch.create_index(
            "ix_agent_situation_report_revision_published_at",
            ["published_at"],
            unique=False,
        )
        batch.create_check_constraint(
            "ck_agent_report_publication",
            "(published_at IS NULL AND published_by IS NULL AND publication_reason IS NULL) OR "
            "(published_at IS NOT NULL AND published_by IS NOT NULL "
            "AND publication_reason IS NOT NULL AND review_state = 'VALIDATED')",
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "agent_situation_report_revision",
        copy_from=_agent_situation_report_table(publication_fields=True),
    ) as batch:
        batch.drop_constraint("ck_agent_report_publication", type_="check")
        batch.drop_index("ix_agent_situation_report_revision_published_at")
        batch.drop_column("publication_reason")
        batch.drop_column("published_at")
        batch.drop_column("published_by")
