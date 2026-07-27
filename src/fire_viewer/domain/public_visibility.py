"""Canonical public-visibility rules for incident lifecycle states.

The persisted visibility is intentionally kept separate from the episode status so an
operator can tombstone an incident without inventing a terminal episode status.  For
every non-tombstoned public projection, however, the pair must match this module's
canonical mapping.  Readers fail closed when it does not.
"""

from typing import Final

from fire_viewer.domain.enums import IncidentStatus, PublicVisibility, VerificationState

PUBLIC_EVIDENCE_STATES: Final[frozenset[VerificationState]] = frozenset(
    {VerificationState.CORROBORATED, VerificationState.VERIFIED}
)

# A closed incident may retain its public location but never a live viewer asset or frame.
PUBLIC_LOCATION_STATUSES: Final[frozenset[IncidentStatus]] = frozenset(
    {
        IncidentStatus.CANDIDATE,
        IncidentStatus.UNDER_REVIEW,
        IncidentStatus.ACTIVE_CONFIRMED,
        IncidentStatus.MONITORING,
        IncidentStatus.EXTINGUISHED,
        IncidentStatus.CLOSED,
    }
)
VIEWER_ASSET_STATUSES: Final[frozenset[IncidentStatus]] = frozenset(
    {
        IncidentStatus.ACTIVE_CONFIRMED,
        IncidentStatus.MONITORING,
        IncidentStatus.EXTINGUISHED,
    }
)
WITHHELD_MANIFEST_STATUSES: Final[frozenset[IncidentStatus]] = frozenset(IncidentStatus)


def canonical_public_visibility(
    status: IncidentStatus,
    verification_state: VerificationState = VerificationState.UNVERIFIED,
) -> PublicVisibility:
    """Return the safe visibility for the operational state and evidence basis.

    A lifecycle status is not evidence.  Every non-suspended incident therefore
    remains limited until a human verification or the independent-proof threshold
    has been recorded on its current episode.
    """

    if status == IncidentStatus.SUSPENDED:
        return PublicVisibility.SUSPENDED
    if status == IncidentStatus.REJECTED or verification_state not in PUBLIC_EVIDENCE_STATES:
        return PublicVisibility.LIMITED
    return PublicVisibility.PUBLIC


def has_canonical_public_visibility(
    status: IncidentStatus,
    visibility: PublicVisibility,
    verification_state: VerificationState = VerificationState.UNVERIFIED,
) -> bool:
    """Whether a persisted lifecycle pair is safe to expose publicly.

    ``TOMBSTONED`` deliberately has no status mapping.  The query layer handles it as
    a 410 before asking this policy whether any projection is allowed.
    """

    return visibility == canonical_public_visibility(status, verification_state)


def permits_public_location(
    status: IncidentStatus,
    visibility: PublicVisibility,
    verification_state: VerificationState = VerificationState.UNVERIFIED,
) -> bool:
    """Whether the public query may expose the incident location."""

    return (
        visibility == PublicVisibility.PUBLIC
        and verification_state in PUBLIC_EVIDENCE_STATES
        and status in PUBLIC_LOCATION_STATUSES
        and has_canonical_public_visibility(status, visibility, verification_state)
    )


def permits_public_viewer_asset(
    status: IncidentStatus,
    visibility: PublicVisibility,
    verification_state: VerificationState = VerificationState.UNVERIFIED,
) -> bool:
    """Whether the public manifest may expose a live 3D asset and spatial frame."""

    return (
        verification_state == VerificationState.VERIFIED
        and status in VIEWER_ASSET_STATUSES
        and has_canonical_public_visibility(status, visibility, verification_state)
    )
