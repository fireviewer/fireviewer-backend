from __future__ import annotations

from fire_viewer.domain.enums import ZonePublicationState
from fire_viewer.domain.errors import ConflictError

ZONE_PUBLICATION_TRANSITIONS: dict[ZonePublicationState, frozenset[ZonePublicationState]] = {
    ZonePublicationState.DRAFT: frozenset({ZonePublicationState.VERIFIED}),
    ZonePublicationState.VERIFIED: frozenset(
        {
            ZonePublicationState.PREVIEWABLE,
            ZonePublicationState.REVOKED,
            ZonePublicationState.ARCHIVED,
        }
    ),
    ZonePublicationState.PREVIEWABLE: frozenset(
        {
            ZonePublicationState.PUBLISHED,
            ZonePublicationState.REVOKED,
            ZonePublicationState.ARCHIVED,
        }
    ),
    ZonePublicationState.PUBLISHED: frozenset(
        {
            ZonePublicationState.WITHDRAWN,
            ZonePublicationState.REVOKED,
            ZonePublicationState.ARCHIVED,
        }
    ),
    ZonePublicationState.WITHDRAWN: frozenset(
        {ZonePublicationState.PUBLISHED, ZonePublicationState.ARCHIVED}
    ),
    ZonePublicationState.REVOKED: frozenset({ZonePublicationState.ARCHIVED}),
    ZonePublicationState.ARCHIVED: frozenset(),
}


def assert_zone_publication_transition(
    current: ZonePublicationState,
    target: ZonePublicationState,
) -> None:
    if target not in ZONE_PUBLICATION_TRANSITIONS[current]:
        raise ConflictError(
            "invalid_zone_publication_transition",
            f"Zone publication cannot transition from {current} to {target}.",
        )


def is_active_zone_publication_state(state: ZonePublicationState) -> bool:
    return state == ZonePublicationState.PUBLISHED
