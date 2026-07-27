"""add CDC v2 evidence, retention and spatial profile metadata

Revision ID: a8c1d4e7f920
Revises: f7a2c3d4e5f6
Create Date: 2026-07-15 18:00:00.000000
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import context, op

revision: str = "a8c1d4e7f920"
down_revision: str | None = "f7a2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Direct ADD COLUMN operations deliberately preserve the custom SQLite triggers
    # attached to episode, observation, spatial_zone_revision and model_asset.
    op.add_column(
        "episode",
        sa.Column(
            "verification_state",
            sa.Enum(
                "UNVERIFIED",
                "PENDING_REVIEW",
                "CORROBORATED",
                "VERIFIED",
                "REJECTED",
                name="episode_verification_state",
                native_enum=False,
            ),
            nullable=False,
            server_default="UNVERIFIED",
        ),
    )
    op.add_column(
        "episode",
        sa.Column(
            "corroborating_source_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column("episode", sa.Column("evidence_basis_at", sa.DateTime(timezone=True)))
    op.add_column("episode", sa.Column("estimated_area_ha", sa.Float()))
    op.add_column(
        "episode",
        sa.Column(
            "evacuation_established", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("episode", sa.Column("evacuation_basis", sa.String(length=1_000)))

    op.add_column(
        "observation",
        sa.Column(
            "public_spatial_mode",
            sa.Enum(
                "WITHHELD",
                "GENERALIZED",
                "EXACT",
                name="evidence_spatial_mode",
                native_enum=False,
            ),
            nullable=False,
            server_default="WITHHELD",
        ),
    )
    op.add_column("observation", sa.Column("raw_purge_due_at", sa.DateTime(timezone=True)))
    op.add_column("observation", sa.Column("raw_purged_at", sa.DateTime(timezone=True)))
    op.add_column(
        "observation", sa.Column("raw_retention_hold_reason", sa.String(length=500))
    )
    op.create_index("ix_observation_raw_purge_due_at", "observation", ["raw_purge_due_at"])

    op.add_column(
        "spatial_zone_revision",
        sa.Column(
            "spatial_profile_version",
            sa.String(length=16),
            nullable=False,
            server_default="1.0",
        ),
    )
    op.add_column("spatial_zone_revision", sa.Column("origin_easting_l93", sa.Float()))
    op.add_column("spatial_zone_revision", sa.Column("origin_northing_l93", sa.Float()))
    op.add_column(
        "spatial_zone_revision", sa.Column("horizontal_crs", sa.String(length=32))
    )
    op.add_column("spatial_zone_revision", sa.Column("vertical_crs", sa.String(length=32)))
    op.add_column("spatial_zone_revision", sa.Column("ground_model", sa.String(length=64)))
    op.add_column("spatial_zone_revision", sa.Column("ground_resolution_m", sa.Float()))
    op.add_column(
        "spatial_zone_revision",
        sa.Column("surface_height_reference", sa.String(length=64)),
    )

    op.add_column("model_asset", sa.Column("purge_after", sa.DateTime(timezone=True)))
    op.add_column("model_asset", sa.Column("purge_requested_at", sa.DateTime(timezone=True)))
    op.add_column("model_asset", sa.Column("purged_at", sa.DateTime(timezone=True)))
    op.add_column("model_asset", sa.Column("retention_hold_reason", sa.String(length=500)))
    op.create_index("ix_model_asset_purge_after", "model_asset", ["purge_after"])

    op.drop_index("ix_admin_local_session_expires", table_name="admin_local_session")
    op.drop_index("ix_admin_local_session_idle", table_name="admin_local_session")
    op.create_index(
        "ix_admin_local_session_expires_at", "admin_local_session", ["expires_at"]
    )
    op.create_index(
        "ix_admin_local_session_idle_expires_at",
        "admin_local_session",
        ["idle_expires_at"],
    )
    op.create_index(
        "ix_admin_local_session_revoked_at", "admin_local_session", ["revoked_at"]
    )
    op.create_index(
        "ix_admin_local_session_session_hash",
        "admin_local_session",
        ["session_hash"],
        unique=True,
    )
    op.drop_index("ix_admin_login_attempt_origin", table_name="admin_login_attempt")
    op.drop_index("ix_admin_login_attempt_when", table_name="admin_login_attempt")
    op.create_index(
        "ix_admin_login_attempt_origin_hash", "admin_login_attempt", ["origin_hash"]
    )
    op.create_index(
        "ix_admin_login_attempt_attempted_at", "admin_login_attempt", ["attempted_at"]
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE episode SET verification_state = 'VERIFIED', "
            "evidence_basis_at = COALESCE(validated_at, updated_at) "
            "WHERE status IN ('ACTIVE_CONFIRMED', 'MONITORING', 'EXTINGUISHED', 'CLOSED') "
            "OR EXISTS (SELECT 1 FROM observation "
            "WHERE observation.attached_episode_id = episode.id "
            "AND observation.verification_state = 'VERIFIED')"
        )
    )
    # Expiry dates depend on each persisted asset timestamp.  They are applied
    # during every real migration; offline SQL cannot inspect those rows.
    if context.is_offline_mode():
        return
    asset_rows = bind.execute(
        sa.text(
            "SELECT id, generated_at FROM model_asset "
            "WHERE state IN ('GENERATED', 'VALIDATED', 'QUARANTINED')"
        )
    ).all()
    for asset_id, generated_at in asset_rows:
        if generated_at is not None:
            generated_datetime = (
                datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                if isinstance(generated_at, str)
                else generated_at
            )
            bind.execute(
                sa.text("UPDATE model_asset SET purge_after = :purge_after WHERE id = :id"),
                {"purge_after": generated_datetime + timedelta(days=30), "id": asset_id},
            )


def downgrade() -> None:
    op.drop_index("ix_admin_login_attempt_attempted_at", table_name="admin_login_attempt")
    op.drop_index("ix_admin_login_attempt_origin_hash", table_name="admin_login_attempt")
    op.create_index("ix_admin_login_attempt_when", "admin_login_attempt", ["attempted_at"])
    op.create_index("ix_admin_login_attempt_origin", "admin_login_attempt", ["origin_hash"])
    op.drop_index("ix_admin_local_session_session_hash", table_name="admin_local_session")
    op.drop_index("ix_admin_local_session_revoked_at", table_name="admin_local_session")
    op.drop_index("ix_admin_local_session_idle_expires_at", table_name="admin_local_session")
    op.drop_index("ix_admin_local_session_expires_at", table_name="admin_local_session")
    op.create_index("ix_admin_local_session_idle", "admin_local_session", ["idle_expires_at"])
    op.create_index("ix_admin_local_session_expires", "admin_local_session", ["expires_at"])

    op.drop_index("ix_model_asset_purge_after", table_name="model_asset")
    op.drop_column("model_asset", "retention_hold_reason")
    op.drop_column("model_asset", "purged_at")
    op.drop_column("model_asset", "purge_requested_at")
    op.drop_column("model_asset", "purge_after")

    op.drop_column("spatial_zone_revision", "surface_height_reference")
    op.drop_column("spatial_zone_revision", "ground_resolution_m")
    op.drop_column("spatial_zone_revision", "ground_model")
    op.drop_column("spatial_zone_revision", "vertical_crs")
    op.drop_column("spatial_zone_revision", "horizontal_crs")
    op.drop_column("spatial_zone_revision", "origin_northing_l93")
    op.drop_column("spatial_zone_revision", "origin_easting_l93")
    op.drop_column("spatial_zone_revision", "spatial_profile_version")

    op.drop_index("ix_observation_raw_purge_due_at", table_name="observation")
    op.drop_column("observation", "raw_retention_hold_reason")
    op.drop_column("observation", "raw_purged_at")
    op.drop_column("observation", "raw_purge_due_at")
    op.drop_column("observation", "public_spatial_mode")

    op.drop_column("episode", "evacuation_basis")
    op.drop_column("episode", "evacuation_established")
    op.drop_column("episode", "estimated_area_ha")
    op.drop_column("episode", "evidence_basis_at")
    op.drop_column("episode", "corroborating_source_count")
    op.drop_column("episode", "verification_state")
