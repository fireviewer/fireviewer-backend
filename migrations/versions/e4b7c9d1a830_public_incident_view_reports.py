"""add public incident projection metadata and anonymous reports

Revision ID: e4b7c9d1a830
Revises: d9e4c2a8f610
Create Date: 2026-07-15 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b7c9d1a830"
down_revision: str | None = "d9e4c2a8f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, validate_strings=True)


def _sha256_hex_check(column: str) -> str:
    remaining = column
    for character in "0123456789abcdef":
        remaining = f"replace({remaining}, '{character}', '')"
    return f"length({column}) = 64 AND length({remaining}) = 0"


def upgrade() -> None:
    op.add_column("source", sa.Column("public_display_name", sa.String(length=255), nullable=True))
    op.add_column("source", sa.Column("public_license", sa.String(length=255), nullable=True))
    op.add_column("source", sa.Column("public_reference_url", sa.String(length=2048), nullable=True))
    op.add_column(
        "source",
        sa.Column("public_transformations", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.create_table(
        "incident_public_report",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.String(length=96), nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column(
            "category",
            _enum("public_report_category", ("information_obsolete", "location", "source", "privacy", "accessibility")),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("origin_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("submitted_day", sa.String(length=10), nullable=False),
        sa.Column(
            "state",
            _enum("public_report_state", ("PENDING", "CORRECTED", "REJECTED")),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("closure_reason", sa.String(length=500), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("version >= 1", name="ck_public_report_version"),
        sa.CheckConstraint(_sha256_hex_check("origin_fingerprint"), name="ck_public_report_origin_hash"),
        sa.CheckConstraint(_sha256_hex_check("content_hash"), name="ck_public_report_content_hash"),
        sa.ForeignKeyConstraint(["incident_id"], ["incident_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id"),
        sa.UniqueConstraint("incident_id", "origin_fingerprint", "content_hash", "submitted_day", name="uq_public_report_origin_content_day"),
    )
    op.create_index(op.f("ix_incident_public_report_report_id"), "incident_public_report", ["report_id"], unique=True)
    op.create_index(op.f("ix_incident_public_report_incident_id"), "incident_public_report", ["incident_id"])
    op.create_index(op.f("ix_incident_public_report_submitted_day"), "incident_public_report", ["submitted_day"])
    op.create_index(op.f("ix_incident_public_report_state"), "incident_public_report", ["state"])
    op.create_index("ix_public_report_origin_day", "incident_public_report", ["origin_fingerprint", "submitted_day"])


def downgrade() -> None:
    op.drop_index("ix_public_report_origin_day", table_name="incident_public_report")
    op.drop_index(op.f("ix_incident_public_report_state"), table_name="incident_public_report")
    op.drop_index(op.f("ix_incident_public_report_submitted_day"), table_name="incident_public_report")
    op.drop_index(op.f("ix_incident_public_report_incident_id"), table_name="incident_public_report")
    op.drop_index(op.f("ix_incident_public_report_report_id"), table_name="incident_public_report")
    op.drop_table("incident_public_report")
    op.drop_column("source", "public_transformations")
    op.drop_column("source", "public_reference_url")
    op.drop_column("source", "public_license")
    op.drop_column("source", "public_display_name")
