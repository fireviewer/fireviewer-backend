"""harden admin zone registry persistence guards

Revision ID: b4d1e6f9a730
Revises: a3c9e5d7b620
Create Date: 2026-07-14 21:20:00.000000
"""

# ruff: noqa: S608

from collections.abc import Sequence

from alembic import op

revision: str = "b4d1e6f9a730"
down_revision: str | None = "a3c9e5d7b620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def _transition_predicate(from_state: str, to_state: str) -> str:
    return (
        f"({from_state} = 'DRAFT' AND {to_state} = 'VERIFIED') "
        f"OR ({from_state} = 'VERIFIED' AND {to_state} IN ('PREVIEWABLE', 'REVOKED', 'ARCHIVED')) "
        f"OR ({from_state} = 'PREVIEWABLE' AND {to_state} IN ('PUBLISHED', 'REVOKED', 'ARCHIVED')) "
        f"OR ({from_state} = 'PUBLISHED' AND {to_state} IN ('WITHDRAWN', 'REVOKED', 'ARCHIVED')) "
        f"OR ({from_state} = 'WITHDRAWN' AND {to_state} IN ('PUBLISHED', 'ARCHIVED')) "
        f"OR ({from_state} = 'REVOKED' AND {to_state} = 'ARCHIVED')"
    )


_PACKAGE_STATE_TRANSITION = _transition_predicate("OLD.state", "NEW.state")
_PUBLICATION_STATE_TRANSITION = _transition_predicate("OLD.state", "NEW.state")
_EVENT_STATE_TRANSITION = _transition_predicate("NEW.from_state", "NEW.to_state")

_LEGACY_SPATIAL_PACKAGE_IDENTITY_TRIGGER = (
    "CREATE TRIGGER spatial_package_identity_immutable "
    "BEFORE UPDATE OF package_id, manifest_uri, manifest_sha256, manifest_size_bytes, "
    "storage_uri, provenance ON spatial_package "
    "BEGIN SELECT RAISE(ABORT, 'spatial package identity is immutable'); END"
)


def _sqlite_trigger_statements() -> dict[str, str]:
    return {
        "spatial_package_identity_immutable": (
            "CREATE TRIGGER spatial_package_identity_immutable "
            "BEFORE UPDATE OF package_id, manifest_uri, manifest_sha256, manifest_size_bytes, "
            "storage_uri, provenance, created_by, created_at ON spatial_package "
            "BEGIN SELECT RAISE(ABORT, 'spatial package identity is immutable'); END"
        ),
        "spatial_package_initial_state_draft": (
            "CREATE TRIGGER spatial_package_initial_state_draft "
            "BEFORE INSERT ON spatial_package "
            "WHEN NEW.state <> 'DRAFT' "
            "OR NEW.spatial_zone_revision_id IS NOT NULL "
            "OR NEW.verified_at IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT, 'spatial package must start as draft'); END"
        ),
        "spatial_package_revision_link_once": (
            "CREATE TRIGGER spatial_package_revision_link_once "
            "BEFORE UPDATE OF spatial_zone_revision_id ON spatial_package "
            "WHEN OLD.spatial_zone_revision_id IS NOT NULL "
            "AND NEW.spatial_zone_revision_id IS NOT OLD.spatial_zone_revision_id "
            "BEGIN SELECT RAISE(ABORT, 'spatial package zone revision is immutable once attached'); END"
        ),
        "spatial_package_verification_immutable": (
            "CREATE TRIGGER spatial_package_verification_immutable "
            "BEFORE UPDATE OF verification_report, verified_at ON spatial_package "
            "WHEN OLD.verified_at IS NOT NULL "
            "AND (NEW.verification_report IS NOT OLD.verification_report "
            "OR NEW.verified_at IS NOT OLD.verified_at) "
            "BEGIN SELECT RAISE(ABORT, 'spatial package verification is immutable'); END"
        ),
        "spatial_package_verification_requires_transition": (
            "CREATE TRIGGER spatial_package_verification_requires_transition "
            "BEFORE UPDATE OF verification_report, verified_at ON spatial_package "
            "WHEN OLD.state = 'DRAFT' AND NEW.state = 'DRAFT' "
            "AND NEW.verified_at IS NOT NULL "
            "BEGIN SELECT RAISE(ABORT, 'spatial package verification requires draft to verified transition'); END"
        ),
        "spatial_package_state_transition": (
            "CREATE TRIGGER spatial_package_state_transition "
            "BEFORE UPDATE OF state ON spatial_package "
            "WHEN NEW.state IS NOT OLD.state "
            "BEGIN "
            f"SELECT CASE WHEN NOT ({_PACKAGE_STATE_TRANSITION}) "
            "THEN RAISE(ABORT, 'invalid spatial package transition') END; "
            "SELECT CASE WHEN NEW.state = 'VERIFIED' AND NEW.verified_at IS NULL "
            "THEN RAISE(ABORT, 'verified spatial package requires verified_at') END; "
            "SELECT CASE WHEN NEW.state = 'VERIFIED' AND ("
            "json_valid(NEW.verification_report) <> 1 "
            "OR json_type(NEW.verification_report) <> 'object' "
            "OR COALESCE(json_extract(NEW.verification_report, '$.status'), '') <> 'passed') "
            "THEN RAISE(ABORT, 'verified spatial package requires passed verification report') END; "
            "SELECT CASE WHEN NEW.state IN ('PREVIEWABLE', 'PUBLISHED') "
            "AND NEW.spatial_zone_revision_id IS NULL "
            "THEN RAISE(ABORT, 'previewable spatial package requires zone revision') END; "
            "END"
        ),
        "spatial_package_no_withdraw_while_active": (
            "CREATE TRIGGER spatial_package_no_withdraw_while_active "
            "BEFORE UPDATE OF state ON spatial_package "
            "WHEN NEW.state IS NOT OLD.state "
            "AND NEW.state IN ('WITHDRAWN', 'REVOKED', 'ARCHIVED') "
            "AND EXISTS (SELECT 1 FROM zone_publication "
            "WHERE spatial_package_id = OLD.id AND state = 'PUBLISHED' AND is_active = 1) "
            "BEGIN SELECT RAISE(ABORT, 'active publication must be withdrawn before its package'); END"
        ),
        "spatial_package_no_delete": (
            "CREATE TRIGGER spatial_package_no_delete "
            "BEFORE DELETE ON spatial_package "
            "BEGIN SELECT RAISE(ABORT, 'spatial packages are non-destructive'); END"
        ),
        "zone_publication_insert_valid": (
            "CREATE TRIGGER zone_publication_insert_valid "
            "BEFORE INSERT ON zone_publication "
            "BEGIN "
            "SELECT CASE WHEN NEW.state <> 'DRAFT' OR NEW.is_active <> 0 "
            "THEN RAISE(ABORT, 'zone publication must start as draft') END; "
            "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM spatial_zone_revision "
            "WHERE id = NEW.spatial_zone_revision_id "
            "AND spatial_zone_id = NEW.spatial_zone_id) "
            "THEN RAISE(ABORT, 'zone publication revision must belong to its zone') END; "
            "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM spatial_package "
            "WHERE id = NEW.spatial_package_id "
            "AND spatial_zone_revision_id = NEW.spatial_zone_revision_id "
            "AND state IN ('VERIFIED', 'PREVIEWABLE', 'PUBLISHED') "
            "AND verified_at IS NOT NULL) "
            "THEN RAISE(ABORT, 'zone publication package must match a verified revision') END; "
            "END"
        ),
        "zone_publication_identity_immutable": (
            "CREATE TRIGGER zone_publication_identity_immutable "
            "BEFORE UPDATE OF publication_id, spatial_zone_id, spatial_zone_revision_id, "
            "spatial_package_id, created_at ON zone_publication "
            "BEGIN SELECT RAISE(ABORT, 'zone publication identity is immutable'); END"
        ),
        "zone_publication_no_delete": (
            "CREATE TRIGGER zone_publication_no_delete "
            "BEFORE DELETE ON zone_publication "
            "BEGIN SELECT RAISE(ABORT, 'zone publications are non-destructive'); END"
        ),
        "zone_publication_state_transition": (
            "CREATE TRIGGER zone_publication_state_transition "
            "BEFORE UPDATE OF state ON zone_publication "
            "WHEN NEW.state IS NOT OLD.state "
            "BEGIN "
            f"SELECT CASE WHEN NOT ({_PUBLICATION_STATE_TRANSITION}) "
            "THEN RAISE(ABORT, 'invalid zone publication transition') END; "
            "SELECT CASE WHEN NEW.state = 'PREVIEWABLE' AND NOT EXISTS ("
            "SELECT 1 FROM spatial_package WHERE id = NEW.spatial_package_id "
            "AND spatial_zone_revision_id = NEW.spatial_zone_revision_id "
            "AND state IN ('PREVIEWABLE', 'PUBLISHED')) "
            "THEN RAISE(ABORT, 'previewable publication requires previewable package') END; "
            "SELECT CASE WHEN NEW.state = 'PUBLISHED' AND NOT EXISTS ("
            "SELECT 1 FROM spatial_package WHERE id = NEW.spatial_package_id "
            "AND spatial_zone_revision_id = NEW.spatial_zone_revision_id "
            "AND state = 'PUBLISHED') "
            "THEN RAISE(ABORT, 'published publication requires published package') END; "
            "SELECT CASE WHEN NEW.state = 'PUBLISHED' AND NEW.is_active <> 1 "
            "THEN RAISE(ABORT, 'published publication must be active') END; "
            "SELECT CASE WHEN NEW.state <> 'PUBLISHED' AND NEW.is_active <> 0 "
            "THEN RAISE(ABORT, 'non-published publication must be inactive') END; "
            "END"
        ),
        "zone_publication_reason_actor_requires_transition": (
            "CREATE TRIGGER zone_publication_reason_actor_requires_transition "
            "BEFORE UPDATE OF reason, actor_id ON zone_publication "
            "WHEN NEW.state IS OLD.state "
            "AND (NEW.reason IS NOT OLD.reason OR NEW.actor_id IS NOT OLD.actor_id) "
            "BEGIN SELECT RAISE(ABORT, 'zone publication reason and actor require a transition'); END"
        ),
        "zone_publication_event_transition_valid": (
            "CREATE TRIGGER zone_publication_event_transition_valid "
            "BEFORE INSERT ON zone_publication_event "
            "BEGIN "
            "SELECT CASE WHEN NEW.to_state NOT IN "
            "('DRAFT', 'VERIFIED', 'PREVIEWABLE', 'PUBLISHED', 'WITHDRAWN', 'REVOKED', 'ARCHIVED') "
            "OR (NEW.from_state IS NOT NULL AND NEW.from_state NOT IN "
            "('DRAFT', 'VERIFIED', 'PREVIEWABLE', 'PUBLISHED', 'WITHDRAWN', 'REVOKED', 'ARCHIVED')) "
            "THEN RAISE(ABORT, 'invalid zone publication event state') END; "
            "SELECT CASE WHEN NEW.from_state IS NULL AND (NEW.to_state <> 'DRAFT' "
            "OR NOT EXISTS (SELECT 1 FROM zone_publication "
            "WHERE id = NEW.zone_publication_id AND state = 'DRAFT') "
            "OR EXISTS (SELECT 1 FROM zone_publication_event "
            "WHERE zone_publication_id = NEW.zone_publication_id)) "
            "THEN RAISE(ABORT, 'zone publication requires one draft creation event') END; "
            "SELECT CASE WHEN NEW.from_state IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM zone_publication_event WHERE zone_publication_id = NEW.zone_publication_id "
            "AND from_state IS NULL AND to_state = 'DRAFT') "
            "THEN RAISE(ABORT, 'zone publication transition requires draft creation event') END; "
            "SELECT CASE WHEN NEW.from_state IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM zone_publication WHERE id = NEW.zone_publication_id "
            "AND state = NEW.from_state) "
            "THEN RAISE(ABORT, 'zone publication event must start at current state') END; "
            f"SELECT CASE WHEN NEW.from_state IS NOT NULL AND NOT ({_EVENT_STATE_TRANSITION}) "
            "THEN RAISE(ABORT, 'invalid zone publication event transition') END; "
            "END"
        ),
        "zone_publication_state_update_requires_event": (
            "CREATE TRIGGER zone_publication_state_update_requires_event "
            "BEFORE UPDATE OF state ON zone_publication "
            "WHEN NEW.state IS NOT OLD.state "
            "BEGIN "
            "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM zone_publication_event "
            "WHERE id = (SELECT MAX(id) FROM zone_publication_event "
            "WHERE zone_publication_id = OLD.id) "
            "AND from_state IS OLD.state AND to_state IS NEW.state "
            "AND actor_id IS NEW.actor_id AND reason IS NEW.reason) "
            "THEN RAISE(ABORT, 'zone publication transition requires matching append-only event') END; "
            "END"
        ),
    }


def _create_sqlite_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS spatial_package_identity_immutable")
    for statement in _sqlite_trigger_statements().values():
        op.execute(statement)


def _drop_sqlite_triggers() -> None:
    for name in reversed(tuple(_sqlite_trigger_statements())):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute(_LEGACY_SPATIAL_PACKAGE_IDENTITY_TRIGGER)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _create_sqlite_triggers()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _drop_sqlite_triggers()
