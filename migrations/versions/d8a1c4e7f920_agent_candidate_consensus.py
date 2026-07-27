"""persist agent model candidates and fail-closed consensus

Revision ID: d8a1c4e7f920
Revises: c7f4a9d2e610
Create Date: 2026-07-22 16:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8a1c4e7f920"
down_revision: str | None = "c7f4a9d2e610"
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
        "agent_model_candidate_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("candidate_rank", sa.Integer(), nullable=False),
        sa.Column("stage_role", sa.String(64), nullable=False),
        sa.Column("model_role", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(512), nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column(
            "state",
            _enum("SUCCEEDED", "FAILED", "SKIPPED", name="agent_model_candidate_run_state"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("load_ms", sa.Integer(), nullable=False),
        sa.Column("inference_ms", sa.Integer(), nullable=False),
        sa.Column("peak_vram_bytes", sa.Integer(), nullable=True),
        sa.Column("repaired", sa.Boolean(), nullable=False),
        sa.Column("output_digest", sa.String(64), nullable=True),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "candidate_rank >= 1 AND candidate_rank <= 8",
            name="ck_agent_model_candidate_run_rank",
        ),
        sa.CheckConstraint("load_ms >= 0", name="ck_agent_model_candidate_run_load_ms"),
        sa.CheckConstraint(
            "inference_ms >= 0", name="ck_agent_model_candidate_run_inference_ms"
        ),
        sa.CheckConstraint(
            "peak_vram_bytes IS NULL OR peak_vram_bytes >= 0",
            name="ck_agent_model_candidate_run_vram",
        ),
        sa.CheckConstraint(
            "output_digest IS NULL OR " + _sha256_hex_check("output_digest"),
            name="ck_agent_model_candidate_run_output_digest",
        ),
        sa.CheckConstraint(
            "(state = 'SUCCEEDED' AND output_digest IS NOT NULL "
            "AND output_payload IS NOT NULL AND error_code IS NULL) OR "
            "(state != 'SUCCEEDED' AND output_digest IS NULL AND output_payload IS NULL)",
            name="ck_agent_model_candidate_run_output",
        ),
        sa.CheckConstraint(
            "repaired = false OR state = 'SUCCEEDED'",
            name="ck_agent_model_candidate_run_repaired",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["agent_dispatch.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "dispatch_id",
            "candidate_id",
            name="uq_agent_model_candidate_run_candidate",
        ),
        sa.UniqueConstraint(
            "dispatch_id",
            "stage_role",
            "candidate_rank",
            name="uq_agent_model_candidate_run_rank",
        ),
    )
    op.create_index(
        "ix_agent_model_candidate_run_dispatch_id",
        "agent_model_candidate_run",
        ["dispatch_id"],
        unique=False,
    )

    op.create_table(
        "agent_consensus_result",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("consensus_id", sa.String(128), nullable=False),
        sa.Column("stage_role", sa.String(64), nullable=False),
        sa.Column(
            "strategy",
            _enum(
                "SINGLE_WITH_RULES",
                "CASCADE",
                "QUORUM",
                name="agent_consensus_strategy",
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            _enum(
                "ACCEPTED",
                "REPAIR",
                "ADJUDICATED",
                "ABSTAIN",
                "HUMAN_REVIEW",
                name="agent_consensus_decision",
            ),
            nullable=False,
        ),
        sa.Column("candidate_ids", sa.JSON(), nullable=False),
        sa.Column("selected_candidate_id", sa.String(128), nullable=True),
        sa.Column("adjudicator_candidate_id", sa.String(128), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("successful_candidates", sa.Integer(), nullable=False),
        sa.Column("required_successful", sa.Integer(), nullable=False),
        sa.Column("agreement_score", sa.Float(), nullable=True),
        sa.Column("agreement_threshold", sa.Float(), nullable=False),
        sa.Column("downstream_allowed", sa.Boolean(), nullable=False),
        sa.Column("comparison_digest", sa.String(64), nullable=False),
        sa.Column("comparison_payload", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "successful_candidates >= 0 AND successful_candidates <= 8",
            name="ck_agent_consensus_successful",
        ),
        sa.CheckConstraint(
            "required_successful >= 1 AND required_successful <= 8",
            name="ck_agent_consensus_required",
        ),
        sa.CheckConstraint(
            "agreement_score IS NULL OR "
            "(agreement_score >= 0 AND agreement_score <= 1)",
            name="ck_agent_consensus_score",
        ),
        sa.CheckConstraint(
            "agreement_threshold >= 0 AND agreement_threshold <= 1",
            name="ck_agent_consensus_threshold",
        ),
        sa.CheckConstraint(
            _sha256_hex_check("comparison_digest"),
            name="ck_agent_consensus_comparison_digest",
        ),
        sa.CheckConstraint(
            "(decision IN ('ACCEPTED', 'REPAIR') AND selected_candidate_id IS NOT NULL "
            "AND adjudicator_candidate_id IS NULL AND downstream_allowed = true) OR "
            "(decision = 'ADJUDICATED' AND selected_candidate_id IS NOT NULL "
            "AND adjudicator_candidate_id IS NOT NULL AND downstream_allowed = true) OR "
            "(decision IN ('ABSTAIN', 'HUMAN_REVIEW') AND selected_candidate_id IS NULL "
            "AND downstream_allowed = false)",
            name="ck_agent_consensus_decision",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["agent_dispatch.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "dispatch_id", "consensus_id", name="uq_agent_consensus_result_id"
        ),
        sa.UniqueConstraint(
            "dispatch_id", "stage_role", name="uq_agent_consensus_result_stage"
        ),
    )
    op.create_index(
        "ix_agent_consensus_result_dispatch_id",
        "agent_consensus_result",
        ["dispatch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_consensus_result_dispatch_id", table_name="agent_consensus_result"
    )
    op.drop_table("agent_consensus_result")
    op.drop_index(
        "ix_agent_model_candidate_run_dispatch_id",
        table_name="agent_model_candidate_run",
    )
    op.drop_table("agent_model_candidate_run")
