"""add dedicated agent media, consent, dispatch and dead-letter persistence

Revision ID: f3b8c1d7a920
Revises: e6f3a1b8c420
Create Date: 2026-07-16 05:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3b8c1d7a920"
down_revision = "e6f3a1b8c420"
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def _sha256_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def upgrade() -> None:
    op.create_table(
        "agent_media_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "batch_type",
            _enum(
                "user_media",
                "external_media",
                "satellite_media",
                name="agent_batch_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "priority",
            _enum(
                "user_deadline",
                "scheduled_combined",
                "scheduled",
                name="agent_batch_priority",
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum(
                "DRAFT",
                "QUEUED",
                "SUBMITTING",
                "RUNNING",
                "SUCCEEDED",
                "PARTIAL_FAILURE",
                "FAILED",
                "DEAD_LETTER",
                "CANCEL_REQUESTED",
                "CANCELLED",
                name="agent_batch_state",
            ),
            nullable=False,
        ),
        sa.Column("incident_id", sa.Integer(), nullable=True),
        sa.Column("episode_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = '1.0'", name="ck_agent_batch_schema_version"),
        sa.CheckConstraint(
            "(incident_id IS NULL AND episode_id IS NULL) OR "
            "(incident_id IS NOT NULL AND episode_id IS NOT NULL)",
            name="ck_agent_batch_incident_episode_pair",
        ),
        sa.CheckConstraint(_sha256_check("request_hash"), name="ck_agent_batch_request_hash"),
        sa.CheckConstraint(
            "payload_hash IS NULL OR (" + _sha256_check("payload_hash") + ")",
            name="ck_agent_batch_payload_hash",
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episode.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_media_batch_idempotency_key"),
    )
    op.create_index("ix_agent_media_batch_batch_id", "agent_media_batch", ["batch_id"], unique=True)
    op.create_index("ix_agent_media_batch_state", "agent_media_batch", ["state"])
    op.create_index("ix_agent_media_batch_incident_id", "agent_media_batch", ["incident_id"])
    op.create_index("ix_agent_media_batch_episode_id", "agent_media_batch", ["episode_id"])
    op.create_index("ix_agent_media_batch_trace_id", "agent_media_batch", ["trace_id"])
    op.create_index("ix_agent_media_batch_deadline_at", "agent_media_batch", ["deadline_at"])
    op.create_index("ix_agent_media_batch_purge_after", "agent_media_batch", ["purge_after"])

    op.create_table(
        "agent_media_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("input_id", sa.String(length=128), nullable=False),
        sa.Column(
            "media_type",
            _enum(
                "image",
                "video",
                "audio",
                "article",
                "satellite_image",
                name="agent_media_type",
            ),
            nullable=False,
        ),
        sa.Column("working_file_url", sa.String(length=2048), nullable=True),
        sa.Column("media_sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("metadata_payload", sa.JSON(), nullable=False),
        sa.Column("processable_payload", sa.JSON(), nullable=False),
        sa.Column("preprocessing_status", sa.String(length=32), nullable=False),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "media_sha256 IS NULL OR (" + _sha256_check("media_sha256") + ")",
            name="ck_agent_media_item_hash",
        ),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes > 0", name="ck_agent_media_item_size"),
        sa.CheckConstraint(
            "working_file_url IS NULL OR working_file_url LIKE 'https://%'",
            name="ck_agent_media_item_https",
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["agent_media_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "input_id", name="uq_agent_media_item_batch_input"),
    )
    op.create_index("ix_agent_media_item_batch_id", "agent_media_item", ["batch_id"])
    op.create_index("ix_agent_media_item_purge_after", "agent_media_item", ["purge_after"])

    op.create_table(
        "agent_media_consent",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column(
            "basis",
            _enum(
                "explicit_upload",
                "source_license",
                "institutional_mandate",
                name="agent_consent_basis",
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum("GRANTED", "WITHDRAWN", "EXPIRED", name="agent_consent_state"),
            nullable=False,
        ),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("terms_version", sa.String(length=64), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("subject_reference_hash", sa.String(length=64), nullable=True),
        sa.Column("source_reference_url", sa.String(length=2048), nullable=True),
        sa.Column("license_identifier", sa.String(length=128), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawal_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_check("evidence_sha256"), name="ck_agent_consent_evidence_hash"),
        sa.CheckConstraint(
            "subject_reference_hash IS NULL OR (" + _sha256_check("subject_reference_hash") + ")",
            name="ck_agent_consent_subject_hash",
        ),
        sa.CheckConstraint(
            "basis != 'source_license' OR "
            "(source_reference_url IS NOT NULL AND license_identifier IS NOT NULL)",
            name="ck_agent_consent_source_license",
        ),
        sa.CheckConstraint(
            "source_reference_url IS NULL OR source_reference_url LIKE 'https://%'",
            name="ck_agent_consent_reference_https",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["agent_media_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_agent_media_consent_item_id"),
    )
    op.create_index("ix_agent_media_consent_state", "agent_media_consent", ["state"])
    op.create_index("ix_agent_media_consent_expires_at", "agent_media_consent", ["expires_at"])

    op.create_table(
        "agent_dispatch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dispatch_id", sa.String(length=128), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            _enum(
                "QUEUED",
                "SUBMITTING",
                "AWAITING_REMOTE",
                "POLL_WAIT",
                "SUCCEEDED",
                "PARTIAL_FAILURE",
                "FAILED",
                "DEAD_LETTER",
                "CANCEL_REQUESTED",
                "CANCELLED",
                name="agent_dispatch_state",
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_models", sa.JSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("poll_count", sa.Integer(), nullable=False),
        sa.Column("remote_job_id", sa.String(length=255), nullable=True),
        sa.Column("remote_status", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_ms", sa.Integer(), nullable=True),
        sa.Column("delay_ms", sa.Integer(), nullable=True),
        sa.Column("raw_output", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_detail", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_check("payload_hash"), name="ck_agent_dispatch_payload_hash"),
        sa.CheckConstraint("attempt >= 0", name="ck_agent_dispatch_attempt"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_agent_dispatch_max_attempts"),
        sa.CheckConstraint("poll_count >= 0", name="ck_agent_dispatch_poll_count"),
        sa.CheckConstraint(
            "execution_ms IS NULL OR execution_ms >= 0", name="ck_agent_dispatch_execution_ms"
        ),
        sa.CheckConstraint("delay_ms IS NULL OR delay_ms >= 0", name="ck_agent_dispatch_delay_ms"),
        sa.ForeignKeyConstraint(["batch_id"], ["agent_media_batch.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", name="uq_agent_dispatch_batch_id"),
        sa.UniqueConstraint("remote_job_id", name="uq_agent_dispatch_remote_job_id"),
    )
    op.create_index("ix_agent_dispatch_dispatch_id", "agent_dispatch", ["dispatch_id"], unique=True)
    op.create_index("ix_agent_dispatch_state", "agent_dispatch", ["state"])
    op.create_index("ix_agent_dispatch_lease_until", "agent_dispatch", ["lease_until"])
    op.create_index("ix_agent_dispatch_next_attempt_at", "agent_dispatch", ["next_attempt_at"])
    op.create_index("ix_agent_dispatch_deadline_at", "agent_dispatch", ["deadline_at"])
    op.create_index(
        "ix_agent_dispatch_claim",
        "agent_dispatch",
        ["state", "next_attempt_at", "lease_until"],
    )

    op.create_table(
        "agent_model_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("model_role", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=512), nullable=False),
        sa.Column("revision", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            _enum("succeeded", "failed", "skipped", name="agent_model_run_state"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("load_ms", sa.Integer(), nullable=False),
        sa.Column("inference_ms", sa.Integer(), nullable=False),
        sa.Column("peak_vram_bytes", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.CheckConstraint("load_ms >= 0", name="ck_agent_model_run_load_ms"),
        sa.CheckConstraint("inference_ms >= 0", name="ck_agent_model_run_inference_ms"),
        sa.CheckConstraint(
            "peak_vram_bytes IS NULL OR peak_vram_bytes >= 0",
            name="ck_agent_model_run_vram",
        ),
        sa.ForeignKeyConstraint(["dispatch_id"], ["agent_dispatch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dispatch_id", "model_role", name="uq_agent_model_run_role"),
    )
    op.create_index("ix_agent_model_run_dispatch_id", "agent_model_run", ["dispatch_id"])

    op.create_table(
        "agent_dead_letter",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dead_letter_id", sa.String(length=128), nullable=False),
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            _enum("OPEN", "ACKNOWLEDGED", "REPLAYED", name="agent_dead_letter_state"),
            nullable=False,
        ),
        sa.Column("failure_class", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=False),
        sa.Column("error_detail", sa.String(length=1000), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("remote_job_id", sa.String(length=255), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sha256_check("payload_hash"), name="ck_agent_dead_letter_payload_hash"),
        sa.ForeignKeyConstraint(["dispatch_id"], ["agent_dispatch.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dispatch_id", name="uq_agent_dead_letter_dispatch_id"),
    )
    op.create_index(
        "ix_agent_dead_letter_dead_letter_id",
        "agent_dead_letter",
        ["dead_letter_id"],
        unique=True,
    )
    op.create_index("ix_agent_dead_letter_state", "agent_dead_letter", ["state"])

    op.create_table(
        "agent_review_task",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("review_id", sa.String(length=128), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            _enum(
                "PENDING",
                "IN_REVIEW",
                "RESOLVED",
                "REJECTED",
                name="agent_review_state",
            ),
            nullable=False,
        ),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("assigned_to", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["agent_media_batch.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", name="uq_agent_review_task_batch_id"),
    )
    op.create_index(
        "ix_agent_review_task_review_id", "agent_review_task", ["review_id"], unique=True
    )
    op.create_index("ix_agent_review_task_state", "agent_review_task", ["state"])


def downgrade() -> None:
    op.drop_index("ix_agent_review_task_state", table_name="agent_review_task")
    op.drop_index("ix_agent_review_task_review_id", table_name="agent_review_task")
    op.drop_table("agent_review_task")
    op.drop_index("ix_agent_dead_letter_state", table_name="agent_dead_letter")
    op.drop_index("ix_agent_dead_letter_dead_letter_id", table_name="agent_dead_letter")
    op.drop_table("agent_dead_letter")
    op.drop_index("ix_agent_model_run_dispatch_id", table_name="agent_model_run")
    op.drop_table("agent_model_run")
    op.drop_index("ix_agent_dispatch_claim", table_name="agent_dispatch")
    op.drop_index("ix_agent_dispatch_deadline_at", table_name="agent_dispatch")
    op.drop_index("ix_agent_dispatch_next_attempt_at", table_name="agent_dispatch")
    op.drop_index("ix_agent_dispatch_lease_until", table_name="agent_dispatch")
    op.drop_index("ix_agent_dispatch_state", table_name="agent_dispatch")
    op.drop_index("ix_agent_dispatch_dispatch_id", table_name="agent_dispatch")
    op.drop_table("agent_dispatch")
    op.drop_index("ix_agent_media_consent_expires_at", table_name="agent_media_consent")
    op.drop_index("ix_agent_media_consent_state", table_name="agent_media_consent")
    op.drop_table("agent_media_consent")
    op.drop_index("ix_agent_media_item_purge_after", table_name="agent_media_item")
    op.drop_index("ix_agent_media_item_batch_id", table_name="agent_media_item")
    op.drop_table("agent_media_item")
    op.drop_index("ix_agent_media_batch_purge_after", table_name="agent_media_batch")
    op.drop_index("ix_agent_media_batch_deadline_at", table_name="agent_media_batch")
    op.drop_index("ix_agent_media_batch_trace_id", table_name="agent_media_batch")
    op.drop_index("ix_agent_media_batch_episode_id", table_name="agent_media_batch")
    op.drop_index("ix_agent_media_batch_incident_id", table_name="agent_media_batch")
    op.drop_index("ix_agent_media_batch_state", table_name="agent_media_batch")
    op.drop_index("ix_agent_media_batch_batch_id", table_name="agent_media_batch")
    op.drop_table("agent_media_batch")
