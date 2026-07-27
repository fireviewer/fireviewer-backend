"""add zone publication lifecycle state machine

Revision ID: a3c9e5d7b620
Revises: f2a8d7c9b410
Create Date: 2026-07-14 18:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e5d7b620"
down_revision: str | None = "f2a8d7c9b410"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLICATION_STATES = (
    "DRAFT",
    "VERIFIED",
    "PREVIEWABLE",
    "PUBLISHED",
    "WITHDRAWN",
    "REVOKED",
    "ARCHIVED",
)


def _state_enum(name: str) -> sa.Enum:
    return sa.Enum(*PUBLICATION_STATES, name=name, native_enum=False, validate_strings=True)


def _create_sqlite_triggers() -> None:
    op.execute(
        "CREATE TRIGGER zone_publication_event_no_update "
        "BEFORE UPDATE ON zone_publication_event "
        "BEGIN SELECT RAISE(ABORT, 'zone publication events are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER zone_publication_event_no_delete "
        "BEFORE DELETE ON zone_publication_event "
        "BEGIN SELECT RAISE(ABORT, 'zone publication events are append-only'); END"
    )


def _drop_sqlite_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS zone_publication_event_no_delete")
    op.execute("DROP TRIGGER IF EXISTS zone_publication_event_no_update")


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "zone_publication",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("publication_id", sa.String(length=96), nullable=False),
        sa.Column("spatial_zone_id", sa.Integer(), nullable=False),
        sa.Column("spatial_zone_revision_id", sa.Integer(), nullable=False),
        sa.Column("spatial_package_id", sa.Integer(), nullable=False),
        sa.Column("state", _state_enum("zone_publication_state"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(is_active AND state = 'PUBLISHED') OR (NOT is_active AND state != 'PUBLISHED')",
            name="ck_zone_publication_active_state",
        ),
        sa.ForeignKeyConstraint(
            ["spatial_package_id"], ["spatial_package.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["spatial_zone_id"], ["spatial_zone.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["spatial_zone_revision_id"], ["spatial_zone_revision.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publication_id"),
    )
    op.create_index(
        "uq_zone_publication_one_active",
        "zone_publication",
        ["spatial_zone_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        op.f("ix_zone_publication_publication_id"),
        "zone_publication",
        ["publication_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_zone_publication_spatial_package_id"), "zone_publication", ["spatial_package_id"]
    )
    op.create_index(
        op.f("ix_zone_publication_spatial_zone_id"), "zone_publication", ["spatial_zone_id"]
    )
    op.create_index(
        op.f("ix_zone_publication_spatial_zone_revision_id"),
        "zone_publication",
        ["spatial_zone_revision_id"],
    )
    op.create_index(op.f("ix_zone_publication_state"), "zone_publication", ["state"])
    op.create_table(
        "zone_publication_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=96), nullable=False),
        sa.Column("zone_publication_id", sa.Integer(), nullable=False),
        sa.Column("from_state", _state_enum("zone_publication_from_state"), nullable=True),
        sa.Column("to_state", _state_enum("zone_publication_to_state"), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["zone_publication_id"], ["zone_publication.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_zone_publication_event_event_id"),
        "zone_publication_event",
        ["event_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_zone_publication_event_zone_publication_id"),
        "zone_publication_event",
        ["zone_publication_id"],
    )
    if bind.dialect.name == "sqlite":
        _create_sqlite_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _drop_sqlite_triggers()
    op.drop_index(
        op.f("ix_zone_publication_event_zone_publication_id"), table_name="zone_publication_event"
    )
    op.drop_index(op.f("ix_zone_publication_event_event_id"), table_name="zone_publication_event")
    op.drop_table("zone_publication_event")
    op.drop_index(op.f("ix_zone_publication_state"), table_name="zone_publication")
    op.drop_index(
        op.f("ix_zone_publication_spatial_zone_revision_id"), table_name="zone_publication"
    )
    op.drop_index(op.f("ix_zone_publication_spatial_zone_id"), table_name="zone_publication")
    op.drop_index(op.f("ix_zone_publication_spatial_package_id"), table_name="zone_publication")
    op.drop_index(op.f("ix_zone_publication_publication_id"), table_name="zone_publication")
    op.drop_index("uq_zone_publication_one_active", table_name="zone_publication")
    op.drop_table("zone_publication")
