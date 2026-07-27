"""persist agent orchestration stage traces

Revision ID: c7f4a9d2e610
Revises: b4e8f2a6c730
Create Date: 2026-07-21 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f4a9d2e610"
down_revision: str | None = "b4e8f2a6c730"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def upgrade() -> None:
    op.create_table(
        "agent_stage_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("stage_role", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column(
            "state",
            _enum("succeeded", "failed", "skipped", name="agent_stage_run_state"),
            nullable=False,
        ),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("preflight_payload", sa.JSON(), nullable=False),
        sa.Column("postflight_payload", sa.JSON(), nullable=True),
        sa.Column("attempts_payload", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1 AND sequence <= 10", name="ck_agent_stage_run_sequence"
        ),
        sa.CheckConstraint(
            _sha256_hex_check("contract_digest"),
            name="ck_agent_stage_run_contract_digest",
        ),
        sa.CheckConstraint("length(contract_id) >= 10", name="ck_agent_stage_run_contract_id"),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["agent_dispatch.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("dispatch_id", "stage_role", name="uq_agent_stage_run_role"),
        sa.UniqueConstraint("dispatch_id", "sequence", name="uq_agent_stage_run_sequence"),
    )
    op.create_index(
        "ix_agent_stage_run_dispatch_id",
        "agent_stage_run",
        ["dispatch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_stage_run_dispatch_id", table_name="agent_stage_run")
    op.drop_table("agent_stage_run")
