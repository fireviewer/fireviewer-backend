from dataclasses import dataclass

from fire_viewer.domain.enums import IncidentStatus


@dataclass(frozen=True, slots=True)
class TransitionRule:
    target: IncidentStatus
    required_roles: frozenset[str]
    requires_validation_basis: bool = False
    require_all_roles: bool = False


_TRANSITIONS: dict[IncidentStatus, tuple[TransitionRule, ...]] = {
    IncidentStatus.CANDIDATE: (
        TransitionRule(IncidentStatus.UNDER_REVIEW, frozenset({"analyst", "validator"})),
        TransitionRule(IncidentStatus.REJECTED, frozenset({"validator"})),
        TransitionRule(
            IncidentStatus.ACTIVE_CONFIRMED,
            frozenset({"validator"}),
            requires_validation_basis=True,
        ),
        TransitionRule(IncidentStatus.SUSPENDED, frozenset({"security_operator"})),
    ),
    IncidentStatus.UNDER_REVIEW: (
        TransitionRule(
            IncidentStatus.ACTIVE_CONFIRMED,
            frozenset({"validator"}),
            requires_validation_basis=True,
        ),
        TransitionRule(IncidentStatus.REJECTED, frozenset({"validator"})),
        TransitionRule(IncidentStatus.SUSPENDED, frozenset({"security_operator"})),
    ),
    IncidentStatus.ACTIVE_CONFIRMED: (
        TransitionRule(IncidentStatus.MONITORING, frozenset({"validator"})),
        TransitionRule(IncidentStatus.EXTINGUISHED, frozenset({"validator"})),
        TransitionRule(IncidentStatus.SUSPENDED, frozenset({"security_operator"})),
    ),
    IncidentStatus.MONITORING: (
        TransitionRule(
            IncidentStatus.ACTIVE_CONFIRMED,
            frozenset({"validator"}),
            requires_validation_basis=True,
        ),
        TransitionRule(IncidentStatus.EXTINGUISHED, frozenset({"validator"})),
        TransitionRule(IncidentStatus.SUSPENDED, frozenset({"security_operator"})),
    ),
    IncidentStatus.EXTINGUISHED: (
        TransitionRule(IncidentStatus.CLOSED, frozenset({"validator"})),
        TransitionRule(IncidentStatus.SUSPENDED, frozenset({"security_operator"})),
    ),
    IncidentStatus.CLOSED: (
        TransitionRule(IncidentStatus.SUSPENDED, frozenset({"security_operator"})),
    ),
    IncidentStatus.SUSPENDED: (
        TransitionRule(IncidentStatus.CANDIDATE, frozenset({"security_operator"})),
        TransitionRule(IncidentStatus.UNDER_REVIEW, frozenset({"security_operator"})),
        TransitionRule(
            IncidentStatus.ACTIVE_CONFIRMED,
            frozenset({"security_operator", "validator"}),
            requires_validation_basis=True,
            require_all_roles=True,
        ),
        TransitionRule(
            IncidentStatus.MONITORING,
            frozenset({"security_operator", "validator"}),
            require_all_roles=True,
        ),
        TransitionRule(
            IncidentStatus.EXTINGUISHED,
            frozenset({"security_operator", "validator"}),
            require_all_roles=True,
        ),
        TransitionRule(
            IncidentStatus.CLOSED,
            frozenset({"security_operator", "validator"}),
            require_all_roles=True,
        ),
    ),
    IncidentStatus.REJECTED: (),
}


def get_transition_rule(current: IncidentStatus, target: IncidentStatus) -> TransitionRule | None:
    return next((rule for rule in _TRANSITIONS[current] if rule.target == target), None)
