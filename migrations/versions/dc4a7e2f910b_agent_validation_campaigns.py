"""add internal ordered agent validation campaigns

Revision ID: dc4a7e2f910b
Revises: db7c2e4f9a10
Create Date: 2026-07-28 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dc4a7e2f910b"
down_revision: str | None = "db7c2e4f9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def upgrade() -> None:
    op.create_table(
        "agent_validation_campaign",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.String(length=128), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _sha256_hex_check("manifest_sha256"),
            name="ck_agent_validation_campaign_manifest_hash",
        ),
        sa.CheckConstraint("version >= 1", name="ck_agent_validation_campaign_version"),
        sa.UniqueConstraint(
            "manifest_sha256",
            name="uq_agent_validation_campaign_manifest",
        ),
        sa.UniqueConstraint("campaign_id"),
    )
    op.create_index(
        "ix_agent_validation_campaign_campaign_id",
        "agent_validation_campaign",
        ["campaign_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_validation_campaign_is_active",
        "agent_validation_campaign",
        ["is_active"],
    )
    op.create_index(
        "uq_agent_validation_campaign_active",
        "agent_validation_campaign",
        ["is_active"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "agent_validation_campaign_day",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_day_id", sa.String(length=128), nullable=False),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("agent_validation_campaign.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analysis_window_id",
            sa.Integer(),
            sa.ForeignKey("agent_analysis_window.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("allowed_media_sha256", sa.JSON(), nullable=False),
        sa.Column("required_operations", sa.JSON(), nullable=False),
        sa.Column("declared_absences", sa.JSON(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "LOCKED",
                "READY",
                "RUNNING",
                "REVIEW",
                "PUBLISHED",
                "FAILED",
                name="agent_validation_campaign_day_state",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_agent_validation_campaign_day_ordinal"),
        sa.CheckConstraint(
            _sha256_hex_check("manifest_sha256"),
            name="ck_agent_validation_campaign_day_manifest_hash",
        ),
        sa.CheckConstraint(
            "state IN ('LOCKED', 'READY', 'RUNNING', 'REVIEW', 'PUBLISHED', 'FAILED')",
            name="ck_agent_validation_campaign_day_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_agent_validation_campaign_day_version"),
        sa.UniqueConstraint(
            "campaign_id",
            "ordinal",
            name="uq_agent_validation_campaign_day_order",
        ),
        sa.UniqueConstraint("campaign_day_id"),
        sa.UniqueConstraint("analysis_window_id"),
    )
    op.create_index(
        "ix_agent_validation_campaign_day_campaign_day_id",
        "agent_validation_campaign_day",
        ["campaign_day_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_validation_campaign_day_campaign_id",
        "agent_validation_campaign_day",
        ["campaign_id"],
    )
    op.create_index(
        "ix_agent_validation_campaign_day_analysis_window_id",
        "agent_validation_campaign_day",
        ["analysis_window_id"],
        unique=True,
    )
    op.create_index(
        "ix_agent_validation_campaign_day_state",
        "agent_validation_campaign_day",
        ["state"],
    )


def downgrade() -> None:
    op.drop_table("agent_validation_campaign_day")
    op.drop_table("agent_validation_campaign")
